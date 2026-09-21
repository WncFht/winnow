"""stages/lib/composite.py — img.layer 图层栈合成最终帧（PLAN.md §7.6）。

移植自 repro/composite_frames.py：
  frame = 上游卡片卡 + nav 叠加 + crumb 叠加 (+ shot 弹卡)
  所有层作为 <img class="layer"> 以 {position:absolute;inset:0} 全屏叠在
  1920x1080 页面里截图 —— 与视频工具看到的图层语义一致。
  同走 file:// workaround：HTML 先落盘再 pg.goto(path.as_uri())
  （pg.set_content 无法加载 file:// 图片，已踩过）。

API：
    from lib import composite
    composite.stack(run_dir, "deepseek", [card, nav, crumb]) -> Path
    composite.stack_all(run_dir, {"deepseek": [...], "deepseek_shot": [...]})
        -> {name: Path}          # 共享一个 browser，批量走这个
    jobs, missing = composite.item_jobs(issue, cards_dir, chrome_dir)
        # 按 issue/v1 组装每条的图层栈（含 <id>_shot 变体和 intro 帧）

输出默认落 run_dir/64_frames/（§4 frames_manifest 的 dir）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence, Union

from playwright.sync_api import sync_playwright

W, H = 1920, 1080
FRAMES_DIR = "64_frames"

PAGE = """<!doctype html><html><head><meta charset='utf-8'><style>
* { margin: 0; padding: 0; }
html, body { width: 1920px; height: 1080px; overflow: hidden; }
img.layer { position: absolute; inset: 0; width: 1920px; height: 1080px; }
</style></head><body>%s</body></html>"""


def _stack_html(layers: Sequence[Union[str, Path]]) -> str:
    imgs = "".join(
        f'<img class="layer" src="{Path(p).resolve().as_uri()}">' for p in layers
    )
    return PAGE % imgs


def stack(
    run_dir: Union[str, Path],
    name: str,
    layers: Sequence[Union[str, Path]],
    out_dir: Union[str, Path, None] = None,
    wait_ms: int = 200,
) -> Path:
    """合成一帧：layers 从底到顶叠放 → <out_dir>/<name>.png，返回该 Path。

    单帧调用会自带一次 browser 启动；批量请用 stack_all。
    name 一般是 item.id；shot 变体约定 <id>_shot。layers 里不存在的文件
    会渲染成透明空洞（不报错）——调用方先用 item_jobs 拿 missing 清单。
    """
    return stack_all(run_dir, {name: layers}, out_dir=out_dir, wait_ms=wait_ms)[name]


def stack_all(
    run_dir: Union[str, Path],
    jobs: Mapping[str, Sequence[Union[str, Path]]],
    out_dir: Union[str, Path, None] = None,
    wait_ms: int = 200,
) -> dict:
    """批量合成 {name: layers}，共享一个 chromium → {name: png Path}。"""
    run_dir = Path(run_dir).resolve()
    out = Path(out_dir) if out_dir is not None else run_dir / FRAMES_DIR
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()

    rendered = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(
                viewport={"width": W, "height": H}, device_scale_factor=1
            )
            for name, layers in jobs.items():
                f = out / f"{name}.html"
                f.write_text(_stack_html(layers), encoding="utf-8")
                pg.goto(f.as_uri())
                pg.wait_for_timeout(wait_ms)
                png = out / f"{name}.png"
                pg.screenshot(path=str(png))
                rendered[name] = png
        finally:
            browser.close()
    return rendered


def item_jobs(
    issue: Union[dict, str, Path],
    cards_dir: Union[str, Path],
    chrome_dir: Union[str, Path],
) -> tuple:
    """按 issue/v1 组装图层栈（repro main() 的移植）。

    每条 item：[cards/<id>.png, chrome/nav_s<sec_idx>.png, chrome/crumb_<id>.png]；
    chrome/shot_<id>.png 存在时追加 "<id>_shot" 变体 = 基层 + shot 弹卡。
    intro 帧（chrome/intro_body + nav_intro + crumb_intro 三件齐）自动加入。

    返回 (jobs, missing)：jobs = {name: [Path,...]}；missing = 基层文件
    不齐（卡/nav/crumb 缺一）或 section slug 未知的 item.id 列表。
    """
    if isinstance(issue, (str, Path)):
        issue = json.loads(Path(issue).read_text(encoding="utf-8"))
    cards_dir, chrome_dir = Path(cards_dir), Path(chrome_dir)
    sec_idx = {s["slug"]: i for i, s in enumerate(issue["sections"])}

    jobs, missing = {}, []

    intro = [
        chrome_dir / "intro_body.png",
        chrome_dir / "nav_intro.png",
        chrome_dir / "crumb_intro.png",
    ]
    if all(p.exists() for p in intro):
        jobs["intro"] = intro

    for it in issue["items"]:
        si = sec_idx.get(it.get("section"))
        layers = [
            cards_dir / f"{it['id']}.png",
            chrome_dir / f"nav_s{si}.png" if si is not None else None,
            chrome_dir / f"crumb_{it['id']}.png",
        ]
        if si is None or not all(p is not None and p.exists() for p in layers):
            missing.append(it["id"])
            continue
        jobs[it["id"]] = layers
        shot = chrome_dir / f"shot_{it['id']}.png"
        if shot.exists():
            jobs[f"{it['id']}_shot"] = layers + [shot]

    return jobs, missing
