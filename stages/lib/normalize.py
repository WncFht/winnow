#!/usr/bin/env python3
"""Normalization helpers shared by collect/filter/dedup (docs/PLAN.md §5.2, §7.1).

url_canon(u)     canonical URL: lower scheme/host, IDNA, strip www./m./amp.,
                 drop tracking params (utm_*/fbclid/gclid/spm/… — same
                 calibrated key regex as experiments/dedup-history/store.py),
                 drop fragment, sort query, strip trailing slash, drop
                 default ports.
item_key(u)      sha256(url_canon(u))[:16] — the mechanical identity used by
                 every artifact contract (contracts/models.py ItemKey).
title_norm(t)    NFKC + zero-width/control strip + fancy-punct fold +
                 whitespace collapse; optional casefold for matching.
parse_date_utc(v)
                 multi-format publish date -> RFC3339 UTC seconds
                 (epoch s/ms/us, ISO, RFC822, common CN formats);
                 moved verbatim from collect._parse_date — collect keeps a
                 same-name wrapper delegating here.
load_aliases(p)  read aliases.json {canonical: [alias, …]}.
apply_aliases(t, aliases)
                 rewrite every known alias/canonical spelling to the
                 canonical name; ASCII terms get word-boundary guards so
                 'Yi' never rewrites 'yield'/'metadata'-like substrings.

Smoke:  uv run stages/lib/normalize.py
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
ALIASES_PATH = REPO_ROOT / "aliases.json"

# Tracking-param keys — calibrated regex lifted verbatim from
# experiments/dedup-history/store.py so collect canon == dedup canon.
TRACKING_KEYS = re.compile(
    r"^(utm_|spm|ref$|ref_src|fbclid|gclid|dclid|mc_cid|mc_eid|share_|share$|"
    r"campaign|medium$|source$|from$|wechat|igshid|si$|feature$|_hsenc|_hsmi|"
    r"oly_|vero_|cmpid|sr_share|tt_|s$|token$|trk)",
    re.I,
)

_MOBILE_HOST = ("www.", "m.", "amp.")
_DEFAULT_PORT = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def url_canon(u: str) -> str:
    """Canonicalize a URL for identity/dedup. Idempotent; never raises."""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        sp = urlsplit(u if "://" in u else "https://" + u)
        scheme = (sp.scheme or "https").lower()
        host = (sp.hostname or "").lower()
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass
        for p in _MOBILE_HOST:
            if host.startswith(p):
                host = host[len(p):]
                break
        port = sp.port
        netloc = host
        if port and port != _DEFAULT_PORT.get(scheme):
            netloc = f"{host}:{port}"
        qs = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
              if not TRACKING_KEYS.match(k)]
        path = re.sub(r"/+$", "", sp.path) or "/"
        return urlunsplit((scheme, netloc, path, urlencode(sorted(qs)), ""))
    except Exception:
        return u


def item_key(u: str) -> str:
    """sha256(url_canon(u))[:16] — matches contracts ItemKey ^[0-9a-f]{16}$."""
    return hashlib.sha256(url_canon(u).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------ title_norm ----

_ZERO_WIDTH = re.compile(r"[​-‏⁠﻿­￼]")  # ZWSP..WJC + FEFF + soft hyphen + object-replacement
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_WS = re.compile(r"\s+")
_PUNCT_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "–": "-", "—": "-", "―": "-",
    "…": "...", "·": "·",
    "　": " ",
})


def title_norm(t: str, *, lower: bool = False) -> str:
    """Normalize a title/summary for dedup & display.

    NFKC folds full-width latin/compatibility forms; zero-width & control
    chars removed; curly quotes/dashes folded to ASCII; whitespace collapsed.
    lower=True additionally casefolds (use for matching, not for display).
    """
    t = unicodedata.normalize("NFKC", t or "")
    t = _ZERO_WIDTH.sub("", t)
    t = _CTRL.sub(" ", t)
    t = t.translate(_PUNCT_MAP)
    t = _WS.sub(" ", t).strip()
    return t.casefold() if lower else t


# -------------------------------------------------------------- aliases ----

def load_aliases(path=None) -> dict:
    """Load aliases.json -> {canonical: [alias, …]}.

    Tolerates a str value (single alias) by wrapping it into a list.
    """
    p = Path(path) if path else ALIASES_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for canon, als in raw.items():
        if isinstance(als, str):
            als = [als]
        out[str(canon)] = [str(a) for a in als if str(a).strip()]
    return out


def _edge(term: str) -> str:
    """Word-boundary guards on the ASCII edges of a term."""
    esc = re.escape(term)
    left = r"(?<![0-9A-Za-z])" if term[0].isascii() and term[0].isalnum() else ""
    right = r"(?![0-9A-Za-z])" if term[-1].isascii() and term[-1].isalnum() else ""
    return left + esc + right


def _freeze(aliases: dict):
    return tuple(sorted(
        (c, tuple(sorted(als))) for c, als in aliases.items()))


@lru_cache(maxsize=8)
def _compiled(frozen):
    """{canon:[aliases]} -> (pattern, casefolded-term -> canon)."""
    pairs = []  # (term, canonical)
    for canon, als in frozen:
        for t in {canon, *als}:
            if t and t.strip():
                pairs.append((t, canon))
    pairs.sort(key=lambda p: -len(p[0]))  # longest alias wins at a position
    pat = re.compile("|".join(_edge(t) for t, _ in pairs), re.I)
    table = {t.casefold(): c for t, c in pairs}
    return pat, table


def apply_aliases(text: str, aliases: dict) -> str:
    """Rewrite alias spellings to canonical names (idempotent).

    aliases: {canonical: [alias,…]} as returned by load_aliases. Matching is
    case-insensitive; pure-ASCII terms require non-alnum boundaries.
    """
    if not text or not aliases:
        return text or ""
    pat, table = _compiled(_freeze(aliases))
    return pat.sub(lambda m: table[m.group(0).casefold()], text)


# ------------------------------------------------------------- dates --------

TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def parse_date_utc(v) -> str | None:
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
        return parse_date_utc(float(s))
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


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":
    # --- url_canon ----------------------------------------------------------
    assert url_canon("HTTPS://WWW.Example.COM/a/b/?utm_source=x&b=2&A=1#frag") \
        == "https://example.com/a/b?A=1&b=2"  # param keys keep case
    assert url_canon("https://example.com/") == "https://example.com/"
    assert url_canon("example.com/news?utm_medium=m") == "https://example.com/news"
    assert url_canon("https://m.example.com/x/?fbclid=abc") == "https://example.com/x"
    assert url_canon("https://example.com:443/x?gclid=1&spm=a2c&keep=1") \
        == "https://example.com/x?keep=1"
    assert url_canon("http://example.com:8080/x") == "http://example.com:8080/x"
    assert url_canon("https://例子.中国/新闻") == "https://xn--fsqu00a.xn--fiqs8s/新闻"
    assert url_canon("https://example.com/x?si=abc&feature=share&v=9") \
        == "https://example.com/x?v=9"
    assert url_canon("") == ""
    canon = url_canon("https://www.bilibili.com/video/BV1?utm_source=s&b=2")
    assert url_canon(canon) == canon, "canon must be idempotent"
    print("url_canon OK ->", canon)

    # --- item_key -----------------------------------------------------------
    k1 = item_key("https://www.example.com/a?utm_campaign=z")
    k2 = item_key("https://example.com/a")
    assert k1 == k2 and re.fullmatch(r"[0-9a-f]{16}", k1), (k1, k2)
    print("item_key OK ->", k1)

    # --- title_norm ---------------------------------------------------------
    assert title_norm("  OpenAI 发布　“GPT-6”——新模型​！ ") \
        == 'OpenAI 发布 "GPT-6"--新模型!'
    assert title_norm("Ｔｅｓｔ　Ｘ") == "Test X"
    assert title_norm("AbC", lower=True) == "abc"
    print("title_norm OK")

    # --- aliases ------------------------------------------------------------
    al = load_aliases()  # repo aliases.json
    assert "OpenAI" in al and "DeepSeek" in al
    t = apply_aliases("deepseek 与 OpenAI公司 发布新模型", al)
    assert t == "DeepSeek 与 OpenAI 发布新模型", t
    # ASCII word-boundary: 'Yi' must not touch 'yield'/'metadata'
    t2 = apply_aliases("yield metadata Yi 系列", al)
    assert t2 == "yield metadata 零一万物 系列", t2
    # longest-alias-first: 'OpenAI ChatGPT' -> ChatGPT, not 'OpenAI ChatGPT'
    t3 = apply_aliases("OpenAI ChatGPT 发布", al)
    assert t3 == "ChatGPT 发布", t3
    assert apply_aliases(t3, al) == t3, "aliases must be idempotent"
    print("apply_aliases OK")

    # --- parse_date_utc ------------------------------------------------------
    assert parse_date_utc(1700000000) == "2023-11-14T22:13:20+00:00"
    assert parse_date_utc("1700000000000") == "2023-11-14T22:13:20+00:00"
    assert parse_date_utc("2026-09-21T06:30:00+08:00") == "2026-09-20T22:30:00+00:00"
    assert parse_date_utc("2026-09-21T06:30:00Z") == "2026-09-21T06:30:00+00:00"
    assert parse_date_utc("Mon, 21 Sep 2026 06:30:00 GMT") == "2026-09-21T06:30:00+00:00"
    assert parse_date_utc("2026/09/21 06:30") == "2026-09-20T22:30:00+00:00"
    assert parse_date_utc("2026-09-21 06:30") == "2026-09-20T22:30:00+00:00"
    assert parse_date_utc("garbage") is None
    assert parse_date_utc(None) is None and parse_date_utc("") is None
    print("parse_date_utc OK")
    print("normalize.py self-test OK")
