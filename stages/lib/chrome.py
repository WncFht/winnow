"""stages/lib/chrome.py — 视频级 chrome 叠加层渲染（PLAN.md §7.6）。

移植自 repro/render_chrome.py（已踩坑修复版），数据面换成 issue/v1：
  - file:// workaround：pg.set_content() 加载不了 file:// 图片 → HTML 写到
    磁盘再 pg.goto(file.as_uri())（.html 中间件同目录留档便于排查）；
  - omit_background=True → 透明 PNG（nav pill / 面包屑 / 截图弹卡），叠加在
    上游卡片之上；intro_body 为不透明整帧；
  - Smiley 字体改为绝对 file:// URI 指向 repro/assets/（repro 里相对路径
    'assets/…' 相对 chrome/ 目录实际 404、一直在静默回退，这里修正）。

API：
    from lib import chrome
    pngs = chrome.render(run_dir, issue)
    # -> {"nav_intro": Path, "nav_s0".."nav_sN": Path,
    #     "crumb_intro": Path, "crumb_<id>": Path,
    #     "shot_<id>": Path, "intro_body": Path}
    # 全部 1920x1080，落在 run_dir/chrome/

issue 为解析后的 50_issue.json dict（issue/v1）：sections[].slug/name，
items[].section 是 slug，nav 缺省回退 id；截图弹卡取 item.media[] 中
kind=="shot" 的 src——相对路径按 run_dir 解析，http(s) 原样使用；文件
不存在则不产 shot_<id>（由调用方记入 64_frames_manifest.missing[]）。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Union

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
FONT_DIR = REPO_ROOT / "repro" / "assets"  # SmileySans-Oblique.ttf（概览卡标题字）
W, H = 1920, 1080

INTRO_CRUMB_ID = "__intro__"  # 面包屑最左 Intro 格（issue/v1 的 intro 不在 items[] 里）
INTRO_LABEL = "Intro"


def _css() -> str:
    font_face = ""
    ttf = FONT_DIR / "SmileySans-Oblique.ttf"
    if ttf.exists():
        font_face = (
            "@font-face { font-family: 'Smiley'; "
            f"src: url('{ttf.as_uri()}'); }}"
        )
    return font_face + """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 1920px; height: 1080px; }
body {
  font-family: 'Noto Sans CJK SC', 'Noto Sans SC', sans-serif;
  overflow: hidden;
}
.nav {
  position: absolute; top: 34px; left: 50%; transform: translateX(-50%);
  display: flex; border-radius: 14px;
  background: #fdfbf6; border: 2px solid #e3ddcd; overflow: hidden;
  box-shadow: 0 2px 10px rgba(90,80,60,.08);
}
.nav div {
  padding: 14px 30px; font-size: 27px; font-weight: 600; color: #6b6459;
  border-right: 1.5px solid #e7e1d2; white-space: nowrap;
}
.nav div:last-child { border-right: none; }
.nav div.on { background: #d14f27; color: #fff; }
.crumb {
  position: absolute; left: 0; right: 0; bottom: 0;
  display: flex; justify-content: center; align-items: stretch;
  background: rgba(253,251,246,.92); border-top: 2px solid #e3ddcd;
}
.crumb div {
  padding: 13px 10px; font-size: 20px; color: #8a8175; white-space: nowrap;
  border-right: 1px solid #e9e3d4; display: flex; align-items: center;
  flex: 1 1 auto; justify-content: center; min-width: 0; overflow: hidden;
}
.crumb div:last-child { border-right: none; }
.crumb div.on { background: #f7dcd2; color: #c2452a; font-weight: 700; }
.dim { position: absolute; inset: 0; background: rgba(60,50,40,.18); }
.shotwrap {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
}
.shotcard {
  background: #fff; border-radius: 24px; padding: 22px;
  box-shadow: 0 18px 60px rgba(60,50,40,.35); max-width: 1380px; max-height: 760px;
  display: flex; align-items: center; justify-content: center;
}
.shotcard img { max-width: 1340px; max-height: 716px; border-radius: 12px; display: block; }
/* intro body (opaque) */
.introbg { position: absolute; inset: 0; background: #fbf9f6; }
.ovwrap { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
.ovcard {
  background: #fff; border-radius: 28px; width: 1620px;
  box-shadow: 0 10px 40px rgba(90,80,60,.12); padding: 54px 64px 40px;
}
.ovcard h2 {
  font-family: 'Smiley','Noto Sans CJK SC',sans-serif; color: #cf4f24;
  font-size: 58px; text-align: center; margin-bottom: 36px;
}
.ovrow {
  display: flex; align-items: baseline; gap: 22px;
  padding: 17px 6px; border-bottom: 1.5px solid #efe9db;
}
.ovrow:last-child { border-bottom: none; }
.ovrow .sec { font-size: 31px; font-weight: 800; color: #b8541f; white-space: nowrap; min-width: 210px; }
.ovrow .lst { font-size: 29px; color: #514c45; line-height: 1.5; }
"""


def _page(body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_css()}</style></head><body>{body}</body></html>"
    )


def _nav_html(issue: dict, cur_idx: int) -> str:
    cells = ['<div class="%s">%s</div>' % ("on" if cur_idx == -1 else "", INTRO_LABEL)]
    for i, s in enumerate(issue["sections"]):
        on = "on" if cur_idx == i else ""
        cells.append(f'<div class="{on}">{html.escape(s["name"])}</div>')
    return '<div class="nav">' + "".join(cells) + "</div>"


def _crumb_html(issue: dict, cur_id: str) -> str:
    cells = [
        '<div class="%s">%s</div>'
        % ("on" if cur_id == INTRO_CRUMB_ID else "", INTRO_LABEL)
    ]
    for it in issue["items"]:
        on = "on" if it["id"] == cur_id else ""
        label = it.get("nav") or it["id"]
        cells.append(f'<div class="{on}">{html.escape(label)}</div>')
    return '<div class="crumb">' + "".join(cells) + "</div>"


def _shot_html(shot_uri: str) -> str:
    return (
        '<div class="dim"></div><div class="shotwrap"><div class="shotcard">'
        f'<img src="{html.escape(shot_uri, quote=True)}"></div></div>'
    )


def _intro_body_html(issue: dict) -> str:
    sec_items = {s["slug"]: [] for s in issue["sections"]}
    for it in issue["items"]:
        if it.get("section") in sec_items:
            sec_items[it["section"]].append(it.get("nav") or it["id"])
    rows = []
    for s in issue["sections"]:
        lst = "、".join(sec_items[s["slug"]])
        icon = html.escape(s["icon"]) + " " if s.get("icon") else ""
        rows.append(
            f'<div class="ovrow"><div class="sec">{icon}{html.escape(s["name"])}</div>'
            f'<div class="lst">{html.escape(lst)}</div></div>'
        )
    date = issue.get("date", "")
    return (
        '<div class="introbg"></div><div class="ovwrap"><div class="ovcard">'
        + f"<h2>{html.escape(date)} 资讯概览</h2>"
        + "".join(rows)
        + "</div></div>"
    )


def _shot_uri(run_dir: Path, item: dict) -> Union[str, None]:
    """item.media[] 里 kind=='shot' 的 src → 可用 URI；找不到/文件缺失 → None。"""
    for m in item.get("media") or []:
        if m.get("kind") != "shot" or not m.get("src"):
            continue
        src = m["src"]
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", src):  # http(s)/data: 原样
            return src
        p = Path(src)
        if not p.is_absolute():
            p = run_dir / src
        if p.exists():
            return p.resolve().as_uri()
    return None


def render(
    run_dir: Union[str, Path],
    issue: Union[dict, str, Path],
    out_dir: Union[str, Path, None] = None,
    wait_ms: int = 250,
) -> dict:
    """渲染整期 chrome 叠加层 → run_dir/chrome/*.png，返回 {name: Path}。

    issue 可以是解析后的 dict，也可以是 50_issue.json 路径。
    out_dir 默认 <run_dir>/chrome。name 集合：
      nav_intro, nav_s<i>（每个 section 一个高亮变体）,
      crumb_intro, crumb_<item.id>,
      shot_<item.id>（仅声明了可用 shot 媒体的条目）,
      intro_body（不透明概览整帧）。
    """
    run_dir = Path(run_dir).resolve()
    if isinstance(issue, (str, Path)):
        issue = json.loads(Path(issue).read_text(encoding="utf-8"))
    out = Path(out_dir) if out_dir is not None else run_dir / "chrome"
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()

    jobs = {}  # name -> (html_doc, transparent)
    jobs["nav_intro"] = (_page(_nav_html(issue, -1)), True)
    for i in range(len(issue["sections"])):
        jobs[f"nav_s{i}"] = (_page(_nav_html(issue, i)), True)
    jobs["crumb_intro"] = (_page(_crumb_html(issue, INTRO_CRUMB_ID)), True)
    for it in issue["items"]:
        jobs[f"crumb_{it['id']}"] = (_page(_crumb_html(issue, it["id"])), True)
        uri = _shot_uri(run_dir, it)
        if uri:
            jobs[f"shot_{it['id']}"] = (_page(_shot_html(uri)), True)
    jobs["intro_body"] = (_page(_intro_body_html(issue)), False)

    rendered = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(
                viewport={"width": W, "height": H}, device_scale_factor=1
            )
            for name, (doc, transparent) in jobs.items():
                f = out / f"{name}.html"
                f.write_text(doc, encoding="utf-8")
                pg.goto(f.as_uri())  # set_content 载不了 file:// 图，必须 goto
                try:
                    pg.evaluate("() => document.fonts.ready.then(() => true)")
                except Exception:
                    pass
                pg.wait_for_timeout(wait_ms)
                png = out / f"{name}.png"
                pg.screenshot(path=str(png), omit_background=transparent)
                rendered[name] = png
        finally:
            browser.close()
    return rendered
