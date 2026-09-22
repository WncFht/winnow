#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.28",
# ]
# ///
"""X 采集四路之② — x.com/<handle> 登出态 SSR HTML → raw_item（PLAN §5.3, §4）。

移植自 experiments/hard-x.com-scraper-tool-or-manual/scrape_profile.py，
并修正了种子脚本的两处死代码（见下）：X 给未登录 profile 页下发 SSR HTML，
内嵌 Relay/RSC 载荷（$R[n] 记录，键为 base64("<Typename>:<id>")）。本模块
解析内嵌记录，把推文映射成 raw_item/1 契约字典，供 collect/x_collect 直接落
10_raw_items.jsonl。

记录键实测结构（2026-09-22 抓档校准）：
    client:<b64"Tweet:id">:details            full_text / created_at_ms
    client:<b64"Tweet:id">:counts             reply/retweet/favorite/…_count
    client:<b64"Tweet:id">:legacy             lang / possibly_sensitive
    client:<b64"Tweet:id">:media_entities2:N  media_url_https / type
    client:<b64"Tweet:id">:url_entities:N     expanded_url（UrlEntity 记录）
    <b64"NoteTweet:id">  （无 client: 前缀！）   text —— 长推文正文
    client:<b64"NoteTweet:id">:entity_set:urls:N  长推文的 expanded_url
    …UserCore",name:"…",screen_name:"…"        页主信息

  ※ 种子脚本 note_tweet 取 `client:<TweetKey>:note_tweet` —— 该记录实为
    `note_tweet:{__ref:"<NoteTweetKey>"}` 指针，正文在无前缀 NoteTweet 记录里；
    且其 expanded_url 抓取用 30KB 盲窗会溢进下一条推文，已改为按记录定点取。

路由语义：HTTP 200 但 SSR 数据字段全缺（JS 空壳页/登录墙变体）→ 抛 ShellOnly，
调用方按序转下一路（syndication → 付费 adapter）。传输层/HTTP 层失败抛
FetchError，error 字段沿用 lib/http.py 的错误分类法
（http_<code>|timeout|walled|rate_limited|dns_fail|empty|parse_error）。

cfg（dict，全键可选；直接吃 sources.yaml 源条目亦可）：
    name / feed_url          → _source（kind 恒 "scrape"）
    proxy                    'required'|'prefer'|'direct_only'（源策略）或代理 URL
    proxy_url                显式代理 URL（优先于 proxy 键；亦接受 proxy={http:..}）
    timeout                  秒，默认 20
    max_items / max_items_per_source   截断护栏，默认 30
    run_dir                  runs/<date> 路径；给定时原始 HTML 落 raw_cache，
                             _raw_ref 指过去（§5.2-2 契约）
    playwright_fallback      True 时空壳结果再走一遍无头 Chromium（懒加载，
                             不在 PEP723 deps 内；不可用则仍抛 ShellOnly）

Smoke:  uv run stages/lib/x_ssr.py              # 离线夹具断言 + 活网实测
        uv run stages/lib/x_ssr.py --offline    # 只跑离线夹具
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root (contracts/)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # stages/ (lib.*)

from lib import http as lib_http
from lib import normalize

REPO_ROOT = Path(__file__).resolve().parents[2]

# 与种子脚本一致的浏览器指纹；lib_http 默认 UA 带 pipeline 标识，X 按 UA
# 决定是否下发 SSR 数据，故这里显式覆盖（实测 Chrome UA 才有内嵌记录）。
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_PROXY = "http://127.0.0.1:7890"  # 本机 clash（config.proxy.http 同值）
PROXY_POLICIES = {"required", "prefer", "direct_only"}

_RAW_ITEM_KEYS = {  # contracts/models.py RawItem extra="forbid" 全集
    "schema", "item_key", "id", "url", "url_canon", "title", "content_text",
    "date_published", "date_fetched", "language", "tags",
    "image", "_source", "_fetch", "_raw_ref",
}


# ------------------------------------------------------------- exceptions ----

class ShellOnly(RuntimeError):
    """HTTP 200 但页面不含任何推文数据（JS 空壳/登录墙变体）→ 转下一路。"""

    def __init__(self, msg: str = "", *, status: int = 200,
                 detail: Optional[str] = None):
        super().__init__(msg or "x.com profile page returned no tweet data")
        self.status = status
        self.detail = detail


class FetchError(RuntimeError):
    """非 200 / 传输失败；.error 沿用 lib/http.py 错误分类法。"""

    def __init__(self, error: str, detail: Optional[str] = None, status: int = 0):
        super().__init__(f"{error}: {detail}" if detail else error)
        self.error = error
        self.status = status
        self.detail = detail


# ----------------------------------------------------------------- proxy ----

def _resolve_proxy(cfg: dict) -> Optional[str]:
    """-> httpx proxy arg（URL str）或 'direct'。

    优先级：cfg.proxy_url > cfg.proxy(=URL 或 {http:..} dict) > env > 本机默认。
    'direct_only' 策略或显式 'direct' → 'direct'（lib_http 的直连哨兵）。
    """
    pv = cfg.get("proxy")
    pol = cfg.get("proxy_policy")
    if pv in PROXY_POLICIES:
        pol, pv = pv, None
    if pol == "direct_only" or pv in ("direct", "none", "off"):
        return "direct"
    p = cfg.get("proxy_url")
    if not p and isinstance(pv, dict):
        p = pv.get("http") or pv.get("https")
    elif not p and isinstance(pv, str) and pv:
        p = pv
    if not p:
        p = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    return p or DEFAULT_PROXY


# ----------------------------------------------------------------- parse ----

def _unesc(s: str) -> str:
    # 记录体里是 JS 字符串转义（\n \" \\ \uXXXX）；正则不放过未转义引号，
    # 故可包一层双引号走 json.loads，同时保住原始 UTF-8。
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


def _tkey(tid: str) -> str:
    return base64.b64encode(f"Tweet:{tid}".encode()).decode()


def _nkey(tid: str) -> str:
    return base64.b64encode(f"NoteTweet:{tid}".encode()).decode()


def _first_rec(h: str, pat: str, span: int) -> str:
    i = h.find(pat)
    return h[i:i + span] if i >= 0 else ""


def _recs(h: str, tid: str, suffix: str, span: int = 2000) -> list:
    """`{__id:"client:<TweetKey>:<suffix>[…]"` 全部记录窗口（按序）。"""
    pat = f'{{__id:"client:{_tkey(tid)}:{suffix}'
    out, i = [], 0
    while True:
        i = h.find(pat, i)
        if i < 0:
            return out
        out.append(h[i:i + span])
        i += 1


_STATUS_HREF = re.compile(
    r'(?:data-href|href)="/([A-Za-z0-9_]+)/status/(\d+)(?:/[a-z0-9_/?=&-]*)?"')
_JSTR = r'((?:[^"\\]|\\.)*)'  # JS 字符串字面量体

# x.com 命名空间前缀，渲染 DOM 的 /i/web/status/、/search?q=..&f=live 等会
# 误中 author 捕获组 —— 不是真 handle。
_NON_AUTHOR = {"i", "web", "search", "home", "explore", "hashtag",
               "notifications", "messages", "compose", "settings", "login",
               "signup", "tos", "privacy", "about", "download", "i18n",
               "help", "ads", "business", "status"}


def parse_profile(h: str) -> tuple[dict, list]:
    """SSR HTML -> (user{name,screen_name}, tweets[按时间倒序，已去重])。

    tweet dict: id/author/url/text/created_at_ms/counts/lang/urls/media/
    note_tweet/retweet。
    """
    seen, order = set(), []
    for author, tid in _STATUS_HREF.findall(h):
        if author.lower() in _NON_AUTHOR:  # /i/status/、/i/web/status/ 等占位
            continue
        if tid not in seen:
            seen.add(tid)
            order.append((author, tid))

    tweets = []
    for author, tid in order:
        rec = {"id": tid, "author": author,
               "url": f"https://x.com/{author}/status/{tid}"}
        det = _first_rec(h, f'{{__id:"client:{_tkey(tid)}:details"', 6000)
        m = re.search(r'full_text:"' + _JSTR + '"', det)
        if m:
            rec["text"] = _unesc(m.group(1))
        m = re.search(r"created_at_ms:(\d+)", det)
        if m:
            rec["created_at_ms"] = int(m.group(1))

        cnt = _first_rec(h, f'{{__id:"client:{_tkey(tid)}:counts"', 900)
        for k in ("reply_count", "retweet_count", "favorite_count",
                  "quote_count", "bookmark_count"):
            m = re.search(k + r":(\d+)", cnt)
            if m:
                rec[k] = int(m.group(1))

        leg = _first_rec(h, f'{{__id:"client:{_tkey(tid)}:legacy"', 900)
        m = re.search(r'lang:"([a-zA-Z-]+)"', leg)
        if m:
            rec["lang"] = m.group(1)

        # 链接实体：短推文 url_entities:N + 长推文 entity_set:urls:N
        urls = []
        for rec_win in _recs(h, tid, "url_entities:", 900):
            m = re.search(r'expanded_url:"' + _JSTR + '"', rec_win)
            if m:
                urls.append(_unesc(m.group(1)))
        nk = _nkey(tid)
        pat = f'{{__id:"client:{nk}:entity_set:urls:'
        i = 0
        while True:
            i = h.find(pat, i)
            if i < 0:
                break
            m = re.search(r'expanded_url:"' + _JSTR + '"', h[i:i + 900])
            if m:
                urls.append(_unesc(m.group(1)))
            i += 1
        rec["urls"] = list(dict.fromkeys(urls))[:6]

        # 媒体：media_entities2:N 记录各取首个 media_url_https
        media = []
        for rec_win in _recs(h, tid, "media_entities2:", 1600):
            m = re.search(r'media_url_https:"' + _JSTR + '"', rec_win)
            if m:
                media.append(_unesc(m.group(1)))
        rec["media"] = media[:4]

        # 长推文：NoteTweet 记录无 client: 前缀，键 = b64("NoteTweet:"+id)
        nt = _first_rec(h, f'{{__id:"{nk}"', 24000)
        m = re.search(r'text:"' + _JSTR + '"', nt)
        if m:
            nt_text = _unesc(m.group(1))
            if len(nt_text) > len(rec.get("text", "")):
                rec["text"] = nt_text
                rec["note_tweet"] = True
        tweets.append(rec)

    user = {}
    m = re.search(
        r'UserCore",name:"' + _JSTR + r'",screen_name:"' + _JSTR + '"', h)
    if m:
        user = {"name": _unesc(m.group(1)), "screen_name": m.group(2)}
    return user, tweets


# ------------------------------------------------------- playwright fallback --

def _fetch_playwright(handle: str, proxy: Optional[str],
                      timeout: float) -> tuple[Optional[str], int]:
    """无头 Chromium 渲染 x.com/<handle> → (page.content(), http_status)。

    playwright 不在 PEP723 deps 内（懒加载）；缺席/异常 → (None, 0)。
    实测 x.com 对无头浏览器直接 403 空页（PLAN §shot_policy 同源结论：
    x.com 截图→403 占位卡），status 带回供调用方按 walled 分类。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None, 0
    url = f"https://x.com/{handle}"
    try:
        with sync_playwright() as pw:
            kw = {"headless": True}
            if proxy and proxy != "direct":
                kw["proxy"] = {"server": proxy}
            br = pw.chromium.launch(**kw)
            try:
                ctx = br.new_context(user_agent=UA, locale="en-US",
                                     viewport={"width": 1280, "height": 1600})
                pg = ctx.new_page()
                resp = pg.goto(url, wait_until="domcontentloaded",
                               timeout=int(timeout * 1000))
                status = resp.status if resp else 0
                try:
                    pg.wait_for_selector(
                        'a[data-href*="/status/"],article,'
                        'a[href*="/status/"]', timeout=15000)
                except Exception:
                    pass
                return pg.content(), status
            finally:
                br.close()
    except Exception:
        return None, 0


# -------------------------------------------------------------- raw_item ----

def _iso_ms(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000, timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _utcnow() -> str:
    return (datetime.now(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


_HASHTAG = re.compile(r"#([\w一-鿿]+)")


def _tweet_to_item(tw: dict, user: dict, handle: str, *,
                   src: dict, fetch: dict, fetched_at: str,
                   raw_ref: Optional[str]) -> dict:
    text = tw.get("text", "") or ""
    title = normalize.title_norm(text)
    if len(title) > 100:
        title = title[:99].rstrip() + "…"
    if not title:
        title = f"@{tw.get('author') or handle} status {tw['id']}"
    canon = normalize.url_canon(tw["url"])
    tags = _HASHTAG.findall(text)[:8]
    if (tw.get("author") or "").lower() != handle.lower():
        tags.append("retweet")  # 外站作者 status 出现在本 profile → 转发/引用
    item = {
        "schema": "raw_item/1",
        "item_key": normalize.item_key(tw["url"]),
        "id": normalize.item_key(tw["url"]),
        "url": tw["url"],
        "url_canon": canon,
        "title": title,
        "content_text": text or None,
        "date_published": (_iso_ms(tw["created_at_ms"])
                           if tw.get("created_at_ms") else None),
        "date_fetched": fetched_at,
        "language": tw.get("lang"),
        "tags": tags,
        "image": tw["media"][0] if tw.get("media") else None,
        "_source": {
            "name": src.get("name") or f"x/{handle}",
            "feed_url": src.get("feed_url") or f"https://x.com/{handle}",
            "kind": "scrape",
            "item_guid": tw["id"],
        },
        "_fetch": fetch,
    }
    if raw_ref:
        item["_raw_ref"] = raw_ref
    return item


# ---------------------------------------------------------------- fetch ----

def fetch_user(handle: str, cfg: Optional[dict] = None) -> list:
    """x.com/<handle> 登出 SSR → [raw_item dict]（契约 raw_item/1，§4）。

    空壳页（200 但零推文数据）→ ShellOnly；传输/HTTP 失败 → FetchError。
    cfg 见模块 docstring；返回顺序 = 页面顺序（新→旧），按 max_items 截断。
    """
    cfg = dict(cfg or {})
    handle = (handle or "").strip().lstrip("@")
    if not handle:
        raise ValueError("empty handle")
    url = f"https://x.com/{handle}"
    proxy = _resolve_proxy(cfg)
    timeout = float(cfg.get("timeout", 20))

    res = lib_http.get(
        url, proxy=proxy, timeout=timeout, retries=1,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    if res.status == 200 and res.body:
        html = res.text
    elif res.error == "shell_only":
        raise ShellOnly(status=res.status, detail="http.py classify=shell_only")
    else:
        raise FetchError(res.error or f"http_{res.status}",
                         res.detail, res.status)

    user, tweets = parse_profile(html)
    pw_status = 0
    if not tweets and cfg.get("playwright_fallback"):
        pw_html, pw_status = _fetch_playwright(handle, proxy, timeout)
        if pw_html:
            user, tweets = parse_profile(pw_html)
            if tweets:
                html = pw_html
    if not tweets:
        # 200 有正文但解析为空：分类器若已给出 walled/rate_limited 等实因
        # 用 FetchError 上报（健康分类更准）；pw 兜底拿到 403/429 同理。
        # http.py 判 shell_only（可见文本极少的挂载点页）与"解析不出数据"
        # 两种情况都抛 ShellOnly —— 调用方只按异常类型转下一路。
        if pw_status in (401, 403):
            raise FetchError("walled", f"playwright http_{pw_status}",
                             pw_status)
        if pw_status == 429:
            raise FetchError("rate_limited", "playwright http_429", 429)
        if res.error == "shell_only":
            raise ShellOnly(status=res.status,
                            detail="http.py classify=shell_only 且无推文记录")
        if res.error not in ("ok", "empty"):
            raise FetchError(res.error, res.detail, res.status)
        raise ShellOnly(status=res.status,
                        detail="SSR 数据字段缺失（无 status 链接/无 $R 记录）")

    max_items = int(cfg.get("max_items")
                    or cfg.get("max_items_per_source") or 30)
    tweets = tweets[:max_items]

    raw_ref = None
    run_dir = cfg.get("run_dir")
    if run_dir:
        src_name = cfg.get("name") or f"x_{handle.lower()}"
        raw_ref = lib_http.save_raw(run_dir, src_name, url,
                                    html.encode("utf-8"), ext=".html")

    fetch = {
        "status": res.status,
        "via": "direct",            # 契约枚举 direct|mirror|cache|manual：
        "reachable": True,          # x.com 源站取数（经本地代理仍属 direct，
        "etag": res.etag,           #   非镜像）；pw 兜底同为源站取数
        "content_sha256": hashlib.sha256(
            html.encode("utf-8")).hexdigest()[:16],
    }
    src = {"name": cfg.get("name"), "feed_url": cfg.get("feed_url") or url}
    fetched_at = _utcnow()
    return [
        _tweet_to_item(tw, user, handle, src=src, fetch=fetch,
                       fetched_at=fetched_at, raw_ref=raw_ref)
        for tw in tweets
    ]


# ------------------------------------------------------------- self test ----

def _fixture() -> str:
    tid = "1111111111111111111"
    tk = _tkey(tid)
    nk = _nkey(tid)
    return f'''<html><body>
<a data-href="/OpenAI/status/{tid}">t</a>
<a data-href="/claudeai/status/2222222222222222222">r</a>
{{__id:"client:{tk}:details",full_text:"hello \\"world\\" \\nnext line",
created_at_ms:1788463933000}}
{{__id:"client:{tk}:counts",reply_count:3,retweet_count:9,
favorite_count:42,quote_count:1,bookmark_count:7}}
{{__id:"client:{tk}:legacy",lang:"en"}}
{{__id:"client:{tk}:url_entities:0",display_url:"openai.com",
expanded_url:"https://openai.com/news/x?utm_source=tw"}}
{{__id:"client:{tk}:media_entities2:0",type:"photo",
media_url_https:"https://pbs.twimg.com/media/abc.jpg"}}
{{__id:"client:{_tkey('2222222222222222222')}:details",
full_text:"retweeted body",created_at_ms:1788463900000}}
{{__id:"{nk}",__typename:"NoteTweet",rest_id:"{tid}",
text:"a much longer note tweet body that exceeds the short full_text"}}
UserCore",name:"OpenAI",screen_name:"OpenAI"
</body></html>'''


def _check_item_shape(it: dict) -> None:
    extra = set(it) - _RAW_ITEM_KEYS
    assert not extra, f"contract extra keys: {extra}"
    for k in ("schema", "item_key", "id", "url", "url_canon", "title",
              "date_fetched", "_source", "_fetch"):
        assert k in it, f"missing {k}"
    assert re.fullmatch(r"[0-9a-f]{16}", it["item_key"]), it["item_key"]
    assert it["id"] == it["item_key"]
    assert it["_source"]["kind"] == "scrape"
    assert it["_fetch"]["via"] in ("direct", "mirror", "cache", "manual")


def _live(handle: str, cfg: dict) -> list:
    try:
        items = fetch_user(handle, cfg)
        print(f"[live @{handle}] HIT {len(items)} items; "
              f"first: {items[0]['date_published']} "
              f"{items[0]['title'][:70]!r}")
        for it in items:
            _check_item_shape(it)
        return items
    except ShellOnly as e:
        print(f"[live @{handle}] MISS shell_only status={e.status} {e.detail}")
    except FetchError as e:
        print(f"[live @{handle}] MISS fetch error={e.error} {e.detail}")
    return []


if __name__ == "__main__":
    # ---- 离线夹具 ----------------------------------------------------------
    user, tweets = parse_profile(_fixture())
    assert user == {"name": "OpenAI", "screen_name": "OpenAI"}, user
    assert len(tweets) == 2, tweets
    t0 = tweets[0]
    assert t0["note_tweet"] and "longer note tweet" in t0["text"]
    assert t0["created_at_ms"] == 1788463933000
    assert t0["favorite_count"] == 42 and t0["reply_count"] == 3
    assert t0["lang"] == "en"
    assert t0["urls"] == ["https://openai.com/news/x?utm_source=tw"], t0["urls"]
    assert t0["media"] == ["https://pbs.twimg.com/media/abc.jpg"]
    assert tweets[1]["author"] == "claudeai"
    print("fixture parse OK ->", len(tweets), "tweets")

    # 空壳检测：全字段缺失 → 0 tweets（fetch_user 判 ShellOnly 的依据）
    shell_html = ('<html><body><div id="root"></div>'
                  "<noscript>JavaScript is not available</noscript></body></html>")
    u2, t2 = parse_profile(shell_html)
    assert t2 == [] and u2 == {}
    print("shell detection OK (0 tweets on shell page)")

    # 真实抓档回放（实验目录若在则跑；缺失不阻塞）
    exp = (REPO_ROOT / "experiments" / "hard-x.com-scraper-tool-or-manual"
           / "profile_karpathy.html")
    if exp.is_file():
        u3, t3 = parse_profile(exp.read_text(encoding="utf-8",
                                             errors="replace"))
        assert len(t3) >= 5, len(t3)
        assert any(t["author"] != "karpathy" for t in t3), "expect a retweet"
        assert all(t.get("text") for t in t3), "empty tweet text"
        print(f"replay {exp.name}: {len(t3)} tweets, "
              f"retweets={sum(1 for t in t3 if t['author'] != 'karpathy')}")

    # raw_item 形状（手写键集 == contracts RawItem extra='forbid' 全集）
    it = _tweet_to_item(tweets[0], user, "OpenAI",
                        src={"name": "x_openai",
                             "feed_url": "https://x.com/OpenAI"},
                        fetch={"status": 200, "via": "direct",
                               "reachable": True, "etag": None,
                               "content_sha256": "0" * 16},
                        fetched_at=_utcnow(), raw_ref=None)
    _check_item_shape(it)
    assert "retweet" not in it["tags"]
    it_rt = _tweet_to_item(tweets[1], user, "OpenAI",
                           src={}, fetch={"status": 200}, fetched_at=_utcnow(),
                           raw_ref=None)
    assert "retweet" in it_rt["tags"]
    assert it["url_canon"] == it["url"], "clean status url should be canonical"
    try:  # pydantic 可用时上真契约校验
        from contracts.models import RawItem
        RawItem.model_validate(it)
        print("contracts.RawItem validation OK")
    except ImportError:
        print("contracts validation skipped (no pydantic in ephemeral env)")

    # ---- 活网实测 ----------------------------------------------------------
    if "--offline" not in sys.argv:
        cfg = {"proxy": "required", "proxy_url": DEFAULT_PROXY,
               "timeout": 25, "name": "selftest"}
        n = 0
        for h in ("OpenAI", "AnthropicAI"):
            n += len(_live(h, dict(cfg, name=f"x_{h.lower()}")))
        if n == 0:
            print("WARN: live SSR route全部 miss —— 若是代理/网络问题属降级，"
                  "非解析器缺陷；可用 --offline 验证解析逻辑")
    else:
        print("live checks skipped (--offline)")

    print("x_ssr.py self-test OK")
