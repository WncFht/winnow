"""stages/lib/layout_d2.py — D2 卡片自适应闭式解（docs/PLAN.md §7.6）。

上游 juya-news-card `claudeStyle` 的自适应是 1px 递减循环 + transform scale
（floor 0.6）——card-density 实测：卡题 >22 字省略号截断、主标题 >44 字撞
minFontSize 后横向切字、desc 长时整帧 transform 缩放且 >=320 字/卡时
minCardTop<0 上下切帧。本模块按 experiments/adaptive-card-layout 的
D2-uniform 思路做闭式解：一次求出全局字号缩放 s 与 chrome 收缩参数，
经 patch_html 注入 `!important` CSS + 有界 verify 脚本，不再走 1px 循环。

接口（stages/cards.py 用法）：

    from stages.lib import layout_d2
    solved = layout_d2.solve(item_content)        # GeneratedContent dict -> dict
    html   = generateTemplateHtml(...)            # tsx 侧产物（见 render_items）
    html   = layout_d2.patch_html(html, solved)   # 注入 CSS var + verify 脚本
    _, url = layout_d2.write_for_render(html, dom_dir, f"{id}.html")
    #   ^ vendor/… 相对引用必须 <base href=file://public/> + page.goto(file://)
    page.goto(url, wait_until="networkidle"); wait ~1400ms
    m      = layout_d2.probe(page)                # -> {wrapperScale,minCardTop,clipped,...}
    ok, reasons = layout_d2.gate(m)
    # 失败 -> 再 solve（已截断文本会变化）重渲，最多 2 次 -> missing[]+flag

也支持纯 render-batch 产物的事后审计：probe(png_path) 走像素扫描
（白卡 #fff vs 底 #fbf9f6），给出 minCardTop/maxCardBottom/边缘裁切；
像素模式测不到文字截断，clipped.text=None。

solved content 里带 "_d2" plan 键（cards/1 契约 extra=forbid，写 63_cards.json
前必须 strip_d2()）。渲染侧把 "_d2" 交给 patch_html 即可，上游
generateTemplateHtml 会忽略未知字段。

自测：uv run stages/lib/layout_d2.py --selftest [out_dir]
  极端 fixture（n=5/6/7、40 字卡题、长 desc、80 字主标题、n=12）各渲
  raw/d2 两版，输出前后三指标对比表；末尾跑一遍真 render-batch.ts 做像素审计。
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_DIR = REPO_ROOT / "upstream" / "juya-news-card"
FONT_PATH = UPSTREAM_DIR / "public" / "assets" / "htmlFont.ttf"

FRAME_W, FRAME_H = 1920, 1080
BOTTOM_RESERVE = 100            # generateTemplateHtml 默认 bottomReservedPx
MAX_H = 1040 - BOTTOM_RESERVE   # 940 = upstream fitViewport maxH
ELL = "…"

# ---------------------------------------------------------------------------
# 布局几何（镜像 layout-calculator.ts DEFAULT_LAYOUT_TIERS + claudeStyle 覆盖，
# 已用 experiments/card-density/probe_* 像素级校准：cardW/top/h 全中）
# ---------------------------------------------------------------------------

# n -> (cols, padX, ctBasePx, descBasePx, descLhRatio, iconPx, wrapGap, contGap, titleInit, titleMin)
_TIER = {
    "t1":   dict(ct_base=54, ct_lh=1.25, d_base=36, d_lh=1.625, icon=72, wg=72, cg=32, t_init=90, t_min=45),
    "t2":   dict(ct_base=42, ct_lh=1.25, d_base=34, d_lh=1.35,  icon=64, wg=36, cg=24, t_init=80, t_min=40),
    "t2_5": dict(ct_base=36, ct_lh=1.25, d_base=32, d_lh=1.35,  icon=52, wg=32, cg=20, t_init=64, t_min=32),
    "t3":   dict(ct_base=33, ct_lh=1.3,  d_base=24, d_lh=1.625, icon=48, wg=32, cg=12, t_init=56, t_min=30),
}
# claudeStyle: n<=3 -> tier1; 4-6 -> tier2; 7-8 -> tier2_5; 9+ -> tier3
# titleConfig:   1-3: 90/45   4: 80/40   5-6: 72/36   7-8: 64/32   9+: 56/30

def _geom(n: int) -> dict:
    if n <= 0:
        n = 1
    if n <= 3:
        t = dict(_TIER["t1"]); t.update(cols=(1 if n == 1 else n), pad_x=(220 if n <= 2 else 96),
                                        t_init=90, t_min=45)
        cols = t["cols"]
    elif n <= 6:
        t = dict(_TIER["t2"])
        if n == 4:
            t.update(cols=2, pad_x=200, t_init=80, t_min=40)
        else:
            t.update(cols=3, pad_x=96, t_init=72, t_min=36)
        cols = t["cols"]
    elif n <= 8:
        t = dict(_TIER["t2_5"]); t.update(cols=4, pad_x=96, t_init=64, t_min=32)
        cols = 4
    else:
        t = dict(_TIER["t3"]); t.update(cols=4, pad_x=96, t_init=56, t_min=30)
        cols = 4
    wc = FRAME_W - 2 * t["pad_x"]
    if n == 1:
        card_w = wc * 2 / 3
    else:
        card_w = (wc - (cols - 1) * t["cg"]) / cols - 1
    rows = [list(range(r, min(r + cols, n))) for r in range(0, n, cols)]
    t.update(wc=wc, card_w=card_w, rows=rows,
             ct_avail=card_w - 16 - 40 - t["icon"] - 8,   # 卡padding8*2+title-box px20*2+icon+gap8
             d_avail=card_w - 16 - 40)                   # 卡padding+desc px20*2
    return t

# chrome 两档：full=上游原始留白；tight=收缩固定留白换字号
_CHROME = {
    "full":  dict(card_pad=8, tb_pt=20, tb_pb=8, d_pb=20, desc_min=80, wg_mul=1.0,  cg_mul=1.0),
    "tight": dict(card_pad=6, tb_pt=10, tb_pb=4, d_pb=10, desc_min=40, wg_mul=0.45, cg_mul=0.7),
}

PACK = 0.90          # 行宽打包系数（换行不齐、<code> padding 等余量）
CT_MIN = 20.0        # 卡题字号下限（低于此宁可截断）
S_FLOOR = 0.38       # 全局字号缩放下限，再低走 transform 兜底
TITLE_MAXW = 1700    # upstream fitTitle 的 scrollWidth 上限

# ---------------------------------------------------------------------------
# 字体宽度测量：fontTools 读 htmlFont.ttf 真实 advance（浏览器 faux-bold 不改
# advance，实测 CJK=1em）；文件缺失退回字类启发式
# ---------------------------------------------------------------------------

_FONT_CACHE: dict = {}

def _load_font(path: Path = FONT_PATH) -> Optional[dict]:
    if path in _FONT_CACHE:
        return _FONT_CACHE[path]
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(str(path))
        cmap = f.getBestCmap()
        hmtx = f["hmtx"]
        upem = f["head"].unitsPerEm
        notdef = hmtx[".notdef"][0] if ".notdef" in hmtx.metrics else upem * 0.5
        _FONT_CACHE[path] = dict(cmap=cmap, hmtx=hmtx, upem=upem, notdef=notdef)
    except Exception:
        _FONT_CACHE[path] = None
    return _FONT_CACHE[path]

def _char_em(ch: str) -> float:
    """无字体文件时的启发式字宽（em）。"""
    if ch == " ":
        return 0.30
    o = ord(ch)
    if o < 0x7F:
        return 0.55 if ch.isalnum() else 0.35
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.55

def width100(text: str) -> float:
    """文本在 100px 字号下的渲染宽度(px)。含 autoAddSpace 等价空格。

    htmlFont.ttf 只有 2852 个拉丁/符号字形、零 CJK —— 浏览器对缺失字形
    按字回退系统字体（CJK≈1em），故 cmap miss 不能按 notdef 计，走
    _char_em 启发式（与实测 scrollWidth 对齐）。"""
    f = _load_font()
    if f is None:
        return sum(_char_em(c) for c in text) * 100
    cmap, hmtx, upem = f["cmap"], f["hmtx"], f["upem"]
    w = 0.0
    for c in text:
        o = ord(c)
        if o in cmap:
            w += hmtx[cmap[o]][0] / upem * 100
        else:
            w += _char_em(c) * 100
    return w

_TAG_RE = re.compile(r"<[^>]+>")
_BOUNDARY_RE1 = re.compile(r"([一-龥])([a-zA-Z0-9])")
_BOUNDARY_RE2 = re.compile(r"([a-zA-Z0-9])([一-龥])")

def auto_spaced(text: str) -> str:
    """复刻 upstream autoAddSpace：中英/中数边界补一个空格。"""
    text = _BOUNDARY_RE1.sub(r"\1 \2", text)
    text = _BOUNDARY_RE2.sub(r"\1 \2", text)
    return text

def desc_width100(html: str) -> float:
    """desc 行内 HTML 的 100px 宽度：<code> 0.9em+0.6em padding+1px 边，其余按字面。"""
    w = 0.0
    for m in re.finditer(r"<code>(.*?)</code>|<[^>]+>|([^<]+)", html, re.S):
        if m.group(1) is not None:
            seg = auto_spaced(_TAG_RE.sub("", m.group(1)))
            w += width100(seg) * 0.9 + 66   # 0.9em 字 + 0.1em*2*3 padding(0.3em*2) + border1
        elif m.group(2):
            w += width100(auto_spaced(m.group(2)))
    return w

def _fit_prefix(text: str, max_w100: float) -> tuple[str, bool]:
    """nowrap 放不下时截到 max_w100 + '…'。返回 (文本, 是否截断)。"""
    if width100(text) <= max_w100:
        return text, False
    ell_w = width100(ELL)
    lo, hi = 0, len(text)
    while lo < hi:                      # 二分最长可保留前缀（纯文本运算，非 DOM 循环）
        mid = (lo + hi + 1) // 2
        if width100(text[:mid]) + ell_w <= max_w100:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ELL, True

# ---------------------------------------------------------------------------
# 闭式解
# ---------------------------------------------------------------------------

def _title_fs(geom: dict, w100: float) -> float:
    """主标题字号：nowrap 且 <=1700 内容宽。"""
    if w100 <= 0:
        return geom["t_init"]
    return min(geom["t_init"], TITLE_MAXW * PACK / w100 * 100)

def _card_h(s: float, i: int, geom: dict, ch: dict, desc_w: list[float],
            ct_base_eff: float) -> float:
    """单卡高度（exact 模型：ceil 行数），s 为全局字号缩放。"""
    icon_h = geom["icon"] * s
    ct_h = ct_base_eff * s * geom["ct_lh"]
    d_fs = geom["d_base"] * s
    lines = max(1, math.ceil(desc_w[i] * d_fs / (100 * geom["d_avail"] * PACK)))
    desc_h = lines * d_fs * geom["d_lh"]
    desc_div = max(ch["desc_min"], desc_h + ch["d_pb"])
    return 2 * ch["card_pad"] + 2 + ch["tb_pt"] + max(icon_h, ct_h) + ch["tb_pb"] + desc_div

def _wrapper_h(s: float, geom: dict, ch: dict, desc_w: list[float],
               title_fs: float, ct_base_eff: float) -> float:
    row_hs = [max(_card_h(s, i, geom, ch, desc_w, ct_base_eff) for i in row)
              for row in geom["rows"]]
    return (title_fs * 1.2 + geom["wg"] * ch["wg_mul"]
            + sum(row_hs) + (len(row_hs) - 1) * geom["cg"] * ch["cg_mul"])

def _solve_s(geom: dict, ch: dict, desc_w: list[float], title_fs: float,
             ct_base_eff: float) -> float:
    """闭式求 s：每行按其 dominant 卡的二次多项式卡 H(s)=B_r（行高占比分摊预算），
    取 min；一次 exact 复算 + 一步 sqrt 修正封顶。"""
    if _wrapper_h(1.0, geom, ch, desc_w, title_fs, ct_base_eff) <= MAX_H:
        return 1.0
    budget = MAX_H - title_fs * 1.2 - geom["wg"] * ch["wg_mul"] \
        - (len(geom["rows"]) - 1) * geom["cg"] * ch["cg_mul"]
    row_h1 = [max(_card_h(1.0, i, geom, ch, desc_w, ct_base_eff) for i in row)
              for row in geom["rows"]]
    tot = sum(row_h1)
    s_min = 1.0
    for row, rh1 in zip(geom["rows"], row_h1):
        i = max(row, key=lambda k: _card_h(1.0, k, geom, ch, desc_w, ct_base_eff))
        b_r = budget * rh1 / tot
        # cardH_i(s) ≈ A + B s + C s^2（desc 高于 desc_min 时走二次支）
        a = 2 * ch["card_pad"] + 2 + ch["tb_pt"] + ch["tb_pb"]
        b = max(geom["icon"], ct_base_eff * geom["ct_lh"])
        c = desc_w[i] * geom["d_base"] ** 2 * geom["d_lh"] / (100 * geom["d_avail"] * PACK)
        # 若 s=1 时 descDiv 就是 min 层高（短文卡），则该卡高度线性
        if desc_w[i] * geom["d_base"] / (100 * geom["d_avail"] * PACK) * geom["d_base"] * geom["d_lh"] \
                + ch["d_pb"] <= ch["desc_min"]:
            c = 0.0
            a += ch["desc_min"]
        else:
            a += ch["d_pb"]
        # 解 A + B s + C s^2 = b_r
        if c > 0:
            disc = b * b + 4 * c * (b_r - a)
            s_r = (-b + math.sqrt(max(disc, 0.0))) / (2 * c) if disc > 0 else 0.0
        else:
            s_r = (b_r - a) / b if b > 0 else 1.0
        s_min = min(s_min, s_r)
    s = max(S_FLOOR, min(1.0, s_min))
    h1 = _wrapper_h(s, geom, ch, desc_w, title_fs, ct_base_eff)
    if h1 > MAX_H:                       # ceil 取整残差的一步修正（非迭代）
        s = max(S_FLOOR, s * math.sqrt(MAX_H / h1) * 0.985)
    return s

def solve(content: dict, *, max_h: float = MAX_H) -> dict:
    """GeneratedContent -> 调整后的 content（拷带 `_d2` plan；写契约前 strip_d2）。

    一次闭式通过：主标题字号 -> 卡题 fit/截断 -> 全局字号缩放 s（full/tight
    两档 chrome 取更宽容的一档）-> 极端时再截断。不改 desc 文本（信息不丢）。
    """
    global MAX_H
    if max_h != MAX_H:                   # 允许调用方覆盖（极少用）
        MAX_H = max_h
    out = {"mainTitle": str(content.get("mainTitle") or ""),
           "cards": [dict(c) for c in content.get("cards") or []]}
    for k, v in content.items():         # 透传调用方自有字段（id 等由上层摘）
        if k not in out and not k.startswith("_"):
            out[k] = v
    cards = out["cards"]
    n = len(cards)
    flags: list[str] = []
    if n == 0:
        out["_d2"] = {"v": 1, "noop": True, "flags": ["no_cards"], "n": 0}
        return out
    geom = _geom(n)

    # 1) 主标题：闭式字号；撞 min 仍超宽 -> 截断
    tf = _title_fs(geom, width100(auto_spaced(out["mainTitle"])))
    if tf < geom["t_min"]:
        tf = geom["t_min"]
        new_t, cut = _fit_prefix(out["mainTitle"], TITLE_MAXW * PACK / tf * 100)
        if cut:
            out["mainTitle"] = new_t
            flags.append("main_title_truncated")
        else:
            flags.append("main_title_below_min")

    # 2) 卡题 nowrap fit（测 faux-bold 不变宽；先记每卡能容纳的字号）
    ct_fit = []
    for c in cards:
        w = width100(str(c.get("title") or ""))
        ct_fit.append(geom["ct_avail"] * PACK / w * 100 if w > 0 else geom["ct_base"])
    desc_w = [desc_width100(str(c.get("desc") or "")) for c in cards]

    # 3) 全局 s：full chrome 先试；<0.72 换 tight chrome 重解（一次性，二选一）
    ch = _CHROME["full"]
    s = _solve_s(geom, ch, desc_w, tf, geom["ct_base"])
    chrome = "full"
    if s < 0.72:
        s_t = _solve_s(geom, _CHROME["tight"], desc_w, tf, geom["ct_base"])
        if s_t > s * 1.12:
            ch, s, chrome = _CHROME["tight"], s_t, "tight"
            flags.append("tight_chrome")
    if s < 1.0:
        flags.append("font_scaled")

    # 4) 卡题在应用字号下仍超宽 -> 截断（fs 下不了 CT_MIN 就截文本）
    ct_fs = geom["ct_base"] * s
    trunc = []
    for i, c in enumerate(cards):
        if ct_fit[i] < ct_fs:
            fs_use = max(CT_MIN, ct_fs)
            new_t, cut = _fit_prefix(str(c.get("title") or ""),
                                     geom["ct_avail"] * PACK / fs_use * 100)
            if cut:
                cards[i]["title"] = new_t
                trunc.append(i)
                ct_fit[i] = fs_use     # 截断后刚好 fit
    if trunc:
        flags.append("card_title_truncated")
    if s <= S_FLOOR + 1e-6:
        flags.append("tx_fallback_expected")   # verify 端会用 transform 兜底

    plan = {
        "v": 1, "n": n, "chrome": chrome,
        "fontScale": round(s, 4),
        "mainTitlePx": round(tf, 1),
        # base*：未乘 s 的档位基准，CSS 里 calc(base * var(--d2-s)) 应用一次
        "cardTitleBase": geom["ct_base"], "descBase": geom["d_base"],
        "iconBase": geom["icon"],
        "cardTitlePx": round(geom["ct_base"] * s, 2),   # 应用后（= base*s）供报告
        "descPx": round(geom["d_base"] * s, 2),
        "descLh": geom["d_lh"],
        "iconPx": round(geom["icon"] * s, 2),
        "wgPx": round(geom["wg"] * ch["wg_mul"], 1),
        "cgPx": round(geom["cg"] * ch["cg_mul"], 1),
        "cardPadPx": ch["card_pad"], "tbPtPx": ch["tb_pt"], "tbPbPx": ch["tb_pb"],
        "dPbPx": ch["d_pb"], "descMinPx": ch["desc_min"],
        "truncated": trunc,
        "predictedH": round(_wrapper_h(s, geom, ch, desc_w, tf, geom["ct_base"]), 1),
        "flags": flags,
    }
    out["_d2"] = plan
    return out

def strip_d2(content: dict) -> dict:
    """剥掉 `_d2`（及任何 `_` 前缀键）以满足 cards/1 的 extra=forbid。"""
    return {k: v for k, v in content.items() if not k.startswith("_")}

# ---------------------------------------------------------------------------
# HTML 注入：!important CSS（calc(var(--d2-s)) 受 verify 端微调）+ 有界 verify
# ---------------------------------------------------------------------------

def _d2_css(p: dict) -> str:
    if p.get("noop"):
        # 仍注入 transform var 兜底：上游 fitViewport 的 inline transform 会被
        # !important 压制，verify 发现真溢出时用 --d2-tx（无 0.6 下限）接管
        return (".content-wrapper{transform:scale(var(--d2-tx,1)) !important;"
                "transform-origin:center center !important}")
    return f"""
.main-title{{font-size:{p['mainTitlePx']}px !important}}
.card-item .card-title{{font-size:calc({p.get('cardTitleBase', p['cardTitlePx'])}px * var(--d2-s,1)) !important}}
.card-item .js-desc{{font-size:calc({p.get('descBase', p['descPx'])}px * var(--d2-s,1)) !important}}
.card-item .title-box .material-symbols-rounded{{font-size:calc({p.get('iconBase', p['iconPx'])}px * var(--d2-s,1)) !important}}
.content-wrapper{{gap:{p['wgPx']}px !important;transform:scale(var(--d2-tx,1)) !important;
 transform-origin:center center !important}}
.card-zone>div{{gap:{p['cgPx']}px !important;--container-gap:{p['cgPx']}px !important}}
.card-item{{padding:{p['cardPadPx']}px !important}}
.card-item .title-box{{padding:{p['tbPtPx']}px 20px {p['tbPbPx']}px !important}}
.card-item>.flex-1{{min-height:{p['descMinPx']}px !important;padding-bottom:{p['dPbPx']}px !important}}
"""

_VERIFY_JS = r"""
<script>
(function(){
  var MAXH = __MAXH__, S0 = __S0__;
  var root = document.documentElement;
  var st = {it: 0};
  function setS(v){ root.style.setProperty('--d2-s', String(v)); }
  function setTx(v){ root.style.setProperty('--d2-tx', String(v)); }
  function curS(){ var v = root.style.getPropertyValue('--d2-s');
                   var f = parseFloat(v); return isFinite(f) ? f : S0; }
  setS(S0); setTx(1);
  function verify(){
    var w = document.querySelector('.content-wrapper');
    if (!w) return;
    var H = w.scrollHeight;              // transform 不影响 scrollHeight
    if (H > MAXH + 1 && st.it < 3){      // 有界 verify：字号再压 sqrt 步
      st.it++;
      setS(Math.max(0.30, curS() * Math.sqrt(MAXH / H) * 0.985));
      setTimeout(verify, 60);
      return;
    }
    // 字号到位仍超 -> transform 兜底（无 0.6 下限，居中不裁顶）
    setTx(w.scrollHeight > MAXH + 1 ? Math.min(1, MAXH / w.scrollHeight) : 1);
    window.__D2 = {s: curS(), tx: parseFloat(root.style.getPropertyValue('--d2-tx')) || 1,
                   h: w.scrollHeight, iters: st.it};
  }
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', verify);
  else verify();
  setTimeout(verify, 300); setTimeout(verify, 800);
  if (document.fonts && document.fonts.ready)
    document.fonts.ready.then(function(){ setTimeout(verify, 30); });
})();
</script>
"""

def patch_html(html: str, content_or_plan: dict) -> str:
    """把 solve 产物注入 generateTemplateHtml 的输出。可传 solved content
    （自动取 ['_d2']）或裸 plan。幂等（重复调用替换旧 D2 块）。"""
    p = content_or_plan.get("_d2", content_or_plan)
    s0 = 1.0 if p.get("noop") else max(0.30, float(p.get("fontScale", 1.0)))
    css = f'<style id="d2css">{_d2_css(p)}</style>'
    js = _VERIFY_JS.replace("__MAXH__", str(int(MAX_H))).replace("__S0__", f"{s0:.4f}")
    html = re.sub(r'<style id="d2css">.*?</style>', "", html, flags=re.S)
    html = re.sub(r'<script>\s*\(function\(\)\{\s*var MAXH.*?</script>', "", html, flags=re.S)
    if "</head>" in html:
        html = html.replace("</head>", css + "</head>", 1)
    else:
        html = css + html
    if "</body>" in html:
        html = html.replace("</body>", js + "</body>", 1)
    else:
        html += js
    return html

def font_face_block(font_path: Path = FONT_PATH) -> str:
    """render-batch.ts 同款 CustomPreviewFont 注入块。"""
    b64 = base64.b64encode(font_path.read_bytes()).decode()
    return ("<style>@font-face{font-family:'CustomPreviewFont';"
            f"src:url(data:font/ttf;base64,{b64}) format('truetype');}}"
            ".main-container{font-family:'CustomPreviewFont',system-ui,"
            "-apple-system,sans-serif !important;}</style>")

# ---------------------------------------------------------------------------
# probe：page -> DOM 三指标；png path -> 像素扫描兜底
# ---------------------------------------------------------------------------

_PROBE_JS = r"""
() => {
  const wrapper = document.querySelector('.content-wrapper');
  const title = document.querySelector('.main-title');
  const tr = wrapper ? getComputedStyle(wrapper).transform : 'none';
  let scale = 1;
  if (tr && tr !== 'none') {
    const m = tr.match(/matrix\(([^,]+)/);
    if (m) scale = parseFloat(m[1]);
  }
  const cards = Array.from(document.querySelectorAll('.card-item'));
  const rects = cards.map(el => { const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right}; });
  const ctStats = Array.from(document.querySelectorAll('.card-title')).map(el => ({
    fs: parseFloat(getComputedStyle(el).fontSize),
    truncated: el.scrollWidth > el.clientWidth + 1,
    overBy: el.scrollWidth - el.clientWidth }));
  const dStats = Array.from(document.querySelectorAll('.js-desc')).map(el => ({
    fs: parseFloat(getComputedStyle(el).fontSize),
    h: el.getBoundingClientRect().height }));
  const tRect = title ? title.getBoundingClientRect() : null;
  return {
    mode: 'dom',
    n: cards.length,
    wrapperScale: scale,
    wrapperScrollH: wrapper ? wrapper.scrollHeight : 0,
    minCardTop: rects.length ? Math.min(...rects.map(r => r.top)) : null,
    maxCardBottom: rects.length ? Math.max(...rects.map(r => r.bottom)) : null,
    cardRects: rects,
    titleFontPx: title ? parseFloat(getComputedStyle(title).fontSize) : 0,
    titleScrollW: title ? title.scrollWidth : 0,
    titleRect: tRect ? {left: tRect.left, right: tRect.right} : null,
    cardTitleStats: ctStats,
    descStats: dStats,
    d2: window.__D2 || null,
  };
}
"""

_PNG_JS = r"""
async (dataUrl) => {
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = dataUrl; });
  const W = img.naturalWidth, H = img.naturalHeight;
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d'); ctx.drawImage(img, 0, 0);
  const d = ctx.getImageData(0, 0, W, H).data;
  // 白卡 #fff vs 底 #fbf9f6：b>=254 && g>=253 只命中卡片
  const rows = new Array(H).fill(0);
  for (let y = 0; y < H; y++) {
    let cnt = 0;
    for (let x = 0; x < W; x += 4) {
      const o = (y * W + x) * 4;
      if (d[o] >= 254 && d[o+1] >= 253 && d[o+2] >= 254) cnt++;
    }
    rows[y] = cnt / (W / 4);
  }
  const thr = 0.03;
  let top = -1, bot = -1;
  for (let y = 0; y < H; y++) if (rows[y] > thr) { if (top < 0) top = y; bot = y; }
  return { mode: 'png', n: null, wrapperScale: null,
           minCardTop: top, maxCardBottom: bot,
           edgeClipTop: top >= 0 && top <= 1 && rows[0] > thr,
           edgeClipBottom: bot === H - 1 && rows[H - 1] > thr,
           titleScrollW: 0, cardTitleStats: [], descStats: [] };
}
"""

def _clip_info(m: dict) -> dict:
    rects = m.get("cardRects") or []
    outside = [i for i, r in enumerate(rects)
               if r["top"] < -0.5 or r["bottom"] > FRAME_H + 0.5
               or r["left"] < -0.5 or r["right"] > FRAME_W + 0.5]
    trunc = [i for i, c in enumerate(m.get("cardTitleStats") or []) if c.get("truncated")]
    title_clip = bool(m.get("titleScrollW", 0) > FRAME_W + 1
                      or (m.get("titleRect")
                          and (m["titleRect"]["left"] < -0.5 or m["titleRect"]["right"] > FRAME_W + 0.5)))
    return {"main_title": title_clip, "card_titles": trunc, "cards_outside": outside,
            "text": None if m.get("mode") == "png" else bool(title_clip or trunc)}

def probe(target: Any, *, browser: Any = None, settle_ms: int = 0) -> dict:
    """三指标探针。target 为 playwright Page（DOM 模式）或 png 路径（像素模式）。

    DOM 模式：settle_ms>0 时先再等 settle_ms 让 verify 收尾。
    像素模式：需要 browser（sync playwright Browser）；不传则临时 launch。
    返回 {mode, wrapperScale, minCardTop, maxCardBottom, clipped{...}, n, ...}。
    """
    if hasattr(target, "evaluate"):                      # playwright Page
        if settle_ms:
            target.wait_for_timeout(settle_ms)
        m = target.evaluate(_PROBE_JS)
        m["clipped"] = _clip_info(m)
        return m
    path = Path(target)                                  # png 像素模式
    data_url = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    own = browser is None
    if own:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        env = dict(os.environ)
        env.setdefault("TMPDIR", str(Path.home() / ".cache"))   # /tmp tmpfs 常满
        browser = pw.chromium.launch(env=env)
    try:
        pg = browser.new_page(viewport={"width": FRAME_W, "height": FRAME_H})
        m = pg.evaluate(_PNG_JS, data_url)
        pg.close()
    finally:
        if own:
            browser.close(); pw.stop()
    m["clipped"] = {"main_title": None, "card_titles": [], "text": None,
                    "cards_outside": [],
                    "edge": bool(m.get("edgeClipTop") or m.get("edgeClipBottom"))}
    return m

def gate(m: dict) -> tuple[bool, list[str]]:
    """PASS 判据：minCardTop>=0 且 maxCardBottom<=1080 且无任何裁切；
    wrapperScale<1 记 flag 'scaled'（可放行的次优）；<-0.5/超界为 hard fail。"""
    reasons: list[str] = []
    ok = True
    top, bot = m.get("minCardTop"), m.get("maxCardBottom")
    if top is None or top < -0.5:
        ok = False; reasons.append(f"minCardTop={top}")
    if bot is None or bot > FRAME_H + 0.5:
        ok = False; reasons.append(f"maxCardBottom={bot}")
    cl = m.get("clipped") or {}
    if cl.get("main_title"):
        ok = False; reasons.append("main_title clipped")
    if cl.get("card_titles"):
        ok = False; reasons.append(f"card_titles truncated idx={cl['card_titles']}")
    if cl.get("cards_outside"):
        ok = False; reasons.append(f"cards outside frame idx={cl['cards_outside']}")
    if cl.get("edge"):
        ok = False; reasons.append("edge clip (png)")
    s = m.get("wrapperScale")
    if s is not None and s < 0.995:
        reasons.append(f"wrapperScale={s:.3f} (scaled, sub-optimal)")
    return ok, reasons

# ---------------------------------------------------------------------------
# 一体化渲染驱动（cards.py 可直接用；也是 selftest 的通路）
# ---------------------------------------------------------------------------

_GEN_MTS = r"""
import fs from 'fs';
import path from 'path';
import { generateTemplateHtml } from 'file://__SSR__';
const [inPath, outDir] = process.argv.slice(2);
const items = JSON.parse(fs.readFileSync(inPath, 'utf-8'));
fs.mkdirSync(outDir, { recursive: true });
for (const item of items) {
  const { id, template, ...content } = item;
  const html = generateTemplateHtml(content, template || 'claudeStyle');
  fs.writeFileSync(path.join(outDir, id + '.html'), html);
  console.log(id);
}
"""

def gen_html(items: list[dict], out_dir: Path, upstream: Path = UPSTREAM_DIR) -> dict[str, Path]:
    """items[{id,mainTitle,cards,template?}] -> {id: html_path}（tsx 一次性产出）。"""
    work = Path(out_dir).resolve() / "_d2_work"   # cwd 切到 upstream，必须绝对路径
    work.mkdir(parents=True, exist_ok=True)
    gen = work / "gen_html.mts"
    gen.write_text(_GEN_MTS.replace("__SSR__", str(upstream / "src/templates/ssr-runtime.ts")))
    items_path = work / "items.json"
    items_path.write_text(json.dumps(items, ensure_ascii=False))
    cp = subprocess.run(["npx", "tsx", str(gen), str(items_path), str(work / "html")],
                        cwd=str(upstream), capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"gen_html failed rc={cp.returncode}: {cp.stderr[-600:]}")
    return {p.stem: p for p in (work / "html").glob("*.html")}

def _public_file_href(upstream: Path) -> str:
    """上游 public/ 的 file:// URL（带尾斜杠）——vendor/assets 相对引用的 base。"""
    return (upstream / "public").as_uri() + "/"

def write_for_render(html: str, out_dir: Path, name: str,
                     upstream: Path = UPSTREAM_DIR) -> tuple[Path, str]:
    """镜像 upstream vendor-assets.writeHtmlForFileRender：注入 <base> 后写盘。

    CDN 自托管后 HTML 里是相对 `vendor/…`、`assets/…` 引用；setContent 的
    about:blank 文档无法解析也不允许加载 file:// 子资源，必须写盘 +
    `<base href="file://…/public/">` 后 page.goto(file://…)。
    """
    out_dir = Path(out_dir).resolve()             # as_uri 需要绝对路径
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f'<base href="{_public_file_href(upstream)}" />'
    html = html.replace("<head>", f"<head>\n  {base}", 1) if "<head>" in html \
        else base + html
    p = out_dir / name
    p.write_text(html)
    return p, p.as_uri()

def render_items(items: list[dict], out_dir: Path, *, upstream: Path = UPSTREAM_DIR,
                 browser: Any = None, shots: bool = True, d2: bool = True,
                 settle_ms: int = 1400, timeout: int = 30_000) -> list[dict]:
    """solve -> gen_html -> patch -> playwright 渲染 -> probe (+png)。

    items: [{id, mainTitle, cards, template?}]。返回 [{id, plan, metrics, gate, png}].
    d2=False 时跳过 solve/patch，等价上游裸渲（before 对照）。
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    solved: dict[str, dict] = {}
    render_in = []
    for it in items:
        it = dict(it)
        cid = it.get("id") or f"item{len(render_in)}"
        content = {k: v for k, v in it.items() if k not in ("id", "template")}
        if d2:
            content = solve(content)
            solved[cid] = content
        render_in.append({"id": cid, "template": it.get("template", "claudeStyle"),
                          **strip_d2(content)})
    htmls = gen_html(render_in, out_dir, upstream)
    ff = font_face_block()

    own = browser is None
    pw = None
    if own:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        env = dict(os.environ)
        env.setdefault("TMPDIR", str(Path.home() / ".cache"))   # /tmp tmpfs 常满
        browser = pw.chromium.launch(env=env)
    results = []
    try:
        ctx = browser.new_context(viewport={"width": FRAME_W, "height": FRAME_H},
                                  device_scale_factor=1)
        for it in render_in:
            cid = it["id"]
            html = htmls[cid].read_text()
            html = html.replace("</head>", ff + "</head>", 1)
            if d2 and cid in solved:
                html = patch_html(html, solved[cid])
            _, file_url = write_for_render(html, out_dir / "_d2_work" / "dom",
                                           f"{cid}{'' if d2 else '_raw'}.html", upstream)
            pg = ctx.new_page()
            pg.set_default_timeout(timeout)
            pg.goto(file_url, wait_until="networkidle")
            pg.evaluate("() => document.fonts && document.fonts.ready")
            m = probe(pg, settle_ms=settle_ms)
            png = None
            if shots:
                png = out_dir / f"{cid}{'' if d2 else '_raw'}.png"
                pg.screenshot(path=str(png))
            pg.close()
            ok, reasons = gate(m)
            results.append({"id": cid, "n": len(it["cards"]),
                            "plan": (solved.get(cid) or {}).get("_d2"),
                            "metrics": m, "gate_ok": ok, "gate_reasons": reasons,
                            "png": str(png) if png else None})
    finally:
        if own:
            browser.close(); pw.stop()
    return results

# ---------------------------------------------------------------------------
# selftest：极端 fixture 的 before/after 三指标 + 真 render-batch 像素审计
# ---------------------------------------------------------------------------

def _mk_cards(n: int, title_len: int, desc: str) -> list[dict]:
    icons = ["campaign", "group", "leaderboard", "translate", "dataset",
             "target", "lock_open", "auto_awesome"]
    t = "卡片标题压力测试超长标题甲乙丙丁戊己庚辛壬癸" * 3
    return [{"title": t[:title_len], "desc": desc, "icon": icons[i % len(icons)]}
            for i in range(n)]

def _selftest(out_dir: Path) -> int:
    desc160 = ("面向真实对话场景的实时同传能力，支持 <strong>60 种语言</strong> 互译，"
               "字均延迟由 <code>2.8s</code> 降至 <code>2.3s</code>。") * 2
    desc320 = ("覆盖 <strong>18</strong> 个解剖结构与 <strong>146</strong> 项影像发现，"
               "内外部验证诊断 AUC 均超过 <strong>0.87</strong>，权重已开放下载。") * 4
    items = [
        {"id": "n5-t40", "mainTitle": "五卡片长标题压测", "cards": _mk_cards(5, 40, desc160)},
        {"id": "n6-t40", "mainTitle": "六卡片长标题压测", "cards": _mk_cards(6, 40, desc160)},
        {"id": "n7-t40", "mainTitle": "七卡片长标题压测", "cards": _mk_cards(7, 40, desc160)},
        {"id": "n6-d320", "mainTitle": "六卡片超长描述", "cards": _mk_cards(6, 8, desc320)},
        {"id": "n8-d160", "mainTitle": "八卡片中长描述", "cards": _mk_cards(8, 12, desc160)},
        {"id": "mt-80", "mainTitle": "OpenAI正式发布GPT-6系列模型并同步上线企业级多模态"
                                    "智能体平台与下一代推理计费体系引发行业广泛关注",
         "cards": _mk_cards(4, 8, desc160[:60])},
        {"id": "n12", "mainTitle": "十二卡片极端密度", "cards": _mk_cards(12, 8, "短描述。")},
        {"id": "n4-ok", "mainTitle": "四卡片正常基线", "cards": _mk_cards(4, 6, "调休上班的周末及法定节假日，全天按<strong>空闲时段费率</strong>计费")},
    ]
    print(f"== D2 selftest -> {out_dir}")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    env = dict(os.environ); env.setdefault("TMPDIR", str(Path.home() / ".cache"))
    br = pw.chromium.launch(env=env)
    rows = []
    try:
        raw = render_items(items, out_dir / "raw", browser=br, d2=False, shots=True)
        new = render_items(items, out_dir / "d2", browser=br, d2=True, shots=True)
    finally:
        br.close(); pw.stop()
    raw_by_id = {r["id"]: r for r in raw}
    print(f"{'id':9s} {'n':>2s} | {'raw scale':>9s} {'raw top':>7s} {'raw bot':>7s} {'rawOK':>5s}"
          f" | {'d2 s':>5s} {'tx':>5s} {'d2 top':>6s} {'d2 bot':>6s} {'d2OK':>4s}  flags")
    for r in new:
        m, rm = r["metrics"], raw_by_id[r["id"]]["metrics"]
        d2i = m.get("d2") or {}
        plan = r.get("plan") or {}
        print(f"{r['id']:9s} {r['n']:>2d} | {rm['wrapperScale']:>9.3f} "
              f"{rm['minCardTop']:>7.0f} {rm['maxCardBottom']:>7.0f} "
              f"{str(raw_by_id[r['id']]['gate_ok']):>5s} | "
              f"{plan.get('fontScale', 1):>5.2f} {d2i.get('tx', 1):>5.2f} "
              f"{m['minCardTop']:>6.0f} {m['maxCardBottom']:>6.0f} "
              f"{str(r['gate_ok']):>4s}  {','.join(plan.get('flags', []))}"
              f"{'|' + '|'.join(r['gate_reasons']) if not r['gate_ok'] else ''}")
        rows.append(r)
    # 真 render-batch.ts 路径 + 像素 probe（cwd 切到 upstream，路径必须绝对）
    rb_dir = (out_dir / "render-batch").resolve()
    rb_dir.mkdir(parents=True, exist_ok=True)
    items_json = rb_dir / "items.json"
    items_json.write_text(json.dumps(
        [{"id": it["id"], "template": "claudeStyle",
          **{k: v for k, v in it.items() if k != "id"}} for it in items],
        ensure_ascii=False))
    cp = subprocess.run(["npx", "tsx", "scripts/render-batch.ts",
                         str(items_json), str(rb_dir / "png")],
                        cwd=str(UPSTREAM_DIR), capture_output=True, text=True)
    print("== render-batch.ts:", cp.returncode == 0 and "ok" or cp.stderr[-400:])
    for png in sorted((rb_dir / "png").glob("*.png")):
        m = probe(png)
        ok, reasons = gate(m)
        print(f"  rb:{png.stem:9s} top={m['minCardTop']} bot={m['maxCardBottom']} "
              f"edgeClip={m['clipped'].get('edge')} ok={ok} {reasons}")
    fails = [r for r in rows if not r["gate_ok"]]
    print(f"== d2 pass {len(rows) - len(fails)}/{len(rows)}")
    return 1 if fails else 0

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        idx = sys.argv.index("--selftest")
        out = Path(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else \
            REPO_ROOT / "out" / "layout-d2-selftest"
        sys.exit(_selftest(out))
    print(__doc__)
