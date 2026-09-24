#!/usr/bin/env python3
"""stages/collect.py — 采集层（docs/PLAN.md §5 + §4 契约）。

输入  : sources.yaml（唯一人工维护源注册表）+ config.yaml|config.example.yaml
        + state/seen.json + state/source_health.json
处理  : preflight(§5.4) → 每源 method 分派（rss|atom|youtube_rss → feedparser；
        json_api → 具名 adapter + 通用 JSON walker；sitemap_diff|changelog_diff
        → URL diff 产 signal 条目；x → 四路级联 lib.x_nitter→lib.x_ssr→
        lib.x_synd→adapters.x_paid 逐路 try/except 落路；reddit →
        lib.reddit_collect.fetch_sub；weibo → lib.weibo_collect.fetch_uid /
        fetch_source；wechat → 记 skipped(disabled)（D3）；全部惰性 import 于
        dispatch try 内，缺模块记 collector_not_ready 不炸 Tier A）→ raw_item
        归一 → 正文补抓 pass（content_text<200 字或 signal → trafilatura）→
        媒体 pass（防盗链图床本地化 runs/<date>/media/）
输出  : 10_raw_items.jsonl (raw_item/1) + 11_raw_manifest.json (raw_manifest/1)
        + state/seen.json + state/source_health.json + 00_meta 登记
        + data/raw_cache/<date>/<src>/ 原文留档

布局  : 本文件只做编排（preflight → 分派 → 归一 → 正文/媒体 pass → 落盘）。
        源形适配器在 stages/lib/sources/{feed,api,diff}.py；文本/构造 helper
        在 lib/{normalize,http,rawitem}.py；旧符号经下方别名层可用。

契约偏差说明（contract extra=forbid 限制下的合法编码）：
  * PLAN §5.2 的 `_fetch.kind="signal"`：RawFetch 无 kind 字段，改为
    tags 含 "signal" + _source.kind="scrape" + date_published=null 表达同一语义。
  * manifest 的 proxy_ok/degraded/每源明细：RawManifest 顶字段 forbid extra，
    proxy_ok/degraded/preflight 收进 window{}（dict 自由形），每源统计进
    sources[] 自由 dict —— 字段都在，只是嵌套位置。
  * _fetch.via 契约枚举 direct|mirror|cache|manual：代理传输记 "mirror"；
    平台采集器命中路由（x:nitter/x:ssr/x:synd/x:paid/reddit:loid/weibo:uid）
    塞不进 via → 记条目 tags 'via:<route>' + manifest sources[].via。
  * 普通源发全量解析条目（dedup L0 按 url_hash 压重）；signal 条目只发新 URL。

feed_url 模板：sources.yaml 的 feed_url/failover 可写相对日期占位符
  {T±Nd}（→ %Y-%m-%d）与 {T±Nd_ts}（→ epoch 秒），锚点 T = 采集窗口右沿
  （run_date 06:30 Asia/Shanghai），故 {T-1d}≈窗口左沿。例 github_search
  `created:>{T-2d}`、hn_algolia `created_at_i>{T-1d_ts}`。

用法: uv run stages/collect.py --run-dir runs/2026-09-22
      uv run stages/collect.py --selftest          # 3 代表源冒烟
      uv run stages/collect.py --max-sources 8 [--only name1,name2]
      uv run stages/collect.py --manual URL [--title T]
      其余 flag：--sources PATH（源注册表，缺省 repo 根 sources.yaml）、
      --config PATH、--max-content-fetches N（正文补抓全局预算，缺省 600）、
      --items-db PATH（跨期条目池，缺省 config.storage.items_db →
      state/items.sqlite；仅真·日期 run-dir 才写池）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


import yaml  # noqa: E402

from contracts.models import RawItem, RawManifest  # noqa: E402
from stages.lib import http as lhttp  # noqa: E402
from stages.lib import meta, normalize, pool, prog, rawitem  # noqa: E402
from stages.lib.sources import api as _src_api  # noqa: E402
from stages.lib.sources import common as _src_common  # noqa: E402
from stages.lib.sources import diff as _src_diff  # noqa: E402
from stages.lib.sources import feed as _src_feed  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

ITEMS_NAME = "10_raw_items.jsonl"
MANIFEST_NAME = "11_raw_manifest.json"
SEEN_PATH = REPO / "state" / "seen.json"
HEALTH_PATH = REPO / "state" / "source_health.json"

CONTENT_MIN = 200                    # <200 字 → 正文补抓
SEEN_URL_CAP = _src_common.SEEN_URL_CAP  # 每源 seen urls 滚动上限
CONTENT_TEXT_CAP = rawitem.CONTENT_TEXT_CAP  # content_text 上限（对齐 trafilatura 回填）

# 防盗链图床（浏览器热链 403）：本地化下载到 run_dir/media/
WALLED_IMG_HOSTS = (
    "mmbiz.qpic.cn", "mmbiz.qlogo.cn", "wx.qlogo.cn",
    "sinaimg.cn", "wx1.sinaimg", "wx2.sinaimg", "wx3.sinaimg", "wx4.sinaimg",
)

# 代理「required」源预检失败时的状态
ST_OK, ST_EMPTY, ST_SKIPPED = "ok", "empty", "skipped"

_METHODS = {"rss", "atom", "json_api", "sitemap_diff", "changelog_diff",
            "x", "reddit", "weibo", "wechat", "youtube_rss", "manual"}

log = logging.getLogger("collect")


# ============================================================ small utils ===

def _today_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _window(run_date: str) -> tuple[datetime, datetime]:
    """采集窗口 = run_date-1 06:30 → run_date 06:30（Asia/Shanghai，§0）。"""
    try:
        d = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError:
        d = datetime.now(TZ).date()
    end = datetime(d.year, d.month, d.day, 6, 30, tzinfo=TZ)
    return end - timedelta(days=1), end


def _in_window(date_rfc: str | None, win: tuple[datetime, datetime]) -> bool:
    if not date_rfc:
        return False
    try:
        dt = datetime.fromisoformat(date_rfc)
        return win[0] <= dt.astimezone(TZ) < win[1]
    except ValueError:
        return False


def _is_walled_img(url: str) -> bool:
    h = normalize.url_host(url)
    return any(h == w or h.endswith("." + w) or w in h for w in WALLED_IMG_HOSTS)


# ============================================================= load state ===

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta.atomic_write(path, data)


def load_cfg(path: str | Path | None = None) -> dict:
    """config.yaml > config.example.yaml；与 justfile cfg 规则一致。"""
    return meta.load_config(path)


def _cfg(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


def _default_proxy(cfg: dict) -> str:
    """全局默认代理解析：env(PIPELINE_PROXY/http_proxy/https_proxy/ALL_PROXY)
    → config proxy.http → ""（=不用代理直连；源 proxy:required 且无代理
    可解析时按 proxy_down 跳过）。
    本机 clash 127.0.0.1:7890 是示例值不是默认——开源环境无代理开箱即跑。"""
    for k in ("PIPELINE_PROXY", "http_proxy", "https_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return str(_cfg(cfg, "proxy.http", "") or "")


def load_sources(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for s in data or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        s.setdefault("method", "rss")
        s.setdefault("enabled", True)
        s.setdefault("proxy", "prefer")
        s.setdefault("tier", "A")
        s.setdefault("failover", []) or s.__setitem__("failover", [])
        s.setdefault("max_items_per_source", 40)
        s.setdefault("freshness_sla_h", 36)
        s.setdefault("daily", False)   # 每日快照源（pool stale-daily 死区用）
        out.append(s)
    return out


# =============================================================== preflight ==

def _clock_skew_s() -> float | None:
    """远端 Date 头对本地钟差（秒）；取不到 → None。"""
    import httpx
    for url in ("https://www.baidu.com/", "https://www.qq.com/"):
        try:
            t0 = time.time()
            r = httpx.get(url, timeout=8, follow_redirects=True)
            srv = parsedate_to_datetime(r.headers["date"]).timestamp()
            return round(abs((t0 + time.time()) / 2 - srv), 1)
        except Exception:
            continue
    return None


def preflight(cfg: dict, proxy_url: str | None) -> dict:
    """PLAN §5.4：clock/disk/tmp/net/proxy/gateway/playwright。"""
    checks: dict[str, object] = {}

    skew = _clock_skew_s()
    checks["clock_skew_s"] = skew
    checks["clock"] = "ok" if skew is not None and skew < 300 else (
        "fail" if skew is not None else "skipped")

    free_gb = shutil.disk_usage(REPO).free / (1 << 30)
    checks["disk_free_gb"] = round(free_gb, 2)
    checks["disk"] = "ok" if free_gb > 2 else "fail"

    try:
        du = shutil.disk_usage("/tmp")
        tmp_pct = du.used / du.total * 100
    except OSError:
        tmp_pct = -1
    checks["tmp_used_pct"] = round(tmp_pct, 1)
    checks["tmp"] = "ok" if 0 <= tmp_pct < 85 else ("fail" if tmp_pct >= 85 else "skipped")

    net_ok = False
    for u in ("https://www.baidu.com/", "https://www.qq.com/"):
        if lhttp.get(u, proxy="direct", timeout=8).ok:
            net_ok = True
            break
    checks["net_out"] = "ok" if net_ok else "fail"

    proxy_ok = False
    urls = list(_cfg(cfg, "proxy.required_check_urls", []) or [])
    urls += ["https://api.ipify.org", "https://api.github.com/"]
    if proxy_url:
        for u in urls[:3]:
            if lhttp.get(u, proxy=proxy_url, timeout=10).ok:
                proxy_ok = True
                break
    checks["proxy_ok"] = proxy_ok
    checks["proxy"] = "ok" if proxy_ok else ("fail" if proxy_url else "skipped")

    key_env = _cfg(cfg, "llm.api_key_env", "SWE2MAX_API_KEY")
    bg_env = _cfg(cfg, "llm.api_key_env_bg", "SWE2MAX_BG_API_KEY") or "SWE2MAX_BG_API_KEY"
    key = os.environ.get(str(bg_env)) or os.environ.get(str(key_env), "")
    base = _cfg(cfg, "llm.base_url", "")
    if base and key:
        import httpx
        try:
            r = httpx.get(
                base.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=8)
            checks["gateway"] = "ok" if r.status_code < 500 else f"http_{r.status_code}"
        except Exception as e:
            checks["gateway"] = f"fail:{type(e).__name__}"
    else:
        checks["gateway"] = "skipped" if base else "no_config"

    checks["playwright"] = ("ok" if importlib.util.find_spec("playwright")
                            else "skipped")
    checks["fails"] = [k for k, v in checks.items()
                       if isinstance(v, str) and v.startswith("fail")]
    return checks


# ============================================================= http layer ===

class Ctx:
    """一轮采集的共享上下文。"""

    def __init__(self, args, cfg, run_dir, seen, win):
        self.args = args
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.seen = seen
        self.win = win
        self.proxy_url = _default_proxy(cfg)
        self.proxy_ok = True          # preflight 回填
        self.content_budget = int(getattr(args, "max_content_fetches", 600))
        self.stats = []               # manifest.sources[]

    def proxy_arg(self, src: dict) -> str | None:
        """源级代理决策 → http.get 的 proxy 参数值。"""
        pol = src.get("proxy", "prefer")
        if pol == "direct_only":
            return "direct"
        if pol == "required":
            return self.proxy_url
        # prefer：代理活着走代理，否则直连
        return self.proxy_url if self.proxy_ok else "direct"

    def get(self, url: str, src: dict, **kw):
        return lhttp.get(url, proxy=self.proxy_arg(src), **kw)


# ============================================= 移出符号别名（lib 下沉） ======
# feed/json_api/diff 适配器 → stages/lib/sources/{feed,api,diff}.py；
# 文本/构造 helper → stages/lib/{normalize,http,rawitem}.py。别名让 collect
# 内部遗留调用与外部 `collect.X` 引用保持可用。

_utcnow = normalize.utcnow
_parse_date = normalize.parse_date_utc
_sha16 = normalize.sha16
_slug_title = normalize.slug_title
_bounded_text = normalize.bounded_text
_TRUNC_MARK = normalize.TRUNC_MARK
mk_item = rawitem.mk_item
_post_json = lhttp.post_json
_via = lhttp.fetch_via

parse_feed = _src_feed.parse_feed
_looks_like_feed = _src_feed.looks_like_feed
api_generic = _src_api.api_generic
API_ADAPTERS = _src_api.API_ADAPTERS
_SELF_FETCH_ADAPTERS = _src_api._SELF_FETCH_ADAPTERS
REQUEST_SPECS = _src_api.REQUEST_SPECS
collect_sitemap = _src_diff.collect_sitemap
collect_changelog = _src_diff.collect_changelog


def _validate_item(it: dict) -> dict | None:
    """契约校验 + 内容上限兜底；不合法条目丢 + 记日志（宁缺勿炸）。

    平台采集器（x/reddit/weibo）直造 raw dict 绕过 mk_item——截断/行分隔符
    归一在这里再兜一次，保证「无 >CAP 内容字段」是全路径不变量。
    content_html 已退役（契约字段保留但标 DEPRECATED）——这里单一收口
    pop 掉，绕过 mk_item 的 producer/旧缓存残留也漏不进产物。"""
    try:
        it = dict(it)
        it.pop("content_html", None)
        it["content_text"] = _bounded_text(it.get("content_text"),
                                           CONTENT_TEXT_CAP)
        d = RawItem.model_validate(it).model_dump(by_alias=True)
        d.pop("content_html", None)
        return d
    except Exception as e:
        log.warning("drop invalid item url=%s: %s",
                    (it.get("url") or "?")[:80], str(e)[:160])
        return None


# ==================================================== Tier-B 平台采集器 =====

class CollectorNotReady(RuntimeError):
    pass


def _platform_cfg(src: dict, ctx) -> dict:
    """平台采集器统一 cfg = 全局 config + 源字段 + 归一化传输键。

    'proxy' 键两边语义冲突（config.proxy={http:url} dict vs 源 proxy=策略串），
    统一落成三个无歧义键：
      proxy      = {"http": <传输>}   x_nitter._proxy_arg / x_synd top_proxy 读
      proxy_url  = "direct"|<url>     x_ssr._resolve_proxy 显式键 / reddit._opt
      proxy_mode = 源策略（直连时强制 direct_only，reddit._client 读它）
    """
    d = dict(ctx.cfg or {})
    for k, v in src.items():
        if k != "proxy":
            d[k] = v
    pa = ctx.proxy_arg(src)                    # "direct" | 代理 URL
    d["proxy"] = {"http": pa}
    d["proxy_url"] = pa
    d["proxy_mode"] = "direct_only" if pa == "direct" \
        else src.get("proxy", "prefer")
    d["src_proxy"] = src.get("proxy")
    d["run_dir"] = str(ctx.run_dir)
    d["source_name"] = src["name"]
    d["_source_name"] = src["name"]            # x_nitter._cfg_get 读此键
    return d


def _stamp_via(items, label: str):
    """命中路由落条目。契约注：_fetch.via 枚举只有 direct|mirror|cache|manual
    （RawFetch Literal），字面 'x:nitter' 会让 RawItem 校验失败被丢 —— 路由名
    改记 tags 'via:<label>'（沿用 reddit 'via:rss' 先例）+ manifest
    sources[].via（info['route']）。"""
    tag = f"via:{label}"
    for it in items:
        tags = it.setdefault("tags", [])
        if tag not in tags:
            tags.append(tag)
    return items


def _x_handle(src: dict) -> str:
    """feed_url 'https://x.com/OpenAI' → 'OpenAI'；failover nitter URL 同形。"""
    seg = [s for s in urlsplit(src.get("feed_url") or "").path.split("/") if s]
    h = seg[0].lstrip("@") if seg else ""
    return h if re.fullmatch(r"[A-Za-z0-9_]{1,20}", h) else ""


def collect_x(src: dict, ctx) -> tuple[list[dict], dict]:
    """X 四路级联（§5.3）：x:nitter → x:ssr → x:synd → x:paid(D12)。

    每路独立 try/except，失败记 attempts[] 落下一 路；import 全在 try 内，
    缺模块不炸 Tier A。全灭 → CollectorNotReady（调用方标 skipped）。"""
    handle = _x_handle(src)
    if not handle:
        raise CollectorNotReady(f"no X handle in feed_url={src['feed_url']!r}")
    pcfg = _platform_cfg(src, ctx)
    attempts: list[dict] = []

    def rec(route: str, err):
        attempts.append({"route": route, "error": str(err)[:200]})
        log.info("%s %s failed: %s", src["name"], route, str(err)[:160])

    # ① nitter 实例池（健康分轮换）
    try:
        from stages.lib import x_nitter
        diag: dict = {}
        got = x_nitter.fetch_user(handle, pcfg, diag=diag,
                                  run_dir=ctx.run_dir)
        if got:
            return _stamp_via(list(got), "x:nitter"), {
                "route": "x:nitter", "attempts": attempts,
                "instance": (diag or {}).get("via")}
        rec("x:nitter", "empty")
    except Exception as e:
        rec("x:nitter", f"{type(e).__name__}: {e}")

    # ② x.com 登出态 SSR（shell-only 检测在模块内）
    try:
        from stages.lib import x_ssr
        got = x_ssr.fetch_user(handle, pcfg)
        if got:
            return _stamp_via(list(got), "x:ssr"), \
                {"route": "x:ssr", "attempts": attempts}
        rec("x:ssr", "empty")
    except Exception as e:
        rec("x:ssr", f"{type(e).__name__}: {e}")

    # ③ cdn.syndication.twimg.com（429 指数退避在模块内）
    try:
        from stages.lib import x_synd
        got = x_synd.fetch_user(handle, pcfg, run_dir=ctx.run_dir,
                                source_name=src["name"])
        if got:
            return _stamp_via(list(got), "x:synd"), \
                {"route": "x:synd", "attempts": attempts}
        rec("x:synd", "empty")
    except Exception as e:
        rec("x:synd", f"{type(e).__name__}: {e}")

    # ④ 付费 adapter（D12：enabled:false 占位；NotConfigured 也记 attempts）
    try:
        from adapters import x_paid
        got = x_paid.fetch_user(handle, pcfg)
        if got:
            return _stamp_via(list(got), "x:paid"), \
                {"route": "x:paid", "attempts": attempts}
        rec("x:paid", "empty")
    except Exception as e:
        rec("x:paid", f"{type(e).__name__}: {e}")

    raise CollectorNotReady("; ".join(
        f"{a['route']}:{a.get('error')}" for a in attempts))


def collect_reddit(src: dict, ctx) -> tuple[list[dict], dict]:
    """reddit → lib.reddit_collect.fetch_sub（loid OAuth 主路 + 匿名 .rss
    兜底已在模块内）。失败/缺模块 → CollectorNotReady。"""
    sub = _sub_of(src)
    if not sub:
        raise CollectorNotReady(f"no subreddit in feed_url={src['feed_url']!r}")
    try:
        from stages.lib import reddit_collect
    except Exception as e:
        raise CollectorNotReady(f"reddit_collect import: {type(e).__name__}")
    try:
        got = reddit_collect.fetch_sub(sub, _platform_cfg(src, ctx))
    except Exception as e:
        raise CollectorNotReady(f"fetch_sub: {type(e).__name__}: {e}"[:200])
    if not got:
        raise CollectorNotReady("fetch_sub empty")
    # .rss 兜底路命中的条目自带 'via:rss' 标签 → 区分真实路由
    route = "reddit:rss" if any("via:rss" in (it.get("tags") or [])
                                for it in got) else "reddit:loid"
    return _stamp_via(list(got), route), {"route": route}


def collect_weibo(src: dict, ctx) -> tuple[list[dict], dict]:
    """weibo → lib.weibo_collect：uid 源走 fetch_uid（§5.3 指定入口）；
    hot_band/containerid 源由 fetch_source 按 feed_url 形状路由。
    m.weibo.cn 为 CN 直连站，proxy=direct。"""
    try:
        from stages.lib import weibo_collect
    except Exception as e:
        raise CollectorNotReady(f"weibo_collect import: {type(e).__name__}")
    wcfg = _platform_cfg(src, ctx)
    wcfg["proxy"] = "direct" if src.get("proxy") == "direct_only" \
        else wcfg["proxy_url"]
    wcfg["max_items"] = src.get("max_items_per_source", 30)
    wcfg["_health"] = health = {}
    uid = _uid_of(src)
    try:
        if uid:
            got = weibo_collect.fetch_uid(uid, wcfg)
            route = "weibo:uid"
        else:
            got = weibo_collect.fetch_source(src, wcfg)
            route = "weibo:source"
    except Exception as e:
        raise CollectorNotReady(f"{type(e).__name__}: {e}"[:200])
    if not got:
        err = health.get("last_error") or health.get("status") or "empty"
        raise CollectorNotReady(f"{route}: {err}")
    return _stamp_via(list(got), route), {"route": route, "health": health}


def _platform_fallback(src: dict) -> str | None:
    """采集器缺位时的公共兜底路由。"""
    m, u = src["method"], src["feed_url"]
    if m == "reddit" and re.search(r"\.rss(\?|$)", u):
        return "rss_fallback"          # 官方 .rss 代理直连兜底
    if m == "weibo" and "weibo" in u:
        return "json_fallback"         # m.weibo.cn JSON 端点通用 walker
    return None


def _sub_of(src: dict) -> str | None:
    m = re.search(r"/r/([A-Za-z0-9_]+)", src["feed_url"])
    return m.group(1) if m else None


def _uid_of(src: dict) -> str | None:
    m = re.search(r"value=(\d+)", src["feed_url"]) or \
        re.search(r"/u/(\d+)", src["feed_url"])
    return m.group(1) if m else None


# ========================================================== 正文/媒体 pass ==

def content_pass(item: dict, src: dict, ctx) -> None:
    """content_text<200 或 signal → trafilatura 抓正文回填（原地改 item）。"""
    text = item.get("content_text") or ""
    is_signal = "signal" in (item.get("tags") or [])
    if not is_signal and len(text) >= CONTENT_MIN:
        return
    if ctx.content_budget <= 0:
        return
    ctx.content_budget -= 1
    try:
        import trafilatura
    except ImportError:
        log.warning("trafilatura missing — content pass disabled")
        ctx.content_budget = 0
        return
    r = ctx.get(item["url"], src, timeout=15, retries=0)
    if not r.ok:
        item["_fetch"]["reachable"] = False
        return
    body = r.body or b""
    try:
        out = trafilatura.extract(
            body, output_format="json", with_metadata=True,
            favor_precision=False, include_comments=False)
        meta_d = json.loads(out) if out else {}
    except Exception as e:
        log.debug("trafilatura fail %s: %s", item["url"][:70], e)
        meta_d = {}
    ext_text = (meta_d.get("text") or meta_d.get("raw_text") or "").strip()
    if ext_text and len(ext_text) > len(text):
        item["content_text"] = _bounded_text(ext_text, CONTENT_TEXT_CAP)
    if meta_d.get("title") and (
            is_signal or not item.get("title") or
            item["title"] == _slug_title(item["url"])):
        item["title"] = normalize.title_norm(meta_d["title"])
    if not item.get("date_published") and meta_d.get("date"):
        # trafilatura 在 JS 壳页会拿版权年/构建戳编日期——只信提取到
        # 达标正文的页，且拒未来日期（date-only 精度给 +2d 宽限）
        d = _parse_date(meta_d["date"])
        if d and len(ext_text) >= 500 and datetime.fromisoformat(d) \
                <= datetime.now(timezone.utc) + timedelta(days=2):
            item["date_published"] = d
    if not item.get("image") and meta_d.get("image"):
        item["image"] = meta_d["image"]
    if not item.get("language") and meta_d.get("language"):
        item["language"] = meta_d["language"]
    try:
        item["_raw_ref"] = lhttp.save_raw(
            ctx.run_dir, src["name"], item["url"], body)
    except Exception:
        pass


_IMG_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg"}


def media_pass(item: dict, src: dict, ctx) -> None:
    """防盗链图床 → 下载到 run_dir/media/，image 改为 run 相对路径。"""
    img = item.get("image")
    if not img or not img.startswith("http") or not _is_walled_img(img):
        return
    media_dir = ctx.run_dir / "media"
    r = ctx.get(img, src, timeout=15,
                headers={"Referer": item.get("url") or src["feed_url"]})
    if not r.ok or not r.body:
        item.setdefault("tags", []).append("img_download_failed")
        return
    ctype = (r.content_type or "").split(";")[0].strip().lower()
    ext = _IMG_EXT.get(ctype)
    if ext is None:
        head = r.body[:12]
        if head.startswith(b"\xff\xd8"):
            ext = ".jpg"
        elif head.startswith(b"\x89PNG"):
            ext = ".png"
        elif head.startswith(b"GIF8"):
            ext = ".gif"
        elif head.startswith(b"RIFF") and b"WEBP" in r.body[:16]:
            ext = ".webp"
        else:
            return                          # 不是图 → 留原 URL
    media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_sha16(img)}{ext}"
    try:
        meta.atomic_write(media_dir / name, r.body)   # tmp+replace，免半截文件
        item["image"] = f"media/{name}"
    except OSError as e:
        log.debug("media write fail: %s", e)


# ============================================================ per-source ====

def _finish_items(partials: list[dict], src: dict, ctx,
                  res: lhttp.FetchResult, raw_ref: str | None,
                  kind: str) -> list[dict]:
    """adapter partial → contract dict（mk_item）。"""
    sha = _sha16(res.body or b"")
    out = []
    for p in partials:
        if not p.get("url"):
            continue
        it = mk_item(
            url=p["url"], title=p.get("title") or "", src=src, kind=kind,
            date=p.get("date"), summary=p.get("summary"),
            image=p.get("image"),
            tags=p.get("tags") or [], guid=p.get("guid"),
            fetch_status=res.status, via=_via(res), etag=res.etag,
            content_sha=sha, raw_ref=raw_ref)
        if p.get("signal"):
            if "signal" not in it["tags"]:
                it["tags"].append("signal")
            it["date_published"] = None
        out.append(it)
    return out


_URL_TPL = re.compile(r"\{T(?:([+-]\d+)d)?(_ts)?\}")


def _render_url(url, win) -> str:
    """feed_url/failover 的相对日期占位符：{T±Nd}→%Y-%m-%d、
    {T±Nd_ts}→epoch 秒。锚点 T=win[1]（窗口右沿），故 {T-1d}≈窗口左沿。
    例 github_search `created:>{T-2d}`、hn_algolia `created_at_i>{T-1d_ts}`。"""
    if not isinstance(url, str) or "{" not in url:
        return url
    def _sub(m):
        t = win[1] + timedelta(days=int(m.group(1) or 0))
        return str(int(t.timestamp())) if m.group(2) else t.strftime("%Y-%m-%d")
    return _URL_TPL.sub(_sub, url)


def fetch_with_failover(src: dict, ctx, spec: dict | None):
    """feed_url + failover 链 → (res, url_used)。validators 按 URL 记；
    POST spec 只作用于主 URL，failover 一律 GET。"""
    urls = [_render_url(u, ctx.win) for u in
            [src["feed_url"], *(x for x in (src.get("failover") or []) if x)]]
    validators = ctx.seen.get(src["name"], {}).get("validators") or {}
    last = lhttp.FetchResult(error="no_url")
    for i, u in enumerate(urls):
        if not u or not u.startswith("http"):
            continue
        v = validators.get(u) or {}
        if i == 0 and spec and spec.get("method") == "POST":
            payload = dict(spec.get("json") or {})
            if "variables" in payload and \
                    payload["variables"].get("since") is None:
                payload["variables"]["since"] = \
                    ctx.win[0].strftime("%Y-%m-%d")
            res = _post_json(u, payload, spec.get("headers") or {},
                             ctx.proxy_arg(src))
        else:
            res = ctx.get(u, src, etag=v.get("etag"),
                          lastmod=v.get("lastmod"), timeout=20, retries=1,
                          headers=spec.get("headers") if spec else None)
        if res.not_modified or res.ok:
            return res, u
        last = res
        log.info("%s: %s -> %s, try failover", src["name"], u[:70], res.error)
        # walled/rate_limited 多为秒级挑战窗口（CF burst limit）——立刻打下一条
        # 只是陪跑；给个小间隔再换。可用 failover_delay_s 逐源覆盖（默认 6s）。
        if i < len(urls) - 1 and res.error in ("walled", "rate_limited"):
            time.sleep(float(src.get("failover_delay_s") or 6))
    return last, urls[0]


def _update_validators(src: dict, ctx, url_used: str, res: lhttp.FetchResult):
    if res.etag or res.lastmod:
        ent = ctx.seen.setdefault(src["name"], {}).setdefault("validators", {})
        ent[url_used] = {"etag": res.etag, "lastmod": res.lastmod}


def collect_source(src: dict, ctx) -> dict:
    """单源全流程 → manifest.sources[] 条目；items 挂 stat['items']。"""
    name = src["name"]
    method = src["method"]
    stat = {"name": name, "method": method, "tier": src.get("tier", "A"),
            "status": ST_EMPTY, "items_new": 0, "items_fresh": 0,
            "items_total": 0, "last_error": None, "latency_ms": 0,
            "via": None, "endpoint": None, "items": []}
    if method not in _METHODS:
        stat["status"], stat["last_error"] = ST_SKIPPED, "unknown_method"
        return stat
    if src.get("proxy") == "required" and not ctx.proxy_ok:
        stat["status"], stat["last_error"] = ST_SKIPPED, "proxy_down"
        return stat

    # ---------- Tier-B 平台采集器（惰性 import）----------
    if method == "wechat":
        # D3 后置：骨架保留，恒记 skipped(disabled)，不走采集器
        log.info("%s: wechat collector disabled (D3)", name)
        stat["status"], stat["last_error"] = ST_SKIPPED, "disabled"
        return stat
    if method in ("x", "reddit", "weibo"):
        items, info = None, {}
        try:
            if method == "x":
                items, info = collect_x(src, ctx)
            elif method == "reddit":
                items, info = collect_reddit(src, ctx)
            else:
                items, info = collect_weibo(src, ctx)
        except CollectorNotReady as e:
            log.info("%s collector not ready: %s", name, str(e)[:200])
        except Exception as e:
            stat["status"], stat["last_error"] = \
                "parse_error", f"{type(e).__name__}: {e}"[:200]
            return stat
        if items:
            stat["via"] = (info or {}).get("route")
            stat["endpoint"] = src["feed_url"]
            valid = [v for v in (_validate_item(i) for i in items) if v]
            stat["items"] = valid[:src["max_items_per_source"]]
            stat["items_total"] = len(stat["items"])
            stat["status"] = ST_OK if stat["items"] else ST_EMPTY
            _mark_new_fresh(stat, src, ctx)
            return stat
        # 采集器缺位/失败 → 公共兜底路径（_feed/_json_path 会记真实错误）
        fb = (info or {}).get("route") or _platform_fallback(src)
        got = None
        if fb == "rss_fallback":
            got = _feed_path(src, ctx, stat)
        elif fb == "json_fallback":
            got = _json_path(src, ctx, stat)
        if got is not None:
            stat["via"] = fb
            return _stat_items(stat, src, ctx, got)
        if stat["status"] == ST_EMPTY:      # 无兜底可跑 → 采集器缺位
            stat["status"], stat["last_error"] = \
                ST_SKIPPED, "collector_not_ready"
        return stat

    if method == "manual":
        stat["status"], stat["last_error"] = ST_SKIPPED, "manual_entry"
        return stat

    # ---------- Tier-A：HTTP 取包 → 分派解析 ----------
    spec = REQUEST_SPECS.get(name) if method == "json_api" else None
    res, url_used = fetch_with_failover(src, ctx, spec)
    stat["latency_ms"] = res.latency_ms
    stat["endpoint"] = url_used
    stat["via"] = res.via
    _update_validators(src, ctx, url_used, res)
    ent = ctx.seen.setdefault(name, {})
    if res.not_modified:
        stat["status"] = ST_OK
        stat["items_new"] = 0
        ent["last_status"] = "not_modified"
        return stat
    if not res.ok:
        stat["status"] = res.error if res.error != "ok" else f"http_{res.status}"
        stat["last_error"] = (res.detail or res.error)[:200]
        ent["last_status"] = stat["status"]
        return stat

    body = res.body or b""
    try:
        raw_ref = lhttp.save_raw(ctx.run_dir, name, url_used, body)
    except Exception as e:
        raw_ref = None
        log.debug("save_raw fail %s: %s", name, e)

    items: list[dict] = []
    is_feed_body = _looks_like_feed(body)

    try:
        if method in ("rss", "atom", "youtube_rss") or is_feed_body:
            items = parse_feed(body, src, res, raw_ref)
            if not items and not is_feed_body:
                items = _json_rescue(body, src, ctx, res, raw_ref, stat)
            if not items and not is_feed_body:
                stat["status"] = "parse_error"
        elif method == "json_api":
            items = _json_items(src, ctx, body, res, raw_ref, stat)
        elif method == "sitemap_diff":
            items = collect_sitemap(src, ctx, body, res, raw_ref)
        elif method == "changelog_diff":
            items = collect_changelog(src, ctx, body, res, raw_ref)
    except ET.ParseError:
        stat["status"] = "parse_error"
    except Exception as e:
        stat["status"] = "parse_error"
        stat["last_error"] = f"{type(e).__name__}: {e}"[:200]

    return _stat_items(stat, src, ctx, items)


def _json_rescue(body, src, ctx, res, raw_ref, stat=None) -> list[dict]:
    """feed 路径拿到非 XML 时的兜底：JSON → 通用 walker。
    覆盖 failover 指到 JSON API 的源（ifanr sso web-feed objects[]，
    2026-09-23 实测主 feed 被 wall 误判后 failover 必然 parse_error）。"""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        if stat is not None:
            stat["last_error"] = "rescue: body not JSON"
        return []
    try:
        partials, _ = api_generic(data, src, ctx)
    except Exception as e:
        if stat is not None:
            stat["last_error"] = f"rescue generic: {e}"[:200]
        return []
    items = _finish_items(partials, src, ctx, res, raw_ref, kind="api")
    if not items and stat is not None:
        stat["last_error"] = f"rescue: 0/{len(partials)} items after finish"
    return items


def _json_items(src, ctx, body, res, raw_ref, stat) -> list[dict]:
    """json_api：具名 adapter → 通用 walker → HTML 降级 changelog diff。"""
    name = src["name"]
    fn = API_ADAPTERS.get(name)
    # 自抓型 adapter（xiaoyuzhou/trust_anthropic）不吃 feed body，
    # 必须在 json 解析/HTML 降级之前分派（它们的首包常是 HTML 壳）。
    if fn in _SELF_FETCH_ADAPTERS:
        try:
            partials, _ = fn(src, ctx)
        except Exception as e:
            stat["status"] = "parse_error"
            stat["last_error"] = f"{type(e).__name__}: {e}"[:200]
            return []
        return _finish_items(partials, src, ctx, res, raw_ref, kind="api")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        text = body.decode("utf-8", "replace")
        if "<html" in text[:4096].lower():
            return collect_changelog(src, ctx, body, res, raw_ref)
        stat["status"] = "parse_error"
        stat["last_error"] = "body not JSON"
        return []
    try:
        if fn:
            partials, _ = fn(data, src, ctx)
        else:
            partials, _ = api_generic(data, src, ctx)
    except Exception as e:
        log.info("adapter %s failed (%s) -> generic", name, e)
        try:
            partials, _ = api_generic(data, src, ctx)
        except Exception as e2:
            stat["status"] = "parse_error"
            stat["last_error"] = str(e2)[:200]
            return []
    return _finish_items(partials, src, ctx, res, raw_ref, kind="api")


def _json_path(src, ctx, stat):
    """weibo 兜底：GET JSON → 通用 walker。失败时 stat 记真实错误。"""
    res, url_used = fetch_with_failover(src, ctx, None)
    stat["latency_ms"] = res.latency_ms
    stat["endpoint"] = url_used
    if not res.ok:
        stat["status"], stat["last_error"] = res.error, \
            (res.detail or res.error)[:200]
        return None
    try:
        data = json.loads((res.body or b"").decode("utf-8", "replace"))
        partials, _ = api_generic(data, src, ctx)
        if not partials:
            stat["status"], stat["last_error"] = "empty", "generic walker 0 items"
            return None
        return _finish_items(partials, src, ctx, res, None, kind="api")
    except Exception as e:
        stat["status"], stat["last_error"] = "parse_error", str(e)[:200]
        return None


def _feed_path(src, ctx, stat):
    """reddit 兜底：feed_url 本身就是 .rss → feedparser。"""
    res, url_used = fetch_with_failover(src, ctx, None)
    stat["latency_ms"] = res.latency_ms
    stat["endpoint"] = url_used
    if not res.ok:
        stat["status"], stat["last_error"] = res.error, \
            (res.detail or res.error)[:200]
        return None
    return parse_feed(res.body or b"", src, res, None)


def _stat_items(stat, src, ctx, items):
    """校验 + cap + 新旧标记 + signal 过滤 → stat 落盘。"""
    valid = [v for v in (_validate_item(i) for i in items) if v]
    seen_urls = set(ctx.seen.get(src["name"], {}).get("urls") or [])

    def tags_of(it):
        return it.get("tags") or []

    normal = [it for it in valid if "signal" not in tags_of(it)]
    signals = [it for it in valid if "signal" in tags_of(it)]
    # signal 只发新；page_sig 页级变更豁免（URL 不变也要年年发）
    new_signals = [it for it in signals
                   if it["url_canon"] not in seen_urls
                   or "page_sig" in tags_of(it)]
    cap = src["max_items_per_source"]
    emitted = (normal + new_signals)[:cap]
    stat["items"] = emitted
    stat["items_total"] = len(emitted)
    if stat["status"] == ST_EMPTY and emitted:
        stat["status"] = ST_OK
    _mark_new_fresh(stat, src, ctx)
    return stat


def _mark_new_fresh(stat, src, ctx):
    seen_urls = set(ctx.seen.get(src["name"], {}).get("urls") or [])
    new = fresh = 0
    for it in stat["items"]:
        if it["url_canon"] not in seen_urls:
            new += 1
        if "signal" not in (it.get("tags") or []) and \
                _in_window(it.get("date_published"), ctx.win):
            fresh += 1
    stat["items_new"] = new
    stat["items_fresh"] = fresh


# ============================================================== manual =====

def manual_item(url: str, title: str | None, ctx) -> dict:
    src = {"name": "manual", "feed_url": url, "proxy": "prefer",
           "max_items_per_source": 1}
    r = ctx.get(url, src, timeout=20, retries=1)
    it = mk_item(url=url, title=title or "", src=src, kind="manual",
                 fetch_status=r.status if r.status else 0, via="manual",
                 etag=r.etag, content_sha=_sha16(r.body or b""),
                 reachable=r.ok)
    if r.ok:
        try:
            import trafilatura
            out = trafilatura.extract(r.body, output_format="json",
                                      with_metadata=True)
            d = json.loads(out) if out else {}
            if d.get("text"):
                it["content_text"] = _bounded_text(d["text"], CONTENT_TEXT_CAP)
            if not title and d.get("title"):
                it["title"] = normalize.title_norm(d["title"])
            if d.get("date"):
                it["date_published"] = _parse_date(d["date"])
            if d.get("image"):
                it["image"] = d["image"]
            it["_raw_ref"] = lhttp.save_raw(ctx.run_dir, "manual", url, r.body)
        except Exception:
            pass
    if not it["title"]:
        it["title"] = _slug_title(url)
    media_pass(it, src, ctx)
    return it


# ============================================================== driver ======

def _ntfy(cfg, title, msg, priority="default", tags=None):
    try:
        from adapters import alert_ntfy
        return alert_ntfy.push(title, msg, priority=priority,
                               tags=tags or [], config=cfg)
    except Exception:
        return False


def _write_outputs(run_dir: Path, items: list[dict], manifest: dict):
    # dumps_jsonl：整写 + 转义 NEL/U+2028/U+2029——产物里唯一行分隔符是 \n，
    # 任何按行读取器（含 str.splitlines）都不会在字符串内断行；再经
    # atomic_write tmp+os.replace 落盘，读者永不碰到写了一半的文件。
    meta.atomic_write(run_dir / ITEMS_NAME, meta.dumps_jsonl(items))
    RawManifest.model_validate(manifest)            # 契约 lint，错则炸
    meta.atomic_write(run_dir / MANIFEST_NAME, manifest)


def _update_health(health: dict, stat: dict, run_date: str) -> None:
    h = health.setdefault(stat["name"], {})
    st = stat["status"]
    if st == ST_SKIPPED:
        pass                                       # 代理挂/采集器缺位不算源死
    elif st in (ST_OK,):
        h["consecutive_fails"] = 0
        h["alerted"] = False
        h["last_ok_date"] = run_date
    else:
        h["consecutive_fails"] = int(h.get("consecutive_fails", 0)) + 1
    h.update({"last_status": st, "last_error": stat.get("last_error"),
              "last_latency_ms": stat.get("latency_ms"),
              "updated_at": _utcnow()})


def _pool_upsert(args, cfg: dict, run_dir: Path, run_date: str,
                 items: list[dict], sources: list[dict]) -> None:
    """采批 → 跨期条目池 state/items.sqlite（lib/pool.py）。

    仅真·日期目录（runs/YYYY-MM-DD）且日期 ≤ 今日(Asia/Shanghai) 才写池——
    _doctor/手工目录与未来日期不污染。池写失败只记 warning，绝不炸 collect。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_dir.name) \
            or run_dir.name > _today_sh():
        return
    try:
        conn = pool.init_db(pool.resolve_path(
            getattr(args, "items_db", None), cfg))
        try:
            st = pool.upsert_items(
                conn, items, run_date,
                {s["name"]: bool(s.get("daily")) for s in sources})
        finally:
            conn.close()
        log.info("pool_upserted episode=%s %s", run_date,
                 json.dumps(st, ensure_ascii=False))
    except Exception as e:
        log.warning("pool upsert failed (non-fatal): %s: %s",
                    type(e).__name__, e)


def run(args) -> int:
    cfg = load_cfg(args.config)
    rd_arg = str(args.run_dir) if args.run_dir else _today_sh()
    run_dir = meta.ensure_run(rd_arg if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd_arg)
                              else Path(rd_arg))
    run_date = run_dir.name if re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_dir.name) \
        else _today_sh()
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(run_dir / "logs" / "collect.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)

    sources = load_sources(Path(args.sources))
    sources = [s for s in sources if s.get("enabled", True)]
    if args.only:
        want = set(args.only.split(","))
        sources = [s for s in sources if s["name"] in want]
    if args.max_sources:
        sources = sources[: args.max_sources]

    with meta.run_lock(run_dir):
        seen = _load_json(SEEN_PATH, {})
        health = _load_json(HEALTH_PATH, {})
        win = _window(run_date)
        ctx = Ctx(args, cfg, run_dir, seen, win)

        # ---------------- manual ----------------
        if args.manual:
            it = manual_item(args.manual, args.title, ctx)
            v = _validate_item(it)
            if not v:
                print("manual fetch produced invalid item", file=sys.stderr)
                return 2
            old = []
            p = run_dir / ITEMS_NAME
            if p.is_file():
                old = list(meta.iter_jsonl(p))
            old = [o for o in old if o.get("item_key") != v["item_key"]] + [v]
            _write_outputs(run_dir, old, _manifest(run_date, win, old, ctx,
                                                 note="manual"))
            _pool_upsert(args, cfg, run_dir, run_date, old, sources)
            print(json.dumps(v, ensure_ascii=False, indent=1)[:2000])
            return 0

        # 运行态登记：manual 轻量分支已在上方 return（不走 stage_done，
        # 登记会留假墓碑）；此处起才是完整采集流程，stage_done 自动清除。
        meta.stage_begin(run_dir)

        # ---------------- preflight ----------------
        pre = preflight(cfg, ctx.proxy_url)
        ctx.proxy_ok = bool(pre.get("proxy_ok"))
        degraded = not ctx.proxy_ok
        log.info("preflight: %s", json.dumps(pre, ensure_ascii=False))

        # ---------------- per-source ----------------
        all_items: list[dict] = []
        emitted_canons: dict[str, list[str]] = {}
        pg = prog.Prog(run_dir, "collect", total=len(sources),
                       step=5, interval=30)
        budget_noted = False
        for i, src in enumerate(sources, 1):
            t0 = time.monotonic()
            try:
                stat = collect_source(src, ctx)
            except Exception as e:
                stat = {"name": src["name"], "method": src.get("method"),
                        "status": "parse_error",
                        "last_error": f"unhandled:{type(e).__name__}:{e}"[:200],
                        "items_new": 0, "items_fresh": 0, "items_total": 0,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "items": []}
            for it in stat.get("items") or []:
                if "signal" in (it.get("tags") or []) or \
                        len(it.get("content_text") or "") < CONTENT_MIN:
                    content_pass(it, src, ctx)
                media_pass(it, src, ctx)
            if not budget_noted and ctx.content_budget <= 0:
                budget_noted = True
                pg.say(f"content budget exhausted at {src['name']} "
                       f"({i}/{len(sources)})")
            emitted_canons[src["name"]] = [it["url_canon"]
                                           for it in stat.get("items") or []]
            all_items.extend(stat.get("items") or [])
            stat.pop("items", None)
            _update_health(health, stat, run_date)
            ctx.stats.append(stat)
            log.info("%-28s %-14s %-12s new=%-3d fresh=%-3d %sms %s",
                     stat["name"], stat.get("method"), stat["status"],
                     stat.get("items_new", 0), stat.get("items_fresh", 0),
                     stat.get("latency_ms", "-"), stat.get("last_error") or "")
            pg.tick(i, f"{stat['name']} {stat['status']}")
        pg.close()

        # item_key 全局唯一（机械身份）
        deduped, seen_keys = [], set()
        for it in all_items:
            if it["item_key"] in seen_keys:
                continue
            seen_keys.add(it["item_key"])
            deduped.append(it)

        # ---------------- 状态落盘 ----------------
        for src in sources:
            ent = seen.setdefault(src["name"], {})
            ent["last_status"] = next(
                (s["status"] for s in ctx.stats if s["name"] == src["name"]),
                "skipped")
            ent["last_ok"] = _utcnow() if ent["last_status"] == ST_OK \
                else ent.get("last_ok")
            # seen.urls 合并：diff 源的 round_urls 全集 > 本批 emitted canons
            round_urls = ent.pop("round_urls", None)
            merged_in = round_urls if round_urls is not None \
                else emitted_canons.get(src["name"], [])
            if merged_in:
                ent["urls"] = list(dict.fromkeys(
                    list(merged_in) +
                    [u for u in (ent.get("urls") or []) if u]))[:SEEN_URL_CAP]
        _save_json(SEEN_PATH, seen)
        _save_json(HEALTH_PATH, health)

        # ---------------- manifest + jsonl ----------------
        manifest = _manifest(run_date, win, deduped, ctx,
                             preflight=pre, degraded=degraded)
        _write_outputs(run_dir, deduped, manifest)
        _pool_upsert(args, cfg, run_dir, run_date, deduped, sources)
        meta.stage_done(run_dir, "collect", ITEMS_NAME, status="done",
                        extra={"n_items": len(deduped)})

        # ---------------- 告警 ----------------
        n_ok = sum(1 for s in ctx.stats if s["status"] == ST_OK)
        bad_sources = [n for n, h in health.items()
                       if h.get("consecutive_fails", 0) >= 3
                       and not h.get("alerted")]
        if not args.selftest:
            if n_ok == 0 and sources:
                _ntfy(cfg, "采集全灭",
                      f"{run_date}: 0/{len(sources)} 源成功",
                      priority="urgent", tags=["rotating_light"])
            elif degraded:
                _ntfy(cfg, "采集降级: proxy 不可达",
                      f"{run_date}: {n_ok}/{len(sources)} 源成功, "
                      "proxy:required 源已跳过", tags=["warning"])
            if bad_sources:
                for n in bad_sources:
                    health[n]["alerted"] = True
                _save_json(HEALTH_PATH, health)
                _ntfy(cfg, "源连败告警",
                      f"连续失败≥3: {', '.join(bad_sources[:8])}",
                      tags=["warning"])

        print(f"[collect] sources ok={n_ok}/{len(sources)} "
              f"items={len(deduped)} degraded={degraded} -> {run_dir}",
              file=sys.stderr)
        return 0 if n_ok or not sources else 1


def _items_stats(items: list[dict]) -> dict:
    """产物体积簿记 → manifest["stats"]：行字节按落盘序列化（dumps_jsonl_row）
    实测，截断数按 _TRUNC_MARK 留痕识别（启发式，理论误报≈0）。"""
    n_text = n_text_trunc = 0
    max_text = max_line = total = 0
    for it in items:
        t = it.get("content_text") or ""
        if t:
            n_text += 1
            max_text = max(max_text, len(t))
            if t.endswith(_TRUNC_MARK):
                n_text_trunc += 1
        b = len((meta.dumps_jsonl_row(it) + "\n").encode("utf-8"))
        max_line, total = max(max_line, b), total + b
    return {
        "jsonl_bytes": total,
        "max_line_bytes": max_line,
        "content_text": {"cap_chars": CONTENT_TEXT_CAP, "n_present": n_text,
                         "n_truncated": n_text_trunc, "max_chars": max_text},
    }


def _manifest(run_date, win, items, ctx, preflight=None, degraded=False,
              note=None) -> dict:
    sources_stats = []
    for s in ctx.stats:
        d = {k: v for k, v in s.items() if k != "items"}
        sources_stats.append(d)
    return {
        "schema": "raw_manifest/1",
        "episode": run_date,
        "window": {
            "from": win[0].isoformat(timespec="seconds"),
            "to": win[1].isoformat(timespec="seconds"),
            "tz": "Asia/Shanghai",
            "proxy_ok": ctx.proxy_ok,
            "degraded": degraded,
            "preflight": preflight or {},
            **({"note": note} if note else {}),
        },
        "file": ITEMS_NAME,
        "n_items": len(items),
        "sources": sources_stats,
        "produced_at": _utcnow(),
        "stats": _items_stats(items),
    }


# ============================================================== selftest ====

_SELFTEST_PICKS = [
    # linux.do 对 httpx 做 JA3/CF 挑战（curl 可过）——自测主选 CN 直连 RSS
    ("rss", ["ithome", "ifanr", "leiphone", "openai_news", "linuxdo"]),
    ("json_api", ["hn_algolia", "jiqizhixin", "lobsters"]),
    ("sitemap_diff", ["claude_sitemap", "anthropic_sitemap"]),
]


def selftest(args) -> int:
    """PLAN §3.6：rss/json_api/sitemap 三个代表源 cond GET + 契约校验。"""
    # _render_url 纯函数断言：{T±Nd}→日期、{T±Nd_ts}→epoch，锚=win[1]
    _w = (datetime(2026, 9, 22, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
          datetime(2026, 9, 23, 6, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert _render_url("https://x/?q=created:%3E{T-2d}", _w) == \
        "https://x/?q=created:%3E2026-09-21"
    assert _render_url("https://x/?f=created_at_i>{T-1d_ts}", _w) == \
        f"https://x/?f=created_at_i>{int(_w[0].timestamp())}"  # =窗口左沿
    assert _render_url("https://x/plain", _w) == "https://x/plain"
    assert _render_url(None, _w) is None
    all_srcs = load_sources(Path(args.sources))
    picks = []
    for method, pref in _SELFTEST_PICKS:
        chosen = None
        for n in pref:
            cand = [s for s in all_srcs
                    if s["name"] == n and s.get("enabled", True)]
            if cand:
                chosen = cand[0]
                break
        if chosen is None:
            cand = [s for s in all_srcs
                    if s["method"] == method and s.get("enabled", True)]
            if cand:
                chosen = cand[0]
        if chosen:
            chosen = dict(chosen)
            chosen["max_items_per_source"] = min(
                chosen.get("max_items_per_source", 40), 10)
            picks.append(chosen)
    if len(picks) < 3:
        print("SELFTEST FAIL: <3 representative sources in sources.yaml")
        return 1
    print("selftest sources:", [s["name"] for s in picks])

    args.max_sources = None
    args.only = None
    args.max_content_fetches = min(getattr(args, "max_content_fetches", 600), 12)
    args.selftest = True

    # 借真 run-dir 但源集只有 3 个 —— 产物合法且不污染状态语义
    run_dir = Path(args.run_dir) if args.run_dir else \
        REPO / "runs" / "_doctor" / "collect"
    run_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_cfg(args.config)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    win = _window(_today_sh())
    # selftest 用独立 seen（不写真 state）—— clone 内存态，落盘到 run_dir
    seen = _load_json(SEEN_PATH, {})
    ctx = Ctx(args, cfg, run_dir, seen, win)
    pre = preflight(cfg, ctx.proxy_url)
    ctx.proxy_ok = bool(pre.get("proxy_ok"))
    print("preflight:", json.dumps({k: v for k, v in pre.items()
                                    if k != "fails"}, ensure_ascii=False))

    items = []
    for src in picks:
        stat = collect_source(src, ctx)
        for it in stat.get("items") or []:
            if "signal" in (it.get("tags") or []) or \
                    len(it.get("content_text") or "") < CONTENT_MIN:
                content_pass(it, src, ctx)
            media_pass(it, src, ctx)
        items.extend(stat.get("items") or [])
        stat.pop("items", None)
        ctx.stats.append(stat)
        print(f"  {src['name']:<22} {stat['status']:<12} "
              f"new={stat.get('items_new', 0)} total={stat.get('items_total', 0)} "
              f"err={stat.get('last_error')}")

    manifest = _manifest(_today_sh(), win, items, ctx,
                         preflight=pre, degraded=not ctx.proxy_ok)
    ok_sources = sum(1 for s in ctx.stats if s["status"] == ST_OK)
    try:
        _write_outputs(run_dir, items, manifest)
    except Exception as e:
        print("SELFTEST FAIL: contract validation:", e)
        return 1
    _save_json(run_dir / "seen_selftest.json", ctx.seen)

    rows = list(meta.iter_jsonl(run_dir / ITEMS_NAME))
    n_lines = len(rows)
    assert all("content_html" not in r for r in rows), \
        "content_html leaked into emitted rows"
    valid = all(RawItem.model_validate(x) for x in rows)
    print(f"selftest: ok_sources={ok_sources}/3 items={n_lines} "
          f"contract_valid={valid}")
    if ok_sources >= 2 and valid and n_lines > 0:
        print("SELFTEST OK")
        return 0
    print("SELFTEST FAIL")
    return 1


# ================================================================= main =====

def main() -> int:
    p = argparse.ArgumentParser(description="AI 早报采集层 (PLAN §5)")
    p.add_argument("--run-dir", type=Path, default=None,
                   help="runs/<date>；缺省 = 今日(Asia/Shanghai)")
    p.add_argument("--sources", type=Path, default=REPO / "sources.yaml")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--max-sources", type=int, default=None)
    p.add_argument("--only", type=str, default=None,
                   help="只跑指定源，逗号分隔")
    p.add_argument("--max-content-fetches", type=int, default=600,
                   help="正文补抓全局上限（防高产源刷流量）")
    p.add_argument("--items-db", default=None,
                   help="条目池 items.sqlite 路径"
                        "（默认 config.storage.items_db > state/items.sqlite）")
    p.add_argument("--manual", metavar="URL", default=None,
                   help="手工入口：抓单 URL 走同一管道")
    p.add_argument("--title", default=None, help="--manual 可选标题")
    p.add_argument("--selftest", action="store_true",
                   help="3 代表源冒烟（写 runs/_doctor/collect）")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    if args.selftest:
        return selftest(args)
    if args.run_dir is None:
        args.run_dir = REPO / "runs" / _today_sh()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
