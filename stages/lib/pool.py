#!/usr/bin/env python3
"""stages/lib/pool.py — 跨期条目池 state/items.sqlite（单文件 WAL，行永不删）。

一行 = 一条新闻的机械身份（item_key = sha256(url_canon)[:16]），跨 episode
累积生命周期：collect 每期 upsert 原文列；filter 回写 verdict 列；summary
回写概要列；dedup 回写 cluster；出片后 mark_used 标记 used_in_episode。

不变量（写路径集中在本模块，调用方不拼 SQL）：
  * content_sha 全库唯一定义：sha256(title \\x1f date_published \\x1f
    content_text \\x1f source_name)[:16]，按归一化后字段计算（归一化幂等，
    filter 重归一不漂移）。upsert 时内容漂移 → filter 判定列全部置 NULL
    重判；used_in_episode/dedup_*/summary_* 语义列永不被 upsert 覆盖。
  * item_runs(item_key, episode) = 该条目出现在哪几期采集批；
    seen_count = COUNT(item_runs) 由 upsert 同事务维护。
  * date_published 入库前统一过 normalize.parse_date_utc（RFC3339 UTC
    秒）——窗口比较是纯字符串比较，靠这个不变量成立。

select_candidates 语义（结转候选，非当期批次）：当期 item_runs 成员被排除
（当期条目走文件管线 10→40），used_in_episode=当期 的条目豁免（同 episode
内可重选）。窗口三子句：A pub∈[wfrom,wto) / B pub NULL 且 first_seen≥grace /
C pub∈[sfloor,wfrom) 且 first_seen≥grace（迟到结转，sfloor=wfrom-
carry_stale_max_days，更老的古董不结转）；Python 精修把 C 对 daily 源
（sources.yaml daily:true，每日快照页）关掉 —— 陈旧日期不结转
（stale-daily 死区）。projected_dedup 叠加"已出片 cluster"投影。

stdlib only —— 与 lib/meta.py 同级约定，PEP723 stage 脚本可安全 import
（pydantic/yaml 只在 __main__ 内惰性 import）。

用法::
    uv run stages/lib/pool.py --selftest              # :memory: 全量自测
    uv run stages/lib/pool.py --import runs/ [--sources sources.yaml] [--model M]
    uv run stages/lib/pool.py --db state/items.sqlite --stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo


from stages.lib import meta, normalize  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "state" / "items.sqlite"
TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

RAW_NAME = "10_raw_items.jsonl"
FILT_NAME = "20_filtered.jsonl"
SUMS_NAME = "30_summaries.jsonl"
DED_NAME = "35_dedup.jsonl"
SEL_NAME = "40_selected.json"

VERDICTS = ("keep", "drop", "review")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# filter.py 占位/非 LLM 判定行（导入时拒收——重跑 filter 会覆盖，不入库以免
# 污染 filter_verdict）：l0 命中 / no-llm 模式 / coverage-miss 重批仍缺。
_PLACEHOLDER_MODELS = ("l0-url-hash", "no-llm")
_PLACEHOLDER_REASON = ("coverage-miss", "no-llm")

log = logging.getLogger("pool")

# ---------------------------------------------------------------------------
# schema（DDL 逐字；user_version=1）
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS items (
  item_key TEXT PRIMARY KEY,            -- sha256(url_canon)[:16]
  url TEXT NOT NULL, url_canon TEXT NOT NULL, title TEXT NOT NULL,
  content_text TEXT,                     -- <=8000 chars, capped by collect
  date_published TEXT,                   -- RFC3339 UTC seconds or NULL
  date_fetched TEXT NOT NULL,
  language TEXT, image TEXT, raw_ref TEXT,
  source_name TEXT NOT NULL, source_feed_url TEXT, source_kind TEXT, item_guid TEXT,
  tags_json TEXT NOT NULL DEFAULT '[]',
  signal INTEGER NOT NULL DEFAULT 0,     -- sticky MAX(tags has 'signal')
  daily INTEGER NOT NULL DEFAULT 0,      -- sources.yaml daily: snapshot at collect
  content_sha TEXT NOT NULL,             -- sha256(title (date_published||'') (content_text||'') source_name)[:16]
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,   -- episode dates 'YYYY-MM-DD'
  seen_count INTEGER NOT NULL DEFAULT 1,
  filter_verdict TEXT,                   -- 'keep'|'drop'|'review'; NULL = unjudged
  ai_relevance REAL, news_value REAL, reasons_json TEXT,
  verdict_prompt TEXT,                   -- 'filter-v2'|'injection-guard-v1'
  verdict_model TEXT, verdict_at TEXT,
  judged_content_sha TEXT,
  title_zh TEXT, summary TEXT, entities_json TEXT, facts_json TEXT, section_guess TEXT,
  summary_sha TEXT,                    -- 被概要内容的 content_sha（缓存命中键，非概要指纹）
  summary_prompt TEXT, summary_model TEXT, summary_at TEXT,
  dedup_verdict TEXT, dedup_cluster_id INTEGER, dedup_match_cos REAL, dedup_judge TEXT, dedup_at TEXT,
  used_in_episode TEXT, used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_verdict ON items(filter_verdict);
CREATE INDEX IF NOT EXISTS idx_items_used ON items(used_in_episode);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(date_published);
CREATE INDEX IF NOT EXISTS idx_items_first ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_name);
CREATE TABLE IF NOT EXISTS item_runs (
  item_key TEXT NOT NULL REFERENCES items(item_key),
  episode TEXT NOT NULL,
  PRIMARY KEY (item_key, episode)
);
"""

_BASE_COLS = ("item_key", "url", "url_canon", "title", "content_text",
              "date_published", "date_fetched", "language", "image", "raw_ref",
              "source_name", "source_feed_url", "source_kind", "item_guid",
              "tags_json", "signal", "daily", "content_sha",
              "first_seen", "last_seen", "seen_count")
_JSON_COLS = ("tags_json", "reasons_json", "entities_json", "facts_json")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today_sh() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _js(v, default):
    """*_json 列解码：已解析对象原样，str 走 json.loads，坏值→default。"""
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default
    return v


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _src_of(it: dict) -> dict:
    s = it.get("_source")
    return s if isinstance(s, dict) else {}


def content_sha(item: dict) -> str:
    """条目内容指纹（全库唯一定义，collect upsert / filter 查库共用）。

    sha256(title ␟ date_published ␟ content_text ␟ source_name)[:16]，
    字段取归一化后的值；缺省一律 ''（␟=U+001F 分隔，免拼接歧义）。
    """
    src = item.get("source_name")
    if src is None:
        src = _src_of(item).get("name")
    parts = [item.get("title") or "", item.get("date_published") or "",
             item.get("content_text") or "", src or ""]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _prov(model, prompt_tag, key, ts=None) -> dict:
    """与 filter._prov 同约定：input_sha = sha256(item_key)[:16]。"""
    return {"model": model, "prompt": prompt_tag,
            "input_sha": hashlib.sha256(str(key).encode()).hexdigest()[:16],
            "decided_at": ts or _utcnow()}


def _base_cols(it: dict, episode: str) -> dict:
    """raw_item dict（或池行 dict）→ items 基列参数。item_key 由调用方按
    url_canon 重算覆盖；date_published 统一过 parse_date_utc 保持 UTC 秒。"""
    src = _src_of(it)
    uc = it.get("url_canon") or normalize.url_canon(it.get("url") or "")
    tags = it.get("tags")
    if tags is None:
        tags = _js(it.get("tags_json"), [])
    tags = [str(t) for t in (tags or [])]
    return {
        "item_key": it.get("item_key") or (normalize.item_key(uc) if uc else ""),
        "url": it.get("url") or "",
        "url_canon": uc,
        "title": it.get("title") or "",
        "content_text": it.get("content_text"),
        "date_published": normalize.parse_date_utc(it.get("date_published")),
        "date_fetched": it.get("date_fetched") or _utcnow(),
        "language": it.get("language"),
        "image": it.get("image"),
        "raw_ref": it.get("raw_ref") if it.get("raw_ref") is not None
        else it.get("_raw_ref"),
        "source_name": it.get("source_name") or src.get("name") or "",
        "source_feed_url": it.get("source_feed_url") or src.get("feed_url") or "",
        "source_kind": it.get("source_kind") or src.get("kind"),
        "item_guid": it.get("item_guid") or src.get("item_guid"),
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "signal": 1 if "signal" in tags else int(it.get("signal") or 0),
        "daily": int(it.get("daily") or 0),
        "content_sha": content_sha(it),
        "first_seen": it.get("first_seen") or episode,
        "last_seen": episode,
        "seen_count": 1,
    }


def _decoded(d: dict) -> dict:
    for c in _JSON_COLS:
        d[c] = _js(d.get(c), [])
    return d


# ---------------------------------------------------------------------------
# 连接 / 路径 / 窗口
# ---------------------------------------------------------------------------

def init_db(path) -> sqlite3.Connection:
    """打开/创建 items.sqlite：WAL + busy_timeout + schema。':memory:' 跑自测。"""
    p = str(path)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # :memory: 不支持 WAL，忽略
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def resolve_path(cli_arg, cfg_doc: Optional[dict] = None) -> Path:
    """--db > cfg_doc['storage']['items_db'] > state/items.sqlite（相对路径基于 repo 根）。"""
    rel = ((cfg_doc or {}).get("storage") or {}).get("items_db")
    p = Path(cli_arg) if cli_arg else (Path(rel) if rel else DEFAULT_DB)
    return p if p.is_absolute() else REPO_ROOT / p


def window_bounds(episode: str) -> tuple[str, str]:
    """canonical mirror of collect._window：W(D) = [D-1 06:30, D 06:30)
    Asia/Shanghai，返回 UTC RFC3339 'YYYY-MM-DDTHH:MM:SS+00:00' 对。"""
    try:
        d = datetime.strptime(str(episode), "%Y-%m-%d").date()
    except ValueError:
        d = datetime.now(TZ).date()
    end = datetime(d.year, d.month, d.day, 6, 30, tzinfo=TZ)
    start = end - timedelta(days=1)
    return (start.astimezone(UTC).isoformat(timespec="seconds"),
            end.astimezone(UTC).isoformat(timespec="seconds"))


def stale_floor(wfrom: str, days: int) -> str:
    """子句 C 的 pub 下限 = wfrom - N 天：只有窗口前 N 天内的迟到条目可
    结转，更老的一律 stale（归档源全量目录/池冷启动 flood 的截断阀）。"""
    return (datetime.fromisoformat(wfrom)
            - timedelta(days=max(int(days), 0))).isoformat()


# ---------------------------------------------------------------------------
# 写入：upsert / verdicts / summaries / dedup / used
# ---------------------------------------------------------------------------

_UPSERT_ITEM = """
INSERT INTO items (item_key,url,url_canon,title,content_text,date_published,
 date_fetched,language,image,raw_ref,source_name,source_feed_url,source_kind,
 item_guid,tags_json,signal,daily,content_sha,first_seen,last_seen,seen_count)
VALUES (:item_key,:url,:url_canon,:title,:content_text,:date_published,
 :date_fetched,:language,:image,:raw_ref,:source_name,:source_feed_url,
 :source_kind,:item_guid,:tags_json,:signal,:daily,:content_sha,
 :first_seen,:last_seen,1)
ON CONFLICT(item_key) DO UPDATE SET
  title=excluded.title, content_text=excluded.content_text,
  image=excluded.image, language=excluded.language, raw_ref=excluded.raw_ref,
  source_name=excluded.source_name, source_feed_url=excluded.source_feed_url,
  source_kind=excluded.source_kind, item_guid=excluded.item_guid,
  tags_json=excluded.tags_json, daily=excluded.daily,
  date_published=COALESCE(excluded.date_published, items.date_published),
  signal=MAX(items.signal, excluded.signal),
  last_seen=MAX(items.last_seen, excluded.last_seen),
  content_sha=excluded.content_sha,
  -- 内容漂移 → filter 判定作废重判；used_in_episode/dedup_* 不在此触碰
  filter_verdict=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                      ELSE items.filter_verdict END,
  judged_content_sha=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                          ELSE items.judged_content_sha END,
  verdict_prompt=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                      ELSE items.verdict_prompt END,
  verdict_model=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                     ELSE items.verdict_model END,
  verdict_at=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                  ELSE items.verdict_at END,
  ai_relevance=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                    ELSE items.ai_relevance END,
  news_value=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                  ELSE items.news_value END,
  reasons_json=CASE WHEN excluded.content_sha<>items.content_sha THEN NULL
                    ELSE items.reasons_json END
"""


def upsert_items(conn: sqlite3.Connection, items: Iterable[dict],
                 episode: str, daily_by_name: Optional[dict] = None) -> dict:
    """一期采集批 → items upsert + item_runs 登记 + seen_count 维护（单事务）。

    url_canon=='' 的行跳过并计数（空 key 碰撞隐患）。daily_by_name =
    {source_name: bool}（sources.yaml daily 快照，缺席→False）。
    """
    items = list(items or [])
    daily_by_name = daily_by_name or {}
    cols, skipped = [], 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        c = _base_cols(it, episode)
        if not c["url_canon"]:
            skipped += 1
            continue
        c["item_key"] = normalize.item_key(c["url_canon"])
        c["daily"] = 1 if daily_by_name.get(c["source_name"]) else 0
        cols.append(c)
    if skipped:
        log.warning("pool.upsert_items: %d 行 url_canon 为空/非 dict 已跳过", skipped)
    stats = {"episode": episode, "received": len(items),
             "skipped_empty_url": skipped, "inserted": 0, "updated": 0,
             "rejudged": 0}
    if not cols:
        return stats
    keys = [c["item_key"] for c in cols]
    old = {}
    for ch in _chunks(keys):
        q = ",".join("?" * len(ch))
        for k, sha in conn.execute(
                "SELECT item_key, content_sha FROM items"
                f" WHERE item_key IN ({q})", ch):
            old[k] = sha
    stats["inserted"] = sum(1 for k in keys if k not in old)
    stats["updated"] = len(keys) - stats["inserted"]
    stats["rejudged"] = sum(1 for c in cols if c["item_key"] in old
                            and old[c["item_key"]] != c["content_sha"])
    with conn:
        conn.executemany(_UPSERT_ITEM, cols)
        conn.executemany(
            "INSERT OR IGNORE INTO item_runs(item_key,episode) VALUES(?,?)",
            [(k, episode) for k in keys])
        for ch in _chunks(keys):
            q = ",".join("?" * len(ch))
            conn.execute(
                "UPDATE items SET seen_count=(SELECT COUNT(*) FROM item_runs r"
                " WHERE r.item_key=items.item_key)"
                f" WHERE item_key IN ({q})", ch)
    return stats


def get_many(conn: sqlite3.Connection, keys: Iterable[str]) -> dict:
    """item_key -> dict(row)；*_json 列已解码为对象。"""
    ks = [k for k in dict.fromkeys(keys or []) if k]
    out = {}
    for ch in _chunks(ks):
        q = ",".join("?" * len(ch))
        for r in conn.execute(
                f"SELECT * FROM items WHERE item_key IN ({q})", ch):
            out[r["item_key"]] = _decoded(dict(r))
    return out


def _upsert_judged(conn, items_by_key, rows, extra: dict, sql: str,
                   episode: str) -> int:
    """判定/概要列 UPSERT 公共骨架：未知 key 走 INSERT 自愈（base 列由
    items_by_key 填，first_seen=episode），已存在只 UPDATE 本次的列。"""
    params = []
    for row in rows or []:
        k = row.get("item_key")
        if not k:
            continue
        it = (items_by_key or {}).get(k) or {}
        c = _base_cols(it, episode)
        c["item_key"] = k
        c.update(extra(row, it, k))
        params.append(c)
    if params:
        with conn:
            conn.executemany(sql, params)
    return len(params)


def write_verdicts(conn: sqlite3.Connection, items_by_key: dict,
                   verdict_rows: Iterable[dict],
                   prompt_tag_of: Callable[[str], str],
                   episode: Optional[str] = None) -> int:
    """20_filtered 行 → 判定列 + judged_content_sha（=当期 raw 的 content_sha）。

    items_by_key: item_key -> 当期 raw dict（自愈 INSERT 用；无则插桩行）。
    prompt_tag_of(key)->prompt 版本兜底（row.prov.prompt 优先）。
    """
    episode = episode or _today_sh()

    def extra(vr, it, k):
        prov = vr.get("prov") or {}
        try:
            tag = prompt_tag_of(k) if callable(prompt_tag_of) else None
        except Exception:
            tag = None
        return {
            "filter_verdict": vr.get("verdict")
            if vr.get("verdict") in VERDICTS else "review",
            "ai_relevance": _f(vr.get("ai_relevance")),
            "news_value": _f(vr.get("news_value")),
            "reasons_json": json.dumps(
                [str(r) for r in (vr.get("reasons") or [])],
                ensure_ascii=False),
            "verdict_prompt": prov.get("prompt") or tag,
            "verdict_model": prov.get("model"),
            "verdict_at": prov.get("decided_at") or _utcnow(),
            "judged_content_sha": content_sha(it),
        }

    return _upsert_judged(conn, items_by_key, verdict_rows, extra,
                          _UPSERT_VERDICT, episode)


def write_summaries(conn: sqlite3.Connection, items_by_key: dict,
                    summary_rows: Iterable[dict],
                    episode: Optional[str] = None) -> int:
    """30_summaries 行 → 概要列 + summary_sha/prompt/model/at。

    summary_sha = 被概要内容的 content_sha（与 judged_content_sha 同口径的
    输入指纹）——缓存命中判定键：条目内容漂移 → sha 变 → 概要自动失效重概。
    """
    episode = episode or _today_sh()

    def extra(sr, it, k):
        prov = sr.get("prov") or {}
        ents = [str(e) for e in (sr.get("entities") or [])]
        facts = [str(f) for f in (sr.get("facts") or [])]
        return {
            "title_zh": sr.get("title_zh"), "summary": sr.get("summary"),
            "entities_json": json.dumps(ents, ensure_ascii=False),
            "facts_json": json.dumps(facts, ensure_ascii=False),
            "section_guess": sr.get("section_guess"),
            "summary_sha": content_sha(it),
            "summary_prompt": prov.get("prompt"),
            "summary_model": prov.get("model"),
            "summary_at": prov.get("decided_at") or _utcnow(),
        }

    return _upsert_judged(conn, items_by_key, summary_rows, extra,
                          _UPSERT_SUMMARY, episode)


_VERDICT_COLS = ("filter_verdict,ai_relevance,news_value,reasons_json,"
                 "verdict_prompt,verdict_model,verdict_at,judged_content_sha")
_SUMMARY_COLS = ("title_zh,summary,entities_json,facts_json,section_guess,"
                 "summary_sha,summary_prompt,summary_model,summary_at")


def _upsert_sql(extra_cols: str) -> str:
    ins = ",".join(_BASE_COLS) + "," + extra_cols
    vals = ",".join(":" + c for c in _BASE_COLS) + \
        "," + ",".join(":" + c for c in extra_cols.split(","))
    upd = ",".join(f"{c}=excluded.{c}" for c in extra_cols.split(","))
    return (f"INSERT INTO items ({ins}) VALUES ({vals})"
            f" ON CONFLICT(item_key) DO UPDATE SET {upd}")


_UPSERT_VERDICT = _upsert_sql(_VERDICT_COLS)
_UPSERT_SUMMARY = _upsert_sql(_SUMMARY_COLS)


def write_dedup(conn: sqlite3.Connection, dedup_rows: Iterable[dict]) -> int:
    """35_dedup 行 → dedup_* 列。返回实际写入行数。

    自压制护栏：新 verdict='suppressed' 且库内 dedup_cluster_id 已等于新
    cluster_id → 跳过（同 cluster 的迟到 dup_exact 再抵达，不许杀掉在位的
    'fresh'/'reissue'）。
    """
    n = 0
    with conn:
        for r in dedup_rows or []:
            k = r.get("item_key")
            if not k:
                continue
            v, cid = r.get("verdict"), r.get("cluster_id")
            if v == "suppressed" and cid is not None:
                cur = conn.execute(
                    "SELECT dedup_cluster_id FROM items WHERE item_key=?",
                    (k,)).fetchone()
                if cur and cur["dedup_cluster_id"] == cid:
                    continue                      # self-suppression guard
            judge = r.get("judge")
            n += conn.execute(
                "UPDATE items SET dedup_verdict=?, dedup_cluster_id=?,"
                " dedup_match_cos=?, dedup_judge=?, dedup_at=?"
                " WHERE item_key=?",
                (v, cid, _f(r.get("match_cos")),
                 json.dumps(judge, ensure_ascii=False)
                 if isinstance(judge, (dict, list)) else judge,
                 _utcnow(), k)).rowcount
    return n


def mark_used(conn_or_path, episode: str, keys: Iterable[str]) -> int:
    """出片回写：used_in_episode=episode + used_at（只标记，永不清除/改写）。

    可传 conn 或 db 路径（路径则自带开关节点）。返回新标记行数。
    """
    own = not isinstance(conn_or_path, sqlite3.Connection)
    conn = init_db(conn_or_path) if own else conn_or_path
    try:
        ks = [k for k in dict.fromkeys(keys or []) if k]
        n, now = 0, _utcnow()
        with conn:
            for ch in _chunks(ks):
                q = ",".join("?" * len(ch))
                n += conn.execute(
                    "UPDATE items SET used_in_episode=?, used_at=?"
                    " WHERE used_in_episode IS NULL"
                    f" AND item_key IN ({q})", (episode, now, *ch)).rowcount
        return n
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 读取：eligible / projected_dedup / select / needs_summary / 契约投影
# ---------------------------------------------------------------------------

def eligible(row: dict, episode: str, wfrom: str, wto: str, grace_from: str,
             stale_floor: str, daily_map: Optional[dict] = None
             ) -> tuple[bool, str]:
    """窗口+used 判定（served-today 行同样可用）。reason 词表：
    window / carry-nodate / carry-stale / used / stale-daily / stale /
    stale-floor / nodate-old / future。
    """
    used = row.get("used_in_episode")
    if used is not None and used != episode:
        return False, "used"
    pub = row.get("date_published")
    fs = row.get("first_seen") or ""
    if pub and wfrom <= pub < wto:
        return True, "window"                        # 子句 A
    if not pub:
        return (True, "carry-nodate") if fs >= grace_from \
            else (False, "nodate-old")               # 子句 B
    if pub < wfrom:                                  # 子句 C：陈旧但迟到
        if (daily_map or {}).get(row.get("source_name") or ""):
            return False, "stale-daily"              # daily 快照源不结转
        if pub < stale_floor:
            return False, "stale-floor"              # 古董不享受迟到宽限
        return (True, "carry-stale") if fs >= grace_from else (False, "stale")
    return False, "future"                           # pub >= wto


def _published_cids(conn: sqlite3.Connection) -> set:
    """已出片 cluster 集（有成员 used_in_episode 非空的 dedup_cluster_id）。"""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT dedup_cluster_id FROM items"
        " WHERE used_in_episode IS NOT NULL AND dedup_cluster_id IS NOT NULL")}


def projected_dedup(row: dict, published_cids: set) -> str:
    """库存 dedup_verdict + 已出片 cluster 集 → 投影判定（不揭 suppressed）。

    stored 'suppressed' → suppressed；cluster 已出片 → suppressed，除非
    stored='reissue'（judge 已判同故事新进展）；结转行 NULL → 'gray'；
    否则原样。
    """
    v = row.get("dedup_verdict")
    if v == "suppressed":
        return "suppressed"
    cid = row.get("dedup_cluster_id")
    if cid is not None and cid in (published_cids or ()):
        return "reissue" if v == "reissue" else "suppressed"
    return v if v is not None else "gray"


_SEL_SQL = """
SELECT * FROM items
WHERE filter_verdict IN ('keep','review')
  AND (used_in_episode IS NULL OR used_in_episode = :episode)
  AND title_zh IS NOT NULL AND summary IS NOT NULL
  AND item_key NOT IN (SELECT item_key FROM item_runs WHERE episode = :episode)
  AND ( (date_published >= :wfrom AND date_published < :wto)
     OR (date_published IS NULL AND first_seen >= :grace_from)
     OR (date_published <  :wfrom AND date_published >= :sfloor
         AND first_seen >= :grace_from) )
ORDER BY (news_value IS NULL), news_value DESC, first_seen DESC, item_key
"""


def _pool_rows(conn, episode, wfrom, wto, grace_from, stale_floor,
               daily_map) -> list:
    """SQL 预筛 + daily 精修（子句 C）+ projected_dedup 注解。"""
    dm = daily_map or {}
    pub = _published_cids(conn)
    rows = []
    for r in conn.execute(_SEL_SQL, {"episode": episode, "wfrom": wfrom,
                                     "wto": wto, "grace_from": grace_from,
                                     "sfloor": stale_floor}):
        d = _decoded(dict(r))
        dp = d.get("date_published")
        if dp is not None and dp < wfrom and dm.get(d.get("source_name") or ""):
            continue            # 子句 C 对 daily 源不生效 → stale-daily 死区
        d["projected"] = projected_dedup(d, pub)
        rows.append(d)
    return rows


def select_candidates(conn: sqlite3.Connection, episode: str, wfrom: str,
                      wto: str, grace_from: str, stale_floor: str,
                      daily_map: Optional[dict] = None) -> list:
    """结转候选：预筛 + projected_dedup 注解，剔除 projected=='suppressed'。"""
    return [r for r in _pool_rows(conn, episode, wfrom, wto, grace_from,
                                  stale_floor, daily_map)
            if r["projected"] != "suppressed"]


def select_suppressed(conn: sqlite3.Connection, episode: str, wfrom: str,
                      wto: str, grace_from: str, stale_floor: str,
                      daily_map: Optional[dict] = None) -> list:
    """同一谓词的压制审计列：只留 projected=='suppressed'。"""
    return [r for r in _pool_rows(conn, episode, wfrom, wto, grace_from,
                                  stale_floor, daily_map)
            if r["projected"] == "suppressed"]


def count_floor_cut(conn: sqlite3.Connection, episode: str,
                    grace_from: str, stale_floor: str,
                    daily_map: Optional[dict] = None) -> int:
    """审计计数：其余条件全满足、仅因 pub<sfloor 被砍的结转条目数
    （daily 源本就不走子句 C，不计）。用于 stats 的 n_stale_floor。"""
    dm = daily_map or {}
    n = 0
    for (sn,) in conn.execute(
            "SELECT source_name FROM items"
            " WHERE filter_verdict IN ('keep','review')"
            " AND (used_in_episode IS NULL OR used_in_episode = :episode)"
            " AND title_zh IS NOT NULL AND summary IS NOT NULL"
            " AND item_key NOT IN (SELECT item_key FROM item_runs"
            "                    WHERE episode = :episode)"
            " AND date_published < :sfloor"
            " AND first_seen >= :grace_from",
            {"episode": episode, "sfloor": stale_floor,
             "grace_from": grace_from}):
        if not dm.get(sn or ""):
            n += 1
    return n


def needs_summary(conn: sqlite3.Connection, episode: str, wfrom: str,
                  wto: str, grace_from: str, stale_floor: str,
                  limit: int = 48) -> list:
    """待概要池行：keep/review + 未用/同期 + 窗口超集（不做 daily 精修）
    + summary IS NULL，按 first_seen 倒序封顶 limit。"""
    rows = conn.execute(
        "SELECT * FROM items"
        " WHERE filter_verdict IN ('keep','review')"
        " AND (used_in_episode IS NULL OR used_in_episode = :episode)"
        " AND summary IS NULL"
        " AND ( (date_published >= :wfrom AND date_published < :wto)"
        "    OR (date_published IS NULL AND first_seen >= :grace_from)"
        "    OR (date_published <  :wfrom AND date_published >= :sfloor"
        "        AND first_seen >= :grace_from) )"
        " ORDER BY first_seen DESC LIMIT :lim",
        {"episode": episode, "wfrom": wfrom, "wto": wto,
         "grace_from": grace_from, "sfloor": stale_floor,
         "lim": int(limit)}).fetchall()
    return [_decoded(dict(r)) for r in rows]


def to_raw_item(row: dict) -> dict:
    """池行 → raw_item/1 契约 dict（extra=forbid，字段恰好对齐 RawItem）。

    content_html 不落库故不出键；_fetch 记缓存语义（via='cache'，
    content_sha256 = content_text 的重算 sha）。
    """
    body = row.get("content_text") or ""
    return {
        "schema": "raw_item/1",
        "item_key": row["item_key"],
        "id": row["item_key"],
        "url": row.get("url") or "",
        "url_canon": row.get("url_canon") or "",
        "title": row.get("title") or "",
        "content_text": row.get("content_text"),
        "date_published": row.get("date_published"),
        "date_fetched": row.get("date_fetched") or _utcnow(),
        "language": row.get("language"),
        "tags": list(_js(row.get("tags_json"), [])),
        "image": row.get("image"),
        "_source": {"name": row.get("source_name") or "?",
                    "feed_url": row.get("source_feed_url") or "",
                    "kind": row.get("source_kind") or "rss",
                    "item_guid": row.get("item_guid")},
        "_fetch": {"status": 200, "via": "cache", "reachable": True,
                   "content_sha256": hashlib.sha256(
                       body.encode("utf-8")).hexdigest()[:16] if body else None},
        "_raw_ref": row.get("raw_ref"),
    }


def to_summary_row(row: dict) -> dict:
    """池行 → summary/1 契约 dict；prov 由 *_prompt/*_model/*_at 列重建。"""
    return {
        "schema": "summary/1",
        "item_key": row["item_key"],
        "title_zh": row.get("title_zh") or "",
        "summary": row.get("summary") or "",
        "entities": list(_js(row.get("entities_json"), [])),
        "facts": list(_js(row.get("facts_json"), [])),
        "section_guess": row.get("section_guess"),
        "prov": _prov(row.get("summary_model") or "pool",
                      row.get("summary_prompt") or "summary-v1",
                      row["item_key"], row.get("summary_at")),
    }


def to_verdict_row(row: dict) -> dict:
    """池行 → filter_verdict/1 契约 dict；prov 由 verdict_* 列重建。"""
    v = row.get("filter_verdict")
    return {
        "schema": "filter_verdict/1",
        "item_key": row["item_key"],
        "verdict": v if v in VERDICTS else "review",
        "ai_relevance": _f(row.get("ai_relevance")) or 0.5,
        "news_value": _f(row.get("news_value")),
        "reasons": [str(r) for r in _js(row.get("reasons_json"), [])],
        "prov": _prov(row.get("verdict_model") or "pool",
                      row.get("verdict_prompt") or "filter-v2",
                      row["item_key"], row.get("verdict_at")),
    }


# ---------------------------------------------------------------------------
# 回填：runs/<date>/ 既有 artifact → 池
# ---------------------------------------------------------------------------

def _read_jsonl(p: Path, errs: list) -> list:
    return meta.load_jsonl(p, errors=errs) if p.is_file() else []


def _is_real_verdict(vr: dict, cfg_model: str) -> bool:
    """20_filtered 里"真 LLM 判定"行：filter-v2 prompt + 当期配置模型，
    排除占位（l0-url-hash/no-llm 模型名、coverage-miss/no-llm 理由）。"""
    prov = vr.get("prov") or {}
    if prov.get("prompt") != "filter-v2":
        return False
    if prov.get("model") != cfg_model:
        return False
    if prov.get("model") in _PLACEHOLDER_MODELS:
        return False
    reasons = vr.get("reasons") or []
    r0 = str(reasons[0]) if reasons else ""
    return not r0.startswith(_PLACEHOLDER_REASON)


def import_run_dir(conn: sqlite3.Connection, dir_path, daily_map=None,
                   cfg_model: str = "swe-2-max") -> dict:
    """runs/<date>/ 10/20/30/35/40 → 池（幂等，缺文件/坏行容忍跳过）。

    episode = 目录名（非日期名由调用方挡）。40_selected.kept → mark_used。
    """
    d = Path(dir_path)
    episode = d.name
    errs: list = []
    raws = [r for r in _read_jsonl(d / RAW_NAME, errs) if isinstance(r, dict)]
    up = upsert_items(conn, raws, episode, daily_map)
    by_key = {}
    for it in raws:
        uc = it.get("url_canon") or normalize.url_canon(it.get("url") or "")
        kk = normalize.item_key(uc) if uc else str(it.get("item_key") or "")
        if kk:
            by_key[kk] = it
    vers = [r for r in _read_jsonl(d / FILT_NAME, errs) if isinstance(r, dict)]
    real = [r for r in vers if _is_real_verdict(r, cfg_model)]
    n_v = write_verdicts(conn, by_key, real, lambda _k: "filter-v2",
                         episode=episode)
    sums = [r for r in _read_jsonl(d / SUMS_NAME, errs) if isinstance(r, dict)
            and re.fullmatch(r"summary-v\d+",
                             str((r.get("prov") or {}).get("prompt") or ""))]
    n_s = write_summaries(conn, by_key, sums, episode=episode)
    deds = [r for r in _read_jsonl(d / DED_NAME, errs) if isinstance(r, dict)]
    n_d = write_dedup(conn, deds)
    n_u = 0
    sel = d / SEL_NAME
    if sel.is_file():
        try:
            doc = json.loads(sel.read_text(encoding="utf-8"))
            n_u = mark_used(conn, episode, [k.get("item_key") for k in
                                            doc.get("kept") or []
                                            if isinstance(k, dict)])
        except (json.JSONDecodeError, OSError) as e:
            errs.append(e)
    return {"episode": episode, "raw_items": len(raws),
            "inserted": up["inserted"], "updated": up["updated"],
            "rejudged": up["rejudged"],
            "skipped_empty_url": up["skipped_empty_url"],
            "verdict_rows": len(vers), "verdicts": n_v,
            "verdicts_skipped": len(vers) - len(real),
            "summaries": n_s, "dedup": n_d, "used": n_u,
            "bad_lines": len(errs)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_cfg_doc() -> dict:
    return meta.load_config()


def _daily_map(sources_path) -> dict:
    p = Path(sources_path) if sources_path else REPO_ROOT / "sources.yaml"
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception as e:
        print(f"[pool] warn: sources 读取失败 {e} — daily_map 按空集",
              file=sys.stderr)
        return {}
    return {str(s["name"]): bool(s.get("daily"))
            for s in data if isinstance(s, dict) and s.get("name")}


def _print_stats(conn: sqlite3.Connection) -> None:
    print("items:", conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    for r in conn.execute(
            "SELECT COALESCE(filter_verdict,'(unjudged)') v, COUNT(*) c"
            " FROM items GROUP BY v ORDER BY c DESC"):
        print(f"  verdict {r[0]:>10}: {r[1]}")
    for r in conn.execute(
            "SELECT first_seen, COUNT(*) c FROM items"
            " GROUP BY first_seen ORDER BY first_seen DESC LIMIT 12"):
        print(f"  first_seen {r[0]}: {r[1]}")
    print("  used:", conn.execute(
        "SELECT COUNT(*) FROM items WHERE used_in_episode IS NOT NULL"
    ).fetchone()[0])
    print("  with_summary:", conn.execute(
        "SELECT COUNT(*) FROM items WHERE summary IS NOT NULL").fetchone()[0])
    print("  signal:", conn.execute(
        "SELECT COUNT(*) FROM items WHERE signal=1").fetchone()[0])
    print("  item_runs:", conn.execute(
        "SELECT COUNT(*) FROM item_runs").fetchone()[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跨期条目池 state/items.sqlite")
    ap.add_argument("--db", default=None,
                    help="items.sqlite 路径（默认 config.storage.items_db > state/）")
    ap.add_argument("--import", dest="import_dir", metavar="RUNS_DIR",
                    help="回填 runs/ 下全部 YYYY-MM-DD 目录")
    ap.add_argument("--sources", default=None,
                    help="sources.yaml 路径（取 daily 标记）")
    ap.add_argument("--model", default=None,
                    help="判定模型名（默认 config.llm.model）")
    ap.add_argument("--stats", action="store_true", help="行数分布（operator 调试）")
    ap.add_argument("--selftest", action="store_true", help=":memory: 全量自测")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    cfg_doc = _load_cfg_doc()
    conn = init_db(resolve_path(args.db, cfg_doc))
    if args.import_dir:
        daily = _daily_map(args.sources)
        model = args.model or ((cfg_doc.get("llm") or {}).get("model")) \
            or "swe-2-max"
        base = Path(args.import_dir)
        base = base if base.is_absolute() else REPO_ROOT / base
        dirs = sorted(p for p in base.iterdir()
                      if p.is_dir() and _DATE_RE.fullmatch(p.name))
        for d in dirs:
            st = import_run_dir(conn, d, daily, model)
            print(f"[import] {d.name}: {json.dumps(st, ensure_ascii=False)}")
        print(f"[import] done: {len(dirs)} episode dirs")
    if args.stats:
        _print_stats(conn)
    if not args.import_dir and not args.stats:
        ap.print_help()
    return 0


# ---------------------------------------------------------------------------
# 自测：uv run stages/lib/pool.py --selftest（:memory:，无外部依赖）
# ---------------------------------------------------------------------------

def _selftest() -> int:
    from contracts.models import FilterVerdict, RawItem, Summary

    conn = init_db(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    EP, PREV, OLD, GRACE = "2026-09-22", "2026-09-21", "2026-09-10", "2026-09-19"
    wfrom, wto = window_bounds(EP)
    assert (wfrom, wto) == ("2026-09-20T22:30:00+00:00",
                            "2026-09-21T22:30:00+00:00"), (wfrom, wto)
    SFLOOR = stale_floor(wfrom, 14)
    assert SFLOOR == "2026-09-06T22:30:00+00:00", SFLOOR
    PUB_IN, PUB_OLD, PUB_FUT = ("2026-09-21T10:00:00+00:00",
                              "2026-09-10T00:00:00+00:00",
                              "2026-09-22T01:00:00+00:00")
    PUB_ANC = "2015-08-16T00:00:00+00:00"           # 远古：超 stale_floor
    PUB_OLD2 = "2024-06-01T00:00:00+00:00"         # 旧但非远古：也超界
    DAILY = {"daily_feed": True}

    def mk(tag, title, pub, source="srcA", content="正文"):
        url = f"https://ex.com/{tag}"
        return {"url": url, "url_canon": normalize.url_canon(url),
                "title": title, "content_text": content,
                "date_published": pub,
                "date_fetched": "2026-09-21T06:00:00+00:00",
                "language": "zh", "image": None,
                "tags": ["t1"], "_raw_ref": f"raw/{tag}.html",
                "_source": {"name": source, "feed_url": "https://ex.com/feed",
                            "kind": "rss", "item_guid": tag}}

    def key(it):
        return normalize.item_key(it["url_canon"])

    def vrow(k, verdict="keep"):
        return {"schema": "filter_verdict/1", "item_key": k, "verdict": verdict,
                "ai_relevance": 0.9, "news_value": 0.8, "reasons": ["r1"],
                "prov": _prov("swe-2-max", "filter-v2", k,
                              "2026-09-21T06:10:00+00:00")}

    def srow(k):
        return {"schema": "summary/1", "item_key": k, "title_zh": f"题{k[:4]}",
                "summary": "概要", "entities": ["OpenAI"], "facts": ["100 万"],
                "section_guess": "model-release",
                "prov": _prov("swe-2-max", "summary-v1", k,
                              "2026-09-21T06:20:00+00:00")}

    def drow(k, verdict, cid):
        return {"schema": "dedup_verdict/1", "item_key": k, "verdict": verdict,
                "cluster_id": cid, "match_cos": 0.5,
                "judge": {"via": "embed_lo"}}

    # ---- upsert 幂等：同期重跑 first_seen/seen_count 稳定；跨期 seen++ -------
    i_dup = mk("dup", "重复条目", PUB_IN)
    upsert_items(conn, [i_dup], PREV, {})
    st = upsert_items(conn, [i_dup], PREV, {})
    r = get_many(conn, [key(i_dup)])[key(i_dup)]
    assert r["seen_count"] == 1 and r["first_seen"] == PREV == r["last_seen"]
    assert st["updated"] == 1 and st["inserted"] == 0
    upsert_items(conn, [i_dup], EP, {})      # 跨期再现
    r = get_many(conn, [key(i_dup)])[key(i_dup)]
    assert (r["seen_count"], r["first_seen"], r["last_seen"]) == (2, PREV, EP)

    # ---- url_canon='' 跳过并计数 ------------------------------------------
    st = upsert_items(conn, [mk("x", "空", PUB_IN)] +
                      [{"url": "", "url_canon": "", "title": "bad",
                        "_source": {"name": "s"}}], PREV, {})
    assert st["skipped_empty_url"] == 1 and st["inserted"] == 1, st

    # ---- 候选矩阵：A/B/C 命中 + daily 死区 + 旧 first_seen 死区 ------------
    i_a = mk("a", "窗内条目", PUB_IN)                    # 子句 A
    i_b = mk("b", "无日期条目", None)                    # 子句 B
    i_c = mk("c", "陈旧结转", PUB_OLD)                   # 子句 C（非 daily）
    i_d = mk("d", "daily 陈旧", PUB_OLD, source="daily_feed")  # C×daily→死区
    i_e = mk("e", "陈旧+老首见", PUB_OLD)                # C miss（first_seen 老）
    i_f = mk("f", "无日期+老首见", None)                 # B miss
    i_g = mk("g", "未来日期", PUB_FUT)                   # 出窗
    i_h = mk("h", "他期已用", PUB_IN)                    # used 他期
    i_i = mk("i", "本期已用", PUB_IN)                    # used 同期（豁免）
    i_dr = mk("dr", "drop 条目", PUB_IN)                 # verdict=drop
    i_ns = mk("ns", "待概要", PUB_IN)                    # keep 无 summary
    i_sp = mk("sp", "已压制", PUB_IN)                    # stored suppressed
    i_pb = mk("pb", "cluster 已出片", PUB_IN)            # cid 命中 published
    i_ri = mk("ri", "已出片但 reissue", PUB_IN)          # reissue 存活
    i_gy = mk("gy", "无 dedup 判定", PUB_IN)             # NULL→gray
    i_rs = mk("rs", "内容漂移重置", PUB_IN)              # re-judge 用
    i_gd = mk("gd", "自压制护栏", PUB_IN)                # dedup guard 用
    i_anc = mk("anc", "远古迟到无概要", PUB_ANC)          # C×floor→stale，且
                                                       # 不进 needs_summary
    i_an2 = mk("an2", "远古迟到有概要", PUB_OLD2)         # C×floor→stale，
                                                       # pre-fix 本可结转
    i_bnd = mk("bnd", "恰在下限", SFLOOR)                # pub==sfloor → 界内
    upsert_items(conn, [i_a, i_b, i_c, i_d, i_h, i_i, i_dr, i_ns, i_sp,
                        i_pb, i_ri, i_gy, i_rs, i_gd, i_anc, i_an2, i_bnd],
                 PREV, DAILY)
    upsert_items(conn, [i_e, i_f, i_g], OLD, {})         # first_seen=OLD<GRACE
    all_keys = [key(x) for x in (i_dup, i_a, i_b, i_c, i_d, i_e, i_f, i_g,
                                 i_h, i_i, i_dr, i_ns, i_sp, i_pb, i_ri,
                                 i_gy, i_rs, i_gd, i_anc, i_an2, i_bnd)]
    by_key = {key(x): x for x in (i_dup, i_a, i_b, i_c, i_d, i_e, i_f, i_g,
                                  i_h, i_i, i_dr, i_ns, i_sp, i_pb, i_ri,
                                  i_gy, i_rs, i_gd, i_anc, i_an2, i_bnd)}
    write_verdicts(conn, by_key,
                   [vrow(k) for k in all_keys if k != key(i_dr)]
                   + [vrow(key(i_dr), "drop")], lambda _k: "filter-v2",
                   episode=PREV)
    write_summaries(conn, by_key,
                    [srow(k) for k in all_keys
                     if k not in (key(i_ns), key(i_anc))],
                    episode=PREV)
    write_dedup(conn, [drow(key(i_sp), "suppressed", 201),
                       drow(key(i_pb), "fresh", 202),
                       drow(key(i_ri), "reissue", 202),
                       drow(key(i_rs), "fresh", 202),
                       drow(key(i_gd), "fresh", 301)])
    mark_used(conn, "2026-09-20", [key(i_h)])            # 他期已用
    mark_used(conn, EP, [key(i_i)])                      # 本期已用（豁免）
    mark_used(conn, "2026-09-20", [key(i_rs)])           # i_rs → published cid 202

    # mark_used 只标记不清除：再标他期不改写
    assert mark_used(conn, "2026-09-21", [key(i_i)]) == 0
    assert get_many(conn, [key(i_i)])[key(i_i)]["used_in_episode"] == EP

    # ---- select：窗口子句 + used 规则 + 当期 item_runs 排除 ----------------
    cands = {r["item_key"]: r for r in
             select_candidates(conn, EP, wfrom, wto, GRACE, SFLOOR, DAILY)}
    got = set(cands)
    expect = {key(i_a), key(i_b), key(i_c), key(i_i),
              key(i_ri), key(i_gy), key(i_gd), key(i_bnd)}
    assert got == expect, {"missing": {k[:6] for k in expect - got},
                           "extra": {k[:6] for k in got - expect}}
    # i_dup 满足其余全部谓词，仅因 item_runs(EP) 被排除（当期批走文件管线）
    assert key(i_dup) not in got
    assert cands[key(i_ri)]["projected"] == "reissue"
    assert cands[key(i_gy)]["projected"] == "gray"
    assert cands[key(i_a)]["projected"] == "gray"      # dedup NULL → gray
    assert cands[key(i_gd)]["projected"] == "fresh"    # 未出片 cluster 原样
    supp = {r["item_key"] for r in
            select_suppressed(conn, EP, wfrom, wto, GRACE, SFLOOR, DAILY)}
    assert supp == {key(i_sp), key(i_pb)}, supp    # stored suppressed + published cid
    # eligible() 逐条核对（served-today 可用）
    rows = get_many(conn, all_keys)
    assert eligible(rows[key(i_a)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (True, "window")
    assert eligible(rows[key(i_b)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (True, "carry-nodate")
    assert eligible(rows[key(i_c)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (True, "carry-stale")
    assert eligible(rows[key(i_d)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "stale-daily")
    assert eligible(rows[key(i_e)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "stale")
    assert eligible(rows[key(i_f)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "nodate-old")
    assert eligible(rows[key(i_g)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "future")
    assert eligible(rows[key(i_h)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "used")
    assert eligible(rows[key(i_i)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (True, "window")
    assert eligible(rows[key(i_anc)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "stale-floor")
    assert eligible(rows[key(i_an2)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (False, "stale-floor")
    assert eligible(rows[key(i_bnd)], EP, wfrom, wto, GRACE, SFLOOR, DAILY) == (True, "carry-stale")
    # count_floor_cut：仅 i_an2 命中（i_anc 无 summary 出局；
    # i_e/i_d pub≥sfloor；daily 源不计）
    assert count_floor_cut(conn, EP, GRACE, SFLOOR, DAILY) == 1

    # ---- needs_summary：keep/review + 无 summary + 窗口超集 ------------------
    ns = {r["item_key"] for r in
          needs_summary(conn, EP, wfrom, wto, GRACE, SFLOOR)}
    assert ns == {key(i_ns)}, {k[:6] for k in ns}   # i_anc 被 floor 截断
    write_summaries(conn, by_key, [srow(key(i_ns))], episode=PREV)
    assert key(i_ns) not in {r["item_key"] for r in
                             needs_summary(conn, EP, wfrom, wto, GRACE, SFLOOR)}

    # ---- write_dedup 自压制护栏 --------------------------------------------
    write_dedup(conn, [drow(key(i_gd), "suppressed", 301)])   # 同 cluster → 跳过
    r = rows[key(i_gd)] = get_many(conn, [key(i_gd)])[key(i_gd)]
    assert r["dedup_verdict"] == "fresh" and r["dedup_cluster_id"] == 301
    write_dedup(conn, [drow(key(i_gd), "suppressed", 302)])   # 异 cluster → 写入
    r = get_many(conn, [key(i_gd)])[key(i_gd)]
    assert r["dedup_verdict"] == "suppressed" and r["dedup_cluster_id"] == 302

    # ---- write_verdicts 自愈未知 key ---------------------------------------
    ghost = "cafecafecafecafe"
    write_verdicts(conn, {}, [vrow(ghost)], lambda _k: "filter-v2", episode=EP)
    r = get_many(conn, [ghost])[ghost]
    assert r["filter_verdict"] == "keep" and r["first_seen"] == EP
    assert r["judged_content_sha"] == content_sha({})

    # ---- 内容漂移：判定列清零，used/dedup 保留 ------------------------------
    i_rs2 = dict(i_rs, title="改题触发漂移", content_text="新正文")
    st = upsert_items(conn, [i_rs2], PREV, {})
    assert st["rejudged"] == 1, st
    r = get_many(conn, [key(i_rs)])[key(i_rs)]
    assert r["filter_verdict"] is None and r["judged_content_sha"] is None
    assert r["verdict_model"] is None and r["ai_relevance"] is None
    assert r["used_in_episode"] == "2026-09-20"          # used 不动
    assert r["dedup_verdict"] == "fresh" and r["dedup_cluster_id"] == 202
    assert r["title"] == "改题触发漂移"                  # 原文列照常更新

    # ---- 契约投影 ----------------------------------------------------------
    RawItem.model_validate(to_raw_item(rows[key(i_a)]))
    Summary.model_validate(to_summary_row(rows[key(i_a)]))
    FilterVerdict.model_validate(to_verdict_row(rows[key(i_a)]))
    rv = to_verdict_row(rows[key(i_a)])
    assert rv["prov"]["model"] == "swe-2-max" and rv["prov"]["prompt"] == "filter-v2"
    assert to_raw_item(rows[key(i_a)])["_fetch"]["via"] == "cache"
    assert "content_html" not in to_raw_item(rows[key(i_a)])

    # ---- resolve_path 优先级 ------------------------------------------------
    assert resolve_path("/abs/x.db", {}) == Path("/abs/x.db")
    assert resolve_path(None, {"storage": {"items_db": "state/y.db"}}) \
        == REPO_ROOT / "state" / "y.db"
    assert resolve_path(None, {}) == DEFAULT_DB

    # ---- import_run_dir：占位行拒收 + 文件容忍 -------------------------------
    fix = REPO_ROOT / "state" / "tmp" / "pool_selftest" / PREV
    fix.mkdir(parents=True, exist_ok=True)
    it = mk("imp", "导入条目", PUB_IN)
    kk = key(it)
    meta.atomic_write(fix / RAW_NAME, meta.dumps_jsonl([it]))
    meta.atomic_write(fix / FILT_NAME, meta.dumps_jsonl([
        vrow(kk),                                     # 真判 → 收
        vrow("0" * 16, "drop") | {"prov": _prov("l0-url-hash", "l0-v1", "0" * 16)},
        vrow("1" * 16) | {"reasons": ["coverage-miss: 重批2轮仍缺，转人工"]},
    ]))
    meta.atomic_write(fix / SUMS_NAME, meta.dumps_jsonl([
        srow(kk),
        srow("2" * 16) | {"prov": _prov("l0-url-hash", "summary-v1-fallback", "2" * 16)},
    ]))
    meta.atomic_write(fix / DED_NAME,
                      meta.dumps_jsonl([drow(kk, "fresh", 501)]))
    meta.atomic_write(fix / SEL_NAME,
                      {"schema": "selected/1", "episode": PREV,
                       "decided_at": "2026-09-21T08:00:00+08:00",
                       "decided_by": "auto",
                       "kept": [{"item_key": kk, "id": "imp", "section": "s",
                                 "note": None}], "dropped": []})
    st = import_run_dir(conn, fix, {}, "swe-2-max")
    assert st["verdicts"] == 1 and st["verdicts_skipped"] == 2, st
    assert st["summaries"] == 1 and st["dedup"] == 1 and st["used"] == 1, st
    r = get_many(conn, [kk])[kk]
    assert r["filter_verdict"] == "keep" and r["used_in_episode"] == PREV
    assert r["judged_content_sha"] == content_sha(it)
    # 占位行对应的 key 不应被自愈建行
    assert get_many(conn, ["0" * 16, "1" * 16, "2" * 16]) == {}

    import shutil
    shutil.rmtree(REPO_ROOT / "state" / "tmp" / "pool_selftest",
                  ignore_errors=True)
    print("pool selftest OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
