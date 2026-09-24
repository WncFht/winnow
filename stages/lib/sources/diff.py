"""stages/lib/sources/diff.py — sitemap_diff / changelog_diff → signal 条目。

diff 方法只发新 URL 的 signal 条目（tags 含 "signal"、kind=scrape、
date_published=null；契约偏差见 collect.py 头注）。
"""
from __future__ import annotations

import html as htmlmod
import json
import re
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from stages.lib import http as lhttp, normalize, rawitem
from stages.lib.sources.common import SEEN_URL_CAP

SITEMAP_CHILD_CAP = 5      # sitemapindex 子图最多抓几个
CHANGELOG_LINK_CAP = 120


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
    host = normalize.url_host(base)
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


_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
_MD_HEAD = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_HTML_HINT = re.compile(r"<a\b[^>]*?href=|<h[23][^>]*>", re.I)
_ASSET_SRC = re.compile(
    r"<(?:script|link)\b[^>]*?(?:src|href)=[\"']([^\"']+)", re.I)


def _md_links(text: str, base: str) -> list[str]:
    """llms.txt / Mintlify .md 原生 markdown 链接 → 同域候选（保序去重）。
    与 _page_links 同一套 host/noise/newsish 过滤。"""
    host = normalize.url_host(base)
    out, seen = [], set()
    for m in _MD_LINK.finditer(text):
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


def _asset_fingerprint(text: str) -> list[str]:
    """纯 JS 壳页（无 a/h 也无 md 链接）的资源指纹：版本化 script/css src。"""
    out = []
    for m in _ASSET_SRC.finditer(text[: 1 << 18]):
        u = htmlmod.unescape(m.group(1)).strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http"):
            out.append(u)
        if len(out) >= 40:
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
    return rawitem.mk_item(url=url, title=title or normalize.slug_title(url), src=src,
                   kind="scrape", date=None, summary=None,
                   fetch_status=res.status, via=lhttp.fetch_via(res), etag=res.etag,
                   content_sha=sha, raw_ref=raw_ref, tags=tags)


def collect_sitemap(src: dict, ctx, body: bytes, res: lhttp.FetchResult,
                    raw_ref: str | None) -> list[dict]:
    urls, _ = _sitemap_urls(body, src, ctx)
    seen_urls = set(ctx.seen.get(src["name"], {}).get("urls") or [])
    sha = normalize.sha16(body)
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
            u, src, res, sha, raw_ref, date=normalize.parse_date_utc(lastmod)))
    # 本轮全集快照进 round_urls —— run 末尾才并入 urls（否则 items_new 永远 0）
    ctx.seen.setdefault(src["name"], {})["round_urls"] = \
        [normalize.url_canon(u) for u, _ in urls][:SEEN_URL_CAP]
    return items


def collect_changelog(src: dict, ctx, body: bytes, res: lhttp.FetchResult,
                      raw_ref: str | None) -> list[dict]:
    text = (body or b"").decode("utf-8", "replace")
    seen_entry = ctx.seen.setdefault(src["name"], {})
    seen_urls = set(seen_entry.get("urls") or [])
    sha = normalize.sha16(body)
    base = res.final_url or src["feed_url"]
    # markdown(llms.txt/.md) 与 HTML 分流：纯 md 页没有 <a href>/<h2-3>，
    # 走 _page_links 恒得空集 → 永远 empty 且 page_sig 恒为空签名。
    if _HTML_HINT.search(text):
        links = _page_links(text, base)
        heads = " ".join(
            re.findall(r"<h[23][^>]*>(.*?)</h[23]>", text, re.S)[:80])
    else:
        links = _md_links(text, base)
        heads = " ".join(_MD_HEAD.findall(text)[:80])
    items = []
    for u in links:
        if len(items) >= src["max_items_per_source"]:
            break
        if normalize.url_canon(u) in seen_urls:
            continue
        items.append(_signal_item(u, src, res, sha, raw_ref))
    # 页面签名：链接清单 + 标题块 hash → 无新链接但内容变了也发一条
    sig_basis = normalize.strip_html(heads)
    if src.get("sig_text"):
        # 名单/计数类页（mathandai endorsers）无 h2/h3 也没有同域深链——
        # 把正文可见文本（前 4K）并进签名，计数/名单变化也能触发信号
        sig_basis += "|" + normalize.strip_html(text)
    if not sig_basis.strip() and not links:
        # 死壳兜底（SPA shell / 空页）：混入资源指纹，前端重部署可发信号；
        # 只在无正文时启用，避免改动正常页的既有签名。
        sig_basis += "|assets:" + ",".join(_asset_fingerprint(text))
    page_sig = normalize.sha16(sig_basis + "|" + ",".join(
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
