#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.28",
# ]
# ///
"""Collect-layer HTTP helper (PLAN.md §5.2).

get(url, *, etag, lastmod, proxy, timeout, headers)
    httpx-based conditional GET: ETag/Last-Modified validators, proxy-aware
    (explicit arg > HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env > direct), redirect-
    following, and the source-health error taxonomy.

save_raw(run_dir, source, url, body)
    persist a raw response body to
        <repo>/data/raw_cache/<date>/<source>/<sha8>.<ext>
    (atomic tmp+rename, ext sniffed from content) and return the
    repo-relative `_raw_ref` string recorded on raw_item.

FetchResult.error taxonomy (shared with 11_raw_manifest source health):
    ok | empty | http_<code> | timeout | parse_error | walled |
    shell_only | rate_limited | dns_fail
    `http_0` = transport failure without an HTTP response (conn refused /
    TLS / protocol reset) — kept inside the http_N family so downstream
    string-matching still works; detail carries the exception text.

Smoke:  uv run stages/lib/http.py          # live checks included
        uv run stages/lib/http.py --offline  # skip live fetches
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

# Running this file directly (uv run stages/lib/http.py) leaves stages/lib/ on
# sys.path[0], where this file shadows the stdlib `http` package that httpx
# imports — drop the script's own dir. Imported as `lib.http` this is a no-op.
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]

ERRORS = {
    "ok", "empty", "timeout", "parse_error", "walled",
    "shell_only", "rate_limited", "dns_fail",
}  # plus "http_<code>" generated dynamically

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36 ai-news-pipeline/0.1")

# Bot-wall / challenge markers — calibrated list lifted from
# experiments/qa-loop/audit_links.py (269-URL live audit), plus common WAFs.
WALL = re.compile(
    r"wappoc_appmsgcaptcha|TCaptcha\.js|cf-chl-|challenge-platform|"
    r"Just a moment|环境异常|安全验证|awswaf|aws-waf-token|geetest|"
    r"Incapsula|ddos-guard|enable javascript and cookies|安全检查|"
    r"access denied - godaddy|验证手机|访问过于频繁",
    re.I,
)

# JS-shell page: HTTP 200 but the document is only a mount-point (x.com etc).
# Heuristic: near-zero visible text AND a known shell marker.
SHELL_MARK = re.compile(
    r"id=[\"'](?:root|app|react-root|__next|__nuxt)[\"']|"
    r"enable javascript|javascript is (?:not available|disabled|required)|"
    r"需要启用\s*javascript|请开启\s*javascript|window\.__INITIAL_STATE__",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

_DNS_PAT = re.compile(
    r"name or service not known|temporary failure in name resolution|"
    r"nodename nor servname|getaddrinfo|name resolution|no address associated",
    re.I,
)


@dataclass
class FetchResult:
    """Outcome of one get(). `via` reports the transport route actually used
    ('direct'|'proxy') — it is NOT raw_item._fetch.via (contract literal
    direct|mirror|cache|manual); collect maps transport -> manifest fields."""

    status: int = 0                    # HTTP status; 0 = no response
    etag: Optional[str] = None         # response ETag (feed back next round)
    lastmod: Optional[str] = None      # response Last-Modified
    body: Optional[bytes] = None       # raw bytes; None on 304 / transport fail
    latency_ms: int = 0
    via: str = "direct"                # direct | proxy
    error: str = "ok"                  # taxonomy above
    final_url: Optional[str] = None    # after redirects
    content_type: Optional[str] = None
    detail: Optional[str] = None       # exception text for http_0/timeout etc.

    @property
    def ok(self) -> bool:
        return self.error == "ok" and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def text(self) -> str:
        return (self.body or b"").decode("utf-8", "replace")

    def to_dict(self, include_body: bool = False) -> dict:
        d = asdict(self)
        if not include_body:
            d.pop("body", None)
        return d


# ---------------------------------------------------------------- proxy ----

def _env_proxy_for(url: str) -> Optional[str]:
    """Approximate which env proxy httpx(trust_env=True) would apply to url."""
    scheme = url.split(":", 1)[0].lower()
    cand = (
        os.environ.get(f"{scheme.upper()}_PROXY")
        or os.environ.get(f"{scheme.lower()}_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    if not cand:
        return None
    no = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if no.strip() in ("*",):
        return None
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].split(":", 1)[0].lower()
    for tok in (t.strip().lstrip(".").lower() for t in no.split(",")):
        if tok and (host == tok or host.endswith("." + tok)):
            return None
    return cand


def _resolve_proxy(url: str, proxy):
    """-> (proxy_url_or_None, via). proxy arg: None=env, 'direct'/'none'=bypass,
    otherwise a proxy URL string."""
    if proxy in ("direct", "none", "off", False):
        return None, "direct"
    if isinstance(proxy, str) and proxy:
        return proxy, "proxy"
    env = _env_proxy_for(url)
    return (env, "proxy") if env else (None, "direct")


# ------------------------------------------------------------ classify ----

def _looks_shell(body: bytes) -> bool:
    head = body[: 1 << 18]
    try:
        txt = head.decode("utf-8", "replace")
    except Exception:
        return False
    if "<" not in txt or not SHELL_MARK.search(txt):
        return False
    visible = _WS.sub("", _TAG.sub(" ", txt))
    return len(visible) < 80


def _classify(status: int, body: bytes, ctype: str) -> str:
    if status == 304:
        return "ok"
    probe = body[: 1 << 18]
    text = probe.decode("utf-8", "replace") if probe else ""
    if WALL.search(text):
        return "walled"
    if status == 429:
        return "rate_limited"
    if 200 <= status < 300:
        if not body:
            return "empty"
        if _looks_shell(body):
            return "shell_only"
        return "ok"
    return f"http_{status}"


# ------------------------------------------------------------------ get ----

def get(
    url: str,
    *,
    etag: Optional[str] = None,
    lastmod: Optional[str] = None,
    proxy=None,
    timeout: float = 20,
    headers: Optional[dict] = None,
    retries: int = 0,
    follow_redirects: bool = True,
) -> FetchResult:
    """Conditional GET -> FetchResult.

    etag/lastmod emit If-None-Match / If-Modified-Since. `proxy`:
    None honors env (trust_env), 'direct' bypasses, '<url>' pins a proxy.
    retries = extra attempts on transport failures (timeout/dns/conn), with
    0.4*2^n backoff capped at 2s — HTTP responses are never retried here.
    """
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/rss+xml,application/atom+xml,application/json,"
                  "*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if headers:
        hdrs.update(headers)
    if etag:
        hdrs["If-None-Match"] = etag
    if lastmod:
        hdrs["If-Modified-Since"] = lastmod

    proxy_url, via = _resolve_proxy(url, proxy)
    res = FetchResult(via=via)
    attempts = 1 + max(0, retries)

    try:
        with httpx.Client(
            proxy=proxy_url,
            trust_env=proxy is None,          # explicit arg disables env read
            timeout=httpx.Timeout(timeout),
            follow_redirects=follow_redirects,
            limits=httpx.Limits(max_connections=4),
        ) as cli:
            for i in range(attempts):
                t0 = time.monotonic()
                try:
                    r = cli.get(url, headers=hdrs)
                    res.latency_ms = int((time.monotonic() - t0) * 1000)
                    res.status = r.status_code
                    res.etag = r.headers.get("etag")
                    res.lastmod = r.headers.get("last-modified")
                    res.final_url = str(r.url)
                    res.content_type = r.headers.get("content-type")
                    res.body = None if r.status_code == 304 else r.content
                    res.error = _classify(r.status_code, r.content or b"",
                                          res.content_type or "")
                    return res
                except httpx.TimeoutException as e:
                    res.error, res.detail = "timeout", f"{type(e).__name__}: {e}"
                except httpx.ConnectError as e:
                    msg = str(e)
                    if _DNS_PAT.search(msg):
                        res.error = "dns_fail"
                    else:
                        res.error = "http_0"
                    res.detail = f"{type(e).__name__}: {e}"
                except httpx.DecodingError as e:
                    res.error, res.detail = "parse_error", str(e)
                    res.latency_ms = int((time.monotonic() - t0) * 1000)
                    return res
                except httpx.HTTPError as e:   # transport w/o response
                    res.error, res.detail = "http_0", f"{type(e).__name__}: {e}"
                res.latency_ms = int((time.monotonic() - t0) * 1000)
                if i < attempts - 1:
                    time.sleep(min(2.0, 0.4 * (2 ** i)))
    except httpx.HTTPError as e:  # client construction/proxy failure
        res.error, res.detail = "http_0", f"{type(e).__name__}: {e}"
    res.status = 0
    return res


# -------------------------------------------------------------- save_raw ----

def _repo_root_for(run_dir: Path) -> Path:
    """repo root = nearest ancestor of run_dir containing PLAN.md; fallback
    run_dir.parent.parent (runs/<date> convention)."""
    for anc in Path(run_dir).resolve().parents:
        if (anc / "PLAN.md").is_file():
            return anc
    return Path(run_dir).resolve().parents[1]


def _sniff_ext(body: bytes) -> str:
    head = body[:512].lstrip()
    if head.startswith((b"{", b"[")):
        try:
            json.loads(body.decode("utf-8"))
            return ".json"
        except Exception:
            pass
    low = head[:200].lower()
    if low.startswith((b"<?xml", b"<rss", b"<feed", b"<rdf", b"<opml",
                      b"<urlset", b"<sitemapindex", b"<feed")):
        return ".xml"
    if b"<html" in low or low.startswith(b"<!doctype html"):
        return ".html"
    if b"\x00" in head[:256]:
        return ".bin"
    return ".txt"


_SRC_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def save_raw(run_dir, source: str, url: str, body, *, ext: Optional[str] = None) -> str:
    """Persist raw body under data/raw_cache/<date>/<source>/<sha8>.<ext>.

    Returns the repo-relative `_raw_ref` path (posix str). Atomic via tmp+mv.
    """
    run_dir = Path(run_dir)
    date = run_dir.name
    repo = _repo_root_for(run_dir)
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    if ext is None:
        ext = _sniff_ext(raw)
    if not ext.startswith("."):
        ext = "." + ext
    src = _SRC_SAFE.sub("_", source or "unknown")[:64] or "unknown"
    sha8 = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    out_dir = repo / "data" / "raw_cache" / date / src
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{sha8}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, dest)
    return dest.relative_to(repo).as_posix()


# ------------------------------------------------------------- self test ----

def _live(label: str, url: str, **kw) -> FetchResult:
    r = get(url, **kw)
    d = r.to_dict()
    d["body_bytes"] = len(r.body or b"")
    print(f"[{label}]", json.dumps(d, ensure_ascii=False))
    return r


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    fails = []

    # --- offline classification asserts ----------------------------------
    wall_html = b"<html><title>Just a moment...</title>cf-chl-challenge</html>"
    shell_html = (b"<html><body><div id=\"root\"></div>"
                  b"<noscript>JavaScript is not available.</noscript>"
                  b"<script src=x.js></script></body></html>")
    assert _classify(200, wall_html, "text/html") == "walled"
    assert _classify(403, wall_html, "text/html") == "walled"
    assert _classify(200, shell_html, "text/html") == "shell_only"
    assert _classify(200, b"", "text/html") == "empty"
    assert _classify(429, b"rate", "") == "rate_limited"
    assert _classify(404, b"nope", "") == "http_404"
    assert _classify(304, b"", "") == "ok"
    assert _classify(200, b"<rss>real content here padding padding padding "
                   b"more text</rss>", "application/rss+xml") == "ok"
    print("offline classify asserts OK")

    # --- save_raw ----------------------------------------------------------
    scratch = Path.home() / ".cache" / "ainews_http_selftest" / "runs" / "2099-01-01"
    ref = save_raw(scratch, "self_test", "https://example.com/a?utm_source=x",
                   b'{"ok": true}')
    rp = _repo_root_for(scratch) / ref
    assert rp.is_file() and rp.suffix == ".json" and ref.startswith(
        "data/raw_cache/2099-01-01/self_test/"), ref
    ref2 = save_raw(scratch, "self_test", "https://example.com/b",
                    b"<?xml version='1.0'?><rss/>")
    assert ref2.endswith(".xml")
    print("save_raw OK ->", ref)

    # --- live fetches -------------------------------------------------------
    if not offline:
        # conditional headers are emitted regardless of 304 support
        r = _live("cond-hdr", "https://www.baidu.com/", etag='"deadbeef"',
                  lastmod="Wed, 01 Jan 2020 00:00:00 GMT", timeout=15,
                  proxy="direct")
        assert r.status != 0, f"baidu direct failed: {r.detail}"
        assert r.error in ERRORS or r.error.startswith("http_")

        domestic_ok = r.error == "ok"
        if not domestic_ok:
            for alt in ("https://www.qq.com/", "https://www.163.com/"):
                rr = _live("direct-alt", alt, timeout=15, proxy="direct")
                if rr.error == "ok":
                    domestic_ok = True
                    break
        if not domestic_ok:
            fails.append("no domestic direct fetch succeeded")

        proxy_ok = False
        for alt in ("https://api.ipify.org?format=json",
                    "https://api.github.com/"):
            rr = _live("proxy", alt, timeout=20,
                       proxy="http://127.0.0.1:7890")
            if rr.error == "ok" and rr.via == "proxy":
                proxy_ok = True
                break
        if not proxy_ok:
            print("WARN: proxy route down — degraded state (PLAN D11), "
                  "not a lib failure")
    else:
        print("live checks skipped (--offline)")

    if fails:
        print("FAIL:", fails)
        sys.exit(1)
    print("http.py self-test OK")
