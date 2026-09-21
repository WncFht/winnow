"""story-line 去重历史库 — state/history.sqlite 的读写层（PLAN §7.2）。

单文件 SQLite（WAL），两级结构，schema 与 experiments/dedup-history/schema.sql
逐字一致，仅 clusters 新增 `published`（该 cluster 有 item verdict='reported'
即置 1，由 meta_qa 在出片后经 mark_reported() 回写）。

行永不删除：cluster 过期只是退出比对集（state='expired'），可审计、可回放。

判定级联（check()，阈值已校准勿改——schema.sql 尾部注释是校准依据）：
  1. url_hash 精确命中任意历史 item          -> suppressed(dup_exact)
  2. simhash(title+summary) 海明距 <=4       -> suppressed(dup_near)
     （只对开放 cluster 的成员比对）
  3. cos(query_embed, 开放 cluster centroid):
       >=0.85 且与该 cluster 成员 simhash<=8 -> suppressed(dup_same)
       [0.58,0.85) -> gray -> judge_fn 三值：
           A 同事件无新信息 -> suppressed(judge_a)
           B 同故事新进展   -> reissue（挂同 cluster、n_reissues++、centroid 并入；
                              update_of 只许挂 published=1 的 cluster）
           C 不同事件       -> fresh（新 cluster）
           judge_fn 缺失/抛错/返回非法 -> gray_pending（进人工 UI；
                              仍挂候选 cluster 为成员，人工判"误并"时用
                              split_cluster 移出并自动重算 centroid）
       <0.58 -> fresh（新 cluster）

不变量（split/merge 重算的依据）：
  cluster.centroid = 其全部成员 item.embed 的 unit-norm 均值；
  cluster.item_count = 成员数；n_reissues = 成员中 verdict='reissue' 数；
  cluster.published = 成员中存在 verdict='reported'。

冷启动：history.sqlite 为空时由 stages/dedup.py 回填近 7 日 raw_cache
预热比对集（本模块只管读写，不管回填来源）。

stdlib only — 与 lib/meta.py 同级约定，任何 PEP 723 stage 脚本可安全 import；
调用方传进来的 embed 可以是 list/np.ndarray/f32 bytes，本模块统一归一。

用法（stages/dedup.py）::

    from lib import store
    conn = store.init_db(run_dir / "state/history.sqlite")   # 或 state/ 下
    for it in candidates:
        it["url_hash"]  = store.url_hash(it["url_canon"])
        it["simhash"]   = store.simhash64(it["title_zh"] + " " + it["summary"])
        it["embed"]     = embedder.doc(it["title_zh"] + " " + it["summary"])
        r = store.check(conn, it, judge_fn=judge)            # 写库 + 判定
        out.append({"item_key": it["item_key"], "verdict": r["verdict"],
                    "cluster_id": r["cluster_id"], "match_cos": r["cos"],
                    "judge": r["judge"]})
    # 出片后（meta_qa）：
    store.mark_reported(conn, episode, kept_item_keys)
    store.expire_clusters(conn)
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from array import array
from datetime import date, timedelta
from math import sqrt
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# schema — 与 experiments/dedup-history/schema.sql 逐字一致 + clusters.published
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (
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
CREATE INDEX IF NOT EXISTS idx_clusters_cmp
  ON clusters(state, expires_at);            -- 比对集 = state='open' AND expires_at>=today

CREATE TABLE IF NOT EXISTS items (
  item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id INTEGER NOT NULL REFERENCES clusters(cluster_id),
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
  match_cos  REAL,                           -- 与命中 cluster 的 cos（证据）
  judge      TEXT,                           -- LLM 判词 JSON（灰区时填）
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_urlhash ON items(url_hash);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_day     ON items(day);
"""

# ---------------------------------------------------------------------------
# 校准阈值（勿改——依据见 experiments/dedup-history/schema.sql 尾注与 PLAN §7.2）
# ---------------------------------------------------------------------------

T_AUTO = 0.85        # cos>=T_AUTO 且成员 simhash<=SIMHASH_HI -> suppressed
T_GRAY = 0.58        # [T_GRAY, T_AUTO) -> gray -> judge；<T_GRAY -> 新 cluster
SIMHASH_H = 4        # dup_near：与任一开放 cluster 成员的海明距阈值
SIMHASH_HI = 8       # dup_same 的双保险：cos>=T_AUTO 时成员海明距上限
TTL_DAYS = 21        # 故事线保鲜期：超 3 周无命中退出比对集，允许重报

_EMPTY_SHA1 = hashlib.sha1(b"").hexdigest()

# ---------------------------------------------------------------------------
# URL 规范化 / item_key / simhash —— 与采集层共用同一套机械身份
# ---------------------------------------------------------------------------

TRACKING_KEYS = re.compile(
    r"^(utm_|spm|ref$|ref_src|fbclid|gclid|dclid|mc_cid|mc_eid|share_|share$|campaign|"
    r"medium$|source$|from$|wechat|igshid|si$|feature$|_hsenc|_hsmi|oly_|vero_|cmpid|"
    r"sr_share|tt_|s$|token$|trk)", re.I)


def canon_url(u: str) -> str:
    """规范化：小写 scheme/host，去 www./m./amp.，丢跟踪参数，去 fragment，去尾斜杠。"""
    u = (u or "").strip()
    if not u:
        return ""
    sp = urlsplit(u if "://" in u else "https://" + u)
    host = (sp.hostname or "").lower().removeprefix("www.") \
        .removeprefix("m.").removeprefix("amp.")
    qs = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
          if not TRACKING_KEYS.match(k)]
    path = re.sub(r"/+$", "", sp.path) or "/"
    return urlunsplit((sp.scheme.lower(), host, path, urlencode(sorted(qs)), ""))


def url_hash(u: str) -> str:
    """sha1(url_canon) — items.url_hash，dup_exact 判定键。"""
    return hashlib.sha1(canon_url(u).encode()).hexdigest()


def item_key(url_canon: str) -> str:
    """sha256(url_canon)[:16] — 管道全程机械身份（contracts/models.py §身份两级）。"""
    return hashlib.sha256((url_canon or "").encode()).hexdigest()[:16]


TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]")


def _features(text: str) -> list:
    """EN 按词，ZH 按 2-gram 字（无分词依赖）。"""
    toks = TOKEN_RE.findall((text or "").lower())
    feats = []
    for t in toks:
        if len(t) > 1 and re.fullmatch(r"[一-鿿]+", t):
            feats += [t[i:i + 2] for i in range(len(t) - 1)] or [t]
        else:
            feats.append(t)
    return feats


def simhash64(text: str) -> int:
    """64-bit simhash（无符号 int 返回；入库前用 _s64 转 signed）。"""
    v = [0] * 64
    for f in _features(text):
        h = int.from_bytes(hashlib.sha1(f.encode()).digest()[:8], "big")
        for i in range(64):
            v[i] += 1 if h >> i & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    """64-bit 海明距；两侧任意传 signed/unsigned。"""
    return bin((a & 0xFFFFFFFFFFFFFFFF) ^ (b & 0xFFFFFFFFFFFFFFFF)).count("1")


def _s64(x: int) -> int:
    """unsigned 64 -> sqlite signed 64。"""
    return x - (1 << 64) if x >= (1 << 63) else x


def feature_text(item: dict) -> str:
    """simhash 特征串约定：title + ' ' + summary（title_zh 优先，与 dedup 阶段一致）。"""
    t = item.get("title_zh") or item.get("title") or ""
    s = item.get("summary") or ""
    return (t + " " + s).strip()


# ---------------------------------------------------------------------------
# 向量：f32 blob <-> list[float]，stdlib 实现（1024d 规模无需 numpy）
# ---------------------------------------------------------------------------

def _v2b(v: Union[Sequence[float], bytes, bytearray]) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return array("f", (float(x) for x in v)).tobytes()


def _b2v(b: Union[bytes, bytearray, memoryview]) -> list:
    a = array("f")
    a.frombytes(bytes(b))
    return list(a)


def _as_vec(embed: Any) -> list:
    """调用方传 np.ndarray / list / f32 bytes 都行；返回 list[float]。"""
    if embed is None:
        raise ValueError("item['embed'] is required")
    if isinstance(embed, (bytes, bytearray, memoryview)):
        return _b2v(embed)
    return [float(x) for x in embed]


def _norm(v: list) -> float:
    return sqrt(sum(x * x for x in v))


def _unit(v: list) -> list:
    n = _norm(v)
    return [x / n for x in v] if n > 0 else list(v)


def _cos(a: list, b: list) -> float:
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _mean_unit(vecs: list) -> list:
    """成员 embed 的 unit-norm 均值（centroid 重算）。"""
    if not vecs:
        raise ValueError("cannot build centroid of zero members")
    d = len(vecs[0])
    acc = [0.0] * d
    for v in vecs:
        for i in range(d):
            acc[i] += v[i]
    return _unit([x / len(vecs) for x in acc])


def _today() -> str:
    return date.today().isoformat()


def _plus_ttl(day: str, ttl_days: int = TTL_DAYS) -> str:
    return (date.fromisoformat(day) + timedelta(days=ttl_days)).isoformat()


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def init_db(path) -> sqlite3.Connection:
    """打开/创建 history.sqlite：WAL + schema。传 ':memory:' 可跑全量自测。"""
    p = str(path)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # :memory: 不支持 WAL，忽略
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 比对集
# ---------------------------------------------------------------------------

def open_clusters(conn: sqlite3.Connection, today: Optional[str] = None) -> list:
    """比对集 = state='open' AND expires_at>=today（默认今天）。行不删，过期仅退出。"""
    today = today or _today()
    rows = conn.execute(
        "SELECT cluster_id, canonical_title, centroid, first_seen, last_seen,"
        "       expires_at, item_count, n_reissues, published"
        " FROM clusters WHERE state='open' AND expires_at>=?", (today,)).fetchall()
    return [dict(r) | {"centroid": _b2v(r["centroid"])} for r in rows]


def _cluster_members(conn: sqlite3.Connection, cid: int) -> list:
    return conn.execute(
        "SELECT item_id, title, simhash, embed, verdict, day FROM items"
        " WHERE cluster_id=?", (cid,)).fetchall()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def add_cluster(conn: sqlite3.Connection, title: str, day: str,
                centroid: Any, state: str = "open",
                first_seen: Optional[str] = None,
                item_count: int = 1, n_reissues: int = 0,
                published: int = 0) -> int:
    """新建 cluster。centroid 自动 unit-norm；expires_at = last_seen + TTL。"""
    c = _unit(_as_vec(centroid))
    fs = first_seen or day
    exp = _plus_ttl(day)
    cur = conn.execute(
        "INSERT INTO clusters(canonical_title,centroid,first_seen,last_seen,"
        " expires_at,item_count,n_reissues,state,published)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (title, _v2b(c), fs, day, exp, item_count, n_reissues, state, published))
    conn.commit()
    return int(cur.lastrowid)


def add_item(conn: sqlite3.Connection, item: dict, cluster_id: int,
             verdict: str = "candidate",
             judge: Union[str, dict, None] = None,
             match_cos: Optional[float] = None) -> int:
    """登记一条 item。item 至少含 title/url_canon/embed；simhash/url_hash 缺省自动算。"""
    url_c = item.get("url_canon") or canon_url(item.get("url", ""))
    uh = item.get("url_hash") or (url_hash(url_c) if url_c else _EMPTY_SHA1)
    sh = item.get("simhash")
    if sh is None:
        sh = simhash64(feature_text(item))
    title = item.get("title") or item.get("title_zh") or url_c or "(untitled)"
    lang = item.get("lang") or ("zh" if re.search(r"[一-鿿]", title) else "en")
    jtxt = judge if isinstance(judge, (str, type(None))) else json.dumps(judge, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO items(cluster_id,day,episode,title,summary,source,url_canon,"
        " url_hash,lang,simhash,embed,verdict,match_cos,judge)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cluster_id, item.get("day") or _today(), item.get("episode"),
         title, item.get("summary"), item.get("source"), url_c, uh, lang,
         _s64(int(sh)), _v2b(_as_vec(item["embed"])), verdict, match_cos, jtxt))
    conn.commit()
    return int(cur.lastrowid)


def _merge_into_cluster(conn: sqlite3.Connection, cid: int, vec: list,
                        day: str, reissue: bool) -> None:
    """把成员向量并入 centroid（运行均值），刷新 last_seen/expires_at。

    expires_at 只延不缩（迟到回填不会缩短已延的保鲜期）。
    """
    row = conn.execute(
        "SELECT centroid,item_count,n_reissues,last_seen,expires_at"
        " FROM clusters WHERE cluster_id=?", (cid,)).fetchone()
    cent, n = _b2v(row["centroid"]), row["item_count"]
    acc = [c * n + v for c, v in zip(cent, vec)]
    m = _unit([x / (n + 1) for x in acc])
    last_seen = max(row["last_seen"], day)
    expires = max(row["expires_at"], _plus_ttl(day))
    conn.execute(
        "UPDATE clusters SET centroid=?, item_count=?, n_reissues=?,"
        " last_seen=?, expires_at=? WHERE cluster_id=?",
        (_v2b(m), n + 1, row["n_reissues"] + (1 if reissue else 0),
         last_seen, expires, cid))
    conn.commit()


def _attach(conn: sqlite3.Connection, item: dict, cid: int, verdict: str,
            day: str, judge: Union[str, dict, None] = None,
            match_cos: Optional[float] = None) -> int:
    """挂成员 + centroid 并入 + 保鲜期刷新（reissue 时 n_reissues++）。"""
    iid = add_item(conn, item, cid, verdict=verdict, judge=judge, match_cos=match_cos)
    _merge_into_cluster(conn, cid, _unit(_as_vec(item["embed"])), day,
                        reissue=(verdict == "reissue"))
    return iid


# ---------------------------------------------------------------------------
# 判定级联
# ---------------------------------------------------------------------------

def check(conn: sqlite3.Connection, item: dict,
          judge_fn: Optional[Callable[[dict, dict], Any]] = None,
          today: Optional[str] = None) -> dict:
    """判定 + 落库。返回 dict：

      verdict     'fresh'|'suppressed'|'reissue'|'gray'  （35_dedup.jsonl 契约词表；
                                                       'gray' 对应 DB verdict='gray_pending'）
      via         dup_exact|dup_near|dup_same|judge_a|judge_b|judge_c|judge_na|embed_lo|no_history
      cluster_id  归属/候选 cluster；item_id 已登记行；match 命中 cluster 标题
      cos/ham     证据字段；judge 判词原文；update_of 仅 reissue 且挂到 published=1 时非空

    item 字段：url_hash, simhash, embed 必需（缺省自动从 url/title+summary 补算）；
              title/title_zh, summary, source, lang, day, episode 可选。
    judge_fn(item, match_ctx) -> 'A'|'B'|'C' 或含 label 的 dict；
    match_ctx = {cluster_id, canonical_title, cos, published}。
    """
    day = item.get("day") or today or _today()
    vec = _unit(_as_vec(item["embed"]))
    sh = item.get("simhash")
    if sh is None:
        sh = simhash64(feature_text(item))
    sh = int(sh) & 0xFFFFFFFFFFFFFFFF
    uh = item.get("url_hash")
    if uh is None:
        uc = item.get("url_canon") or item.get("url") or ""
        uh = url_hash(uc) if uc else ""

    def ret(verdict, via, cid, iid, match=None, cos=None, ham=None,
            judge=None, update_of=None):
        return {"verdict": verdict, "via": via, "cluster_id": cid, "item_id": iid,
                "match": match, "cos": cos, "ham": ham, "judge": judge,
                "update_of": update_of}

    # 1) url_hash 精确命中任意历史 item -> dup_exact（不限开放 cluster：
    #    同一 URL 以前见过即压，哪怕故事已过期）
    if uh and uh != _EMPTY_SHA1:
        row = conn.execute(
            "SELECT i.cluster_id, i.title, c.published FROM items i"
            " JOIN clusters c ON c.cluster_id=i.cluster_id"
            " WHERE i.url_hash=? LIMIT 1", (uh,)).fetchone()
        if row:
            cid = int(row["cluster_id"])
            iid = _attach(conn, item, cid, "suppressed", day)
            return ret("suppressed", "dup_exact", cid, iid, match=row["title"])

    # 2)+3) 单趟扫开放 cluster：成员海明距（dup_near + dup_same 双保险）+ centroid cos
    best = None          # (cos, cluster dict)
    best_ham = None      # best cluster 成员最小海明距
    for cl in open_clusters(conn, day):
        members = _cluster_members(conn, cl["cluster_id"])
        min_ham = min((hamming(sh, m["simhash"]) for m in members), default=64)
        if min_ham <= SIMHASH_H:
            cid = cl["cluster_id"]
            iid = _attach(conn, item, cid, "suppressed", day)
            return ret("suppressed", "dup_near", cid, iid,
                       match=cl["canonical_title"], ham=min_ham)
        c = _cos(vec, cl["centroid"])
        if best is None or c > best[0]:
            best, best_ham = (c, cl), min_ham

    if best is None:     # 无历史/比对集为空 -> 新 cluster
        cid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                          day, vec)
        iid = add_item(conn, item, cid, verdict="candidate")
        return ret("fresh", "no_history", cid, iid)

    cos, cl = best
    cid, match, published = cl["cluster_id"], cl["canonical_title"], cl["published"]

    # cos>=0.85 且成员 simhash<=8 -> dup_same（双保险，几乎不单独触发）
    if cos >= T_AUTO and (best_ham is not None and best_ham <= SIMHASH_HI):
        iid = _attach(conn, item, cid, "suppressed", day, match_cos=round(cos, 4))
        return ret("suppressed", "dup_same", cid, iid, match=match,
                   cos=round(cos, 4), ham=best_ham)

    # [0.58, 0.85) 或 cos>=0.85 但海明距超 8 -> gray -> judge_fn
    if cos >= T_GRAY:
        ctx = {"cluster_id": cid, "canonical_title": match,
               "cos": round(cos, 4), "published": published}
        label, jraw = None, None
        if judge_fn is not None:
            try:
                jraw = judge_fn(item, ctx)
                label = (jraw.get("label") if isinstance(jraw, dict) else jraw)
                label = str(label).strip().upper() if label is not None else None
            except Exception as e:                       # judge 不可用 -> 人工
                jraw = {"label": "ERR", "reason": f"{type(e).__name__}: {e}"}
        if label == "A":                                 # 同事件无新信息 -> 压
            iid = _attach(conn, item, cid, "suppressed", day,
                          judge=jraw, match_cos=round(cos, 4))
            return ret("suppressed", "judge_a", cid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw)
        if label == "B":                                 # 同故事新进展 -> 重报
            iid = _attach(conn, item, cid, "reissue", day,
                          judge=jraw, match_cos=round(cos, 4))
            # update_of 只许挂 published=1 的 cluster（未曾出片的线不算"更新"）
            return ret("reissue", "judge_b", cid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw,
                       update_of=cid if published else None)
        if label == "C":                                 # 不同事件 -> 新 cluster
            ncid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                               day, vec)
            iid = add_item(conn, item, ncid, verdict="candidate",
                           judge=jraw, match_cos=round(cos, 4))
            return ret("fresh", "judge_c", ncid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw)
        # judge 缺失/非法 -> gray_pending 进人工 UI；仍挂候选 cluster（误并可 split）
        iid = _attach(conn, item, cid, "gray_pending", day,
                      judge=jraw, match_cos=round(cos, 4))
        return ret("gray", "judge_na", cid, iid, match=match,
                   cos=round(cos, 4), ham=best_ham, judge=jraw)

    # cos < 0.58 -> 新 cluster
    ncid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                       day, vec)
    iid = add_item(conn, item, ncid, verdict="candidate", match_cos=round(cos, 4))
    return ret("fresh", "embed_lo", ncid, iid, match=match, cos=round(cos, 4))


# ---------------------------------------------------------------------------
# 出片回写 / 保鲜 / 人工算子
# ---------------------------------------------------------------------------

def _find_item_ids(conn: sqlite3.Connection, episode: str,
                   keys: Iterable[Union[int, str]]) -> list:
    """item_key(=sha256(url_canon)[:16]) 或 int item_id -> items.item_id 列表。"""
    keys = list(keys)
    int_ids = {int(k) for k in keys if isinstance(k, int) or
               (isinstance(k, str) and k.isdigit() and len(k) != 16)}
    hex_keys = {k for k in keys if isinstance(k, str) and
                not (k.isdigit() and len(k) != 16)}
    out = set()
    for rows in (conn.execute(
            "SELECT item_id, url_canon FROM items WHERE day=?", (episode,)),
            conn.execute("SELECT item_id, url_canon FROM items")):
        for iid, uc in rows:
            if iid in int_ids or item_key(uc) in hex_keys:
                out.add(int(iid))
        if out:                       # episode 当天命中即可，不命中再全表扫
            break
    return sorted(out)


def mark_reported(conn: sqlite3.Connection, episode: str,
                  item_keys: Iterable[Union[int, str]]) -> int:
    """出片后回写：items.verdict='reported' + episode，clusters.published=1。

    item_keys 收契约 item_key（sha256(url_canon)[:16]）或 items.item_id。
    返回被标记的 item 数。
    """
    iids = _find_item_ids(conn, episode, item_keys)
    if not iids:
        return 0
    q = ",".join("?" * len(iids))
    conn.execute(
        f"UPDATE items SET verdict='reported', episode=? WHERE item_id IN ({q})",
        (episode, *iids))
    cids = [r[0] for r in conn.execute(
        f"SELECT DISTINCT cluster_id FROM items WHERE item_id IN ({q})", iids)]
    conn.execute(
        f"UPDATE clusters SET published=1 WHERE cluster_id IN"
        f" ({','.join('?' * len(cids))})", cids)
    conn.commit()
    return len(iids)


def expire_clusters(conn: sqlite3.Connection, today: Optional[str] = None,
                    ttl_days: int = TTL_DAYS) -> int:
    """过期 pass：先把 expires_at 归一为 last_seen+ttl_days（TTL 改过也生效），
    再把 expires_at<today 的开放 cluster 置 'expired'（行保留可回放）。返回条数。"""
    today = today or _today()
    conn.execute(
        "UPDATE clusters SET expires_at=date(last_seen, '+' || ? || ' days')"
        " WHERE state='open'", (ttl_days,))
    n = conn.execute(
        "UPDATE clusters SET state='expired' WHERE state='open' AND expires_at<?",
        (today,)).rowcount
    conn.commit()
    return n


def _recompute_cluster(conn: sqlite3.Connection, cid: int) -> None:
    """由成员重算 centroid/item_count/n_reissues/published/first_seen/last_seen/expires。"""
    members = _cluster_members(conn, cid)
    if not members:
        return
    vecs = [_b2v(m["embed"]) for m in members]
    days = [m["day"] for m in members]
    n_re = sum(1 for m in members if m["verdict"] == "reissue")
    pub = 1 if any(m["verdict"] == "reported" for m in members) else 0
    first, last = min(days), max(days)
    conn.execute(
        "UPDATE clusters SET centroid=?, item_count=?, n_reissues=?, published=?,"
        " first_seen=?, last_seen=?, expires_at=? WHERE cluster_id=?",
        (_v2b(_mean_unit(vecs)), len(members), n_re, pub,
         first, last, _plus_ttl(last), cid))
    conn.commit()


def split_cluster(conn: sqlite3.Connection, cluster_id: int,
                  item_ids: Optional[Iterable[int]] = None) -> Optional[int]:
    """把误并的成员拆成新 cluster（防误并污染 centroid——两侧都按成员重算）。

    item_ids 缺省 = cluster 里 day==last_seen 的那批（最近一次挂进来的，
    人工算子 `--split <cluster_id>` 的语义）；拆不动（<=1 成员）返回 None。
    """
    members = _cluster_members(conn, cluster_id)
    if len(members) <= 1:
        return None
    if item_ids is None:
        last = conn.execute("SELECT last_seen FROM clusters WHERE cluster_id=?",
                            (cluster_id,)).fetchone()["last_seen"]
        item_ids = [m["item_id"] for m in members if m["day"] == last]
        if len(item_ids) >= len(members):      # 全部同天 -> 留最早一条其余拆出
            item_ids = sorted(item_ids)[1:]
    item_ids = [int(i) for i in item_ids]
    if not item_ids or len(item_ids) >= len(members):
        return None
    moved = [m for m in members if m["item_id"] in set(item_ids)]
    title = sorted(moved, key=lambda m: m["day"])[0]["title"]
    ncid = add_cluster(conn, title, min(m["day"] for m in moved),
                       _mean_unit([_b2v(m["embed"]) for m in moved]))
    conn.execute(
        f"UPDATE items SET cluster_id=? WHERE item_id IN"
        f" ({','.join('?' * len(item_ids))})", (ncid, *item_ids))
    conn.commit()
    _recompute_cluster(conn, ncid)
    _recompute_cluster(conn, cluster_id)
    return ncid


def merge_clusters(conn: sqlite3.Connection, a: int, b: int) -> int:
    """把 b 并进 a（a 存活，b.state='merged' 留痕）。成员、centroid、published 全并入。"""
    if a == b:
        return a
    conn.execute("UPDATE items SET cluster_id=? WHERE cluster_id=?", (a, b))
    conn.execute("UPDATE clusters SET state='merged' WHERE cluster_id=?", (b,))
    conn.commit()
    _recompute_cluster(conn, a)
    return a


# ---------------------------------------------------------------------------
# 自测：python3 stages/lib/store.py（:memory:，无外部依赖）
# ---------------------------------------------------------------------------

def _selftest() -> None:
    conn = init_db(":memory:")
    D = "2026-09-20"

    def mk(title, url, vec, summary="", day=D):
        return {"title": title, "summary": summary, "url_canon": canon_url(url),
                "url": url, "day": day, "embed": vec,
                "url_hash": url_hash(url),
                "simhash": simhash64(title + " " + summary)}

    v1 = _unit([1.0] + [0.0] * 1023)
    v1b = _unit([0.9999] + [0.01] + [0.0] * 1022)   # ~cos 1.0 vs v1
    v2 = _unit([0.0, 1.0] + [0.0] * 1022)
    vg = _unit([0.8] + [0.6] + [0.0] * 1022)        # cos 0.8 vs v1 -> gray
    vgc = _unit([0.75, 0.0, 0.6614] + [0.0] * 1021)  # cos 0.75 vs v1, cos 0.6 vs vg

    # 冷启动：无历史 -> fresh
    r = check(conn, mk("OpenAI 发布 GPT-6", "https://openai.com/blog/gpt6?utm_source=x", v1))
    assert r["verdict"] == "fresh" and r["via"] == "no_history", r
    c1 = r["cluster_id"]

    # url_hash 命中（带跟踪参数的同 URL）-> dup_exact
    r = check(conn, mk("换皮标题", "https://openai.com/blog/gpt6?fbclid=zz", v1b))
    assert r["verdict"] == "suppressed" and r["via"] == "dup_exact" and r["cluster_id"] == c1, r

    # simhash<=4（标题微调、特征相同）-> dup_near
    r = check(conn, mk("OpenAI 发布 GPT-6。", "https://news.site/a1", v1b))
    assert r["via"] == "dup_near" and r["verdict"] == "suppressed", r

    # cos>=0.85 且与成员 ham∈(4,8] -> dup_same（构造：成员 simhash 翻 6 bit，
    # 既过 dup_near 的 >4，又满足双保险的 <=8；措辞完全不同故天然 simhash 很远）
    m1_sh = conn.execute(
        "SELECT simhash FROM items WHERE cluster_id=? LIMIT 1", (c1,)).fetchone()[0]
    it = mk("完全不同的措辞 某实验室新模型", "https://news.site/a2", v1b)
    it["simhash"] = (m1_sh & 0xFFFFFFFFFFFFFFFF) ^ 0x3F      # ham=6 vs m1
    r = check(conn, it)
    assert r["verdict"] == "suppressed" and r["via"] == "dup_same", r

    # gray：cos~0.8 -> judge A -> suppressed
    it = mk("GPT-6 正式开放 API", "https://news.site/a3", vg)
    r = check(conn, it, judge_fn=lambda i, m: {"label": "A", "reason": "同文"})
    assert r["verdict"] == "suppressed" and r["via"] == "judge_a", r

    # gray -> judge B -> reissue；cluster 未 published -> update_of 必须 None
    it = mk("GPT-6 API 涨价 50%", "https://news.site/a4", vg)
    r = check(conn, it, judge_fn=lambda i, m: "B")
    assert r["verdict"] == "reissue" and r["update_of"] is None and r["cluster_id"] == c1, r
    nre = conn.execute("SELECT n_reissues FROM clusters WHERE cluster_id=?", (c1,)).fetchone()[0]
    assert nre == 1, nre

    # gray -> judge C -> fresh（新 cluster；用 vgc 避免后续 vg 条目误吸进它）
    it = mk("Claude 5 发布", "https://news.site/a5", vgc)
    r = check(conn, it, judge_fn=lambda i, m: {"label": "C"})
    assert r["verdict"] == "fresh" and r["via"] == "judge_c" and r["cluster_id"] != c1, r

    # gray -> judge 抛错 -> gray_pending（契约词表 'gray'）
    it = mk("OpenAI 董事会改组", "https://news.site/a6", vg)
    def boom(i, m):
        raise RuntimeError("gateway_fail")
    r = check(conn, it, judge_fn=boom)
    assert r["verdict"] == "gray" and r["via"] == "judge_na", r
    gid = r["item_id"]

    # mark_reported：published=1 之后 judge B 才给 update_of
    #（注意：dup_exact 条目与首条同 url_canon -> 同 item_key，会一起被标记；
    #  取 url 唯一的成员验证 n==1）
    ik = conn.execute(
        "SELECT url_canon FROM items WHERE cluster_id=? AND title LIKE '%GPT-6。%'",
        (c1,)).fetchone()[0]
    n = mark_reported(conn, D, [item_key(ik)])
    assert n == 1, n
    pub = conn.execute("SELECT published FROM clusters WHERE cluster_id=?", (c1,)).fetchone()[0]
    assert pub == 1, pub
    it = mk("某实验室官宣下一代训练框架", "https://news.site/a7", vg)
    r = check(conn, it, judge_fn=lambda i, m: "B")
    assert r["verdict"] == "reissue" and r["update_of"] == c1, r

    # merge / split
    c_new = add_cluster(conn, "独立故事", D, v2)
    add_item(conn, mk("独立故事 A", "https://news.site/b1", v2), c_new, verdict="candidate")
    merge_clusters(conn, c1, c_new)
    st = conn.execute("SELECT state FROM clusters WHERE cluster_id=?", (c_new,)).fetchone()[0]
    assert st == "merged", st
    ncid = split_cluster(conn, c1)          # 默认拆最近一天挂入批
    assert ncid is not None and ncid != c1
    oc = {c["cluster_id"] for c in open_clusters(conn, D)}
    assert c1 in oc and ncid in oc and c_new not in oc

    # expire：把 last_seen 拨到 30 天前 -> 退出比对集
    conn.execute("UPDATE clusters SET last_seen='2026-08-20' WHERE cluster_id=?", (ncid,))
    conn.commit()
    n = expire_clusters(conn, today=D, ttl_days=TTL_DAYS)
    assert n >= 1
    assert ncid not in {c["cluster_id"] for c in open_clusters(conn, D)}
    assert conn.execute("SELECT state FROM clusters WHERE cluster_id=?",
                        (ncid,)).fetchone()[0] == "expired"

    # gray_pending 成员确实在库（人工可 split 出）
    assert conn.execute("SELECT verdict FROM items WHERE item_id=?",
                        (gid,)).fetchone()[0] == "gray_pending"
    print("store selftest OK")


if __name__ == "__main__":
    _selftest()
