"""story-line 去重历史库 — state/state.sqlite 内 dedup_* 表的读写层
（PLAN §7.2；与条目池 items/item_runs 同库，表名去重前缀防撞）。

单文件 SQLite（WAL），两级结构 dedup_clusters/dedup_items；published
标记由 meta_qa 在出片后经 mark_reported() 回写。

行基本不删：cluster 过期只是退出比对集（state='expired'），可审计、可回放；
唯二的删除路径是 purge_episode()（--rerun 整期重跑）与 init_db 的
(url_hash,day) 自愈去重——两者都按剩余成员重算受影响簇，空簇删行。

事务约定：写函数（add_*/_attach/_merge_into_cluster/_recompute_cluster/
mark_reported/expire/unexpire/split/merge/purge_*）一律不 commit——事务
边界在调用方（dedup cmd_run 整期单事务；meta_qa 回写单事务）。rerun
幂等靠两层：默认 replay_uh 同日重放（零写零 LLM，产物逐字节复现），
--rerun 时 purge_episode 先清当日再全量重算；(url_hash,day) 部分唯一
索引兜底任何漏网的双写。

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

冷启动：state.sqlite 的 dedup_* 为空时由 stages/dedup.py 回填近 7 日
raw_cache 预热比对集（本模块只管读写，不管回填来源）。

stdlib only — 与 lib/meta.py 同级约定，任何 PEP 723 stage 脚本可安全 import；
调用方传进来的 embed 可以是 list/np.ndarray/f32 bytes，本模块统一归一。

用法（stages/dedup.py）::

    from stages.lib import store
    conn = store.init_db()   # state/state.sqlite（dedup_* 表侧）
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

from stages.lib import state

# DDL 已收口 stages/lib/state.py（表名 items/clusters → dedup_*
# 家族，与条目池同库共存）。


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
    """sha1(url_canon) — dedup_items.url_hash，dup_exact 判定键。"""
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


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _check_day(day) -> str:
    """期号/日期入参校验：必须 YYYY-MM-DD 且是合法日期。

    day/today 会进 expires_at 字符串比较（比对集归属）与 INSERT——
    非法值（如非日期 run-dir 名 'verify-dedup'）必须在任何写库/
    更新之前抛 ValueError，否则字符串比较会静默污染整个比对集。
    """
    d = str(day or "")
    if not _DAY_RE.fullmatch(d):
        raise ValueError(f"day/episode 必须是 YYYY-MM-DD，得到 {day!r}")
    date.fromisoformat(d)                # 拦 2026-02-31 这类形似但非法的日期
    return d


def _plus_ttl(day: str, ttl_days: int = TTL_DAYS) -> str:
    return (date.fromisoformat(_check_day(day))
            + timedelta(days=ttl_days)).isoformat()


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def init_db(path=None) -> sqlite3.Connection:
    """打开/创建 state.sqlite 的 dedup_* 表侧：schema + 幂等迁移。

    ':memory:' 可跑全量自测。迁移（每次打开都跑，全是 no-op 安全的）：
      dedup_items.via 列补缺 → (url_hash,day) 重复行自愈去重（旧版 rerun
      残留）→ 部分 UNIQUE 索引兜底。注意去重会真删行并重算受影响簇。
    """
    conn = state.open(path if path is not None else state.DEFAULT_DB)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dedup_items)")}
    if "via" not in cols:
        conn.execute("ALTER TABLE dedup_items ADD COLUMN via TEXT")
    _dedupe_url_day(conn)
    conn.execute(  # 空 url 行（_EMPTY_SHA1）不参与唯一约束
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dedup_items_url_day"
        f" ON dedup_items(url_hash, day) WHERE url_hash <> '{_EMPTY_SHA1}'")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 比对集
# ---------------------------------------------------------------------------

def open_clusters(conn: sqlite3.Connection, today: Optional[str] = None) -> list:
    """比对集 = state='open' AND expires_at>=today（默认今天）。行不删，过期仅退出。"""
    today = _check_day(today or _today())
    rows = conn.execute(
        "SELECT cluster_id, canonical_title, centroid, first_seen, last_seen,"
        "       expires_at, item_count, n_reissues, published"
        " FROM dedup_clusters WHERE state='open' AND expires_at>=?", (today,)).fetchall()
    return [dict(r) | {"centroid": _b2v(r["centroid"])} for r in rows]


def _cluster_members(conn: sqlite3.Connection, cid: int) -> list:
    return conn.execute(
        "SELECT item_id, title, simhash, embed, verdict, day FROM dedup_items"
        " WHERE cluster_id=?", (cid,)).fetchall()


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def add_cluster(conn: sqlite3.Connection, title: str, day: str,
                centroid: Any, state: str = "open",
                first_seen: Optional[str] = None,
                item_count: int = 1, n_reissues: int = 0,
                published: int = 0,
                cmpset: "CmpSet | None" = None) -> int:
    """新建 cluster。centroid 自动 unit-norm；expires_at = last_seen + TTL。"""
    c = _unit(_as_vec(centroid))
    day = _check_day(day)
    fs = _check_day(first_seen) if first_seen else day
    exp = _plus_ttl(day)
    cur = conn.execute(
        "INSERT INTO dedup_clusters(canonical_title,centroid,first_seen,last_seen,"
        " expires_at,item_count,n_reissues,state,published)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (title, _v2b(c), fs, day, exp, item_count, n_reissues, state, published))
    cid = int(cur.lastrowid)
    if cmpset is not None and state == "open":
        cmpset._add_row(cid, {
            "cluster_id": cid, "canonical_title": title,
            "first_seen": fs, "last_seen": day, "expires_at": exp,
            "item_count": item_count, "n_reissues": n_reissues,
            "published": published},
            _b2v(_v2b(c)))                    # f32 回环——与 DB blob 逐字节一致
    return cid


def _insert_item(conn: sqlite3.Connection, item: dict, cluster_id: int,
                 verdict: str = "candidate",
                 judge: Union[str, dict, None] = None,
                 match_cos: Optional[float] = None,
                 via: Optional[str] = None,
                 cmpset: "CmpSet | None" = None) -> tuple:
    """INSERT items 一行 -> (item_id, inserted)。

    (url_hash,day) 唯一约束命中（本 run 内同 url 二次登记/历史残留）时
    不报错：复用已有行返回 inserted=False——调用方据此跳过 centroid 合并
    与镜像成员追加，防同向量重复计数。"""
    url_c = item.get("url_canon") or canon_url(item.get("url", ""))
    uh = item.get("url_hash") or (url_hash(url_c) if url_c else _EMPTY_SHA1)
    sh = item.get("simhash")
    if sh is None:
        sh = simhash64(feature_text(item))
    title = item.get("title") or item.get("title_zh") or url_c or "(untitled)"
    lang = item.get("lang") or ("zh" if re.search(r"[一-鿿]", title) else "en")
    jtxt = judge if isinstance(judge, (str, type(None))) else json.dumps(judge, ensure_ascii=False)
    iday = _check_day(item.get("day") or _today())
    try:
        cur = conn.execute(
            "INSERT INTO dedup_items(cluster_id,day,episode,title,summary,source,url_canon,"
            " url_hash,lang,simhash,embed,verdict,via,match_cos,judge)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cluster_id, iday, item.get("episode"),
             title, item.get("summary"), item.get("source"), url_c, uh, lang,
             _s64(int(sh)), _v2b(_as_vec(item["embed"])), verdict, via,
             match_cos, jtxt))
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT item_id FROM dedup_items WHERE url_hash=? AND day=?"
            " ORDER BY item_id LIMIT 1", (uh, iday)).fetchone()
        if row is None:
            raise
        return int(row["item_id"]), False
    if cmpset is not None:
        cmpset._append_member(cluster_id, int(sh))
    return int(cur.lastrowid), True


def add_item(conn: sqlite3.Connection, item: dict, cluster_id: int,
             verdict: str = "candidate",
             judge: Union[str, dict, None] = None,
             match_cos: Optional[float] = None,
             via: Optional[str] = None,
             cmpset: "CmpSet | None" = None) -> int:
    """登记一条 item -> item_id。item 至少含 title/url_canon/embed；
    simhash/url_hash 缺省自动算。(url_hash,day) 冲突时返回已有行 id。"""
    iid, _ = _insert_item(conn, item, cluster_id, verdict=verdict,
                          judge=judge, match_cos=match_cos, via=via,
                          cmpset=cmpset)
    return iid


def _merge_into_cluster(conn: sqlite3.Connection, cid: int, vec: list,
                        day: str, reissue: bool,
                        cmpset: "CmpSet | None" = None) -> None:
    """把成员向量并入 centroid（运行均值），刷新 last_seen/expires_at。

    expires_at 只延不缩（迟到回填不会缩短已延的保鲜期）。
    """
    day = _check_day(day)
    row = conn.execute(
        "SELECT centroid,item_count,n_reissues,last_seen,expires_at"
        " FROM dedup_clusters WHERE cluster_id=?", (cid,)).fetchone()
    cent, n = _b2v(row["centroid"]), row["item_count"]
    acc = [c * n + v for c, v in zip(cent, vec)]
    m = _unit([x / (n + 1) for x in acc])
    last_seen = max(row["last_seen"], day)
    expires = max(row["expires_at"], _plus_ttl(day))
    conn.execute(
        "UPDATE dedup_clusters SET centroid=?, item_count=?, n_reissues=?,"
        " last_seen=?, expires_at=? WHERE cluster_id=?",
        (_v2b(m), n + 1, row["n_reissues"] + (1 if reissue else 0),
         last_seen, expires, cid))
    if cmpset is not None:
        cmpset._set_centroid(cid, _b2v(_v2b(m)))
        idx = cmpset._idx.get(cid)
        if idx is not None:
            cmpset.meta[idx]["item_count"] = n + 1
            cmpset.meta[idx]["n_reissues"] = row["n_reissues"] + (1 if reissue else 0)
            cmpset.meta[idx]["last_seen"] = last_seen
            cmpset.meta[idx]["expires_at"] = expires


def _attach(conn: sqlite3.Connection, item: dict, cid: int, verdict: str,
            day: str, judge: Union[str, dict, None] = None,
            match_cos: Optional[float] = None,
            via: Optional[str] = None,
            cmpset: "CmpSet | None" = None) -> int:
    """挂成员 + centroid 并入 + 保鲜期刷新（reissue 时 n_reissues++）。

    (url_hash,day) 冲突（本 run 内同 url 二次命中）时复用已有行且不再
    并 centroid——同向量重复计数会污染 centroid。"""
    iid, inserted = _insert_item(conn, item, cid, verdict=verdict, judge=judge,
                                 match_cos=match_cos, via=via, cmpset=cmpset)
    if inserted:
        _merge_into_cluster(conn, cid, _unit(_as_vec(item["embed"])), day,
                            reissue=(verdict == "reissue"), cmpset=cmpset)
    return iid


# ---------------------------------------------------------------------------
# 比对集内存镜像（可选加速路径）
# ---------------------------------------------------------------------------

class CmpSet:
    """开放 cluster 比对集的内存镜像：centroid 矩阵 + 成员 simhash 段。

    原 check() 每 rep 全表线性扫（open_clusters + 逐簇 _cluster_members SELECT
    + 纯 Python cos/hamming），~1300 open clusters 下单 rep 0.15-1.5s。
    check(cmpset=...) 改走 numpy 快路（~2ms/rep）；写入路径
    （add_cluster/add_item/_merge_into_cluster/_recompute_cluster）传 cmpset
    同步增量维护，镜像与 DB 逐字节一致（f32 回环），语义与原逐簇扫描等价：
    同行序、同阈值、同"首个海明距命中即短路"顺序。

    numpy 惰性导入（__init__），模块本体仍是 stdlib-only。
    """

    _POP = None    # uint8 popcount 表（lazy 共享）

    def __init__(self, cap: int = 8192, mcap: int = 1 << 17):
        import numpy as np
        self._np = np
        self.ids: list[int] = []        # row idx -> cluster_id（open_clusters 同序）
        self.meta: list[dict] = []      # row idx -> cluster dict（同 open_clusters 键）
        self.cent = np.zeros((cap, 1024), np.float64)
        self.n = 0
        self.mem_cid = np.empty(mcap, np.int32)   # 成员行 -> cluster row idx
        self.mem_sim = np.empty(mcap, np.uint64)
        self.mem_n = 0
        self._idx: dict[int, int] = {}            # cluster_id -> row idx

    @classmethod
    def load(cls, conn: sqlite3.Connection, today: Optional[str] = None) -> "CmpSet":
        """与 open_clusters() 同一 SELECT 建镜像；成员只取 simhash
        （原路径 SELECT embed 但比对只用 simhash——省掉全部 blob 解码）。"""
        cs = cls()
        np = cs._np
        today = _check_day(today or _today())
        rows = conn.execute(
            "SELECT cluster_id, canonical_title, centroid, first_seen, last_seen,"
            "       expires_at, item_count, n_reissues, published"
            " FROM dedup_clusters WHERE state='open' AND expires_at>=?", (today,)).fetchall()
        for r in rows:
            cl = dict(r)
            cid = cl["cluster_id"]
            if cs.n >= cs.cent.shape[0]:
                cs.cent = np.vstack([cs.cent, np.zeros_like(cs.cent)])
            cs._idx[cid] = cs.n
            cs.ids.append(cid)
            cs.meta.append(cl)
            cs.cent[cs.n] = np.asarray(_b2v(cl["centroid"]), dtype=np.float64)
            cs.n += 1
        if cs.n:
            qm = ",".join("?" * cs.n)
            for cid, sim in conn.execute(
                    f"SELECT cluster_id, simhash FROM dedup_items"
                    f" WHERE cluster_id IN ({qm})", tuple(cs.ids)):
                cs._append_member(cid, int(sim))
        return cs

    # -- 镜像维护（写路径调用；cluster 不在比对集内则跳过，属正常） --

    def _append_member(self, cid: int, sim: int) -> None:
        np = self._np
        ridx = self._idx.get(cid)
        if ridx is None:
            return                       # 闭/过期 cluster 成员不参与比对
        if self.mem_n >= self.mem_sim.shape[0]:
            self.mem_cid = np.concatenate([self.mem_cid, np.empty_like(self.mem_cid)])
            self.mem_sim = np.concatenate([self.mem_sim, np.empty_like(self.mem_sim)])
        self.mem_cid[self.mem_n] = ridx
        self.mem_sim[self.mem_n] = np.uint64(int(sim) & 0xFFFFFFFFFFFFFFFF)
        self.mem_n += 1

    def _add_row(self, cid: int, cl: dict, cent) -> None:
        np = self._np
        if self.n >= self.cent.shape[0]:
            self.cent = np.vstack([self.cent, np.zeros_like(self.cent)])
        self._idx[cid] = self.n
        self.ids.append(cid)
        self.meta.append(cl)
        self.cent[self.n] = np.asarray(cent, dtype=np.float64)
        self.n += 1

    def _set_centroid(self, cid: int, cent_f32_blob_vec: list) -> None:
        ridx = self._idx.get(cid)
        if ridx is not None:
            self.cent[ridx] = self._np.asarray(cent_f32_blob_vec, dtype=self._np.float64)

    # -- 比对 --

    def match(self, vec: list, sh: int):
        """(near_ridx|None, near_ham, best_ridx|None, best_cos, best_ham)。
        near = 行序上首个 min_ham<=SIMHASH_H 的 cluster（与旧逐簇顺序短路等价）。"""
        np = self._np
        if self.n == 0:
            return None, None, None, None, None
        if CmpSet._POP is None:
            CmpSet._POP = np.array([bin(i).count("1") for i in range(256)], np.uint8)
        minham = np.full(self.n, 64, np.int16)
        if self.mem_n:
            x = self.mem_sim[:self.mem_n] ^ np.uint64(sh & 0xFFFFFFFFFFFFFFFF)
            ham = CmpSet._POP[x.view(np.uint8).reshape(-1, 8)].sum(1).astype(np.int16)
            np.minimum.at(minham, self.mem_cid[:self.mem_n], ham)
        hits = np.nonzero(minham <= SIMHASH_H)[0]
        near = int(hits[0]) if len(hits) else None
        near_ham = int(minham[near]) if near is not None else None
        v = np.asarray(vec, dtype=np.float64)
        C = self.cent[:self.n]
        denom = np.linalg.norm(C, axis=1) * np.linalg.norm(v)
        cos = (C @ v) / np.where(denom == 0, 1.0, denom)
        best = int(np.argmax(cos))
        return near, near_ham, best, float(cos[best]), int(minham[best])


# ---------------------------------------------------------------------------
# 判定级联
# ---------------------------------------------------------------------------

def _scan(conn: sqlite3.Connection, uh: str, sh: int, vec: list,
          day: str, cmpset: "CmpSet | None" = None) -> dict:
    """check() 的只读判定扫描（不落库）。返回 {"kind", ...}：

      dup_exact  url_hash 命中任意历史 item —— row 含 cluster_id/title/published
      dup_near   首个成员海明距<=SIMHASH_H 的开放 cluster —— cl + ham
      best       无近命中时的 cos 最优开放 cluster —— cl + cos + ham（成员最小海明距）
      empty      比对集为空

    批量预判（dedup --judge-batch）用它生成待判对快照；apply 相位仍走
    check() 的权威重扫（快照仅决定要不要批量 judge，不写库）。
    """
    # 1) url_hash 精确命中任意历史 item -> dup_exact（不限开放 cluster：
    #    同一 URL 以前见过即压，哪怕故事已过期）
    if uh and uh != _EMPTY_SHA1:
        row = conn.execute(
            "SELECT i.cluster_id, i.title, c.published FROM dedup_items i"
            " JOIN dedup_clusters c ON c.cluster_id=i.cluster_id"
            " WHERE i.url_hash=? LIMIT 1", (uh,)).fetchone()
        if row:
            return {"kind": "dup_exact", "row": row}

    # 2)+3) 单趟扫开放 cluster：成员海明距（dup_near + dup_same 双保险）+ centroid cos
    if cmpset is not None:
        near_i, near_ham, best_i, best_cos, bh = cmpset.match(vec, sh)
        if near_i is not None:
            return {"kind": "dup_near", "cl": cmpset.meta[near_i],
                    "ham": near_ham}
        if best_i is not None:
            return {"kind": "best", "cl": cmpset.meta[best_i],
                    "cos": best_cos, "ham": bh}
        return {"kind": "empty"}

    best = None          # (cos, cluster dict)
    best_ham = None      # best cluster 成员最小海明距
    for cl in open_clusters(conn, day):
        members = _cluster_members(conn, cl["cluster_id"])
        min_ham = min((hamming(sh, m["simhash"]) for m in members), default=64)
        if min_ham <= SIMHASH_H:
            return {"kind": "dup_near", "cl": cl, "ham": min_ham}
        c = _cos(vec, cl["centroid"])
        if best is None or c > best[0]:
            best, best_ham = (c, cl), min_ham
    if best is None:
        return {"kind": "empty"}
    return {"kind": "best", "cl": best[1], "cos": best[0], "ham": best_ham}


def check(conn: sqlite3.Connection, item: dict,
          judge_fn: Optional[Callable[[dict, dict], Any]] = None,
          today: Optional[str] = None,
          cmpset: "CmpSet | None" = None,
          replay_uh: "set | None" = None) -> dict:
    """判定 + 落库。返回 dict：

      verdict     'fresh'|'suppressed'|'reissue'|'gray'  （35_dedup.jsonl 契约词表；
                                                       'gray' 对应 DB verdict='gray_pending'）
      via         dup_exact|dup_near|dup_same|judge_a|judge_b|judge_c|judge_na|embed_lo|no_history
      cluster_id  归属/候选 cluster；item_id 已登记行；match 命中 cluster 标题
      cos/ham     证据字段；judge 判词原文；update_of 仅 reissue 且挂到 published=1 时非空
      replayed    仅同日重放时为 True（verdict 自库内当日行重放，未新写）

    item 字段：url_hash, simhash, embed 必需（缺省自动从 url/title+summary 补算）；
              title/title_zh, summary, source, lang, day, episode 可选。
    judge_fn(item, match_ctx) -> 'A'|'B'|'C' 或含 label 的 dict；
    match_ctx = {cluster_id, canonical_title, cos, published}。
    replay_uh：本 run 开始前该 day 已落库的 url_hash 集——命中即重放
    （dedup rerun 幂等）；本 run 内新写的 url 不在其中，同 url 二次命中
    仍走正常级联（dup_exact → suppressed，靠 (url_hash,day) 唯一约束兜底）。
    """
    day = _check_day(item.get("day") or today or _today())
    vec = _unit(_as_vec(item["embed"]))
    sh = item.get("simhash")
    if sh is None:
        sh = simhash64(feature_text(item))
    sh = int(sh) & 0xFFFFFFFFFFFFFFFF
    uh = item.get("url_hash")
    if uh is None:
        uc = item.get("url_canon") or item.get("url") or ""
        uh = url_hash(uc) if uc else ""

    if (replay_uh is not None and uh and uh != _EMPTY_SHA1
            and uh in replay_uh):
        rep = replay_row(conn, uh, day)
        if rep is not None:
            return rep

    def ret(verdict, via, cid, iid, match=None, cos=None, ham=None,
            judge=None, update_of=None):
        return {"verdict": verdict, "via": via, "cluster_id": cid, "item_id": iid,
                "match": match, "cos": cos, "ham": ham, "judge": judge,
                "update_of": update_of}

    s = _scan(conn, uh, sh, vec, day, cmpset)
    kind = s["kind"]

    if kind == "dup_exact":
        row = s["row"]
        cid = int(row["cluster_id"])
        iid = _attach(conn, item, cid, "suppressed", day, via="dup_exact",
                      cmpset=cmpset)
        return ret("suppressed", "dup_exact", cid, iid, match=row["title"])

    if kind == "dup_near":
        cl = s["cl"]
        cid = cl["cluster_id"]
        iid = _attach(conn, item, cid, "suppressed", day, via="dup_near",
                      cmpset=cmpset)
        return ret("suppressed", "dup_near", cid, iid,
                   match=cl["canonical_title"], ham=s["ham"])

    if kind == "empty":  # 无历史/比对集为空 -> 新 cluster
        cid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                          day, vec, cmpset=cmpset)
        iid = add_item(conn, item, cid, verdict="candidate", via="no_history",
                       cmpset=cmpset)
        return ret("fresh", "no_history", cid, iid)

    cos, cl, best_ham = s["cos"], s["cl"], s["ham"]
    cid, match, published = cl["cluster_id"], cl["canonical_title"], cl["published"]

    # cos>=0.85 且成员 simhash<=8 -> dup_same（双保险，几乎不单独触发）
    if cos >= T_AUTO and (best_ham is not None and best_ham <= SIMHASH_HI):
        iid = _attach(conn, item, cid, "suppressed", day, match_cos=round(cos, 4),
                      via="dup_same", cmpset=cmpset)
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
                          judge=jraw, match_cos=round(cos, 4),
                          via="judge_a", cmpset=cmpset)
            return ret("suppressed", "judge_a", cid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw)
        if label == "B":                                 # 同故事新进展 -> 重报
            iid = _attach(conn, item, cid, "reissue", day,
                          judge=jraw, match_cos=round(cos, 4),
                          via="judge_b", cmpset=cmpset)
            # update_of 只许挂 published=1 的 cluster（未曾出片的线不算"更新"）
            return ret("reissue", "judge_b", cid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw,
                       update_of=cid if published else None)
        if label == "C":                                 # 不同事件 -> 新 cluster
            ncid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                               day, vec, cmpset=cmpset)
            iid = add_item(conn, item, ncid, verdict="candidate",
                           judge=jraw, match_cos=round(cos, 4),
                           via="judge_c", cmpset=cmpset)
            return ret("fresh", "judge_c", ncid, iid, match=match,
                       cos=round(cos, 4), ham=best_ham, judge=jraw)
        # judge 缺失/非法 -> gray_pending 进人工 UI；仍挂候选 cluster（误并可 split）
        iid = _attach(conn, item, cid, "gray_pending", day,
                      judge=jraw, match_cos=round(cos, 4),
                      via="judge_na", cmpset=cmpset)
        return ret("gray", "judge_na", cid, iid, match=match,
                   cos=round(cos, 4), ham=best_ham, judge=jraw)

    # cos < 0.58 -> 新 cluster
    ncid = add_cluster(conn, item.get("title") or item.get("title_zh") or "",
                       day, vec, cmpset=cmpset)
    iid = add_item(conn, item, ncid, verdict="candidate", match_cos=round(cos, 4),
                   via="embed_lo", cmpset=cmpset)
    return ret("fresh", "embed_lo", ncid, iid, match=match, cos=round(cos, 4))


# ---------------------------------------------------------------------------
# 出片回写 / 保鲜 / 人工算子
# ---------------------------------------------------------------------------

def _find_item_ids(conn: sqlite3.Connection, episode: str,
                   keys: Iterable[Union[int, str]]) -> list:
    """item_key(=sha256(url_canon)[:16]) 或 int item_id -> dedup_items.item_id 列表。"""
    keys = list(keys)
    int_ids = {int(k) for k in keys if isinstance(k, int) or
               (isinstance(k, str) and k.isdigit() and len(k) != 16)}
    hex_keys = {k for k in keys if isinstance(k, str) and
                not (k.isdigit() and len(k) != 16)}
    out = set()
    for rows in (conn.execute(
            "SELECT item_id, url_canon FROM dedup_items WHERE day=?", (episode,)),
            conn.execute("SELECT item_id, url_canon FROM dedup_items")):
        for iid, uc in rows:
            if iid in int_ids or item_key(uc) in hex_keys:
                out.add(int(iid))
        if out:                       # episode 当天命中即可，不命中再全表扫
            break
    return sorted(out)


def mark_reported(conn: sqlite3.Connection, episode: str,
                  item_keys: Iterable[Union[int, str]]) -> int:
    """出片后回写：dedup_items.verdict='reported' + episode，dedup_clusters.published=1。

    item_keys 收契约 item_key（sha256(url_canon)[:16]）或 dedup_items.item_id。
    返回被标记的 item 数。
    """
    episode = _check_day(episode)
    iids = _find_item_ids(conn, episode, item_keys)
    if not iids:
        return 0
    q = ",".join("?" * len(iids))
    conn.execute(
        f"UPDATE dedup_items SET verdict='reported', episode=? WHERE item_id IN ({q})",
        (episode, *iids))
    cids = [r[0] for r in conn.execute(
        f"SELECT DISTINCT cluster_id FROM dedup_items WHERE item_id IN ({q})", iids)]
    conn.execute(
        f"UPDATE dedup_clusters SET published=1 WHERE cluster_id IN"
        f" ({','.join('?' * len(cids))})", cids)
    return len(iids)


def expire_clusters(conn: sqlite3.Connection, today: Optional[str] = None,
                    ttl_days: int = TTL_DAYS) -> int:
    """过期 pass：先把 expires_at 归一为 last_seen+ttl_days（TTL 改过也生效），
    再把 expires_at<today 的开放 cluster 置 'expired'（行保留可回放）。返回条数。

    today 先过 _check_day —— 非 ISO 日期（如 run-dir 名）在 UPDATE 之前抛错；
    expires_at<today 是字符串比较，'verify-dedup' 这类值曾把全部开放 cluster
    误置 expired。
    """
    today = _check_day(today or _today())
    conn.execute(
        "UPDATE dedup_clusters SET expires_at=date(last_seen, '+' || ? || ' days')"
        " WHERE state='open'", (ttl_days,))
    n = conn.execute(
        "UPDATE dedup_clusters SET state='expired' WHERE state='open' AND expires_at<?",
        (today,)).rowcount
    return n


def unexpire_clusters(conn: sqlite3.Connection, today: Optional[str] = None) -> int:
    """expire 的逆操作（人工算子）：把仍在 TTL 内（expires_at>=today）却误置
    'expired' 的 cluster 恢复回 'open' 比对集。

    expire 只移出比对集、行不删，所以恢复无损；state='merged' 的行不受影响。
    返回恢复条数。
    """
    today = _check_day(today or _today())
    n = conn.execute(
        "UPDATE dedup_clusters SET state='open' WHERE state='expired'"
        " AND expires_at>=?", (today,)).rowcount
    return n


def _recompute_cluster(conn: sqlite3.Connection, cid: int,
                       cmpset: "CmpSet | None" = None) -> None:
    """由成员重算 centroid/item_count/n_reissues/published/first_seen/last_seen/expires。"""
    members = _cluster_members(conn, cid)
    if not members:
        return
    vecs = [_b2v(m["embed"]) for m in members]
    days = [m["day"] for m in members]
    n_re = sum(1 for m in members if m["verdict"] == "reissue")
    pub = 1 if any(m["verdict"] == "reported" for m in members) else 0
    first, last = min(days), max(days)
    exp = _plus_ttl(last)
    conn.execute(
        "UPDATE dedup_clusters SET centroid=?, item_count=?, n_reissues=?, published=?,"
        " first_seen=?, last_seen=?, expires_at=? WHERE cluster_id=?",
        (_v2b(_mean_unit(vecs)), len(members), n_re, pub,
         first, last, exp, cid))
    if cmpset is not None:
        ridx = cmpset._idx.get(cid)
        if ridx is not None:   # 闭/过期 cluster 不在比对集；published 翻转等字段须回镜像
            cmpset._set_centroid(cid, _b2v(_v2b(_mean_unit(vecs))))
            cmpset.meta[ridx].update(
                item_count=len(members), n_reissues=n_re, published=pub,
                first_seen=first, last_seen=last, expires_at=exp)


def _purge_items(conn: sqlite3.Connection, item_ids: Iterable[int],
                 cmpset: "CmpSet | None" = None) -> dict:
    """按 item_id 删成员行；受影响簇按剩余成员重算，空簇整行删除。

    注意必须在 CmpSet.load 之前调用（或之后整体重建镜像）——镜像无
    成员删除原语，删掉的簇/成员在已建镜像里会变成幽灵比对项。
    """
    ids = [int(i) for i in item_ids]
    if not ids:
        return {"items": 0, "clusters_recomputed": 0, "clusters_deleted": 0}
    q = ",".join("?" * len(ids))
    cids = {r[0] for r in conn.execute(
        f"SELECT DISTINCT cluster_id FROM dedup_items WHERE item_id IN ({q})", ids)}
    conn.execute(f"DELETE FROM dedup_items WHERE item_id IN ({q})", ids)
    recomputed = deleted = 0
    for cid in cids:
        if conn.execute("SELECT 1 FROM dedup_items WHERE cluster_id=? LIMIT 1",
                        (cid,)).fetchone():
            _recompute_cluster(conn, cid, cmpset=cmpset)
            recomputed += 1
        else:
            conn.execute("DELETE FROM dedup_clusters WHERE cluster_id=?", (cid,))
            deleted += 1
    return {"items": len(ids), "clusters_recomputed": recomputed,
            "clusters_deleted": deleted}


def purge_episode(conn: sqlite3.Connection, day: str) -> dict:
    """删除某期（day=episode）写入的全部成员行并复原受影响簇 —— dedup
    --rerun 的显式重算入口：清干净当日的判重痕迹后整期重跑。

    复原语义：删行 → 簇按剩余成员重算 centroid/item_count/last_seen/
    expires_at/n_reissues/published；成员清零的簇（当日新建且只收了当日
    条目）整簇删除。注意它只回滚 dedup 写入——若该期已出片
    （mark_reported 写过 verdict='reported'/published=1），重跑后需重新
    走 produce 回写；跨天演进后再 rerun 旧期也不是逐字节回放（比对集已变）。
    """
    day = _check_day(day)
    ids = [r[0] for r in conn.execute(
        "SELECT item_id FROM dedup_items WHERE day=?", (day,))]
    out = _purge_items(conn, ids)
    out["day"] = day
    return out


def _dedupe_url_day(conn: sqlite3.Connection) -> dict:
    """(url_hash,day) 重复行自愈：保留每组最小 item_id，删其余 + 重算簇。

    UNIQUE 索引建立前的清场步骤；旧版 dedup rerun 曾把同 url 重复挂进
    items 并污染 centroid。正常库上是单趟 GROUP BY 空转，开销可忽略。
    """
    ids = [r[0] for r in conn.execute(
        "SELECT i.item_id FROM dedup_items i WHERE i.url_hash <> ?"
        " AND EXISTS (SELECT 1 FROM dedup_items j WHERE j.url_hash=i.url_hash"
        "            AND j.day=i.day AND j.item_id<i.item_id)",
        (_EMPTY_SHA1,))]
    return _purge_items(conn, ids)


_VERDICT_FROM_DB = {"candidate": "fresh", "reported": "fresh",
                    "suppressed": "suppressed", "reissue": "reissue",
                    "gray_pending": "gray"}


def _via_replay(row: sqlite3.Row, j) -> str:
    """旧行 via 列为空时的推断：judge.label/same_day 判词自带 via →
    verdict+match_cos 反推。dup_near 的 ham 不入库、与 dup_exact 不可再分，
    归 dup_exact——这类遗留行的重放 via/ham 字段是 best-effort
    （新行 via 落库后不存在此问题）。"""
    if row["via"]:
        return row["via"]
    if isinstance(j, dict) and j.get("via"):
        return str(j["via"])
    v = row["verdict"]
    if isinstance(j, dict):
        lb = str(j.get("label") or "").strip().upper()
        return {"A": "judge_a", "B": "judge_b", "C": "judge_c"}.get(
            lb, "judge_na")
    if v == "suppressed":
        return "dup_same" if row["match_cos"] is not None else "dup_exact"
    if v in ("candidate", "reported"):
        return "no_history" if row["match_cos"] is None else "embed_lo"
    return "replay"


def replay_row(conn: sqlite3.Connection, uh: str, day: str):
    """当日已落库的同 url_hash 行 → check() 形状的重放结果（不判不写）。

    dedup rerun 幂等的另一半：run_pipeline 把 run 开始前该 day 已有的
    url_hash 集传进来，命中的 rep 直接重放行内 verdict/via/judge——
    35_dedup.jsonl 逐字节复现且零 LLM 调用。当日多行（旧残留）取最早。
    """
    row = conn.execute(
        "SELECT i.item_id, i.cluster_id, i.verdict, i.via, i.match_cos,"
        "       i.judge, c.canonical_title, c.published"
        " FROM dedup_items i JOIN dedup_clusters c ON c.cluster_id=i.cluster_id"
        " WHERE i.url_hash=? AND i.day=? ORDER BY i.item_id LIMIT 1",
        (uh, day)).fetchone()
    if row is None:
        return None
    try:
        j = json.loads(row["judge"]) if row["judge"] else None
    except (json.JSONDecodeError, TypeError):
        j = row["judge"]
    verdict = _VERDICT_FROM_DB.get(row["verdict"], row["verdict"])
    return {"verdict": verdict,
            "via": _via_replay(row, j),
            "cluster_id": int(row["cluster_id"]),
            "item_id": int(row["item_id"]),
            "match": row["canonical_title"],
            "cos": row["match_cos"], "ham": None, "judge": j,
            "update_of": (int(row["cluster_id"])
                          if verdict == "reissue" and row["published"]
                          else None),
            "replayed": True}


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
        last = conn.execute("SELECT last_seen FROM dedup_clusters WHERE cluster_id=?",
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
        f"UPDATE dedup_items SET cluster_id=? WHERE item_id IN"
        f" ({','.join('?' * len(item_ids))})", (ncid, *item_ids))
    _recompute_cluster(conn, ncid)
    _recompute_cluster(conn, cluster_id)
    return ncid


def merge_clusters(conn: sqlite3.Connection, a: int, b: int) -> int:
    """把 b 并进 a（a 存活，b.state='merged' 留痕）。成员、centroid、published 全并入。"""
    if a == b:
        return a
    conn.execute("UPDATE dedup_items SET cluster_id=? WHERE cluster_id=?", (a, b))
    conn.execute("UPDATE dedup_clusters SET state='merged' WHERE cluster_id=?", (b,))
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
        "SELECT simhash FROM dedup_items WHERE cluster_id=? LIMIT 1", (c1,)).fetchone()[0]
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
    nre = conn.execute("SELECT n_reissues FROM dedup_clusters WHERE cluster_id=?", (c1,)).fetchone()[0]
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
        "SELECT url_canon FROM dedup_items WHERE cluster_id=? AND title LIKE '%GPT-6。%'",
        (c1,)).fetchone()[0]
    n = mark_reported(conn, D, [item_key(ik)])
    assert n == 1, n
    pub = conn.execute("SELECT published FROM dedup_clusters WHERE cluster_id=?", (c1,)).fetchone()[0]
    assert pub == 1, pub
    it = mk("某实验室官宣下一代训练框架", "https://news.site/a7", vg)
    r = check(conn, it, judge_fn=lambda i, m: "B")
    assert r["verdict"] == "reissue" and r["update_of"] == c1, r

    # merge / split
    c_new = add_cluster(conn, "独立故事", D, v2)
    add_item(conn, mk("独立故事 A", "https://news.site/b1", v2), c_new, verdict="candidate")
    merge_clusters(conn, c1, c_new)
    st = conn.execute("SELECT state FROM dedup_clusters WHERE cluster_id=?", (c_new,)).fetchone()[0]
    assert st == "merged", st
    ncid = split_cluster(conn, c1)          # 默认拆最近一天挂入批
    assert ncid is not None and ncid != c1
    oc = {c["cluster_id"] for c in open_clusters(conn, D)}
    assert c1 in oc and ncid in oc and c_new not in oc

    # expire：把 last_seen 拨到 30 天前 -> 退出比对集
    conn.execute("UPDATE dedup_clusters SET last_seen='2026-08-20' WHERE cluster_id=?", (ncid,))
    conn.commit()
    n = expire_clusters(conn, today=D, ttl_days=TTL_DAYS)
    assert n >= 1
    assert ncid not in {c["cluster_id"] for c in open_clusters(conn, D)}
    assert conn.execute("SELECT state FROM dedup_clusters WHERE cluster_id=?",
                        (ncid,)).fetchone()[0] == "expired"

    # gray_pending 成员确实在库（人工可 split 出）
    assert conn.execute("SELECT verdict FROM dedup_items WHERE item_id=?",
                        (gid,)).fetchone()[0] == "gray_pending"

    # ---- rerun 幂等（dedup 的默认重放 + --rerun 重算两条路都走这里）----
    D2 = "2026-09-21"
    it = mk("Anthropic 发布 Claude 6", "https://anthropic.com/claude-6", v2,
            day=D2)
    r1 = check(conn, it, today=D2)
    # via 落库：重放的逐字节复现依赖它
    assert conn.execute("SELECT via FROM dedup_items WHERE item_id=?",
                        (r1["item_id"],)).fetchone()[0] == r1["via"]
    n0 = conn.execute("SELECT COUNT(*) FROM dedup_items").fetchone()[0]
    # run_pipeline 同款 replay_uh（run 开始前当日已落库集）
    replay = {r[0] for r in conn.execute(
        "SELECT url_hash FROM dedup_items WHERE day=? AND url_hash<>?",
        (D2, _EMPTY_SHA1))}
    r2v = check(conn, dict(it), today=D2, replay_uh=replay)
    assert r2v["replayed"] and r2v["verdict"] == r1["verdict"] \
        and r2v["item_id"] == r1["item_id"] and r2v["via"] == r1["via"], \
        (r1, r2v)
    # 本 run 内同 url 二次命中（不在 replay 集）→ dup_exact + 唯一约束兜住不重插
    itA = mk("全新独立报道甲", "https://news.site/dup-a1", v2, day=D2)
    check(conn, itA, today=D2, replay_uh=replay)
    rA2 = check(conn, dict(itA), today=D2, replay_uh=replay)
    assert rA2["verdict"] == "suppressed" and rA2["via"] == "dup_exact", rA2
    n1 = conn.execute("SELECT COUNT(*) FROM dedup_items").fetchone()[0]
    assert n1 == n0 + 1, (n0, n1)          # r2v/rA2 均未新增行

    # purge_episode：清掉 D2 全部痕迹，簇复原；幂等可重入
    st = purge_episode(conn, D2)
    assert st["items"] == 2, st            # r1 + rA 两行（rA2 复用未落行）
    assert conn.execute("SELECT COUNT(*) FROM dedup_items WHERE day=?",
                        (D2,)).fetchone()[0] == 0
    assert replay_row(conn, url_hash(it["url_canon"]), D2) is None
    st = purge_episode(conn, D2)
    assert st["items"] == 0
    print("store selftest OK")


if __name__ == "__main__":
    _selftest()
