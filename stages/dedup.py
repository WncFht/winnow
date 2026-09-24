"""dedup — story-line 去重（PLAN §7.2）。

输入：runs/<date>/30_summaries.jsonl（必需，缺则 fail-fast 提示先跑 just filter）
      runs/<date>/10_raw_items.jsonl（可选 join：url/url_canon/_source）
      runs/<date>/20_filtered.jsonl（可选 join：news_value 选同日代表）
      state/state.sqlite（dedup_items/dedup_clusters 跨天比对集，真源）

处理顺序（与 §7.2 一致）：
  1) 同日聚类（跨天之前）：
     - item_key 相同（url_canon 一致）直接并；
     - 稀有 token 倒排（title_zh + entities token，df≤min(8,max(3,N//3)) 才产对）
       ∪ embed kNN top-5（doc 向量互cos）双路召回候选对；
     - 候选对判定：simhash ham≤4 直接并；值得才调 JUDGE_PROMPT
       （共稀有 token 且 cos≥0.55 / 仅 kNN 且 cos≥0.62 / 任意对 cos≥0.80，
       按 cos 降序、上限 MAX_PAIR_JUDGE 次——召回层降本闸门，不是判定阈值），
       label∈{A,B} 且 confidence≥0.6 并；judge 失败/低置信/判 C 不并
       （同日不并=保守，两条都留给人工闸，绝不在失败时自动压）。
     - union-find 合并为同日组，组内代表 = news_value 最高者（缺省取首个）。
  2) 跨天级联（每组代表）：store.check 校准级联
     url_hash→simhash≤4→centroid cos 分层；灰区 judge_fn=llm.chat_json
     （LLMError/超时/低置信 → store 落 gray_pending，绝不自动 suppress）。
  3) 同日 sibling：并入代表所落 cluster，verdict=suppressed（35 可审计）。

嵌入约定（校准自 experiments/dedup-history README：instruct 前缀显著
扩大 keep/dup 间距，必须用）：
  - check() 用 instruct(query) 向量做 cos 比对；
  - 入库 dedup_items.embed / centroid 一律 doc 向量（schema 注释"doc 侧无 instruct"）；
  - check 之后把刚写入的 item 行 embed 改回 doc 向量并 _recompute_cluster，
    保持不变量 centroid = 成员 doc embed 均值。

输出：35_dedup.jsonl —— 每条输入一行（suppressed 也写，可审计），
  {schema, item_key, verdict∈fresh|suppressed|reissue|gray, cluster_id,
   match_cos, judge}。judge 字段：LLM 判词原文 {label,confidence,reason,via}
  或确定性路径证据 {via:dup_exact|dup_near|dup_same|same_day|embed_lo|…}。

CLI：
  uv run stages/dedup.py --run-dir runs/<date>          # 正常跑
  uv run stages/dedup.py --run-dir <YYYY-MM-DD>         # 期号直给（= runs/<date>）
  uv run stages/dedup.py --run-dir runs/<date> --no-judge   # 离线：灰区全 gray_pending
  uv run stages/dedup.py --split <cluster_id> [--db P|--state D]  # 人工拆误并
  uv run stages/dedup.py --merge <a> <b> [--db P|--state D]       # 人工并 cluster
  uv run stages/dedup.py --unexpire [--db P|--state D] [--day D]  # 恢复误过期 cluster
  uv run stages/dedup.py --backfill <raw_items.jsonl> [--db P|--state D]  # 冷启动回填
  uv run stages/dedup.py --selftest                     # 合成 fixture 端到端

  --db P   直接给 state.sqlite 文件路径；
  --state D 给跨天 state 目录（取 <D>/state.sqlite），与 --db 互斥。
  --items-db P  条目池所在 state.sqlite（默认 config.storage.state_db）；
           cmd_run 写完 35_dedup.jsonl 后把 dedup 列投影进池（file-first，失败只告警）。
  --run-dir 的末级目录必须是期号 YYYY-MM-DD（episode/day 由它派生，
  非日期名会 fail-fast exit 2，且不触碰 state.sqlite）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path


import numpy as np

from stages.lib import meta, normalize, pool, prog, prompts, simhash, state, store
from stages.lib import embed as embedlib
from adapters import llm_swe2max as llm
from contracts import models as cm
from concurrent.futures import ThreadPoolExecutor

REPO = Path(__file__).resolve().parents[1]

# ---- 同日聚类参数（召回层自定，判定阈值仍走 store 校准级联） ----
KNN_K = 5                    # embed kNN 每 item 召回近邻数
JUDGE_COS_TOK = 0.55         # 共享稀有 token 的对：cos≥此才问 judge
JUDGE_COS_KNN = 0.62         # 仅 kNN 召回（无 token 重叠）的对：更高门槛
JUDGE_COS_ANY = 0.80         # 任何召回路径：cos≥此必问 judge
JUDGE_MIN_CONF = 0.6         # judge 置信下限，低于 → 不并/gray_pending
MAX_PAIR_JUDGE = 60          # 同日 judge 调用上限（按 cos 降序截断）
JUDGE_WORKERS = int(os.environ.get("DEDUP_JUDGE_WORKERS", "16"))
                             # judge.pair 并发在飞数（IO-bound LLM 调用；网关 bg 爬坡
                             # 放行 ~15-50/min，16 在飞足以吃满且不过度排队）

_TOK_EN = re.compile(r"[a-z0-9]+")
_TOK_ZH = re.compile(r"[一-鿿]+")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return meta.load_jsonl(path)


def _dump_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _tokens(text: str) -> set[str]:
    """title_zh/entity → {en 词, zh 二字组}（稀有 token 倒排用）。"""
    t = normalize.title_norm(text or "", lower=True)
    toks = set(_TOK_EN.findall(t))
    for run in _TOK_ZH.findall(t):
        if len(run) == 1:
            toks.add(run)
        else:
            toks.update(run[i:i + 2] for i in range(len(run) - 1))
    return toks


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

def _norm_judge_out(out) -> dict:
    """LLM 原始 JSON → {label:A|B|C|<非法>, confidence, reason}。"""
    if not isinstance(out, dict):
        return {"label": "PARSE", "confidence": 0.0, "reason": "non-dict reply"}
    label = str(out.get("verdict") or out.get("label") or "").strip().upper()
    try:
        conf = float(out.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if label not in ("A", "B", "C"):
        return {"label": "PARSE", "confidence": conf,
                "reason": str(out.get("reason") or "")[:80]}
    if conf < JUDGE_MIN_CONF:
        return {"label": "LOWCONF", "confidence": conf,
                "reason": str(out.get("reason") or "")[:80]}
    return {"label": label, "confidence": conf,
            "reason": str(out.get("reason") or "")[:80]}


def _hist_for_ctx(conn, ctx: dict) -> dict:
    """把命中 cluster 的成员拼成判词的"已报道"侧（cross_day / 批量共用）。"""
    members = conn.execute(
        "SELECT title, summary, source, day FROM dedup_items"
        " WHERE cluster_id=? ORDER BY day, item_id LIMIT 6",
        (ctx["cluster_id"],)).fetchall()
    seen, sums = set(), []
    for m in members:
        s = (m["summary"] or "").strip()
        if s and s not in seen:
            seen.add(s)
            sums.append(s[:160])
    return {
        "title": ctx["canonical_title"],
        "summary": " ／ ".join(sums)[:400],
        "date_published": members[-1]["day"] if members else "",
        "_source": {"name": members[0]["source"] or ""} if members else {},
    }


class Judge:
    """JUDGE_PROMPT + llm.chat_json 封装；cfg 缺失时 .ok=False → 全部走人工。"""

    def __init__(self, cfg: dict | None, provs: list | None = None):
        self.cfg = cfg
        self.provs = provs if provs is not None else []
        self.ok = cfg is not None
        self.calls = 0

    def pair(self, a: dict, b: dict) -> dict:
        """判一对 → _norm_judge_out；LLM 不可用/抛错 → {label:'ERR'}。"""
        if not self.ok:
            return {"label": "ERR", "confidence": 0.0, "reason": "judge disabled"}
        sys_p, user = prompts.JUDGE_PROMPT(a, b)
        try:
            out = llm.chat_json(prompts.messages(sys_p, user),
                                tag="judge", cfg=self.cfg, prov_out=self.provs)
            self.calls += 1
            r = _norm_judge_out(out)
            r["via"] = "judge_" + r["label"].lower()
            return r
        except llm.LLMError as e:
            return {"label": "ERR", "confidence": 0.0,
                    "reason": f"{type(e).__name__}: {e}"[:120]}

    def batch(self, pairs: list) -> list:
        """JUDGE_BATCH_PROMPT 一次判 K 对 → 每条 _norm_judge_out（缺对补 ERR）。"""
        err = {"label": "ERR", "confidence": 0.0, "reason": "judge disabled"}
        if not self.ok:
            return [dict(err) for _ in pairs]
        sys_p, user = prompts.JUDGE_BATCH_PROMPT(pairs)
        try:
            out = llm.chat_json(prompts.messages(sys_p, user),
                                tag="judge_batch", cfg=self.cfg,
                                prov_out=self.provs)
            self.calls += 1
        except llm.LLMError as e:
            err["reason"] = f"{type(e).__name__}: {e}"[:120]
            return [dict(err) for _ in pairs]
        arr = out if isinstance(out, list) else (
            out.get("pairs") or out.get("results") or out.get("verdicts")
            if isinstance(out, dict) else []) or []
        by_idx = {}
        for o in arr:
            if isinstance(o, dict):
                try:
                    by_idx[int(o.get("pair"))] = o
                except (TypeError, ValueError):
                    pass
        res = []
        for i in range(len(pairs)):
            r = _norm_judge_out(by_idx.get(i))
            r["via"] = "judge_batch"
            res.append(r)
        return res

    def cross_day(self, conn, item: dict, ctx: dict) -> dict:
        """store.check 的 judge_fn(item, match_ctx)。ctx={cluster_id,
        canonical_title, cos, published}；把命中 cluster 的成员拼成"已报道"侧。"""
        hist = _hist_for_ctx(conn, ctx)
        r = self.pair(item, hist)
        r["match"] = ctx["canonical_title"]
        return r


# ---------------------------------------------------------------------------
# 输入装配
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _resolve_run_dir(s: str) -> Path:
    """'YYYY-MM-DD' -> runs/<date>；否则按路径解析（相对 repo 根）。

    与 gate_select/compose/render_plan 同款约定；只解析不创建
    （run_lock/load_items 各自负责）。
    """
    if _DATE_RE.fullmatch(s or ""):
        return meta.RUNS_DIR / s
    p = Path(s)
    return p if p.is_absolute() else REPO / p


def _resolve_db(cli_db: str | None, cli_state: str | None = None) -> Path:
    """--db 文件 > --state 目录（其下 state.sqlite）> config.storage > state/。"""
    if cli_db:
        return Path(cli_db)
    if cli_state:
        p = Path(cli_state)
        # 兼容直接给文件路径；目录则取其下 state.sqlite
        if p.is_file() or p.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
            return p
        return p / "state.sqlite"
    return state.resolve_path(None, meta.load_config())


def _resolve_items_db(cli_arg: str | None) -> Path:
    """--items-db > config.storage.state_db > state/state.sqlite。"""
    return state.resolve_path(cli_arg, meta.load_config())


def load_items(run_dir: Path) -> list[dict]:
    """30_summaries + (可选)10_raw_items/20_filtered join → 处理用 item dict。"""
    sums = _load_jsonl(run_dir / "30_summaries.jsonl")
    if not sums:
        raise SystemExit(
            f"[dedup] 缺输入 {run_dir}/30_summaries.jsonl —— 先跑 `just filter`")
    raws = {r.get("item_key"): r for r in _load_jsonl(run_dir / "10_raw_items.jsonl")}
    filts = {f.get("item_key"): f for f in _load_jsonl(run_dir / "20_filtered.jsonl")}
    items = []
    for s in sums:
        k = s.get("item_key") or ""
        raw = raws.get(k) or {}
        url_canon = raw.get("url_canon") or normalize.url_canon(raw.get("url") or "")
        src = raw.get("_source")
        if not isinstance(src, dict):
            src = {}
        items.append({
            "item_key": k,
            "title_zh": s.get("title_zh") or raw.get("title") or "",
            "title": raw.get("title") or s.get("title_zh") or "",
            "summary": s.get("summary") or "",
            "entities": s.get("entities") or [],
            "url": raw.get("url") or "",
            "url_canon": url_canon,
            "source": src.get("name") or raw.get("source") or "",
            "lang": raw.get("language") or "",
            "news_value": (filts.get(k) or {}).get("news_value"),
            "filter_verdict": (filts.get(k) or {}).get("verdict"),
        })
    return items


def prepare_features(items: list[dict], emb: embedlib.Embedder,
                     day: str, p: prog.Prog | None = None) -> None:
    """就地补 simhash(fp_day) / doc 向量 / url_hash / day。

    注意：store 侧 simhash 用 store.simhash64（与历史库存值同一实现），
    同日对判用 lib/simhash.fingerprint_parts（同侧自比，实现自洽）。
    item 不传 "simhash"/"embed" 键给 store —— store 自动用自家实现补算，
    存库键走 "embed_doc" 自定义键，check 时再显式喂。
    query 向量只有跨天 check 的 rep 用得上 → run_pipeline 聚类后按 rep 补算
    （embed_q），全量 N→rep 数，省掉大头的 query 批。
    """
    texts = [store.feature_text(it) for it in items]
    if p:
        p.say(f"prepare_features: embedding {len(texts)} texts (doc)")
    E_doc = emb.embed(texts, mode="doc") if items else np.zeros((0, 1024), np.float32)
    if p:
        p.say("prepare_features: embed done")
    for i, it in enumerate(items):
        it["day"] = it.get("day") or day
        it["episode"] = it.get("episode") or it["day"]
        it["fp_day"] = simhash.fingerprint_parts(
            normalize.title_norm(it["title_zh"], lower=True), it["summary"])
        it["url_hash"] = store.url_hash(it["url_canon"]) if it["url_canon"] else ""
        it["embed_doc"] = E_doc[i]


# ---------------------------------------------------------------------------
# 1) 同日聚类：稀有 token 倒排 ∪ embed kNN → 候选对 → judge → union-find
# ---------------------------------------------------------------------------

def _candidate_pairs(items: list[dict], E: np.ndarray) -> dict[tuple, dict]:
    """{(i,j): {cos,tok,knn}} —— 稀有 token 共现 ∪ 每个 item doc-cos top-K 近邻。"""
    n = len(items)
    pairs: dict[tuple, dict] = {}

    def _add(i: int, j: int, flag: str, cos: float) -> None:
        p = (min(i, j), max(i, j))
        d = pairs.setdefault(p, {"cos": 0.0, "tok": False, "knn": False})
        d[flag] = True
        d["cos"] = max(d["cos"], cos)

    if n < 2:
        return pairs
    # 稀有 token 倒排
    rare_max = min(8, max(3, n // 3))
    inv: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        toks = set(_tokens(it["title_zh"]))
        for e in it.get("entities") or []:
            toks |= _tokens(str(e))
        for t in toks:
            inv.setdefault(t, []).append(i)
    S = E @ E.T
    for t, ids in inv.items():
        if 2 <= len(ids) <= rare_max:
            for a in range(len(ids)):
                for b in range(a + 1, len(ids)):
                    _add(ids[a], ids[b], "tok", float(S[ids[a], ids[b]]))
    # embed kNN top-K
    k = min(KNN_K, n - 1)
    for i in range(n):
        cnt = 0
        for j in np.argsort(-S[i]):
            j = int(j)
            if j == i:
                continue
            _add(i, j, "knn", float(S[i, j]))
            cnt += 1
            if cnt >= k:
                break
    return pairs


def _worth_judge(rec: dict) -> bool:
    """候选对值不值得花一次 LLM judge（召回层降本闸门，非判定阈值）。"""
    c = rec["cos"]
    if c >= JUDGE_COS_ANY:
        return True
    if rec["tok"] and c >= JUDGE_COS_TOK:
        return True
    return bool(rec["knn"]) and c >= JUDGE_COS_KNN


def same_day_cluster(items: list[dict], judge: Judge,
                     p: prog.Prog | None = None) -> tuple[list[list[int]], dict]:
    """返回 (组内下标列表的列表, pair_decisions{(i,j):record})。"""
    n = len(items)
    uf = _UF(n)
    decisions: dict[tuple, dict] = {}

    # 0) item_key 相同（url_canon 一致）→ 确定性并，不问 judge
    by_key: dict[str, int] = {}
    for i, it in enumerate(items):
        k = it.get("item_key")
        if not k:
            continue
        if k in by_key:
            uf.union(by_key[k], i)
            decisions[(by_key[k], i)] = {"via": "same_item_key"}
        else:
            by_key[k] = i

    E = np.stack([it["embed_doc"] for it in items]) if n else np.zeros((0, 1024))
    pairs = _candidate_pairs(items, E)
    # judge 相位进度：每对一次 LLM 调用（分钟级）。分母 = 预计判定数
    # （worth-judge 对数封顶 MAX_PAIR_JUDGE），step=1 保证每次判定都出一行。
    n_judge = min(sum(1 for r in pairs.values() if _worth_judge(r)),
                  MAX_PAIR_JUDGE)
    if p:
        p.retune(n_judge, step=1)
        p.say(f"same-day: {len(pairs)} candidate pairs, judge ≤{n_judge}"
              f" ×{JUDGE_WORKERS}并发")

    # judge.pair 并发：IO-bound LLM 调用按 cos 降序懒提交（在飞 ≤JUDGE_WORKERS），
    # 结果仍按提交序应用 uf.union —— 语义与串行版同构：apply 前 uf.find 剪枝
    # 继续生效，只是"在飞的对"等不到彼此的合并结果（最多浪费 workers-1 次
    # 调用，判重只会多查不会错并）。judge.pair 无共享状态（calls/provs 仅计数）。
    judged = 0
    applied = 0
    pending: list[tuple[int, int, dict, object]] = []  # (i,j,rec,Future)

    def _apply(i: int, j: int, rec: dict, r: dict) -> None:
        rec.update({"via": "same_day_judge", "label": r.get("label"),
                    "conf": r.get("confidence"), "reason": r.get("reason")})
        if r.get("label") in ("A", "B"):
            uf.union(i, j)
        rec["merged"] = uf.find(i) == uf.find(j)

    def _drain(block: bool = False) -> None:
        nonlocal applied
        while pending and (block or pending[0][3].done()):
            i, j, rec, fu = pending.pop(0)
            _apply(i, j, rec, fu.result())
            applied += 1
            if p:
                p.tick(applied, f"cos={rec['cos']:.3f} {rec.get('label')}")

    with ThreadPoolExecutor(max_workers=JUDGE_WORKERS,
                            thread_name_prefix="judge") as ex:
        for (i, j), rec in sorted(pairs.items(), key=lambda kv: -kv[1]["cos"]):
            _drain()                    # 先落已完成的判定，uf 保持最新再剪枝
            if uf.find(i) == uf.find(j):
                continue
            ham = simhash.hamming(items[i]["fp_day"], items[j]["fp_day"])
            rec["ham"] = ham
            if ham <= simhash.NEAR_DUP_HAMMING:
                rec["via"] = "same_day_ham"
                uf.union(i, j)
            elif judged < MAX_PAIR_JUDGE and _worth_judge(rec):
                judged += 1
                pending.append((i, j, rec,
                                ex.submit(judge.pair, items[i], items[j])))
            else:
                rec["via"] = ("skip_cap" if judged >= MAX_PAIR_JUDGE
                              else "skip_lo_cos")
            decisions[(i, j)] = rec
        _drain(block=True)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    def rep_key(g: list[int]) -> int:
        return sorted(g, key=lambda i: (-(items[i].get("news_value") or 0.0), i))[0]

    out = []
    for g in groups.values():
        rep = rep_key(g)
        out.append([rep] + [i for i in g if i != rep])
    out.sort(key=lambda g: g[0])
    return out, decisions


# ---------------------------------------------------------------------------
# 2)+3) 跨天级联 + sibling 并入 + 35 写出
# ---------------------------------------------------------------------------

def _fix_stored_embed(conn, item_id: int, cluster_id: int, doc_vec,
                      cmpset: "store.CmpSet | None" = None) -> None:
    """check() 吃的是 instruct query 向量；落库改成 doc 并重算 centroid。"""
    conn.execute("UPDATE dedup_items SET embed=? WHERE item_id=?",
                 (store._v2b(np.asarray(doc_vec, dtype=np.float32).tolist()), item_id))
    store._recompute_cluster(conn, cluster_id, cmpset=cmpset)


def _row(item_key: str, verdict: str, cluster_id, cos, judge) -> dict:
    row = {"schema": "dedup_verdict/1", "item_key": item_key,
           "verdict": verdict, "cluster_id": cluster_id,
           "match_cos": cos, "judge": judge}
    cm.DedupVerdict.model_validate(row)          # 契约 lint（extra=forbid）
    return row


def _batch_prejudge(conn, items: list[dict], groups: list, judge: Judge,
                    day: str, cmpset: "store.CmpSet", chunk: int,
                    log=print) -> dict:
    """相位A 只读扫描收灰区候选 -> 相位B JUDGE_BATCH 批量判（chunk 对/call，
    JUDGE_WORKERS 并发）。返回 {rep_i: {cluster_id, jraw, used}}。

    快照只决定"这对要不要批量判"：apply 相位仍逐 rep 走 check() 权威重扫，
    命中 cluster 与快照一致才用缓存判词，漂移/漏报回落单条 cross_day——
    写序与串行完全一致，judge 只是换了个供货渠道。
    """
    pend = []    # (rep_i, item, ctx)
    for g in groups:
        rep_i = g[0]
        rep = items[rep_i]
        chk = dict(rep)
        eq = rep.get("embed_q")        # ndarray：`or` 会触发真值歧义
        chk["embed"] = eq if eq is not None else rep["embed_doc"]
        uh = chk.get("url_hash") or (store.url_hash(chk["url_canon"])
                                     if chk.get("url_canon") else "")
        shv = chk.get("simhash")
        if shv is None:
            shv = store.simhash64(store.feature_text(chk))
        vec = store._unit(store._as_vec(chk["embed"]))
        s = store._scan(conn, uh, int(shv) & 0xFFFFFFFFFFFFFFFF, vec, day, cmpset)
        if s["kind"] != "best" or s["cos"] < store.T_GRAY:
            continue
        if (s["cos"] >= store.T_AUTO and s["ham"] is not None
                and s["ham"] <= store.SIMHASH_HI):
            continue                      # dup_same 双保险：check 不问 judge
        pend.append((rep_i, rep, {"cluster_id": s["cl"]["cluster_id"],
                                  "canonical_title": s["cl"]["canonical_title"],
                                  "cos": s["cos"],
                                  "published": s["cl"]["published"]}))
    if not pend:
        log("[dedup] 批量预判: 灰区 0 对，跳过")
        return {}
    log(f"[dedup] 批量预判: {len(pend)} 对灰区快照，{chunk} 对/call"
        f" ×{JUDGE_WORKERS}并发")

    # hist 成员查询在主线程做完（sqlite conn 不可跨线程）；worker 只跑 LLM。
    prepared = [(rep_i, it, ctx, _hist_for_ctx(conn, ctx))
                for rep_i, it, ctx in pend]
    chunks = [prepared[i:i + chunk] for i in range(0, len(prepared), chunk)]

    def work(ch):
        return judge.batch([(it, hist) for _, it, _, hist in ch])

    snaps = {}
    with ThreadPoolExecutor(max_workers=JUDGE_WORKERS) as ex:
        for ch, res in zip(chunks, ex.map(work, chunks)):
            for (rep_i, _it, ctx, _h), r in zip(ch, res):
                r["match"] = ctx["canonical_title"]
                snaps[rep_i] = {"cluster_id": ctx["cluster_id"],
                                "jraw": r, "used": False}
    return snaps


def run_pipeline(conn, items: list[dict], judge: Judge, day: str,
                 emb: embedlib.Embedder,
                 log=print, p: prog.Prog | None = None,
                 judge_batch: int = 0,
                 replay_uh: "set | None" = None) -> list[dict]:
    """同日聚类 → 跨天级联 → 35 行（输入序）。返回 rows。

    replay_uh：本 run 开始前 day 已落库的 url_hash 集（rerun 幂等——
    rep/sib 命中即重放行内 verdict，不判不写）。None=不重放。
    """
    # url_hash 前置：url 已在库的条目走 check() 的 dup_exact 短路（向量根本
    # 不会被读），query embed 只给未见过 url 的条目算。跨天 rep 大多来自
    # 昨日 carryover，~46% 直接省掉。
    uh_hist = {r[0] for r in conn.execute(
        "SELECT url_hash FROM dedup_items WHERE url_hash != ''")}
    q_idx = [i for i, it in enumerate(items)
             if not it.get("url_hash") or it["url_hash"] not in uh_hist]
    log(f"[dedup] url 前置: {len(items) - len(q_idx)}/{len(items)} 条 url 已在库"
        f"（query embed {len(q_idx)} 条）")

    # query(instruct) 向量只给跨天 check 用 —— 后台单线程与同日聚类的
    # judge 调用重叠（embed 是 CPU/ONNX，judge 是网络 IO，互不抢）。
    q_fut = None
    ex = ThreadPoolExecutor(max_workers=1)
    if q_idx:
        q_fut = ex.submit(emb.embed,
                          [store.feature_text(items[i]) for i in q_idx],
                          mode="query")
    try:
        groups, decisions = same_day_cluster(items, judge, p=p)
    finally:
        ex.shutdown(wait=False)
    n_multi = sum(1 for g in groups if len(g) > 1)
    log(f"[dedup] 同日聚类: {len(items)} 条 → {len(groups)} 组"
        f"（{n_multi} 组含合并，judge 调用 {judge.calls} 次）")

    if q_fut is not None:
        for i, q in zip(q_idx, q_fut.result()):
            items[i]["embed_q"] = q
    if p:
        # 跨天相位：分母换成 rep 组数，step 按总量节流（10..100，见 prog 约定）
        p.retune(len(groups), step=max(10, min(100, max(1, len(groups) // 40))))
        p.say(f"cross-day cascade: {len(groups)} reps")

    # 开放 cluster 比对集一次性载入内存（numpy 扫描 ~2ms/rep，替代原来
    # 每 rep 全表 + 逐簇 SELECT 的 0.15-1.5s）。写路径同步增量维护镜像。
    cmpset = store.CmpSet.load(conn, day)
    log(f"[dedup] 比对集: {cmpset.n} open clusters / {cmpset.mem_n} 成员")

    # --judge-batch：先对全体 rep 做只读扫描快照，灰区对批量判完再进入
    # 下面的逐 rep apply 循环（写序不变；缓存判词只对快照同一 cluster 生效）。
    snaps = {}
    stats = {"fb": 0}
    if judge_batch > 0:
        snaps = _batch_prejudge(conn, items, groups, judge, day, cmpset,
                                judge_batch, log=log)

    rows: dict[int, dict] = {}
    for gi, g in enumerate(groups, 1):
        rep_i = g[0]
        rep = items[rep_i]
        # 跨天：query(instruct) 向量进 check，匹配后把存库向量改回 doc。
        # url 已在库的 rep 没算 embed_q——doc 向量占位，check 会走
        # dup_exact 短路根本读不到它。
        chk_item = dict(rep)
        eq = rep.get("embed_q")
        chk_item["embed"] = eq if eq is not None else rep["embed_doc"]

        snap = snaps.get(rep_i)

        def _jfn(it, ctx, snap=snap):
            if snap is not None and ctx["cluster_id"] == snap["cluster_id"]:
                snap["used"] = True
                return snap["jraw"]
            stats["fb"] += 1
            return judge.cross_day(conn, it, ctx)   # 漂移/漏拍 -> 单条兜底

        r = store.check(conn, chk_item, judge_fn=_jfn,
                        today=day, cmpset=cmpset, replay_uh=replay_uh)
        if not r.get("replayed"):
            _fix_stored_embed(conn, r["item_id"], r["cluster_id"],
                              rep["embed_doc"], cmpset=cmpset)
        jdict = r.get("judge")
        if not isinstance(jdict, dict):
            jdict = {"via": r["via"],
                     **({"match": r["match"]} if r.get("match") else {}),
                     **({"ham": r["ham"]} if r.get("ham") is not None else {})}
        else:
            jdict = dict(jdict)
            jdict.setdefault("via", r["via"])
        rows[rep_i] = _row(rep["item_key"], r["verdict"], r["cluster_id"],
                           r.get("cos"), jdict)
        log(f"  rep  {rep['item_key'][:8]} {r['verdict']:10s} via={r['via']:9s}"
            f" cos={r.get('cos')} cid={r['cluster_id']}"
            f"{' [replay]' if r.get('replayed') else ''}"
            f" «{rep['title_zh'][:30]}»")

        # 同日 sibling：并入 rep 的 cluster，suppressed（可审计）
        for si in g[1:]:
            sib = items[si]
            pair = (min(rep_i, si), max(rep_i, si))
            d = decisions.get(pair) or {}
            cos = d.get("cos")
            if cos is None:
                cos = float(np.asarray(rep["embed_doc"], dtype=np.float64) @
                            np.asarray(sib["embed_doc"], dtype=np.float64))
            cos = round(float(cos), 4)
            # rerun 重放：sib 当日行已存在 → 直接重放行内 verdict/judge
            sib_uh = sib.get("url_hash")
            rep_row = (store.replay_row(conn, sib_uh, day)
                       if replay_uh and sib_uh and sib_uh in replay_uh
                       else None)
            if rep_row is not None:
                rj = rep_row["judge"] if isinstance(rep_row["judge"], dict) \
                    else {"via": rep_row["via"]}
                rows[si] = _row(sib["item_key"], rep_row["verdict"],
                                rep_row["cluster_id"], rep_row.get("cos"), rj)
                log(f"  sib  {sib['item_key'][:8]} {rep_row['verdict']:10s}"
                    f" via={rep_row['via']:9s} [replay]"
                    f" cid={rep_row['cluster_id']} «{sib['title_zh'][:30]}»")
                continue
            sib_item = dict(sib)
            sib_item["embed"] = sib["embed_doc"]      # doc 侧入库
            sib_judge = {"via": "same_day", "rep": rep["item_key"],
                         "label": d.get("label"), "ham": d.get("ham")}
            store.add_item(conn, sib_item, r["cluster_id"],
                           verdict="suppressed", judge=sib_judge,
                           match_cos=cos, via="same_day", cmpset=cmpset)
            store._recompute_cluster(conn, r["cluster_id"], cmpset=cmpset)
            rows[si] = _row(sib["item_key"], "suppressed", r["cluster_id"], cos,
                            sib_judge)
            log(f"  sib  {sib['item_key'][:8]} suppressed via=same_day "
                f"cos={cos} cid={r['cluster_id']} «{sib['title_zh'][:30]}»")
        if p:
            p.tick(gi, f"{r['verdict']} cid={r['cluster_id']}")
    if snaps:
        hit = sum(1 for s in snaps.values() if s["used"])
        log(f"[dedup] 批量判词命中 {hit}/{len(snaps)}"
            f"（漂移回落单条 judge {stats['fb']} 次）")
    return [rows[i] for i in range(len(items))]


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    run_dir = _resolve_run_dir(args.run_dir)
    episode = run_dir.name
    if not _DATE_RE.fullmatch(episode):
        # episode/day 由 run-dir 末级派生并写进 state.sqlite（expires_at
        # 比较/写库都依赖它是 ISO 日期）——非日期名必须在触碰 DB 前 fail-fast。
        print(f"[dedup] --run-dir 末级目录必须是期号 runs/<YYYY-MM-DD>"
              f"（得到 {episode!r}）；也可直接传期号：--run-dir <YYYY-MM-DD>",
              file=sys.stderr)
        return 2
    db = _resolve_db(args.db, args.state)

    items = load_items(run_dir)
    with meta.run_lock(run_dir):
        meta.stage_begin(run_dir)    # 锁内登记：RMW 串行化 + running==持锁语义
        conn = store.init_db(db)
        # rerun 幂等两层：--rerun 显式重算先 purge 当日痕迹；否则默认同日
        # 重放——replay_uh=run 开始前当日已落库 url_hash 集，命中的 rep/sib
        # 直接重放行内 verdict（零写零 LLM，35_dedup.jsonl 逐字节复现）。
        if args.rerun:
            st = store.purge_episode(conn, episode)
            replay_uh = None
            print(f"[dedup] --rerun: purge_episode({episode}) → "
                  f"删 {st['items']} 行，重算 {st['clusters_recomputed']} 簇，"
                  f"删空簇 {st['clusters_deleted']}")
        else:
            replay_uh = {r[0] for r in conn.execute(
                "SELECT url_hash FROM dedup_items WHERE day=? AND url_hash<>?",
                (episode, store._EMPTY_SHA1))}
        expired = store.expire_clusters(conn, today=episode)
        provs: list = []
        cfg = None
        if not args.no_judge:
            try:
                cfg = llm.load_cfg()
            except llm.LLMError as e:
                print(f"[dedup] WARN judge 不可用（{e}）→ 灰区全 gray_pending",
                      file=sys.stderr)
        judge = Judge(cfg, provs)
        emb = embedlib.Embedder()
        p = prog.Prog(run_dir, "dedup", total=len(items),
                      step=max(10, min(100, max(1, len(items) // 40))),
                      interval=30.0)
        prepare_features(items, emb, episode, p=p)
        print(f"[dedup] run_dir={run_dir} db={db} items={len(items)}"
              f" expired={expired} judge={'on' if judge.ok else 'OFF'}"
              f" replay={len(replay_uh) if replay_uh else 0}")
        rows = run_pipeline(conn, items, judge, episode, emb, p=p,
                            judge_batch=args.judge_batch,
                            replay_uh=replay_uh)
        p.close()
        conn.commit()   # 整期写相位单事务：expire/purge/级联/兄弟挂全部原子
        meta.atomic_write(run_dir / "35_dedup.jsonl", _dump_jsonl(rows))
        meta.stage_done(run_dir, "dedup", "35_dedup.jsonl", status="done",
                        extra={"n_items": len(items),
                               "n_suppressed": sum(1 for r in rows
                                                   if r["verdict"] == "suppressed"),
                               "n_gray": sum(1 for r in rows
                                             if r["verdict"] == "gray"),
                               "judge_calls": judge.calls})
        # 条目池投影：35_dedup.jsonl 是真源（file-first），池写失败只告警
        if _DATE_RE.fullmatch(run_dir.name):
            try:
                items_db = _resolve_items_db(args.items_db)
                pconn = pool.init_db(items_db)
                try:
                    with pconn:
                        n_pool = pool.write_dedup(pconn, rows)
                finally:
                    pconn.close()
                print(f"[dedup] pool: {n_pool} 行 dedup 列回写 → {items_db}")
            except Exception as e:
                print(f"[dedup] WARN state.sqlite 回写失败（不影响文件管线）: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
    vc = {}
    for r in rows:
        vc[r["verdict"]] = vc.get(r["verdict"], 0) + 1
    print(f"[dedup] done: {vc} → {run_dir}/35_dedup.jsonl")
    return 0


def cmd_split(args) -> int:
    conn = store.init_db(_resolve_db(args.db, args.state))
    ncid = store.split_cluster(conn, int(args.split))
    conn.commit()
    print(json.dumps({"op": "split", "cluster_id": int(args.split),
                      "new_cluster_id": ncid}, ensure_ascii=False))
    return 0 if ncid is not None else 1


def cmd_merge(args) -> int:
    conn = store.init_db(_resolve_db(args.db, args.state))
    a, b = int(args.merge[0]), int(args.merge[1])
    cid = store.merge_clusters(conn, a, b)
    conn.commit()
    print(json.dumps({"op": "merge", "surviving": cid, "merged": b},
                     ensure_ascii=False))
    return 0


def cmd_unexpire(args) -> int:
    """expire 的逆操作：误跑 expire（或非日期 today 污染）后恢复比对集。"""
    conn = store.init_db(_resolve_db(args.db, args.state))
    n = store.unexpire_clusters(conn, today=args.day)
    conn.commit()
    print(json.dumps({"op": "unexpire", "restored": n, "today":
                      args.day or date.today().isoformat()},
                     ensure_ascii=False))
    return 0


def _backfill_iter(path: Path, default_day: str):
    """raw_items/summary 混合 JSONL → (title, summary, url, day, source)。"""
    for ln in _load_jsonl(path):
        title = ln.get("title") or ln.get("title_zh") or ""
        summary = ln.get("summary") or (ln.get("content_text") or "")[:300]
        url = ln.get("url") or ln.get("url_canon") or ""
        day = default_day
        dp = ln.get("date_published") or ln.get("date") or ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(dp))
        if m:
            day = m.group(1)
        src = ln.get("_source") or {}
        yield {"title": title, "title_zh": title, "summary": summary,
               "url": url, "url_canon": ln.get("url_canon") or
               normalize.url_canon(url), "day": day,
               "source": src.get("name") if isinstance(src, dict) else str(src)}


def cmd_backfill(args) -> int:
    """冷启动：把往期条目逐条建成 verdict=reported 的单条 cluster（published=1）。

    幂等：url_hash 已在库则跳过。每条一 cluster —— 比对集只需召回能力，
    跨天匹配本来取 best-match，碎片同题 cluster 无害。
    """
    src = Path(args.backfill)
    if not src.exists():
        raise SystemExit(f"[dedup] backfill 文件不存在: {src}")
    default_day = args.day or date.today().isoformat()
    store._check_day(default_day)          # --day 非法值在 embed 之前 fail-fast
    conn = store.init_db(_resolve_db(args.db, args.state))
    emb = embedlib.Embedder()
    items = list(_backfill_iter(src, default_day))
    texts = [store.feature_text(it) for it in items]
    E = emb.embed(texts, mode="doc") if items else np.zeros((0, 1024))
    n_add = n_skip = 0
    p = prog.Prog(None, "dedup", total=len(items),
                  step=max(10, min(100, max(1, len(items) // 40))),
                  interval=30.0)   # backfill 无 run_dir → 仅 stderr 进度
    for idx, (it, vec) in enumerate(zip(items, E), 1):
        p.tick(idx, (it["title"] or "")[:28])
        uh = store.url_hash(it["url_canon"]) if it["url_canon"] else ""
        if uh and conn.execute("SELECT 1 FROM dedup_items WHERE url_hash=? LIMIT 1",
                               (uh,)).fetchone():
            n_skip += 1
            continue
        it["embed"] = vec
        cid = store.add_cluster(conn, it["title"], it["day"], vec)
        store.add_item(conn, it, cid, verdict="reported", via="backfill")
        store._recompute_cluster(conn, cid)   # → published=1 + doc centroid
        n_add += 1
    p.close()
    conn.commit()
    print(f"[dedup] backfill {src}: +{n_add} clusters, skip {n_skip}（已存在）")
    return 0


# ---------------------------------------------------------------------------
# selftest：合成 ~18 条 fixture 走真管线（真 embedder + live judge）
# ---------------------------------------------------------------------------

def _selftest(args) -> int:
    work = REPO / "out" / "dedup_selftest"
    work.mkdir(parents=True, exist_ok=True)
    db = work / "state.sqlite"
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        p.unlink(missing_ok=True)

    day0 = (date.today() - timedelta(days=1)).isoformat()   # 历史日
    day1 = date.today().isoformat()                          # 今日

    # ---- 种子历史（昨日已报道）----
    seeds = [
        {"title": "OpenAI 发布 GPT-6 旗舰模型，上下文达 100 万 token",
         "url": "https://openai.com/blog/gpt-6",
         "summary": "OpenAI 官宣新一代旗舰模型 GPT-6，支持 100 万 token 上下文，推理能力大幅提升。"},
        {"title": "英伟达发布 Rubin 架构 GPU，推理性能翻倍",
         "url": "https://nvidia.com/news/rubin-gpu",
         "summary": "英伟达发布下一代 Rubin GPU，推理性能较 Blackwell 翻倍。"},
        {"title": "谷歌 Gemini 3 上线多模态推理",
         "url": "https://blog.google/gemini-3",
         "summary": "谷歌 Gemini 3 正式上线，支持原生多模态推理。"},
        {"title": "欧盟 AI 法案实施细则正式公布",
         "url": "https://europa.eu/ai-act-rules",
         "summary": "欧盟委员会公布 AI 法案实施细则，明确通用模型合规要求。"},
        {"title": "Meta 开源 Llama 5 系列模型",
         "url": "https://ai.meta.com/blog/llama-5",
         "summary": "Meta 开源 Llama 5 全家桶，含 8B/70B/400B 三档。"},
    ]
    conn = store.init_db(db)
    emb = embedlib.Embedder()
    for s in seeds:
        s["title_zh"] = s["title"]
        s["day"] = day0
        s["url_canon"] = normalize.url_canon(s["url"])
        s["embed"] = emb.embed([store.feature_text(s)], mode="doc")[0]
        cid = store.add_cluster(conn, s["title"], day0, s["embed"])
        store.add_item(conn, s, cid, verdict="reported")
        store._recompute_cluster(conn, cid)
    print(f"[selftest] seeded {len(seeds)} reported clusters (day={day0})")

    # ---- 今日 fixture（summary 形状 + raw 字段）----
    def mk(tz, url, summ, ents=None, nv=0.5):
        return {"item_key": normalize.item_key(normalize.url_canon(url)),
                "title_zh": tz, "title": tz, "summary": summ,
                "entities": ents or [], "url": url,
                "url_canon": normalize.url_canon(url), "source": "selftest",
                "lang": "zh", "news_value": nv, "filter_verdict": "keep"}

    gpt6_sum = "OpenAI 官宣新一代旗舰模型 GPT-6，支持 100 万 token 上下文。"
    items = [
        # 2 exact-dup urls：canonical 与 seed S1 相同 → dup_exact（确定性）
        mk("OpenAI 发布 GPT-6 旗舰模型", "https://openai.com/blog/gpt-6?utm_source=rss",
           gpt6_sum, ["OpenAI", "GPT-6"], 0.9),
        mk("GPT-6 正式发布", "https://openai.com/blog/gpt-6?fbclid=zz",
           gpt6_sum, ["GPT-6"], 0.4),
        # 2 near-dup：同一事件不同措辞（互相同日近义 + 对 S1 近义）
        mk("OpenAI 正式推出 GPT-6，百万 token 上下文", "https://news-a.com/gpt6-launch",
           "OpenAI 今日正式推出 GPT-6 旗舰模型，上下文窗口达 100 万 token。", ["OpenAI"]),
        mk("GPT-6 旗舰模型上线：支持百万上下文", "https://news-b.com/gpt6-news",
           "OpenAI 今日正式推出 GPT-6 旗舰模型，上下文窗口达 100 万 token。", ["GPT-6"]),
        # 1 newdev：同故事线明确新进展 → 期望 reissue（gray 可接受）；
        # nv=1.0 兜底——即便与发布组同日并组，它也是 rep，仍走跨天判定
        mk("GPT-6 上线 48 小时：OpenAI 披露首日 API 调用量破百亿并紧急扩容",
           "https://news-c.com/gpt6-first48h",
           "OpenAI 公布 GPT-6 发布 48 小时数据：首日 API 调用量破百亿次，"
           "因流量超预期紧急扩容限流。", ["OpenAI", "GPT-6"], 1.0),
        # fresh ×13（与种子主题明显错开）
        mk("Anthropic 发布 Claude 6，编码能力大幅升级", "https://anthropic.com/claude-6",
           "Anthropic 发布 Claude 6，主打 agentic 编码与长任务。", ["Anthropic"]),
        mk("xAI 完成 100 亿美元新一轮融资", "https://x.ai/fund",
           "xAI 宣布完成百亿美元融资，估值翻倍。", ["xAI"]),
        mk("苹果发布 M6 芯片，本地 AI 算力提升 3 倍", "https://apple.com/m6",
           "苹果 M6 芯片发布，神经引擎算力提升 3 倍。", ["Apple"]),
        mk("DeepSeek 推出 V4 模型，推理成本再降", "https://deepseek.com/v4",
           "DeepSeek 发布 V4，推理成本较 V3 降 60%。", ["DeepSeek"]),
        mk("微软 Copilot 新增 Agent 模式", "https://microsoft.com/copilot-agent",
           "微软为 Copilot 增加 Agent 模式，可自动操作 Office。", ["Microsoft"]),
        mk("斯坦福开源大规模机器人操作数据集", "https://stanford.edu/dataset",
           "斯坦福开源 10 万小时机器人操作数据集。", []),
        mk("AWS 发布 Trainium3 训练芯片", "https://aws.amazon.com/trainium3",
           "AWS 发布 Trainium3，训练性价比提升 40%。", ["AWS"]),
        mk("Figure 03 人形机器人宣布量产", "https://figure.ai/figure-03",
           "Figure 宣布 03 型人形机器人进入量产阶段。", ["Figure"]),
        mk("Mistral 发布 Codestral 2 代码模型", "https://mistral.ai/codestral-2",
           "Mistral 推出 Codestral 2，补全速度提升一倍。", ["Mistral"]),
        mk("联合国 AI 治理峰会在日内瓦开幕", "https://un.org/ai-summit",
           "联合国 AI 治理峰会开幕，聚焦前沿模型监管。", []),
        # 注：上条与种子 S4（欧盟 AI 法案）落在 gray 带（cos≈0.59）——
        # 判对开时 judge 裁 C→fresh；--no-judge 下保留 gray 属预期（见断言）
        mk("Runway 发布 Gen-5 视频生成模型", "https://runwayml.com/gen-5",
           "Runway Gen-5 发布，支持分钟级一致性视频。", ["Runway"]),
        mk("HuggingFace 推出推理集群服务", "https://huggingface.co/clusters",
           "HuggingFace 上线 Inference Clusters，主打专用算力。", ["HuggingFace"]),
        mk("台积电 2nm 制程量产进度提前", "https://tsmc.com/2nm",
           "台积电宣布 2nm 制程量产提前至下季度。", ["TSMC"]),
    ]
    p = prog.Prog(None, "dedup")   # selftest 无 run_dir：仅 stderr，顺带演练仪表
    prepare_features(items, emb, day1, p=p)

    provs: list = []
    try:
        cfg = None if args.no_judge else llm.load_cfg()
    except llm.LLMError as e:
        print(f"[selftest] WARN judge 不可用: {e}")
        cfg = None
    judge = Judge(cfg, provs)
    print(f"[selftest] items={len(items)} judge={'on' if judge.ok else 'OFF'}")

    rows = run_pipeline(conn, items, judge, day1, emb, p=p,
                        judge_batch=getattr(args, "judge_batch", 0))
    p.close()

    print("\n== cascade verdicts ==")
    for it, r in zip(items, rows):
        j = r.get("judge") or {}
        print(f"{r['verdict']:10s} cid={str(r['cluster_id']):>4s} "
              f"cos={r.get('match_cos')} via={j.get('via')} label={j.get('label')}"
              f"  {it['title_zh'][:38]}")

    # ---- asserts ----
    v = [r["verdict"] for r in rows]
    cids = [r["cluster_id"] for r in rows]
    assert v[0] == "suppressed" and v[1] == "suppressed", \
        f"exact-dup urls 必须 suppressed: {v[0]},{v[1]}"
    assert cids[0] == cids[1], "exact-dup pair 应同 cluster"
    assert v[2] in ("suppressed", "gray") and v[3] in ("suppressed", "gray"), \
        f"near-dup 应 suppressed/gray: {v[2]},{v[3]}"
    assert cids[2] == cids[3], f"near-dup pair 应同日合并: {cids[2]} vs {cids[3]}"
    assert v[4] in ("reissue", "gray"), f"newdev 应 reissue/gray: {v[4]}"
    fresh_v = v[5:]
    if judge.ok:
        assert all(x == "fresh" for x in fresh_v), f"fresh 组出现非 fresh: {fresh_v}"
    else:
        # --no-judge：gray 带条目保留 gray 属预期（判对缺席无人能裁 C→fresh），
        # 但 suppressed/reissue 仍只能是确定性路径产出——出现即真 bug
        bad = [x for x in fresh_v if x not in ("fresh", "gray")]
        assert not bad, f"fresh 组出现意外 verdict: {fresh_v}"
    assert len(set(cids[5:])) == len(cids[5:]), "fresh 应各自新 cluster"
    n_clusters = conn.execute("SELECT COUNT(*) FROM dedup_clusters").fetchone()[0]
    n_items = conn.execute("SELECT COUNT(*) FROM dedup_items").fetchone()[0]
    print(f"\n[selftest] OK — verdicts={ {x: v.count(x) for x in set(v)} }, "
          f"db: {n_clusters} clusters/{n_items} items, judge_calls={judge.calls}")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="story-line dedup (PLAN §7.2)")
    ap.add_argument("--run-dir", help="runs/<date>")
    ap.add_argument("--db", help="state.sqlite 路径（默认 config.storage.state_db 或 state/）")
    ap.add_argument("--state",
                    help="跨天 state 目录（取 <dir>/state.sqlite；与 --db 互斥）")
    ap.add_argument("--items-db",
                    help="state.sqlite 路径（默认 config.storage.state_db 或 state/state.sqlite）")
    ap.add_argument("--no-judge", action="store_true",
                    help="禁用 LLM judge：灰区全落 gray_pending（离线/调试）")
    ap.add_argument("--rerun", action="store_true",
                    help="显式重算本期：先 purge 当日判重痕迹再全量重跑"
                    "（默认 rerun 走同日重放，零写零 judge 逐字节复现）")
    ap.add_argument("--judge-batch", type=int, default=0, metavar="K",
                    help="跨天灰区批量预判：先只读扫描出待判对，K 对/call 并发"
                    "判完再逐 rep apply（写序不变、漂移回落单条 judge）；"
                    "0=逐条串行（默认）")
    ap.add_argument("--split", metavar="CLUSTER_ID",
                    help="人工算子：拆出 cluster 最近挂入批为新 cluster")
    ap.add_argument("--merge", nargs=2, metavar=("A", "B"),
                    help="人工算子：把 B 并入 A（B 留痕 merged）")
    ap.add_argument("--unexpire", action="store_true",
                    help="人工算子：恢复误置 expired 且仍在 TTL 内的 cluster")
    ap.add_argument("--backfill", metavar="RAW_ITEMS.jsonl",
                    help="冷启动回填：往期条目作为 reported 种子进比对集")
    ap.add_argument("--day", help="backfill 兜底 day / --unexpire 比对日 (YYYY-MM-DD)")
    ap.add_argument("--selftest", action="store_true",
                    help="合成 fixture 端到端测试（真 embedder + live judge）")
    args = ap.parse_args(argv)

    if args.db and args.state:
        ap.error("--db 与 --state 互斥（前者给文件，后者给目录）")
    if args.selftest:
        return _selftest(args)
    if args.split:
        return cmd_split(args)
    if args.merge:
        return cmd_merge(args)
    if args.unexpire:
        return cmd_unexpire(args)
    if args.backfill:
        return cmd_backfill(args)
    if not args.run_dir:
        ap.error("--run-dir / --split / --merge / --unexpire / --backfill / --selftest 必选一")
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
