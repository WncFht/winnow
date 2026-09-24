"""stages/lib/sources/feed.py — RSS/Atom/YouTube feed → raw_item dicts。

feedparser 惰性 import（仅 feed 源用到，缺库不炸其他 method）。
looks_like_feed 嗅根元素防 sitemap 误标 rss+xml（claude.com sitemap 就是
Content-Type 误标 rss+xml 的标准 urlset —— sources.yaml note）。
"""
from __future__ import annotations

from stages.lib import http as lhttp, normalize, rawitem


def _feed_date(e) -> str | None:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(attr)
        if t:
            return normalize.parse_date_utc(t)
    for attr in ("published", "updated", "dc_date"):
        if e.get(attr):
            return normalize.parse_date_utc(e[attr])
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
    sha = normalize.sha16(body or b"")
    kind = "rss" if src.get("method") in ("rss", "youtube_rss") else "atom"
    fetched = normalize.utcnow()
    for e in feed.entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        content_html = ""
        if e.get("content"):
            content_html = e["content"][0].get("value") or ""
        summary_html = e.get("summary") or e.get("description") or ""
        text = normalize.strip_html(content_html) or normalize.strip_html(summary_html)
        tags = [t.get("term") for t in (e.get("tags") or []) if t.get("term")][:8]
        items.append(rawitem.mk_item(
            url=link, title=e.get("title") or "", src=src, kind=kind,
            date=_feed_date(e),
            summary=text or None,
            image=_feed_image(e), tags=tags,
            guid=e.get("id") or e.get("guid"),
            fetch_status=res.status, via=lhttp.fetch_via(res), etag=res.etag,
            content_sha=sha, raw_ref=raw_ref, fetched=fetched))
    return items


def looks_like_feed(body: bytes) -> bool:
    """sniff 根元素：urlset/sitemapindex 不是 feed（claude.com sitemap
    就是 Content-Type 误标 rss+xml 的标准 urlset —— sources.yaml note）。"""
    return lhttp.xml_root_tag(body) in (b"rss", b"feed", b"rdf", b"opml")
