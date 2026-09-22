# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy>=1.26",
#   "onnxruntime>=1.17",
#   "tokenizers>=0.19",
#   "httpx>=0.27",
#   "pyyaml>=6",
#   "pydantic>=2",
# ]
# ///
"""dedup — story-line 去重（PLAN §7.2）。

输入：runs/<date>/30_summaries.jsonl（必需，缺则 fail-fast 提示先跑 just filter）
      runs/<date>/10_raw_items.jsonl（可选 join：url/url_canon/_source）
      runs/<date>/20_filtered.jsonl（可选 join：news_value 选同日代表）
      state/history.sqlite（跨天比对集，真源）

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
  - 入库 items.embed / centroid 一律 doc 向量（schema 注释"doc 侧无 instruct"）；
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

  --db P   直接给 history.sqlite 文件路径；
  --state D 给跨天 state 目录（取 <D>/history.sqlite），与 --db 互斥。
  --run-dir 的末级目录必须是期号 YYYY-MM-DD（episode/day 由它派生，
  非日期名会 fail-fast exit 2，且不触碰 history.sqlite）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import numpy as np

from lib import meta, normalize, prompts, simhash, store
from lib import embed as embedlib
from adapters import llm_swe2max as llm
from contracts import models as cm

REPO = Path(__file__).resolve().parents[1]

# ---- 同日聚类参数（召回层自定，判定阈值仍走 store 校准级联） ----
KNN_K = 5                    # embed kNN 每 item 召回近邻数
JUDGE_COS_TOK = 0.55         # 共享稀有 token 的对：cos≥此才问 judge
JUDGE_COS_KNN = 0.62         # 仅 kNN 召回（无 token 重叠）的对：更高门槛
JUDGE_COS_ANY = 0.80         # 任何召回路径：cos≥此必问 judge
JUDGE_MIN_CONF = 0.6         # judge 置信下限，低于 → 不并/gray_pending
MAX_PAIR_JUDGE = 60          # 同日 judge 调用上限（按 cos 降序截断）

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

    def cross_day(self, conn, item: dict, ctx: dict) -> dict:
        """store.check 的 judge_fn(item, match_ctx)。ctx={cluster_id,
        canonical_title, cos, published}；把命中 cluster 的成员拼成"已报道"侧。"""
        members = conn.execute(
            "SELECT title, summary, source, day FROM items"
            " WHERE cluster_id=? ORDER BY day, item_id LIMIT 6",
            (ctx["cluster_id"],)).fetchall()
        seen, sums = set(), []
        for m in members:
            s = (m["summary"] or "").strip()
            if s and s not in seen:
                seen.add(s)
                sums.append(s[:160])
        hist = {
            "title": ctx["canonical_title"],
            "summary": " ／ ".join(sums)[:400],
            "date_published": members[-1]["day"] if members else "",
            "_source": {"name": members[0]["source"] or ""} if members else {},
        }
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
    """--db 文件 > --state 目录（其下 history.sqlite）> config.storage > state/。"""
    if cli_db:
        return Path(cli_db)
    if cli_state:
        p = Path(cli_state)
        # 兼容直接给文件路径；目录则取其下 history.sqlite
        if p.is_file() or p.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
            return p
        return p / "history.sqlite"
    for name in ("config.yaml", "config.example.yaml"):
        p = REPO / name
        if p.exists():
            try:
                import yaml
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                hp = (doc.get("storage") or {}).get("history_db")
                if hp:
                    q = Path(hp)
                    return q if q.is_absolute() else REPO / q
            except Exception:
                pass
            break
    return REPO / "state" / "history.sqlite"


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
                     day: str) -> None:
    """就地补 simhash(fp_day) / doc+query 向量 / url_hash / day。

    注意：store 侧 simhash 用 store.simhash64（与历史库存值同一实现），
    同日对判用 lib/simhash.fingerprint_parts（同侧自比，实现自洽）。
    item 不传 "simhash"/"embed" 键给 store —— store 自动用自家实现补算，
    存库键走 "embed_doc"/"embed_q" 自定义键，check 时再显式喂。
    """
    texts = [store.feature_text(it) for it in items]
    E_doc = emb.embed(texts, mode="doc") if items else np.zeros((0, 1024), np.float32)
    E_q = emb.embed(texts, mode="query") if items else np.zeros((0, 1024), np.float32)
    for i, it in enumerate(items):
        it["day"] = it.get("day") or day
        it["episode"] = it.get("episode") or it["day"]
        it["fp_day"] = simhash.fingerprint_parts(
            normalize.title_norm(it["title_zh"], lower=True), it["summary"])
        it["url_hash"] = store.url_hash(it["url_canon"]) if it["url_canon"] else ""
        it["embed_doc"] = E_doc[i]
        it["embed_q"] = E_q[i]


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


def same_day_cluster(items: list[dict], judge: Judge) -> tuple[list[list[int]], dict]:
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
    judged = 0
    for (i, j), rec in sorted(pairs.items(), key=lambda kv: -kv[1]["cos"]):
        if uf.find(i) == uf.find(j):
            continue
        cos = rec["cos"]
        ham = simhash.hamming(items[i]["fp_day"], items[j]["fp_day"])
        rec["ham"] = ham
        if ham <= simhash.NEAR_DUP_HAMMING:
            rec["via"] = "same_day_ham"
            uf.union(i, j)
        elif judged < MAX_PAIR_JUDGE and _worth_judge(rec):
            judged += 1
            r = judge.pair(items[i], items[j])
            rec.update({"via": "same_day_judge", "label": r.get("label"),
                        "conf": r.get("confidence"), "reason": r.get("reason")})
            if r.get("label") in ("A", "B"):
                uf.union(i, j)
            rec["merged"] = uf.find(i) == uf.find(j)
        else:
            rec["via"] = "skip_cap" if judged >= MAX_PAIR_JUDGE else "skip_lo_cos"
        decisions[(i, j)] = rec

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

def _fix_stored_embed(conn, item_id: int, cluster_id: int, doc_vec) -> None:
    """check() 吃的是 instruct query 向量；落库改成 doc 并重算 centroid。"""
    conn.execute("UPDATE items SET embed=? WHERE item_id=?",
                 (store._v2b(np.asarray(doc_vec, dtype=np.float32).tolist()), item_id))
    conn.commit()
    store._recompute_cluster(conn, cluster_id)


def _row(item_key: str, verdict: str, cluster_id, cos, judge) -> dict:
    row = {"schema": "dedup_verdict/1", "item_key": item_key,
           "verdict": verdict, "cluster_id": cluster_id,
           "match_cos": cos, "judge": judge}
    cm.DedupVerdict.model_validate(row)          # 契约 lint（extra=forbid）
    return row


def run_pipeline(conn, items: list[dict], judge: Judge, day: str,
                 log=print) -> list[dict]:
    """同日聚类 → 跨天级联 → 35 行（输入序）。返回 rows。"""
    groups, decisions = same_day_cluster(items, judge)
    n_multi = sum(1 for g in groups if len(g) > 1)
    log(f"[dedup] 同日聚类: {len(items)} 条 → {len(groups)} 组"
        f"（{n_multi} 组含合并，judge 调用 {judge.calls} 次）")

    rows: dict[int, dict] = {}
    for g in groups:
        rep_i = g[0]
        rep = items[rep_i]
        # 跨天：query(instruct) 向量进 check，匹配后把存库向量改回 doc
        chk_item = dict(rep)
        chk_item["embed"] = rep["embed_q"]
        r = store.check(conn, chk_item, judge_fn=lambda it, ctx: judge.cross_day(conn, it, ctx),
                        today=day)
        _fix_stored_embed(conn, r["item_id"], r["cluster_id"], rep["embed_doc"])
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
            f" cos={r.get('cos')} cid={r['cluster_id']} «{rep['title_zh'][:30]}»")

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
            sib_item = dict(sib)
            sib_item["embed"] = sib["embed_doc"]      # doc 侧入库
            store.add_item(conn, sib_item, r["cluster_id"],
                                 verdict="suppressed",
                                 judge={"via": "same_day", "rep": rep["item_key"],
                                        "label": d.get("label"),
                                        "cos": cos, "ham": d.get("ham")},
                                 match_cos=cos)
            store._recompute_cluster(conn, r["cluster_id"])
            rows[si] = _row(sib["item_key"], "suppressed", r["cluster_id"], cos,
                            {"via": "same_day", "rep": rep["item_key"],
                             "label": d.get("label"), "ham": d.get("ham")})
            log(f"  sib  {sib['item_key'][:8]} suppressed via=same_day "
                f"cos={cos} cid={r['cluster_id']} «{sib['title_zh'][:30]}»")
    return [rows[i] for i in range(len(items))]


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
    run_dir = _resolve_run_dir(args.run_dir)
    episode = run_dir.name
    if not _DATE_RE.fullmatch(episode):
        # episode/day 由 run-dir 末级派生并写进 history.sqlite（expires_at
        # 比较/写库都依赖它是 ISO 日期）——非日期名必须在触碰 DB 前 fail-fast。
        print(f"[dedup] --run-dir 末级目录必须是期号 runs/<YYYY-MM-DD>"
              f"（得到 {episode!r}）；也可直接传期号：--run-dir <YYYY-MM-DD>",
              file=sys.stderr)
        return 2
    db = _resolve_db(args.db, args.state)

    items = load_items(run_dir)
    with meta.run_lock(run_dir):
        conn = store.init_db(db)
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
        prepare_features(items, emb, episode)
        print(f"[dedup] run_dir={run_dir} db={db} items={len(items)}"
              f" expired={expired} judge={'on' if judge.ok else 'OFF'}")
        rows = run_pipeline(conn, items, judge, episode)
        meta.atomic_write(run_dir / "35_dedup.jsonl", _dump_jsonl(rows))
        meta.stage_done(run_dir, "dedup", "35_dedup.jsonl", status="done",
                        extra={"n_items": len(items),
                               "n_suppressed": sum(1 for r in rows
                                                   if r["verdict"] == "suppressed"),
                               "n_gray": sum(1 for r in rows
                                             if r["verdict"] == "gray"),
                               "judge_calls": judge.calls})
    vc = {}
    for r in rows:
        vc[r["verdict"]] = vc.get(r["verdict"], 0) + 1
    print(f"[dedup] done: {vc} → {run_dir}/35_dedup.jsonl")
    return 0


def cmd_split(args) -> int:
    conn = store.init_db(_resolve_db(args.db, args.state))
    ncid = store.split_cluster(conn, int(args.split))
    print(json.dumps({"op": "split", "cluster_id": int(args.split),
                      "new_cluster_id": ncid}, ensure_ascii=False))
    return 0 if ncid is not None else 1


def cmd_merge(args) -> int:
    conn = store.init_db(_resolve_db(args.db, args.state))
    a, b = int(args.merge[0]), int(args.merge[1])
    cid = store.merge_clusters(conn, a, b)
    print(json.dumps({"op": "merge", "surviving": cid, "merged": b},
                     ensure_ascii=False))
    return 0


def cmd_unexpire(args) -> int:
    """expire 的逆操作：误跑 expire（或非日期 today 污染）后恢复比对集。"""
    conn = store.init_db(_resolve_db(args.db, args.state))
    n = store.unexpire_clusters(conn, today=args.day)
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
    for it, vec in zip(items, E):
        uh = store.url_hash(it["url_canon"]) if it["url_canon"] else ""
        if uh and conn.execute("SELECT 1 FROM items WHERE url_hash=? LIMIT 1",
                               (uh,)).fetchone():
            n_skip += 1
            continue
        it["embed"] = vec
        cid = store.add_cluster(conn, it["title"], it["day"], vec)
        store.add_item(conn, it, cid, verdict="reported")
        store._recompute_cluster(conn, cid)   # → published=1 + doc centroid
        n_add += 1
    conn.commit()
    print(f"[dedup] backfill {src}: +{n_add} clusters, skip {n_skip}（已存在）")
    return 0


# ---------------------------------------------------------------------------
# selftest：合成 ~18 条 fixture 走真管线（真 embedder + live judge）
# ---------------------------------------------------------------------------

def _selftest(args) -> int:
    work = REPO / "out" / "dedup_selftest"
    work.mkdir(parents=True, exist_ok=True)
    db = work / "history.sqlite"
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
        mk("Runway 发布 Gen-5 视频生成模型", "https://runwayml.com/gen-5",
           "Runway Gen-5 发布，支持分钟级一致性视频。", ["Runway"]),
        mk("HuggingFace 推出推理集群服务", "https://huggingface.co/clusters",
           "HuggingFace 上线 Inference Clusters，主打专用算力。", ["HuggingFace"]),
        mk("台积电 2nm 制程量产进度提前", "https://tsmc.com/2nm",
           "台积电宣布 2nm 制程量产提前至下季度。", ["TSMC"]),
    ]
    prepare_features(items, emb, day1)

    provs: list = []
    try:
        cfg = None if args.no_judge else llm.load_cfg()
    except llm.LLMError as e:
        print(f"[selftest] WARN judge 不可用: {e}")
        cfg = None
    judge = Judge(cfg, provs)
    print(f"[selftest] items={len(items)} judge={'on' if judge.ok else 'OFF'}")

    rows = run_pipeline(conn, items, judge, day1)

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
    assert all(x == "fresh" for x in fresh_v), f"fresh 组出现非 fresh: {fresh_v}"
    assert len(set(cids[5:])) == len(cids[5:]), "fresh 应各自新 cluster"
    n_clusters = conn.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
    n_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"\n[selftest] OK — verdicts={ {x: v.count(x) for x in set(v)} }, "
          f"db: {n_clusters} clusters/{n_items} items, judge_calls={judge.calls}")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="story-line dedup (PLAN §7.2)")
    ap.add_argument("--run-dir", help="runs/<date>")
    ap.add_argument("--db", help="history.sqlite 路径（默认 config.storage 或 state/）")
    ap.add_argument("--state",
                    help="跨天 state 目录（取 <dir>/history.sqlite；与 --db 互斥）")
    ap.add_argument("--no-judge", action="store_true",
                    help="禁用 LLM judge：灰区全落 gray_pending（离线/调试）")
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
