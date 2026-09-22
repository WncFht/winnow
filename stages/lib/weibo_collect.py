#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.28",
# ]
# ///
"""Weibo collector — m.weibo.cn JSON + visitor-cookie flow (PLAN.md §5.3 Tier B).

Lifts the verified flow from experiments/weibo-monitor + weibo-stability-probe:

- Visitor cookie minted via POST https://visitor.passport.weibo.cn/visitor/genvisitor2
  (body ``cb=visitor_gray_callback&tid=&from=weibo&webdriver=false``, mobile UA,
  Referer weibo.com) → ``{"sub","subp"}`` → Cookie header ``SUB=..; SUBP=..``.
- Cached at ``state/weibo_cookie.json`` (mint time + last request ts inside the
  same file). Re-minted automatically when a call returns any 4xx or body
  ``"ok":-100`` (sso signin sentinel) — one remint+retry per call.
- Timeline: GET m.weibo.cn/api/container/getIndex?type=uid&value=<uid> →
  tabsInfo.tabs[tab_type=="weibo"].containerid → getIndex?type=uid&value=<uid>
  &containerid=<cid>[&page=N] → data.cards[].mblog / card_type 11 card_group.
- Long text: statuses/extend?id=<mid> when mblog.isLongText (cfg fetch_extend).
- Rate limit: ≥3s between requests (≤20rpm), persisted across runs via the
  cookie file's last_req_at.
- Logged-in cookie fallback: cfg["login_cookie"] or env WEIBO_COOKIES is used
  when visitor mint fails entirely (PLAN 保底 route).

API:
    fetch_uid(uid, cfg)         -> [raw_item, ...]   (required entry point)
    fetch_hot_band(cfg)         -> [raw_item, ...]   (weibo_hot_band source)
    fetch_container(feed_url, cfg) -> [raw_item, ...] (keyword containerid src)
    fetch_source(src, cfg)      -> routes a sources.yaml method:weibo entry

cfg (dict, all optional):
    state_dir      default <repo>/state        (cookie + ratelimit file)
    min_interval   default 3.0 s               (≤20rpm)
    timeout        default 20
    max_items      default 30
    pages          default 1 (timeline pages, ≤3)
    fetch_extend   default True (long-text expansion, ≤max_items extra req)
    proxy          default "direct"            (weibo sources are direct_only)
    source_name    _source.name  (default from uid / feed)
    feed_url       _source.feed_url
    run_dir        if set, dump raw JSON via lib.http.save_raw → _raw_ref
    login_cookie   logged-in Cookie header value (fallback)
    _health        caller-passed dict; filled with {status,n,last_error,
                   latency_ms,via,reminted,cookie} for 11_raw_manifest health

raw_item fields satisfy contracts RawItem (raw_item/1): schema/item_key/id/
url/url_canon/title/content_text/date_published/date_fetched/
language/tags/image/_source{name,feed_url,kind:"api",item_guid}/_fetch/_raw_ref.

Smoke:  uv run stages/lib/weibo_collect.py            # live: mint + 2 AI uids
        uv run stages/lib/weibo_collect.py --offline  # fixture asserts only
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
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, parse_qs

# --- sys.path wiring -------------------------------------------------------
# Repo root for `contracts`/`adapters`; stages/ for `lib.*` sibling imports.
# The script's own dir (stages/lib) is stripped so `import http` inside httpx
# can't resolve to lib/http.py (same trick as lib/http.py itself).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # stages/
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]

import httpx  # noqa: E402

from lib.http import get as http_get, save_raw  # noqa: E402
from lib.normalize import url_canon, item_key, title_norm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) "
             "AppleWebKit/604.1.38 (KHTML, like Gecko) Version/11.0 "
             "Mobile/15A372 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

GENVISITOR = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
GETINDEX = "https://m.weibo.cn/api/container/getIndex"
EXTEND = "https://m.weibo.cn/statuses/extend"
HOT_BAND = "https://weibo.com/ajax/statuses/hot_band"
HOT_SEARCH = "https://weibo.com/ajax/side/hotSearch"

MIN_INTERVAL = 3.0          # ≤20 rpm (PLAN §5.3)
COOKIE_MAX_AGE = 20 * 3600  # re-mint daily anyway
REMINT_STATUSES = {400, 401, 403, 404, 414, 418, 429, 432}

_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.I)
_WS = re.compile(r"[ \t]+")
_SUB_RE = re.compile(r'"sub"\s*:\s*"([^"]+)"')
_SUBP_RE = re.compile(r'"subp"\s*:\s*"([^"]+)"')
_OK_NEG = re.compile(r'"ok"\s*:\s*(-?\d+)')


# ------------------------------------------------------------------ cfg ----

def _state_dir(cfg) -> Path:
    d = Path(cfg.get("state_dir") or (REPO_ROOT / "state"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cookie_path(cfg) -> Path:
    return _state_dir(cfg) / "weibo_cookie.json"


def _min_interval(cfg) -> float:
    return float(cfg.get("min_interval", MIN_INTERVAL))


# ------------------------------------------------------------ rate limit ---

def _throttle(cfg):
    """Enforce ≥min_interval between weibo requests; persists across runs."""
    p = _cookie_path(cfg)
    last = 0.0
    try:
        last = float(json.loads(p.read_text()).get("last_req_at") or 0)
    except Exception:
        pass
    wait = _min_interval(cfg) - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    try:
        st = json.loads(p.read_text()) if p.is_file() else {}
        if not isinstance(st, dict):
            st = {}
        st["last_req_at"] = time.time()
        p.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    except Exception:
        pass


# ---------------------------------------------------------------- cookie ---

def _mint_visitor(cfg) -> dict | None:
    """POST genvisitor2 → {sub, subp} (probe.sh-verified). Returns cookie dict
    {kind:'visitor',sub,subp,minted_at} or None."""
    _throttle(cfg)
    try:
        with httpx.Client(
            proxy=None if cfg.get("proxy", "direct") in ("direct", "none")
            else cfg.get("proxy"),
            trust_env=False,
            timeout=httpx.Timeout(float(cfg.get("timeout", 20))),
            follow_redirects=True,
        ) as cli:
            r = cli.post(
                GENVISITOR,
                data="cb=visitor_gray_callback&tid=&from=weibo&webdriver=false",
                headers={
                    "User-Agent": MOBILE_UA,
                    "Referer": "https://weibo.com/",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        text = r.text or ""
        m_sub, m_subp = _SUB_RE.search(text), _SUBP_RE.search(text)
        if r.status_code == 200 and m_sub:
            return {
                "kind": "visitor",
                "sub": m_sub.group(1),
                "subp": m_subp.group(1) if m_subp else "",
                "minted_at": time.time(),
                "last_req_at": time.time(),
            }
    except Exception:
        pass
    return None


def _login_cookie(cfg) -> dict | None:
    raw = (cfg.get("login_cookie") or os.environ.get("WEIBO_COOKIES") or "").strip()
    if raw:
        return {"kind": "login", "raw": raw, "minted_at": time.time()}
    return None


def _ensure_cookie(cfg, *, force=False) -> dict | None:
    """Load cached visitor cookie (fresh < COOKIE_MAX_AGE), else mint; final
    fallback is a logged-in cookie from cfg/env (never cached to disk)."""
    p = _cookie_path(cfg)
    if not force and p.is_file():
        try:
            st = json.loads(p.read_text())
            if st.get("sub") and time.time() - float(st.get("minted_at", 0)) \
                    < COOKIE_MAX_AGE:
                st["kind"] = "visitor"
                return st
        except Exception:
            pass
    ck = _mint_visitor(cfg)
    if ck:
        try:
            prev = json.loads(p.read_text()) if p.is_file() else {}
            if not isinstance(prev, dict):
                prev = {}
            ck["last_req_at"] = max(time.time(),
                                    float(prev.get("last_req_at") or 0))
            p.write_text(json.dumps(ck, ensure_ascii=False, indent=1))
        except Exception:
            pass
        return ck
    return _login_cookie(cfg)


def _cookie_header(ck: dict) -> str:
    if ck.get("kind") == "login":
        return ck.get("raw") or ""
    return f"SUB={ck.get('sub','')}; SUBP={ck.get('subp','')}".rstrip("; ")


# ------------------------------------------------------------------ http ---

def _needs_remint(res) -> bool:
    """4xx (incl. 432 visitor-expired) or JSON ok:-100 → sso signin sentinel."""
    if res.status in REMINT_STATUSES:
        return True
    if res.status == 200 and res.body:
        m = _OK_NEG.search(res.text[:4096])
        if m and int(m.group(1)) < 0:
            return True
    return False


def _api_get(url: str, cfg, referer: str, ck: dict):
    _throttle(cfg)
    return http_get(
        url,
        proxy=cfg.get("proxy", "direct"),
        timeout=float(cfg.get("timeout", 20)),
        headers={
            "User-Agent": MOBILE_UA,
            "Referer": referer,
            "MWeibo-Pwa": "1",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Cookie": _cookie_header(ck),
        },
    )


def _api_get_auth(url: str, cfg, referer: str, health: dict):
    """GET with visitor cookie; one remint+retry on 4xx/ok:-100."""
    ck = _ensure_cookie(cfg)
    if not ck:
        health["last_error"] = "cookie_mint_failed"
        return None
    health["cookie"] = ck.get("kind")
    res = _api_get(url, cfg, referer, ck)
    if res is not None and _needs_remint(res):
        ck = _ensure_cookie(cfg, force=True)
        health["reminted"] = True
        if ck:
            res = _api_get(url, cfg, referer, ck)
    return res


# ----------------------------------------------------------------- parse ---

def _walk_mblogs(payload: dict) -> list[dict]:
    """Yield mblog dicts from getIndex/search payloads, dedup by bid."""
    out, seen = [], set()

    def walk(node):
        if isinstance(node, dict):
            mbs = []
            if "mblog" in node:
                mbs.append(node["mblog"])
            for c in node.get("card_group") or []:
                if isinstance(c, dict) and "mblog" in c:
                    mbs.append(c["mblog"])
            for c in node.get("cards") or []:
                walk(c)
            for mb in mbs:
                k = mb.get("bid") or mb.get("id")
                if k and k not in seen:
                    seen.add(k)
                    out.append(mb)

    walk(payload.get("data") if isinstance(payload, dict) else {})
    return out


def _html_to_text(h: str) -> str:
    t = _BR.sub("\n", h or "")
    t = _TAG.sub("", t)
    t = _html.unescape(t).replace("\xa0", " ")
    lines = [_WS.sub(" ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _parse_created_at(s: str) -> str | None:
    """'Tue Aug 25 11:59:06 +0800 2026' → UTC RFC3339. Lenient fallback."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds") \
                     .replace("+00:00", "Z")
        except (ValueError, TypeError):
            pass
    try:
        dt = parsedate_to_datetime(s)
        if dt and dt.tzinfo:
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds") \
                     .replace("+00:00", "Z")
    except Exception:
        pass
    return None


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds") \
                                   .replace("+00:00", "Z")


def _mblog_url(mb: dict, uid_fallback: str = "") -> str:
    bid = mb.get("bid") or mb.get("mblogid")
    uid = str((mb.get("user") or {}).get("id") or uid_fallback or "")
    if bid and uid:
        return f"https://weibo.com/{uid}/{bid}"
    if bid:
        return f"https://m.weibo.cn/status/{bid}"
    if mb.get("id"):
        return f"https://m.weibo.cn/status/{mb['id']}"
    return (mb.get("scheme") or "").split("?")[0] or "https://weibo.com/"


def _fetch_extend(mid, cfg, health) -> str | None:
    """statuses/extend?id= for isLongText posts → full text."""
    res = _api_get_auth(f"{EXTEND}?id={mid}", cfg,
                        "https://m.weibo.cn/", health)
    if res and res.ok:
        try:
            d = json.loads(res.text)
            lt = ((d.get("data") or {}).get("longTextContent") or "")
            if lt:
                return _html_to_text(lt)
        except Exception:
            pass
    return None


def _mblog_to_item(mb: dict, cfg, uid_fallback: str, status: int,
                   fetched_at: str, health: dict) -> dict:
    text_html = mb.get("text") or ""
    plain = _html_to_text(text_html)
    if mb.get("isLongText") and cfg.get("fetch_extend", True):
        full = _fetch_extend(mb.get("id") or mb.get("mid"), cfg, health)
        if full and len(full) > len(plain):
            plain = full
            text_html = f"<p>{full}</p>"
    rt = mb.get("retweeted_status")
    if isinstance(rt, dict):
        rt_user = ((rt.get("user") or {}).get("screen_name")) or "unknown"
        rt_txt = _html_to_text(rt.get("text") or "")
        if rt_txt:
            plain = f"{plain}\n\n// @{rt_user}: {rt_txt}".strip()

    title = title_norm(plain.split("\n", 1)[0])[:80] or f"微博 {mb.get('bid') or mb.get('id','')}"
    url = _mblog_url(mb, uid_fallback)
    canon = url_canon(url)
    key = item_key(url)

    pics = mb.get("pics") or []
    image = None
    if pics:
        p0 = pics[0]
        image = ((p0.get("large") or {}).get("url")) or p0.get("url")
    if not image:
        pi = mb.get("page_info") or {}
        image = ((pi.get("page_pic") or {}).get("url")) or pi.get("page_pic")

    src_name = cfg.get("source_name") or f"weibo_{uid_fallback or 'search'}"
    feed_url = cfg.get("feed_url") or (
        f"{GETINDEX}?type=uid&value={uid_fallback}" if uid_fallback
        else GETINDEX)
    sha = hashlib.sha256(
        json.dumps(mb, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]

    item = {
        "schema": "raw_item/1",
        "item_key": key,
        "id": key,
        "url": url,
        "url_canon": canon,
        "title": title,
        "content_text": plain,
        "date_published": _parse_created_at(mb.get("created_at") or ""),
        "date_fetched": fetched_at,
        "language": "zh",
        "tags": ["weibo"],
        "image": image,
        "_source": {
            "name": src_name,
            "feed_url": feed_url,
            "kind": "api",
            "item_guid": str(mb.get("id") or mb.get("bid") or ""),
        },
        "_fetch": {
            "status": status,
            "via": "direct",
            "reachable": True,
            "etag": None,
            "content_sha256": sha,
        },
    }
    if cfg.get("_raw_ref"):
        item["_raw_ref"] = cfg["_raw_ref"]
    return item


def _save_run_raw(cfg, source: str, url: str, body: str) -> str | None:
    rd = cfg.get("run_dir")
    if not rd:
        return None
    try:
        return save_raw(rd, source, url, body)
    except Exception:
        return None


# ------------------------------------------------------------- fetch_uid ---

def fetch_uid(uid, cfg=None) -> list[dict]:
    """Fetch one weibo uid timeline → [raw_item]. Visitor cookie auto-minted /
    reminted; ≤20rpm. Diagnostics go to cfg['_health'] if provided."""
    cfg = dict(cfg or {})
    uid = str(uid).strip()
    health = cfg.get("_health")
    if health is None:
        health = {}
        cfg["_health"] = health
    health.setdefault("source", f"weibo_uid:{uid}")
    t0 = time.monotonic()
    fetched_at = _now_rfc3339()
    items: list[dict] = []
    referer = f"https://m.weibo.cn/u/{uid}"
    max_items = int(cfg.get("max_items", 30))
    pages = max(1, min(int(cfg.get("pages", 1)), 3))

    def finish(status, err=None):
        health.update({
            "status": status,
            "n": len(items),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "via": "direct",
        })
        if err:
            health["last_error"] = err
        return items

    # 1) profile getIndex → weibo tab containerid
    r1 = _api_get_auth(f"{GETINDEX}?type=uid&value={uid}", cfg, referer, health)
    if r1 is None:
        return finish("walled", health.get("last_error"))
    if not r1.ok:
        return finish(r1.error, f"profile getIndex {r1.error} "
                              f"status={r1.status}")
    try:
        d1 = json.loads(r1.text)
    except Exception:
        return finish("parse_error", "profile getIndex non-JSON")
    if int(d1.get("ok") or 0) != 1:
        return finish("walled", f"profile ok={d1.get('ok')}")

    _save_run_raw(cfg, f"weibo_{uid}", r1.final_url or GETINDEX, r1.text)

    tab_ids = {"weibo": f"107603{uid}", "profile": f"230283{uid}"}
    tabs = (((d1.get("data") or {}).get("tabsInfo") or {}).get("tabs")) or []
    for t in tabs:
        tt, cid = t.get("tab_type"), t.get("containerid")
        if tt in tab_ids and cid:
            tab_ids[tt] = str(cid)
    containerid = tab_ids["weibo"]

    screen = (((d1.get("data") or {}).get("userInfo") or {})
              .get("screen_name")) or ""
    if screen and not cfg.get("source_name"):
        cfg["source_name"] = f"weibo_{screen}"
    health["screen_name"] = screen

    mblogs = _walk_mblogs(d1)  # some payloads embed cards on first call
    seen = {m.get("bid") or m.get("id") for m in mblogs}

    # 2) timeline pages
    status = r1.status
    last_url, last_text = "", ""
    for page in range(1, pages + 1):
        url = (f"{GETINDEX}?type=uid&value={uid}&containerid={containerid}"
               + (f"&page={page}" if page > 1 else ""))
        r = _api_get_auth(url, cfg, referer, health)
        if r is None or not r.ok:
            if not mblogs:
                err = health.get("last_error") or (
                    f"timeline {getattr(r,'error','?')} "
                    f"status={getattr(r,'status',0)}")
                return finish(getattr(r, "error", "walled") or "walled", err)
            break  # keep what we already have
        status = r.status
        last_url, last_text = url, r.text
        try:
            d = json.loads(r.text)
        except Exception:
            if not mblogs:
                return finish("parse_error", "timeline non-JSON")
            break
        if int(d.get("ok") or 0) == -100 and not mblogs:
            return finish("walled", "timeline ok=-100 (signin)")
        for mb in _walk_mblogs(d):
            k = mb.get("bid") or mb.get("id")
            if k not in seen:
                seen.add(k)
                mblogs.append(mb)
        if len(mblogs) >= max_items:
            break
    health["via_tab"] = "weibo"

    # Weibo tab empty (total:0, e.g. 月之暗面Kimi posts live under 精选) →
    # fall back to the featured/profile tab's HOTMBLOG card group once.
    if not mblogs and tab_ids.get("profile") != containerid:
        url = (f"{GETINDEX}?type=uid&value={uid}"
               f"&containerid={tab_ids['profile']}")
        r = _api_get_auth(url, cfg, referer, health)
        if r is not None and r.ok:
            status = r.status
            try:
                d = json.loads(r.text)
                for mb in _walk_mblogs(d):
                    k = mb.get("bid") or mb.get("id")
                    if k not in seen:
                        seen.add(k)
                        mblogs.append(mb)
                if mblogs:
                    health["via_tab"] = "profile"
                    last_url, last_text = url, r.text
            except Exception:
                pass

    if not mblogs:
        return finish("empty", "no mblog cards")

    raw_ref = _save_run_raw(cfg, f"weibo_{uid}", last_url, last_text) \
        if last_text else None
    if raw_ref:
        cfg["_raw_ref"] = raw_ref

    for mb in mblogs[:max_items]:
        try:
            items.append(_mblog_to_item(mb, cfg, uid, status, fetched_at,
                                        health))
        except Exception as e:
            health.setdefault("item_errors", []).append(
                f"{mb.get('id')}: {type(e).__name__} {e}")
    if not items:
        return finish("parse_error", "all mblogs failed mapping")
    return finish("ok")


# ------------------------------------------------------------- hot_band ----

def _band_items(band: list[dict], cfg, status: int, fetched_at: str,
                src_name: str, feed_url: str) -> list[dict]:
    items = []
    for b in band:
        word = (b.get("note") or b.get("word") or "").strip()
        if not word:
            continue
        url = (b.get("word_scheme") or "").strip()
        if not url.startswith("http"):
            url = "https://s.weibo.com/weibo?q=" + quote(f"#{word}#")
        canon = url_canon(url)
        key = item_key(url)
        cat = b.get("category") or ""
        num = b.get("num") or b.get("raw_hot") or 0
        desc = " ".join(x for x in (
            f"热搜:{word}", f"热度{num}" if num else "",
            f"分类:{cat}" if cat else "",
            (b.get("icon_desc") or "")) if x)
        items.append({
            "schema": "raw_item/1",
            "item_key": key,
            "id": key,
            "url": url,
            "url_canon": canon,
            "title": title_norm(word)[:80],
            "content_text": desc,
            "date_published": None,  # signal-type source (PLAN §5.2.4)
            "date_fetched": fetched_at,
            "language": "zh",
            "tags": ["weibo", "hot_band"] + ([cat] if cat else []),
            "image": b.get("icon") or None,
            "_source": {"name": src_name, "feed_url": feed_url,
                        "kind": "api",
                        "item_guid": str(b.get("word") or word)},
            "_fetch": {"status": status, "via": "direct", "reachable": True,
                       "etag": None, "content_sha256": None},
        })
    return items


def fetch_hot_band(cfg=None) -> list[dict]:
    """weibo.com/ajax/statuses/hot_band — zero-credential path verified in
    experiments/weibo-monitor (browser UA + Referer + weibo.com cookie jar)."""
    cfg = dict(cfg or {})
    health = cfg.get("_health")
    if health is None:
        health = {}
        cfg["_health"] = health
    t0 = time.monotonic()
    fetched_at = _now_rfc3339()
    items: list[dict] = []
    feed_url = cfg.get("feed_url") or HOT_BAND
    src_name = cfg.get("source_name") or "weibo_hot_band"

    def finish(status, err=None):
        health.update({"status": status, "n": len(items), "via": "direct",
                       "latency_ms": int((time.monotonic() - t0) * 1000)})
        if err:
            health["last_error"] = err
        return items

    _throttle(cfg)
    try:
        with httpx.Client(
            proxy=None if cfg.get("proxy", "direct") in ("direct", "none")
            else cfg.get("proxy"),
            trust_env=False,
            timeout=httpx.Timeout(float(cfg.get("timeout", 20))),
            follow_redirects=True,
            headers={"User-Agent": DESKTOP_UA},
        ) as cli:
            cli.get("https://weibo.com/")          # cookie jar: XSRF-TOKEN etc.
            _throttle(cfg)
            r = cli.get(feed_url, headers={
                "Referer": "https://weibo.com/",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
            })
    except httpx.HTTPError as e:
        return finish("http_0", f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        return finish(f"http_{r.status_code}",
                      f"hot_band status={r.status_code}")
    _save_run_raw(cfg, src_name, feed_url, r.text)
    try:
        d = r.json()
    except Exception:
        return finish("parse_error", "hot_band non-JSON")
    data = d.get("data") or {}
    band = list(data.get("band_list") or [])
    if data.get("hotgov"):
        band.insert(0, data["hotgov"])
    items = _band_items(band[: int(cfg.get("max_items", 50))], cfg,
                        r.status_code, fetched_at, src_name, feed_url)
    return finish("ok" if items else "empty",
                  None if items else "empty band_list")


# -------------------------------------------------------- container/uid ----

def fetch_container(feed_url: str, cfg=None) -> list[dict]:
    """Keyword/search containerid getIndex URL → raw_items (weibo_ai_keyword
    source shape)."""
    cfg = dict(cfg or {})
    health = cfg.get("_health")
    if health is None:
        health = {}
        cfg["_health"] = health
    t0 = time.monotonic()
    fetched_at = _now_rfc3339()
    items: list[dict] = []

    def finish(status, err=None):
        health.update({"status": status, "n": len(items), "via": "direct",
                       "latency_ms": int((time.monotonic() - t0) * 1000)})
        if err:
            health["last_error"] = err
        return items

    cfg.setdefault("feed_url", feed_url)
    r = _api_get_auth(feed_url, cfg, "https://m.weibo.cn/search", health)
    if r is None:
        return finish("walled", health.get("last_error"))
    if not r.ok:
        return finish(r.error, f"container {r.error} status={r.status}")
    try:
        d = json.loads(r.text)
    except Exception:
        return finish("parse_error", "container non-JSON")
    _save_run_raw(cfg, cfg.get("source_name") or "weibo_search",
                  feed_url, r.text)
    mblogs = _walk_mblogs(d)
    if not mblogs:
        return finish("empty", "no mblog cards")
    for mb in mblogs[: int(cfg.get("max_items", 30))]:
        try:
            items.append(_mblog_to_item(mb, cfg, "", r.status, fetched_at,
                                        health))
        except Exception:
            pass
    return finish("ok" if items else "parse_error")


def fetch_source(src: dict, cfg=None) -> list[dict]:
    """Route a sources.yaml method:weibo entry by feed_url shape:
    hot_band/hotSearch → fetch_hot_band; getIndex&value=<uid> → fetch_uid;
    containerid= → fetch_container."""
    cfg = dict(cfg or {})
    cfg.setdefault("source_name", src.get("name"))
    cfg.setdefault("feed_url", src.get("feed_url"))
    cfg.setdefault("max_items", src.get("max_items_per_source", 30))
    cfg.setdefault("proxy", "direct" if src.get("proxy") == "direct_only"
                   else src.get("proxy", "direct"))
    feed = src.get("feed_url") or ""
    if "hot_band" in feed or "hotSearch" in feed:
        return fetch_hot_band(cfg)
    if "getIndex" in feed:
        m = re.search(r"[?&]value=(\d+)", feed)
        if m:
            return fetch_uid(m.group(1), cfg)
        if "containerid=" in feed:
            return fetch_container(feed, cfg)
    return []


# ------------------------------------------------------------- self test ----

def _fixture(name):
    p = REPO_ROOT / "experiments" / "weibo-stability-probe" / name
    return json.loads(p.read_text()) if p.is_file() else None


if __name__ == "__main__":
    offline = "--offline" in sys.argv

    # --- offline asserts ----------------------------------------------------
    assert _html_to_text("a<br />b&nbsp;<a href='x'>话题</a>") == "a\nb 话题"
    assert _parse_created_at("Tue Aug 25 11:59:06 +0800 2026") == \
        "2026-08-25T03:59:06Z"
    assert _parse_created_at("") is None

    class _R:  # minimal FetchResult stand-in for _needs_remint
        def __init__(self, status, body=b""):
            self.status, self.body = status, body
            self.text = body.decode() if body else ""
    assert _needs_remint(_R(432))
    assert _needs_remint(_R(403))
    assert _needs_remint(_R(200, b'{"ok":-100,"msg":"x"}'))
    assert not _needs_remint(_R(200, b'{"ok":1}'))
    assert not _needs_remint(_R(500))

    tl = _fixture("tl.json")
    if tl:
        mbs = _walk_mblogs(tl)
        assert len(mbs) >= 2, f"fixture walk got {len(mbs)}"
        it = _mblog_to_item(mbs[0], {"fetch_extend": False}, "1195230310",
                            200, _now_rfc3339(), {})
        assert re.fullmatch(r"[0-9a-f]{16}", it["item_key"])
        assert it["url"].startswith("https://weibo.com/1195230310/")
        assert it["date_published"] and it["date_published"].endswith("Z")
        assert it["_source"]["kind"] == "api" and it["title"]
        print(f"fixture tl.json: {len(mbs)} mblogs, item OK: {it['title'][:40]}")
    kw = _fixture("kw.json")
    if kw:
        mbs = _walk_mblogs(kw)
        it = _mblog_to_item(mbs[0], {"fetch_extend": False}, "", 200,
                            _now_rfc3339(), {})
        assert it["url"].startswith("https://weibo.com/")
        print(f"fixture kw.json: {len(mbs)} mblogs, item OK: {it['title'][:40]}")

    # contract lint on a produced item
    try:
        from contracts.models import RawItem
        if tl:
            RawItem.model_validate(it)
            print("RawItem contract validation OK")
    except ImportError:
        print("contracts unavailable — skipped model validation")

    if offline:
        print("weibo_collect.py offline self-test OK")
        sys.exit(0)

    # --- live test: mint visitor cookie + fetch 2 AI uids -------------------
    report = {"cookie_mint": None, "uids": {}}
    ck = _ensure_cookie({})
    report["cookie_mint"] = bool(ck and ck.get("sub")) if ck else False
    print(f"[live] cookie: {ck.get('kind') if ck else 'NONE'} "
          f"sub_len={len((ck or {}).get('sub',''))}")

    for uid in ("7909392214", "7876775013"):   # 月之暗面 / 腾讯微信团队
        h = {}
        items = fetch_uid(uid, {"_health": h, "max_items": 30,
                                "fetch_extend": False})
        report["uids"][uid] = {"n": len(items), **h}
        print(f"[live] uid={uid} items={len(items)} "
              f"health={json.dumps(h, ensure_ascii=False)[:200]}")
        if items:
            print(f"       first: {items[0]['title'][:60]} | "
                  f"{items[0]['date_published']} | {items[0]['url']}")

    h = {}
    band = fetch_hot_band({"_health": h, "max_items": 10})
    report["hot_band"] = {"n": len(band), **h}
    print(f"[live] hot_band items={len(band)} status={h.get('status')}")

    out = REPO_ROOT / "out" / "weibo_collect_selftest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print("weibo_collect.py self-test done →", out)
