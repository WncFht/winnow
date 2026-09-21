# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "pydantic>=2"]
# ///
"""stages/render_plan.py — PLAN.md §7.7：编译 70_render_plan.json + 70_cards.ffconcat。

输入（文件即依赖边，缺则 fail-fast 提示先跑哪个 just 目标）：
  62_timeline.json        timeline/1：items+segs+overlays（shot 窗口已编译为绝对时间）
  64_frames_manifest.json frames_manifest/1：files[{item,kind,path,w,h,sha256,t?}] + missing[]
  63_cards.json           cards/1（覆盖核对：每个正片 item 应有卡条目）
  50_issue.json           （可选回退）items[].video.shot_sentences——timeline.overlays
                          缺失/不全时按 1-based 句区间自行编译 shot 窗口
  config.render           config.yaml > config.example.yaml：fps/size/aspect/engine
                          （可选扩展键 subtitle_xy / chrome_xy / min_seg）

编译规则（绝对时间轴；repro/compose.py 已验证语义，修掉 v1 ~8s 逐段漂移）：
  - 视频钟=音频钟：item i 的卡持显示窗 W_i=[item.start, next_item.start)，
    首个 item 从 0.0 起（含 lead_in 静默），末 item 到 timeline.total（含 tail）。
  - item.visual 为显示绑定（如 outro.visual='kimi' → 该窗挂 kimi 的卡/shot）。
  - shot 窗口在 W_i 内切 card→shot→card 三段；不足 MIN_SEG(0.15s) 的卡缝被
    shot 吸收（compose.py 同款阈值；绝对时间轴下等价于 shot 扩到缝边）。
  - 文件缺失→manifest missing[] 政策占位：shot→本 item 卡；card→cover→
    前一 item 已解析卡→任意现存卡→生成的 _placeholder.png。声明过 missing
    的记 warn；未声明却缺的记 error（manifest 失真）但仍占位出片。
  - audio_track：逐 seg {src:seg.file, at:seg.start}。
  - overlay_track：chrome/非-shot overlay 在前（全幅 xy='0:0'），逐 seg 字幕
    pill 在后（字幕永远画最上层；pill 源 = manifest kind=sub 按 t 窗/si 匹配，
    或 65_subs/{n:03d}.png / subs/{n:03d}.png 约定路径）。

内置 validators（写完即跑，§7.7 验收）：video_track 满铺无洞
（end==next.start ±0.04、首段 0.0、末段==total）、plan.total==timeline.total
±0.05、audio_track.at==seg.start、所有引用文件存在（占位替换后）。
校验有错仍写产物（便于排查），但 00_meta 记 failed、退出码 1。

CLI：
  uv run stages/render_plan.py --run-dir runs/<date>   # 编译 + 写 70_* + 校验
  uv run stages/render_plan.py --run-dir R --check     # 只重校验已写的 70_render_plan.json
  uv run stages/render_plan.py --selftest              # repro fixture + golden + 边界自测
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> contracts/adapters
from lib import meta  # stages/lib/meta.py（stages/ 即 sys.path 脚本目录）

REPO = Path(__file__).resolve().parents[1]
REPRO = REPO / "repro"
FIXTURE_RUN = REPO / "experiments" / "artifact-contracts" / "runs" / "2026-09-20"

F_TIMELINE = "62_timeline.json"
F_CARDS = "63_cards.json"
F_FRAMES = "64_frames_manifest.json"
F_ISSUE = "50_issue.json"          # 可选：shot_sentences 回退源
OUT_PLAN = "70_render_plan.json"
OUT_FFCONCAT = "70_cards.ffconcat"

MIN_SEG = 0.15      # 卡缝最小宽度（s），更窄则并入相邻 shot
CONTIG_TOL = 0.04   # video_track 相邻段接缝容差（§7.7 验收值）
TOTAL_TOL = 0.05    # plan.total vs timeline.total 容差
AT_TOL = 0.001      # audio at vs seg.start
T_MATCH = 0.06      # manifest t 窗 ↔ 句窗 匹配容差

SUB_XY = "(main_w-overlay_w)/2:930"   # 字幕 pill（compose.py: overlay y=930 居中）
FULL_XY = "0:0"                        # 全幅 chrome/sticker 叠加
PSEUDO_ITEMS = {"intro", "outro", "cover"}

DEFAULTS = {"engine": "remotion", "fps": 30, "size": [1920, 1080],
            "aspect": "16:9", "concurrency": 4}
PNG_SIG = b"\x89PNG\r\n\x1a\n"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def eprint(*a):
    print(*a, file=sys.stderr)


def _jload(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_run_dir(s: str) -> Path:
    """'YYYY-MM-DD' -> runs/<date>；否则按路径解析（相对路径基于 repo 根）。"""
    if DATE_RE.match(s):
        return meta.ensure_run(s)
    return meta.ensure_run(Path(s))


def load_config() -> dict:
    """config.yaml > config.example.yaml 的 render 节 + 默认值。"""
    import yaml

    cfg = {}
    for name in ("config.yaml", "config.example.yaml"):
        f = REPO / name
        if f.exists():
            try:
                cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception as e:
                eprint(f"[render_plan] warn: {name} 解析失败 {e} — 用默认值")
            break
    r = cfg.get("render") or {}
    out = dict(DEFAULTS)
    for k in ("engine", "fps", "size", "aspect", "concurrency",
              "subtitle_xy", "chrome_xy", "min_seg"):
        if r.get(k) is not None:
            out[k] = r[k]
    out["fps"] = int(out["fps"])
    out["size"] = [int(out["size"][0]), int(out["size"][1])]
    out["min_seg"] = float(out.get("min_seg") or MIN_SEG)
    out["subtitle_xy"] = str(out.get("subtitle_xy") or SUB_XY)
    out["chrome_xy"] = str(out.get("chrome_xy") or FULL_XY)
    return out


def png_dims(p: Path):
    """读 PNG IHDR 拿 (w,h)；非 PNG/失败 → None。"""
    try:
        with open(p, "rb") as f:
            b = f.read(26)
        if b[:8] != PNG_SIG:
            return None
        return struct.unpack(">II", b[16:24])
    except OSError:
        return None


def write_placeholder_png(path: Path, w: int, h: int, rgb=(20, 24, 32)) -> None:
    """stdlib 生成纯色 PNG —— 一张可用卡都不存在时的最后占位。"""
    row = b"\x00" + bytes(rgb) * w          # filter byte + RGB 行
    raw = row * h

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_SIG + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw, 6))
                     + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# frames manifest 索引与占位解析
# --------------------------------------------------------------------------- #

class FrameIndex:
    """64_frames_manifest 查询层：by(item,kind) + missing[] + 磁盘存在性。"""

    def __init__(self, run_dir: Path, fm: dict):
        self.run_dir = run_dir
        self.dir = str(fm.get("dir") or "64_frames")
        self.missing = set(fm.get("missing") or [])
        self.by_ik: dict[tuple[str, str], list[dict]] = {}
        for f in fm.get("files") or []:
            self.by_ik.setdefault((str(f.get("item")), str(f.get("kind"))),
                                  []).append(f)
        for fl in self.by_ik.values():
            fl.sort(key=lambda f: str(f.get("path") or ""))

    def exists(self, rel) -> bool:
        return bool(rel) and (self.run_dir / str(rel)).is_file()

    def declared(self, item: str, kind: str) -> bool:
        return f"{item}.{kind}" in self.missing

    def find(self, item: str, kind: str, t=None):
        """manifest (item,kind) → 现存 path；t=[s,e] 时优先 t 窗匹配项。"""
        cands = self.by_ik.get((item, kind), [])
        if t is not None:
            for f in cands:
                ft = f.get("t")
                if (ft and abs(ft[0] - t[0]) <= T_MATCH
                        and abs(ft[1] - t[1]) <= T_MATCH
                        and self.exists(f.get("path"))):
                    return f["path"]
        for f in cands:
            if self.exists(f.get("path")):
                return f["path"]
        return None

    def any_card(self):
        """任意现存 card 帧（占位链倒数第二级）。"""
        for (it, k), fl in sorted(self.by_ik.items()):
            if k != "card":
                continue
            for f in fl:
                if self.exists(f.get("path")):
                    return f["path"]
        return None


def conv_path(fx: FrameIndex, item: str, kind: str) -> str:
    """约定路径：card=<dir>/<item>.png，其余=<dir>/<item>.<kind>.png。"""
    if kind == "card":
        return f"{fx.dir}/{item}.png"
    return f"{fx.dir}/{item}.{kind}.png"


class Compiler:
    """一次编译的上下文：run_dir / cfg / 索引 / 告警与错误收集器。"""

    def __init__(self, run_dir: Path, cfg: dict):
        self.run_dir = run_dir
        self.cfg = cfg
        self.min_seg = float(cfg.get("min_seg") or MIN_SEG)
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self._placeholder_rel: str | None = None

    def warn(self, m): self.warnings.append(m)
    def err(self, m): self.errors.append(m)

    def placeholder(self, fx: FrameIndex) -> str:
        """生成的纯色占位帧（落 fx.dir/_placeholder.png，幂等）。"""
        if self._placeholder_rel and fx.exists(self._placeholder_rel):
            return self._placeholder_rel
        rel = f"{fx.dir}/_placeholder.png"
        p = self.run_dir / rel
        if not p.exists():
            w, h = self.cfg["size"]
            write_placeholder_png(p, w, h)
            self.warn(f"生成兜底占位帧 {rel}（{w}x{h} 纯色）")
        self._placeholder_rel = rel
        return rel

    def card_for(self, fx: FrameIndex, item: str, prev_card: str | None) -> str:
        """item 正卡解析链：card → cover → 约定路径 → 前一卡 → 任意卡 → 占位图。"""
        p = fx.find(item, "card")
        if not p:
            conv = conv_path(fx, item, "card")
            if fx.exists(conv):
                p = conv
        if p:
            return p
        declared = fx.declared(item, "card")
        where = f"{item}.card"
        p = fx.find(item, "cover")
        if p:
            self._sub_note(declared, f"{where} 缺失→cover 占位 {p}")
            return p
        if prev_card:
            self._sub_note(declared, f"{where} 缺失→复用前一卡 {prev_card}")
            return prev_card
        p = fx.any_card()
        if p:
            self._sub_note(declared, f"{where} 缺失→借用现存卡 {p}")
            return p
        p = self.placeholder(fx)
        self._sub_note(declared, f"{where} 缺失→生成占位 {p}")
        return p

    def _sub_note(self, declared: bool, msg: str):
        if declared:
            self.warn(msg + "（missing[] 已声明）")
        else:
            self.err(msg + "（未在 missing[] 声明——上游 manifest 失真）")

    # ---- shot 窗口：timeline.overlays 优先，50_issue.video.shot_sentences 回退

    def shot_windows(self, tl: dict, issue: dict | None, item: str,
                     segs_by_item: dict[str, list[dict]]) -> list[dict]:
        wins = []
        for o in tl.get("overlays") or []:
            if o.get("item") == item and o.get("kind", "shot") == "shot":
                try:
                    wins.append({"start": float(o["start"]),
                                 "end": float(o["end"]),
                                 "src": o.get("src")})
                except (KeyError, TypeError, ValueError):
                    self.warn(f"overlay shot 窗非法跳过: {o}")
        if issue:
            for it in issue.get("items") or []:
                if it.get("id") != item:
                    continue
                ss = ((it.get("video") or {}).get("shot_sentences")
                      or it.get("shot_sentences"))
                if not ss:
                    continue
                isegs = segs_by_item.get(item, [])
                try:
                    a, b = int(ss[0]), int(ss[-1])
                except (TypeError, ValueError, IndexError):
                    self.warn(f"{item}.shot_sentences 非法跳过: {ss!r}")
                    continue
                if 1 <= a <= len(isegs) and 1 <= b <= len(isegs):
                    wins.append({"start": float(isegs[a - 1]["start"]),
                                 "end": float(isegs[b - 1]["end"]),
                                 "src": None})
                else:
                    self.warn(f"{item}.shot_sentences {ss} 越界（{len(isegs)} 句）")
        return wins

    def norm_windows(self, wins: list[dict], S: float, E: float) -> list[dict]:
        """clip 到 [S,E)、丢 <MIN_SEG、排序、合并重叠/微缝（保留首个非空 src）。"""
        out: list[dict] = []
        for w in sorted(wins, key=lambda x: (x["start"], x["end"])):
            s, e = max(w["start"], S), min(w["end"], E)
            if e - s < self.min_seg:
                continue
            if out and s - out[-1]["end"] < self.min_seg:
                out[-1]["end"] = max(out[-1]["end"], e)
                if not out[-1].get("src") and w.get("src"):
                    out[-1]["src"] = w["src"]
            else:
                out.append({"start": s, "end": e, "src": w.get("src")})
        return out

    def shot_src(self, fx: FrameIndex, item: str, w: dict):
        """shot 帧解析：overlay.src → manifest(item,shot,t≈窗) → 约定路径。"""
        if w.get("src") and fx.exists(w["src"]):
            return w["src"]
        p = fx.find(item, "shot", t=[w["start"], w["end"]])
        if p:
            return p
        conv = conv_path(fx, item, "shot")
        if fx.exists(conv):
            return conv
        return None

    # ---- 字幕 pill 解析

    def sub_src(self, fx: FrameIndex, seg: dict, item_seg_n: dict) -> str | None:
        item, si, n = seg.get("item"), seg.get("si"), seg.get("n")
        # manifest kind=sub：t 窗匹配优先
        cands = fx.by_ik.get((item, "sub"), [])
        for f in cands:
            ft = f.get("t")
            if (ft and abs(ft[0] - seg["start"]) <= T_MATCH
                    and abs(ft[1] - seg["end"]) <= T_MATCH
                    and fx.exists(f.get("path"))):
                return f["path"]
        # 无 t 的 sub 文件按 si 序对位（数量与该 item 句数一致才可信）
        unt = [f for f in cands if not f.get("t") and fx.exists(f.get("path"))]
        if unt and si is not None and item_seg_n.get(item) == len(unt) \
                and 0 <= si < len(unt):
            return unt[si]["path"]
        seg_id = str(seg.get("seg_id") or "")
        stems = [f"{n:03d}"] if isinstance(n, int) else []
        if seg_id:
            stems.append(seg_id)
        for d in ("65_subs", "subs", fx.dir):
            for st in stems:
                rel = f"{d}/{st}.png"
                if fx.exists(rel):
                    return rel
        return None


# --------------------------------------------------------------------------- #
# 编译主流程
# --------------------------------------------------------------------------- #

def compile_plan(run_dir: Path, cfg: dict) -> tuple[dict | None, list, list]:
    """读输入 → (plan dict | None, warnings, errors)。None = 结构性失败别写。"""
    c = Compiler(run_dir, cfg)
    tl = _jload(run_dir / F_TIMELINE)
    fm = _jload(run_dir / F_FRAMES)
    cards = _jload(run_dir / F_CARDS)
    issue = _jload(run_dir / F_ISSUE) if (run_dir / F_ISSUE).exists() else None

    fx = FrameIndex(run_dir, fm)
    segs = list(tl.get("segs") or [])
    items = list(tl.get("items") or [])
    total = float(tl.get("total") or 0)
    if not items or total <= 0:
        c.err("62_timeline items 为空或 total<=0 —— 无法铺视频轨")
        return None, c.warnings, c.errors
    if any(items[i + 1]["start"] < items[i]["start"] for i in range(len(items) - 1)):
        c.warn("timeline.items 未按 start 排序 —— render_plan 按 start 重排")
        items.sort(key=lambda i: i["start"])

    segs_by_item: dict[str, list[dict]] = {}
    for s in segs:
        segs_by_item.setdefault(str(s.get("item")), []).append(s)
    for v in segs_by_item.values():
        v.sort(key=lambda s: (s.get("si", 0), s.get("start", 0)))

    # cards/1 覆盖核对（伪 item 豁免）
    if isinstance(cards, dict):
        card_ids = {str(c.get("id")) for c in cards.get("items") or []}
        for it in items:
            iid = str(it.get("id"))
            if iid not in card_ids and iid not in PSEUDO_ITEMS:
                c.warn(f"timeline item {iid} 不在 63_cards.items（卡片内容可能缺）")

    video_track: list[dict] = []
    audio_track: list[dict] = []
    overlay_track: list[dict] = []
    display_win: dict[str, tuple[float, float]] = {}
    prev_card: str | None = None

    for i, it in enumerate(items):
        iid = str(it.get("id"))
        S = 0.0 if i == 0 else float(it["start"])          # 首 item 从 0.0 起
        E = float(items[i + 1]["start"]) if i + 1 < len(items) else total
        E = min(E, total)
        if E - S <= 0:
            c.err(f"item {iid} 显示窗为空/负 [{S},{E}) —— 跳过")
            continue
        display_win[iid] = (S, E)
        v = str(it.get("visual") or iid)                    # 显示绑定
        card = c.card_for(fx, v, prev_card)

        # shot 窗（按显示绑定的 item 取窗）→ 解析 src → card/shot/card 三段
        wins = c.norm_windows(c.shot_windows(tl, issue, v, segs_by_item), S, E)
        parts: list[tuple[str, float, float]] = []
        cur = S
        for w in wins:
            src = c.shot_src(fx, v, w)
            if not src:
                msg = f"{v}.shot 帧缺失 → [{w['start']:.3f},{w['end']:.3f}) 用正卡占位"
                (c.warn if fx.declared(v, "shot") else c.err)(msg)
                continue
            sS, sE = w["start"], w["end"]
            if sS - cur >= c.min_seg:
                parts.append((card, cur, sS))
            else:
                sS = cur                                    # 微缝并进 shot 头
            parts.append((src, sS, sE))
            cur = sE
        if not parts or E - cur >= c.min_seg:
            parts.append((card, cur, E))
        else:
            parts[-1] = (parts[-1][0], parts[-1][1], E)     # 微缝并进末段
        for src, a, b in parts:
            video_track.append({"src": src,
                                "start": round(a, 3), "end": round(b, 3)})
        prev_card = card

    # audio_track：逐 seg {src, at=start}
    for s in segs:
        src = str(s.get("file") or "")
        if src and not fx.exists(src) and not src.startswith("61_audio/"):
            alt = f"61_audio/{src}"
            if fx.exists(alt):
                src = alt
        audio_track.append({"src": src, "at": round(float(s["start"]), 3)})

    # overlay_track —— A. chrome/非-shot overlay（全幅，画在卡上、字幕下）
    item_ids = {str(i.get("id")) for i in items}
    for (it_id, kind), fl in sorted(fx.by_ik.items()):
        if kind != "chrome":
            continue
        for f in fl:
            path = f.get("path")
            if not fx.exists(path):
                (c.warn if fx.declared(it_id, "chrome") else c.err)(
                    f"chrome 帧缺失跳过: {path}")
                continue
            if f.get("t"):
                s_, e_ = float(f["t"][0]), float(f["t"][1])
            elif it_id in display_win:
                s_, e_ = display_win[it_id]
            elif it_id in item_ids:
                s_, e_ = float(next(i for i in items if i["id"] == it_id)["start"]), \
                         float(next(i for i in items if i["id"] == it_id)["end"])
            else:
                c.warn(f"chrome {path} 的 item {it_id} 不在 timeline —— 跳过")
                continue
            overlay_track.append({"src": path, "start": round(s_, 3),
                                  "end": round(e_, 3), "xy": cfg["chrome_xy"]})
    for o in tl.get("overlays") or []:
        if o.get("kind", "shot") == "shot":
            continue                                    # shot 已编译进 video_track
        src = o.get("src")
        if src and fx.exists(src):
            overlay_track.append({"src": src, "start": round(float(o["start"]), 3),
                                  "end": round(float(o["end"]), 3), "xy": FULL_XY})
        elif fx.declared(str(o.get("item")), str(o.get("kind", "overlay"))):
            c.warn(f"overlay {o.get('item')}.{o.get('kind')} 缺失已声明 —— 跳过")
        else:
            c.warn(f"overlay {src} 不存在 —— 跳过")

    # overlay_track —— B. 逐 seg 字幕 pill（始终最上层）
    item_seg_n = {iid: len(v) for iid, v in segs_by_item.items()}
    for s in segs:
        sub = c.sub_src(fx, s, item_seg_n)
        if sub:
            overlay_track.append({"src": sub, "start": round(float(s["start"]), 3),
                                  "end": round(float(s["end"]), 3),
                                  "xy": cfg["subtitle_xy"]})
        elif fx.declared(str(s.get("item")), "sub"):
            c.warn(f"seg {s.get('seg_id', s.get('n'))} 字幕缺失已声明 —— 跳过")
        else:
            c.warn(f"seg {s.get('seg_id', s.get('n'))} 无字幕 pill —— "
                   "ffmpeg 兜底将无该句字幕（Remotion live-text 不受影响）")

    plan = {
        "schema": "render_plan/1",
        "episode": str(tl.get("episode") or run_dir.name),
        "fps": cfg["fps"],
        "size": cfg["size"],
        "aspect": str(cfg["aspect"]),
        "total": round(total, 3),
        "video_track": video_track,
        "audio_track": audio_track,
        "overlay_track": overlay_track,
    }
    return plan, c.warnings, c.errors


# --------------------------------------------------------------------------- #
# validators + 投影
# --------------------------------------------------------------------------- #

def validate_plan(plan: dict, tl: dict, run_dir: Path) -> list[str]:
    """§7.7 验收：满铺无洞 / total 对齐 / audio at==seg.start / 引用文件存在。"""
    errs: list[str] = []
    vt = plan.get("video_track") or []
    total = float(plan.get("total") or 0)
    if not vt:
        errs.append("video_track 为空")
    else:
        if abs(float(vt[0]["start"])) > 1e-6:
            errs.append(f"video_track 首段 start={vt[0]['start']} ≠ 0.0")
        for i, (a, b) in enumerate(zip(vt, vt[1:])):
            d = float(b["start"]) - float(a["end"])
            if abs(d) > CONTIG_TOL:
                errs.append(f"video_track[{i}]→[{i+1}] 接缝 {d:+.3f}s "
                            f"（{a['src']} {a['end']} → {b['src']} {b['start']}）")
            if float(b["end"]) <= float(b["start"]):
                errs.append(f"video_track[{i+1}] 非正时长 {b}")
        if abs(float(vt[-1]["end"]) - total) > CONTIG_TOL:
            errs.append(f"video_track 末段 end={vt[-1]['end']} ≠ total {total}")
    if abs(total - float(tl.get("total") or 0)) > TOTAL_TOL:
        errs.append(f"plan.total {total} ≠ timeline.total {tl.get('total')}")

    segs = tl.get("segs") or []
    at = plan.get("audio_track") or []
    if len(at) != len(segs):
        errs.append(f"audio_track {len(at)} 条 ≠ timeline.segs {len(segs)} 条")
    for a, s in zip(at, segs):
        if abs(float(a["at"]) - float(s["start"])) > AT_TOL:
            errs.append(f"audio {a['src']} at={a['at']} ≠ seg.start {s['start']}")

    seen: set[str] = set()
    for tr in vt + at + (plan.get("overlay_track") or []):
        src = str(tr.get("src") or "")
        if not src or src in seen:
            continue
        seen.add(src)
        if not (run_dir / src).is_file():
            errs.append(f"引用文件不存在: {src}")
    return errs


def _ffq(p: str) -> str:
    """ffconcat file 行 quoting（' → '\\''）。"""
    return "'" + str(p).replace("'", "'\\''") + "'"


def ffconcat_text(plan: dict) -> str:
    """70_cards.ffconcat 投影：video_track → concat demuxer 文档。
    末文件重复一次（concat demuxer 对最后一张静帧 duration 的已知怪癖）。"""
    lines = ["ffconcat version 1.0",
             "# generated by stages/render_plan.py from 70_render_plan.json"
             " — do not edit"]
    vt = plan.get("video_track") or []
    for v in vt:
        lines.append(f"file {_ffq(v['src'])}")
        lines.append(f"duration {float(v['end']) - float(v['start']):.3f}")
    if vt:
        lines.append(f"file {_ffq(vt[-1]['src'])}")
    return "\n".join(lines) + "\n"


def check_ffconcat(path: Path) -> tuple[bool, str]:
    """ffprobe -f concat 解析检查；ffprobe 不在则跳过返回 (True, 'skip')。"""
    if not shutil.which("ffprobe"):
        return True, "skip: ffprobe 不在 PATH"
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "concat", "-safe", "0",
         "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", path.name],
        cwd=path.parent, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"ffprobe rc={r.returncode}: {r.stderr.strip()[:200]}"
    if "video" not in r.stdout:
        return False, f"ffprobe 未检出视频流: {r.stdout.strip()[:120]}"
    return True, r.stdout.strip().splitlines()[0]


# --------------------------------------------------------------------------- #
# run / check
# --------------------------------------------------------------------------- #

_REQUIRE = [(F_TIMELINE, "just voice"),
            (F_CARDS, "just cards"),
            (F_FRAMES, "just cards")]


def cmd_run(run_dir: Path) -> int:
    missing = [n for n, _ in _REQUIRE if not (run_dir / n).exists()]
    if missing:
        need = sorted({h for n, h in _REQUIRE if n in missing})
        eprint(f"[render_plan] 缺输入 {missing} — 先跑: {' && '.join(need)}")
        return 2
    if not (run_dir / F_ISSUE).exists():
        eprint(f"[render_plan] 提示: 无 {F_ISSUE} —— shot 窗口仅取 timeline.overlays")

    cfg = load_config()
    try:
        plan, warnings, errors = compile_plan(run_dir, cfg)
    except json.JSONDecodeError as e:
        eprint(f"[render_plan] 输入 JSON 解析失败: {e}")
        return 2
    if plan is None:
        for m in warnings:
            eprint(f"[render_plan] WARN {m}")
        for m in errors:
            eprint(f"[render_plan] ERR  {m}")
        meta.stage_done(run_dir, "render_plan", None, status="failed")
        return 2

    tl = _jload(run_dir / F_TIMELINE)
    verrs = validate_plan(plan, tl, run_dir)
    all_errs = errors + verrs

    meta.atomic_write(run_dir / OUT_PLAN, plan)
    meta.atomic_write(run_dir / OUT_FFCONCAT, ffconcat_text(plan))
    ok_ff, ff_msg = check_ffconcat(run_dir / OUT_FFCONCAT)
    if not ok_ff:
        all_errs.append(f"ffconcat 解析失败: {ff_msg}")

    status = "done" if not all_errs else "failed"
    meta.stage_done(run_dir, "render_plan", OUT_PLAN, status=status, extra={
        "ffconcat": OUT_FFCONCAT,
        "n_vsegs": len(plan["video_track"]),
        "n_asegs": len(plan["audio_track"]),
        "n_overlays": len(plan["overlay_track"]),
        "n_warnings": len(warnings),
    })
    for m in warnings:
        eprint(f"[render_plan] WARN {m}")
    for m in all_errs:
        eprint(f"[render_plan] ERR  {m}")
    print(f"[render_plan] {OUT_PLAN}: "
          f"v{len(plan['video_track'])}/a{len(plan['audio_track'])}/"
          f"o{len(plan['overlay_track'])} total={plan['total']}s "
          f"({cfg['size'][0]}x{cfg['size'][1]}@{cfg['fps']} {cfg['aspect']}) "
          f"+ {OUT_FFCONCAT} ffprobe[{ff_msg}] —— {status}")
    return 0 if not all_errs else 1


def cmd_check(run_dir: Path) -> int:
    """--check：只重校验已写出的 70_render_plan.json。"""
    if not (run_dir / OUT_PLAN).exists():
        eprint(f"[render_plan] {OUT_PLAN} 不存在 — 先跑 render_plan")
        return 2
    if not (run_dir / F_TIMELINE).exists():
        eprint(f"[render_plan] {F_TIMELINE} 不存在 — 无法重校验")
        return 2
    plan = _jload(run_dir / OUT_PLAN)
    tl = _jload(run_dir / F_TIMELINE)
    errs = validate_plan(plan, tl, run_dir)
    ok_ff, ff_msg = (True, "skip")
    if (run_dir / OUT_FFCONCAT).exists():
        ok_ff, ff_msg = check_ffconcat(run_dir / OUT_FFCONCAT)
        if not ok_ff:
            errs.append(f"ffconcat 解析失败: {ff_msg}")
    for m in errs:
        eprint(f"[render_plan] ERR  {m}")
    print(f"[render_plan] --check {run_dir.name}: "
          f"{'OK' if not errs else f'{len(errs)} errors'} ffprobe[{ff_msg}]")
    return 0 if not errs else 1


# --------------------------------------------------------------------------- #
# selftest —— repro/ fixture + golden run + 边界用例
# --------------------------------------------------------------------------- #

def _build_repro_run(rd: Path, with_overlays: bool = True,
                     with_issue: bool = True) -> dict:
    """从 repro/{timeline,items,audio,subs,frames_v2} 造一个 timeline/1 标准 run dir。
    返回 {'expect_vsegs': n, 'shot_windows': {item:[s,e]}}。"""
    for d in ("61_audio", "65_subs", "64_frames"):
        (rd / d).mkdir(parents=True, exist_ok=True)
    for mp3 in (REPRO / "audio").glob("*.mp3"):
        shutil.copy2(mp3, rd / "61_audio" / mp3.name)
    for png in (REPRO / "subs").glob("*.png"):
        shutil.copy2(png, rd / "65_subs" / png.name)
    for png in (REPRO / "frames_v2").glob("*.png"):
        name = png.name.replace("_shot.png", ".shot.png")
        shutil.copy2(png, rd / "64_frames" / name)

    rtl = _jload(REPRO / "timeline.json")
    items_json = _jload(REPRO / "items.json")

    # shot_sentences → 绝对窗（voice 阶段产物语义）
    segs_by_item: dict[str, list[dict]] = {}
    for s in rtl["segs"]:
        segs_by_item.setdefault(s["item"], []).append(s)
    shot_win: dict[str, list[float]] = {}
    overlays = []
    for it in items_json["items"]:
        ss = it.get("shot_sentences")
        if not ss:
            continue
        isegs = segs_by_item.get(it["id"], [])
        a, b = ss[0], ss[-1]
        w = [isegs[a - 1]["start"], isegs[b - 1]["end"]]
        shot_win[it["id"]] = w
        overlays.append({"kind": "shot", "item": it["id"],
                         "src": f"64_frames/{it['id']}.shot.png",
                         "start": w[0], "end": w[1],
                         "at_sentences": list(range(a, b + 1))})

    tl = {
        "schema": "timeline/1", "episode": rd.name, "time_unit": "seconds",
        "total": rtl["total"], "lead_in": 0.6, "tail": 0.8,
        "gap": {"sentence": 0.22, "item": 0.55},
        "items": [{"id": i["id"], "start": i["start"], "end": i["end"],
                   "visual": None} for i in rtl["items"]],
        "segs": [{"n": s["n"], "seg_id": f"{s['n']:03d}_{s['item']}_{s['si']}",
                  "item": s["item"], "si": s["si"],
                  "file": f"61_audio/{s['file']}", "text": s["text"],
                  "start": s["start"], "end": s["end"],
                  "dur": round(s["end"] - s["start"], 3)}
                 for s in rtl["segs"]],
        "overlays": overlays if with_overlays else [],
    }
    meta.atomic_write(rd / F_TIMELINE, tl)

    # frames manifest：card/shot + 一个 chrome 条目（借 qwen 卡当 chrome 测试）
    files = []
    declared_shots = {it["id"] for it in items_json["items"]
                      if it.get("shot_sentences")}
    missing = []
    for png in sorted((rd / "64_frames").glob("*.png")):
        stem = png.stem
        kind = "card"
        item = stem
        if stem.endswith(".shot"):
            kind, item = "shot", stem[: -len(".shot")]
        entry = {"item": item, "kind": kind,
                 "path": f"64_frames/{png.name}",
                 "w": (png_dims(png) or (None, None))[0],
                 "h": (png_dims(png) or (None, None))[1],
                 "sha256": meta.sha256_file(png),
                 "t": shot_win.get(item) if kind == "shot" else None}
        files.append(entry)
    chrome_png = rd / "64_frames" / "qwen.chrome.png"
    shutil.copy2(rd / "64_frames" / "qwen.png", chrome_png)
    files.append({"item": "qwen", "kind": "chrome",
                  "path": "64_frames/qwen.chrome.png", "w": 1920, "h": 1080,
                  "sha256": meta.sha256_file(chrome_png), "t": None})
    for iid in declared_shots:
        if not any(f["item"] == iid and f["kind"] == "shot" for f in files):
            missing.append(f"{iid}.shot")
    meta.atomic_write(rd / F_FRAMES,
                      {"schema": "frames_manifest/1", "episode": rd.name,
                       "dir": "64_frames", "files": files,
                       "missing": sorted(missing)})

    # 63_cards.json（GeneratedContent+id envelope；icon 用占位名即可）
    meta.atomic_write(rd / F_CARDS, {
        "schema": "cards/1", "episode": rd.name,
        "renderer": "repro-fixture", "template": "claudeStyle",
        "items": [{"id": it["id"],
                   "mainTitle": it.get("nav") or it["id"],
                   "cards": [{"title": c.get("label", "")[:8],
                              "desc": c.get("body", "")[:60],
                              "icon": "info"}
                             for c in (it.get("cards") or [])][:8] or
                            [{"title": "概要", "desc": it.get("nav") or it["id"],
                              "icon": "info"}]}
                  for it in items_json["items"]]})

    if with_issue:
        meta.atomic_write(rd / F_ISSUE, {
            "schema": "issue/v1", "date": rd.name, "sections": [],
            "items": [{"id": it["id"], "section": it.get("section", ""),
                       "headline": it.get("title", ""), "tldr": "", "body": [],
                       "sources": [],
                       "video": {"shot_sentences": it.get("shot_sentences")}}
                      for it in items_json["items"]]})

    n_eff_shot = len(declared_shots - {m.split(".")[0] for m in missing})
    return {"expect_vsegs": len(rtl["items"]) + n_eff_shot,
            "shot_windows": shot_win, "missing": sorted(missing),
            "n_segs": len(rtl["segs"]), "total": rtl["total"]}


def _contig_errors(plan: dict) -> list[str]:
    errs = []
    vt = plan["video_track"]
    for a, b in zip(vt, vt[1:]):
        if abs(b["start"] - a["end"]) > CONTIG_TOL:
            errs.append(f"hole {a['end']}→{b['start']}")
    if vt and abs(vt[0]["start"]) > 1e-6:
        errs.append("first start != 0")
    if vt and abs(vt[-1]["end"] - plan["total"]) > CONTIG_TOL:
        errs.append("last end != total")
    return errs


def _pydantic_check(plan: dict) -> str:
    """contracts.models.RenderPlan 校验（aspect 已收录进 render_plan/1）。
    先校验剥离 aspect 的文档以区分"aspect 之外字段"的失败，返回诊断串。"""
    try:
        from contracts.models import RenderPlan
    except Exception as e:
        return f"pydantic/contracts 不可用（跳过 schema 校验）: {e}"
    doc = {k: v for k, v in plan.items() if k != "aspect"}
    try:
        RenderPlan.model_validate(doc)
    except Exception as e:
        return f"RenderPlan 校验失败: {str(e)[:160]}"
    try:
        RenderPlan.model_validate(plan)
        return "ok（含 aspect）"
    except Exception as e:
        return f"RenderPlan 校验失败（仅 aspect 相关）: {str(e)[:160]}"


def cmd_selftest() -> int:
    base = REPO / "runs" / "_render_plan_selftest"
    shutil.rmtree(base, ignore_errors=True)
    fails: list[str] = []

    def check(name, cond, extra=""):
        print(f"  {'PASS' if cond else 'FAIL'} {name} {extra}")
        if not cond:
            fails.append(name)

    # ---------- T1: repro 派生 run dir（overlays + issue + chrome + missing shot）
    rd = meta.ensure_run("2099-01-01", base=base)
    fx = _build_repro_run(rd, with_overlays=True, with_issue=True)
    print(f"[selftest] T1 repro run: {fx['n_segs']} segs, "
          f"missing={fx['missing']}")
    check("T1 run rc", cmd_run(rd) == 0)
    plan = _jload(rd / OUT_PLAN)
    check("T1 schema", plan["schema"] == "render_plan/1")
    check("T1 必填字段", all(k in plan for k in
          ("episode", "fps", "size", "aspect", "total",
           "video_track", "audio_track", "overlay_track")))
    check("T1 aspect/fps/size", plan["aspect"] == "16:9" and plan["fps"] == 30
          and plan["size"] == [1920, 1080])
    check("T1 total==timeline.total", abs(plan["total"] - fx["total"]) <= TOTAL_TOL)
    check("T1 满铺无洞", _contig_errors(plan) == [], ";".join(_contig_errors(plan)))
    check("T1 vseg 数 == items+有效shot",
          len(plan["video_track"]) == fx["expect_vsegs"],
          f"got {len(plan['video_track'])} want {fx['expect_vsegs']}")
    check("T1 audio_track 逐句 at==start",
          len(plan["audio_track"]) == fx["n_segs"]
          and all(abs(a["at"] - s["start"]) <= AT_TOL
                  for a, s in zip(plan["audio_track"],
                                  _jload(rd / F_TIMELINE)["segs"])))
    srcs = {t["src"] for t in plan["video_track"]}
    check("T1 missing shot 全占位为正卡",
          not any(s.endswith(("tibo.shot.png", "anthropic.shot.png",
                              "kimi.shot.png")) for s in srcs))
    check("T1 有效 shot 进 video_track",
          "64_frames/step5.shot.png" in srcs
          and "64_frames/radar.shot.png" in srcs)
    tl1 = _jload(rd / F_TIMELINE)
    s5 = next(i for i in tl1["items"] if i["id"] == "step5")
    q5 = next(i for i in tl1["items"] if i["id"] == "qwen")
    check("T1 shot 三段切分正确",
          {"src": "64_frames/step5.shot.png", "start": 45.634,
           "end": 53.194} in plan["video_track"]
          and {"src": "64_frames/step5.png", "start": 53.194,
               "end": 62.412} in plan["video_track"])
    check("T1 首 item 从 0.0 起", plan["video_track"][0]["start"] == 0.0
          and plan["video_track"][0]["src"] == "64_frames/intro.png")
    check("T1 item 卡跨过条间 gap",
          {"src": "64_frames/tibo.png", "start": 24.076,
           "end": 45.634} in plan["video_track"])
    subs = [o for o in plan["overlay_track"]
            if o["src"].startswith("65_subs/")]
    check("T1 字幕 pill 逐 seg", len(subs) == fx["n_segs"]
          and subs[0]["xy"] == SUB_XY
          and {"src": "65_subs/000.png", "start": 0.6, "end": 4.392,
               "xy": SUB_XY} in subs)
    chrome = [o for o in plan["overlay_track"] if "chrome" in o["src"]]
    check("T1 chrome overlay 覆盖 item 显示窗（含条间 gap）",
          chrome == [{"src": "64_frames/qwen.chrome.png",
                      "start": 62.412, "end": 80.246, "xy": FULL_XY}],
          str(chrome))
    check("T1 全部引用文件存在",
          all((rd / t["src"]).is_file()
              for t in plan["video_track"] + plan["audio_track"]
              + plan["overlay_track"]))
    check("T1 ffconcat 投影可解析",
          check_ffconcat(rd / OUT_FFCONCAT)[0])
    fct = (rd / OUT_FFCONCAT).read_text()
    check("T1 ffconcat 末文件重复", fct.rstrip().endswith(
        f"file '64_frames/kimi.png'"))
    check("T1 --check 绿", cmd_check(rd) == 0)
    print("  schema:", _pydantic_check(plan))

    # ---------- T2: 无 overlays → 50_issue video.shot_sentences 回退同结果
    rd2 = meta.ensure_run("2099-01-02", base=base)
    _build_repro_run(rd2, with_overlays=False, with_issue=True)
    check("T2 run rc", cmd_run(rd2) == 0)
    plan2 = _jload(rd2 / OUT_PLAN)
    check("T2 回退编译 video_track 与 T1 一致",
          plan2["video_track"] == plan["video_track"])

    # ---------- T3: golden —— 与 contracts fixture 的 70_render_plan 逐点一致
    rdg = meta.ensure_run("2026-09-20", base=base)
    for f in FIXTURE_RUN.iterdir():
        if f.is_file():
            shutil.copy2(f, rdg / f.name)
        elif f.name in ("61_audio", "64_frames", "65_subs", "63_cards"):
            shutil.copytree(f, rdg / f.name)
    check("T3 golden run rc", cmd_run(rdg) == 0)
    gp = _jload(rdg / OUT_PLAN)
    want = _jload(FIXTURE_RUN / OUT_PLAN)
    check("T3 video_track == fixture", gp["video_track"] == want["video_track"])
    check("T3 audio_track == fixture", gp["audio_track"] == want["audio_track"])
    check("T3 overlay_track == fixture",
          gp["overlay_track"] == want["overlay_track"])
    check("T3 total/fps/size", gp["total"] == want["total"]
          and gp["fps"] == want["fps"] and gp["size"] == want["size"])
    check("T3 outro visual 绑定 kimi 卡到 total",
          gp["video_track"][-1] == {"src": "64_frames/kimi.png",
                                    "start": 264.952, "end": 268.488})
    print("  schema:", _pydantic_check(gp))

    # ---------- T4: 合成边界 —— shot 尾缝 <MIN_SEG 被吸收；audio 61_audio/ 前缀回退
    rd4 = meta.ensure_run("2099-01-03", base=base)
    (rd4 / "61_audio").mkdir(parents=True)
    (rd4 / "64_frames").mkdir(parents=True)
    for name, src in (("a.png", "intro.png"), ("a.shot.png", "step5_shot.png"),
                      ("b.png", "qwen.png")):
        shutil.copy2(REPRO / "frames_v2" / src, rd4 / "64_frames" / name)
    for name, src in (("000_a_0.mp3", "000_intro_0.mp3"),
                      ("001_a_1.mp3", "001_intro_1.mp3"),
                      ("002_b_0.mp3", "002_deepseek_0.mp3")):
        shutil.copy2(REPRO / "audio" / src, rd4 / "61_audio" / name)
    tl4 = {
        "schema": "timeline/1", "episode": rd4.name, "time_unit": "seconds",
        "total": 10.5, "lead_in": 0.5, "tail": 0.5,
        "gap": {"sentence": 0.2, "item": 0.55},
        "items": [{"id": "a", "start": 0.5, "end": 5.0, "visual": None},
                  {"id": "b", "start": 5.55, "end": 10.0, "visual": None}],
        "segs": [
            {"n": 0, "seg_id": "000_a_0", "item": "a", "si": 0,
             "file": "000_a_0.mp3", "text": "第一句。",
             "start": 0.5, "end": 2.0, "dur": 1.5},
            {"n": 1, "seg_id": "001_a_1", "item": "a", "si": 1,
             "file": "001_a_1.mp3", "text": "第二句。",
             "start": 2.2, "end": 5.0, "dur": 2.8},
            {"n": 2, "seg_id": "002_b_0", "item": "b", "si": 0,
             "file": "002_b_0.mp3", "text": "第三句。",
             "start": 5.55, "end": 10.0, "dur": 4.45},
        ],
        # shot 尾距显示窗末仅 0.10s（<MIN_SEG）→ shot 应延伸至 5.55
        "overlays": [{"kind": "shot", "item": "a",
                      "src": "64_frames/a.shot.png",
                      "start": 2.2, "end": 5.45, "at_sentences": [1]}],
    }
    meta.atomic_write(rd4 / F_TIMELINE, tl4)
    files4 = []
    for p in sorted((rd4 / "64_frames").glob("*.png")):
        stem = p.stem
        kind, item = ("shot", stem[:-5]) if stem.endswith(".shot") \
            else ("card", stem)
        files4.append({"item": item, "kind": kind,
                       "path": f"64_frames/{p.name}", "w": 1920, "h": 1080,
                       "sha256": meta.sha256_file(p),
                       "t": [2.2, 5.45] if kind == "shot" else None})
    meta.atomic_write(rd4 / F_FRAMES,
                      {"schema": "frames_manifest/1", "episode": rd4.name,
                       "dir": "64_frames", "files": files4, "missing": []})
    meta.atomic_write(rd4 / F_CARDS,
                      {"schema": "cards/1", "episode": rd4.name,
                       "renderer": "t", "items": [
                           {"id": "a", "mainTitle": "A",
                            "cards": [{"title": "t", "desc": "d", "icon": "i"}]},
                           {"id": "b", "mainTitle": "B",
                            "cards": [{"title": "t", "desc": "d", "icon": "i"}]}]})
    check("T4 run rc", cmd_run(rd4) == 0)
    p4 = _jload(rd4 / OUT_PLAN)
    check("T4 微缝并入 shot（shot 延至 5.55）",
          {"src": "64_frames/a.shot.png", "start": 2.2, "end": 5.55}
          in p4["video_track"], str(p4["video_track"]))
    check("T4 audio 61_audio/ 前缀回退",
          p4["audio_track"][0]["src"] == "61_audio/000_a_0.mp3"
          and p4["audio_track"][0]["at"] == 0.5)
    check("T4 满铺无洞", _contig_errors(p4) == [])
    check("T4 无 pill 仅 warn 不 fail", cmd_check(rd4) == 0)

    # ---------- T5: 缺输入 fail-fast
    rd5 = meta.ensure_run("2099-01-04", base=base)
    check("T5 缺输入 exit=2", cmd_run(rd5) == 2
          and not (rd5 / OUT_PLAN).exists())

    shutil.rmtree(base, ignore_errors=True)
    print(f"[selftest] {'FAIL ' + str(fails) if fails else 'ALL PASS'}")
    return 1 if fails else 0


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="render_plan 编译器：62+63+64+config.render → "
                    "70_render_plan.json + 70_cards.ffconcat（PLAN §7.7）")
    ap.add_argument("--run-dir", help="runs/<date> 或日期 YYYY-MM-DD")
    ap.add_argument("--check", action="store_true",
                    help="只重校验已写的 70_render_plan.json + ffconcat")
    ap.add_argument("--selftest", action="store_true",
                    help="repro/golden/边界 fixture 端到端自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if not args.run_dir:
        ap.error("--run-dir 必填（--selftest 除外）")
    run_dir = resolve_run_dir(args.run_dir)
    if args.check:
        return cmd_check(run_dir)
    with meta.run_lock(run_dir):
        return cmd_run(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
