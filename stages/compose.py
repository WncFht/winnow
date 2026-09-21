# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2"]
# ///
"""stages/compose.py — PLAN.md §7.8 ffmpeg 兜底合成引擎。

唯一驱动输入：70_render_plan.json（render_plan/1 —— 完全解析后的绝对
时间轴，composer/fallback 共用同一份计划）。把 repro/compose.py 已验证
的 ffmpeg 图谱语义移植到契约产物上，不做任何二次推断：

  video_track[]   → -loop 1 -framerate {fps} -t {end-start} -i {src}
                    → scale=W:H,fps,format=yuv420p,setsar=1 → concat
  overlay_track[] → -loop 1 -i {src} → overlay={xy}:enable=between(t,s,e)
                    （字幕 pill 等 PNG 叠加层；xy 由 plan 给出，
                    缺省 (main_w-overlay_w)/2:930 = 居中、y≈930）
  audio_track[]   → -i {src} → aresample=48000,aformat=stereo,
                    adelay={at_ms}|{at_ms} → amix normalize=0
  输出            → -c:v libx264 -preset medium -crf 19 -r {fps}
                    -c:a aac -b:a 192k -t {total}  <run>/out/final.mp4

产物：
  out/final.mp4           正片（--out 可改路径，--max-t N 截顶测试）
  80_build_manifest.json  build/1：inputs sha256 哈希链 + tool + output 度量
  80_graph.txt            实际执行的 filter_complex（审计/复跑用）

CLI：
  uv run stages/compose.py --run-dir runs/<date>
  uv run stages/compose.py --run-dir runs/<date> --max-t 10 --out out/smoke.mp4
  uv run stages/compose.py --selftest      # artifact-contracts fixture ~10s 冒烟
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> contracts/adapters
from lib import meta  # stages/lib/meta.py（stages/ 即 sys.path 脚本目录）

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")

PLAN_NAME = "70_render_plan.json"
MANIFEST_NAME = "80_build_manifest.json"
GRAPH_NAME = "80_graph.txt"
DEFAULT_OUT = "out/final.mp4"
# inputs 哈希链：manifest key → run 内文件名（build/1 契约 §4）
INPUT_FILES = {
    "render_plan": "70_render_plan.json",
    "timeline": "62_timeline.json",
    "audio_manifest": "61_audio_manifest.json",
    "frames_manifest": "64_frames_manifest.json",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_SUB_XY = "(main_w-overlay_w)/2:930"

VCODEC, PRESET, CRF = "libx264", "medium", 19
ACODEC, ABITRATE, ARATE = "aac", "192k", 48000


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def eprint(*a):
    print(*a, file=sys.stderr)


def resolve_run_dir(s: str) -> Path:
    """'YYYY-MM-DD' -> runs/<date>；否则按路径解析（相对路径基于 repo 根）。"""
    p = Path(s)
    if DATE_RE.match(s):
        return meta.ensure_run(s)
    return meta.ensure_run(p)


def load_plan(run_dir: Path) -> dict:
    p = run_dir / PLAN_NAME
    if not p.exists():
        raise SystemExit(f"[compose] 缺输入 {p} —— 先跑 `just render-plan`")
    plan = json.loads(p.read_text(encoding="utf-8"))
    try:
        from contracts.models import RenderPlan
        RenderPlan.model_validate(plan)
    except ImportError:
        eprint("[compose] warn: contracts/pydantic 不可用，跳过 schema 校验")
    except Exception as ex:
        raise SystemExit(f"[compose] {PLAN_NAME} 不合 render_plan/1: {ex}")
    return plan


def ffmpeg_version() -> str:
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                         text=True, check=True).stdout.splitlines()[0]
    m = re.search(r"version\s+(\S+)", out)
    return m.group(1) if m else out.strip()


def ffprobe_out(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    res = {"dur": round(float(info["format"]["duration"]), 3)}
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            res["width"], res["height"] = s.get("width"), s.get("height")
            num, _, den = str(s.get("r_frame_rate", "0/1")).partition("/")
            res["fps"] = round(int(num) / max(int(den or 1), 1), 3)
        elif s.get("codec_type") == "audio":
            res["has_audio"] = True
    return res


# ---------------------------------------------------------------- graph

def build_command(plan: dict, run_dir: Path, out_path: Path, eff_total: float):
    """render_plan → (argv, filter_complex)。完全复刻 repro/compose.py 语义。"""
    fps = int(plan.get("fps", 30))
    W, H = (int(x) for x in plan.get("size", [1920, 1080]))
    vsegs = sorted(plan["video_track"], key=lambda v: v["start"])
    osegs = plan.get("overlay_track", [])
    asegs = plan.get("audio_track", [])
    if not vsegs:
        raise SystemExit("[compose] video_track 为空")

    missing = [t["src"] for t in vsegs + osegs + asegs
               if not (run_dir / t["src"]).exists()]
    if missing:
        raise SystemExit("[compose] plan 引用文件缺失:\n  " + "\n  ".join(missing[:20]))

    inputs: list[str] = []
    for v in vsegs:
        dur = max(v["end"] - v["start"], 0.001)
        inputs += ["-loop", "1", "-framerate", str(fps),
                   "-t", f"{dur:.3f}", "-i", str(run_dir / v["src"])]
    sub_base = len(vsegs)
    for o in osegs:
        inputs += ["-loop", "1", "-i", str(run_dir / o["src"])]
    a_base = sub_base + len(osegs)
    for a in asegs:
        inputs += ["-i", str(run_dir / a["src"])]

    fc: list[str] = []
    labels = []
    for i in range(len(vsegs)):
        fc.append(f"[{i}:v]scale={W}:{H},fps={fps},format=yuv420p,setsar=1[c{i}]")
        labels.append(f"[c{i}]")
    fc.append("".join(labels) + f"concat=n={len(vsegs)}:v=1:a=0[vbase]")
    cur = "vbase"
    for i, o in enumerate(osegs):
        nxt = f"vs{i}"
        xy = o.get("xy") or DEFAULT_SUB_XY
        fc.append(
            f"[{cur}][{sub_base + i}:v]overlay={xy}"
            f":enable=between(t\\,{o['start']:.3f}\\,{o['end']:.3f})[{nxt}]")
        cur = nxt
    fc.append(f"[{cur}]format=yuv420p[vout]")

    if asegs:
        for i, a in enumerate(asegs):
            ms = int(round(a["at"] * 1000))
            fc.append(f"[{a_base + i}:a]aresample={ARATE},"
                      f"aformat=channel_layouts=stereo,"
                      f"adelay={ms}|{ms}[a{i}]")
        fc.append("".join(f"[a{i}]" for i in range(len(asegs)))
                  + f"amix=inputs={len(asegs)}:normalize=0[aout]")
    else:  # 无音轨 → 静音床，保证 mp4 恒有 audio stream
        inputs += ["-f", "lavfi", "-t", f"{eff_total:.3f}",
                   "-i", f"anullsrc=r={ARATE}:cl=stereo"]
        fc.append(f"[{a_base}:a]aresample={ARATE}[aout]")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", VCODEC, "-preset", PRESET, "-crf", str(CRF), "-r", str(fps),
        "-c:a", ACODEC, "-b:a", ABITRATE,
        "-t", f"{eff_total:.3f}",
        str(out_path),
    ]
    return cmd, ";".join(fc)


# ---------------------------------------------------------------- render

def cmd_render(run_dir: Path, max_t: float | None, out_arg: str | None) -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("[compose] ffmpeg/ffprobe 不在 PATH")
    plan = load_plan(run_dir)
    total = float(plan["total"])
    eff_total = min(total, float(max_t)) if max_t else total
    if eff_total <= 0:
        raise SystemExit(f"[compose] 有效时长 {eff_total} <= 0")

    out_path = (Path(out_arg) if out_arg else run_dir / DEFAULT_OUT)
    if not out_path.is_absolute():
        out_path = run_dir / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd, graph = build_command(plan, run_dir, out_path, eff_total)
    meta.atomic_write(run_dir / GRAPH_NAME, graph + "\n")
    print(f"[compose] v={len(plan['video_track'])} o={len(plan.get('overlay_track', []))} "
          f"a={len(plan.get('audio_track', []))} total={eff_total:.3f}s -> {out_path}")
    t0 = datetime.now()
    subprocess.run(cmd, cwd=run_dir, check=True)
    elapsed = (datetime.now() - t0).total_seconds()

    probe = ffprobe_out(out_path)
    drift = abs(probe["dur"] - eff_total)
    ok = drift <= 0.5 and probe.get("has_audio")
    print(f"[compose] ffprobe dur={probe['dur']:.3f} (Δ{drift:.3f}) "
          f"fps={probe.get('fps')} {probe.get('width')}x{probe.get('height')} "
          f"audio={probe.get('has_audio')} render={elapsed:.1f}s")

    inputs_chain = {}
    for key, fname in INPUT_FILES.items():
        p = run_dir / fname
        if p.exists():
            inputs_chain[key] = "sha256:" + meta.sha256_file(p)
    manifest = {
        "schema": "build/1",
        "episode": plan["episode"],
        "built_at": now_iso(),
        "inputs": inputs_chain,
        "tool": {"ffmpeg": ffmpeg_version(), "vcodec": VCODEC, "preset": PRESET,
                 "crf": CRF, "fps": int(plan.get("fps", 30)),
                 "acodec": ACODEC, "abitrate": ABITRATE},
        "output": {"path": str(out_path.relative_to(run_dir)),
                   "dur": probe["dur"], "bytes": out_path.stat().st_size,
                   "sha256": meta.sha256_file(out_path),
                   "width": probe.get("width"), "height": probe.get("height")},
    }
    try:
        from contracts.models import BuildManifest
        BuildManifest.model_validate(manifest)
    except ImportError:
        pass
    except Exception as ex:
        raise SystemExit(f"[compose] manifest 不合 build/1: {ex}")
    meta.atomic_write(run_dir / MANIFEST_NAME, manifest)
    meta.stage_done(run_dir, "compose", MANIFEST_NAME, status="done",
                    extra={"video": manifest["output"]["path"],
                           "dur": probe["dur"], "elapsed_s": round(elapsed, 1)})
    if not ok:
        eprint(f"[compose] FAIL: dur 漂移 {drift:.3f}s 或无音轨"
               f"（期望 {eff_total:.3f}±0.5）")
        return 1
    print(f"[compose] done -> {out_path} + {MANIFEST_NAME}")
    return 0


# ---------------------------------------------------------------- selftest

def cmd_selftest() -> int:
    """artifact-contracts fixture → runs/_compose_smoke，--max-t 10 冒烟。"""
    src = REPO / "experiments/artifact-contracts/runs/2026-09-20"
    dst = REPO / "runs/_compose_smoke"
    if not (src / PLAN_NAME).exists():
        print(f"[selftest] FAIL fixture 缺 {PLAN_NAME}: {src}")
        return 1
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in INPUT_FILES.values():
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    for d in ("61_audio", "64_frames", "65_subs"):
        if (src / d).is_dir():
            shutil.copytree(src / d, dst / d)
    rc = cmd_render(dst, max_t=10.0, out_arg=None)
    if rc != 0:
        return rc
    probe = ffprobe_out(dst / DEFAULT_OUT)
    checks = [
        ("dur≈10s", abs(probe["dur"] - 10.0) <= 0.5),
        ("fps==30", abs(probe.get("fps", 0) - 30) < 0.01),
        ("1920x1080", (probe.get("width"), probe.get("height")) == (1920, 1080)),
        ("has audio", bool(probe.get("has_audio"))),
        ("manifest", (dst / MANIFEST_NAME).exists()),
    ]
    fails = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"[selftest] {'ok' if ok else 'FAIL'} {n}")
    print(f"[selftest] {'FAIL ' + str(fails) if fails else 'ALL PASS'} -> {dst}")
    return 1 if fails else 0


# ----------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ffmpeg 兜底合成（PLAN §7.8）：70_render_plan.json → out/final.mp4")
    ap.add_argument("--run-dir", help="runs/<date> 或日期 YYYY-MM-DD")
    ap.add_argument("--max-t", type=float, default=None, metavar="SEC",
                    help="只渲染前 N 秒（冒烟测试）")
    ap.add_argument("--out", default=None,
                    help=f"输出路径（默认 run_dir/{DEFAULT_OUT}）")
    ap.add_argument("--selftest", action="store_true",
                    help="fixture 端到端自测（~10s + ffprobe 断言）")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if not args.run_dir:
        ap.error("--run-dir 必填（--selftest 除外）")
    run_dir = resolve_run_dir(args.run_dir)
    with meta.run_lock(run_dir):
        return cmd_render(run_dir, args.max_t, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
