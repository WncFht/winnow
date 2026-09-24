#!/usr/bin/env python3
"""X 平台采集器 ③ syndication 路（docs/PLAN.md §5.3 四路之三，429 指数退避）。

Endpoint（实测自 experiments/hard-x.com-official-api-or-native-feed/，
endpoint shape = X 官方 embedded-timeline widget 的 first-party JSON-in-HTML）：

    https://cdn.syndication.twimg.com/srv/timeline-profile/screen-name/<handle>
        ?dnt=true&lang=en&showReplies=false&frame=false
        &hideBorder=true&hideFooter=true&hideHeader=true&hideScrollBar=true
        &transparent=true

返回 HTML，内嵌 <script id="__NEXT_DATA__">JSON →
props.pageProps.timeline.entries[].content.tweet ≈ 最近 20 条
（full_text / created_at / permalink / entities.urls / extended_entities.media）。

限速（2026-09-21/22 实测，共享出口 IP）：
    x-rate-limit-limit: 30 / ~15min 窗口，per-IP；
    三个 host 计数器今日已合一（同 reset 同 remaining），仍按序轮换——
    昨日实测各 host 独立计数，轮换是免费保险。
    429 → 尊重 x-rate-limit-reset（sleep 到 reset+buffer，预算
    max_wait_s 封顶）；无 reset 头时指数退避 base*2^n；耗尽抛 RateLimited。

API:
    fetch_user(handle, cfg=None, *, run_dir=None, source_name=None,
               timeout=20) -> list[dict]   # raw_item/1 形状（§4 契约）
    fetch_user_meta(handle, cfg=None, **kw) -> {"items","meta"}
    load_cfg(path=None)                    -> x_collector.syndication + proxy
    parse_created_at(s)                    -> RFC3339 str | None

cfg 读取顺序：dict 直传（全 config 或 x_collector 子树均可）→
config.yaml → config.example.yaml。相关键：

    x_collector.syndication:
      endpoints: [cdn.syndication.twimg.com, syndication.twitter.com,
                  syndication.x.com]
      lang: en
      max_wait_s: 660        # 429 累计等待预算，超出即 RateLimited
      backoff_base_s: 2.0
      max_rounds: 3          # 传输错误整轮重试上限
      proxy: null            # "direct" 绕过；默认 config.proxy.http → env
    proxy.http: ""           # 示例 http://127.0.0.1:7890（本机 clash——是示例
                             # 不是默认）；空 → *_proxy env → 直连

异常：XSyndError（http/解析/传输，.status/.detail）；
      RateLimited(XSyndError)（.retry_after=epoch|None）。

CLI:  uv run stages/lib/x_synd.py            # live：抓 1-2 个 handle
      uv run stages/lib/x_synd.py --offline  # 只解析 lib/fixtures/ 缓存样本
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 直接运行本文件时 stages/lib 留在 sys.path[0]，本目录的 http.py 会 shadow
# stdlib `http`（httpx 依赖它）——剔除本目录；作为 lib.x_synd 导入时这是 no-op。
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]

import httpx  # noqa: E402

from stages.lib import meta, normalize  # noqa: E402
from stages.lib import http as lib_http  # noqa: E402  (save_raw 复用)

REPO = Path(__file__).resolve().parents[2]

DEFAULT_ENDPOINTS = (
    "cdn.syndication.twimg.com",
    "syndication.twitter.com",
    "syndication.x.com",
)
_PATH = "/srv/timeline-profile/screen-name/{handle}"
_QS = ("dnt=true&lang={lang}&showReplies=false&frame=false"
       "&hideBorder=true&hideFooter=true&hideHeader=true&hideScrollBar=true"
       "&transparent=true")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_RESET_BUF_S = 1.5      # x-rate-limit-reset 到点后多等的缓冲
_BACKOFF_CAP_S = 60.0   # 无 reset 头时单次退避上限
_TITLE_MAX = 100


# ---------------------------------------------------------------- errors ----

class XSyndError(RuntimeError):
    """syndication 路失败（非 429-耗尽：http/解析/传输）。"""

    def __init__(self, msg: str, *, status: int | None = None,
                 detail: str = ""):
        super().__init__(msg)
        self.status = status
        self.detail = detail[:500]


class RateLimited(XSyndError):
    """429 退避/等待预算耗尽。retry_after=最早 reset epoch（未知则 None）。"""

    def __init__(self, msg: str, *, retry_after: int | None = None,
                 detail: str = ""):
        super().__init__(msg, status=429, detail=detail)
        self.retry_after = retry_after


# ---------------------------------------------------------------- config ----

def load_cfg(path: str | Path | None = None) -> dict:
    """读 config.yaml（缺省 config.example.yaml）→ 扁平化 syndication 配置。"""
    return resolve_cfg(meta.load_config(path))


def resolve_cfg(cfg: Any) -> dict:
    """接受全 config dict / x_collector 子树 / syndication 子树，归一成运行配置。"""
    d = dict(cfg or {})
    top_proxy = ((d.get("proxy") or {}).get("http")
                 if isinstance(d.get("proxy"), dict) else None)
    xc = d.get("x_collector") if isinstance(d.get("x_collector"), dict) else d
    if isinstance(xc.get("syndication"), dict):
        sc = xc["syndication"]
    elif any(k in xc for k in ("endpoints", "max_wait_s", "backoff_base_s")):
        sc = xc           # 已是 syndication/resolved 层级
    else:
        sc = {}
    out = {
        "endpoints": tuple(sc.get("endpoints") or DEFAULT_ENDPOINTS),
        "lang": sc.get("lang") or "en",
        "max_wait_s": float(sc.get("max_wait_s", 660)),
        "backoff_base_s": float(sc.get("backoff_base_s", 2.0)),
        "max_rounds": int(sc.get("max_rounds", 3)),
        "proxy": sc.get("proxy", top_proxy),  # None → 走 env/trust_env
        "timeout": float(sc.get("timeout", 20)),
    }
    return out


def _proxy_arg(v: Any):
    """'direct'/'none'/'off' → httpx proxy=None 且 trust_env=False；URL → pin。"""
    if v in ("direct", "none", "off", False):
        return None, False
    if isinstance(v, str) and v:
        return v, False
    return None, True   # unset → honor env proxy


# ---------------------------------------------------------------- fetch -----

def _url(host: str, handle: str, lang: str) -> str:
    return (f"https://{host}{_PATH.format(handle=handle)}?"
            + _QS.format(lang=lang))


def _reset_epoch(r: httpx.Response) -> int | None:
    v = r.headers.get("x-rate-limit-reset")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _fetch_page(handle: str, sc: dict, timeout: float):
    """轮换 endpoint 抓时间线 HTML；429 走 reset 感知退避。

    -> (body:str, url:str, headers:httpx.Headers)
    """
    proxy, trust_env = _proxy_arg(sc.get("proxy"))
    deadline = time.monotonic() + sc["max_wait_s"]
    round_no = 0
    last_detail = ""
    with httpx.Client(proxy=proxy, trust_env=trust_env,
                      timeout=httpx.Timeout(timeout),
                      follow_redirects=True,
                      headers={"User-Agent": UA,
                               "Accept-Language": "en-US,en;q=0.9"}) as cli:
        while True:
            resets: list[int] = []
            terrs: list[str] = []
            herrs: list[str] = []
            for host in sc["endpoints"]:
                url = _url(host, handle, sc["lang"])
                try:
                    r = cli.get(url)
                except httpx.HTTPError as e:
                    terrs.append(f"{host}: {type(e).__name__} {e}")
                    continue
                if r.status_code == 200 and _NEXT_RE.search(r.text):
                    return r.text, url, r.headers
                if r.status_code == 429:
                    ep = _reset_epoch(r)
                    resets.append(ep if ep else int(time.time()) + 900)
                    last_detail = (
                        f"{host} 429 remaining="
                        f"{r.headers.get('x-rate-limit-remaining')} "
                        f"reset={ep}")
                    continue
                if r.status_code == 200:  # 200 但无 __NEXT_DATA__ → 结构变了
                    herrs.append(f"{host} 200-no-next_data({len(r.content)}B)")
                else:
                    herrs.append(f"{host} http_{r.status_code}")
            if resets:
                wait = max(1.0, min(resets) - time.time() + _RESET_BUF_S)
                if time.monotonic() + wait > deadline:
                    raise RateLimited(
                        f"syndication 429 on all hosts for @{handle}; "
                        f"reset in {int(wait)}s > budget {sc['max_wait_s']}s",
                        retry_after=min(resets), detail=last_detail)
                time.sleep(wait)
                round_no += 1
                continue
            # 无 429：纯 http 错直接抛；含传输错按指数退避重试整轮
            if herrs and not terrs:
                raise XSyndError(
                    f"syndication endpoints failed for @{handle}: {herrs}",
                    detail="; ".join(herrs))
            round_no += 1
            if round_no > sc["max_rounds"]:
                raise XSyndError(
                    f"syndication transport/http failed for @{handle} "
                    f"after {round_no - 1} rounds",
                    detail="; ".join(terrs + herrs))
            time.sleep(min(_BACKOFF_CAP_S,
                           sc["backoff_base_s"] * (2 ** round_no)))


# ---------------------------------------------------------------- parse -----

def parse_created_at(s: str | None) -> str | None:
    """'Thu Sep 03 19:32:13 +0000 2026' → RFC3339；已是 ISO 则归一输出。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(
            s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _entries(body: str) -> list[dict]:
    m = _NEXT_RE.search(body or "")
    if not m:
        raise XSyndError("no __NEXT_DATA__ in syndication page",
                         detail=f"size={len(body or '')}")
    try:
        data = json.loads(_html.unescape(m.group(1)))
    except json.JSONDecodeError as e:
        raise XSyndError("__NEXT_DATA__ JSON parse failed",
                         detail=str(e)) from e
    entries = (((data.get("props") or {}).get("pageProps") or {})
               .get("timeline") or {}).get("entries") or []
    out = []
    for e in entries:
        t = ((e or {}).get("content") or {}).get("tweet")
        if isinstance(t, dict) and (t.get("id_str") or t.get("id")):
            out.append(t)
    return out


def _permalink(t: dict, handle: str) -> str:
    tid = str(t.get("id_str") or t.get("id") or "")
    pl = t.get("permalink") or ""
    if pl.startswith("/"):
        return f"https://x.com{pl}"
    if pl.startswith("http"):
        return pl
    return f"https://x.com/{handle}/status/{tid}"


def _expand_urls(text: str, t: dict) -> str:
    for u in ((t.get("entities") or {}).get("urls") or []):
        short, long_ = u.get("url"), u.get("expanded_url")
        if short and long_:
            text = text.replace(short, long_)
    return text


def _to_raw_item(t: dict, handle: str, *, feed_url: str, fetched: str,
                 source_name: str, status: int, content_sha: str,
                 raw_ref: str | None) -> dict:
    tid = str(t.get("id_str") or t.get("id") or "")
    text = _expand_urls((t.get("full_text") or t.get("text") or ""), t)
    text = re.sub(r"\s+", " ", text).strip()
    url = _permalink(t, handle)
    media = ((t.get("extended_entities") or t.get("entities") or {})
             .get("media") or [])
    image = next((m.get("media_url_https") for m in media
                  if m.get("media_url_https")), None)
    tags = [h.get("text") for h in
            ((t.get("entities") or {}).get("hashtags") or []) if h.get("text")]
    canon = normalize.url_canon(url)
    return {
        "schema": "raw_item/1",
        "item_key": normalize.item_key(url),
        "id": normalize.item_key(url),
        "url": url,
        "url_canon": canon,
        "title": text if len(text) <= _TITLE_MAX
        else text[:_TITLE_MAX - 1].rstrip() + "…",
        "content_text": text or None,
        "date_published": parse_created_at(t.get("created_at")),
        "date_fetched": fetched,
        "language": t.get("lang"),
        "tags": tags,
        "image": image,
        "_source": {"name": source_name, "feed_url": feed_url,
                    "kind": "api", "item_guid": tid},
        "_fetch": {"status": status, "via": "direct", "reachable": True,
                   "etag": None, "content_sha256": content_sha},
        "_raw_ref": raw_ref,
    }


# ------------------------------------------------------------------ API -----

def fetch_user_meta(handle: str, cfg: Any = None, *,
                    run_dir: str | Path | None = None,
                    source_name: str | None = None,
                    timeout: float | None = None) -> dict:
    """抓 @handle 时间线 → {"items": [raw_item…], "meta": {...}}。

    cfg: 全 config dict / x_collector / syndication 子树 / yaml 路径 / None。
    兼容 x_collect 统一调用键：cfg["run_dir"|"name"|"_source_name"|
    "max_items"|"max_items_per_source"]；显式 kwarg 优先。
    run_dir 给了就把原始 HTML 落 data/raw_cache 并填 _raw_ref。
    """
    handle = (handle or "").lstrip("@").strip()
    if not handle:
        raise XSyndError("empty handle")
    if cfg is None or isinstance(cfg, (str, Path)):
        sc = load_cfg(cfg)
        cdict: dict = {}
    else:
        sc = resolve_cfg(cfg)
        cdict = dict(cfg)
    run_dir = run_dir if run_dir is not None else cdict.get("run_dir")
    source_name = (source_name or cdict.get("name")
                   or cdict.get("_source_name"))
    body, url, hdrs = _fetch_page(
        handle, sc, timeout or sc["timeout"])
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    raw_ref = None
    if run_dir is not None:
        raw_ref = lib_http.save_raw(
            run_dir, f"x_synd_{handle}", url, body)
    tweets = _entries(body)
    cap = int(cdict.get("max_items") or cdict.get("max_items_per_source")
              or 0) or None
    if cap:
        tweets = tweets[:cap]
    name = source_name or f"x.com/{handle}"
    items = [_to_raw_item(t, handle, feed_url=url, fetched=fetched,
                          source_name=name, status=200, content_sha=sha,
                          raw_ref=raw_ref)
             for t in tweets]
    meta = {"endpoint": url, "n": len(items),
            "rate_limit": {k: hdrs.get(k) for k in
                           ("x-rate-limit-limit", "x-rate-limit-remaining",
                            "x-rate-limit-reset")},
            "content_sha256": sha, "raw_ref": raw_ref}
    return {"items": items, "meta": meta}


def fetch_user(handle: str, cfg: Any = None, **kw) -> list[dict]:
    """@handle 最近 ~20 条 → raw_item/1 dict 列表（§4 契约字段）。"""
    return fetch_user_meta(handle, cfg, **kw)["items"]


# ------------------------------------------------------------- self test ----

_FIXTURES = [
    REPO / "stages/lib/fixtures/feed_xcom_variant.html",
    REPO / "stages/lib/fixtures/synd_test.html",
]


def _offline() -> int:
    from contracts.models import RawItem
    n_ok = 0
    for fx in _FIXTURES:
        if not fx.is_file():
            print(f"fixture missing: {fx}")
            continue
        body = fx.read_text(encoding="utf-8", errors="replace")
        tweets = _entries(body)
        assert tweets, f"no tweets in {fx.name}"
        items = [_to_raw_item(t, "OpenAI", feed_url="fixture://x",
                              fetched="2026-09-22T00:00:00+00:00",
                              source_name="x.com/OpenAI", status=200,
                              content_sha="0" * 16, raw_ref=None)
                 for t in tweets]
        for it in items:
            RawItem.model_validate(it)          # §4 契约校验
            assert re.fullmatch(r"[0-9a-f]{16}", it["item_key"])
            assert it["date_published"], f"bad created_at {it['id']}"
        n_ok += len(items)
        print(f"{fx.name}: {len(items)} raw_item OK, "
              f"first={items[0]['date_published']} "
              f"'{items[0]['title'][:50]}'")
    assert n_ok > 0, "no fixture parsed"
    # created_at 双格式
    assert parse_created_at("Thu Sep 03 19:32:13 +0000 2026") \
        == "2026-09-03T19:32:13+00:00"
    assert parse_created_at("2026-09-11T01:10:27.000Z") is not None
    print(f"offline OK: {n_ok} items validated against raw_item/1")
    return 0


def _live(handles: list[str], cfg_path: str | None) -> int:
    sc = load_cfg(cfg_path)
    print(f"endpoints={sc['endpoints']} max_wait_s={sc['max_wait_s']} "
          f"proxy={sc['proxy']!r}")
    rc = 0
    for h in handles:
        t0 = time.time()
        try:
            r = fetch_user_meta(h, sc)
        except RateLimited as e:
            print(f"@{h}: RATE_LIMITED retry_after={e.retry_after} "
                  f"({e}); detail={e.detail}")
            rc = 2
            continue
        except XSyndError as e:
            print(f"@{h}: FAIL {e} status={e.status} detail={e.detail}")
            rc = 1
            continue
        m = r["meta"]
        print(f"@{h}: {m['n']} items in {time.time() - t0:.1f}s "
              f"via {m['endpoint'].split('/')[2]} rl={m['rate_limit']}")
        for it in r["items"][:3]:
            print(f"   {it['date_published']} {it['title'][:60]!r} "
                  f"{it['url']}")
    return rc


if __name__ == "__main__":
    if "--offline" in sys.argv:
        sys.exit(_offline())
    hs = [a for a in sys.argv[1:] if not a.startswith("-")] or \
        ["OpenAI", "AnthropicAI"]
    sys.exit(_live(hs, os.environ.get("X_SYND_CFG")))
