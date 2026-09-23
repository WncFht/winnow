#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.28",
#   "pydantic>=2",
# ]
# ///
"""x_nitter.py — X/Twitter 采集路①：nitter 实例池（PLAN.md §5.3）。

fetch_user(handle, cfg, *, diag=None, run_dir=None) -> list[raw_item dict]
    按持久化健康分（state/x_nitter_health.json：成功 +1 / 失败 -2）排序并
    在 top 实例间轮换，经代理抓 https://<inst>/<handle>/rss，把 nitter RSS
    解析为 raw_item/1 契约 dict；link/guid 中的 status id 重写回
    https://x.com/<user>/status/<id> —— 跨实例去重身份不依赖实例域名。
    diag 传入 dict 则回填 {attempts[], via="nitter:<inst>", feed_url}；
    run_dir 给定时命中实例的 RSS 原文经 lib.http.save_raw 落
    data/raw_cache 并给全体 item 记 _raw_ref（落盘失败不阻塞）。
    全部实例失败 → raise AllRoutesDead（.attempts 带每实例诊断）。

fetch_search(query, cfg, *, diag=None) -> list[raw_item dict]
    关键词搜索路：GET https://<inst>/search/rss?f=tweets&q=<query>，
    同一实例池/健康分/轮换/解析管道；source_name 记 "x-search:<query>"，
    全灭 → AllRoutesDead("search:<query>")。

实例池 = cfg.x_collector.nitter_instances（操作员优先）+ 实验目录种子
（experiments/hard-x.com-rsshub-or-mirror-instance/：nitter-*.rss 文件名
与其 channel <atom:link>/<link> 暴露验证过的实例；twiiit-instances.txt、
nitter-status.html 补充候选）+ DEFAULT_INSTANCES 兜底。

实测要点（experiments .../notes.md 2026-09-21）：
  * UA 必须非浏览器 —— Mozilla UA 在多数实例吃 Anubis PoW 挑战页；
    Miniflux/curl/python UA 放行 → 用 NITTER_UA。
  * RSS 仅最近 ~20 条；title=推文全文；description=CDATA HTML
    （含展开链接+媒体图）；link=/<user>/status/<id>#m；guid=status id。
  * 实例靠捐赠 session 存活、随时会死 → 池化+健康分是硬需求。

契约映射说明：raw_item/1 的 _source.kind ∈ rss|atom|api|scrape|manual 且
extra="forbid" —— nitter feed 本质即 'rss'，实例名由 _source.feed_url 承载，
非标 x.com 路由由 _fetch.via="mirror" 标记（任务描述里的 kind:'x'/
via:'nitter:<inst>' 与契约枚举冲突，以 PLAN.md §4 契约为准）。

Smoke:  uv run stages/lib/x_nitter.py            # 含 live 探测
        uv run stages/lib/x_nitter.py --offline  # 跳过 live
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

# stages/lib/*.py：脚本目录(stages/lib)在 sys.path[0] 会遮蔽 stdlib http
# （httpx 依赖它）——先摘掉，再补 stages/（from lib import …）与 repo root
# （from contracts/adapters import …）。
_SELF_DIR = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path
               if str(Path(p or ".").resolve()) != _SELF_DIR]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # stages/
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from lib import http as _http          # noqa: E402
from lib import normalize as _norm     # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
EXP_DIR = REPO_ROOT / "experiments" / "hard-x.com-rsshub-or-mirror-instance"
HEALTH_PATH = REPO_ROOT / "state" / "x_nitter_health.json"

# 2026-09-21 实测验证（notes.md）：meowing.monster 主、jaydenha.uk 备、
# thepixora 慢备（nitter.thepixora.com 307 → shitter.thepixora.com）。
DEFAULT_INSTANCES = [
    "nitter.meowing.monster",
    "nitter.jaydenha.uk",
    "nitter.thepixora.com",
    "shitter.thepixora.com",
]

NITTER_UA = "Miniflux/2.2.14"           # 非浏览器 UA（Anubis 放行）
TOP_ROTATE = 4                          # 轮换窗口：top-N 内轮转起步
DEFAULT_MAX_ATTEMPTS = 8                # 单次 fetch_user 最多尝试的实例数
DEFAULT_TIMEOUT = 25.0                  # thepixora 实测 3–35s，给足余量
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")

_DC = "{http://purl.org/dc/elements/1.1/}"


class AllRoutesDead(RuntimeError):
    """池内所有候选实例均失败。`.attempts` 带每实例诊断。"""

    def __init__(self, handle: str, attempts: list[dict]):
        self.handle = handle
        self.attempts = attempts
        brief = "; ".join(f"{a['instance']}:{a['error']}" for a in attempts)
        super().__init__(f"all nitter routes dead for @{handle}: {brief}")


# ------------------------------------------------------------------ cfg ----

def _cfg_get(cfg: Any, path: str, default=None):
    """dotted-path lookup，兼容 dict 与属性对象。"""
    cur = cfg
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return default if cur is None else cur


def _norm_instance(entry: Any) -> Optional[str]:
    """'https://host/path' | 'host/path' | 'host' -> 'host'（小写）。"""
    if not isinstance(entry, str) or not entry.strip():
        return None
    e = entry.strip()
    host = urlsplit(e if "://" in e else f"https://{e}").hostname
    return host.lower() if host else None


# ------------------------------------------------------------ seed pool ----

def _mine_seed_hosts() -> list[str]:
    """从 experiments/hard-x.com-*/ 挖验证过/候选的实例 host。

    - nitter-*.rss：channel <atom:link href>/<link> 的 host（文件头是真实
      服务实例，含 307 后的落地 host）；文件名 nitter-<host>-<Handle>.rss
      中 host 含合法 TLD 时也收。
    - twiiit-instances.txt：每行 https://<host>/…
    - nitter-status.html：status.d420.de 快照表格里的实例链接。
    """
    hosts: list[str] = []
    seen = set()

    def add(h: Optional[str]):
        h = _norm_instance(h or "")
        if h and h not in seen and "." in h:
            seen.add(h)
            hosts.append(h)

    if EXP_DIR.is_dir():
        for p in sorted(EXP_DIR.glob("nitter-*.rss")):
            try:
                head = p.read_bytes()[:8192].decode("utf-8", "replace")
            except OSError:
                continue
            for m in re.finditer(
                    r'<atom:link[^>]+href="https?://([^"/\s]+)', head):
                add(m.group(1))
            for m in re.finditer(
                    r"<link>\s*https?://([^/<\s]+)", head):
                add(m.group(1))
            m = re.match(
                r"nitter-([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)-[^/]+\.rss$",
                p.name)
            if m:
                add(m.group(1))

        twiiit = EXP_DIR / "twiiit-instances.txt"
        if twiiit.is_file():
            for line in twiiit.read_text(encoding="utf-8",
                                         errors="replace").splitlines():
                m = re.match(r"\s*https?://([^/\s]+)", line)
                if m:
                    add(m.group(1))

        status = EXP_DIR / "nitter-status.html"
        if status.is_file():
            try:
                txt = html.unescape(
                    status.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                txt = ""
            for m in re.finditer(r'href="https://([a-zA-Z0-9.-]+)"', txt):
                add(m.group(1))
    return hosts


def instance_pool(cfg: Any) -> list[str]:
    """合并池：cfg.x_collector.nitter_instances → 实验种子 → 内置默认。"""
    pool: list[str] = []
    seen = set()

    def add(entry):
        h = _norm_instance(entry)
        if h and h not in seen:
            seen.add(h)
            pool.append(h)

    for e in (_cfg_get(cfg, "x_collector.nitter_instances", []) or []):
        add(e)
    for h in _mine_seed_hosts():
        add(h)
    for h in DEFAULT_INSTANCES:
        add(h)
    return pool


# --------------------------------------------------------------- health ----

def _health_path(cfg: Any = None) -> Path:
    p = _cfg_get(cfg, "x_collector.health_file") if cfg is not None else None
    p = p or os.environ.get("X_NITTER_HEALTH")
    return Path(p) if p else HEALTH_PATH


def _load_health(cfg: Any = None) -> dict:
    p = _health_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(
                data.get("instances"), dict):
            data.setdefault("rr", 0)
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"schema": "x_nitter_health/1", "rr": 0, "instances": {}}


def _save_health(health: dict, cfg: Any = None) -> None:
    p = _health_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _bump(health: dict, inst: str, ok: bool, err: str = "ok") -> None:
    e = health["instances"].setdefault(
        inst, {"score": 0, "ok": 0, "fail": 0})
    if ok:
        e["score"] = int(e.get("score", 0)) + 1
        e["ok"] = int(e.get("ok", 0)) + 1
        e["last_ok"] = _utcnow()
    else:
        e["score"] = int(e.get("score", 0)) - 2
        e["fail"] = int(e.get("fail", 0)) + 1
        e["last_fail"] = _utcnow()
    e["last_err"] = err


def _ordered(pool: list[str], health: dict) -> list[str]:
    """健康分降序；top-TOP_ROTATE 窗口按持久化 rr 轮转起点。"""
    inst = health.get("instances", {})
    ranked = sorted(
        pool,
        key=lambda h: (-int(inst.get(h, {}).get("score", 0)), h))
    k = min(TOP_ROTATE, len(ranked))
    if k <= 1:
        return ranked
    rr = int(health.get("rr", 0)) % k
    return ranked[rr:k] + ranked[:rr] + ranked[k:]


# --------------------------------------------------------------- parsing ---

_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>", re.I)
_WS = re.compile(r"[ \t\xa0]+")
_IMG = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_STATUS = re.compile(r"/status/(\d+)")
_FIRST_P = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)


def _text_of(fragment: str) -> str:
    frag = _BR.sub("\n", fragment or "")
    txt = html.unescape(_TAG.sub("", frag))
    lines = [_WS.sub(" ", ln).strip() for ln in txt.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _content_text(desc: str, fallback: str) -> str:
    """description 首个 <p> 是推文正文；其后是媒体/卡片引用，剔除。"""
    m = _FIRST_P.search(desc or "")
    body = _text_of(m.group(1)) if m else _text_of(desc or "")
    return body or (fallback or "")


def _status_id(link: str, guid: str) -> Optional[str]:
    m = _STATUS.search(link or "")
    if m:
        return m.group(1)
    g = (guid or "").strip()
    return g if g.isdigit() else None


def _status_author(link: str, creator: str, handle: str) -> str:
    """status URL 的 author 段：link path > dc:creator > 调用 handle。"""
    m = re.search(r"https?://[^/]+/([^/]+)/status/\d+", link or "")
    if m:
        return m.group(1)
    c = (creator or "").lstrip("@").strip()
    return c or handle


def _absolutize(src: str, inst: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return f"https://{inst}{src}"
    return src


def _rfc3339(pub: str) -> Optional[str]:
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def parse_rss(body: bytes, inst: str, handle: str, *,
              status: int = 200, etag: Optional[str] = None,
              fetched_at: Optional[str] = None,
              source_name: Optional[str] = None) -> list[dict]:
    """nitter RSS body -> raw_item/1 dicts。非 RSS/零条目 -> []（调用方判负）。"""
    head = body[:512].lstrip()[:200].lower()
    if not (head.startswith(b"<?xml") or head.startswith(b"<rss")
            or head.startswith(b"<feed")):
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    fetched = fetched_at or _utcnow()
    feed_url = f"https://{inst}/{handle}/rss"
    sha = hashlib.sha256(body).hexdigest()[:16]
    items: list[dict] = []

    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        desc = it.findtext("description") or ""
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        creator = it.findtext(f"{_DC}creator") or ""
        sid = _status_id(link, guid)
        if not sid or not title:
            continue
        author = _status_author(link, creator, handle)
        url = f"https://x.com/{author}/status/{sid}"
        canon = _norm.url_canon(url)
        img_m = _IMG.search(desc)
        item = {
            "schema": "raw_item/1",
            "item_key": _norm.item_key(url),
            "id": _norm.item_key(url),
            "url": url,
            "url_canon": canon,
            "title": _norm.title_norm(title),
            "content_text": _content_text(desc, _norm.title_norm(title)),
            "date_published": _rfc3339(it.findtext("pubDate") or ""),
            "date_fetched": fetched,
            "language": None,
            "tags": ["x", f"@{author}"],
            "image": _absolutize(img_m.group(1), inst) if img_m else None,
            "_source": {
                "name": source_name or f"x:@{handle}",
                "feed_url": feed_url,
                "kind": "rss",
                "item_guid": sid,
            },
            "_fetch": {
                "status": status,
                "via": "mirror",
                "reachable": True,
                "etag": etag,
                "content_sha256": sha,
            },
        }
        items.append(item)
    return items


# ---------------------------------------------------------------- fetch ----

def _proxy_arg(cfg: Any):
    p = _cfg_get(cfg, "proxy.http") or _cfg_get(cfg, "proxy.https")
    if isinstance(p, str) and p:
        return p
    return None  # -> env（HTTP(S)_PROXY/ALL_PROXY）；本机 env 即 7890


def _fetch_rss(url: str, cfg: Any) -> "_http.FetchResult":
    timeout = float(_cfg_get(cfg, "x_collector.timeout", DEFAULT_TIMEOUT)
                    or DEFAULT_TIMEOUT)
    return _http.get(
        url,
        proxy=_proxy_arg(cfg),
        timeout=timeout,
        headers={
            "User-Agent": NITTER_UA,
            "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )


def _attempt(inst: str, handle: str, cfg: Any,
             ) -> tuple[list[dict], dict, Optional[bytes]]:
    """单实例一次尝试 -> (items, attempt-record, response_body)。"""
    url = f"https://{inst}/{handle}/rss"
    rec = {"instance": inst, "url": url, "status": 0, "error": "ok",
           "latency_ms": 0, "n_items": 0}
    res = _fetch_rss(url, cfg)
    rec.update(status=res.status, error=res.error,
               latency_ms=res.latency_ms, via_transport=res.via)
    if res.detail:
        rec["detail"] = res.detail[:200]
    items: list[dict] = []
    if res.ok and res.body:
        items = parse_rss(res.body, inst, handle,
                          status=res.status, etag=res.etag,
                          source_name=_cfg_get(cfg, "_source_name"))
        if not items:
            rec["error"] = "parse_error"   # 200 但非 RSS/无 item → 判负
    rec["n_items"] = len(items)
    return items, rec, res.body if res.ok else None


def fetch_user(handle: str, cfg: Any, *,
               diag: Optional[dict] = None,
               run_dir: Optional[Path] = None) -> list[dict]:
    """抓 @handle 时间线 -> raw_item/1 dicts；全灭 -> AllRoutesDead。

    diag: 传入 dict 则回填 {attempts[], via, feed_url}。run_dir 给定时把
    命中的 RSS 原文经 lib.http.save_raw 落 raw_cache，首个 item 记 _raw_ref。
    """
    handle = (handle or "").strip().lstrip("@")
    if not _HANDLE_RE.match(handle):
        raise ValueError(f"bad X handle: {handle!r}")

    health = _load_health(cfg)
    pool = instance_pool(cfg)
    if not pool:
        raise AllRoutesDead(handle, [{"instance": "<pool>", "error":
                                      "empty_pool"}])
    ordered = _ordered(pool, health)
    health["rr"] = int(health.get("rr", 0)) + 1   # 下次轮换起点前移
    max_att = int(_cfg_get(cfg, "x_collector.max_attempts",
                           DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
    attempts: list[dict] = []
    items: list[dict] = []
    winner: Optional[str] = None
    win_body: Optional[bytes] = None

    for inst in ordered[:max(1, max_att)]:
        got, rec, rbody = _attempt(inst, handle, cfg)
        attempts.append(rec)
        _bump(health, inst, bool(got), rec["error"])
        _save_health(health, cfg)                # 每次尝试即落盘，崩溃不丢
        if got:
            items, winner, win_body = got, inst, rbody
            break

    if diag is not None:
        diag["attempts"] = attempts
        diag["via"] = f"nitter:{winner}" if winner else None
        diag["feed_url"] = (f"https://{winner}/{handle}/rss"
                            if winner else None)

    if not items:
        raise AllRoutesDead(handle, attempts)

    if run_dir is not None and win_body:
        try:
            ref = _http.save_raw(
                run_dir, f"x_{handle}",
                f"https://{winner}/{handle}/rss", win_body)
            for it in items:
                it["_raw_ref"] = ref
        except Exception:
            pass                               # 原文落盘失败不阻塞
    return items


def fetch_search(query: str, cfg: Any, *,
                 diag: Optional[dict] = None) -> list[dict]:
    """关键词搜索 RSS（/search/rss?f=tweets&q=…，实测可用）-> raw_items。"""
    from urllib.parse import quote
    health = _load_health(cfg)
    pool = instance_pool(cfg)
    ordered = _ordered(pool, health)
    health["rr"] = int(health.get("rr", 0)) + 1
    max_att = int(_cfg_get(cfg, "x_collector.max_attempts",
                           DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
    attempts: list[dict] = []
    for inst in ordered[:max(1, max_att)]:
        url = f"https://{inst}/search/rss?f=tweets&q={quote(query)}"
        rec = {"instance": inst, "url": url, "status": 0,
               "error": "ok", "latency_ms": 0, "n_items": 0}
        res = _fetch_rss(url, cfg)
        rec.update(status=res.status, error=res.error,
                   latency_ms=res.latency_ms)
        items = parse_rss(res.body or b"", inst, "search",
                          status=res.status,
                          etag=res.etag,
                          source_name=f"x-search:{query}") if res.ok else []
        rec["n_items"] = len(items)
        attempts.append(rec)
        _bump(health, inst, bool(items), rec["error"])
        _save_health(health, cfg)
        if items:
            if diag is not None:
                diag["attempts"] = attempts
                diag["via"] = f"nitter:{inst}"
            return items
    if diag is not None:
        diag["attempts"] = attempts
        diag["via"] = None
    raise AllRoutesDead(f"search:{query}", attempts)


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":
    offline = "--offline" in sys.argv
    fails: list[str] = []

    # --- 离线：真实样本解析 + 契约校验 --------------------------------------
    fixture = EXP_DIR / "nitter-thepixora-OpenAI.rss"
    body = fixture.read_bytes()
    items = parse_rss(body, "shitter.thepixora.com", "OpenAI",
                      status=200)
    assert len(items) >= 10, f"fixture items={len(items)}"
    it0 = items[0]
    assert it0["url"].startswith("https://x.com/OpenAI/status/"), it0["url"]
    assert it0["_source"]["kind"] == "rss"
    assert it0["_fetch"]["via"] == "mirror"
    assert it0["date_published"] and it0["date_published"].endswith("Z")
    assert it0["content_text"], "content_text empty"
    try:
        from contracts.models import RawItem
        for it in items:
            RawItem.model_validate(it)
        print(f"offline: {len(items)} items parse + RawItem validate OK")
    except ImportError:
        print("offline: contracts unavailable — skipped pydantic validate")

    # --- 离线：健康分/轮换/全灭 ---------------------------------------------
    tmp_state = (Path.home() / ".cache" / "ainews_xnitter_selftest"
                 / "x_nitter_health.json")
    tmp_state.parent.mkdir(parents=True, exist_ok=True)
    if tmp_state.exists():
        tmp_state.unlink()
    os.environ["X_NITTER_HEALTH"] = str(tmp_state)
    cfg_off = {"x_collector": {"nitter_instances": ["nitter.jaydenha.uk",
                                                    "nitter.meowing.monster"],
                             "max_attempts": 2},
               "proxy": {"http": "direct"}}
    h = _load_health(cfg_off)
    _bump(h, "nitter.meowing.monster", True)
    _bump(h, "nitter.jaydenha.uk", False, "http_502")
    _save_health(h, cfg_off)
    h2 = _load_health(cfg_off)
    assert h2["instances"]["nitter.meowing.monster"]["score"] == 1
    assert h2["instances"]["nitter.jaydenha.uk"]["score"] == -2
    order = _ordered(["a.example", "nitter.meowing.monster",
                      "nitter.jaydenha.uk"], h2)
    assert order[-1] == "nitter.jaydenha.uk", order   # 低分垫底
    # 全灭路径：monkeypatch 池为必死实例（.invalid DNS 秒败，无真实流量）
    _orig_pool = instance_pool
    globals()["instance_pool"] = lambda cfg: ["dead.invalid"]
    try:
        fetch_user("OpenAI",
                   {"x_collector": {"max_attempts": 1},
                    "proxy": {"http": "direct"}},
                   diag={})
        fails.append("dead instance did not raise AllRoutesDead")
    except AllRoutesDead as e:
        assert e.attempts and e.attempts[0]["error"] != "ok"
    finally:
        globals()["instance_pool"] = _orig_pool
    print("offline: health scoring + rotation + AllRoutesDead OK")
    os.environ.pop("X_NITTER_HEALTH")

    # --- live：≥4 实例经代理逐探测 + fetch_user 端到端 -----------------------
    if not offline:
        proxy = "http://127.0.0.1:7890"
        cfg_live = {
            "proxy": {"http": proxy},
            "x_collector": {
                "nitter_instances": DEFAULT_INSTANCES,
                "timeout": 25,
                "max_attempts": 8,
            },
        }
        pool = instance_pool(cfg_live)
        print("pool:", pool)
        probe_n = 0
        for inst in pool:
            if probe_n >= 6:
                break
            got, rec, _ = _attempt(inst, "OpenAI", cfg_live)
            probe_n += 1
            print(f"  probe {inst}: status={rec['status']} "
                  f"err={rec['error']} items={rec['n_items']} "
                  f"{rec['latency_ms']}ms")
        if probe_n < 4:
            fails.append(f"only {probe_n} instances probed")
        # 端到端（用临时 health，不污染真实 state）
        os.environ["X_NITTER_HEALTH"] = str(tmp_state)
        try:
            d: dict = {}
            out = fetch_user("OpenAI", cfg_live, diag=d)
            print(f"fetch_user: {len(out)} items via {d.get('via')}")
            try:
                from contracts.models import RawItem
                RawItem.model_validate(out[0])
                print("live item RawItem validate OK")
            except ImportError:
                pass
        except AllRoutesDead as e:
            print(f"fetch_user AllRoutesDead: {e}")
        finally:
            os.environ.pop("X_NITTER_HEALTH")
        if probe_n >= 4:
            print(f"live probe done: {probe_n} instances attempted")
    else:
        print("live checks skipped (--offline)")

    if fails:
        print("FAIL:", fails)
        sys.exit(1)
    print("x_nitter.py self-test OK")
