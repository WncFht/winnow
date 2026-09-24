"""tts.synth 的 edge-tts 实现（PLAN §7.5 / 决策 D2，breeze 之前的主力引擎）。

接口：
    synth(text, seg_id, out_dir, *, voice=None, rate="+0%",
          config_path=None) -> {"file", "dur", "boundaries"}

行为：
- voice 默认取 config.tts.voice（config_path 指定文件 → repo 根
  config.yaml → config.example.yaml → 内置 zh-CN-YunyangNeural；
  YunxiNeural 备选），显式传参可覆盖。rate 只经参数传入（缺省 "+0%"；
  config.tts.rate 由调用方 stages/voice.py 解析，本层不读）。
- 代理解析序：config.tts.proxy > config.proxy.http > 环境变量
  https_proxy/HTTPS_PROXY/all_proxy/ALL_PROXY；皆无则直连。
- 每种 boundary 模式重试 RETRY_TRIES=3 次（退避 1.5s×attempt）；每模式的
  最后一次尝试改走已解析代理——bing 端点间歇掐直连时的兜底路由。
- 每个产出文件必经 ffmpeg atrim 裁残余静音：头 config.tts.trim.head（缺省
  0.20s）、尾 config.tts.trim.tail（缺省 0.78s）——gap-ab 实测值；短到
  裁不动则原样改名不破坏音频。
- dur 由 ffprobe 在裁剪后实测（format=duration）。
- edge-tts 边界事件 → boundaries[{text,start,end}]，时间轴换算到裁剪后
  （offset-head，clamp 到 [0,dur]）：先 WordBoundary，耗尽降级
  SentenceBoundary，仍不行 boundaries=[]；合成全失败抛 TTSError。
- 换引擎只换本文件，接口不变。

自检：uv run adapters/tts_edge.py --selftest
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path


import edge_tts
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOICE = "zh-CN-YunyangNeural"
DEFAULT_HEAD_S = 0.20
DEFAULT_TAIL_S = 0.78
TICKS_PER_SECOND = 10_000_000  # edge-tts offset/duration 单位 = 100ns tick
MP3_BITRATE = "48k"            # 与 edge-tts 输出 audio-24khz-48kbitrate-mono-mp3 对齐


class TTSError(RuntimeError):
    """合成/裁剪失败，交给调用方容错层（D1 同款语义）。"""


def _load_doc(config_path: str | Path | None = None) -> dict:
    """读整份 config；config.yaml 优先，config.example.yaml 兜底，全缺返回 {}。"""
    candidates = [Path(config_path)] if config_path else []
    candidates += [REPO_ROOT / "config.yaml", REPO_ROOT / "config.example.yaml"]
    for p in candidates:
        try:
            if p.is_file():
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


def _load_tts_config(config_path: str | Path | None = None) -> dict:
    tts = _load_doc(config_path).get("tts")
    return tts if isinstance(tts, dict) else {}


def _resolve_proxy(doc: dict) -> str | None:
    """tts.proxy > proxy.http > *_proxy env；皆无则 None（直连）。"""
    tts = doc.get("tts")
    if isinstance(tts, dict) and tts.get("proxy"):
        return str(tts["proxy"])
    px = doc.get("proxy")
    if isinstance(px, dict) and px.get("http"):
        return str(px["http"])
    for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        if os.environ.get(k):
            return os.environ[k]
    return None


def ffprobe_dur(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise TTSError(f"ffprobe failed on {path}: {out.stderr.strip()[:300]}")
    return float(json.loads(out.stdout)["format"]["duration"])


async def _stream_edge(text: str, voice: str, rate: str, raw_path: Path,
                       boundary: str, proxy: str | None = None) -> list[dict]:
    """跑一次 edge-tts 流式合成，audio 落盘，boundary 事件收集为秒。"""
    bounds: list[dict] = []
    comm = edge_tts.Communicate(text, voice=voice, rate=rate, boundary=boundary,
                                proxy=proxy)
    with open(raw_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                start = chunk["offset"] / TICKS_PER_SECOND
                end = (chunk["offset"] + chunk["duration"]) / TICKS_PER_SECOND
                bounds.append({"text": chunk["text"], "start": start, "end": end})
    return bounds


RETRY_TRIES = 3  # 每种 boundary 模式的重试次数；bing 端点间歇性拒连是常态

def _synth_to_raw(text: str, voice: str, rate: str, raw_path: Path,
                  proxy: str | None = None) -> list[dict]:
    """WordBoundary 优先（带退避重试）；耗尽后降级 SentenceBoundary 再试。
    每种模式的最后一次尝试改走 proxy（若配置）——直连被掐时的兜底路由。"""
    last: Exception | None = None
    for boundary in ("WordBoundary", "SentenceBoundary"):
        for attempt in range(RETRY_TRIES):
            px = proxy if (proxy and attempt == RETRY_TRIES - 1) else None
            try:
                return asyncio.run(
                    _stream_edge(text, voice, rate, raw_path, boundary, px))
            except Exception as e:
                last = e
                if raw_path.exists():
                    raw_path.unlink()
                if attempt < RETRY_TRIES - 1:
                    time.sleep(1.5 * (attempt + 1))
    raise TTSError(f"edge-tts synth failed (voice={voice}): {last}") from last


def _trim_silence(raw: Path, final: Path, head: float, tail: float) -> tuple[float, float, bool]:
    """ffmpeg atrim 裁头/尾残余静音。返回 (裁剪后 dur, 原始 dur, 是否真裁了)。"""
    d0 = ffprobe_dur(raw)
    if d0 - head - tail <= 0.05:
        # 短到裁不动：直接改名，不破坏音频
        raw.replace(final)
        return d0, d0, False
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(raw),
        "-af", f"atrim=start={head}:end={d0 - tail}",
        "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE,
        str(final),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not final.is_file() or final.stat().st_size == 0:
        raise TTSError(f"ffmpeg trim failed: {proc.stderr.strip()[:300]}")
    raw.unlink()
    return ffprobe_dur(final), d0, True


def synth(text: str, seg_id: str, out_dir: str | Path, *,
          voice: str | None = None, rate: str = "+0%",
          config_path: str | Path | None = None) -> dict:
    """tts.synth 契约实现（PLAN §7.5）。

    返回 {"file": <mp3 路径 str>, "dur": <裁剪后实测秒>,
          "boundaries": [{"text","start","end"} ...]}（裁剪后时间轴）。
    """
    if not isinstance(text, str) or not text.strip():
        raise TTSError("text must be non-empty str")
    if "/" in seg_id or "\\" in seg_id:
        raise TTSError(f"seg_id must be a bare slug, got {seg_id!r}")

    doc = _load_doc(config_path)
    cfg = doc.get("tts") if isinstance(doc.get("tts"), dict) else {}
    voice = voice or cfg.get("voice") or DEFAULT_VOICE
    proxy = _resolve_proxy(doc)
    trim = cfg.get("trim") if isinstance(cfg.get("trim"), dict) else {}
    head = float(trim.get("head", DEFAULT_HEAD_S))
    tail = float(trim.get("tail", DEFAULT_TAIL_S))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{seg_id}.raw.mp3"
    final = out_dir / f"{seg_id}.mp3"

    bounds = _synth_to_raw(text, voice, rate, raw, proxy)
    dur, _raw_dur, trimmed = _trim_silence(raw, final, head, tail)

    # 边界时间轴换算到裁剪后：整体左移 head，clamp 进 [0, dur]
    shift = head if trimmed else 0.0
    boundaries = []
    for b in bounds:
        s = max(0.0, b["start"] - shift)
        e = min(dur, b["end"] - shift)
        if e > s:
            boundaries.append({"text": b["text"], "start": round(s, 3),
                               "end": round(e, 3)})

    return {"file": str(final), "dur": dur, "boundaries": boundaries}


def _selftest(out_dir: str | Path, voice: str | None) -> int:
    """LIVE TEST：真实合成一句，断言出文件、dur>0、头尾确实被裁、有边界。"""
    text = "DeepSeek发布新模型，API价格下调百分之三十。"
    seg_id = "000_selftest_0"
    cfg = _load_tts_config()
    eff_voice = voice or cfg.get("voice") or DEFAULT_VOICE
    trim = cfg.get("trim") if isinstance(cfg.get("trim"), dict) else {}
    head = float(trim.get("head", DEFAULT_HEAD_S))
    tail = float(trim.get("tail", DEFAULT_TAIL_S))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"{seg_id}.raw.mp3"
    final = out_dir / f"{seg_id}.mp3"
    for p in (raw, final):
        if p.exists():
            p.unlink()

    bounds = _synth_to_raw(text, eff_voice, "+0%", raw)
    d0 = ffprobe_dur(raw)
    dur, _d0b, trimmed = _trim_silence(raw, final, head, tail)
    assert _d0b == d0

    shift = head if trimmed else 0.0
    boundaries = [
        {"text": b["text"], "start": round(max(0.0, b["start"] - shift), 3),
         "end": round(min(dur, b["end"] - shift), 3)}
        for b in bounds
        if min(dur, b["end"] - shift) > max(0.0, b["start"] - shift)
    ]
    result = {"file": str(final), "dur": dur, "boundaries": boundaries}

    ok = True
    checks = {
        "file_exists": final.is_file() and final.stat().st_size > 0,
        "dur_gt_0": dur > 0,
        "trimmed": trimmed and abs((d0 - dur) - (head + tail)) < 0.20,
        "boundaries_nonempty": len(boundaries) > 0,
    }
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        ok = ok and v
    print(json.dumps({
        "voice": eff_voice, "raw_dur": round(d0, 3),
        "trim": {"head": head, "tail": tail},
        "result": {**result, "boundaries": result["boundaries"][:8],
                            "n_boundaries": len(boundaries)},
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="tts_edge adapter selftest / one-shot synth")
    ap.add_argument("--selftest", action="store_true",
                    help="live synth 测试句 + 断言裁剪生效")
    ap.add_argument("--text", default=None, help="一次性合成文本")
    ap.add_argument("--seg-id", default="000_selftest_0")
    ap.add_argument("--out-dir", default=str(REPO_ROOT / "out" / "tts_edge_selftest"))
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", default="+0%")
    args = ap.parse_args()

    if args.selftest:
        return _selftest(args.out_dir, args.voice)
    if args.text:
        r = synth(args.text, args.seg_id, args.out_dir,
                  voice=args.voice, rate=args.rate)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
