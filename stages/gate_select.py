"""gate_select — 人工闸 1（docs/PLAN.md §7.3）。

`40_candidates.json`（UI 数据源，非契约）的五类来源——前四个 run 文件
缺一即 fail-fast（文件即依赖边，提示按 DAG 序给），第五个缺席/半空只
WARN 退回纯文件路径：

  20_filtered.jsonl   filter verdict + ai_relevance/news_value/reasons
  30_summaries.jsonl  title_zh/summary/entities/section_guess（文案）
  35_dedup.jsonl      dedup verdict/cluster_id/match_cos/judge
  10_raw_items.jsonl  url/源名/date_published/url_canon
  state/items.sqlite  条目池：结转候选源 + used/eligible/projected-dedup
                      谓词数据（--items-db 可改路径）

人工/自动勾选 → `40_selected.json`（契约 selected/1）。

候选集 = filter verdict∈{keep,review} 且 dedup verdict∉{suppressed}
（dedup 的 gray/gray_pending 自动进列表并打灰区标记）。

POOL-MODE（run_dir 名为 YYYY-MM-DD 且 state/items.sqlite 已有本期 item_runs）：
候选集 = 当期文件路径 ∪ 条目池结转（lib.pool.select_candidates）∪ L0 丢行
兜底（当期被 l0-url-hash 占位 drop、但池行 standing verdict∈{keep,review}
且 eligible 者 → carried=True，feed 重发不丢候选格）。并集统一过
used-check / eligible 窗口 / projected-dedup（叠加 history.sqlite 已出片
cluster 投影）谓词；出局按 skipped_used / skipped_window / suppressed
（文件侧 ∪ 池投影审计列）归账，filter 判 drop 仅计 n_dropped_by_filter，
n_stale_floor 亦仅计数（pub<下限的结转根本没进候选评估）。结转条目的
raw/summary 每次 build 都重新物化到 38_pool_items.jsonl /
38_pool_summaries.jsonl（stale-safe）。40_selected 落盘后 mark_used
回写池的 used_in_episode。池缺席/无本期 → 退回纯文件路径
（响亮 WARN，绝不静默半空）。

用法（六路入口；--selftest 之外 --run-dir 必填，给日期或路径均可）：
  gate_select.py --run-dir R                # 缺省 = --prepare：重建 40_candidates.json
  gate_select.py --run-dir R --prepare      # 同上，显式
  gate_select.py --run-dir R --serve        # prepare + 启动 review_server 勾选 UI
  gate_select.py --run-dir R --auto [--force] [--topk N]
        # top-K by news_value → decided_by:auto；K = --topk（覆盖
        # config.schedule.topk_autopick）再被 max_items 截顶
  gate_select.py --run-dir R --deadline-check HH:MM
        # 死线已过且未提交 → 自动放行（供 timer 调用）
  gate_select.py --selftest                 # fixture 端到端自测（含 schema 断言）
  公共 flag：--items-db P = 条目池改走 P（> config.storage.items_db；
        --serve 会转发给 review_server）

不覆盖原则：40_selected.json 已存在时 --auto/--deadline-check 直接跳过（人工已拍板），
--force 可强制重判。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from stages.lib import meta, pool, prog, rawitem, selkit  # stages/lib/*（stages.lib 包）
from stages.lib.selkit import (  # 共用件下沉 lib；bare 名调用点保持不变
    CAND_NAME, DATE_RE, KEY_RE, SEL_NAME, SLUG_RE, TZ, eprint, lint_selected,
    load_config, mark_used, now_iso, pydantic_validate, resolve_run_dir,
    section_slug, slugify_id, unique_slug)

REPO = Path(__file__).resolve().parents[1]

RAW_NAME = "10_raw_items.jsonl"
FILT_NAME = "20_filtered.jsonl"
SUMS_NAME = "30_summaries.jsonl"
DED_NAME = "35_dedup.jsonl"
POOL_ITEMS_NAME = "38_pool_items.jsonl"     # 结转池 raw_item/1 投影（digest 可选输入）
POOL_SUMS_NAME = "38_pool_summaries.jsonl"  # 结转池 summary/1 投影

GRAY = {"gray", "gray_pending"}
SUPPRESSED = {"suppressed"}


# ---------------------------------------------------------------- helpers

def load_jsonl(p: Path) -> list[dict]:
    """流式读 JSONL；坏行/截断行 → 指明 文件:行号 的报错后 fail-fast。

    下游抢读写了一半的产物（或上游崩溃留残）会拿到 Unterminated string
    之类的裸 JSONDecodeError traceback——翻成可定位的输入错误。
    """
    out = []
    with open(p, encoding="utf-8") as f:
        for n, ln in enumerate(f, 1):
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError as e:
                eprint(f"[gate_select] 输入 {p.name}:{n} 行损坏（{e.msg}）"
                       " — 产物截断或上游写到一半崩了；重跑对应上游阶段")
                raise SystemExit(2)
    return out


# ------------------------------------------------------------- pool helpers

def _published_cluster_ids(cfg: dict) -> set:
    """history.sqlite 已出片 cluster_id 集（clusters.published=1，只读连接）；
    库缺席/打不开 -> 空集（projected_dedup 退化为纯存储判定）。"""
    p = Path(str(cfg.get("history_db") or "state/history.sqlite"))
    if not p.is_absolute():
        p = REPO / p
    if not p.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute(
                "SELECT cluster_id FROM clusters WHERE published=1")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        eprint(f"[gate_select] WARN history.sqlite 只读打开失败 {e}"
               " — published 投影按空集")
        return set()


def _open_pool(cfg: dict, episode: str):
    """POOL-MODE 判定：run_dir 名是日期 + items.sqlite 存在 + item_runs 有本期
    -> (conn, wfrom, wto, grace_from, sfloor, published_cids)；否则全 None
    六元组 + 响亮 WARN（配置/库就位却半空，绝不静默）。

    grace_from = episode - arrival_grace_days（first_seen 是期号日期：迟到的
    无日期/陈旧条目只再宽限这么多期）；sfloor = wfrom - carry_stale_max_days
    （子句 C 的 pub 下限，挡住归档源全量目录与池冷启动的古董洪水）。
    """
    none = (None, "", "", "", "", set())
    if not DATE_RE.fullmatch(episode):
        return none                                   # fixture/冒烟目录：纯文件
    db = pool.resolve_path(cfg.get("items_db"))
    if not db.exists():
        eprint(f"[gate_select] WARN 条目池 {db} 不存在 — 退回纯文件路径"
               "（collect 池回写尚未接入/未跑？）")
        return none
    try:
        conn = pool.init_db(db)
    except Exception as e:
        eprint(f"[gate_select] WARN 条目池 {db} 打开失败 {e} — 退回纯文件路径")
        return none
    try:
        has_ep = conn.execute(
            "SELECT 1 FROM item_runs WHERE episode=? LIMIT 1",
            (episode,)).fetchone() is not None
    except sqlite3.Error as e:
        conn.close()
        eprint(f"[gate_select] WARN 条目池 {db} 读 item_runs 失败 {e}"
               " — 退回纯文件路径")
        return none
    if not has_ep:
        conn.close()
        eprint(f"[gate_select] WARN 条目池 {db} 无 episode={episode} 的 "
               "item_runs — 退回纯文件路径（池半空，collect 池回写缺失？）")
        return none
    wfrom, wto = pool.window_bounds(episode)
    grace_from = (datetime.strptime(episode, "%Y-%m-%d").date()
                  - timedelta(days=int(cfg.get("arrival_grace_days") or 2))
                  ).isoformat()
    sfloor_days = cfg.get("carry_stale_max_days")
    sfloor = pool.stale_floor(wfrom, 14 if sfloor_days is None
                              else int(sfloor_days))
    return conn, wfrom, wto, grace_from, sfloor, _published_cluster_ids(cfg)


def _pool_candidate(row: dict) -> dict:
    """池行 -> 与文件路径同形的 candidate dict + carried=True +
    first_seen/last_seen/seen_count 展示字段。"""
    key = row["item_key"]
    ri = pool.to_raw_item(row)
    sr = pool.to_summary_row(row)
    src = ri.get("_source") or {}
    judge = row.get("dedup_judge")
    if isinstance(judge, str):                       # 库里存的是 JSON 文本
        try:
            judge = json.loads(judge)
        except json.JSONDecodeError:
            pass
    dv = row.get("projected") or row.get("dedup_verdict") or "fresh"
    fv = row.get("filter_verdict")
    return {
        "item_key": key,
        "id": "",  # 排序后统一 slug 化
        "section": section_slug(sr.get("section_guess"), key),
        "title_zh": sr.get("title_zh") or ri.get("title") or "",
        "summary": sr.get("summary") or (ri.get("content_text") or "")[:140],
        "date_published": ri.get("date_published"),
        "date_fetched": ri.get("date_fetched"),
        "ai_relevance": row.get("ai_relevance"),
        "news_value": row.get("news_value"),
        "reasons": [str(x) for x in (row.get("reasons_json") or [])],
        "url": ri.get("url") or ri.get("url_canon") or "",
        "source": src.get("name") or "?",
        "filter_verdict": fv,
        "dedup": {"verdict": dv,
                  "cluster_id": row.get("dedup_cluster_id"),
                  "match_cos": row.get("dedup_match_cos"),
                  "judge": judge},
        "recommend": (fv == "keep") and dv in ("fresh", "reissue"),
        "gray": dv in GRAY,
        "carried": True,
        "first_seen": row.get("first_seen"),
        "last_seen": row.get("last_seen"),
        "seen_count": row.get("seen_count"),
        "_slug_basis": [*(sr.get("entities") or []), ri.get("title") or "",
                        sr.get("title_zh") or ""],
    }


# ---------------------------------------------------------- build candidates

def build_candidates(run_dir: Path, items_db: str | None = None) -> dict:
    """20+30+35(+10) ∪ 条目池结转 -> candidates envelope。缺输入即 fail-fast（文件即依赖边）。
    items_db = --items-db 覆盖（> config.storage.items_db）。"""
    missing = [n for n in (RAW_NAME, FILT_NAME, SUMS_NAME, DED_NAME)
               if not (run_dir / n).exists()]
    if missing:
        # 提示必须按 DAG 序 collect -> filter -> dedup——对文件名 sorted()
        # 会把 dedup 排到 filter 前面，给出根本跑不通的顺序。
        hint = {RAW_NAME: "just collect", FILT_NAME: "just filter",
                SUMS_NAME: "just filter", DED_NAME: "just dedup"}
        need = []
        for n in (RAW_NAME, FILT_NAME, SUMS_NAME, DED_NAME):   # = DAG 序
            if n in missing and hint[n] not in need:
                need.append(hint[n])
        eprint(f"[gate_select] 缺输入 {missing} — 先跑: {' && '.join(need)}"
               "（或一把补齐: just gather）")
        raise SystemExit(2)

    raw = {r["item_key"]: r for r in load_jsonl(run_dir / RAW_NAME)}
    filts = load_jsonl(run_dir / FILT_NAME)
    sums = {s["item_key"]: s for s in load_jsonl(run_dir / SUMS_NAME)}
    ded = {d["item_key"]: d for d in load_jsonl(run_dir / DED_NAME)}

    cfg = load_config(items_db)
    episode = run_dir.name
    daily_map = cfg.get("daily_map") or {}
    candidates, suppressed, skipped = [], [], []
    skipped_window, skipped_used = [], []
    n_floor = 0                                  # 仅被 stale_floor 砍掉的结转条目

    pconn, wfrom, wto, grace_from, sfloor, published_cids = \
        _open_pool(cfg, episode)
    pool_rows_out: list[dict] = []
    try:
        served = set(raw)                       # 当期批次（10_raw_items 的键）
        prows = pool.get_many(pconn, served) if pconn is not None else {}
        for f in filts:
            key = f.get("item_key", "")
            fv = f.get("verdict")
            d = ded.get(key)
            dv = (d or {}).get("verdict") or "fresh"  # 无 dedup 行按 fresh
            prow = prows.get(key)
            # 自压制：35 行 cluster 与池行在位 cluster 相同 → 迟到的 dup_exact
            # 不杀在位条目，落回正常候选评估（同 cluster 语义见 pool.write_dedup）
            self_supp = (prow is not None
                         and prow.get("dedup_cluster_id") is not None
                         and prow.get("dedup_cluster_id")
                         == (d or {}).get("cluster_id"))
            if dv in SUPPRESSED and not self_supp:
                suppressed.append({"item_key": key,
                                   "cluster_id": (d or {}).get("cluster_id"),
                                   "match_cos": (d or {}).get("match_cos")})
                continue
            if fv not in ("keep", "review"):
                # L0 丢行兜底：feed 重发一条在窗未选的 keep 不该因 url_hash
                # 命中历史而丢候选格——池行 standing verdict∈{keep,review}
                # 且 eligible → 结转候选（carried=True）。兜底一旦评估过，
                # 排除理由以 eligible 为准归账（used/窗口），不再计
                # n_dropped_by_filter——占位 verdict 非真判，记 filter 丢弃
                # 是假账（上期 kept 条目重发时会被埋进错误的桶）。
                if (prow is not None
                        and (f.get("prov") or {}).get("model") == "l0-url-hash"
                        and prow.get("filter_verdict") in ("keep", "review")):
                    ok, why = pool.eligible(prow, episode, wfrom, wto,
                                            grace_from, sfloor, daily_map)
                    if ok:
                        candidates.append(_pool_candidate(prow))
                    elif why == "used":
                        skipped_used.append(
                            {"item_key": key,
                             "used_in_episode": prow.get("used_in_episode")})
                    else:
                        skipped_window.append({"item_key": key,
                                               "reason": why})
                    continue
                skipped.append({"item_key": key, "verdict": fv})
                continue
            s = sums.get(key) or {}
            r = raw.get(key) or {}
            src = r.get("_source") or {}
            gray = dv in GRAY
            candidates.append({
                "item_key": key,
                "id": "",  # 排序后统一 slug 化
                "section": section_slug(s.get("section_guess"), key),
                "title_zh": s.get("title_zh") or r.get("title") or "",
                "summary": s.get("summary") or (r.get("content_text") or "")[:140],
                "date_published": r.get("date_published"),
                "date_fetched": r.get("date_fetched"),
                "ai_relevance": f.get("ai_relevance"),
                "news_value": f.get("news_value"),
                "reasons": f.get("reasons") or [],
                "url": r.get("url") or r.get("url_canon") or "",
                "source": src.get("name") or "?",
                "filter_verdict": fv,
                "dedup": {"verdict": dv,
                          "cluster_id": (d or {}).get("cluster_id"),
                          "match_cos": (d or {}).get("match_cos"),
                          "judge": (d or {}).get("judge")},
                "recommend": (fv == "keep") and dv in ("fresh", "reissue"),
                "gray": gray,
                "_slug_basis": [*(s.get("entities") or []), r.get("title") or "",
                                s.get("title_zh") or ""],
            })

        carried_rows = []
        if pconn is not None:
            # 池结转候选：往期 keep/review 未出片且仍在窗/宽限内
            # （当期 item_runs 成员已被 SQL 排除，served 双保险）
            for row in pool.select_candidates(pconn, episode, wfrom, wto,
                                              grace_from, sfloor, daily_map):
                if row["item_key"] in served:
                    continue
                carried_rows.append(row)
                candidates.append(_pool_candidate(row))
            # 压制审计：文件侧 suppressed ∪ 池投影 suppressed
            for row in pool.select_suppressed(pconn, episode, wfrom, wto,
                                              grace_from, sfloor, daily_map):
                if row["item_key"] in served:
                    continue
                suppressed.append({"item_key": row["item_key"],
                                   "cluster_id": row.get("dedup_cluster_id"),
                                   "match_cos": row.get("dedup_match_cos"),
                                   "carried": True})
            # 下限审计：其余谓词全过、仅因 pub<sfloor 出局的条目数
            # （这些条目不进 skipped_window——它们根本没进候选评估）
            n_floor = pool.count_floor_cut(pconn, episode, grace_from,
                                           sfloor, daily_map)

        if pconn is not None:
            # ---- 统一谓词：used -> 窗口 -> projected-dedup（覆盖整个并集）----
            rows_by_key = dict(prows)
            rows_by_key.update({r["item_key"]: r for r in carried_rows})
            kept_c = []
            for c in candidates:
                row = rows_by_key.get(c["item_key"])
                if row is None:
                    kept_c.append(c)
                    continue
                used = row.get("used_in_episode")
                if used is not None and used != episode:
                    skipped_used.append({"item_key": c["item_key"],
                                         "used_in_episode": used})
                    continue
                ok, reason = pool.eligible(row, episode, wfrom, wto,
                                           grace_from, sfloor, daily_map)
                if not ok:
                    skipped_window.append({"item_key": c["item_key"],
                                           "reason": reason})
                    continue
                proj = pool.projected_dedup(row, published_cids)
                if proj == "suppressed":
                    suppressed.append({"item_key": c["item_key"],
                                       "cluster_id": row.get("dedup_cluster_id"),
                                       "match_cos": row.get("dedup_match_cos"),
                                       "carried": bool(c.get("carried"))})
                    continue
                # 出片投影覆盖展示判定：NULL->gray / 已出片 cluster->suppressed
                # （reissue 例外存活）；recommend/gray 随之重算
                c["dedup"]["verdict"] = proj
                c["gray"] = proj in GRAY
                c["recommend"] = (c.get("filter_verdict") == "keep"
                                  and proj in ("fresh", "reissue"))
                kept_c.append(c)
            candidates = kept_c
            pool_rows_out = [rows_by_key[c["item_key"]] for c in candidates
                             if c.get("carried") and c["item_key"] not in served
                             and c["item_key"] in rows_by_key]

        # 结转条目物化：每次 build 都重写（stale-safe），digest 按 item_key 兜底
        meta.atomic_write(run_dir / POOL_ITEMS_NAME,
                          meta.dumps_jsonl([pool.to_raw_item(r)
                                            for r in pool_rows_out]))
        meta.atomic_write(run_dir / POOL_SUMS_NAME,
                          meta.dumps_jsonl([pool.to_summary_row(r)
                                            for r in pool_rows_out]))
    finally:
        if pconn is not None:
            pconn.close()

    # 分区排序：分区按区内最高 news_value 排，区内按 news_value→ai_relevance 排
    def nv(c):
        return c["news_value"] if isinstance(c.get("news_value"), (int, float)) else -1

    sec_rank: dict[str, float] = {}
    for c in candidates:
        sec_rank[c["section"]] = max(sec_rank.get(c["section"], -1), nv(c))
    candidates.sort(key=lambda c: (-sec_rank[c["section"]], c["section"],
                                   -nv(c), -(c.get("ai_relevance") or 0),
                                   c["item_key"]))

    taken: set[str] = set()
    for c in candidates:
        base = ""
        for b in c.pop("_slug_basis"):
            base = slugify_id(str(b), c["item_key"])
            if SLUG_RE.match(base) and not base.startswith("n" + c["item_key"][:8]):
                break
        c["id"] = unique_slug(base, c["item_key"], taken)

    return {
        "schema": "candidates/1",
        "episode": episode,
        "generated_at": now_iso(),
        "config": cfg,
        "stats": {"n_candidates": len(candidates),
                  "n_suppressed": len(suppressed),
                  "n_dropped_by_filter": len(skipped),
                  "n_carried": sum(1 for c in candidates if c.get("carried")),
                  "n_skipped_window": len(skipped_window),
                  "n_skipped_used": len(skipped_used),
                  "n_stale_floor": n_floor},
        "candidates": candidates,
        "suppressed": suppressed,
        "skipped_window": skipped_window,
        "skipped_used": skipped_used,
    }


def cmd_prepare(run_dir: Path, items_db: str | None = None) -> int:
    meta.stage_begin(run_dir, "gate_prepare")   # 与下方 stage_done 同名才自动清除
    p = prog.Prog(run_dir, "gate_select")
    env = build_candidates(run_dir, items_db=items_db)
    meta.atomic_write(run_dir / CAND_NAME, env)
    meta.stage_done(run_dir, "gate_prepare", CAND_NAME, status="done")
    st = env["stats"]
    print(f"[gate_select] {CAND_NAME}: {st['n_candidates']} 候选 "
          f"(suppressed {st['n_suppressed']}, filter-drop {st['n_dropped_by_filter']}"
          + (f", carried {st['n_carried']}, "
             f"skip-window {st['n_skipped_window']}, "
             f"skip-used {st['n_skipped_used']}"
             if st.get("n_carried") or st.get("n_skipped_window")
             or st.get("n_skipped_used") else "")
          + (f", floor-cut {st['n_stale_floor']}"
             if st.get("n_stale_floor") else "")
          + ")")
    p.say(f"candidates={st['n_candidates']} "
          f"suppressed={st['n_suppressed']} filter-drop={st['n_dropped_by_filter']} "
          f"carried={st['n_carried']} skip-window={st['n_skipped_window']} "
          f"skip-used={st['n_skipped_used']} floor-cut={st['n_stale_floor']}")
    p.close()
    return 0


# ------------------------------------------------------------- write selected

def write_selected(run_dir: Path, kept_cands: list[dict], dropped_cands: list[dict],
                   decided_by: str, drop_reason: str,
                   items_db: str | None = None) -> int:
    doc = {
        "schema": "selected/1",
        "episode": run_dir.name,
        "decided_at": now_iso(),
        "decided_by": decided_by,
        "kept": [{"item_key": c["item_key"], "id": c["id"],
                  "section": c["section"], "note": c.get("note")}
                 for c in kept_cands],
        "dropped": [{"item_key": c["item_key"], "reason": drop_reason}
                    for c in dropped_cands],
    }
    errs = lint_selected(doc) + pydantic_validate(doc)
    for e in errs:
        eprint(f"[gate_select] lint warn: {e}")
    meta.atomic_write(run_dir / SEL_NAME, doc)
    meta.stage_done(run_dir, "gate_select", SEL_NAME, status="done",
                    extra={"decided_by": decided_by, "n_kept": len(kept_cands)})
    mark_used(run_dir, doc["episode"], [k["item_key"] for k in doc["kept"]],
              items_db=items_db)
    print(f"[gate_select] {SEL_NAME}: kept={len(kept_cands)} "
          f"dropped={len(dropped_cands)} by={decided_by}"
          + (" [kept 为空 — §11 no_items]" if not kept_cands else ""))
    return 0


def cmd_auto(run_dir: Path, force: bool = False, topk: int | None = None,
             items_db: str | None = None) -> int:
    sel = run_dir / SEL_NAME
    if sel.exists() and not force:
        try:
            by = json.loads(sel.read_text(encoding="utf-8")).get("decided_by")
        except Exception:
            by = "?"
        print(f"[gate_select] {SEL_NAME} 已存在 (decided_by={by}) — 跳过 (--force 可覆盖)")
        return 0
    meta.stage_begin(run_dir, "gate_select")  # 早退路径之后才登记——写端 stage_done 自动清除
    p = prog.Prog(run_dir, "gate_select")
    env = build_candidates(run_dir, items_db=items_db)
    meta.atomic_write(run_dir / CAND_NAME, env)  # 同步刷新 UI 数据源
    cfg = env["config"]
    k = min(topk or cfg["topk_autopick"], cfg["max_items"])
    order = sorted(env["candidates"],
                   key=lambda c: (-(c["news_value"] if isinstance(c.get("news_value"), (int, float)) else -1),
                                  -(c.get("ai_relevance") or 0), c["item_key"]))
    kept, dropped = order[:k], order[k:]
    p.say(f"auto-pick top-{k} of {len(order)} candidates "
          f"(kept={len(kept)} dropped={len(dropped)})")
    rc = write_selected(run_dir, kept, dropped, "auto", "below_topk",
                        items_db=items_db)
    p.say(f"{SEL_NAME} written: kept={len(kept)} decided_by=auto")
    p.close()
    return rc


def cmd_deadline_check(run_dir: Path, hhmm: str,
                       items_db: str | None = None) -> int:
    """timer 死线路径：40_selected 缺失且 now > run_date HH:MM(Asia/Shanghai) → auto。"""
    sel = run_dir / SEL_NAME
    if sel.exists():
        print(f"[gate_select] {SEL_NAME} 已存在 — 死线检查通过，无需放行")
        return 0
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm.strip())
    if not m:
        eprint(f"[gate_select] --deadline-check 参数非法: {hhmm!r}（要 HH:MM）")
        return 2
    try:
        day = datetime.strptime(run_dir.name, "%Y-%m-%d")
    except ValueError:
        eprint(f"[gate_select] run dir 名非日期: {run_dir.name}")
        return 2
    deadline = day.replace(hour=int(m.group(1)), minute=int(m.group(2)), tzinfo=TZ)
    now = datetime.now(TZ)
    if now <= deadline:
        print(f"[gate_select] 未到死线 {hhmm}（now {now.strftime('%H:%M')}）— 等待人工")
        return 0
    print(f"[gate_select] 已过死线 {hhmm} 且未提交 — 自动放行 top-K")
    return cmd_auto(run_dir, items_db=items_db)


# ------------------------------------------------------------------ selftest

@contextmanager
def _run_lock(run_dir: Path):
    """meta.run_lock 的提示包装：锁被占时先说明再阻塞等。

    残留的 .lock 文件本身无害——flock 锁的是打开的 inode，持锁进程一死
    锁即释放；文件留在盘上不阻塞任何人，无需清理。
    """
    contested = False
    try:
        with meta.run_lock(run_dir, blocking=False):
            pass  # 拿到即释放，纯探测占用
    except BlockingIOError:
        contested = True
    if contested:
        eprint(f"[gate_select] {run_dir} 有阶段运行中 — 等待 .lock 释放"
               "（Ctrl-C 退出）")
    with meta.run_lock(run_dir) as fd:
        yield fd


def _fixture_item(i: int, url_suffix: str = "") -> dict:
    url = f"https://example.com/news/{i}{url_suffix}"
    it = rawitem.build(
        url, source_name=f"Src{i}", feed_url="https://example.com/feed",
        kind="rss", date_fetched="2099-01-01T01:00:00+08:00",
        title=f"GPT-{i} released with benchmark {10+i}%",
        content_text=f"fixture body {i}")
    return it["item_key"], it


def _prov() -> dict:
    return {"model": "fixture", "prompt": "t", "input_sha": "0" * 16,
            "decided_at": "2099-01-01T01:00:00+08:00"}


def cmd_selftest() -> int:
    base = REPO / "runs" / "_gate_selftest"
    shutil.rmtree(base, ignore_errors=True)
    fails = []

    def check(name, cond, extra=""):
        print(f"  {'PASS' if cond else 'FAIL'} {name} {extra}")
        if not cond:
            fails.append(name)

    for ep, deadline in (("2099-01-01", "00:01"), ("2000-01-01", "23:59")):
        rd = meta.ensure_run(ep, base=base)
        keys = []
        for i in range(6):
            k, it = _fixture_item(i)
            keys.append(k)
            with open(rd / RAW_NAME, "a", encoding="utf-8") as f:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        filt = [  # keep keep keep review drop keep
            {"schema": "filter_verdict/1", "item_key": keys[0], "verdict": "keep",
             "ai_relevance": 0.95, "news_value": 90, "reasons": ["重大发布"], "prov": _prov()},
            {"schema": "filter_verdict/1", "item_key": keys[1], "verdict": "keep",
             "ai_relevance": 0.9, "news_value": 80, "reasons": [], "prov": _prov()},
            {"schema": "filter_verdict/1", "item_key": keys[2], "verdict": "keep",
             "ai_relevance": 0.8, "news_value": 60, "reasons": [], "prov": _prov()},
            {"schema": "filter_verdict/1", "item_key": keys[3], "verdict": "review",
             "ai_relevance": 0.5, "news_value": 70, "reasons": ["边界"], "prov": _prov()},
            {"schema": "filter_verdict/1", "item_key": keys[4], "verdict": "drop",
             "ai_relevance": 0.1, "news_value": 5, "reasons": [], "prov": _prov()},
            {"schema": "filter_verdict/1", "item_key": keys[5], "verdict": "keep",
             "ai_relevance": 0.85, "news_value": 55, "reasons": [], "prov": _prov()},
        ]
        sums = [{"schema": "summary/1", "item_key": k,
                 "title_zh": f" fixture 标题 {i}", "summary": f"概要 {i}",
                 "entities": [f"GPT-{i}"], "facts": [], "section_guess": "模型发布",
                 "prov": _prov()} for i, k in enumerate(keys)]
        ded = [
            {"schema": "dedup_verdict/1", "item_key": keys[0], "verdict": "fresh",
             "cluster_id": 1, "match_cos": None, "judge": None},
            {"schema": "dedup_verdict/1", "item_key": keys[1], "verdict": "gray",
             "cluster_id": 2, "match_cos": 0.7, "judge": {"label": "uncertain"}},
            {"schema": "dedup_verdict/1", "item_key": keys[2], "verdict": "suppressed",
             "cluster_id": 3, "match_cos": 0.99, "judge": None},
            {"schema": "dedup_verdict/1", "item_key": keys[3], "verdict": "fresh",
             "cluster_id": 4, "match_cos": None, "judge": None},
            {"schema": "dedup_verdict/1", "item_key": keys[4], "verdict": "fresh",
             "cluster_id": 5, "match_cos": None, "judge": None},
            {"schema": "dedup_verdict/1", "item_key": keys[5], "verdict": "reissue",
             "cluster_id": 6, "match_cos": 0.9, "judge": {"label": "B"}},
        ]
        for name, rows in ((FILT_NAME, filt), (SUMS_NAME, sums), (DED_NAME, ded)):
            (rd / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                           for r in rows), encoding="utf-8")

        print(f"[selftest] episode={ep} fixture 6 items "
              f"(keepx4 review drop / fresh gray suppressed fresh fresh reissue)")

        # -- prepare
        check("prepare rc", cmd_prepare(rd) == 0)
        env = json.loads((rd / CAND_NAME).read_text(encoding="utf-8"))
        cand_keys = [c["item_key"] for c in env["candidates"]]
        check("候选集 = keep/review 且非 suppressed",
              cand_keys == [keys[0], keys[1], keys[3], keys[5]],
              f"got {len(cand_keys)}")
        check("suppressed 入审计列", env["suppressed"] == [
            {"item_key": keys[2], "cluster_id": 3, "match_cos": 0.99}])
        check("灰区标记", env["candidates"][1]["gray"] is True
              and env["candidates"][1]["recommend"] is False)
        check("候选字段齐", all(
            all(k in c for k in ("title_zh", "summary", "ai_relevance",
                                 "news_value", "reasons", "url", "source",
                                 "dedup", "id", "section"))
            for c in env["candidates"]))
        check("候选 id 合法唯一",
              len({c["id"] for c in env["candidates"]}) == len(cand_keys)
              and all(SLUG_RE.match(c["id"]) for c in env["candidates"]))

        # -- deadline-check before deadline (2099 future -> no fire)
        rc = cmd_deadline_check(rd, deadline)
        if ep.startswith("2099"):
            check("未到死线不放行", rc == 0 and not (rd / SEL_NAME).exists())
        else:
            check("过死线自动放行", rc == 0 and (rd / SEL_NAME).exists())

        # -- auto
        check("auto rc", cmd_auto(rd) == 0)
        doc = json.loads((rd / SEL_NAME).read_text(encoding="utf-8"))
        check("schema selected/1", doc["schema"] == "selected/1")
        check("decided_by auto", doc["decided_by"] == "auto")
        check("episode", doc["episode"] == ep)
        check("kept 有序 = news_value 降序 top-K",
              [k["item_key"] for k in doc["kept"]] ==
              [keys[0], keys[1], keys[3], keys[5]])
        check("kept 全字段", all(
            KEY_RE.match(k["item_key"]) and SLUG_RE.match(k["id"])
            and isinstance(k["section"], str) and "note" in k
            for k in doc["kept"]))
        check("dropped 为空(4候选全取)",
              doc["dropped"] == [])
        check("lint_selected 无错", lint_selected(doc) == [],
              ";".join(lint_selected(doc)))
        check("pydantic 校验", pydantic_validate(doc) == [],
              ";".join(pydantic_validate(doc)))

        # -- auto 不覆盖已有决定
        (rd / SEL_NAME).write_text(json.dumps({**doc, "decided_by": "human"}))
        check("auto 不覆盖已有 40_selected", cmd_auto(rd) == 0
              and json.loads((rd / SEL_NAME).read_text())["decided_by"] == "human")
        check("deadline-check 不覆盖", cmd_deadline_check(rd, deadline) == 0
              and json.loads((rd / SEL_NAME).read_text())["decided_by"] == "human")

    # -- top-K 截断 + max_items 强制的单独用例
    rd = meta.ensure_run("2000-01-02", base=base)
    keys = []
    raws, filts, sums, ded = [], [], [], []
    for i in range(25):
        k, it = _fixture_item(i, "-b")
        keys.append(k)
        raws.append(it)
        filts.append({"schema": "filter_verdict/1", "item_key": k, "verdict": "keep",
                      "ai_relevance": 0.5, "news_value": 100 - i, "reasons": [],
                      "prov": _prov()})
        sums.append({"schema": "summary/1", "item_key": k, "title_zh": f"t{i}",
                     "summary": "s", "entities": [], "facts": [],
                     "section_guess": "sec", "prov": _prov()})
        ded.append({"schema": "dedup_verdict/1", "item_key": k, "verdict": "fresh",
                    "cluster_id": i, "match_cos": None, "judge": None})
    (rd / RAW_NAME).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in raws))
    for name, rows in ((FILT_NAME, filts), (SUMS_NAME, sums), (DED_NAME, ded)):
        (rd / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    check("25 候选 auto rc", cmd_auto(rd) == 0)
    doc = json.loads((rd / SEL_NAME).read_text(encoding="utf-8"))
    cfg = load_config()
    expect = min(cfg["topk_autopick"], cfg["max_items"])
    check(f"top-K 截断 kept==min(topk,max_items)={expect}",
          len(doc["kept"]) == expect and len(doc["dropped"]) == 25 - expect)
    check("kept 仍是 nv 最高者", doc["kept"][0]["item_key"] == keys[0])

    # -- 缺输入 fail-fast：提示必须按 DAG 序（filter 在 dedup 前）
    import contextlib, io
    rd = meta.ensure_run("2000-01-04", base=base)
    k, it = _fixture_item(0, "-c")
    (rd / RAW_NAME).write_text(json.dumps(it, ensure_ascii=False) + "\n")
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stderr(buf):
            cmd_prepare(rd)
    except SystemExit as e:
        rc = e.code
    msg = buf.getvalue()
    check("缺输入 exit=2", rc == 2)
    check("缺输入提示 filter 在 dedup 前（DAG 序）",
          msg.find("just filter") != -1
          and msg.find("just filter") < msg.find("just dedup"),
          msg.strip().splitlines()[-1] if msg.strip() else "no stderr")

    # -- 损坏 JSONL 行：报 文件:行号 而非裸 traceback
    bad = meta.ensure_run("2000-01-05", base=base)
    for name in (FILT_NAME, SUMS_NAME, DED_NAME):
        (bad / name).write_text('{"ok": 1}\n{"broken": "unterminated\n')
    (bad / RAW_NAME).write_text(json.dumps(it, ensure_ascii=False) + "\n")
    buf = io.StringIO()
    rc = 0
    try:
        with contextlib.redirect_stderr(buf):
            cmd_prepare(bad)
    except SystemExit as e:
        rc = e.code
    msg = buf.getvalue()
    check("坏行 exit=2", rc == 2)
    check("坏行报错带 文件:行号", "20_filtered.jsonl:2" in msg,
          msg.strip().splitlines()[-1] if msg.strip() else "no stderr")

    shutil.rmtree(base, ignore_errors=True)
    print(f"[selftest] {'FAIL ' + str(fails) if fails else 'ALL PASS'}")
    return 1 if fails else 0


# ----------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="人工闸 1：候选构建 + 勾选/自动放行（PLAN §7.3）")
    ap.add_argument("--run-dir", help="runs/<date> 或日期 YYYY-MM-DD")
    ap.add_argument("--prepare", action="store_true", help="只重建 40_candidates.json")
    ap.add_argument("--serve", action="store_true", help="prepare + 启动 review_server")
    ap.add_argument("--auto", action="store_true", help="top-K by news_value 自动放行")
    ap.add_argument("--force", action="store_true", help="--auto 覆盖已有 40_selected")
    ap.add_argument("--topk", type=int, default=None, help="覆盖 config.schedule.topk_autopick")
    ap.add_argument("--deadline-check", metavar="HH:MM",
                    help="死线检查：无 40_selected 且已过点 → auto")
    ap.add_argument("--items-db", default=None,
                    help="items.sqlite 路径（默认 config.storage.items_db > state/；"
                         "--serve 会转发给 review_server）")
    ap.add_argument("--selftest", action="store_true", help="fixture 端到端自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if not args.run_dir:
        ap.error("--run-dir 必填（--selftest 除外）")
    run_dir = resolve_run_dir(args.run_dir)

    if args.deadline_check:
        with _run_lock(run_dir):
            return cmd_deadline_check(run_dir, args.deadline_check,
                                      items_db=args.items_db)
    if args.auto:
        with _run_lock(run_dir):
            return cmd_auto(run_dir, force=args.force, topk=args.topk,
                            items_db=args.items_db)
    if args.serve:
        with _run_lock(run_dir):
            rc = cmd_prepare(run_dir, items_db=args.items_db)
        if rc != 0:
            return rc
        # 服务器持锁会阻塞 pick-auto 死线 watcher —— 锁外启动（justfile 同款约定）
        return cmd_serve_no_prepare(run_dir, items_db=args.items_db)
    # 默认 = --prepare
    with _run_lock(run_dir):
        return cmd_prepare(run_dir, items_db=args.items_db)


def cmd_serve_no_prepare(run_dir: Path, items_db: str | None = None) -> int:
    server = REPO / "stages" / "review_server.py"
    argv = [sys.executable, str(server), "--run-dir", str(run_dir)]
    if items_db:                 # 人工提交的 used 回写须落在同一条目池上
        argv += ["--items-db", str(items_db)]
    print("[gate_select] 启动 review_server（Ctrl-C 退出）…")
    return subprocess.call(argv, env=dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
