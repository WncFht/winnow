"""adapters/tts_local — 本地 TTS 引擎入口（breeze2；worker 子进程 JSONL-RPC）。

与 tts_edge 同一 synth() 合同：产出 {out_dir}/{seg_id}.mp3（24kHz mono 48k），
返回 {"file","dur","boundaries":[]}。引擎依赖（torch/transformers/breeze_infer）
全隔离在 venvs/breeze + tools/tts_workers/breeze-tts——本模块零三方依赖。

worker 生命周期：首次 synth 懒启动（模型载一次，{"ready":true} 握手）；进程级
单例 + threading.Lock 串行化（GPU 本就串行，voice.py ThreadPool 提交进来排队）。
崩溃→下次 synth 自动重启；连续 _MAX_FAILS 次失败抛出。启动前 VRAM 门槛检查。

config.yaml：
  tts:
    engine: breeze
    breeze:
      venv/repo/weights/ref_audio/ref_text/template/guidance_scale/seed/
      max_new_tokens/min_free_gb/ready_timeout_s/synth_timeout_s
    （键缺省走 _DEFAULTS；ref_text 支持 "path.json:key" 从 json 取值）
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_DEFAULTS = {
    "venv": "venvs/breeze/bin/python",
    "repo": "tools/tts_workers/breeze-tts",
    "worker": "tools/tts_workers/breeze.py",
    "weights": "",          # 空 → HF cache models--BreezeBlue--Breeze-TTS-2
    "refs_dir": "state/tts-bakeoff/refs",
    "ref_audio": "",        # 空 → {refs_dir}/g_orig.wav
    "ref_text": "",         # 空 → {refs_dir}/transcripts.json:g_orig；"p:k" 或字面
    "template": "ref_clone_tata",
    "guidance_scale": 1.0,
    "seed": 42,
    "max_new_tokens": 1500,
    "min_free_gb": 8.0,     # eager 实测 ~7.7GiB，留余量
    "ready_timeout_s": 300,
    "synth_timeout_s": 240,
}
_MAX_FAILS = 3

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_cfg_used: dict | None = None


def _load_cfg(config_path) -> dict:
    try:
        import yaml
    except Exception:
        yaml = None
    for p in ([Path(config_path)] if config_path else
              [REPO / "config.yaml", REPO / "config.example.yaml"]):
        if p.is_file() and yaml:
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                pass
    return {}


def breeze_cfg(tts_breeze: dict | None, config_path=None) -> dict:
    """tts.breeze 子键 + 缺省 → 完整 worker 配置（路径全部解析成绝对）。"""
    bc = {**_DEFAULTS, **(tts_breeze or {})}
    refs_dir = Path(bc["refs_dir"])
    if not refs_dir.is_absolute():
        refs_dir = REPO / refs_dir
    bc["refs_dir"] = str(refs_dir)
    if not bc.get("ref_audio"):
        bc["ref_audio"] = str(refs_dir / "g_orig.wav")
    elif not Path(bc["ref_audio"]).is_absolute():
        bc["ref_audio"] = str(REPO / bc["ref_audio"])
    if not bc.get("ref_text"):
        bc["ref_text"] = f"{refs_dir}/transcripts.json:g_orig"
    for k in ("venv", "repo", "worker"):
        p = Path(bc[k])
        bc[k] = str(p if p.is_absolute() else REPO / p)
    bc["weights"] = _resolve_weights(bc.get("weights"))
    bc["ref_text"] = resolve_ref_text(bc["ref_text"])
    return bc


def _resolve_weights(w: str | None) -> str:
    if w:
        p = Path(w).expanduser()
        return str(p if p.is_absolute() else REPO / p)
    hub = (Path.home() / ".cache/huggingface/hub"
           / "models--BreezeBlue--Breeze-TTS-2/snapshots")
    cands = sorted(hub.glob("*/"), key=lambda p: p.stat().st_mtime) \
        if hub.is_dir() else []
    if not cands:
        raise RuntimeError(
            "找不到 Breeze-TTS-2 weights：tts.breeze.weights 未设且 HF cache "
            "无 models--BreezeBlue--Breeze-TTS-2 snapshot")
    return str(cands[-1])


def resolve_ref_text(spec: str) -> str:
    """"path.json:key" → json[key]；否则按字面文本。"""
    if ":" in spec:
        path_s, _, key = spec.rpartition(":")
        p = Path(path_s)
        if p.suffix == ".json" and p.is_file():
            try:
                return str(json.loads(p.read_text(encoding="utf-8"))[key])
            except Exception:
                pass
    return spec


def engine_label(bc: dict) -> str:
    """写进 audio_manifest.engine 的标识——ref/gs/seed/weights 版本都进来，
    参数一变 manifest 比对自动失效（缓存键的 engine 维度）。"""
    ref = Path(bc["ref_audio"]).stem
    w = Path(bc["weights"]).name[:8]
    return (f"breeze2 {ref} gs{bc['guidance_scale']:g} s{bc['seed']} "
            f"w{w}")


def _free_vram_gb() -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return float(out.stdout.strip().splitlines()[0]) / 1024
    except Exception:
        return -1.0


def _kill():
    global _proc
    try:
        if _proc:
            _proc.kill()
    except Exception:
        pass
    _proc = None


def _ensure_worker(bc: dict):
    """懒启动 + ready 握手；VRAM 门槛在 spawn 前。"""
    global _proc
    if _proc and _proc.poll() is None:
        return
    free = _free_vram_gb()
    if 0 <= free < float(bc["min_free_gb"]):
        raise RuntimeError(
            f"GPU 空闲 {free:.1f}G < tts.breeze.min_free_gb="
            f"{bc['min_free_gb']}G（eager 需 ~7.7GiB）——先释放显存或调低门槛")
    py = bc["venv"]
    if not Path(py).is_file():
        raise RuntimeError(f"breeze venv 不存在：{py}（跑 `just setup-breeze`）")
    cmd = [py, bc["worker"], "--repo", bc["repo"],
           "--weights", bc["weights"],
           "--ref-audio", bc["ref_audio"], "--ref-text", bc["ref_text"],
           "--tpl", bc["template"], "--gs", str(bc["guidance_scale"]),
           "--seed", str(bc["seed"]), "--max-new", str(bc["max_new_tokens"])]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    _proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=None, text=True, env=env, cwd=str(REPO))
    # ready 握手：模型加载 ~1-2min
    deadline = time.time() + float(bc["ready_timeout_s"])
    while time.time() < deadline:
        if _proc.poll() is not None:
            _kill()
            raise RuntimeError("breeze worker 启动即退出（stderr 见终端/日志）")
        line = _readline(timeout=max(1, deadline - time.time()))
        if line is None:
            break
        try:
            msg = json.loads(line)
        except Exception:
            continue                    # 第三方库杂输出，跳过非 JSON 行
        if msg.get("ready"):
            return
    _kill()
    raise RuntimeError(f"breeze worker ready 超时（{bc['ready_timeout_s']}s）")


def _readline(timeout: float) -> str | None:
    """stdout 单行读取（select 超时；EOF/超时 → None）。"""
    import select
    fd = _proc.stdout.fileno()
    r, _, _ = select.select([fd], [], [], max(0, timeout))
    if not r:
        return None
    line = _proc.stdout.readline()
    return line if line else None


def _rpc(payload: dict, timeout: float) -> dict:
    assert _proc and _proc.poll() is None
    _proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _proc.stdin.flush()
    line = _readline(timeout)
    if line is None:
        raise RuntimeError("breeze worker 响应超时/EOF")
    return json.loads(line)


def _wav_to_mp3(wav: Path, mp3: Path):
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(wav),
         "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k",
         str(mp3)], capture_output=True, text=True)
    if proc.returncode != 0 or not mp3.is_file() or mp3.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg wav→mp3 失败: {proc.stderr.strip()[:300]}")


def _ffprobe_dur(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)], capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def synth(text: str, seg_id: str, out_dir, *,
          voice=None, rate=None, config_path=None) -> dict:
    """tts_edge 同合同：{out_dir}/{seg_id}.mp3 + dur 实测 + boundaries=[]。"""
    global _cfg_used
    out_dir = Path(out_dir).resolve()   # worker cwd 在 breeze-tts repo，必须绝对路径
    fails = 0
    while True:
        with _lock:
            try:
                if _cfg_used is None:
                    tts = _load_cfg(config_path).get("tts") or {}
                    _cfg_used = breeze_cfg(tts.get("breeze"), config_path)
                _ensure_worker(_cfg_used)
                wav = out_dir / f"{seg_id}.breeze.wav"
                resp = _rpc({"id": seg_id, "text": text, "out": str(wav)},
                            float(_cfg_used["synth_timeout_s"]))
                if not resp.get("ok"):
                    raise RuntimeError(resp.get("err") or "worker synth failed")
                mp3 = out_dir / f"{seg_id}.mp3"
                _wav_to_mp3(wav, mp3)
                try:
                    wav.unlink()
                except OSError:
                    pass
                return {"file": str(mp3), "dur": _ffprobe_dur(mp3),
                        "boundaries": []}
            except Exception:
                fails += 1
                _kill()
                if fails >= _MAX_FAILS:
                    raise
                time.sleep(2)


def shutdown():
    """进程退出前清理 worker（atexit 兜底；voice.py 结束自然回收）。"""
    _kill()


import atexit  # noqa: E402
atexit.register(_kill)
