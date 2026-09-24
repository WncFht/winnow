"""stages/meta_qa.py — docs/PLAN.md §7.9：标题候选 + 封面 + 确定性审计 + 出片回写 + 告警。

产物（§4 契约）：
  90_title_candidates.json   meta/1  {candidates:[{title,items,chars}]}（LLM；
                               网关挂时 deterministic fallback 兜底，flag 记录）
  90_cover.png               2560×1440 模板封面（experiments/cover-title 的
                               render_cover.py 移植，模板内嵌本文件）
  90_qa.json                 qa/1    {flags[], checks{}}——合并 digest 期合规
                               flags（stage=="digest" 原样保留，本阶段 flags
                               幂等替换 stage=="meta_qa" 的旧值）
  metrics.json               各阶段耗时（00_stage_stats 并回的 elapsed_s
                               实测值优先；produced_at 相邻差分仅兜底，
                               负值记 null）/条数/LLM token 汇总

审计（种子 experiments/qa-loop/）：
  link-check   adapters/bin/lychee -vv --format json 扫全部 50.sources[].url，
               回填 sources[].reachable；dead(404/410)/botwall(401/403/429)/
               unreachable 进 flags。ok 类再经 httpx 32KB 嗅探挑战页标记
               （cf-chl/TCaptcha/geetest/awswaf…）→ botwall_200（实测 269
               URL：223 ok/15 botwall_200/2 dead，见 factcheck-layer）。
  validate     contracts.validate.validate_run(run_dir) 全量 schema lint +
               跨字段校验（含 coverage reconcile：LLM 批式 in==out 条数）；
               error→high flag，warn→low flag。
  embed-leak   抽样 ≤8 句 issue 正文（headline/tldr/body 跨条目均摊），
               lib.embed doc-cos vs 该条源 content_text；>0.95 近逐字泄漏 → flag。
  sensitive    digest --callb 已写 90_qa.flags（确定性敏感词+COMPLIANCE_PROMPT），
               本阶段合并保留。
  （ASR round-trip 属对齐后备路径，PLAN §7.9 列为审计项；edge-tts 档未启用
    ——deferred，见 checks.asr。）

回写：store.mark_reported(episode, kept item_keys) → items.verdict='reported'
  + cluster.published=1；再 expire_clusters 保鲜维护。40_selected 缺失时按
  sources[].url→raw item_key 兜底映射。

告警（§9 分级）：stage 全成功且零 flags → deadman.ping()；任何 flags →
  alert_ntfy.push 明细摘要；stage 自身崩溃 → alert fatal 后非零退出。

CLI：
  uv run stages/meta_qa.py --run-dir runs/<date> [--config config.yaml]
      [--skip-links] [--skip-embed] [--skip-cover] [--no-llm] [--no-alerts]
  uv run stages/meta_qa.py --selftest     # 离线自检（不碰网络/文件系统）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode


from adapters import alert_ntfy, deadman  # noqa: E402
from adapters import llm_swe2max as llm  # noqa: E402
from stages.lib import meta, pool, prog, prompts, store  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# artifacts this stage owns / reads
F_ISSUE = "50_issue.json"
F_SELECTED = "40_selected.json"
F_RAW = "10_raw_items.jsonl"
F_POOL_ITEMS = "38_pool_items.jsonl"   # 结转池 raw 投影（可选输入）
F_QA = "90_qa.json"
F_TITLES = "90_title_candidates.json"
F_COVER = "90_cover.png"
F_METRICS = "metrics.json"

LYCHEE = REPO / "adapters" / "bin" / "lychee"
LYCHEE_TIMEOUT = 20          # 单 URL 超时（秒）
LYCHEE_RETRIES = 2
LYCHEE_CONC = 16
LYCHEE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LEAK_SAMPLE_N = 8            # embedding-leak 抽样句数（任务指定）
LEAK_COS = 0.95              # 近逐字阈值（任务指定）

# ---------------------------------------------------------------------------
# botwall_200 嗅探标记（种子 experiments/qa-loop/audit_links.py + factcheck
# -layer/linkcheck2.py：单 marker 命中不足以判墙，须同时“页面看起来无内容”）
# ---------------------------------------------------------------------------
WALL_MARKERS = [
    re.compile(r"wappoc_appmsgcaptcha|TCaptcha\.js|captcha\.gtimg", re.I),
    re.compile(r"cf-chl-|challenge-platform|Just a moment\.\.\.", re.I),
    re.compile(r"awswaf.*challenge|aws-waf-token", re.I),
    re.compile(r"环境异常|完成验证后即可继续访问|访问过于频繁|操作频繁", re.I),
    re.compile(r"geetest|滑块验证|拖动下方滑块", re.I),
    re.compile(r"px-captcha|perimeterx|human-challenge", re.I),
]
BLOCK_TITLE = re.compile(
    r"(just a moment|attention required|security check|access denied|"
    r"环境异常|安全验证|页面不存在|^404)", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# ---------------------------------------------------------------------------
# 封面模板（逐字移植 experiments/cover-title/cover.html；query 参数驱动：
# brand/big/sub/logos/hero/date。logo 走 cdn.simpleicons.org，失败时 .fb 文本
# 兜底——确定性渲染。字体走本机 fontconfig：Alibaba PuHuiTi 3.0 / Smiley Sans /
# DingTalk JinBuTi 均已装）
# ---------------------------------------------------------------------------
COVER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#edf0ff;--blue:#486efd;--ink:#000}
body{width:2560px;height:1440px;background:var(--bg);font-family:"Alibaba PuHuiTi 3.0","HarmonyOS Sans SC",sans-serif;position:relative;overflow:hidden}
.pill{position:absolute;top:64px;background:var(--blue);color:#fff;font-weight:800;
  font-size:52px;letter-spacing:4px;padding:22px 44px;border-radius:999px}
.pill.l{left:64px}.pill.r{right:64px;font-size:46px;letter-spacing:2px}
.panel{position:absolute;left:64px;top:50%;transform:translateY(-50%);
  width:1050px;height:1130px;background:#fff;border-radius:56px;
  display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);
  padding:70px;gap:40px}
.cell{display:flex;align-items:center;justify-content:center;border-radius:40px;position:relative}
.cell img{width:150px;height:150px;object-fit:contain}
.cell .fb{font-size:64px;font-weight:900;color:#222;display:none}
.cell.hero::after{content:"";position:absolute;inset:-14px;border:14px solid var(--blue);border-radius:48px}
.right{position:absolute;left:1230px;right:100px;top:50%;transform:translateY(-54%)}
.brand{font-size:150px;font-weight:900;color:var(--ink);letter-spacing:0px;
  font-family:"Alibaba PuHuiTi 3.0";line-height:1.0;margin-bottom:70px}
.big{font-family:"Smiley Sans","DingTalk JinBuTi","Alibaba PuHuiTi 3.0",sans-serif;
  font-size:400px;font-weight:900;color:var(--ink);line-height:1.02;letter-spacing:2px}
.sub{margin-top:56px;font-size:64px;font-weight:600;color:#3a3f55;letter-spacing:2px}
</style></head><body>
<div class="pill l">AI DAILY</div>
<div class="pill r" id="date"></div>
<div class="panel" id="grid"></div>
<div class="right">
  <div class="brand" id="brand"></div>
  <div class="big" id="big"></div>
  <div class="sub" id="sub"></div>
</div>
<script>
const P = new URLSearchParams(location.search);
const logos = (P.get('logos')||'').split(',').filter(Boolean);
const hero  = P.get('hero')||logos[0]||'';
document.getElementById('brand').textContent = P.get('brand')||hero||'AI';
document.getElementById('big').textContent   = P.get('big')||'AI 早报';
document.getElementById('sub').textContent   = P.get('sub')||'';
document.getElementById('date').textContent  = P.get('date')||'';
const grid = document.getElementById('grid');
logos.slice(0,9).forEach(s=>{
  const d=document.createElement('div'); d.className='cell'+(s===hero?' hero':'');
  const i=document.createElement('img'); i.src=`https://cdn.simpleicons.org/${encodeURIComponent(s)}`; i.alt=s;
  const f=document.createElement('span'); f.className='fb'; f.textContent=s.slice(0,4).toUpperCase();
  i.onerror=()=>{i.style.display='none';f.style.display='block'};
  d.appendChild(i); d.appendChild(f); grid.appendChild(d);
});
</script></body></html>
"""

# 实体/品牌名 → simpleicons slug（icon 404 时模板有文本兜底，映射是锦上添花）
_ICON_SLUG = {
    "openai": "openai", "chatgpt": "openai", "gpt": "openai",
    "deepseek": "deepseek",
    "google": "google", "gemini": "googlegemini", "googlegemini": "googlegemini",
    "anthropic": "anthropic", "claude": "anthropic",
    "meta": "meta", "llama": "meta", "facebook": "meta",
    "qwen": "qwen", "千问": "qwen", "通义千问": "qwen", "通义": "qwen",
    "kimi": "kimi", "月之暗面": "kimi", "moonshot": "kimi",
    "alibaba": "alibabacloud", "阿里巴巴": "alibabacloud", "阿里云": "alibabacloud",
    "达摩院": "alibabacloud", "mistral": "mistralai", "mistralai": "mistralai",
    "xai": "xai", "grok": "xai", "x": "x",
    "huggingface": "huggingface", "hugging face": "huggingface",
    "github": "github", "microsoft": "microsoft", "微软": "microsoft",
    "nvidia": "nvidia", "英伟达": "nvidia", "apple": "apple", "苹果": "apple",
    "bytedance": "bytedance", "字节跳动": "bytedance", "字节": "bytedance",
    "perplexity": "perplexity", "ollama": "ollama", "stability": "stabilityai",
    "midjourney": "midjourney", "minimax": "minimax",
    "阶跃星辰": "stepfun", "stepfun": "stepfun", "step": "stepfun",
    "智谱": "zhipu", "zhipu": "zhipu", "chatglm": "zhipu",
    "中国电信": "chinatelecom", "chinatelecom": "chinatelecom",
    "android": "android", "arxiv": "arxiv", "bilibili": "bilibili",
}


# ---------------------------------------------------------------------------
# small utils（与 digest.py 同款形状）
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _load_jsonl(p: Path) -> list:
    return meta.load_jsonl(p)


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


def _flag(item: str, kind: str, severity: str, snippet: str, reason: str) -> dict:
    return {"id": item, "kind": kind, "severity": severity,
            "snippet": str(snippet)[:80], "reason": str(reason)[:80],
            "stage": "meta_qa", "at": _utcnow()}


def _cfg(config_path) -> dict:
    """整份 config（llm 段单独走 llm.load_cfg 拿 api_key）。"""
    return alert_ntfy.load_config(config_path)


def _proxy_env(cfg: dict) -> dict:
    """lychee/httpx 子进程的代理 env：config proxy.http 兜底，尊重已有 env。"""
    env = dict(os.environ)
    px = (((cfg or {}).get("proxy") or {}).get("http") or "").strip()
    if px and not (env.get("https_proxy") or env.get("HTTPS_PROXY")):
        env["https_proxy"] = env["HTTPS_PROXY"] = px
        env["http_proxy"] = env["HTTP_PROXY"] = px
    return env


def _history_db_path(cfg: dict) -> Path:
    p = str((((cfg or {}).get("storage") or {}).get("history_db"))
            or "state/history.sqlite")
    pp = Path(p)
    return pp if pp.is_absolute() else REPO / pp


# ---------------------------------------------------------------------------
# ① 标题候选（prompts.TITLE_PROMPT → llm.chat_json；fallback 确定性兜底）
# ---------------------------------------------------------------------------

def _fallback_titles(issue: dict, episode: str) -> list:
    """LLM 不可用时的确定性候选：headline 截断 + 期号尾巴，≤30 字。"""
    items = issue.get("items", []) or []
    head = (items[0].get("headline") or items[0].get("nav") or "AI 早报") if items else "AI 早报"
    head = re.sub(r"\s+", "", str(head))
    suffix = f"【AI 早报 {episode}】"
    out = [{"title": (head[: 30 - len(suffix)] + suffix), "items": [items[0]["id"]] if items else []}]
    if len(items) > 1:
        h2 = re.sub(r"\s+", "", str(items[1].get("headline") or items[1].get("nav") or ""))
        if h2:
            out.append({"title": h2[: 30 - len(suffix)] + suffix,
                        "items": [items[1]["id"]]})
    return out


def gen_titles(run_dir: Path, issue: dict, llm_cfg: dict | None,
               flags: list) -> tuple[dict, list]:
    """→ 90_title_candidates.json。返回 (doc, provs)。"""
    episode = str(issue.get("date") or issue.get("episode") or run_dir.name)
    provs: list = []
    candidates, fallback = [], False
    known_ids = {it.get("id") for it in issue.get("items", []) or []}

    if llm_cfg is not None:
        system, user = prompts.TITLE_PROMPT(issue)
        try:
            out = llm.chat_json(prompts.messages(system, user), retries=2,
                                prov_out=provs, tag="title", cfg=llm_cfg)
            for t in (out or {}).get("titles", []) or []:
                if not isinstance(t, dict) or not str(t.get("title") or "").strip():
                    continue
                title = " ".join(str(t["title"]).split())
                items = [i for i in (t.get("items") or []) if i in known_ids]
                candidates.append({"title": title, "items": items,
                                   "chars": len(title)})
        except llm.LLMError as e:
            flags.append(_flag("_title", "title_llm_unavailable", "medium",
                               str(e)[:60], "标题 LLM 调用失败，已用确定性兜底"))
            sys.stderr.write(f"WARN title LLM failed: {e} -> fallback\n")

    # 去重 + 数量/长度/期号校验
    seen, dedup = set(), []
    for c in candidates:
        if c["title"] in seen:
            continue
        seen.add(c["title"])
        dedup.append(c)
    candidates = dedup[:5]
    for c in candidates:
        if c["chars"] > 30:
            flags.append(_flag(c["items"][0] if c["items"] else "_title",
                               "title_over_30", "low", c["title"],
                               f"标题 {c['chars']} 字 >30"))
        if episode not in c["title"]:
            flags.append(_flag(c["items"][0] if c["items"] else "_title",
                               "title_missing_episode", "low", c["title"],
                               "标题未含期号"))
    if not (3 <= len(candidates) <= 5):
        if candidates:
            flags.append(_flag("_title", "title_count", "low",
                               str(len(candidates)), f"候选 {len(candidates)} 个，spec 3-5"))
        fallback = True
        candidates = _fallback_titles(issue, episode)
        for c in candidates:
            c["chars"] = len(c["title"])

    doc = {"schema": "meta/1", "episode": episode, "generated_at": _utcnow(),
           "prompt": prompts.PROMPT_VERSIONS["title"], "fallback": fallback,
           "candidates": candidates,
           "prov": provs[-1] if provs else None}
    meta.atomic_write(run_dir / F_TITLES, doc)
    print(f"titles: {len(candidates)} candidates"
          + (" (fallback)" if fallback else ""))
    return doc, provs


# ---------------------------------------------------------------------------
# ② 封面（experiments/cover-title/render_cover.py 移植，模板内嵌上方）
# ---------------------------------------------------------------------------

def _slug_of(name: str) -> str:
    """实体名 → simpleicons slug；未命中映射时 slugify（中文名原样给模板做文本兜底）。"""
    n = re.sub(r"\s+", " ", str(name or "").strip().lower())
    if not n:
        return ""
    if n in _ICON_SLUG:
        return _ICON_SLUG[n]
    slug = re.sub(r"[^a-z0-9]+", "", n)
    return slug or str(name).strip()


def _cover_params(issue: dict, titles_doc: dict | None,
                  sums_by_key: dict, key_by_id: dict) -> dict:
    """确定性封面文案：hero=标题候选[0]引用的条目（缺省首条），
    big=title_short/nav（≤10字），sub=headline（≤24字），brand=hero 实体名，
    logos=全期实体 slug（≤9，hero 置首）。"""
    items = issue.get("items", []) or []
    hero = items[0] if items else {}
    cand = ((titles_doc or {}).get("candidates") or [])
    if cand and cand[0].get("items"):
        hid = cand[0]["items"][0]
        hero = next((it for it in items if it.get("id") == hid), hero)

    hero_key = key_by_id.get(hero.get("id"))
    hero_sum = sums_by_key.get(hero_key) or {}
    ents = list(hero.get("entities") or hero_sum.get("entities") or [])
    brand = ents[0] if ents else (hero.get("nav") or hero.get("id") or "AI")
    big = hero.get("title_short") or hero.get("nav") or hero.get("headline") or "AI 早报"
    big = re.sub(r"\s+", "", str(big))[:10] or "AI 早报"
    sub = re.sub(r"\s+", "", str(hero.get("headline") or hero.get("tldr") or ""))[:24]

    logos, seen = [], set()

    def push(name):
        s = _slug_of(name)
        if s and s not in seen:
            seen.add(s)
            logos.append(s)

    push(brand)
    for it in items:
        k = key_by_id.get(it.get("id"))
        for e in (it.get("entities") or (sums_by_key.get(k) or {}).get("entities") or []):
            push(e)
        if it.get("nav"):
            push(it["nav"])
    logos = logos[:9] or ["github", "huggingface", "openai"]
    hero_slug = logos[0] if _slug_of(brand) not in logos else _slug_of(brand)
    return {"brand": str(brand), "big": big, "sub": sub,
            "hero": hero_slug, "logos": ",".join(logos),
            "date": str(issue.get("date") or "")}


def render_cover(run_dir: Path, params: dict, flags: list) -> Path | None:
    """模板 HTML → 90_cover.png（2560×1440）。playwright 失败 → flag，不炸阶段。"""
    tmp = run_dir / "tmp" / "meta_qa"
    tmp.mkdir(parents=True, exist_ok=True)
    tpl = tmp / "cover.html"
    tpl.write_text(COVER_HTML, encoding="utf-8")
    url = tpl.resolve().as_uri() + "?" + urlencode(params, quote_via=quote)
    out = run_dir / F_COVER
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # /tmp 是 >90% 满的 tmpfs——chromium profile/临时文件指到 run_dir/tmp
            b = p.chromium.launch(
                args=["--no-sandbox", "--force-color-profile=srgb",
                      "--disable-dev-shm-usage"],
                env={**os.environ, "TMPDIR": str(tmp)})
            try:
                pg = b.new_page(viewport={"width": 2560, "height": 1440},
                                device_scale_factor=1)
                pg.goto(url)
                pg.wait_for_function(
                    "Array.from(document.images).every(i=>i.complete)",
                    timeout=20000)
                pg.wait_for_timeout(400)
                pg.screenshot(path=str(out))
            finally:
                b.close()
    except Exception as e:
        flags.append(_flag("_cover", "cover_render_failed", "medium",
                           str(e)[:80], "模板封面渲染失败（playwright/chromium）"))
        sys.stderr.write(f"WARN cover render failed: {e}\n")
        return None
    if not out.exists() or out.stat().st_size == 0:
        flags.append(_flag("_cover", "cover_empty", "medium", "", "封面产物为空"))
        return None
    print(f"cover: {out.name} {out.stat().st_size} bytes "
          f"(hero={params.get('hero')}, big={params.get('big')!r})")
    return out


# ---------------------------------------------------------------------------
# ③a link-check：lychee 全量 + httpx botwall_200 嗅探 + reachable 回填
# ---------------------------------------------------------------------------

def _lychee_run(urls: list, cfg: dict) -> dict | None:
    """stdin 喂 URL → `-vv --format json` 解析。返回解析后的 dict 或 None。"""
    if not LYCHEE.exists():
        return None
    cmd = [str(LYCHEE), "-vv", "--format", "json", "--no-progress",
           "--timeout", str(LYCHEE_TIMEOUT), "--max-retries", str(LYCHEE_RETRIES),
           "--max-concurrency", str(LYCHEE_CONC),
           "--user-agent", LYCHEE_UA, "-"]
    try:
        # lychee 有错链时退出码非零——照常解析 stdout JSON
        r = subprocess.run(cmd, input="\n".join(urls) + "\n",
                           capture_output=True, text=True, timeout=600,
                           env=_proxy_env(cfg))
    except Exception as e:
        sys.stderr.write(f"WARN lychee spawn failed: {e}\n")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"WARN lychee bad json (rc={r.returncode}): "
                         f"{(r.stdout or r.stderr)[:300]}\n")
        return None


def _classify_lychee(status: dict) -> str:
    """lychee status → ok|dead|botwall|timeout|unreachable|http_<code>。"""
    code = status.get("code")
    text = str(status.get("text") or "") + str(status.get("details") or "")
    if code is not None:
        code = int(code)
        if 200 <= code < 400:
            return "ok"
        if code in (404, 410):
            return "dead"
        if code in (401, 403, 429):
            return "botwall"
        if 400 <= code < 600:
            return f"http_{code}"
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return "timeout"
    return "unreachable"


def _sniff_botwall200(url: str, cfg: dict) -> str:
    """ok 类 URL 的 32KB 嗅探：挑战页标记 + 空内容 → botwall_200。"""
    import httpx
    try:
        proxy = (((cfg or {}).get("proxy") or {}).get("http") or "") or None
        with httpx.Client(headers={"User-Agent": LYCHEE_UA},
                          follow_redirects=True, timeout=12.0,
                          proxy=proxy, trust_env=(proxy is None)) as c:
            body = b""
            with c.stream("GET", url) as r:
                for chunk in r.iter_bytes(32768):
                    body += chunk
                    if len(body) >= 32768:
                        break
        text = body.decode("utf-8", "ignore")
        m = TITLE_RE.search(text)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        wall = any(p.search(text) for p in WALL_MARKERS)
        looks_empty = (not title) or bool(BLOCK_TITLE.search(title)) or len(body) < 4000
        if wall and looks_empty:
            return "botwall_200"
        if BLOCK_TITLE.search(title):
            return "botwall_200"
        return "ok"
    except Exception:
        return "ok"      # 嗅探失败不降级 lychee 的 ok 结论


def link_audit(run_dir: Path, issue: dict, cfg: dict, flags: list,
               p: "prog.Prog | None" = None) -> dict:
    """扫全部 sources[].url，回填 reachable。返回 checks['links']。"""
    urls, seen = [], set()
    for it in issue.get("items", []) or []:
        for s in it.get("sources", []) or []:
            u = s.get("url")
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
    check = {"total": len(urls), "checked": 0, "ok": 0, "dead": [],
             "botwall": [], "unreachable": [], "skipped": False}
    if not urls:
        check["skipped"] = True
        return check

    if p:
        p.say(f"links: lychee 扫 {len(urls)} urls（子进程 ≤600s）")
    res = _lychee_run(urls, cfg)
    if res is None:
        flags.append(_flag("_links", "lychee_unavailable", "medium",
                           str(LYCHEE), "lychee 不可用/输出不可解析，link-check 跳过"))
        check["skipped"] = True
        check["reason"] = "lychee_unavailable"
        return check

    cls = {}   # url -> class
    def _each(name, fn):
        for entries in (res.get(name) or {}).values():
            for e in entries or []:
                if e.get("url"):
                    fn(e["url"], e)

    _each("success_map", lambda u, e: cls.__setitem__(u, "ok"))
    _each("redirect_map", lambda u, e: cls.__setitem__(u, "ok"))
    _each("timeout_map", lambda u, e: cls.__setitem__(u, "timeout"))
    _each("excluded_map", lambda u, e: cls.__setitem__(u, "excluded"))
    _each("error_map", lambda u, e: cls.__setitem__(u, _classify_lychee(e.get("status") or {})))
    for u in urls:
        cls.setdefault(u, "unknown")

    # botwall_200 嗅探：仅 ok 类（lychee 只看了状态码，挑战页 200 它看不见）
    ok_urls = [u for u, c in cls.items() if c == "ok"]
    if ok_urls:
        if p:
            p.say(f"links: {len(ok_urls)} ok urls 进 botwall_200 嗅探（8 workers）")
        sp = prog.Prog(run_dir, "meta_qa", total=len(ok_urls),
                       step=max(10, min(100, len(ok_urls) // 40)),
                       interval=30.0)
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                for i, (u, c) in enumerate(zip(
                        ok_urls,
                        ex.map(lambda x: _sniff_botwall200(x, cfg), ok_urls)), 1):
                    cls[u] = c
                    sp.tick(i, "botwall sniff")
            sp.tick(len(ok_urls), "botwall sniff done", force=True)
        finally:
            sp.close()

    for u, c in cls.items():
        if c in ("ok",):
            check["ok"] += 1
        elif c == "dead":
            check["dead"].append(u)
        elif c in ("botwall", "botwall_200"):
            check["botwall"].append(u)
        elif c == "excluded":
            continue
        else:
            check["unreachable"].append(u)
    check["checked"] = len(urls)

    # reachable 回填 + flags
    owner = {}   # url -> [item ids]（flag 归位用）
    for it in issue.get("items", []) or []:
        for s in it.get("sources", []) or []:
            u = s.get("url")
            if not u:
                continue
            owner.setdefault(u, []).append(it.get("id", "?"))
            c = cls.get(u)
            if c is None or c == "excluded":
                continue                    # 未判定：不动 reachable
            s["reachable"] = (c == "ok")
    for u in check["dead"]:
        flags.append(_flag(",".join(owner.get(u, ["?"])), "dead_link", "high",
                           u, "link-check: 404/410 死链"))
    for u in check["botwall"]:
        flags.append(_flag(",".join(owner.get(u, ["?"])), "botwall", "medium",
                           u, "link-check: 反爬/挑战页（403/429/botwall_200）"))
    for u in check["unreachable"]:
        flags.append(_flag(",".join(owner.get(u, ["?"])), "link_unreachable", "low",
                           u, "link-check: 超时/网络错误（可为瞬时）"))

    meta.atomic_write(run_dir / F_ISSUE, issue)
    print(f"links: {check['ok']}/{check['total']} ok, "
          f"{len(check['dead'])} dead, {len(check['botwall'])} botwall, "
          f"{len(check['unreachable'])} unreachable")
    return check


# ---------------------------------------------------------------------------
# ③b contracts.validate_run（schema lint + 跨字段 + coverage reconcile）
# ---------------------------------------------------------------------------

def validate_audit(run_dir: Path, flags: list, cfg: dict | None = None) -> dict:
    """contracts.validate_run 包装；cfg.storage.items_db 透传给 db-consistency
    （--config 指向 scratch 配置时查的是同一个池，与 writeback 口径一致）。"""
    from contracts.validate import validate_run
    items_db = (((cfg or {}).get("storage") or {}).get("items_db"))
    rep = validate_run(run_dir, items_db=items_db)
    for v in rep.get("violations", []):
        flags.append(_flag(v.get("where", "?"),
                           f"validate_{v.get('rule', 'rule')}",
                           "high" if v.get("level") == "error" else "low",
                           v.get("msg", "")[:80],
                           "contracts.validate 违例"))
    check = {"ok": rep.get("ok"), "checked": rep.get("checked", []),
             "skipped_artifacts": rep.get("skipped", []),
             "errors": sum(1 for v in rep["violations"] if v["level"] == "error"),
             "warns": sum(1 for v in rep["violations"] if v["level"] == "warn")}
    print(f"validate: ok={check['ok']} errors={check['errors']} "
          f"warns={check['warns']} checked={len(check['checked'])}")
    return check


# ---------------------------------------------------------------------------
# ③c embedding-leak：抽样 issue 句 vs 源 content_text 的 doc-cos
# ---------------------------------------------------------------------------

def _sample_sentences(issue: dict, n: int = LEAK_SAMPLE_N) -> list:
    """跨条目均摊抽 ≤n 句（headline/tldr/body，确定性：按序取步长抽样）。"""
    pool = []
    for it in issue.get("items", []) or []:
        for sent in [it.get("headline"), it.get("tldr")] + list(it.get("body") or []):
            s = re.sub(r"\s+", " ", str(sent or "")).strip()
            s = re.sub(r"\*\*|`", "", s)
            if len(s) >= 8:
                pool.append((it.get("id", "?"), s))
    if not pool:
        return []
    stride = max(1, len(pool) // n) if len(pool) > n else 1
    return pool[::stride][:n]


def embed_leak_audit(issue: dict, content_by_key: dict, key_by_id: dict,
                     flags: list) -> dict:
    check = {"sampled": 0, "max_cos": None, "flagged": 0, "skipped": False}
    sample = _sample_sentences(issue)
    pairs = []
    for iid, sent in sample:
        content = (content_by_key.get(key_by_id.get(iid)) or "").strip()
        if content:
            pairs.append((iid, sent, content[:2500]))
    if not pairs:
        check["skipped"] = True
        check["reason"] = "no source content_text"
        return check
    try:
        from stages.lib import embed
        vs = embed.embed([p[1] for p in pairs], mode="doc")
        vc = embed.embed([p[2] for p in pairs], mode="doc")
    except Exception as e:
        flags.append(_flag("_embed", "embed_unavailable", "low", str(e)[:60],
                           "embedding-leak 审计跳过（embed 模型不可用）"))
        check["skipped"] = True
        check["reason"] = "embed_unavailable"
        return check
    import numpy as np
    cos = np.sum(vs * vc, axis=1)      # 行已 unit-norm → 点积即 cos
    check["sampled"] = len(pairs)
    check["max_cos"] = round(float(np.max(cos)), 4)
    for (iid, sent, _), c in zip(pairs, cos):
        if float(c) > LEAK_COS:
            check["flagged"] += 1
            flags.append(_flag(iid, "near_verbatim", "medium", sent[:60],
                               f"issue 句与源正文 cos={float(c):.3f} > {LEAK_COS} 近逐字"))
    print(f"embed-leak: {check['sampled']} sampled, max_cos={check['max_cos']}, "
          f"flagged={check['flagged']}")
    return check


# ---------------------------------------------------------------------------
# ④ history 回写 + ⑤ metrics
# ---------------------------------------------------------------------------

def _kept_maps(run_dir: Path, issue: dict) -> tuple[list, dict]:
    """kept item_keys + id→item_key。40_selected 缺失时按 sources.url→raw 兜底。"""
    key_by_id, keys = {}, []
    sel_p = run_dir / F_SELECTED
    if sel_p.exists():
        for k in (_load_json(sel_p).get("kept") or []):
            if k.get("item_key") and k.get("id"):
                key_by_id[k["id"]] = k["item_key"]
                keys.append(k["item_key"])
    if not key_by_id:
        by_url = {}
        for name in (F_RAW, F_POOL_ITEMS):   # 当期 raw ∪ 结转池投影（缺文件跳过）
            p = run_dir / name
            if not p.exists():
                continue
            for r in _load_jsonl(p):
                k = r.get("item_key")
                if not k:
                    continue
                for u in {r.get("url"), r.get("url_canon"),
                          (r.get("url") or "").rstrip("/"),
                          (r.get("url_canon") or "").rstrip("/")} - {None, ""}:
                    by_url[u] = k
        for it in issue.get("items", []) or []:
            for s in it.get("sources", []) or []:
                u = (s.get("url") or "")
                k = by_url.get(u) or by_url.get(u.rstrip("/"))
                if k and it.get("id") not in key_by_id:
                    key_by_id[it["id"]] = k
                    keys.append(k)
    return keys, key_by_id


def writeback(run_dir: Path, issue: dict, cfg: dict, keys: list,
              flags: list) -> dict:
    episode = str(issue.get("date") or issue.get("episode") or run_dir.name)
    db = _history_db_path(cfg)
    check = {"db": str(db), "marked": 0, "expired": 0, "skipped": not keys}
    if not keys:
        return check
    try:
        conn = store.init_db(db)
        check["marked"] = store.mark_reported(conn, episode, keys)
        check["expired"] = store.expire_clusters(conn, today=episode)
        conn.commit()
        conn.close()
    except Exception as e:
        flags.append(_flag("_writeback", "history_writeback_failed", "medium",
                           str(e)[:80], f"history.sqlite 回写失败: {db}"))
        check["error"] = str(e)[:120]
        return check
    # items.sqlite 池回写（独立 try：历史库成功后池失败不拖累主回写）
    try:
        check["pool_marked"] = pool.mark_used(
            pool.resolve_path(None, cfg), episode, keys)
    except Exception as e:
        check["pool_marked"] = 0
        check["pool_error"] = str(e)[:120]
        flags.append(_flag("_writeback", "pool_mark_used_failed", "low",
                           str(e)[:80], "items.sqlite used_in_episode 回写失败"))
    if keys and check["marked"] == 0:
        flags.append(_flag("_writeback", "history_writeback_empty", "medium",
                           f"{len(keys)} kept keys", "kept 条目不在 history.sqlite（dedup 未跑？）"))
    print(f"writeback: marked={check['marked']}/{len(keys)} "
          f"expired={check['expired']} pool_marked={check['pool_marked']} db={db}")
    return check


def build_metrics(run_dir: Path, qa: dict, title_provs: list,
                  sub_seconds: dict, checks: dict) -> dict:
    """metrics.json：各阶段耗时（00_meta produced_at 差分）+ 条数 + LLM token。"""
    m = meta.meta_status(run_dir)
    stages = m.get("stages", {})
    order = sorted(stages.items(), key=lambda kv: kv[0])

    def _ts(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    # 先算相邻 produced_at 差分作兜底（负值=重写/乱序 → None），
    # 再用 00_stage_stats 并回 entry 的 elapsed_s 实测值覆盖——双保险。
    diffs = {}
    prev_name, prev_ts = None, None
    for name, e in order:
        ts = _ts(e.get("produced_at"))
        if prev_name and ts is not None and prev_ts is not None:
            d = round(ts - prev_ts, 1)
            diffs[prev_name] = d if d >= 0 else None
        prev_name, prev_ts = name, ts
    stage_secs = {}
    for name, e in order:
        if isinstance(e.get("elapsed_s"), (int, float)):
            stage_secs[name] = round(float(e["elapsed_s"]), 1)
        elif name in diffs:
            stage_secs[name] = diffs[name]
    stage_secs["meta_qa"] = round(sum(sub_seconds.values()), 1)

    counts = {}
    def _n_jsonl(name):
        p = run_dir / name
        if not p.exists():
            return None
        with open(p, encoding="utf-8", newline=None) as f:
            return sum(1 for l in f if l.strip())
    counts["raw_items"] = _n_jsonl("10_raw_items.jsonl")
    counts["filtered"] = _n_jsonl("20_filtered.jsonl")
    counts["summaries"] = _n_jsonl("30_summaries.jsonl")
    counts["dedup"] = _n_jsonl("35_dedup.jsonl")
    counts["voice_segs"] = _n_jsonl("60_voice_script.jsonl")
    for name, key in (("40_selected.json", "kept"), ("50_issue.json", "items")):
        p = run_dir / name
        if p.exists():
            try:
                doc = _load_json(p)
                counts[name.split("_")[1].split(".")[0]] = len(doc.get(key) or [])
            except Exception:
                pass
    for name, key, label in (("61_audio_manifest.json", "files", "audio_files"),
                             ("64_frames_manifest.json", "files", "frames")):
        p = run_dir / name
        if p.exists():
            try:
                counts[label] = len(_load_json(p).get(key) or [])
            except Exception:
                pass
    counts["links_checked"] = (checks.get("links") or {}).get("checked")
    counts["flags"] = len(qa.get("flags", []))

    # LLM token：00_meta stage extras 里的 prov 类字段 + 本阶段 title 调用
    llm_agg = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "by_tag": {}}
    provs = list(title_provs)
    for name, e in stages.items():
        for k, v in (e or {}).items():
            if isinstance(v, dict) and "prompt_tokens" in v:
                provs.append(v | {"tag": v.get("tag") or f"{name}.{k}"})
    for p in provs:
        pt, ct = p.get("prompt_tokens"), p.get("completion_tokens")
        if pt is None and ct is None:
            continue
        llm_agg["calls"] += 1
        llm_agg["prompt_tokens"] += int(pt or 0)
        llm_agg["completion_tokens"] += int(ct or 0)
        tag = p.get("tag") or "?"
        t = llm_agg["by_tag"].setdefault(tag, {"calls": 0, "prompt_tokens": 0,
                                               "completion_tokens": 0})
        t["calls"] += 1
        t["prompt_tokens"] += int(pt or 0)
        t["completion_tokens"] += int(ct or 0)

    doc = {"schema": "metrics/1", "episode": qa.get("episode"),
           "generated_at": _utcnow(),
           "stage_seconds": stage_secs,
           "stage_seconds_note": "stage_begin→done 实测 elapsed_s 优先；缺失退相邻 produced_at 差分（负值记 null）",
           "item_counts": counts,
           "llm": llm_agg,
           "meta_qa_seconds": {k: round(v, 1) for k, v in sub_seconds.items()}}
    meta.atomic_write(run_dir / F_METRICS, doc)
    return doc


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------

def _alert(flags: list, cfg: dict, episode: str, enabled: bool) -> None:
    """§9 分级：零 flags → deadman ping；有 flags → ntfy 明细（default 级）。"""
    if not enabled:
        return
    if not flags:
        ok = deadman.ping(config=cfg)
        print(f"deadman ping -> {ok}")
        return
    from collections import Counter
    kinds = Counter(f["kind"] for f in flags)
    top = flags[:8]
    body = (f"期号 {episode} QA 共 {len(flags)} 个 flag\n"
            + "分类: " + ", ".join(f"{k}×{n}" for k, n in kinds.most_common())
            + "\n" + "\n".join(f"- [{f['severity']}] {f['id']}: {f['reason']}"
                               for f in top))
    ok = alert_ntfy.notify(f"AI早报 {episode} QA {len(flags)} flags", body,
                           config=cfg, tags=["memo"])
    print(f"ntfy push -> {ok} ({len(flags)} flags)")


def run(run_dir: Path, config_path=None, *, skip_links=False, skip_embed=False,
        skip_cover=False, no_llm=False, no_alerts=False) -> int:
    t0 = time.time()
    sub = {}
    flags: list = []
    cfg = _cfg(config_path)
    p = prog.Prog(run_dir, "meta_qa", total=6, step=1, interval=30.0)
    issue_p = _need(run_dir, F_ISSUE,
                    "先跑 `just digest`（Call A）+ `just edit-import`（编辑闸锁 50_issue.json）")
    issue = _load_json(issue_p)
    episode = str(issue.get("date") or issue.get("episode") or run_dir.name)
    degraded = bool(issue.get("degraded")) or not (issue.get("items") or [])

    # 辅助映射：id→item_key、item_key→summary/raw
    keys, key_by_id = _kept_maps(run_dir, issue)
    sums_by_key, content_by_key = {}, {}
    if (run_dir / "30_summaries.jsonl").exists():
        for s in _load_jsonl(run_dir / "30_summaries.jsonl"):
            sums_by_key[s.get("item_key")] = s
    if (run_dir / F_RAW).exists():
        for r in _load_jsonl(run_dir / F_RAW):
            content_by_key[r.get("item_key")] = r.get("content_text") or ""

    # ① 标题
    t = time.time()
    llm_cfg = None if no_llm else llm.load_cfg(config_path)
    p.say("titles: 标题候选生成（" + ("LLM" if llm_cfg else "确定性兜底") + "）")
    titles_doc, title_provs = gen_titles(run_dir, issue, llm_cfg, flags)
    sub["titles"] = time.time() - t
    p.tick(1, "titles")

    # ② 封面
    t = time.time()
    cover_p = None
    if not skip_cover:
        p.say("cover: playwright 渲染 2560×1440 封面")
        cover_p = render_cover(run_dir, _cover_params(issue, titles_doc,
                                                      sums_by_key, key_by_id), flags)
    sub["cover"] = time.time() - t
    p.tick(2, "cover" + (" skipped" if skip_cover else ""))

    # ③ 审计
    checks = {"title": {"candidates": len(titles_doc.get("candidates", [])),
                        "fallback": titles_doc.get("fallback", False)},
              "cover": {"rendered": bool(cover_p)}}
    t = time.time()
    checks["links"] = ({"skipped": True, "reason": "--skip-links"} if skip_links or degraded
                       else link_audit(run_dir, issue, cfg, flags, p=p))
    sub["links"] = time.time() - t
    p.tick(3, "links" + (" skipped" if skip_links or degraded else ""))
    t = time.time()
    checks["validate"] = validate_audit(run_dir, flags, cfg)
    sub["validate"] = time.time() - t
    p.tick(4, "validate")
    t = time.time()
    if not (skip_embed or degraded):
        p.say("embed-leak: 抽样句 embed + doc-cos 审计")
    checks["embed_leak"] = ({"skipped": True, "reason": "--skip-embed/degraded"}
                            if skip_embed or degraded
                            else embed_leak_audit(issue, content_by_key,
                                                  key_by_id, flags))
    sub["embed_leak"] = time.time() - t
    p.tick(5, "embed_leak" + (" skipped" if skip_embed or degraded else ""))
    checks["asr"] = {"skipped": True,
                     "reason": "对齐后备未启用（PLAN §7.9 审计项，换非 edge 引擎时启用）"}
    if degraded:
        checks["skipped"] = "no_items"        # §11 零条目停刊标记

    # 合并 digest 期 flags（stage!="meta_qa" 全保留）+ 写 90_qa.json
    qa_p = run_dir / F_QA
    qa = {"schema": "qa/1", "episode": episode, "flags": [], "checks": {}}
    if qa_p.exists():
        try:
            qa = _load_json(qa_p) | {"schema": "qa/1", "episode": episode}
        except Exception:
            pass
    qa["flags"] = [f for f in qa.get("flags", []) if f.get("stage") != "meta_qa"] + flags
    qa["checks"] = (qa.get("checks") or {}) | checks
    qa["produced_at"] = _utcnow()
    meta.atomic_write(qa_p, qa)
    sub["qa_write"] = 0.0

    # ④ 回写
    t = time.time()
    checks["writeback"] = writeback(run_dir, issue, cfg, keys, flags)
    if flags != [f for f in qa["flags"] if f.get("stage") == "meta_qa"]:
        # writeback 追加的 flags 要落回 90_qa.json
        qa["flags"] = [f for f in qa["flags"] if f.get("stage") != "meta_qa"] + flags
        qa["checks"]["writeback"] = checks["writeback"]
        meta.atomic_write(qa_p, qa)
    sub["writeback"] = time.time() - t

    # ⑤ metrics + meta 登记
    t = time.time()
    build_metrics(run_dir, qa, title_provs, sub, checks)
    sub["metrics"] = time.time() - t
    p.tick(6, "writeback+metrics", force=True)
    # 注意：StageEntry extra=forbid——extra 键会违 schema-lint，簿记走 metrics.json
    meta.stage_done(run_dir, "meta_title", F_TITLES, status="done")
    if cover_p:
        meta.stage_done(run_dir, "meta_cover", F_COVER, status="done")
    meta.stage_done(run_dir, "meta_qa", F_QA, status="done")

    _alert(qa["flags"], cfg, episode, enabled=not no_alerts)
    print(f"meta_qa done in {time.time() - t0:.1f}s: "
          f"{len(qa['flags'])} flags total ({len(flags)} from meta_qa)")
    p.close()
    return 0


# ---------------------------------------------------------------------------
# selftest（离线：纯函数断言，不碰网络/文件系统/LLM）
# ---------------------------------------------------------------------------

def _selftest() -> None:
    # lychee 分类
    assert _classify_lychee({"code": 404}) == "dead"
    assert _classify_lychee({"code": 410}) == "dead"
    assert _classify_lychee({"code": 403}) == "botwall"
    assert _classify_lychee({"code": 429}) == "botwall"
    assert _classify_lychee({"code": 200}) == "ok"
    assert _classify_lychee({"code": 301}) == "ok"
    assert _classify_lychee({"code": 500}) == "http_500"
    assert _classify_lychee({"text": "Timeout: request timed out"}) == "timeout"
    assert _classify_lychee({"text": "Network error: Connection closed"}) == "unreachable"

    # fallback 标题 ≤30 字且含期号
    issue = {"date": "2026-09-20", "items": [
        {"id": "deepseek", "nav": "DeepSeek",
         "headline": "DeepSeek API 节假日全天按空闲时段计费",
         "tldr": "DeepSeek 调价。", "body": ["DeepSeek 调价，节假日生效。"],
         "sources": [{"url": "https://platform.deepseek.com/", "primary": True}]},
        {"id": "qwen", "nav": "Qwen", "headline": "Qwen 发布 LiveTranslate",
         "tldr": "60 种语言。", "body": [], "sources": []}]}
    fb = _fallback_titles(issue, "2026-09-20")
    assert 1 <= len(fb) <= 5 and all(len(t["title"]) <= 30 for t in fb), fb
    assert all("2026-09-20" in t["title"] for t in fb)

    # cover 参数推导
    params = _cover_params(issue, {"candidates": [{"title": "x", "items": ["qwen"]}]},
                           {"k2": {"entities": ["Qwen", "阿里巴巴"]}},
                           {"deepseek": "k1", "qwen": "k2"})
    assert params["hero"] == "qwen" and params["big"] == "Qwen", params
    assert params["logos"].split(",")[0] == "qwen"
    assert params["date"] == "2026-09-20"
    assert _slug_of("月之暗面") == "kimi" and _slug_of("DeepSeek") == "deepseek"

    # 抽样均摊 ≤8
    big_issue = {"items": [{"id": f"i{i}", "headline": f"第{i}条标题甲乙丙丁戊己",
                            "tldr": f"第{i}条概要甲乙丙丁戊己", "body": [f"第{i}条正文甲乙丙丁戊己"]}
                           for i in range(10)]}
    sample = _sample_sentences(big_issue, 8)
    assert len(sample) == 8 and len({s[0] for s in sample}) >= 4
    assert not _sample_sentences({"items": []})

    # flag 形状
    f = _flag("x", "dead_link", "high", "u", "r")
    assert f["stage"] == "meta_qa" and f["at"] and len(f["snippet"]) <= 80

    # 模板无残留占位
    assert "{date}" not in COVER_HTML and "URLSearchParams" in COVER_HTML
    print("meta_qa selftest OK")


def main() -> None:
    ap = argparse.ArgumentParser(description="meta+QA stage (PLAN §7.9)")
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--skip-links", action="store_true", help="跳过 lychee/sniff 网络审计")
    ap.add_argument("--skip-embed", action="store_true", help="跳过 embedding-leak 审计")
    ap.add_argument("--skip-cover", action="store_true", help="跳过封面渲染")
    ap.add_argument("--no-llm", action="store_true", help="标题走确定性兜底（不打网关）")
    ap.add_argument("--no-alerts", action="store_true", help="不发 ntfy/deadman")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return
    if not args.run_dir:
        raise _die("--run-dir 必填", "uv run stages/meta_qa.py --run-dir runs/<date>")
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        raise _die(f"run_dir 不存在: {run_dir}", "先跑 `just collect` 等上游阶段")

    try:
        with meta.run_lock(run_dir):
            meta.stage_begin(run_dir)  # 锁内登记；stage_done 时自动清除
            rc = run(run_dir, args.config, skip_links=args.skip_links,
                     skip_embed=args.skip_embed, skip_cover=args.skip_cover,
                     no_llm=args.no_llm, no_alerts=args.no_alerts)
    except SystemExit:
        raise
    except Exception as e:
        # fatal 级（§9）：ntfy urgent + 非零退出
        try:
            cfg = _cfg(args.config)
            alert_ntfy.fatal("AI早报 meta_qa 崩溃", f"{run_dir.name}: {type(e).__name__}: {e}",
                             config=cfg)
        except Exception:
            pass
        raise _die(f"meta_qa 未捕获异常: {type(e).__name__}: {e}",
                   "看 runs/<date>/logs 或重跑 --selftest") from e
    sys.exit(rc)


if __name__ == "__main__":
    main()
