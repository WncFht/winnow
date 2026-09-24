#!/usr/bin/env python3
"""Reddit collector (docs/PLAN.md §5.3, Tier B platform collector).

Port of experiments/hard-reddit.com-official-api-or-native-feed/fetch_reddit.sh
+ loid_token.json — route A, the recommended path per that experiment's
RESULTS.md (2026-09-21): anonymous "loid" OAuth gives a ~24h bearer token and
100 QPM on oauth.reddit.com; the anonymous .rss/.json surfaces are either dead
(.json 403, old.reddit login-wall) or throttled to ~1 req/45-60s (.rss), so .rss
is kept only as a zero-credential fallback.

Public API
----------
fetch_sub(sub, cfg) -> list[dict]
    Mint/load the anonymous token (cached in state/reddit_token.json), pull the
    subreddit listing via oauth.reddit.com JSON (`.rss` Atom fallback when
    OAuth is unreachable), and return raw_item/1-shaped dicts:
      url            = outbound link for link posts, comments permalink for
                       self/media posts (cross-source url_hash dedup works on
                       the article URL — PLAN §4 item_key semantics)
      title          = post title (title_norm'd)
      content_text   = selftext + a "[r/<sub> · score N · M comments · permalink]"
                       footer (score/permalink stay visible to the filter LLM)
      date_published = created_utc normalized to RFC3339 UTC
      _source        = {name, feed_url=<actual request URL>, kind=api|rss,
                        item_guid=t3_fullname}
      _fetch         = {status, via=direct, reachable, etag, content_sha256}
      _raw_ref       = data/raw_cache path when cfg carries run_dir

cfg keys consulted (all optional): limit | max_items_per_source, sort
(new|hot|top|rising), feed_url (fallback endpoint + sort/limit hints),
proxy_url | proxy_http | proxy{http}|proxy-mode string, state_path | token_path,
run_dir, source_name | name, timeout, min_interval_s (default 2.0 = 30 rpm),
rss_min_interval_s (default 60 per RESULTS.md), rss_fallback (default True),
client_id, user_agent.

Errors raise RedditError with .error in the shared taxonomy
(timeout|dns_fail|http_<code>|rate_limited|walled|parse_error|proxy_unavailable)
so collect.py can feed 11_raw_manifest source health verbatim.

Selftest:  uv run stages/lib/reddit_collect.py            # live r/LocalLLaMA
           uv run stages/lib/reddit_collect.py --offline  # fixture parse only
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Same guard as lib/http.py: run directly, stages/lib is sys.path[0] and this
# file's siblings shadow stdlib `http`/`store` packages that httpx imports —
# drop our own dir before importing httpx. `from stages.lib import reddit_collect`
# (collect.py) never puts stages/lib on sys.path, so this is a no-op then.
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]

import httpx

from stages.lib.normalize import item_key, title_norm, url_canon

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DEFAULT = REPO_ROOT / "state" / "reddit_token.json"
FIXTURE = REPO_ROOT / "stages" / "lib" / "fixtures" / "reddit_oauth_hot.json"

# --- loid OAuth constants (fetch_reddit.sh / RESULTS.md route A) --------------
TOKEN_URL = "https://www.reddit.com/auth/v2/oauth/access-token/loid"
CLIENT_ID = "ohXpoqrZYub1kg"          # Reddit official Android client_id (public)
OAUTH = "https://oauth.reddit.com"
REDDIT_UA = "Reddit/2025.45.0/Android 14"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
              "winnow/0.1")
SCOPES = ["*", "email", "pii"]
TOKEN_MARGIN_S = 600                  # re-mint this many seconds before expiry
RPM = 30                              # PLAN §5.3: ≤30 rpm pacing
RSS_RPM = 1                           # route B measured budget ≈1 req/45-60s
SORTS = ("new", "hot", "top", "rising", "best")
_REDDIT_HOSTS = ("reddit.com", "redd.it")


class RedditError(Exception):
    """Fetch failure carrying the shared error taxonomy (.error attr)."""

    def __init__(self, error: str, detail: Optional[str] = None,
                 status: int = 0):
        super().__init__(f"{error}: {detail or ''}".rstrip(": "))
        self.error = error
        self.detail = detail
        self.status = status


# ------------------------------------------------------------------ cfg -----

def _opt(cfg: dict, *keys, default=None):
    """Dotted-path lookup over a merged sources.yaml-entry + global config."""
    for k in keys:
        cur = cfg
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur is not None:
            return cur
    return default


def _proxy_mode(cfg: dict) -> str:
    p = cfg.get("proxy")
    if isinstance(p, str) and p in ("required", "prefer", "direct_only"):
        return p
    return _opt(cfg, "proxy_mode", "proxy.mode", default="prefer")


def _repo_proxy() -> Optional[str]:
    """config.yaml / config.example.yaml 的 proxy.http——env 缺省时的兜底。
    （本机 clash 127.0.0.1:7890 是示例不是默认；无配置 → None = 直连。）"""
    try:
        import yaml
        for name in ("config.yaml", "config.example.yaml"):
            p = REPO_ROOT / name
            if p.is_file():
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                px = doc.get("proxy")
                if isinstance(px, dict) and px.get("http"):
                    return str(px["http"])
    except Exception:
        pass
    return None


def _proxy_url(cfg: dict) -> Optional[str]:
    """显式 cfg 键 > *_proxy env > config.yaml proxy.http > None（默认空 =
    不用代理直连；本机 clash 127.0.0.1:7890 是示例不是默认）。"""
    p = cfg.get("proxy")
    if isinstance(p, str) and "://" in p:
        return p
    if isinstance(p, dict) and p.get("http"):
        return p["http"]
    u = _opt(cfg, "proxy_url", "http_proxy", "proxy.http", "config.proxy.http")
    if u:
        return u
    env = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
           or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
    return env or _repo_proxy()


def _state_path(cfg: dict) -> Path:
    p = _opt(cfg, "state_path", "token_path", "reddit_token_path")
    return Path(p) if p else STATE_DEFAULT


def _timeout(cfg: dict) -> float:
    try:
        return float(_opt(cfg, "timeout", default=30))
    except (TypeError, ValueError):
        return 30.0


def sub_from_feed_url(feed_url: str) -> Optional[str]:
    """'https://www.reddit.com/r/LocalLLaMA/new/.rss?limit=50' -> 'LocalLLaMA'."""
    m = re.search(r"/r/([A-Za-z0-9_]+)/", feed_url or "")
    return m.group(1) if m else None


def _sort_from(cfg: dict) -> str:
    s = cfg.get("sort")
    if isinstance(s, str) and s in SORTS:
        return s
    m = re.search(r"/r/[A-Za-z0-9_]+/([a-z]+)", cfg.get("feed_url") or "")
    return m.group(1) if m and m.group(1) in SORTS else "new"


def _limit_from(cfg: dict) -> int:
    for k in ("limit", "max_items_per_source"):
        try:
            v = int(cfg.get(k) or 0)
            if v > 0:
                return min(v, 100)
        except (TypeError, ValueError):
            pass
    m = re.search(r"[?&]limit=(\d+)", cfg.get("feed_url") or "")
    if m:
        return min(int(m.group(1)), 100)
    return 50


# ---------------------------------------------------------- state / pace ----

def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # cache loss is non-fatal; token just gets re-minted next run


def _pace(state: dict, min_interval: float) -> None:
    """Sleep until min_interval since the previous request (persisted ts).

    last_request_wall lives in the token state file, so pacing survives across
    collect invocations — the RSS route's 60 s budget relies on this."""
    last = float(state.get("last_request_wall") or 0)
    wait = min_interval - (time.time() - last) if last else 0
    if wait > 0:
        time.sleep(wait)


def _touch(state: dict) -> None:
    state["last_request_wall"] = time.time()


# -------------------------------------------------------------- transport ---

def _client(cfg: dict, ua: str) -> httpx.Client:
    proxy = _proxy_url(cfg) if _proxy_mode(cfg) != "direct_only" else None
    if _proxy_mode(cfg) == "required" and not proxy:
        raise RedditError("proxy_unavailable",
                          "source marks proxy:required but no proxy URL "
                          "resolved (cfg.proxy_url / proxy.http / env)")
    return httpx.Client(
        proxy=proxy,
        trust_env=False,
        timeout=httpx.Timeout(_timeout(cfg)),
        follow_redirects=True,
        headers={"User-Agent": ua, "Accept": "*/*"},
        limits=httpx.Limits(max_connections=4),
    )


def _request(client: httpx.Client, method: str, url: str, state: dict,
             min_interval: float, attempts: int = 2, **kw) -> httpx.Response:
    """Paced request; transport failures retry once then raise RedditError."""
    last_exc: Optional[Exception] = None
    for i in range(max(1, attempts)):
        _pace(state, min_interval)
        try:
            r = client.request(method, url, **kw)
            if r.status_code == 429:
                raise RedditError("rate_limited",
                                  f"429 on {url} "
                                  f"(x-ratelimit-remaining="
                                  f"{r.headers.get('x-ratelimit-remaining')})",
                                  status=429)
            return r
        except httpx.TimeoutException as e:
            last_exc = e
            err = "timeout"
        except httpx.ConnectError as e:
            last_exc = e
            err = ("dns_fail" if re.search(
                r"name or service not known|temporary failure in name "
                r"resolution|getaddrinfo|no address associated", str(e), re.I)
                else "http_0")
        except httpx.HTTPError as e:
            last_exc = e
            err = "http_0"
        finally:
            _touch(state)
        if i < attempts - 1:
            time.sleep(min(2.0, 0.4 * (2 ** i)))
    raise RedditError(err, f"{type(last_exc).__name__}: {last_exc}")


# ------------------------------------------------------------------ OAuth ---

def _mint_token(client: httpx.Client, cfg: dict, state: dict) -> dict:
    """POST /auth/v2/oauth/access-token/loid — anonymous 24h bearer token."""
    cid = _opt(cfg, "client_id", default=CLIENT_ID)
    device = state.get("device_id") or str(uuid.uuid4())
    state["device_id"] = device
    r = _request(
        client, "POST", TOKEN_URL, state,
        float(_opt(cfg, "min_interval_s", default=60.0 / RPM)),
        auth=(cid, ""),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Reddit-Device-Id": device,
        },
        json={"scopes": SCOPES},
    )
    if r.status_code != 200:
        raise RedditError(f"http_{r.status_code}",
                          f"loid token mint failed: {r.text[:200]}",
                          status=r.status_code)
    try:
        data = r.json()
        tok = data["access_token"]
    except Exception as e:
        raise RedditError("parse_error", f"loid token body: {e}")
    state.update({
        "access_token": tok,
        "token_type": data.get("token_type", "bearer"),
        "expiry_ts": time.time() + int(data.get("expires_in", 3600))
                     - TOKEN_MARGIN_S,
        "loid": str(uuid.uuid4()),
        "minted_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(_state_path(cfg), state)
    return state


def _ensure_token(client: httpx.Client, cfg: dict,
                  force: bool = False) -> dict:
    path = _state_path(cfg)
    state = _load_state(path)
    if (not force and state.get("access_token")
            and float(state.get("expiry_ts") or 0) > time.time()):
        return state
    return _mint_token(client, cfg, state)


# ------------------------------------------------------------- JSON route ---

def _thumb(d: dict) -> Optional[str]:
    t = d.get("thumbnail")
    if isinstance(t, str) and t.startswith("http"):
        return t
    return None  # "self" | "default" | "nsfw" | "spoiler" | ""


def _outbound(d: dict) -> str:
    """Outbound article URL for link posts; '' for self posts."""
    if d.get("is_self"):
        return ""
    u = d.get("url_overridden_by_dest") or d.get("url") or ""
    host = re.sub(r"^https?://(www\.|old\.|np\.)?", "", u).split("/", 1)[0]
    if host and not any(host == h or host.endswith("." + h)
                        for h in _REDDIT_HOSTS):
        return u
    if host.endswith("redd.it"):      # i.redd.it / v.redd.it hosted media
        return u
    return ""


def _meta_footer(sub: str, d: dict, permalink: str) -> str:
    return (f"[r/{sub} · score {d.get('score', 0)} · "
            f"{d.get('num_comments', 0)} comments · {permalink}]")


def _children_to_items(children: list, sub: str, cfg: dict,
                       fetch_meta: dict) -> list[dict]:
    src_name = _opt(cfg, "source_name", "name", default=f"r/{sub}")
    feed_url = fetch_meta["request_url"]
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for ch in children:
        if not isinstance(ch, dict) or ch.get("kind") != "t3":
            continue
        d = ch.get("data") or {}
        title = title_norm(d.get("title") or "")
        permalink_path = d.get("permalink") or ""
        permalink = ("https://www.reddit.com" + permalink_path
                     if permalink_path.startswith("/") else permalink_path)
        outbound = _outbound(d)
        url = outbound or permalink
        if not (title and url):
            continue
        st = (d.get("selftext") or "").strip()
        if st in ("[removed]", "[deleted]"):
            st = ""
        content_text = (st + "\n\n" if st else "") + _meta_footer(
            sub, d, permalink)
        ts = d.get("created_utc")
        published = (datetime.fromtimestamp(float(ts), tz=timezone.utc)
                     .isoformat() if ts else None)
        tags = ["reddit", f"r/{sub}"]
        flair = d.get("link_flair_text")
        if flair:
            tags.append(str(flair))
        if d.get("over_18"):
            tags.append("nsfw")
        if d.get("stickied"):
            tags.append("stickied")
        key = item_key(url)
        out.append({
            "schema": "raw_item/1",
            "item_key": key,
            "id": key,
            "url": url,
            "url_canon": url_canon(url),
            "title": title,
            "content_text": content_text,
            "date_published": published,
            "date_fetched": now,
            "language": "en",
            "tags": tags,
            "image": _thumb(d),
            "_source": {
                "name": src_name,
                "feed_url": feed_url,
                "kind": "api",
                "item_guid": d.get("name"),          # t3_fullname
            },
            "_fetch": dict(fetch_meta["fetch"]),
            "_raw_ref": fetch_meta.get("raw_ref"),
        })
    return out


def _fetch_json_route(client: httpx.Client, sub: str, cfg: dict) -> list[dict]:
    sort, limit = _sort_from(cfg), _limit_from(cfg)
    url = f"{OAUTH}/r/{sub}/{sort}?limit={limit}&raw_json=1"
    min_iv = float(_opt(cfg, "min_interval_s", default=60.0 / RPM))

    resp = None
    for attempt in (1, 2):
        state = _ensure_token(client, cfg, force=(attempt == 2))
        r = _request(client, "GET", url, state, min_iv, headers={
            "Authorization": f"Bearer {state['access_token']}",
            "x-reddit-loid": state.get("loid") or str(uuid.uuid4()),
        })
        _save_state(_state_path(cfg), state)
        if r.status_code == 401 and attempt == 1:
            continue                        # expired early → re-mint once
        resp = r
        break
    if resp is None or resp.status_code != 200:
        raise RedditError(
            f"http_{resp.status_code if resp is not None else 0}",
            f"listing {url}: {(resp.text[:200] if resp is not None else '')}",
            status=resp.status_code if resp is not None else 0)
    try:
        payload = resp.json()
        children = payload["data"]["children"]
    except Exception as e:
        raise RedditError("parse_error", f"listing JSON: {e}")

    meta = {
        "request_url": url,
        "fetch": {
            "status": resp.status_code,
            "via": "direct",                # contract enum; proxy = direct
            "reachable": True,
            "etag": resp.headers.get("etag"),
            "content_sha256": hashlib.sha256(resp.content).hexdigest()[:16],
        },
    }
    if cfg.get("run_dir"):
        try:
            from stages.lib.http import save_raw
            meta["raw_ref"] = save_raw(cfg["run_dir"], sub, url, resp.content)
        except Exception:
            pass                            # raw cache is non-fatal
    return _children_to_items(children, sub, cfg, meta)


# -------------------------------------------------------------- RSS route ---

_MD_DIV = re.compile(r'<div class="md">(.*?)<!-- SC_ON', re.S)
_LINK_A = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _rss_text(content_html: str) -> str:
    """selftext inside <div class="md">; '' for link posts (no md block)."""
    m = _MD_DIV.search(content_html or "")
    if not m:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", m.group(1)))).strip()


def _rss_to_items(xml_body: bytes, sub: str, cfg: dict,
                  fetch_meta: dict) -> list[dict]:
    src_name = _opt(cfg, "source_name", "name", default=f"r/{sub}")
    now = datetime.now(timezone.utc).isoformat()
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as e:
        raise RedditError("parse_error", f"Atom parse: {e}")
    ns = {"a": "http://www.w3.org/2005/Atom",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in root.findall("a:entry", ns):
        def txt(tag):
            el = e.find(f"a:{tag}", ns)
            return (el.text or "").strip() if el is not None else ""

        title = title_norm(txt("title"))
        link_el = e.find("a:link", ns)
        permalink = link_el.get("href", "") if link_el is not None else ""
        content_html = txt("content")
        m = _LINK_A.search(content_html)
        outbound = html.unescape(m.group(1)) if m else ""
        out_host = re.sub(r"^https?://", "", outbound).split("/", 1)[0]
        if out_host == "reddit.com" or out_host.endswith(".reddit.com"):
            outbound = ""                       # [link]→permalink on self posts
        url = outbound or permalink
        if not (title and url):
            continue
        published = txt("published") or txt("updated") or None
        st = _rss_text(content_html)
        guid = txt("id") or None
        thumb_el = e.find("m:thumbnail", ns)
        if thumb_el is None:
            thumb_el = e.find("a:thumbnail", ns)
        thumb = thumb_el.get("url") if thumb_el is not None else None
        sub_label = ""
        cat = e.find("a:category", ns)
        if cat is not None:
            sub_label = cat.get("label", "")
        rss_sub = re.sub(r"^r/", "", sub_label) or sub
        content_text = (st + "\n\n" if st else "") + _meta_footer(
            rss_sub, {"score": "?", "num_comments": "?"}, permalink)
        key = item_key(url)
        out.append({
            "schema": "raw_item/1",
            "item_key": key,
            "id": key,
            "url": url,
            "url_canon": url_canon(url),
            "title": title,
            "content_text": content_text,
            "date_published": published,
            "date_fetched": now,
            "language": "en",
            "tags": ["reddit", f"r/{rss_sub}", "via:rss"],
            "image": thumb,
            "_source": {
                "name": src_name,
                "feed_url": fetch_meta["request_url"],
                "kind": "rss",
                "item_guid": guid,
            },
            "_fetch": dict(fetch_meta["fetch"]),
            "_raw_ref": fetch_meta.get("raw_ref"),
        })
    return out


def _fetch_rss_route(client: httpx.Client, sub: str, cfg: dict,
                     state: dict) -> list[dict]:
    feed = cfg.get("feed_url") or ""
    if ".rss" not in feed:
        limit = _limit_from(cfg)
        feed = f"https://www.reddit.com/r/{sub}/new/.rss?limit={limit}"
    min_iv = float(_opt(cfg, "rss_min_interval_s", default=60.0 / RSS_RPM))
    r = _request(client, "GET", feed, state, min_iv)
    _save_state(_state_path(cfg), state)
    if r.status_code != 200:
        raise RedditError(f"http_{r.status_code}",
                          f"rss {feed}: {r.text[:200]}", status=r.status_code)
    meta = {
        "request_url": feed,
        "fetch": {
            "status": r.status_code,
            "via": "direct",
            "reachable": True,
            "etag": r.headers.get("etag"),
            "content_sha256": hashlib.sha256(r.content).hexdigest()[:16],
        },
    }
    if cfg.get("run_dir"):
        try:
            from stages.lib.http import save_raw
            meta["raw_ref"] = save_raw(cfg["run_dir"], sub, feed, r.content)
        except Exception:
            pass
    return _rss_to_items(r.content, sub, cfg, meta)


# ------------------------------------------------------------------ public --

def fetch_sub(sub: str, cfg: Optional[dict] = None) -> list[dict]:
    """Fetch one subreddit -> raw_item/1 dicts.

    Route A (oauth.reddit.com + loid anonymous token) first; when the token
    cannot be minted or the listing is unreachable, fall back to the anonymous
    .rss feed (route B) unless cfg['rss_fallback'] is False.
    """
    cfg = dict(cfg or {})
    sub = re.sub(r"^(?:r/|/r/|/)", "", (sub or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_]+", sub):
        alt = sub_from_feed_url(cfg.get("feed_url") or "")
        if alt:
            sub = alt
        else:
            raise RedditError("parse_error", f"bad subreddit name: {sub!r}")

    oauth_err: Optional[RedditError] = None
    with _client(cfg, REDDIT_UA) as client:
        try:
            return _fetch_json_route(client, sub, cfg)
        except RedditError as e:
            oauth_err = e

    if not cfg.get("rss_fallback", True):
        raise oauth_err
    # route B: anonymous .rss — heavily throttled, separate slower pacing;
    # reload state so its last_request_wall reflects the oauth attempt
    state = _load_state(_state_path(cfg))
    with _client(cfg, BROWSER_UA) as client:
        try:
            return _fetch_rss_route(client, sub, cfg, state)
        except RedditError as rss_err:
            raise RedditError(
                rss_err.error,
                f"oauth={oauth_err.error}({oauth_err.detail}) ; "
                f"rss={rss_err.error}({rss_err.detail})",
                status=rss_err.status)


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":
    offline = "--offline" in sys.argv

    # --- offline: parse the verified live fixture --------------------------
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta = {
        "request_url": "https://oauth.reddit.com/r/MachineLearning/hot",
        "fetch": {"status": 200, "via": "direct", "reachable": True,
                  "etag": None, "content_sha256": "0" * 16},
    }
    items = _children_to_items(payload["data"]["children"], "MachineLearning",
                               {"name": "reddit_machinelearning"}, meta)
    assert len(items) == 5, len(items)
    self_post = items[0]
    assert self_post["url"].startswith(
        "https://www.reddit.com/r/MachineLearning/comments/"), self_post["url"]
    assert "score 77" in self_post["content_text"]
    assert self_post["_source"]["item_guid"] == "t3_1wjuki0"
    link_post = items[4]
    assert link_post["url"] == "https://i.redd.it/cwnmw2lc7kqh1.gif", \
        link_post["url"]
    assert re.fullmatch(r"[0-9a-f]{16}", link_post["item_key"])
    for it in items:
        assert it["schema"] == "raw_item/1" and it["id"] == it["item_key"]
        assert it["_fetch"]["status"] == 200
    print(f"offline fixture parse OK ({len(items)} items; "
          f"self→permalink, link→outbound)")

    # --- offline: RSS fixture ----------------------------------------------
    rss = (FIXTURE.parent / "rss_burst_1.xml").read_bytes()
    items_r = _rss_to_items(rss, "MachineLearning",
                            {"name": "reddit_machinelearning"}, meta)
    assert len(items_r) == 10, len(items_r)
    assert items_r[0]["_source"]["item_guid"].startswith("t3_")
    assert items_r[0]["content_text"]
    print(f"offline rss parse OK ({len(items_r)} items)")

    # --- contract validation ------------------------------------------------
    try:
        from contracts.models import RawItem
        for it in items + items_r:
            RawItem.model_validate(it)
        print("contract RawItem validation OK")
    except ImportError:
        print("WARN: contracts.models unavailable, skipped validation")

    if not offline:
        # 不显式给 proxy_url——走 env→config→None 默认链
        #（本机 clash 7890 是示例不是默认）
        got = fetch_sub("LocalLLaMA", {
            "limit": 25, "sort": "new",
            "name": "reddit_localllama",
        })
        assert got, "LocalLLaMA returned 0 items"
        print(f"live r/LocalLLaMA via oauth route -> {len(got)} items")
        first = got[0]
        print(f"  first: {first['title'][:70]}\n"
          f"         url={first['url'][:90]}\n"
          f"         published={first['date_published']} "
          f"guid={first['_source']['item_guid']}")
        try:
            from contracts.models import RawItem
            for it in got:
                RawItem.model_validate(it)
            print("live items contract-valid")
        except ImportError:
            pass
    else:
        print("live checks skipped (--offline)")
    print("reddit_collect.py self-test OK")
