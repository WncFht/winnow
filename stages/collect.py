#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.28",
#   "feedparser>=6.0.11",
#   "trafilatura>=2",
#   "pyyaml>=6",
#   "pydantic>=2",
# ]
# ///
"""stages/collect.py — 采集层（PLAN.md §5 + §4 契约）。

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

用法: uv run stages/collect.py --run-dir runs/2026-09-22
      uv run stages/collect.py --selftest          # 3 代表源冒烟
      uv run stages/collect.py --max-sources 8 [--only name1,name2]
      uv run stages/collect.py --manual URL [--title T]
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
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
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import yaml  # noqa: E402

from contracts.models import RawItem, RawManifest  # noqa: E402
from lib import http as lhttp  # noqa: E402
from lib import meta, normalize  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

ITEMS_NAME = "10_raw_items.jsonl"
MANIFEST_NAME = "11_raw_manifest.json"
SEEN_PATH = REPO / "state" / "seen.json"
HEALTH_PATH = REPO / "state" / "source_health.json"

DEFAULT_PROXY = "http://127.0.0.1:7890"
CONTENT_MIN = 200                    # <200 字 → 正文补抓
SEEN_URL_CAP = 5000                  # 每源 seen urls 滚动上限
SITEMAP_CHILD_CAP = 5                # sitemapindex 子图最多抓几个
CHANGELOG_LINK_CAP = 120
MAX_CONTENT_PER_SOURCE = 60          # 每源正文补抓上限
HTML_MIN = 1 << 14                   # >16KB 才可能走 changelog 链接抽取

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

def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def _parse_date(v) -> str | None:
    """多格式发布时间 → RFC3339 UTC。识别 epoch(s/ms/µs)/ISO/RFC822/中文格式。"""
    if v is None or v == "":
        return None
    if isinstance(v, time.struct_time):
        return datetime(*v[:6], tzinfo=UTC).isoformat(timespec="seconds")
    if isinstance(v, (int, float)):
        ts = float(v)
        if ts > 1e14:
            ts /= 1e6
        elif ts > 1e11:
            ts /= 1e3
        if ts < 9e8 or ts > 4e9:        # <1998 / >2096 视为无效
            return None
        return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{10,13}", s):
        return _parse_date(float(s))
    try:                               # RFC 822 / feed dates
        return parsedate_to_datetime(s).astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    try:                               # ISO 8601 (+ 'Z')
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)  # 裸时间按源站常见时区
        return dt.astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ) \
                            .astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            continue
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s)
    if m:                            # 残损 ISO，给个保底
        try:
            return datetime.fromisoformat(
                f"{m.group(1)}T{m.group(2)}+08:00") \
                .astimezone(UTC).isoformat(timespec="seconds")
        except ValueError:
            pass
    return None


def _in_window(date_rfc: str | None, win: tuple[datetime, datetime]) -> bool:
    if not date_rfc:
        return False
    try:
        dt = datetime.fromisoformat(date_rfc)
        return win[0] <= dt.astimezone(TZ) < win[1]
    except ValueError:
        return False


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str | None, limit: int = 4000) -> str:
    if not s:
        return ""
    txt = _WS_RE.sub(" ", _TAG_RE.sub(" ", htmlmod.unescape(s))).strip()
    return txt[:limit]


_CJK = re.compile(r"[一-鿿]")


def _guess_lang(*texts: str) -> str | None:
    t = "".join(texts)[:600]
    if not t.strip():
        return None
    cjk = len(_CJK.findall(t))
    if cjk >= 8 and cjk / max(len(t), 1) > 0.08:
        return "zh"
    return "en"


def _sha16(b: bytes | str) -> str:
    h = b if isinstance(b, bytes) else b.encode("utf-8")
    return hashlib.sha256(h).hexdigest()[:16]


def _slug_title(url: str) -> str:
    """URL 末段 → 人读标题（sitemap/changelog signal 的占位题）。"""
    seg = [s for s in urlsplit(url).path.split("/") if s]
    last = unquote(seg[-1] if seg else urlsplit(url).netloc)
    last = re.sub(r"\.(html?|php|aspx?|md)$", "", last, flags=re.I)
    return re.sub(r"[-_+]+", " ", last).strip() or urlsplit(url).netloc


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _is_walled_img(url: str) -> bool:
    h = _host(url)
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
    if path:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for name in ("config.yaml", "config.example.yaml"):
        p = REPO / name
        if p.is_file():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def _cfg(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


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
    base = _cfg(cfg, "llm.base_url", "")
    if base and os.environ.get(str(key_env)):
        import httpx
        try:
            r = httpx.get(
                base.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {os.environ[str(key_env)]}"},
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
        self.proxy_url = _cfg(cfg, "proxy.http", DEFAULT_PROXY) or DEFAULT_PROXY
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


def _post_json(url: str, payload, headers: dict, proxy: str | None,
               timeout: float = 20) -> lhttp.FetchResult:
    """少数 json_api 需要 POST（juejin/infoq/bloomberglaw）。复用 FetchResult。"""
    import httpx
    res = lhttp.FetchResult(via="proxy" if proxy not in (None, "direct") else "direct")
    try:
        with httpx.Client(proxy=None if proxy in (None, "direct") else proxy,
                          trust_env=proxy is None, timeout=timeout,
                          follow_redirects=True) as cli:
            t0 = time.monotonic()
            hdrs = {"User-Agent": lhttp.UA, "Content-Type": "application/json",
                    "Accept": "application/json"}
            hdrs.update(headers or {})
            r = cli.post(url, content=json.dumps(payload), headers=hdrs)
            res.latency_ms = int((time.monotonic() - t0) * 1000)
            res.status = r.status_code
            res.etag = r.headers.get("etag")
            res.final_url = str(r.url)
            res.content_type = r.headers.get("content-type")
            res.body = r.content
            res.error = "ok" if 200 <= r.status_code < 300 else f"http_{r.status_code}"
    except httpx.TimeoutException as e:
        res.error, res.detail = "timeout", str(e)
    except httpx.HTTPError as e:
        res.error, res.detail = "http_0", f"{type(e).__name__}: {e}"
    return res


# ====================================================== raw_item 构造/校验 ==

def mk_item(*, url: str, title: str, src: dict, kind: str,
            date: str | None = None, summary: str | None = None,
            content_html: str | None = None, image: str | None = None,
            tags: list[str] | None = None, guid: str | None = None,
            fetch_status: int = 200, via: str = "direct",
            etag: str | None = None, content_sha: str | None = None,
            raw_ref: str | None = None, reachable: bool = True,
            fetched: str | None = None, lang: str | None = None) -> dict:
    canon = normalize.url_canon(url)
    key = normalize.item_key(url)
    return {
        "schema": "raw_item/1",
        "item_key": key,
        "id": key,
        "url": url,
        "url_canon": canon,
        "title": normalize.title_norm(title or "") or _slug_title(url),
        "content_text": (summary or None),
        "content_html": content_html,
        "date_published": date,
        "date_fetched": fetched or _utcnow(),
        "language": lang if lang is not None else _guess_lang(title or "", summary or ""),
        "tags": tags or [],
        "image": image,
        "_source": {"name": src.get("name", "?"),
                    "feed_url": src.get("feed_url", ""),
                    "kind": kind, "item_guid": guid},
        "_fetch": {"status": fetch_status,
                   "via": via if via in ("direct", "mirror", "cache", "manual")
                   else "mirror",
                   "reachable": reachable, "etag": etag,
                   "content_sha256": content_sha},
        "_raw_ref": raw_ref,
    }


def _validate_item(it: dict) -> dict | None:
    """契约校验；不合法条目丢 + 记日志（宁缺勿炸）。"""
    try:
        return RawItem.model_validate(it).model_dump(by_alias=True)
    except Exception as e:
        log.warning("drop invalid item url=%s: %s",
                    (it.get("url") or "?")[:80], str(e)[:160])
        return None


def _via(res: lhttp.FetchResult) -> str:
    return "mirror" if res.via == "proxy" else "direct"


# ============================================================ feed 方法 =====

def _feed_date(e) -> str | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(attr)
        if t:
            return _parse_date(t)
    for attr in ("published", "updated", "dc_date"):
        if e.get(attr):
            return _parse_date(e[attr])
    return None


def _feed_image(e) -> str | None:
    for m in e.get("media_thumbnail") or []:
        if m.get("url"):
            return m["url"]
    for m in e.get("media_content") or []:
        if str(m.get("medium", "")).startswith("image") or \
                "image" in str(m.get("type", "")):
            if m.get("url"):
                return m["url"]
    if e.get("itunes_image") and e["itunes_image"].get("href"):
        return e["itunes_image"]["href"]
    for l_ in e.get("links") or []:
        if l_.get("rel") == "enclosure" and "image" in str(l_.get("type", "")):
            return l_.get("href")
    if e.get("image") and isinstance(e["image"], dict) and e["image"].get("href"):
        return e["image"]["href"]
    return None


def parse_feed(body: bytes, src: dict, res: lhttp.FetchResult,
               raw_ref: str | None) -> list[dict]:
    """RSS/Atom/YouTube feed → raw_item dicts。"""
    import feedparser
    feed = feedparser.parse(body or b"")
    items = []
    sha = _sha16(body or b"")
    kind = "rss" if src.get("method") in ("rss", "youtube_rss") else "atom"
    fetched = _utcnow()
    for e in feed.entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        content_html = ""
        if e.get("content"):
            content_html = e["content"][0].get("value") or ""
        summary_html = e.get("summary") or e.get("description") or ""
        text = _strip_html(content_html) or _strip_html(summary_html)
        tags = [t.get("term") for t in (e.get("tags") or []) if t.get("term")][:8]
        items.append(mk_item(
            url=link, title=e.get("title") or "", src=src, kind=kind,
            date=_feed_date(e),
            summary=text or None,
            content_html=content_html or summary_html or None,
            image=_feed_image(e), tags=tags,
            guid=e.get("id") or e.get("guid"),
            fetch_status=res.status, via=_via(res), etag=res.etag,
            content_sha=sha, raw_ref=raw_ref, fetched=fetched))
    return items


def _looks_like_feed(body: bytes) -> bool:
    """sniff 根元素：urlset/sitemapindex 不是 feed（claude.com sitemap
    就是 Content-Type 误标 rss+xml 的标准 urlset —— sources.yaml note）。"""
    head = (body or b"")[:600].lstrip().lower()
    if head.startswith(b"<?xml"):
        m = re.search(rb"<\s*([a-z_][\w.:+-]*)", head[5:])
    else:
        m = re.match(rb"<\s*([a-z_][\w.:+-]*)", head)
    root = (m.group(1) if m else b"").split(b":")[-1]
    return root in (b"rss", b"feed", b"rdf", b"opml")


# ======================================================== json_api 适配 =====
#
# 每个 adapter: fn(data, src, ctx) -> (partial_items, extra_meta|None)
# partial item 键: title/url/date/summary/content_html/image/tags/guid
# data = 已解析 JSON（GET 拿到非 JSON → 调用方降级 html diff）

def _dget(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _first_str(v) -> str | None:
    """取首个可用 str：list 取 [0]，dict 取 rendered/name/title。"""
    if isinstance(v, str):
        return v or None
    if isinstance(v, list) and v:
        return _first_str(v[0])
    if isinstance(v, dict):
        for k in ("rendered", "name", "title", "term", "url"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return None


def api_hn(d, src, ctx):
    out = []
    for h in d.get("hits") or []:
        url = h.get("url") or \
            f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({"title": h.get("title"), "url": url,
                    "date": _parse_date(h.get("created_at")),
                    "summary": _strip_html(h.get("story_text")),
                    "guid": str(h.get("objectID") or ""),
                    "tags": ["points:%s" % h.get("points", 0)]})
    return out, None


def api_lobsters(d, src, ctx):
    out = []
    for it in d if isinstance(d, list) else []:
        out.append({"title": it.get("title"),
                    "url": it.get("url") or it.get("short_id_url")
                           or it.get("comments_url"),
                    "date": _parse_date(it.get("created_at")),
                    "summary": _strip_html(it.get("description")),
                    "guid": it.get("short_id"),
                    "tags": [t for t in it.get("tags") or [] if t][:8]})
    return out, None


def api_github_search(d, src, ctx):
    out = []
    for it in d.get("items") or []:
        out.append({"title": it.get("full_name") or it.get("name"),
                    "url": it.get("html_url"),
                    "date": _parse_date(it.get("created_at")
                                        or it.get("pushed_at")),
                    "summary": it.get("description"),
                    "guid": str(it.get("id") or ""),
                    "image": (it.get("owner") or {}).get("avatar_url"),
                    "tags": [f"stars:{it.get('stargazers_count', 0)}"]})
    return out, None


def api_wordpress(d, src, ctx):
    out = []
    for it in d if isinstance(d, list) else []:
        img = None
        emb = it.get("_embedded") or {}
        try:
            img = emb["wp:featuredmedia"][0].get("source_url")
        except (KeyError, IndexError, TypeError):
            img = it.get("jetpack_featured_media_url")
        out.append({"title": _strip_html((it.get("title") or {}).get("rendered")),
                    "url": it.get("link"),
                    "date": _parse_date(it.get("date_gmt") or it.get("date")),
                    "summary": _strip_html(
                        (it.get("excerpt") or {}).get("rendered")),
                    "guid": str(it.get("id") or ""),
                    "image": img})
    return out, None


def api_bilibili(d, src, ctx):
    out = []
    for it in ((d.get("data") or {}).get("archives") or []):
        bvid = it.get("bvid")
        out.append({"title": it.get("title"),
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid
                           else None,
                    "date": _parse_date(it.get("pubdate")),
                    "summary": it.get("desc"),
                    "guid": bvid or str(it.get("aid") or ""),
                    "image": ("https:" + it["pic"]) if str(
                        it.get("pic", "")).startswith("//") else it.get("pic")})
    return [o for o in out if o["url"]], None


def api_huggingface(d, src, ctx):
    out = []
    for it in d if isinstance(d, list) else []:
        p = it.get("paper") or {}
        pid = p.get("id") or it.get("id")
        out.append({"title": it.get("title") or p.get("title"),
                    "url": f"https://huggingface.co/papers/{pid}" if pid else None,
                    "date": _parse_date(it.get("publishedAt")
                                        or p.get("publishedAt")),
                    "summary": it.get("summary") or p.get("summary"),
                    "guid": str(pid or ""),
                    "image": it.get("thumbnail"),
                    "tags": [f"upvotes:{it.get('upvotes', p.get('upvotes', 0))}"]})
    return [o for o in out if o["url"]], None


def api_jiqizhixin(d, src, ctx):
    out = []
    for it in d.get("articles") or []:
        slug = it.get("slug")
        out.append({"title": it.get("title"),
                    "url": f"https://www.jiqizhixin.com/articles/{slug}"
                           if slug else None,
                    "date": _parse_date(it.get("publishedAt")),
                    "summary": it.get("content"),
                    "guid": it.get("id"),
                    "image": it.get("coverImageUrl"),
                    "tags": it.get("tagList") or []})
    return [o for o in out if o["url"]], None


def api_sspai(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        iid = it.get("id")
        out.append({"title": it.get("morning_paper_title") or it.get("title"),
                    "url": f"https://sspai.com/post/{iid}" if iid else None,
                    "date": _parse_date(it.get("released_time")
                                        or it.get("created_time")),
                    "summary": it.get("summary"),
                    "guid": str(iid or ""),
                    "image": it.get("banner")})
    return [o for o in out if o["url"]], None


def api_tmtpost(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        img = it.get("thumb_image") or {}
        try:
            img = img["original"][0].get("url")
        except (KeyError, IndexError, TypeError):
            img = None
        out.append({"title": it.get("title"),
                    "url": it.get("short_url") or it.get("share_link"),
                    "date": _parse_date(it.get("time_published")),
                    "summary": it.get("summary"),
                    "guid": str(it.get("guid") or it.get("post_guid") or ""),
                    "image": img})
    return [o for o in out if o["url"]], None


def api_zhihu_col(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        aid = it.get("id")
        url = it.get("url") or ""
        if "api.zhihu.com" in url and aid:
            url = f"https://zhuanlan.zhihu.com/p/{aid}"
        elif aid and "zhihu.com" not in url:
            url = f"https://zhuanlan.zhihu.com/p/{aid}"
        out.append({"title": it.get("title"), "url": url or None,
                    "date": _parse_date(it.get("created")),
                    "summary": it.get("excerpt") or _strip_html(it.get("content")),
                    "content_html": it.get("content"),
                    "guid": str(aid or ""),
                    "image": it.get("image_url") or it.get("title_image")})
    return [o for o in out if o["url"]], None


def api_qwen(d, src, ctx):
    out = []
    arts = ((d.get("data") or {}).get("articles")
            or (d.get("data") or {}).get("list") or [])
    for it in arts:
        if not isinstance(it, dict):
            continue
        path = it.get("path") or it.get("slug")
        url = f"https://qwen.ai/blog/{path}" if path else it.get("url")
        extra = it.get("extra") or {}
        out.append({"title": it.get("title"), "url": url,
                    "date": _parse_date(extra.get("date") or it.get("date")),
                    "summary": extra.get("introduction")
                               or extra.get("description")
                               or _strip_html(it.get("content"), 800),
                    "content_html": it.get("content"),
                    "guid": str(it.get("id") or "")})
    return [o for o in out if o["url"]], None


def api_infoq(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        if not isinstance(it, dict):
            continue
        out.append({"title": it.get("article_title") or it.get("article_sharetitle"),
                    "url": f"https://www.infoq.cn/article/{it.get('uuid')}"
                           if it.get("uuid") else None,
                    "date": _parse_date(it.get("publish_time") or it.get("ctime")),
                    "summary": it.get("article_summary"),
                    "guid": str(it.get("uuid") or it.get("aid") or ""),
                    "image": it.get("article_cover")})
    return [o for o in out if o["url"]], None


def api_juejin(d, src, ctx):
    out = []
    for it in d.get("data") or []:
        ai = (it or {}).get("article_info") or {}
        aid = ai.get("article_id") or it.get("article_id")
        out.append({"title": ai.get("title"),
                    "url": f"https://juejin.cn/post/{aid}" if aid else None,
                    "date": _parse_date(ai.get("ctime")),
                    "summary": ai.get("brief_content"),
                    "guid": str(aid or ""),
                    "image": ai.get("cover_image"),
                    "tags": [f"view:{ai.get('view_count', 0)}",
                             f"digg:{ai.get('digg_count', 0)}"]})
    return [o for o in out if o["url"]], None


def api_oschina(d, src, ctx):
    out = []
    res = d.get("result") or []
    if isinstance(res, dict):
        res = res.get("items") or res.get("list") or []
    for it in res:
        if not isinstance(it, dict):
            continue
        oid = it.get("obj_id") or it.get("id")
        url = it.get("url") or it.get("obj_url") or \
            (f"https://www.oschina.net/news/{oid}" if oid else None)
        out.append({"title": it.get("title") or it.get("obj_title"),
                    "url": url,
                    "date": _parse_date(it.get("time") or it.get("pub_time")
                                        or it.get("create_time")),
                    "summary": it.get("summary") or it.get("obj_summary"),
                    "guid": str(oid or "")})
    return [o for o in out if o["url"]], None


def api_alphaxiv(d, src, ctx):
    out = []
    papers = d.get("papers") or (d.get("data") or {}).get("papers") or []
    for it in papers:
        if not isinstance(it, dict):
            continue
        pid = it.get("canonical_id") or it.get("id")
        url = it.get("external_link") or \
            (f"https://www.alphaxiv.org/abs/{pid}" if pid else None)
        out.append({"title": it.get("title"), "url": url,
                    "date": _parse_date(it.get("publication_date")
                                        or it.get("first_publication_date")),
                    "summary": it.get("feed_description")
                               or it.get("paper_summary") or it.get("abstract"),
                    "guid": str(pid or ""),
                    "image": it.get("image_url"),
                    "tags": it.get("topics") or []})
    return [o for o in out if o["url"]], None


def api_zenodo(d, src, ctx):
    out = []
    for it in (d.get("data") or d.get("hits", {}).get("hits") or []):
        a = it.get("attributes") or it.get("metadata") or it
        doi = a.get("doi") or it.get("id")
        url = a.get("url") or (f"https://doi.org/{doi}" if doi else None)
        out.append({"title": _first_str(a.get("titles")) or a.get("title"),
                    "url": url,
                    "date": _parse_date(a.get("published") or a.get("created")
                                        or a.get("publication_date")),
                    "summary": _strip_html(_first_str(a.get("descriptions"))
                                           or a.get("description")),
                    "guid": str(doi or it.get("id") or "")})
    return [o for o in out if o["url"]], None


def api_openrouter(d, src, ctx):
    """模型目录 diff —— 标 signal 只发新增（见 §5.2 signal 语义）。"""
    out = []
    for it in d.get("data") or []:
        mid = it.get("id")
        out.append({"title": it.get("name") or mid,
                    "url": f"https://openrouter.ai/{mid}" if mid else None,
                    "date": _parse_date(it.get("created")),
                    "summary": _strip_html(it.get("description"), 800),
                    "guid": mid,
                    "signal": True})           # 每日 diff：只发新模型
    return [o for o in out if o["url"]], None


def api_cohere(d, src, ctx):
    out = []
    res = d.get("result") or d.get("data") or []
    for it in res:
        if not isinstance(it, dict):
            continue
        slug = it.get("slug")
        out.append({"title": it.get("title"),
                    "url": f"https://cohere.com/blog/{slug}" if slug else None,
                    "date": _parse_date(it.get("date")),
                    "summary": it.get("subtitle"),
                    "guid": str(slug or it.get("_id") or "")})
    return [o for o in out if o["url"]], None


def api_rsshub_routes(d, src, ctx):
    """routes.json = {path:{…}} —— 生态雷达，signal 只发新增路由。"""
    out = []
    if isinstance(d, dict):
        for k in list(d.keys())[:8000]:
            if isinstance(d[k], dict) and k.startswith("/"):
                out.append({"title": f"RSSHub route {k}",
                            "url": f"https://docs.rsshub.app/routes{k}",
                            "guid": k, "signal": True})
    return out, None


def api_bloomberglaw(d, src, ctx):
    arts = ((d.get("data") or {}).get("articles") or {})
    out = []
    for it in arts.get("items") or []:
        out.append({"title": it.get("headline"), "url": it.get("url"),
                    "date": _parse_date(it.get("postedDate")),
                    "summary": _strip_html(it.get("summary")),
                    "guid": it.get("id"),
                    "tags": ["free" if it.get("free") else "paywalled"]})
    return out, None


def api_xiaoyuzhou(src, ctx):
    """小宇宙播客：__NEXT_DATA__ 取 buildId → _next/data/podcast/<pid>.json。"""
    pid = urlsplit(src["feed_url"]).path.rstrip("/").split("/")[-1]
    home = ctx.get("https://www.xiaoyuzhoufm.com/", src, timeout=15)
    res = lhttp.FetchResult(status=home.status)
    if not home.ok:
        res.error = home.error
        return [], res
    m = re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>',
                  home.text, re.S)
    if not m:
        res.error = "parse_error"
        return [], res
    try:
        bid = json.loads(htmlmod.unescape(m.group(1)))["buildId"]
    except (json.JSONDecodeError, KeyError):
        res.error = "parse_error"
        return [], res
    api = f"https://www.xiaoyuzhoufm.com/_next/data/{bid}/podcast/{pid}.json"
    r = ctx.get(api, src, timeout=15)
    if not r.ok:
        return [], r
    try:
        pod = json.loads(r.text)["pageProps"]["podcast"]
    except (json.JSONDecodeError, KeyError):
        r.error = "parse_error"
        return [], r
    out = []
    for ep in pod.get("episodes") or []:
        eid = ep.get("eid") or ep.get("id")
        img = ep.get("image") or pod.get("image") or {}
        out.append({"title": ep.get("title"),
                    "url": f"https://www.xiaoyuzhoufm.com/episode/{eid}"
                           if eid else None,
                    "date": _parse_date(ep.get("pubDate")),
                    "summary": _strip_html(ep.get("shownotes")
                                           or ep.get("description")),
                    "guid": eid,
                    "image": img.get("picUrl") if isinstance(img, dict) else None})
    return [o for o in out if o["url"]], r


# 具名 adapter 注册表；值 None → 走通用 walker
API_ADAPTERS = {
    "hn_algolia": api_hn,
    "lobsters": api_lobsters,
    "github_search": api_github_search,
    "bilibili_newlist": api_bilibili,
    "huggingface_daily": api_huggingface,
    "jiqizhixin": api_jiqizhixin,
    "sspai": api_sspai,
    "tmtpost": api_tmtpost,
    "zhihu_qbitai": api_zhihu_col,
    "qwen_blog": api_qwen,
    "infoq_ai": api_infoq,
    "juejin_ai": api_juejin,
    "oschina_ai": api_oschina,
    "alphaxiv_feed": api_alphaxiv,
    "zenodo_datacite": api_zenodo,
    "openrouter_models": api_openrouter,
    "cohere_blog": api_cohere,
    "rsshub_routes": api_rsshub_routes,
    "bloomberglaw_ai": api_bloomberglaw,
    "xiaoyuzhoufm_ai": api_xiaoyuzhou,   # 签名特殊：src/ctx 自取
}

# json_api 但需 POST/特殊头的请求覆写
REQUEST_SPECS = {
    "juejin_ai": {
        "method": "POST",
        "json": {"id_type": 2, "sort_type": 300,
                 "cate_id": "6809637773935378440", "cursor": "0", "limit": 30},
    },
    "infoq_ai": {
        "method": "POST",
        "json": {"id": 31, "size": 30},
        "headers": {"Referer": "https://www.infoq.cn/",
                    "Origin": "https://www.infoq.cn"},
    },
    "bloomberglaw_ai": {
        "method": "POST",
        "json": {"query": "query($c:[String],$since:String){articles("
                          "channelIds:$c,startDate:$since,order:PostedDate,"
                          "direction:Descending,limit:50){count items{id "
                          "headline postedDate url free summary}}}",
                 "variables": {"c": ["00000188-05d6-db7f-a7e8-f7d6f0170000"],
                               "since": None}},   # 运行时填 window 起点日期
    },
    "tmtpost": {"headers": {"app-version": "web1.0"}},
}

_TITLE_KEYS = {"title", "name", "headline", "article_title", "morning_paper_title"}
_URL_KEYS = {"url", "link", "share_url", "item_url", "html_url", "article_url",
             "short_url", "permalink", "web_url", "page_url"}
_ID_KEYS = {"id", "guid", "article_id", "objectid", "aid", "uuid", "eid",
            "bvid", "slug", "path", "short_id"}
_DATE_KEYS = {"published", "published_at", "publishedat", "pubdate", "date",
              "created", "created_at", "createdat", "created_time", "ctime",
              "utime", "updated", "updated_at", "posteddate", "posted_at",
              "released_time", "release_time", "first_publication_date",
              "publication_date", "publish_time", "time_published", "post_time"}
_SUM_KEYS = {"summary", "description", "desc", "brief_content", "excerpt",
             "article_summary", "subtitle", "feed_description", "introduction",
             "content", "abstract"}
_IMG_KEYS = {"image", "cover", "coverimageurl", "cover_url", "thumbnail",
             "thumb", "pic", "image_url", "share_pic", "article_cover", "banner"}


def _walk_lists(node, depth=0):
    if depth > 6:
        return
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if len(dicts) >= 3:
            yield node
        for v in node[:200]:
            yield from _walk_lists(v, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_lists(v, depth + 1)


def _score_list(lst) -> int:
    n = 0
    for it in lst[:50]:
        ks = {k.lower() for k in it.keys()}
        if ks & _TITLE_KEYS and (ks & _URL_KEYS or ks & _ID_KEYS):
            n += 1
    return n


def api_generic(d, src, ctx):
    """通用 JSON walker：找「最像条目列表」的 dict list，启发式字段映射。"""
    best, best_score = [], 0
    for lst in _walk_lists(d):
        s = _score_list(lst)
        if s > best_score:
            best, best_score = lst, s
    if best_score < 3:
        return [], None
    out = []
    for it in best:
        ks = {k.lower(): k for k in it.keys()}     # lc -> orig
        def gv(keyset):
            for k in keyset:
                if k in ks:
                    return it[ks[k]]
            return None
        url = _first_str(gv(_URL_KEYS))
        if url and not url.startswith("http"):
            url = urljoin(src["feed_url"], url)
        if not url:
            continue
        title = _first_str(gv(_TITLE_KEYS))
        guid = _first_str(gv(_ID_KEYS))
        out.append({"title": title, "url": url,
                    "date": _parse_date(gv(_DATE_KEYS)),
                    "summary": _strip_html(_first_str(gv(_SUM_KEYS)), 1500),
                    "guid": str(guid or ""),
                    "image": _first_str(gv(_IMG_KEYS))})
    return out, None


# ==================================================== diff 方法 (signal) ====

_SITEMAP_INTEREST = re.compile(
    r"news|post|article|blog|changelog|release|research|engineering|docs|update",
    re.I)


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _sitemap_urls(body: bytes, src: dict, ctx) -> tuple[list[tuple[str, str | None]], int]:
    """→ ([(url,lastmod)], http_status)。sitemapindex 递归抓子图（限量）。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], 0
    kind = _xml_local(root.tag)
    if kind == "urlset":
        out = []
        for u in root:
            if _xml_local(u.tag) != "url":
                continue
            loc = lastmod = None
            for c in u:
                t = _xml_local(c.tag)
                if t == "loc":
                    loc = (c.text or "").strip()
                elif t == "lastmod":
                    lastmod = (c.text or "").strip()
            if loc:
                out.append((loc, lastmod))
        return out, 200
    if kind == "sitemapindex":
        children = []
        for s in root:
            loc = None
            for c in s:
                if _xml_local(c.tag) == "loc":
                    loc = (c.text or "").strip()
            if loc:
                children.append(loc)
        hot = [u for u in children if _SITEMAP_INTEREST.search(u)]
        urls: list[tuple[str, str | None]] = []
        for cu in (hot or children)[:SITEMAP_CHILD_CAP]:
            r = ctx.get(cu, src, timeout=15)
            if not r.ok:
                continue
            sub, _ = _sitemap_urls(r.body or b"", src, ctx)
            urls.extend(sub)
            if len(urls) > 3000:
                break
        return urls, 200
    return [], 200


_NOISE_PATH = re.compile(
    r"login|signin|signup|subscribe|search|/tag|category|author|about|contact|"
    r"privacy|terms|legal|rss|atom|feed|cdn|static|assets|_next|wp-json|"
    r"wp-admin|account|settings|share|comment", re.I)
_NEWSISH_PATH = re.compile(
    r"news|blog|changelog|release|article|post|docs|updates|announce|notes|"
    r"engineering|research|/20\d\d", re.I)


def _page_links(body_text: str, base: str) -> list[str]:
    """changelog 页 → 同域详情链接候选（保序去重）。"""
    host = _host(base)
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\'#]+)', body_text, re.I):
        href = htmlmod.unescape(m.group(1)).strip()
        if href.startswith(("javascript:", "mailto:", "data:", "tel:")):
            continue
        u = urljoin(base, href)
        sp = urlsplit(u)
        if sp.scheme not in ("http", "https"):
            continue
        h = (sp.hostname or "").lower()
        if h != host and h != host.lstrip("www.") and \
                h != "www." + host.lstrip("www."):
            continue
        path = sp.path
        if _NOISE_PATH.search(path):
            continue
        depth = len([s for s in path.split("/") if s])
        if not (_NEWSISH_PATH.search(path) or depth >= 2):
            continue
        canon = normalize.url_canon(u)
        if canon and canon not in seen:
            seen.add(canon)
            out.append(u)
        if len(out) >= CHANGELOG_LINK_CAP:
            break
    return out


def _signal_item(url: str, src: dict, res: lhttp.FetchResult,
                 sha: str, raw_ref: str | None, title: str | None = None,
                 date: str | None = None,
                 extra_tags: list[str] | None = None) -> dict:
    """diff 检出条目：date_published 恒 None（§5.2 signal 语义）；
    sitemap lastmod 等日期提示降级进 tags。"""
    tags = ["signal"] + list(extra_tags or [])
    if date:
        tags.append("lastmod:" + date[:10])
    return mk_item(url=url, title=title or _slug_title(url), src=src,
                   kind="scrape", date=None, summary=None,
                   fetch_status=res.status, via=_via(res), etag=res.etag,
                   content_sha=sha, raw_ref=raw_ref, tags=tags)


def collect_sitemap(src: dict, ctx, body: bytes, res: lhttp.FetchResult,
                    raw_ref: str | None) -> list[dict]:
    urls, _ = _sitemap_urls(body, src, ctx)
    seen_urls = set(ctx.seen.get(src["name"], {}).get("urls") or [])
    sha = _sha16(body)
    # 首跑 bootstrap 全是"新"URL——newsish 路径优先，避免 cap 被
    # /ja/solutions 之类的本地化营销页吃满（claude.com sitemap 实测如此）
    urls = sorted(urls, key=lambda t: 0 if _NEWSISH_PATH.search(
        urlsplit(t[0]).path) else 1)
    items = []
    for u, lastmod in urls:
        if len(items) >= src["max_items_per_source"]:
            break
        canon = normalize.url_canon(u)
        if canon in seen_urls:
            continue
        items.append(_signal_item(
            u, src, res, sha, raw_ref, date=_parse_date(lastmod)))
    # 本轮全集快照进 round_urls —— run 末尾才并入 urls（否则 items_new 永远 0）
    ctx.seen.setdefault(src["name"], {})["round_urls"] = \
        [normalize.url_canon(u) for u, _ in urls][:SEEN_URL_CAP]
    return items


def collect_changelog(src: dict, ctx, body: bytes, res: lhttp.FetchResult,
                      raw_ref: str | None) -> list[dict]:
    text = (body or b"").decode("utf-8", "replace")
    seen_entry = ctx.seen.setdefault(src["name"], {})
    seen_urls = set(seen_entry.get("urls") or [])
    sha = _sha16(body)
    links = _page_links(text, res.final_url or src["feed_url"])
    items = []
    for u in links:
        if len(items) >= src["max_items_per_source"]:
            break
        if normalize.url_canon(u) in seen_urls:
            continue
        items.append(_signal_item(u, src, res, sha, raw_ref))
    # 页面签名：链接清单 + 标题块 hash → 无新链接但内容变了也发一条
    heads = " ".join(re.findall(r"<h[23][^>]*>(.*?)</h[23]>", text, re.S)[:80])
    page_sig = _sha16(_strip_html(heads) + "|" + ",".join(
        normalize.url_canon(u) for u in links[:80]))
    old_sig = seen_entry.get("page_sig")
    seen_entry["page_sig"] = page_sig
    seen_entry["round_urls"] = [normalize.url_canon(u) for u in
                              links][:SEEN_URL_CAP]
    if old_sig and old_sig != page_sig and not items:
        it = _signal_item(src["feed_url"], src, res, sha, raw_ref,
                          title=f"{src['name']} changelog updated")
        it["tags"].append("page_sig")      # 页级变更信号：豁免 new-only
        items.append(it)
    return items


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
        from lib import x_nitter
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
        from lib import x_ssr
        got = x_ssr.fetch_user(handle, pcfg)
        if got:
            return _stamp_via(list(got), "x:ssr"), \
                {"route": "x:ssr", "attempts": attempts}
        rec("x:ssr", "empty")
    except Exception as e:
        rec("x:ssr", f"{type(e).__name__}: {e}")

    # ③ cdn.syndication.twimg.com（429 指数退避在模块内）
    try:
        from lib import x_synd
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
        from lib import reddit_collect
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
        from lib import weibo_collect
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
        item["content_text"] = ext_text[:8000]
    if not item.get("content_html") and len(body) < (1 << 22):
        item["content_html"] = None      # 原 HTML 太大不入契约，留 _raw_ref
    if meta_d.get("title") and (
            is_signal or not item.get("title") or
            item["title"] == _slug_title(item["url"])):
        item["title"] = normalize.title_norm(meta_d["title"])
    if not item.get("date_published") and meta_d.get("date"):
        item["date_published"] = _parse_date(meta_d["date"])
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
        (media_dir / name).write_bytes(r.body)
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
            content_html=p.get("content_html"), image=p.get("image"),
            tags=p.get("tags") or [], guid=p.get("guid"),
            fetch_status=res.status, via=_via(res), etag=res.etag,
            content_sha=sha, raw_ref=raw_ref)
        if p.get("signal"):
            if "signal" not in it["tags"]:
                it["tags"].append("signal")
            it["date_published"] = None
        out.append(it)
    return out


def fetch_with_failover(src: dict, ctx, spec: dict | None):
    """feed_url + failover 链 → (res, url_used)。validators 按 URL 记；
    POST spec 只作用于主 URL，failover 一律 GET。"""
    urls = [src["feed_url"]] + [u for u in (src.get("failover") or []) if u]
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


def _json_items(src, ctx, body, res, raw_ref, stat) -> list[dict]:
    """json_api：具名 adapter → 通用 walker → HTML 降级 changelog diff。"""
    name = src["name"]
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        text = body.decode("utf-8", "replace")
        if "<html" in text[:4096].lower():
            return collect_changelog(src, ctx, body, res, raw_ref)
        stat["status"] = "parse_error"
        stat["last_error"] = "body not JSON"
        return []
    fn = API_ADAPTERS.get(name)
    try:
        if fn is api_xiaoyuzhou:
            partials, _ = fn(src, ctx)
        elif fn:
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
                it["content_text"] = d["text"][:8000]
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
    lines = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
    meta.atomic_write(run_dir / ITEMS_NAME, lines)
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


def run(args) -> int:
    cfg = load_cfg(args.config)
    run_dir = meta.ensure_run(Path(args.run_dir)
                              if args.run_dir else _today_sh())
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
                old = [json.loads(x) for x in
                       p.read_text(encoding="utf-8").splitlines() if x.strip()]
            old = [o for o in old if o.get("item_key") != v["item_key"]] + [v]
            _write_outputs(run_dir, old, _manifest(run_date, win, old, ctx,
                                                 note="manual"))
            print(json.dumps(v, ensure_ascii=False, indent=1)[:2000])
            return 0

        # ---------------- preflight ----------------
        pre = preflight(cfg, ctx.proxy_url)
        ctx.proxy_ok = bool(pre.get("proxy_ok"))
        degraded = not ctx.proxy_ok
        log.info("preflight: %s", json.dumps(pre, ensure_ascii=False))

        # ---------------- per-source ----------------
        all_items: list[dict] = []
        emitted_canons: dict[str, list[str]] = {}
        for src in sources:
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
    health = _load_json(HEALTH_PATH, {})
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

    n_lines = sum(1 for _ in open(run_dir / ITEMS_NAME, encoding="utf-8"))
    valid = all(RawItem.model_validate(json.loads(x))
                for x in (run_dir / ITEMS_NAME)
                .read_text(encoding="utf-8").splitlines() if x.strip())
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
