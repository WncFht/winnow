#!/usr/bin/env python3
"""Compose final mp4: card PNGs on the audio timeline + subtitle pill overlays.

ffmpeg graph: concat(card segments) -> overlay each subtitle PNG -> x264/aac.
Audio is synthesized per sentence, so each mp3 is simply delayed to its
timeline start and amix'ed — no guessing at alignment.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    data = json.loads((ROOT / "items.json").read_text())
    tl = json.loads((ROOT / "timeline.json").read_text())
    items = {i["id"]: i for i in data["items"]}
    spans = {s["id"]: s for s in tl["items"]}
    segs = tl["segs"]

    # ---- visual segments: [(png, dur)] on the absolute timeline ----
    # Each item's card holds from its span start until the NEXT item's start
    # (through the inter-item gap), so the video clock matches the audio clock.
    ordered = data["items"]
    bounds = []
    for i, it in enumerate(ordered):
        S = spans[it["id"]]["start"]
        E = spans[ordered[i + 1]["id"]]["start"] if i + 1 < len(ordered) else tl["total"]
        bounds.append((it, S, E))
    bounds[0] = (bounds[0][0], 0.0, bounds[0][2])

    vsegs = []
    for it, S, E in bounds:
        iid = it["id"]
        card = ROOT / "frames_v2" / f"{iid}.png"
        shotcard = ROOT / "frames_v2" / f"{iid}_shot.png"
        shot_win = None
        if it.get("shot") and it.get("shot_sentences") and shotcard.exists():
            a, b = it["shot_sentences"][0], it["shot_sentences"][-1]
            isegs = [s for s in segs if s["item"] == iid]
            if 0 < a <= len(isegs) and 0 < b <= len(isegs):
                shot_win = (isegs[a - 1]["start"], isegs[b - 1]["end"])
        if shot_win:
            sS, sE = shot_win
            if sS - S > 0.15:
                vsegs.append((card, sS - S))
            vsegs.append((shotcard, min(sE, E) - sS))
            if E - sE > 0.15:
                vsegs.append((card, E - max(sE, S)))
        else:
            vsegs.append((card, E - S))

    inputs = []
    for png, d in vsegs:
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{d:.3f}", "-i", str(png)]
    sub_base = len(vsegs)
    nsubs = len(segs)
    for i in range(nsubs):
        inputs += ["-loop", "1", "-i", str(ROOT / "subs" / f"{i:03d}.png")]
    a_base = sub_base + nsubs
    for s in segs:
        inputs += ["-i", str(ROOT / "audio" / s["file"])]

    fc = []
    # concat cards
    labels = []
    for i in range(len(vsegs)):
        fc.append(f"[{i}:v]fps=30,format=yuv420p,setsar=1[c{i}]")
        labels.append(f"[c{i}]")
    fc.append("".join(labels) + f"concat=n={len(vsegs)}:v=1:a=0[vbase]")
    # overlay subtitle pills
    cur = "vbase"
    for i, s in enumerate(segs):
        nxt = f"vs{i}"
        fc.append(
            f"[{cur}][{sub_base + i}:v]overlay=(main_w-overlay_w)/2:930"
            f":enable=between(t\\,{s['start']:.3f}\\,{s['end']:.3f})[{nxt}]"
        )
        cur = nxt
    fc.append(f"[{cur}]format=yuv420p[vout]")
    # audio: delay each sentence to its start, mix
    for i, s in enumerate(segs):
        ms = int(round(s["start"] * 1000))
        fc.append(f"[{a_base + i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
    fc.append("".join(f"[a{i}]" for i in range(len(segs))) + f"amix=inputs={len(segs)}:normalize=0[aout]")

    (ROOT / "graph.txt").write_text(";".join(fc))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", "30",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{tl['total']:.3f}",
        "out.mp4",
    ]
    print(" ".join(cmd[:8]), "...")
    subprocess.run(cmd, cwd=ROOT, check=True)
    print("done ->", ROOT / "out.mp4")


if __name__ == "__main__":
    main()
