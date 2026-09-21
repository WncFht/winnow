#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "pyyaml>=6", "pydantic>=2"]
# ///
"""stages/filter.py — L0 精确去重 + LLM 批式相关性门 + 逐条概要（PLAN.md §7.1）。

输入  : <run_dir>/10_raw_items.jsonl   (raw_item/1，采集层产物)
        rulebook.md + aliases.json + config.yaml(llm/storage 段)
处理  :
  1) normalize: url_canon/item_key 重算（机械身份）+ title_norm
  2) L0: store.url_hash 命中 state/history.sqlite → 跳过 LLM，
     20 行记 verdict=drop（理由注明 dedup 将标 suppressed/dup_exact），
     同时写一条本地兜底概要进 30（不烧 LLM，保证 35 可审计留痕）
  3) 批式相关性门: prompts.FILTER_PROMPT(rulebook 全文, <item_data> 包裹)
     batch_size=config.llm.batch_size → keep|drop|review + ai_relevance +
     news_value + reasons；coverage_reconcile 缺项子集重批 ≤2 轮，
     仍缺 → verdict=review + prov 记错误（宁 review 勿错杀）
  4) keep|review 逐条 SUMMARY_PROMPT（线程池并发，默认 4 路）→
     title_zh/summary/entities/facts/section_guess；别名归一后未命中
     实体回流 state/alias_suggestions.jsonl
输出  : 20_filtered.jsonl (filter_verdict/1，全量含 L0，输入序)
        30_summaries.jsonl (summary/1)

用法: uv run stages/filter.py --run-dir runs/2026-09-22 [--limit N]
      [--config config.yaml] [--db state/history.sqlite] [--jobs 4] [--no-llm]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import yaml  # noqa: E402

from adapters import llm_swe2max as llm  # noqa: E402
from contracts.models import FilterVerdict, Summary  # noqa: E402
from lib import meta, normalize, prompts, store  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
RULEBOOK_PATH = REPO / "rulebook.md"
ALIAS_SUGGESTIONS = REPO / "state" / "alias_suggestions.jsonl"
VERDICTS = ("keep", "drop", "review")

RAW_IN = "10_raw_items.jsonl"
OUT_FILTER = "20_filtered.jsonl"
OUT_SUMMARY = "30_summaries.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clamp01(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, f))


def _prov(model: str, prompt_tag: str, item_key: str, ts: str | None = None) -> dict:
    """contracts.Provenance：input_sha 用 item_key（契约允许的输入指纹）。"""
    return {
        "model": model,
        "prompt": prompt_tag,
        "input_sha": hashlib.sha256(item_key.encode()).hexdigest()[:16],
        "decided_at": ts or _now(),
    }


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def _out_id(x):
    """coverage key：输出回引 item_data id(=item_key)；输入 dict 的 id 同构。"""
    if isinstance(x, dict):
        return x.get("id") or x.get("item_key")
    return x


def _as_verdict_list(out) -> list:
    """chat_json 返回应是 list；宽容解包 {items|verdicts|results:[...]} / 单对象。"""
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        for k in ("items", "verdicts", "results", "data"):
            if isinstance(out.get(k), list):
                return out[k]
        return [out]
    return []


# ---------------------------------------------------------------------------
# 输入规整
# ---------------------------------------------------------------------------

def load_items(run_dir: Path, limit: int | None) -> list[dict]:
    p = run_dir / RAW_IN
    if not p.exists():
        sys.exit(f"[filter] 缺少输入 {p}（先跑 collect）")
    items = []
    seen = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        it = json.loads(ln)
        uc = normalize.url_canon(it.get("url_canon") or it.get("url") or "")
        it["url_canon"] = uc
        it["item_key"] = normalize.item_key(uc)
        it["id"] = it.get("id") or it["item_key"]
        it["title"] = normalize.title_norm(it.get("title") or "")
        if it["item_key"] in seen:      # item_key 撞车只留首条（机械身份唯一）
            continue
        seen.add(it["item_key"])
        items.append(it)
        if limit and len(items) >= limit:
            break
    return items


def l0_lookup(items: list[dict], db_path: Path | None) -> dict:
    """url_hash 精确命中 history → {item_key: 命中行标题}。库不存在 → 空集。"""
    if not db_path or not Path(db_path).exists():
        return {}
    conn = store.init_db(str(db_path))
    hits = {}
    for it in items:
        uh = store.url_hash(it["url_canon"])
        it["_url_hash"] = uh
        if not uh:
            continue
        row = conn.execute(
            "SELECT title FROM items WHERE url_hash=? LIMIT 1", (uh,)
        ).fetchone()
        if row:
            hits[it["item_key"]] = row["title"]
    conn.close()
    return hits


# ---------------------------------------------------------------------------
# LLM 批式筛选（含 coverage reconcile 重批 ≤2 轮）
# ---------------------------------------------------------------------------

def filter_batch(batch: list[dict], cfg: dict, rulebook: str) -> tuple[list, list, dict | None, Exception | None]:
    """-> (accepted_outputs, missing_items, last_prov, last_error)."""
    missing = list(batch)
    outs, prov, err = [], None, None
    for _attempt in range(3):                    # 首轮 + 重批 ≤2
        if not missing:
            break
        sm, um = prompts.FILTER_PROMPT(missing, rulebook)
        provs: list = []
        try:
            out = llm.chat_json(prompts.messages(sm, um), prov_out=provs,
                                cfg=cfg, tag="filter")
        except llm.LLMError as e:
            err = e
            break                                # 网关层失败：整批转人工
        if provs:
            prov = provs[-1]
        rec = llm.coverage_reconcile(missing, _as_verdict_list(out), key=_out_id)
        outs.extend(rec["outputs"])
        missing = rec["missing"]
    return outs, missing, prov, err


def verdict_row(item_key: str, verdict: str, ai, nv, reasons: list,
                model: str, prompt_tag: str, ts: str | None = None) -> dict:
    return {
        "schema": "filter_verdict/1",
        "item_key": item_key,
        "verdict": verdict if verdict in VERDICTS else "review",
        "ai_relevance": _clamp01(ai, 0.5),
        "news_value": _clamp01(nv),
        "reasons": [str(r)[:60] for r in (reasons or [])][:3],
        "prov": _prov(model, prompt_tag, item_key, ts),
    }


def run_filter(items: list[dict], l0: dict, cfg: dict, rulebook: str,
               batch_size: int, no_llm: bool) -> dict:
    """-> {item_key: filter_verdict row}，按输入序输出。"""
    rows: dict[str, dict] = {}
    ptag = prompts.PROMPT_VERSIONS["filter"]
    for it in items:
        if it["item_key"] in l0:
            rows[it["item_key"]] = verdict_row(
                it["item_key"], "drop", 0.0, 0.0,
                [f"l0:url_hash 命中历史《{l0[it['item_key']][:24]}》，35 将标 suppressed"],
                model="l0-url-hash", prompt_tag="l0-v1")
    pending = [it for it in items if it["item_key"] not in rows]
    if no_llm:
        for it in pending:
            rows[it["item_key"]] = verdict_row(
                it["item_key"], "review", 0.5, None,
                ["no-llm 模式，全部转人工"], model="no-llm", prompt_tag=ptag)
        return rows
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        outs, missing, prov, err = filter_batch(batch, cfg, rulebook)
        ts = (prov or {}).get("ts")
        model = (prov or {}).get("model") or cfg.get("model", "swe-2-max")
        by_id = {it["id"]: it for it in batch}
        for o in outs:
            src = by_id.get(_out_id(o)) or {}
            key = src.get("item_key") or str(_out_id(o))
            v = str(o.get("verdict") or "").strip().lower()
            reasons = o.get("reasons") or []
            if v not in VERDICTS:
                v, reasons = "review", list(reasons) + [f"非法 verdict 归一: {o.get('verdict')}"]
            rows[key] = verdict_row(key, v, o.get("ai_relevance"),
                                    o.get("news_value"), reasons,
                                    model=model, prompt_tag=ptag, ts=ts)
        for it in missing:                       # 重批 2 轮仍缺 → review
            rows[it["item_key"]] = verdict_row(
                it["item_key"], "review", 0.5, None,
                [f"coverage-miss: 重批2轮仍缺，转人工"
                 + (f"；网关错误 {type(err).__name__}" if err else "")],
                model=model, prompt_tag=ptag, ts=ts)
    return rows


# ---------------------------------------------------------------------------
# 逐条概要（keep|review；L0 走本地兜底）
# ---------------------------------------------------------------------------

def _summary_fallback(it: dict) -> dict:
    return {
        "title_zh": (it.get("title") or "(untitled)")[:30],
        "summary": (it.get("content_text") or it.get("title") or "")[:120],
        "entities": [], "facts": [], "section_guess": None,
    }


def summary_row(it: dict, out, provs: list, cfg: dict,
                aliases: dict, l0_hit: bool) -> dict:
    now = _now()
    if isinstance(out, dict):
        fb = _summary_fallback(it)
        title_zh = str(out.get("title_zh") or "").strip() or fb["title_zh"]
        summary = str(out.get("summary") or "").strip() or fb["summary"]
        ents = [str(e).strip() for e in (out.get("entities") or []) if str(e).strip()][:12]
        facts = [str(f).strip() for f in (out.get("facts") or []) if str(f).strip()][:20]
        sec = out.get("section_guess") or None
        model = (provs[-1] if provs else {}).get("model") or cfg.get("model", "?")
        ts = (provs[-1] if provs else {}).get("ts") or now
        ptag = prompts.PROMPT_VERSIONS["summary"]
    else:
        fb = _summary_fallback(it)
        title_zh, summary, ents, facts, sec = (fb["title_zh"], fb["summary"],
                                             fb["entities"], fb["facts"],
                                             fb["section_guess"])
        model = "l0-url-hash" if l0_hit else "local-fallback"
        ts, ptag = now, "summary-v1-fallback"
    # 实体别名归一（facts 保原文逐字，不动——下游数字白名单以此为据）
    title_zh = normalize.apply_aliases(title_zh, aliases)
    summary = normalize.apply_aliases(summary, aliases)
    seen, ents_n = set(), []
    for e in (normalize.apply_aliases(e, aliases) for e in ents):
        if e not in seen:
            seen.add(e)
            ents_n.append(e)
    return {
        "schema": "summary/1",
        "item_key": it["item_key"],
        "title_zh": title_zh,
        "summary": summary,
        "entities": ents_n,
        "facts": facts,
        "section_guess": str(sec) if sec else None,
        "prov": _prov(model, ptag, it["item_key"], ts),
    }


def run_summaries(items: list[dict], verdicts: dict, l0: dict, cfg: dict,
                  aliases: dict, jobs: int, no_llm: bool) -> tuple[list, list]:
    """-> (summary rows 按输入序, 未命中别名实体 [(entity,item_key)])."""
    todo = [it for it in items
            if verdicts[it["item_key"]]["verdict"] in ("keep", "review")
            or it["item_key"] in l0]
    rows: dict[str, dict] = {}
    unknown: list[tuple[str, str]] = []
    known = {t.casefold() for canon, als in aliases.items()
             for t in [canon, *als]}

    def work(it):
        if it["item_key"] in l0 or no_llm:
            return it["item_key"], summary_row(it, None, [], cfg, aliases,
                                               l0_hit=it["item_key"] in l0)
        sm, um = prompts.SUMMARY_PROMPT(it)
        provs: list = []
        try:
            out = llm.chat_json(prompts.messages(sm, um), prov_out=provs,
                                cfg=cfg, tag="summary")
        except llm.LLMError:
            out = None
        return it["item_key"], summary_row(it, out, provs, cfg, aliases,
                                           l0_hit=False)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(todo)))) as ex:
            for key, row in ex.map(work, todo):
                rows[key] = row
    for it in todo:
        for e in rows[it["item_key"]]["entities"]:
            if e.casefold() not in known:
                unknown.append((e, it["item_key"]))
    return [rows[it["item_key"]] for it in todo], unknown


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="PLAN §7.1 filter stage")
    ap.add_argument("--run-dir", required=True, help="runs/<date>")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--db", default=None, help="history.sqlite（默认 config.storage.history_db）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（测试用）")
    ap.add_argument("--batch-size", type=int, default=0, help="覆盖 llm.batch_size")
    ap.add_argument("--jobs", type=int, default=4, help="summary 并发数")
    ap.add_argument("--no-llm", action="store_true", help="不调网关：全部 review（冒烟用）")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    t0 = time.time()
    items = load_items(run_dir, args.limit or None)
    rulebook = RULEBOOK_PATH.read_text(encoding="utf-8")
    aliases = normalize.load_aliases()
    cfg = ({"model": "no-llm", "batch_size": 24} if args.no_llm
           else llm.load_cfg(args.config))
    batch_size = args.batch_size or int(cfg.get("batch_size") or 24)

    if args.db:
        db_path = Path(args.db)
    else:
        cfg_path = Path(args.config) if args.config else REPO / "config.yaml"
        if not cfg_path.exists():
            cfg_path = REPO / "config.example.yaml"
        doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        db_path = REPO / ((doc.get("storage") or {}).get("history_db")
                          or "state/history.sqlite")

    with meta.run_lock(run_dir):
        l0 = l0_lookup(items, db_path)
        verdicts = run_filter(items, l0, cfg, rulebook, batch_size, args.no_llm)
        ordered = [verdicts[it["item_key"]] for it in items]
        for r in ordered:                        # 契约 lint（写完即调 §4）
            FilterVerdict.model_validate(r)
        meta.atomic_write(run_dir / OUT_FILTER, _jsonl(ordered))
        meta.stage_done(run_dir, "filter", OUT_FILTER,
                        extra={"n_items": len(items), "l0_hits": len(l0),
                               "kept": sum(1 for r in ordered if r["verdict"] == "keep"),
                               "dropped": sum(1 for r in ordered if r["verdict"] == "drop"),
                               "review": sum(1 for r in ordered if r["verdict"] == "review")})

        summaries, unknown = run_summaries(items, verdicts, l0, cfg, aliases,
                                           args.jobs, args.no_llm)
        for r in summaries:
            Summary.model_validate(r)
        meta.atomic_write(run_dir / OUT_SUMMARY, _jsonl(summaries))
        meta.stage_done(run_dir, "summaries", OUT_SUMMARY,
                        extra={"n": len(summaries),
                               "alias_suggestions": len(unknown)})
        if unknown:
            ALIAS_SUGGESTIONS.parent.mkdir(parents=True, exist_ok=True)
            seen, lines = set(), []
            for e, k in unknown:
                if (e, k) in seen:
                    continue
                seen.add((e, k))
                lines.append(json.dumps({"ts": _now(), "entity": e,
                                         "item_key": k, "run": run_dir.name},
                                        ensure_ascii=False))
            with open(ALIAS_SUGGESTIONS, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    # ---- 报告 ----
    n = len(items)
    cnt = {v: sum(1 for r in ordered if r["verdict"] == v) for v in VERDICTS}
    print(f"[filter] {run_dir.name}: {n} items in {time.time()-t0:.0f}s | "
          f"L0={len(l0)} keep={cnt['keep']} drop={cnt['drop']} review={cnt['review']} "
          f"| summaries={len(summaries)} alias_new={len(unknown)}")
    for r in ordered:
        it = next(i for i in items if i["item_key"] == r["item_key"])
        print(f"  {r['verdict']:6s} ai={r['ai_relevance']:.2f} "
              f"nv={r['news_value'] if r['news_value'] is not None else '-':>4} "
              f"{r['item_key'][:8]} {it['title'][:52]} | {';'.join(r['reasons'])[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
