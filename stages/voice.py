# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "edge-tts>=7.2",
#   "pyyaml>=6",
#   "pydantic>=2",
# ]
# ///
"""stages/voice.py — PLAN §7.5：50_issue.json → 口播音频 + 时间轴 + 字幕投影。

产物（§4 契约）：
  60_voice_script.jsonl    voice_seg/1  {seg_id=NNN_item_si,item,si,text,role}
  61_audio/<seg_id>.mp3    逐句合成（tts.engine 分派 edge/breeze——
                           adapters/tts_edge | adapters/tts_local；
                           edge 残余静音已裁——见 tts_edge）
  61_audio/<seg_id>.words.json   词边界 sidecar（断点续跑保留 words 高亮数据）
  61_audio/<seg_id>.textsha      text_sha sidecar（manifest 缺失时裸 mp3 也能续跑）
  61_audio_manifest.json   audio_manifest/1  {engine,voice,rate,codec,sample_rate,
                           files[{seg_id,file,dur,sha256,text_sha}]}
  62_timeline.json         timeline/1  {total,lead_in,tail,gap,items,segs,overlays}
  62_episode.srt/.vtt      generated-header 只读投影
  61_audio/voice_full.wav  loudnorm 归一的整片备用轨（= lead_in + segs + gaps + tail）

流程：issue.intro.voice → items[].voice → issue.outro.voice 拍平成有序 seg
序列（intro/body/outro 角色按位置）；ttsnorm.normalize 过 tts_dict + 连字符
规则；逐句 adapter.synth（config tts.engine 分派 edge/breeze；缓存键含
engine 维度）——两级复用：manifest.files[].text_sha 校验命中 →
裸 mp3 + <seg_id>.textsha sidecar（manifest 缺失/中途崩溃仍能续跑），断点
续跑/词典微调只重合成受影响句子；ffprobe 实测 dur 推绝对时间轴；
shot_sentences(1-based 句区间) 编译成 overlays 绝对时间窗；最后 ffmpeg
装配 voice_full.wav。

时间轴参数：lead_in/tail/gap.sentence/gap.item 取 CLI flag > config.timeline
段 > 校准缺省（0.6/0.8/0.15/0.55，§7.5 校准值）。

CLI：
  uv run stages/voice.py --run-dir runs/<date> [--config config.yaml]
      [--jobs 4] [--voice zh-CN-...] [--rate "+0%"] [--tts-dict PATH]
      [--lead-in S] [--tail S] [--gap-sentence S] [--gap-item S]
      [--force] [--dry-run]
  uv run stages/voice.py --selftest    # fixture→真合成端到端冒烟
                                       #（引擎随 config tts.engine，缺省 edge）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root


from adapters import tts_edge  # noqa: E402
from contracts.models import AudioManifest, Timeline, VoiceSeg  # noqa: E402
from lib import meta, prog, ttsnorm  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

F_ISSUE = "50_issue.json"
F_SCRIPT = "60_voice_script.jsonl"
AUDIO_DIR = "61_audio"
F_MANIFEST = "61_audio_manifest.json"
F_TIMELINE = "62_timeline.json"
F_SRT = "62_episode.srt"
F_VTT = "62_episode.vtt"
FULL_WAV = f"{AUDIO_DIR}/voice_full.wav"
WORDS_SUFFIX = ".words.json"          # boundaries sidecar：断点续跑保留 words
SHA_SUFFIX = ".textsha"               # text_sha sidecar：manifest 缺失时裸 mp3 也能续跑
TTS_DICT = REPO / "state" / "tts_dict.yaml"

# §7.5 校准值；config.yaml `timeline:` 段或 CLI 可覆盖
D_LEAD_IN, D_TAIL = 0.6, 0.8
D_GAP_SENTENCE, D_GAP_ITEM = 0.15, 0.55

_SLUG_RE = re.compile(r"^[a-z0-9-]{2,24}$")
_EPISODE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------

def _die(msg: str, hint: str = "") -> "SystemExit":
    sys.stderr.write(f"ERROR: {msg}\n")
    if hint:
        sys.stderr.write(f"HINT: {hint}\n")
    return SystemExit(2)


def _need(run_dir: Path, name: str, hint: str) -> Path:
    p = run_dir / name
    if not p.exists():
        raise _die(f"{run_dir}/{name} 缺失", hint)
    return p


def _warn(msg: str) -> None:
    sys.stderr.write(f"WARN  {msg}\n")


def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha_key(engine_id: str, text: str) -> str:
    """sidecar 复用键：engine|voice|rate 维度 + 文本——换引擎/参数不会误吃旧 mp3。"""
    return _sha_text(f"{engine_id}\x00{text}")[:16]


def _load_cfg(path: str | Path | None) -> dict:
    """config.yaml > config.example.yaml（与 collect/justfile 规则一致）。"""
    return meta.load_config(path)


def _timeline_cfg(cfg: dict, args) -> dict:
    """lead_in/tail/gap{sentence,item}：CLI > config.timeline > 校准缺省。

    config 形态（任选）：
        timeline: {lead_in: 0.6, tail: 0.8, gap: {sentence: 0.15, item: 0.55}}
        timeline: {lead_in: ..., gap_sentence: ..., gap_item: ...}   # 平铺也行
    """
    tl = cfg.get("timeline") if isinstance(cfg.get("timeline"), dict) else {}
    gap = tl.get("gap") if isinstance(tl.get("gap"), dict) else {}

    def pick(cli, *candidates, default):
        if cli is not None:
            return float(cli)
        for v in candidates:
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return default

    return {
        "lead_in": pick(args.lead_in, tl.get("lead_in"), default=D_LEAD_IN),
        "tail": pick(args.tail, tl.get("tail"), default=D_TAIL),
        "gap_sentence": pick(args.gap_sentence, gap.get("sentence"),
                             tl.get("gap_sentence"), default=D_GAP_SENTENCE),
        "gap_item": pick(args.gap_item, gap.get("item"),
                         tl.get("gap_item"), default=D_GAP_ITEM),
    }


# ---------------------------------------------------------------------------
# 1) 50_issue.json → 60_voice_script.jsonl
# ---------------------------------------------------------------------------

def _voice_list(node) -> list:
    """issue 里 voice 字段统一成 [str]；容错 dict 形 {"text":...}。"""
    out = []
    for s in (node or []):
        if isinstance(s, dict):
            s = s.get("text", "")
        s = str(s or "")
        if s.strip():
            out.append(s)
    return out


def _voice_node(issue: dict, key: str) -> list:
    """issue.intro/.outro：{voice:[...]} 或裸 list 都接受。"""
    node = issue.get(key)
    if isinstance(node, dict):
        node = node.get("voice")
    return _voice_list(node)


def flatten_plan(issue: dict) -> list:
    """→ [(item_id, role, [raw_texts])]，序即正片序：intro→items→outro。"""
    plan = []
    intro = _voice_node(issue, "intro")
    if intro:
        plan.append(("intro", "intro", intro))
    for it in issue.get("items", []) or []:
        iid = it.get("id")
        texts = _voice_list(it.get("voice"))
        if not texts:
            _warn(f"item {iid}: voice[] 为空，本条无口播（不占时间轴）")
            continue
        plan.append((iid, "body", texts))
    outro = _voice_node(issue, "outro")
    if outro:
        plan.append(("outro", "outro", outro))
    return plan


def build_script(plan: list, pron: dict) -> list:
    """拍平+规范化 → voice_seg/1 行（pydantic lint 每行）。"""
    rows = []
    n = 0
    for iid, role, texts in plan:
        if not _SLUG_RE.fullmatch(iid):
            raise _die(f"item id {iid!r} 不符合 slug 规范（voice_seg.item 契约）")
        for si, raw in enumerate(texts):
            text = ttsnorm.normalize(raw, pron)
            if not text:
                _warn(f"{iid}[{si}] 规范化后为空，已跳过")
                continue
            row = {"schema": "voice_seg/1", "seg_id": f"{n:03d}_{iid}_{si}",
                   "item": iid, "si": si, "text": text,
                   "text_display": raw if raw != text else None,
                   "role": role}
            VoiceSeg.model_validate(row)          # 契约 lint（extra=forbid）
            rows.append(row)
            n += 1
    return rows


# ---------------------------------------------------------------------------
# 2) 逐句合成（断点续跑：同 text_sha 的 mp3 直接复用）
# ---------------------------------------------------------------------------

def _synth_all(rows: list, run_dir: Path, *, adapter, engine_id: str,
               eng_label: str, voice: str, rate: str | None,
               jobs: int, force: bool, config_path, p) -> dict:
    """→ {seg_id: {file,dur,boundaries}}；缺失/文本变了才真合成。"""
    audio_dir = run_dir / AUDIO_DIR
    audio_dir.mkdir(parents=True, exist_ok=True)

    old: dict = {}
    mp = run_dir / F_MANIFEST
    if mp.exists() and not force:
        try:
            mo = json.loads(mp.read_text(encoding="utf-8"))
            # engine 维度：manifest 级比对——引擎/嗓音/速率任一变化即全量重合成
            if (mo.get("engine") == eng_label and mo.get("voice") == voice
                    and mo.get("rate") == rate):
                old = {f["seg_id"]: f for f in mo.get("files", [])}
            elif mo.get("files"):
                _warn(f"61 manifest 引擎参数变化（{mo.get('engine')} "
                      f"{mo.get('voice')}/{mo.get('rate')} → {eng_label} "
                      f"{voice}/{rate}）——旧 mp3 不复用")
        except Exception:
            old = {}

    results: dict = {}
    todo = []
    for r in rows:
        sid = r["seg_id"]
        ent = old.get(sid)
        sha = _sha_text(r["text"])[:16]
        key = _sha_key(engine_id, r["text"])
        # 两级复用：manifest 命中（engine 门 + text_sha 校验）→ 裸 mp3 +
        # .textsha sidecar（engine_id 复合键；manifest 缺失/中途崩溃时续跑）。
        fpath = None
        if (ent and ent.get("text_sha") == sha
                and (run_dir / ent.get("file", "")).is_file()):
            fpath = run_dir / ent["file"]
        else:
            mp3, sp = audio_dir / f"{sid}.mp3", audio_dir / f"{sid}{SHA_SUFFIX}"
            try:
                if (mp3.is_file() and sp.is_file()
                        and sp.read_text(encoding="utf-8").strip() == key):
                    fpath = mp3
            except OSError:
                pass
        if fpath is not None:
            words = []
            wp = audio_dir / f"{sid}{WORDS_SUFFIX}"
            if wp.is_file():
                try:
                    words = json.loads(wp.read_text(encoding="utf-8"))
                except Exception:
                    words = []
            results[sid] = {"file": f"{AUDIO_DIR}/{sid}.mp3",
                            "dur": tts_edge._ffprobe_dur(fpath),
                            "boundaries": words}
            continue
        todo.append(r)

    if todo:
        print(f"synth: {len(todo)} segs to synth, {len(results)} reused "
              f"(jobs={jobs}, voice={voice} rate={rate})")

    def one(r):
        res = adapter.synth(r["text"], r["seg_id"], audio_dir,
                            voice=voice, rate=rate, config_path=config_path)
        return r["seg_id"], res

    p.total = len(todo)                    # 复用命中后 todo 才是真分母
    errs = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = [ex.submit(one, r) for r in todo]
        for i, fut in enumerate(futs):
            try:
                sid, res = fut.result()
                res["file"] = f"{AUDIO_DIR}/{sid}.mp3"
                results[sid] = res
                meta.atomic_write(audio_dir / f"{sid}{WORDS_SUFFIX}",
                                  res.get("boundaries") or [])
                meta.atomic_write(audio_dir / f"{sid}{SHA_SUFFIX}",
                                  _sha_key(engine_id, todo[i]["text"]) + "\n")
            except Exception as e:  # 已落盘的 seg 下轮复用
                errs.append(str(e))
            p.tick(i + 1, todo[i]["seg_id"], force=i + 1 == len(todo))
    if errs:
        raise _die(f"{len(errs)} 句合成失败：{errs[0]}",
                   "修复/稍后重跑 `just voice`——已合成的 seg 会复用不重打")
    return results


def _probe_sample_rate(p: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True)
    try:
        return int(out.stdout.strip().split(",")[0])
    except Exception:
        return 24000   # edge-tts audio-24khz-mono 实测值兜底


def write_manifest(rows: list, results: dict, run_dir: Path, episode: str,
                   voice: str, rate, eng: str) -> dict:
    files = []
    for r in rows:
        sid = r["seg_id"]
        res = results[sid]
        fpath = run_dir / res["file"]
        files.append({"seg_id": sid, "file": res["file"],
                      "dur": round(float(res["dur"]), 3),
                      "sha256": meta.sha256_file(fpath),
                      "text_sha": _sha_text(r["text"])[:16]})
    manifest = {"schema": "audio_manifest/1", "episode": episode,
                "engine": eng, "voice": voice, "rate": rate,
                "codec": "mp3",
                "sample_rate": _probe_sample_rate(run_dir / files[0]["file"])
                if files else 24000,
                "files": files}
    AudioManifest.model_validate(manifest)          # 契约 lint
    meta.atomic_write(run_dir / F_MANIFEST, manifest)
    return manifest


# ---------------------------------------------------------------------------
# 3) 62_timeline.json + overlays
# ---------------------------------------------------------------------------

def build_timeline(rows: list, results: dict, issue: dict, tl_cfg: dict,
                   episode: str) -> tuple:
    """seg.start 全精度累加，落档 round 到 ms；dur 取 round(end-start) 保持一致。"""
    lead_in, tail = tl_cfg["lead_in"], tl_cfg["tail"]
    gap_s, gap_i = tl_cfg["gap_sentence"], tl_cfg["gap_item"]

    # 保序分组（rows 已按 intro→items→outro、si 升序）
    groups: list = []          # [(iid, [row,...])]
    for r in rows:
        if groups and groups[-1][0] == r["item"]:
            groups[-1][1].append(r)
        else:
            groups.append((r["item"], [r]))

    segs: list = []
    items_tl: list = []
    seg_exact: dict = {}       # seg_id → (start_exact, dur_exact)
    t = float(lead_in)
    n = 0
    for gi, (iid, grows) in enumerate(groups):
        if gi > 0:
            t += gap_i
        span_start = None
        for j, r in enumerate(grows):
            if j > 0:
                t += gap_s
            dur = float(results[r["seg_id"]]["dur"])
            start_exact = t
            t += dur
            seg = {"n": n, "seg_id": r["seg_id"], "item": iid, "si": r["si"],
                   "file": results[r["seg_id"]]["file"], "text": r["text"],
                   "start": round(start_exact, 3), "end": round(t, 3)}
            if r.get("text_display"):
                seg["text_display"] = r["text_display"]
            seg["dur"] = round(seg["end"] - seg["start"], 3)
            words = results[r["seg_id"]].get("boundaries") or []
            if words:           # edge WordBoundary → 绝对秒（逐词高亮预留）
                seg["words"] = [{"text": w["text"],
                                 "start": round(start_exact + w["start"], 3),
                                 "end": round(start_exact + w["end"], 3)}
                                for w in words
                                if w.get("end", 0) > w.get("start", -1)]
                if not seg["words"]:
                    del seg["words"]
            segs.append(seg)
            seg_exact[r["seg_id"]] = (start_exact, dur)
            span_start = start_exact if span_start is None else span_start
            n += 1
        # outro span 复用前一条（最后一张内容卡）；其余 visual=null=自身
        visual = items_tl[-1]["id"] if iid == "outro" and items_tl else None
        items_tl.append({"id": iid, "start": round(span_start, 3),
                         "end": round(t, 3), "visual": visual})

    total = t + tail

    # shot_sentences（1-based 句区间）→ overlay 绝对时间窗
    segs_by_item: dict = {}
    for s in segs:
        segs_by_item.setdefault(s["item"], []).append(s)
    overlays = []
    for it in issue.get("items", []) or []:
        shot_s = (it.get("video") or {}).get("shot_sentences") or []
        own = segs_by_item.get(it.get("id"), [])
        if not shot_s or not own:
            continue
        want = set()
        for x in shot_s:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= xi <= len(own):
                want.add(xi - 1)                      # 1-based → si
            else:
                _warn(f"{it['id']}: shot_sentence {x} 越界 seg {len(own)}，已丢")
        hit = [s for s in own if s["si"] in want]
        if not hit:
            continue
        overlays.append({"kind": "shot", "item": it["id"],
                         "src": f"64_frames/{it['id']}.shot.png",
                         "start": min(s["start"] for s in hit),
                         "end": max(s["end"] for s in hit),
                         "at_sentences": sorted(want_i + 1 for want_i in want)})

    tl = {"schema": "timeline/1", "episode": episode, "time_unit": "seconds",
          "total": round(total, 3), "lead_in": lead_in, "tail": tail,
          "gap": {"sentence": gap_s, "item": gap_i},
          "items": items_tl, "segs": segs, "overlays": overlays}
    Timeline.model_validate(tl)                     # 契约 lint
    return tl, seg_exact


# ---------------------------------------------------------------------------
# 4) 投影：62_episode.srt / .vtt（generated-header 只读）
# ---------------------------------------------------------------------------

def _ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_vtt(t: float) -> str:
    return _ts_srt(t).replace(",", ".")


def write_projections(tl: dict, run_dir: Path) -> None:
    hdr = "# generated by stages/voice.py from 62_timeline.json — do not edit\n"
    srt = hdr + "\n".join(
        f"{s['n'] + 1}\n{_ts_srt(s['start'])} --> {_ts_srt(s['end'])}\n"
        f"{s.get('text_display') or s['text']}\n" for s in tl["segs"])
    meta.atomic_write(run_dir / F_SRT, srt)

    vtt = ("WEBVTT\n"
           "NOTE generated by stages/voice.py from 62_timeline.json"
           " — do not edit\n\n" + "\n".join(
               f"{_ts_vtt(s['start'])} --> {_ts_vtt(s['end'])}\n"
               f"{s.get('text_display') or s['text']}\n"
               for s in tl["segs"]))
    meta.atomic_write(run_dir / F_VTT, vtt)


# ---------------------------------------------------------------------------
# 5) voice_full.wav —— loudnorm 归一整片备用轨（时长 == timeline.total）
# ---------------------------------------------------------------------------

def build_full_wav(tl: dict, seg_exact: dict, run_dir: Path) -> Path | None:
    segs = tl["segs"]
    if not segs:
        return None
    total, lead_in = tl["total"], tl["lead_in"]
    out = run_dir / FULL_WAV
    out.parent.mkdir(parents=True, exist_ok=True)

    has_lead = lead_in > 0.001                    # lead_in=0 时不挂静音输入
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    off = 0
    if has_lead:
        cmd += ["-f", "lavfi", "-t", f"{lead_in:.4f}",
                "-i", "anullsrc=r=48000:cl=mono"]
        off = 1
    for s in segs:
        cmd += ["-i", str(run_dir / s["file"])]

    parts = []
    labels = []
    for i, s in enumerate(segs):
        start_exact, _dur = seg_exact[s["seg_id"]]
        # slot = 本句 start → 下句 start（末句 → total，含 tail 静默）
        slot = ((seg_exact[segs[i + 1]["seg_id"]][0]) if i + 1 < len(segs)
                else total) - start_exact
        parts.append(
            f"[{i + off}:a]aformat=sample_rates=48000:channel_layouts=mono,"
            f"atrim=0:{slot:.4f},apad=whole_dur={slot:.4f}[p{i}]")
        labels.append(f"[p{i}]")
    head = "[sil]" if has_lead else ""
    cat = head + "".join(labels)
    if has_lead:
        parts.insert(0, "[0:a]aformat=sample_rates=48000:channel_layouts=mono[sil]")
    # loudnorm 会带非零起始 PTS（内部延迟）——必须先 asetpts 归零再 atrim，
    # 否则输出比 total 短 ~66ms（实测）。
    parts.append(
        f"{cat}concat=n={len(segs) + (1 if has_lead else 0)}:v=0:a=1,"
        f"apad=whole_dur={total + 0.5:.4f},"
        f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
        f"asetpts=PTS-STARTPTS,atrim=0:{total:.4f},apad=whole_dur={total:.4f}[aout]")
    cmd += ["-filter_complex", ";".join(parts),
            "-map", "[aout]", "-ar", "48000", "-ac", "1",
            "-c:a", "pcm_s16le", str(out)]

    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise _die(f"ffmpeg voice_full.wav 装配失败: {proc.stderr.strip()[:400]}")
    return out


# ---------------------------------------------------------------------------
# stage entry
# ---------------------------------------------------------------------------

def run_voice(run_dir: Path, cfg: dict, args) -> dict:
    issue_p = _need(run_dir, F_ISSUE,
                    "先跑 digest（Call B 回填 voice[]）：`just digest` + `just edit-import`")
    issue = json.loads(issue_p.read_text(encoding="utf-8"))

    tts_cfg = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
    engine_req = str(tts_cfg.get("engine") or "edge").lower()
    if engine_req in ("edge", "edge-tts", "edge_tts"):
        adapter = tts_edge
        voice = args.voice or tts_cfg.get("voice") or tts_edge.DEFAULT_VOICE
        rate = args.rate or str(tts_cfg.get("rate") or "+0%")
        jobs = args.jobs
        try:
            eng_label = f"edge-tts {importlib.metadata.version('edge-tts')}"
        except Exception:
            eng_label = "edge-tts"
    elif engine_req in ("breeze", "breeze2", "breeze-tts"):
        from adapters import tts_local
        adapter = tts_local
        bc = tts_local.breeze_cfg(tts_cfg.get("breeze"), args.config)
        if args.voice:                     # --voice 选 ref：refs/<v>.wav+transcripts[v]
            rd = Path(bc["refs_dir"])
            bc["ref_audio"] = str(rd / f"{args.voice}.wav")
            bc["ref_text"] = tts_local._resolve_ref_text(
                f"{rd}/transcripts.json:{args.voice}")
        voice = args.voice or Path(bc["ref_audio"]).stem
        rate = None                        # 本地引擎无 rate 概念
        jobs = 1                           # GPU 串行（worker 锁排队）
        eng_label = tts_local.engine_label(bc)
    else:
        raise _die(f"tts.engine={engine_req} 未实现（edge|breeze）",
                   "config.yaml tts.engine 设 edge 或 breeze（breeze 见 "
                   "docs/PLAN.md §7.5）")
    tl_cfg = _timeline_cfg(cfg, args)
    episode = str(issue.get("date") or "")
    if not _EPISODE_RE.fullmatch(episode):
        episode = run_dir.name

    pron = ttsnorm.load_dict(args.tts_dict or TTS_DICT)
    plan = flatten_plan(issue)
    rows = build_script(plan, pron)
    if not rows:
        raise _die("50_issue.json 无可用 voice 句（items/intro/outro 全空）",
                   "先跑 `just digest --callb` 回填 voice[]")
    meta.atomic_write(
        run_dir / F_SCRIPT,
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"60_voice_script: {len(rows)} segs "
          f"({sum(1 for r in rows if r['role'] == 'intro')} intro / "
          f"{sum(1 for r in rows if r['role'] == 'body')} body / "
          f"{sum(1 for r in rows if r['role'] == 'outro')} outro)")

    if args.dry_run:
        print("--dry-run：只写 60_voice_script.jsonl，不合成")
        return {"segs": len(rows), "dry_run": True}

    p = prog.Prog(run_dir, "voice", step=5, interval=30.0)
    engine_id = f"{eng_label}|{voice}|{rate}"
    results = _synth_all(rows, run_dir, adapter=adapter, engine_id=engine_id,
                         eng_label=eng_label, voice=voice, rate=rate,
                         jobs=jobs, force=args.force,
                         config_path=args.config, p=p)
    manifest = write_manifest(rows, results, run_dir, episode, voice, rate,
                              eng_label)
    print(f"61_audio_manifest: {len(manifest['files'])} files, "
          f"engine={manifest['engine']} voice={manifest['voice']}")

    tl, seg_exact = build_timeline(rows, results, issue, tl_cfg, episode)
    meta.atomic_write(run_dir / F_TIMELINE, tl)
    write_projections(tl, run_dir)
    print(f"62_timeline: total={tl['total']:.3f}s "
          f"(lead_in={tl_cfg['lead_in']} tail={tl_cfg['tail']} "
          f"gap.s={tl_cfg['gap_sentence']} gap.i={tl_cfg['gap_item']}), "
          f"{len(tl['items'])} item spans, {len(tl['overlays'])} shot overlays")

    p.say(f"ffmpeg 装配 voice_full.wav：{len(tl['segs'])} segs, "
          f"total={tl['total']:.1f}s")
    wav = build_full_wav(tl, seg_exact, run_dir)
    if wav:
        p.say(f"voice_full.wav 完成 {wav.stat().st_size / 1024:.0f} KiB")
        print(f"voice_full.wav: {meta.sha256_file(wav)[:16]}… "
              f"{wav.stat().st_size / 1024:.0f} KiB")
    p.close()
    return {"segs": len(rows), "total": tl["total"],
            "manifest": manifest, "timeline": tl}


def _selftest() -> int:
    """LIVE TEST：fixture issue（3 items×2 voice + intro/outro）→ 真合成 →
    断言 manifest.text_sha==sha256(60.text)[:16]、timeline.total==Σdurs+gaps±0.1、
    srt 非空、voice_full.wav 时长==total±0.5。"""
    run_dir = meta.ensure_run("_voice_smoke")
    fixture = {
        "schema": "issue/1", "date": "2026-09-22", "weekday": "周二",
        "lang": "zh-CN",
        "sections": [{"slug": "model-release", "name": "模型发布"}],
        "items": [
            {"id": "alpha", "section": "model-release", "nav": "甲",
             "headline": "甲实验室发布 Alpha 模型", "tldr": "tldr",
             "body": ["b"], "confidence": "confirmed",
             "facts": ["支持 1M 上下文", "已在 GPT 6 榜单现身"],
             "sources": [{"url": "https://example.com/a", "kind": "official",
                          "primary": True}],
             "voice": ["甲实验室发布Alpha模型，API价格下调百分之三十。",
                       "新模型支持1M上下文，已在GPT 6榜单现身。"],
             "video": {"shot_sentences": [1]}},
            {"id": "beta", "section": "model-release", "nav": "乙",
             "headline": "乙公司开源 Beta", "tldr": "t",
             "body": ["b"], "confidence": "reported",
             "facts": ["可训练参数仅 70.6 万"],
             "sources": [{"url": "https://example.com/b", "kind": "repo",
                          "primary": True}],
             "voice": ["乙公司开源Beta框架，可训练参数仅70.6万。",
                       "代码与权重已开放，支持本地运行。"]},
            {"id": "gamma", "section": "model-release", "nav": "丙",
             "headline": "丙发布 Gamma-2", "tldr": "t",
             "body": ["b"], "confidence": "confirmed",
             "facts": ["发布 Gamma 2 芯片", "吞吐提升约 96%"],
             "sources": [{"url": "https://example.com/c", "kind": "media",
                          "primary": True}],
             "voice": ["丙发布Gamma-2芯片，吞吐提升约96%。",
                       "面向Agent任务优化，基于国产算力训练。"],
             "video": {"shot_sentences": [2]}},
        ],
        "intro": {"voice": ["各位观众早上好，欢迎收看AI早报。"]},
        "outro": {"voice": ["今天的资讯播送完了，明天见。"]},
    }
    meta.atomic_write(run_dir / F_ISSUE, fixture)
    print(f"selftest fixture → {run_dir / F_ISSUE}")

    class A:  # 默认参数
        config = None; jobs = 4; force = False; dry_run = False
        rate = None; voice = None; tts_dict = None
        lead_in = tail = gap_sentence = gap_item = None
    with meta.run_lock(run_dir):
        out = run_voice(run_dir, _load_cfg(None), A)

    # ---- asserts ----
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")

    script = meta.load_jsonl(run_dir / F_SCRIPT)
    manifest = json.loads((run_dir / F_MANIFEST).read_text())
    tl = json.loads((run_dir / F_TIMELINE).read_text())

    chk("8 segs (intro1+3×2+outro1)", len(script) == 8, f"got {len(script)}")
    by_seg = {f["seg_id"]: f for f in manifest["files"]}
    chk("manifest.files == script.seg_id 集合",
        set(by_seg) == {r["seg_id"] for r in script})
    chk("text_sha 对应 60 文本",
        all(by_seg[r["seg_id"]]["text_sha"] == _sha_text(r["text"])[:16]
            for r in script))
    expect = (tl["lead_in"] + tl["tail"]
              + sum(f["dur"] for f in manifest["files"])
              + tl["gap"]["sentence"] * (len(script) - len(tl["items"]))
              + tl["gap"]["item"] * (len(tl["items"]) - 1))
    chk("timeline.total == Σdurs+gaps ±0.1",
        abs(tl["total"] - expect) <= 0.1,
        f"total={tl['total']:.3f} expect={expect:.3f}")
    srt = (run_dir / F_SRT).read_text()
    chk("srt 非空且含箭头", "-->" in srt and len(srt) > 100)
    wav = run_dir / FULL_WAV
    wav_dur = tts_edge._ffprobe_dur(wav) if wav.is_file() else 0.0
    chk("voice_full.wav 存在且时长==total±0.05",
        wav.is_file() and abs(wav_dur - tl["total"]) <= 0.05,
        f"wav={wav_dur:.3f} total={tl['total']:.3f}")
    # ttsnorm 生效痕迹：GPT 6 连字符已拆、API 词典命中
    joined = " ".join(r["text"] for r in script)
    chk("ttsnorm: 无字母-数字连字符", not ttsnorm.LETTER_DIGIT_HYPHEN.search(joined))
    # 幂等重跑：manifest 复用路径不炸
    with meta.run_lock(run_dir):
        out2 = run_voice(run_dir, _load_cfg(None), A)
    chk("幂等重跑 segs 数一致", out2["segs"] == out["segs"])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="voice stage (PLAN §7.5)")
    ap.add_argument("--run-dir", type=Path, default=None, help="runs/<date>")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--jobs", type=int, default=4, help="并发合成数")
    ap.add_argument("--voice", default=None, help="覆盖 config.tts.voice")
    ap.add_argument("--rate", default=None, help="edge-tts rate，如 +15%")
    ap.add_argument("--tts-dict", type=Path, default=None,
                    help="发音词典，缺省 state/tts_dict.yaml")
    ap.add_argument("--lead-in", type=float, default=None)
    ap.add_argument("--tail", type=float, default=None)
    ap.add_argument("--gap-sentence", type=float, default=None)
    ap.add_argument("--gap-item", type=float, default=None)
    ap.add_argument("--force", action="store_true", help="全部重合成（弃缓存）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只规范化+写 60_voice_script.jsonl，不打 TTS")
    ap.add_argument("--selftest", action="store_true",
                    help="fixture + 真合成端到端冒烟（引擎随 tts.engine）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.run_dir:
        ap.error("--run-dir 必填（或 --selftest）")
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = REPO / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    with meta.run_lock(run_dir):
        # dry-run 不走 stage_done —— 只在真实运行登记，否则留假墓碑
        if not args.dry_run:
            meta.stage_begin(run_dir, "voice")
        out = run_voice(run_dir, _load_cfg(args.config), args)
        if not args.dry_run:
            meta.stage_done(run_dir, "voice", F_TIMELINE, status="done",
                            extra={"segs": out["segs"], "total": out["total"],
                                   "artifacts": [F_SCRIPT, F_MANIFEST,
                                                 F_TIMELINE, F_SRT, F_VTT,
                                                 FULL_WAV]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
