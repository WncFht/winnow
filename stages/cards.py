#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright==1.63.*",
#   "pyyaml>=6",
#   "pydantic>=2",
#   "googlenewsdecoder>=0.7",
# ]
# ///
"""stages/cards.py — 内容卡渲染 + chrome 叠加 + 帧合成（PLAN.md §7.6）。

输入  : 50_issue.json（items[].{id,section,nav,title_short,cards[{label,body,icon}],
        sources[{url,primary}],media[],video.shot_sentences} + sections[] + date）
输出  : 63_cards.json            cards/1（GeneratedContent+id 数组的 envelope）
        63_cards_manifest.json   frames_manifest/1（64_frames/cards/ 原始卡登记）
        64_frames_manifest.json  frames_manifest/1（合成帧 + missing[]）
        64_frames/cards/<id>.png    上游 claudeStyle 逐像素渲染
        64_frames/shots/<id>.png    来源页截图/品牌占位卡（shotlib）
        64_frames/chrome/*.png      nav pill/面包屑/截图弹卡/intro_body 叠加层
        64_frames/<id>.png          合成帧 = 卡 + nav + crumb
        64_frames/<id>.shot.png     + shot 弹卡变体
        64_frames/intro.png         概览帧（item=intro 伪条目）

流程：issue → 63_cards.json → `npx tsx upstream/juya-news-card/scripts/
render-batch.ts` → 逐条 probe 三指标（wrapperScale/minCardTop/clipped，D2
gate，card-density sweep 标定阈值）→ 不过则按确定性降级 spec 重排重渲（≤2
次）→ shotlib 按 shot_sentences 截来源页（命中 shot_policy.yaml 的域名直接
产占位卡；真失败 → missing[] + 占位帧不阻塞）→ chrome.render 叠加层 →
composite.stack_all 合成 → manifest 落盘。

lib.layout_d2 / shotlib / chrome / composite 均惰性 import：模块缺失或调用
签名不符时回退到本文件内移植自 repro/ 与 experiments/ 的同语义实现，
降级产物进 missing[] 而不是炸阶段。

用法: uv run stages/cards.py --run-dir runs/<date>
      uv run stages/cards.py --run-dir runs/<date> --limit 2   # 只渲前 N 条（测试）
      uv run stages/cards.py --shot-test --run-dir runs/<date> [--url URL]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import html as htmlmod
import importlib
import json
import logging
import os
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from contracts.models import Cards, FramesManifest  # noqa: E402
from lib import meta, prog  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
UPSTREAM = REPO / "upstream" / "juya-news-card"
RENDER_BATCH = UPSTREAM / "scripts" / "render-batch.ts"
SSR_RUNTIME = UPSTREAM / "src" / "templates" / "ssr-runtime.ts"
FONT_CANDIDATES = [UPSTREAM / "assets" / "htmlFont.ttf",
                   UPSTREAM / "public" / "assets" / "htmlFont.ttf"]

F_ISSUE = "50_issue.json"
F_CARDS = "63_cards.json"
F_CARDS_MANIFEST = "63_cards_manifest.json"
F_FRAMES_MANIFEST = "64_frames_manifest.json"
F_TIMELINE = "62_timeline.json"          # 可选：存在时回填 shot 帧绝对时间窗
FRAMES_DIR = "64_frames"

W, H = 1920, 1080
TEMPLATE = "claudeStyle"
# 三指标 gate（experiments/card-density probe_sweep 实测标定）：
# 模板 scale 下限 0.60，贴底后 minCardTop 转负/bottom>1080 = clipped。
SCALE_FLOOR = 0.55          # 0.60 留 epsilon
CLIP_MARGIN = 2.0           # px 容差
PROBE_WAIT_MS = 1400        # 对齐 probe.mts（render-batch 1200 + font refit）
RENDER_WAIT_S = 30          # setContent networkidle 上限（CDN 抖动容忍）
MAX_ADJUST = 2              # §7.6: 重排重渲最多 2 次

# 降级 spec：按卡数给 desc 字符预算（sweep：n=4 desc≤200 不裁切，n=8 desc≤120）
DESC_BUDGET = [(3, 260), (4, 200), (6, 150), (8, 110), (99, 80)]
DESC_BUDGET_TIGHT = [(3, 180), (4, 140), (6, 100), (8, 80), (99, 60)]
MAINTITLE_MAX = 40          # sweep：标题到 80 仍缩排不裁切，40 以上压一记保险
SHOT_SOURCES_MAX = 3        # 每条最多试几个来源 URL

log = logging.getLogger("cards")

# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _die(msg: str, hint: str = "") -> "SystemExit":
    sys.stderr.write(f"ERROR: {msg}\n")
    if hint:
        sys.stderr.write(f"HINT: {hint}\n")
    return SystemExit(2)


def _need(run_dir: Path, name: str, hint: str) -> Path:
    p = run_dir / name
    if not p.is_file():
        raise _die(f"缺输入 {name}", hint)
    return p


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _png_size(p: Path) -> tuple:
    """IHDR 直接读宽高（不依赖 PIL）。"""
    try:
        with open(p, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return struct.unpack(">II", head[16:24])
    except Exception:
        pass
    return None, None


def _lib(name: str):
    """惰性 import lib.<name>——并行 agent 可能还在写；任何失败都降级。"""
    try:
        return importlib.import_module(f"lib.{name}")
    except Exception as e:
        log.warning("lib.%s 不可用（%s: %s）→ 内建回退", name,
                    type(e).__name__, str(e)[:120])
        return None


def _proxy() -> str:
    return os.environ.get("PIPELINE_PROXY", "http://127.0.0.1:7890")


def _node_env(run_dir: Path) -> dict:
    """node/tsx 子进程环境：TMPDIR 指真盘（/tmp tmpfs 常满，PLAN §7.8 踩坑），
    CDN 外链（fonts.googleapis/tailwind JIT 自托管补丁未上前）走代理。"""
    env = dict(os.environ)
    tmp = run_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.setdefault(k, _proxy())
    env.setdefault("no_proxy", "localhost,127.0.0.1")
    env.setdefault("NO_PROXY", "localhost,127.0.0.1")
    return env


def _host(url: str) -> str:
    """进度行用的短域名标签。"""
    try:
        return urlparse(str(url)).netloc or str(url)[:48]
    except Exception:
        return str(url)[:48]


def _prog(run_dir: Path, total: int = 0,
          step: Optional[int] = None) -> "prog.Prog":
    """统一节流参数：step≈max(1,total//40) 夹到 [10,100]、interval=30s；
    分钟级顺序循环传 step=1 逐条出。"""
    s = step if step else min(100, max(10, max(1, int(total or 0) // 40)))
    return prog.Prog(run_dir, "cards", total=total, step=s, interval=30.0)


def _run_streamed(cmd: list, *, cwd: Path, env: dict, timeout: float,
                  tag: str) -> tuple:
    """Popen 版 subprocess.run：子进程 stdout/stderr 合并逐行透传到本进程
    stderr（`[cards] <tag>` 前缀），超时 kill。capture_output 会把 tsx
    输出憋满整个 timeout（render 900s / probe 600s），长跑期间零反馈。
    返回 (rc, tail, timed_out)；Popen 启动 OSError 直接抛给调用方。"""
    tail: "collections.deque" = collections.deque(maxlen=60)
    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)

    def _pump() -> None:
        try:
            for ln in proc.stdout or ():
                ln = ln.strip()
                if not ln:
                    continue
                tail.append(ln)
                sys.stderr.write(f"[cards] {tag}{ln[:160]}\n")
            sys.stderr.flush()
        except Exception:
            pass                    # 透传失败绝不拖垮阶段

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
        timed_out = True
    t.join(timeout=5)
    return rc, "\n".join(tail), timed_out


# ---------------------------------------------------------------------------
# step 1: issue → 63_cards.json（cards/1）
# ---------------------------------------------------------------------------

_MD_STRONG = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_CODE = re.compile(r"`([^`]+?)`", re.S)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def md_to_html(text: str) -> str:
    """issue 内联方言（**粗** `码` [t](u)）→ GeneratedContent desc 方言
    （<strong>/<code>）。先 escape 再回填标签，保证产出是合法子集。"""
    t = htmlmod.escape(str(text or ""), quote=False)
    t = _MD_LINK.sub(lambda m: m.group(1), t)          # 链接只留文字
    t = _MD_STRONG.sub(lambda m: f"<strong>{m.group(1)}</strong>", t)
    t = _MD_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", t)
    return t


def html_to_text(text: str) -> str:
    """desc HTML → 纯文本（降级 spec 截断时剥标签用）。"""
    return re.sub(r"<[^>]+>", "", str(text or ""))


def _desc_budget(n: int, table) -> int:
    for cap, budget in table:
        if n <= cap:
            return budget
    return table[-1][1]


def item_to_card(it: dict) -> dict:
    """issue item → {id, mainTitle, cards:[{title,desc,icon}]}（GeneratedContent）。

    cards 缺省（Call B 未跑/漏条）时用 tldr 合成单卡兜底，保证 coverage：
    63.items.id == 50.items.id（validate_run coverage 硬规则）。
    """
    cards = []
    raw = it.get("cards")
    if isinstance(raw, dict):            # 防御：上游若直接塞 GeneratedContent
        raw = raw.get("cards") or []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        title = " ".join(str(c.get("label") or c.get("title") or "").split())
        body = " ".join(str(c.get("body") or c.get("desc") or "").split())
        icon = str(c.get("icon") or "").strip() or "article"
        if title or body:
            cards.append({"title": title, "desc": md_to_html(body),
                          "icon": icon})
    if not cards:
        cards = [{"title": "要点",
                  "desc": md_to_html(it.get("tldr") or it.get("headline") or it["id"]),
                  "icon": "article"}]
    mt = str(it.get("title_short") or it.get("nav") or it.get("headline")
             or it["id"]).strip()
    return {"id": it["id"], "mainTitle": mt, "cards": cards[:8]}


def _renderer_id() -> str:
    try:
        sha = hashlib.sha256(SSR_RUNTIME.read_bytes()).hexdigest()[:12]
        return f"juya-news-card@ssr-{sha}"
    except OSError:
        return "juya-news-card@unknown"


def write_cards_json(run_dir: Path, issue: dict, items: list) -> Path:
    doc = {"schema": "cards/1",
           "episode": str(issue.get("date") or run_dir.name),
           "renderer": _renderer_id(),
           "template": TEMPLATE,
           "items": items}
    Cards.model_validate(doc)            # 契约自检，违例直接炸（自己的 bug）
    return meta.atomic_write(run_dir / F_CARDS, doc)


# ---------------------------------------------------------------------------
# step 2: render-batch.ts 渲染
# ---------------------------------------------------------------------------


def _render_input(items: list) -> list:
    """63_cards.items[] → render-batch 输入（带 per-item template 字段）。"""
    return [{"id": it["id"], "template": TEMPLATE,
             "mainTitle": it["mainTitle"], "cards": it["cards"]}
            for it in items]


def render_batch(items: list, cards_dir: Path, run_dir: Path,
                 tag: str = "") -> set:
    """调上游 render-batch.ts → cards_dir/<id>.png；返回渲出 png 的 id 集。"""
    if not RENDER_BATCH.is_file():
        log.error("缺 %s", RENDER_BATCH)
        return set()
    cards_dir.mkdir(parents=True, exist_ok=True)
    inp = run_dir / FRAMES_DIR / f"_render_items{tag}.json"
    meta.atomic_write(inp, _render_input(items))
    timeout = min(900, 60 + 25 * max(1, len(items)))
    cmd = ["npx", "tsx", "scripts/render-batch.ts", str(inp), str(cards_dir)]
    log.info("render-batch: %d items → %s", len(items), cards_dir)
    try:
        rc, tail, timed_out = _run_streamed(
            cmd, cwd=UPSTREAM, env=_node_env(run_dir), timeout=timeout,
            tag="tsx| ")
        if timed_out:
            log.error("render-batch 超时 %ds", timeout)
        elif rc != 0:
            log.error("render-batch rc=%d: %s", rc, tail[-400:])
    except OSError as e:
        log.error("render-batch 启动失败: %s", e)
    return {p.stem for p in cards_dir.glob("*.png")}


# ---------------------------------------------------------------------------
# step 3: probe 三指标（layout_d2 优先，内建 tsx 探针兜底）
# ---------------------------------------------------------------------------

# 内建探针：等价 experiments/card-density/probe.mts 的测量面（只测不截图）。
# 脚本落在 run_dir/tmp/_probe.mts，cwd=upstream 让 createRequire 命中其
# node_modules/playwright；ssr-runtime 用绝对路径 import。
_PROBE_MTS = r"""
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';
import { createRequire } from 'module';

const UPSTREAM = process.env.CARDS_UPSTREAM || process.cwd();
const req = createRequire(path.join(UPSTREAM, 'package.json'));
const { chromium } = req('playwright');
const { generateTemplateHtml } = await import(
  path.join(UPSTREAM, 'src/templates/ssr-runtime.ts'));

const [itemsPath, outJson] = process.argv.slice(2);
const items = JSON.parse(fs.readFileSync(itemsPath, 'utf-8'));
const domDir = path.join(path.dirname(outJson), 'probe_dom');
fs.mkdirSync(domDir, { recursive: true });

// CDN 自托管补丁后 SSR HTML 里是相对 vendor/… 引用：setContent(about:blank)
// 禁止加载 file:// 子资源 —— 注入 <base href=…/public/> 后 goto(file://)，
// 与 render-batch.ts 的 writeHtmlForFileRender 同路径。
const publicHref = pathToFileURL(
  path.join(UPSTREAM, 'public') + path.sep).href;
function withBase(html) {
  const base = `<base href="${publicHref}" />`;
  return html.includes('<head>')
    ? html.replace('<head>', `<head>\n  ${base}`)
    : `${base}\n${html}`;
}

const fontPath = [path.join(UPSTREAM, 'assets', 'htmlFont.ttf'),
                  path.join(UPSTREAM, 'public', 'assets', 'htmlFont.ttf')]
                 .find(p => fs.existsSync(p));
let fontFace = '';
if (fontPath) {
  const b64 = fs.readFileSync(fontPath).toString('base64');
  fontFace = `<style>@font-face{font-family:'CustomPreviewFont';` +
    `src:url(data:font/ttf;base64,${b64}) format('truetype');}` +
    `.main-container{font-family:'CustomPreviewFont',system-ui,` +
    `-apple-system,sans-serif !important;}</style>`;
}

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const results = [];
for (const item of items) {
  const { id, template, ...content } = item;
  const html = generateTemplateHtml(content, template || 'claudeStyle');
  const finalHtml = withBase(
    fontFace ? html.replace('</head>', `${fontFace}</head>`) : html);
  const htmlPath = path.join(domDir, `${id}.html`);
  fs.writeFileSync(htmlPath, finalHtml);
  const page = await context.newPage();
  const rec = { id };
  try {
    await page.goto(pathToFileURL(htmlPath).href,
                    { waitUntil: 'networkidle', timeout: 30000 });
    try { await page.evaluate('document.fonts.ready'); } catch (e) {}
  } catch (e) { rec.renderErr = String(e).slice(0, 120); }
  await page.waitForTimeout(1400);
  try {
    Object.assign(rec, await page.evaluate(() => {
      const wrapper = document.querySelector('.content-wrapper');
      const tr = wrapper ? getComputedStyle(wrapper).transform : '';
      let scale = 1;
      if (tr && tr !== 'none') {
        const a = tr.match(/matrix\(([^,]+)/); if (a) scale = parseFloat(a[1]);
      }
      const cardEls = Array.from(document.querySelectorAll('.card-item'));
      const rects = cardEls.map(el => {
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
      });
      const ct = Array.from(document.querySelectorAll('.card-title'));
      return {
        docScrollH: document.documentElement.scrollHeight,
        wrapperScale: scale,
        minCardTop: rects.length ? Math.min(...rects.map(r => r.top)) : null,
        maxCardBottom: rects.length ? Math.max(...rects.map(r => r.bottom)) : null,
        nCards: rects.length,
        truncatedTitles: ct.filter(el => el.scrollWidth > el.clientWidth + 1).length,
      };
    }));
  } catch (e) { rec.probeErr = String(e).slice(0, 120); }
  results.push(rec);
  await page.close();
  console.log(`[probe] ${id} scale=${rec.wrapperScale ?? '-'} ` +
    `top=${rec.minCardTop ?? '-'}${rec.renderErr || rec.probeErr ? ' ERR' : ''}`);
}
fs.writeFileSync(outJson, JSON.stringify(results, null, 1));
await context.close();
await browser.close();
"""


def _probe_tsx(items: list, run_dir: Path) -> dict:
    """内建探针：npx tsx 跑 _PROBE_MTS → {id: metrics}。失败返回 {}。"""
    tmp = run_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    script = tmp / "_probe.mts"
    script.write_text(_PROBE_MTS, encoding="utf-8")
    inp = tmp / "_probe_items.json"
    inp.write_text(json.dumps(_render_input(items), ensure_ascii=False),
                   encoding="utf-8")
    out = tmp / "_probe_out.json"
    out.unlink(missing_ok=True)
    env = _node_env(run_dir)
    env["CARDS_UPSTREAM"] = str(UPSTREAM)
    timeout = min(600, 60 + 20 * max(1, len(items)))
    try:
        rc, tail, timed_out = _run_streamed(
            ["npx", "tsx", str(script), str(inp), str(out)],
            cwd=UPSTREAM, env=env, timeout=timeout, tag="probe| ")
        if timed_out:
            log.warning("probe 超时 %ds", timeout)
        elif rc != 0:
            log.warning("probe rc=%d: %s", rc, tail[-300:])
    except Exception as e:
        log.warning("probe 执行失败: %s", e)
        return {}
    try:
        return {m["id"]: m for m in json.loads(out.read_text())}
    except Exception as e:
        log.warning("probe 结果解析失败: %s", e)
        return {}


def probe_items(items: list, run_dir: Path) -> dict:
    """逐条三指标 {id:{wrapperScale,minCardTop,clipped,ok,...}}。

    优先 lib.layout_d2.probe（§7.6 正主，并行 agent 可能在写，签名按
    常见形态逐个试）；不可用则内建 tsx 探针全量测量后归一化。
    """
    mod = _lib("layout_d2")
    fn = getattr(mod, "probe", None) if mod is not None else None
    if callable(fn):
        out = {}
        for it in items:
            for args in ((it, str(run_dir / "tmp")), (it,),):
                try:
                    m = fn(*args)
                    if isinstance(m, dict):
                        out[it["id"]] = _normalize_metrics(it, m)
                        break
                except TypeError:
                    continue
                except Exception as e:
                    log.warning("layout_d2.probe(%s) 失败: %s → 内建探针",
                                it["id"], str(e)[:100])
                    break
            else:
                continue
        if len(out) == len(items):
            return out
        log.warning("layout_d2.probe 覆盖 %d/%d → 内建探针全量补测",
                    len(out), len(items))

    raw = _probe_tsx(items, run_dir)
    return {it["id"]: _normalize_metrics(it, raw.get(it["id"]) or {})
            for it in items}


def _normalize_metrics(item: dict, m: dict) -> dict:
    """原始测量 → {wrapperScale,minCardTop,clipped,ok} 三指标 gate。"""
    scale = m.get("wrapperScale")
    top = m.get("minCardTop")
    bottom = m.get("maxCardBottom")
    scroll_h = m.get("docScrollH")
    clipped = m.get("clipped")
    if clipped is None:
        clipped = bool(
            (top is not None and top < -CLIP_MARGIN)
            or (bottom is not None and bottom > H + CLIP_MARGIN)
            or (scroll_h is not None and scroll_h > H + CLIP_MARGIN))
    scale_bad = scale is not None and scale < SCALE_FLOOR
    top_bad = top is not None and top < -CLIP_MARGIN
    ok = bool(m) and not clipped and not scale_bad and not top_bad \
        and not m.get("probeErr")
    return {"wrapperScale": scale, "minCardTop": top,
            "maxCardBottom": bottom, "docScrollH": scroll_h,
            "truncatedTitles": m.get("truncatedTitles"),
            "clipped": bool(clipped), "ok": ok,
            "err": m.get("probeErr") or m.get("renderErr")}


def adjust_spec(item: dict, metrics: dict, attempt: int) -> dict:
    """确定性降级 spec（§7.6 重排重渲 ≤2 次）：
      attempt 1 — desc 按卡数预算剥标签截断 + 超长 mainTitle 截断；
      attempt 2 — 减卡（≤6）+ 更紧预算。
    返回调整后的 item（新 dict），None 表示无调整空间。"""
    mod = _lib("layout_d2")
    for name in ("adjust", "adjust_item", "fit"):
        fn = getattr(mod, name, None) if mod is not None else None
        if callable(fn):
            try:
                r = fn(item, metrics, attempt)
                if isinstance(r, dict) and r.get("cards"):
                    return r
            except TypeError:
                try:
                    r = fn(item, metrics)
                    if isinstance(r, dict) and r.get("cards"):
                        return r
                except Exception:
                    pass
            except Exception as e:
                log.warning("layout_d2.%s 失败: %s", name, str(e)[:100])

    it = json.loads(json.dumps(item))    # deepcopy
    n = len(it["cards"])
    table = DESC_BUDGET if attempt <= 1 else DESC_BUDGET_TIGHT
    budget = _desc_budget(n, table)
    if attempt >= 2 and n > 6:
        it["cards"] = it["cards"][:6]
        n = 6
        budget = _desc_budget(n, table)
    changed = attempt >= 2 and len(it["cards"]) < len(item["cards"])
    for c in it["cards"]:
        plain = html_to_text(c["desc"])
        if len(plain) > budget:
            c["desc"] = htmlmod.escape(plain[:budget - 1], quote=False) + "…"
            changed = True
    if len(it["mainTitle"]) > MAINTITLE_MAX:
        it["mainTitle"] = it["mainTitle"][:MAINTITLE_MAX - 1] + "…"
        changed = True
    if not changed and n >= len(item["cards"]):
        return None
    return it


def render_with_gate(items: list, cards_dir: Path, run_dir: Path,
                     missing: list, flags: list) -> dict:
    """渲染 + probe gate + ≤2 次降级重渲。返回 {id: card png Path}。"""
    rendered = render_batch(items, cards_dir, run_dir)
    metrics = probe_items(items, run_dir) if rendered else {}
    good = {}
    pending = []
    for it in items:
        png = cards_dir / f"{it['id']}.png"
        if it["id"] not in rendered or not png.is_file():
            flags.append({"item": it["id"], "kind": "render_no_output",
                          "metrics": None})
            missing.append(f"{it['id']}.card")
            continue
        m = metrics.get(it["id"]) or {}
        if m.get("ok", True):            # probe 不可用 → 宽松放行（warn）
            if not m:
                log.warning("%s: probe 无数据，放行", it["id"])
            good[it["id"]] = png
        else:
            pending.append(it)
            log.warning("%s: probe 不过 scale=%s top=%s clipped=%s",
                        it["id"], m.get("wrapperScale"),
                        m.get("minCardTop"), m.get("clipped"))

    pending_p = _prog(run_dir, total=len(pending), step=1)  # 单次重渲分钟级
    for i, it in enumerate(pending, 1):
        cur, last_m = it, metrics.get(it["id"]) or {}
        ok = False
        for attempt in range(1, MAX_ADJUST + 1):
            adj = adjust_spec(cur, last_m, attempt)
            if adj is None:
                break
            pending_p.say(f"{it['id']} 降级 spec 第{attempt}次重渲"
                          f"（cards {len(cur['cards'])}→{len(adj['cards'])}）")
            log.info("%s: 降级 spec 第%d次重渲（cards %d→%d）",
                     it["id"], attempt, len(cur["cards"]), len(adj["cards"]))
            render_batch([adj], cards_dir, run_dir, tag=f"_retry_{it['id']}")
            last_m = (probe_items([adj], run_dir) or {}).get(it["id"]) or {}
            if last_m.get("ok"):
                pending_p.say(f"{it['id']} 第{attempt}次重渲过 gate")
                # 调整后的 spec 写回 63_cards items（manifest 记录重排事实）
                cur = adj
                ok = True
                break
        pending_p.tick(i, f"{it['id']} {'ok' if ok else '仍不过'}",
                       force=True)
        if ok:
            good[it["id"]] = cards_dir / f"{it['id']}.png"
            idx = next(j for j, x in enumerate(items) if x["id"] == it["id"])
            items[idx] = cur             # 写回调整后的 spec → 63_cards.json
            flags.append({"item": it["id"], "kind": "layout_adjusted",
                          "metrics": last_m})
        else:
            (cards_dir / f"{it['id']}.png").unlink(missing_ok=True)
            missing.append(f"{it['id']}.card")
            flags.append({"item": it["id"], "kind": "layout_failed",
                          "metrics": last_m})
    pending_p.close()
    return good


# ---------------------------------------------------------------------------
# step 4: shots（shotlib，按 video.shot_sentences 声明）
# ---------------------------------------------------------------------------


def _reddit_alt(u: str) -> Optional[str]:
    """www.reddit.com → old.reddit.com 镜像：新 UI 对 headless 恒 403。"""
    try:
        pr = urlparse(u)
    except Exception:
        return None
    if (pr.hostname or "").lower() in ("www.reddit.com", "reddit.com"):
        return urlunparse(pr._replace(netloc="old.reddit.com"))
    return None


def _shot_candidates(it: dict) -> list:
    """截图目标 URL：primary 优先，其余按序，去重，封顶。
    reddit 源以 old.reddit.com 镜像打头（www 对 headless 恒 403，先试纯浪费）。"""
    srcs = sorted(it.get("sources") or [],
                  key=lambda s: 0 if s.get("primary") else 1)
    urls, seen = [], set()
    for s in srcs:
        u = s.get("url")
        if u:
            alt = _reddit_alt(u)
            if alt and alt not in seen:
                seen.add(alt)
                urls.append(alt)
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls[:SHOT_SOURCES_MAX]


def _existing_shot(run_dir: Path, it: dict) -> Optional[Path]:
    """item.media[] 已有本地 shot 素材（采集层产出）→ 直接复用。"""
    for m in it.get("media") or []:
        if m.get("kind") != "shot" or not m.get("src"):
            continue
        src = m["src"]
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", src):
            continue                          # 远程不下载，交 shotlib
        p = Path(src)
        if not p.is_absolute():
            p = run_dir / src
        if p.is_file():
            return p
    return None


def fetch_shots(run_dir: Path, issue_items: list, good_cards: dict,
                missing: list, flags: list) -> dict:
    """每条声明了 video.shot_sentences 的 item → shots_dir/<id>.png。

    返回 {id: {"path": Path, "kind": "shot"|"placeholder", "url": str}}。
    任何一条失败只记 missing[]/flags，不阻塞阶段（§7.6 降级语义）。
    """
    shots_dir = run_dir / FRAMES_DIR / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    wants = [it for it in issue_items
             if (it.get("video") or {}).get("shot_sentences")]
    skipped = [it["id"] for it in wants if it["id"] not in good_cards]
    for iid in skipped:                      # 卡都没了，shot 帧无从叠加
        if f"{iid}.shot" not in missing:
            missing.append(f"{iid}.shot")
    wants = [it for it in wants if it["id"] in good_cards]
    if not wants:
        return {}

    shotlib = _lib("shotlib")
    out = {}
    session = None
    if shotlib is not None:
        try:
            session = shotlib.ShotSession()
            session.__enter__()
        except Exception as e:
            log.warning("ShotSession 启动失败: %s", str(e)[:140])
            session = None

    progress = _prog(run_dir, total=len(wants))
    progress.say(f"shots 开始 {len(wants)} 条"
                 f"（session={'on' if session is not None else 'off'}）")
    for i, it in enumerate(wants, 1):
        iid = it["id"]
        progress.tick(i, iid)
        dst = shots_dir / f"{iid}.png"
        reuse = _existing_shot(run_dir, it)
        if reuse is not None:
            out[iid] = {"path": reuse, "kind": "shot", "url": None,
                        "via": "media_reuse"}
            continue
        rec = None
        if session is not None:
            for url in _shot_candidates(it):
                progress.tick(i, f"{iid} ← {_host(url)}")
                try:
                    r = session.shot(url, dst)
                except Exception as e:
                    r = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
                log.info("shot %s ← %s : ok=%s kind=%s %s", iid, url,
                         r.get("ok"), r.get("kind"), r.get("reason") or "")
                if r.get("ok"):
                    rec = r
                    if r.get("kind") == "shot":
                        break          # 真截图优先于占位卡
                    # placeholder：留着兜底，但继续试后面的源
            if rec is None:
                rec = {"ok": False, "reason": "no_source_url"}
        if rec is None or not rec.get("ok") or not rec.get("path"):
            missing.append(f"{iid}.shot")
            flags.append({"item": iid, "kind": "shot_failed",
                          "reason": (rec or {}).get("reason") or
                                    "shotlib_unavailable"})
            continue
        out[iid] = {"path": Path(rec["path"]),
                    "kind": rec.get("kind") or "shot",
                    "url": rec.get("url")}
        if out[iid]["kind"] == "placeholder":
            missing.append(f"{iid}.shot")   # 降级占位：missing 记录 + 帧照出
            flags.append({"item": iid, "kind": "shot_placeholder",
                          "reason": rec.get("reason") or
                                    rec.get("rule") or "policy"})
    progress.tick(len(wants), f"done {len(out)}/{len(wants)}", force=True)
    progress.close()

    if session is not None:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# step 5+6: chrome 叠加 + 合成
# ---------------------------------------------------------------------------


def _issue_for_render(issue: dict, shots: dict) -> dict:
    """给 chrome.render 用的 issue 副本：注入/屏蔽 media.shot。

    chrome 弹卡只取 media[] kind=='shot' 的首个可用 src——把本轮 shotlib
    产物放最前；没声明 shot_sentences 的条目剥掉旧 shot 媒体（不产弹卡）。
    """
    doc = json.loads(json.dumps(issue))
    want_shot = {iid for iid in shots}
    for it in doc.get("items") or []:
        media = [m for m in (it.get("media") or [])
                 if not (m.get("kind") == "shot")]
        if it["id"] in want_shot:
            # chrome 按 run_dir 解析相对路径——存 run_dir 相对路径
            media.insert(0, {"kind": "shot", "src": shots[it["id"]]["src"]})
        it["media"] = media
    return doc


def render_chrome(run_dir: Path, issue: dict) -> dict:
    """lib.chrome.render → {name: Path}；模块缺席 → {}（合成降级为裸卡）。"""
    mod = _lib("chrome")
    fn = getattr(mod, "render", None) if mod is not None else None
    if not callable(fn):
        return {}
    try:
        return fn(run_dir, issue,
                  out_dir=run_dir / FRAMES_DIR / "chrome")
    except TypeError:
        try:
            return fn(run_dir, issue)
        except Exception as e:
            log.warning("chrome.render 失败: %s", str(e)[:160])
            return {}
    except Exception as e:
        log.warning("chrome.render 失败: %s", str(e)[:160])
        return {}


_STACK_PAGE = """<!doctype html><html><head><meta charset='utf-8'><style>
* { margin: 0; padding: 0; }
html, body { width: 1920px; height: 1080px; overflow: hidden; }
img.layer { position: absolute; inset: 0; width: 1920px; height: 1080px; }
</style></head><body>%s</body></html>"""


def _fallback_stack_all(jobs: dict, out_dir: Path, run_dir: Path) -> dict:
    """composite 缺席时的 img.layer 栈（repro/composite_frames.py 直译）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    rendered = {}
    progress = _prog(run_dir, total=len(jobs))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": W, "height": H},
                              device_scale_factor=1)
        for i, (name, layers) in enumerate(jobs.items(), 1):
            imgs = "".join(
                f'<img class="layer" src="{Path(x).resolve().as_uri()}">'
                for x in layers)
            f = out_dir / f"{name}.html"
            f.write_text(_STACK_PAGE % imgs, encoding="utf-8")
            pg.goto(f.as_uri())
            pg.wait_for_timeout(200)
            png = out_dir / f"{name}.png"
            pg.screenshot(path=str(png))
            rendered[name] = png
            progress.tick(i, name)
        browser.close()
    progress.tick(len(jobs), "stack done", force=True)
    progress.close()
    return rendered


def _build_jobs(issue: dict, cards_dir: Path, chrome_dir: Path) -> tuple:
    """composite.item_jobs 语义的内建版（lib 缺席/失败时兜底）。"""
    sec_idx = {s["slug"]: i for i, s in enumerate(issue.get("sections") or [])}
    jobs, miss = {}, []
    intro = [chrome_dir / "intro_body.png", chrome_dir / "nav_intro.png",
             chrome_dir / "crumb_intro.png"]
    if all(p.exists() for p in intro):
        jobs["intro"] = intro
    for it in issue.get("items") or []:
        si = sec_idx.get(it.get("section"))
        layers = [cards_dir / f"{it['id']}.png",
                  chrome_dir / f"nav_s{si}.png" if si is not None else None,
                  chrome_dir / f"crumb_{it['id']}.png"]
        if si is None or not all(p is not None and p.exists() for p in layers):
            miss.append(it["id"])
            continue
        jobs[it["id"]] = layers
        shot = chrome_dir / f"shot_{it['id']}.png"
        if shot.exists():
            jobs[f"{it['id']}.shot"] = layers + [shot]
    return jobs, miss


def composite_frames(run_dir: Path, issue: dict, good_cards: dict,
                     chrome_dir: Path, missing: list, flags: list) -> dict:
    """合成 64_frames/*.png。返回 {name: Path}（name=<id>|intro|<id>.shot）。"""
    cards_dir = run_dir / FRAMES_DIR / "cards"
    frames_dir = run_dir / FRAMES_DIR
    frames_dir.mkdir(parents=True, exist_ok=True)

    jobs, jmiss = None, []
    mod = _lib("composite")
    item_jobs = getattr(mod, "item_jobs", None) if mod is not None else None
    if callable(item_jobs):
        try:
            jobs, jmiss = item_jobs(issue, cards_dir, chrome_dir)
            # 命名约定对齐 §4 fixture：<id>.shot.png（lib 产出 <id>_shot）
            jobs = {(k[:-5] + ".shot") if k.endswith("_shot") else k: v
                    for k, v in (jobs or {}).items()}
        except Exception as e:
            log.warning("composite.item_jobs 失败: %s → 内建", str(e)[:140])
            jobs = None
    if jobs is None:
        jobs, jmiss = _build_jobs(issue, cards_dir, chrome_dir)
    for iid in jmiss:
        card_png = cards_dir / f"{iid}.png"
        if card_png.is_file():
            # 卡渲出来了只是 chrome 缺层 → 裸卡帧兜底（比空帧强），记 chrome 缺失
            jobs[iid] = [card_png]
            if f"{iid}.chrome" not in missing:
                missing.append(f"{iid}.chrome")
            flags.append({"item": iid, "kind": "chrome_missing",
                          "reason": "nav/crumb 缺层，帧=裸卡"})
        else:
            if f"{iid}.card" not in missing:
                missing.append(f"{iid}.card")
            flags.append({"item": iid, "kind": "layers_incomplete",
                          "reason": "card/nav/crumb 缺层"})

    progress = _prog(run_dir, total=len(jobs))
    try:
        stack_all = getattr(mod, "stack_all", None) if mod is not None else None
        if callable(stack_all):
            try:
                progress.say(f"composite.stack_all {len(jobs)} 帧（lib）")
                return stack_all(run_dir, jobs, out_dir=frames_dir)
            except TypeError:
                try:
                    return stack_all(run_dir, jobs)
                except Exception as e:
                    log.warning("composite.stack_all 失败: %s → 内建",
                                str(e)[:140])
            except Exception as e:
                log.warning("composite.stack_all 失败: %s → 内建",
                            str(e)[:140])
        progress.say(f"内建 stack_all {len(jobs)} 帧")
        try:
            return _fallback_stack_all(jobs, frames_dir, run_dir)
        except Exception as e:
            log.error("帧合成彻底失败: %s", str(e)[:200])
            for name in jobs:
                iid = name.split(".")[0]
                kind = "shot" if name.endswith(".shot") else "card"
                tag = f"{iid}.{kind}"
                if tag not in missing:
                    missing.append(tag)
            return {}
    finally:
        progress.close()


# ---------------------------------------------------------------------------
# step 7: manifests
# ---------------------------------------------------------------------------


def _frame_entry(run_dir: Path, png: Path, item: str, kind: str,
                 t=None) -> dict:
    w, h = _png_size(png)
    e = {"item": item, "kind": kind,
         "path": str(png.relative_to(run_dir)),
         "w": w, "h": h, "sha256": meta.sha256_file(png)}
    e["t"] = list(t) if t else None
    return e


def _shot_windows(run_dir: Path) -> dict:
    """62_timeline.json（若已产）→ {(item): [start,end]} shot 窗口。"""
    p = run_dir / F_TIMELINE
    if not p.is_file():
        return {}
    try:
        tl = _load_json(p)
        return {o["item"]: [o["start"], o["end"]]
                for o in tl.get("overlays") or [] if o.get("kind") == "shot"}
    except Exception:
        return {}


def write_manifests(run_dir: Path, issue: dict, cards_dir: Path,
                    good_cards: dict, frames: dict, missing: list) -> Path:
    episode = str(issue.get("date") or run_dir.name)
    windows = _shot_windows(run_dir)

    # 63_cards_manifest：原始渲染卡登记（无卡条的也记 missing）
    cm_files, cm_missing = [], []
    for it in issue.get("items") or []:
        iid = it["id"]
        png = cards_dir / f"{iid}.png"
        if iid in good_cards and png.is_file():
            cm_files.append(_frame_entry(run_dir, png, iid, "card"))
        else:
            cm_missing.append(f"{iid}.card")
    cm = {"schema": "frames_manifest/1", "episode": episode,
          "dir": f"{FRAMES_DIR}/cards",
          "files": sorted(cm_files, key=lambda x: (x["item"], x["kind"])),
          "missing": sorted(set(cm_missing))}
    FramesManifest.model_validate(cm)
    meta.atomic_write(run_dir / F_CARDS_MANIFEST, cm)

    files = []
    for name, png in sorted(frames.items()):
        if not png or not Path(png).is_file():
            continue
        if name == "intro":
            files.append(_frame_entry(run_dir, Path(png), "intro", "card"))
        elif name.endswith(".shot"):
            iid = name[:-5]
            files.append(_frame_entry(run_dir, Path(png), iid, "shot",
                                      t=windows.get(iid)))
        else:
            files.append(_frame_entry(run_dir, Path(png), name, "card"))
    fm = {"schema": "frames_manifest/1", "episode": episode,
          "dir": FRAMES_DIR,
          "files": sorted(files, key=lambda x: (x["item"], x["kind"])),
          "missing": sorted(set(missing))}
    FramesManifest.model_validate(fm)
    return meta.atomic_write(run_dir / F_FRAMES_MANIFEST, fm)


# ---------------------------------------------------------------------------
# shot-test / run / main
# ---------------------------------------------------------------------------


def shot_test(run_dir: Path, url: Optional[str]) -> int:
    """just shot-test：截图管道自检（策略命中 + 真实截图各一）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    out_dir = run_dir / FRAMES_DIR / "shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not url:
        issue_p = run_dir / F_ISSUE
        if issue_p.is_file():
            try:
                for it in _load_json(issue_p).get("items") or []:
                    for s in it.get("sources") or []:
                        if s.get("primary") and s.get("url"):
                            url = s["url"]
                            break
                    if url:
                        break
            except Exception:
                pass
        url = url or "https://example.com/"
    shotlib = _lib("shotlib")
    if shotlib is None:
        print("SHOT-TEST FAIL: lib.shotlib 不可用")
        return 1
    ok_all = True
    for label, u in (("policy(x.com)", "https://x.com/"),
                     ("live", url)):
        dst = out_dir / f"_shot_test_{label.split('(')[0]}.png"
        try:
            rec = shotlib.shot(u, dst)
        except Exception as e:
            rec = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        ok = bool(rec.get("ok")) and dst.is_file() and dst.stat().st_size > 5000
        ok_all &= ok
        print(f"  {label:14s} {u[:60]:60s} ok={ok} kind={rec.get('kind')} "
              f"{dst.stat().st_size // 1024 if dst.exists() else 0}KB "
              f"{rec.get('reason') or rec.get('rule') or ''}")
    print("SHOT-TEST OK" if ok_all else "SHOT-TEST FAIL")
    return 0 if ok_all else 1


def _fix_meta_contract(run_dir: Path, episode: str) -> None:
    """把 00_meta.json 顶层对齐 RunMeta 契约（episode 必填、extra=forbid）。

    lib.meta._load_meta 骨架写的是 run_date=<dir名>——契约无此字段且缺
    episode。stage_done 之后就地归一化：episode 取 issue.date（须匹配
    YYYY-MM-DD），删掉 run_date；episode 值不合法时保留原样（不编造）。
    """
    p = run_dir / "00_meta.json"
    if not p.is_file() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", episode):
        return
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(m, dict):
            return
        m["episode"] = episode
        m.pop("run_date", None)
        meta.atomic_write(p, m)
    except Exception as e:
        log.warning("00_meta 契约归一化失败（非阻塞）: %s", e)


def run(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    # 输入存在性校验先于运行态登记：_need 报错退出不该留"崩溃"假墓碑；
    # 登记放锁内，RMW 串行化 + running==持锁语义。
    issue_p = _need(run_dir, F_ISSUE,
                    "先跑 `just digest`（Call A）+ `just digest --callb`")
    os.environ.setdefault("TMPDIR", str(run_dir / "tmp"))  # playwright/Chrome

    with meta.run_lock(run_dir), prog.Prog(run_dir, "cards") as progress:
        meta.stage_begin(run_dir, "cards")   # 00_running.json 运行态登记
        issue = _load_json(issue_p)
        items = [item_to_card(it) for it in issue.get("items") or []]
        if args.limit:
            keep = {c["id"] for c in items[: args.limit]}
            issue = dict(issue)
            issue["items"] = [it for it in issue["items"] if it["id"] in keep]
            items = items[: args.limit]
        log.info("issue %s: %d items, %d sections",
                 issue.get("date"), len(items), len(issue.get("sections") or []))
        progress.say(f"issue {issue.get('date')}: {len(items)} items")

        # 2-3) render + probe gate + 降级重渲（items 会被写回最终 spec）
        missing, flags = [], []
        cards_dir = run_dir / FRAMES_DIR / "cards"
        t0 = time.time()
        progress.say(f"render+probe 开始（{len(items)} items）")
        good_cards = render_with_gate(items, cards_dir, run_dir,
                                      missing, flags)
        log.info("render+probe: %d/%d ok in %.0fs",
                 len(good_cards), len(items), time.time() - t0)
        progress.say(f"render+probe 完成 {len(good_cards)}/{len(items)} ok")

        # 1) 63_cards.json（在 gate 之后写——内容=实际渲出的 spec）
        write_cards_json(run_dir, issue, items)
        print(f"cards: {F_CARDS} <- {len(items)} items")

        # 4) shots
        shots = fetch_shots(run_dir, issue.get("items") or [], good_cards,
                            missing, flags)
        # shots dict 补 run_dir 相对 src（给 chrome media 注入用）
        for iid, s in shots.items():
            try:
                s["src"] = str(s["path"].resolve().relative_to(run_dir))
            except ValueError:
                s["src"] = str(s["path"])
        print(f"shots: {len(shots)} 条出图（"
              f"{sum(1 for s in shots.values() if s['kind'] == 'placeholder')}"
              " 占位）")
        progress.say(f"shots 完成 {len(shots)} 条")

        # 5) chrome 叠加层
        rissue = _issue_for_render(issue, shots)
        chrome_pngs = render_chrome(run_dir, rissue)
        chrome_dir = run_dir / FRAMES_DIR / "chrome"
        if not chrome_pngs:
            log.warning("chrome 叠加层为空 → 帧退化为裸卡")
            chrome_dir.mkdir(parents=True, exist_ok=True)
        progress.say("chrome 叠加层完成 → 帧合成")

        # 6) 合成
        frames = composite_frames(run_dir, rissue, good_cards,
                                  chrome_dir, missing, flags)
        print(f"frames: {len(frames)} 帧 -> {FRAMES_DIR}/")

        # 7) manifests
        fm_p = write_manifests(run_dir, issue, cards_dir, good_cards,
                               frames, missing)
        progress.say("manifests 落盘")
        # StageEntry extra=forbid：n_frames 等计数进 stdout 报告，不进 meta
        meta.stage_done(run_dir, "cards", F_FRAMES_MANIFEST, status="done")
        meta.stage_done(run_dir, "cards_json", F_CARDS, status="done")
        meta.stage_done(run_dir, "cards_manifest", F_CARDS_MANIFEST,
                        status="done")
        _fix_meta_contract(run_dir, str(issue.get("date") or ""))

        rep = {"episode": issue.get("date"), "items": len(items),
               "cards_rendered": len(good_cards), "frames": len(frames),
               "shots": len(shots), "missing": sorted(set(missing)),
               "flags": flags}
        print(json.dumps({k: v for k, v in rep.items() if k != "flags"},
                         ensure_ascii=False))
        if flags:
            print("flags:", json.dumps(flags, ensure_ascii=False)[:1500])
        if missing:
            print("MISSING:", ", ".join(sorted(set(missing))))
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="cards 阶段（PLAN §7.6）")
    p.add_argument("--run-dir", type=Path, default=None,
                   help="runs/<date>；缺省=今日(Asia/Shanghai)")
    p.add_argument("--shot-test", action="store_true",
                   help="截图管道自检（just shot-test 目标）")
    p.add_argument("--url", default=None, help="--shot-test 指定 URL")
    p.add_argument("--limit", type=int, default=None,
                   help="只处理前 N 条 item（联调用）")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s cards: %(message)s")
    if args.run_dir is None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        args.run_dir = REPO / "runs" / datetime.now(
            ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    if args.shot_test:
        return shot_test(Path(args.run_dir).resolve(), args.url)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
