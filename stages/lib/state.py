#!/usr/bin/env python3
"""stages/lib/state.py — 跨期持久态单文件收口（state/state.sqlite）。

一本库装四类持久态，调用方各取所需（WAL 下读写互不堵）：

  items / item_runs            条目池（原 state/items.sqlite 两表，逐字搬迁；
                               pool.py 全部 SQL 不变）
  dedup_items / dedup_clusters 去重历史（原 state/history.sqlite items/clusters；
                               与池表撞名，改名 dedup_* 是本次唯一结构性变更）
  source_state                 采集源状态合并表：seen_json（原 seen.json 每源
                               dict：last_status/last_ok/urls/validators）+
                               health_json（原 source_health.json 每源 dict）
  kv                           小 JSON 杂项（x_nitter_health / reddit_token /
                               weibo_cookie —— 原各自一文件，现一键一值）

路径解析一处收口：resolve_path() = --items-db/--db CLI 覆盖
> config.storage.state_db > storage.items_db（旧键兜底）> state/state.sqlite。
store.init_db / pool.init_db 都是本模块 open() 的薄壳（签名不变，调用方
无感）；事务约定同 store.py：写函数不 commit，调用方持事务。

迁移：uv run stages/lib/state.py --migrate
  把旧双库 + JSON 文件导入 state.sqlite 并做行数对账；旧文件原地保留
  （已在 state/pre-migrate-*/ 有备份，确认无恙后人工删）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "state" / "state.sqlite"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# DDL —— 单文件全部表的唯一事实源（store/pool 不再自带 SCHEMA）
# ---------------------------------------------------------------------------

SCHEMA = f"""
PRAGMA user_version = {SCHEMA_VERSION};

-- ---- 条目池（原 items.sqlite，逐字） ----
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

-- ---- 去重历史（原 history.sqlite items/clusters → dedup_*） ----
CREATE TABLE IF NOT EXISTS dedup_clusters (
  cluster_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_title TEXT    NOT NULL,          -- 故事线代表标题（首见 item 标题）
  centroid        BLOB    NOT NULL,          -- float32 1024d unit-norm 运行均值
  first_seen      TEXT    NOT NULL,          -- ISO date 首次进入候选
  last_seen       TEXT    NOT NULL,          -- 最近一次命中/重报的日期
  expires_at      TEXT    NOT NULL,          -- last_seen + TTL_DAYS，命中刷新
  item_count      INTEGER NOT NULL DEFAULT 1,
  n_reissues      INTEGER NOT NULL DEFAULT 0, -- 以 newdev 身份被重报次数
  state           TEXT    NOT NULL DEFAULT 'open',  -- open|expired|merged
  published       INTEGER NOT NULL DEFAULT 0  -- 有成员 verdict='reported' 即 1
);
CREATE INDEX IF NOT EXISTS idx_dedup_clusters_cmp
  ON dedup_clusters(state, expires_at);      -- 比对集 = state='open' AND expires_at>=today

CREATE TABLE IF NOT EXISTS dedup_items (
  item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES dedup_clusters(cluster_id),
  day        TEXT    NOT NULL,               -- 进入候选/被报道的日期
  episode    TEXT,                           -- 所属期号（如 2026-09-20）
  title      TEXT    NOT NULL,
  summary    TEXT,                           -- 上游逐条概要（判重特征可拼入）
  source     TEXT,
  url_canon  TEXT    NOT NULL,               -- 规范化 URL（去跟踪参数/www./尾斜杠）
  url_hash   TEXT    NOT NULL,               -- sha1(url_canon)，唯一性靠应用层
  lang       TEXT,
  simhash    INTEGER NOT NULL,               -- 64-bit（存为 sqlite signed）
  embed      BLOB    NOT NULL,               -- float32 1024d，doc 侧无 instruct
  verdict    TEXT    NOT NULL DEFAULT 'candidate',
             -- reported | suppressed | candidate | reissue | gray_pending
  via        TEXT,                           -- 判定路径（dup_exact/judge_a/…），重放用
  match_cos  REAL,                           -- 与命中 cluster 的 cos（证据）
  judge      TEXT,                           -- LLM 判词 JSON（灰区时填）
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dedup_items_urlhash ON dedup_items(url_hash);
CREATE INDEX IF NOT EXISTS idx_dedup_items_cluster ON dedup_items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_dedup_items_day     ON dedup_items(day);

-- ---- 采集源状态（seen.json + source_health.json 合并） ----
CREATE TABLE IF NOT EXISTS source_state (
  name        TEXT PRIMARY KEY,            -- sources.yaml 源名
  seen_json   TEXT,                        -- {{last_status,last_ok,urls[],validators{{}}}}
  health_json TEXT,                        -- {{consecutive_fails,alerted,last_ok_date,...}}
  updated_at  TEXT
);

-- ---- 小 JSON 杂项（原 x_nitter_health/reddit_token/weibo_cookie 三个文件） ----
CREATE TABLE IF NOT EXISTS kv (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,                -- JSON 文本
  updated_at TEXT NOT NULL
);
"""

# kv 键名 ↔ 原文件（迁移 + 各模块读写统一用这三个键）
KV_NITTER_HEALTH = "x_nitter_health"
KV_REDDIT_TOKEN = "reddit_token"
KV_WEIBO_COOKIE = "weibo_cookie"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# open / resolve
# ---------------------------------------------------------------------------

def open(path=None) -> sqlite3.Connection:
    """打开/创建 state.sqlite：WAL + busy_timeout + 全表 DDL（幂等）。

    ':memory:' 跑自测。只建表不碰数据——store 侧的自愈迁移仍在
    store.init_db 内（dedup_* 表专属）。
    """
    p = str(path) if path is not None else str(DEFAULT_DB)
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


def resolve_path(cli_arg=None, cfg_doc: Optional[dict] = None) -> Path:
    """--db/--items-db CLI > cfg.storage.state_db > cfg.storage.items_db（旧键）
    > state/state.sqlite。相对路径基于 repo 根。"""
    st = (cfg_doc or {}).get("storage") or {}
    rel = st.get("state_db") or st.get("items_db")
    p = Path(cli_arg) if cli_arg else (Path(rel) if rel else DEFAULT_DB)
    return p if p.is_absolute() else REPO_ROOT / p


def open_ro(path) -> sqlite3.Connection | None:
    """只读连接；文件缺席返回 None（调用方按空集退化）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# kv —— 小 JSON 杂项
# ---------------------------------------------------------------------------

def kv_get(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    """kv 读：JSON 解码；缺席/坏 JSON → None。"""
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["value"] if isinstance(r, sqlite3.Row) else r[0])
    except (json.JSONDecodeError, TypeError):
        return None


def kv_set(conn: sqlite3.Connection, key: str, obj) -> None:
    """kv 写：JSON 序列化 upsert（调用方持事务，本函数不 commit）。"""
    conn.execute(
        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=excluded.updated_at",
        (key, json.dumps(obj, ensure_ascii=False), _utcnow()))


# ---------------------------------------------------------------------------
# source_state —— seen/health 两 dict 的双列合并存储
# ---------------------------------------------------------------------------

def load_source_state(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """→ (seen, health)：{源名: dict} 两张大 dict，形状与旧 JSON 文件一致。"""
    seen, health = {}, {}
    for r in conn.execute("SELECT name, seen_json, health_json FROM source_state"):
        name = r["name"] if isinstance(r, sqlite3.Row) else r[0]
        sj = r["seen_json"] if isinstance(r, sqlite3.Row) else r[1]
        hj = r["health_json"] if isinstance(r, sqlite3.Row) else r[2]
        try:
            if sj:
                seen[name] = json.loads(sj)
        except json.JSONDecodeError:
            pass
        try:
            if hj:
                health[name] = json.loads(hj)
        except json.JSONDecodeError:
            pass
    return seen, health


def save_source_state(conn: sqlite3.Connection, seen: Optional[dict] = None,
                      health: Optional[dict] = None) -> int:
    """每源一行 upsert（seen_json/health_json 各 None 的一侧不动旧值）。

    调用方持事务，本函数不 commit。返回触碰行数。
    """
    names = set()
    if seen:
        names.update(seen)
    if health:
        names.update(health)
    now = _utcnow()
    n = 0
    for name in names:
        sj = json.dumps(seen[name], ensure_ascii=False) \
            if seen and name in seen else None
        hj = json.dumps(health[name], ensure_ascii=False) \
            if health and name in health else None
        conn.execute(
            "INSERT INTO source_state(name,seen_json,health_json,updated_at)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET"
            "   seen_json=COALESCE(excluded.seen_json, source_state.seen_json),"
            "   health_json=COALESCE(excluded.health_json, source_state.health_json),"
            "   updated_at=excluded.updated_at",
            (name, sj, hj, now))
        n += 1
    return n


# ---------------------------------------------------------------------------
# migrate —— 旧双库 + JSON 文件 → state.sqlite
# ---------------------------------------------------------------------------

def _count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def migrate(state_db: Path, items_db: Optional[Path] = None,
            history_db: Optional[Path] = None,
            seen_json: Optional[Path] = None,
            health_json: Optional[Path] = None,
            kv_files: Optional[dict] = None, *,
            force: bool = False) -> dict:
    """旧 items/history.sqlite + seen/source_health/kv JSON → state.sqlite。

    行数对账：每表 old vs new 计数一致才放行；目标库已有数据且未 --force
    时拒绝（防二次导入翻倍）。返回对账报告 dict。
    """
    state_db = Path(state_db)
    items_db = Path(items_db or REPO_ROOT / "state" / "items.sqlite")
    history_db = Path(history_db or REPO_ROOT / "state" / "history.sqlite")
    seen_json = Path(seen_json or REPO_ROOT / "state" / "seen.json")
    health_json = Path(health_json or REPO_ROOT / "state" / "source_health.json")
    kv_files = kv_files or {
        KV_NITTER_HEALTH: REPO_ROOT / "state" / "x_nitter_health.json",
        KV_REDDIT_TOKEN: REPO_ROOT / "state" / "reddit_token.json",
        KV_WEIBO_COOKIE: REPO_ROOT / "state" / "weibo_cookie.json",
    }

    conn = open(state_db)
    report = {"state_db": str(state_db), "tables": {}, "kv": {}, "sources": 0}
    if not force and (_count(conn, "items") or _count(conn, "dedup_items")
                      or _count(conn, "source_state") or _count(conn, "kv")):
        conn.close()
        raise SystemExit(
            f"[migrate] {state_db} 已有数据，拒绝二次导入（--force 覆盖）")

    try:
        # ---- 条目池 ----
        if items_db.exists():
            conn.execute("ATTACH DATABASE ? AS old_items", (str(items_db),))
            with conn:
                conn.execute("INSERT INTO items SELECT * FROM old_items.items")
                conn.execute(
                    "INSERT INTO item_runs SELECT * FROM old_items.item_runs")
            old_i = sqlite3.connect(f"file:{items_db}?mode=ro", uri=True)
            report["tables"]["items"] = (
                _count(old_i, "items"), _count(conn, "items"))
            report["tables"]["item_runs"] = (
                _count(old_i, "item_runs"), _count(conn, "item_runs"))
            old_i.close()
            conn.execute("DETACH DATABASE old_items")
        # ---- 去重历史（items/clusters → dedup_*） ----
        if history_db.exists():
            conn.execute("ATTACH DATABASE ? AS old_hist", (str(history_db),))
            cols = [r[1] for r in conn.execute(
                "PRAGMA old_hist.table_info(items)")]
            sel = ",".join(c for c in
                           ("item_id", "cluster_id", "day", "episode", "title",
                            "summary", "source", "url_canon", "url_hash", "lang",
                            "simhash", "embed", "verdict", "via", "match_cos",
                            "judge", "created_at") if c in cols)
            with conn:
                conn.execute(
                    "INSERT INTO dedup_clusters"
                    " SELECT * FROM old_hist.clusters")
                conn.execute(
                    f"INSERT INTO dedup_items({sel})"
                    f" SELECT {sel} FROM old_hist.items")
            old_h = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
            report["tables"]["dedup_items"] = (
                _count(old_h, "items"), _count(conn, "dedup_items"))
            report["tables"]["dedup_clusters"] = (
                _count(old_h, "clusters"), _count(conn, "dedup_clusters"))
            old_h.close()
            conn.execute("DETACH DATABASE old_hist")
        # ---- 源状态 + kv ----
        seen = json.loads(seen_json.read_text()) if seen_json.exists() else {}
        health = json.loads(health_json.read_text()) \
            if health_json.exists() else {}
        kvs = {}
        for k, fp in kv_files.items():
            fp = Path(fp)
            if fp.exists():
                kvs[k] = json.loads(fp.read_text())
        with conn:
            report["sources"] = save_source_state(conn, seen, health)
            for k, v in kvs.items():
                kv_set(conn, k, v)
                report["kv"][k] = len(json.dumps(v, ensure_ascii=False))
        # ---- 对账 ----
        bad = {t: (a, b) for t, (a, b) in report["tables"].items() if a != b}
        report["reconciled"] = not bad
        if bad:
            report["mismatch"] = bad
    finally:
        conn.close()
    return report


# ---------------------------------------------------------------------------
# CLI / selftest
# ---------------------------------------------------------------------------

def _selftest():
    conn = open(":memory:")
    assert _count(conn, "items") == 0 and _count(conn, "dedup_items") == 0
    kv_set(conn, "k1", {"a": 1})
    assert kv_get(conn, "k1") == {"a": 1}
    assert kv_get(conn, "nope") is None
    kv_set(conn, "k1", {"b": 2})
    assert kv_get(conn, "k1") == {"b": 2}
    n = save_source_state(conn,
                          seen={"s1": {"urls": ["u1"]}},
                          health={"s1": {"consecutive_fails": 2}})
    assert n == 1
    seen, health = load_source_state(conn)
    assert seen["s1"]["urls"] == ["u1"]
    assert health["s1"]["consecutive_fails"] == 2
    # 单侧更新不动另一侧
    save_source_state(conn, seen={"s1": {"urls": ["u2"], "last_status": "ok"}})
    seen, health = load_source_state(conn)
    assert seen["s1"]["urls"] == ["u2"] and seen["s1"]["last_status"] == "ok"
    assert health["s1"]["consecutive_fails"] == 2
    conn.commit()
    conn.close()
    print("state.py selftest OK")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="state.sqlite 迁移/自测")
    ap.add_argument("--migrate", action="store_true", help="执行迁移")
    ap.add_argument("--force", action="store_true", help="目标库已有数据仍导入")
    ap.add_argument("--state-db", default=str(DEFAULT_DB))
    ap.add_argument("--items-db", default=None)
    ap.add_argument("--history-db", default=None)
    args = ap.parse_args()
    if args.migrate:
        rep = migrate(Path(args.state_db), items_db=args.items_db,
                      history_db=args.history_db, force=args.force)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if not rep.get("reconciled"):
            sys.exit("[migrate] 行数对账失败")
        print("[migrate] OK")
    else:
        _selftest()
