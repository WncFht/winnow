"""subs — 逐句字幕 pill PNG（ffmpeg 合成路径用；PLAN §7.7/§7.8）。

Remotion 路径用 FullDaily.tsx 的 live-text pill（CSS 实时排版），ffmpeg 的
overlay 只能贴 PNG——本 stage 把 62_timeline.json 每个 seg 渲染成
65_subs/{n:03d}.png（RGBA、宽度随内容自适应）。render_plan.py sub_src()
按约定路径兜底拾取，无需 manifest 注册。

样式对齐 composer/src/FullDaily.tsx SubtitlePill：
  bg rgba(0,0,0,0.75) / 白字 44px / lineHeight 1.35 / padding 16×42 /
  radius 44 / 盒宽上限 1600（文字区 1516）/ overlay 落点 y=930 ≈ bottom:60。

CLI：
    uv run stages/subs.py --run-dir runs/<date> [--force]
      输入 62_timeline.json（缺 → exit 1，先跑 voice）；65_subs/*.png 已存在
      且未 --force 时幂等跳过（exit 0）。字体从 FONT_CANDIDATES 按序挑首个
      真含 CJK 字形的（htmlFont.ttf → NotoSansCJK），全缺 → RuntimeError。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from stages.lib import meta, prog  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

F_TIMELINE = "62_timeline.json"
SUBS_DIR = "65_subs"

# ---- pill 样式（与 SubtitlePill 对齐，单位 px @1080p） ----
FONT_SIZE = 44
LINE_H = 60                # 44 * 1.35 ≈ 59.4
PAD_X, PAD_Y = 42, 16
RADIUS = 44
MAX_BOX_W = 1600
TEXT_W = MAX_BOX_W - PAD_X * 2   # 1516
BG = (0, 0, 0, 191)        # rgba(0,0,0,0.75)
FG = (255, 255, 255, 255)

FONT_CANDIDATES = [
    "upstream/juya-news-card/public/assets/htmlFont.ttf",   # 与卡片同字体
    "upstream/juya-news-card/assets/htmlFont.ttf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",    # Noto Sans CJK SC
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]

# CJK/标点可断字符；拉丁串尽量不断词（先找空格/标点回退断点）
_BREAK_AFTER = set("，。！？；：、）】》」』”’% ,.!?;:　")
_NO_BREAK_BEFORE = set("，。！？；：、）】》」』,.!?;:%")


def _has_cjk(font) -> bool:
    """该字体是否真含中文字形（htmlFont.ttf 之类缺字体会渲成豆腐块，
    getmask 也不报错——把两个不同汉字渲成小图互比，一致即 .notdef）。"""
    def glyph(ch: str) -> bytes:
        img = Image.new("L", (FONT_SIZE + 8, FONT_SIZE + 16))
        ImageDraw.Draw(img).text((4, 4), ch, font=font, fill=255)
        return img.tobytes()
    return glyph("汉") != glyph("字")


def load_font(repo: Path, size: int) -> ImageFont.FreeTypeFont:
    for rel in FONT_CANDIDATES:
        p = repo / rel if not rel.startswith("/") else Path(rel)
        if not p.is_file():
            continue
        cands = []
        if p.suffix == ".ttc":
            for idx in range(10):               # ttc 内多 face，逐个试
                try:
                    cands.append(ImageFont.truetype(str(p), size, index=idx))
                except Exception:
                    break
        else:
            try:
                cands.append(ImageFont.truetype(str(p), size))
            except Exception:
                continue
        for f in cands:
            if _has_cjk(f):
                return f
    raise RuntimeError("无可用 CJK 字体（找过 htmlFont.ttf / NotoSansCJK）")


def wrap_text(draw: ImageDraw.ImageDraw, font, text: str) -> list[str]:
    """贪心断行：超 TEXT_W 时在最近的合法断点切开；CJK 逐字可断。"""
    lines, cur, last_bp = [], "", -1   # last_bp = cur 中可断位置（含断点后字符）
    for ch in text:
        cur += ch
        if ch in _BREAK_AFTER or ord(ch) > 0x2E7F:   # CJK 逐字可断
            last_bp = len(cur)
        if draw.textlength(cur, font=font) > TEXT_W:
            if last_bp > 0:
                # 断点字符属于上一行；避免下行以标点开头
                head, tail = cur[:last_bp], cur[last_bp:]
                while tail and tail[0] in _NO_BREAK_BEFORE and head:
                    head, tail = head[:-1], head[-1] + tail
                lines.append(head.rstrip())
                cur = tail
            else:
                lines.append(cur[:-1])
                cur = ch
            last_bp = len(cur) if (cur and (cur[-1] in _BREAK_AFTER or ord(cur[-1]) > 0x2E7F)) else -1
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


def render_pill(text: str, font, draw_probe) -> Image.Image:
    lines = wrap_text(draw_probe, font, text)
    w = max(int(draw_probe.textlength(ln, font=font)) for ln in lines)
    box_w = min(w + PAD_X * 2, MAX_BOX_W)
    box_h = len(lines) * LINE_H + PAD_Y * 2
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, box_w - 1, box_h - 1], radius=RADIUS, fill=BG)
    for i, ln in enumerate(lines):
        lw = d.textlength(ln, font=font)
        d.text(((box_w - lw) / 2, PAD_Y + i * LINE_H), ln, font=font, fill=FG)
    return img


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="字幕 pill PNG 生成（ffmpeg 路径）")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 65_subs/")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir)
    repo = Path(__file__).resolve().parents[1]

    tl_path = run_dir / F_TIMELINE
    if not tl_path.is_file():
        print(f"[subs] 缺 {F_TIMELINE}（先跑 voice）", file=sys.stderr)
        return 1
    segs = (json.loads(tl_path.read_text("utf-8")).get("segs")) or []

    out_dir = run_dir / SUBS_DIR
    # run_lock 包住 跳过判定+渲染+登记：并发重入时第二个实例在锁内看到
    # 已产出的 65_subs/ 走幂等早退，不会双渲染。
    with meta.run_lock(run_dir):
        if out_dir.is_dir() and any(out_dir.glob("*.png")) and not args.force:
            print(f"[subs] {SUBS_DIR}/ 已存在（--force 重渲）")
            return 0
        out_dir.mkdir(parents=True, exist_ok=True)

        meta.stage_begin(run_dir)
        p = prog.Prog(run_dir, "subs", total=len(segs),
                      step=min(100, max(10, len(segs) // 40)), interval=30)

        font = load_font(repo, FONT_SIZE)
        probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

        made = skipped = 0
        for i, s in enumerate(segs, 1):
            p.tick(i, "pill")
            n, text = s.get("n"), \
                (s.get("text_display") or s.get("text") or "").strip()
            if not isinstance(n, int) or not text:
                skipped += 1
                print(f"[subs] 跳过 seg n={n}（无 text）", file=sys.stderr)
                continue
            render_pill(text, font, probe).save(out_dir / f"{n:03d}.png")
            (out_dir / f"{n:03d}.txt").write_text(text, encoding="utf-8")
            made += 1

        p.say(f"{made}/{len(segs)} pill 完成（skipped {skipped}）")
        p.close()

        meta.stage_done(run_dir, "subs", SUBS_DIR, status="done",
                        producer="stages/subs.py",
                        extra={"segs": len(segs), "made": made, "skipped": skipped})
    print(f"[subs] {made}/{len(segs)} pill → {SUBS_DIR}/（skipped {skipped}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
