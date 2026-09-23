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
  5) 条目池缓存（state/items.sqlite，--no-llm/--no-pool 时完全断开）：
     判定命中条件 judged_content_sha==content_sha 且 prompt/model 未变；
     概要命中条件 summary_sha（被概要内容的 content_sha）+prompt+model。
     判定/概要回写先于文件落盘——池是主记录，20/30 是可重放投影；
     跑完后扫 needs_summary 补结转行概要（pool-only，不写 30）。
输出  : 20_filtered.jsonl (filter_verdict/1，全量含 L0，输入序)
        30_summaries.jsonl (summary/1)

用法: uv run stages/filter.py --run-dir runs/2026-09-22 [--limit N]
      [--config config.yaml] [--db state/history.sqlite] [--jobs 4] [--no-llm]
      [--items-db state/items.sqlite] [--no-pool]
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import yaml  # noqa: E402

from adapters import llm_swe2max as llm  # noqa: E402
from contracts.models import FilterVerdict, RawItem, Summary  # noqa: E402
from lib import meta, normalize, pool, prog, prompts, store  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
RULEBOOK_PATH = REPO / "rulebook.md"
ALIAS_SUGGESTIONS = REPO / "state" / "alias_suggestions.jsonl"
VERDICTS = ("keep", "drop", "review")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

RAW_IN = "10_raw_items.jsonl"
OUT_FILTER = "20_filtered.jsonl"
OUT_SUMMARY = "30_summaries.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _score01(v, default=None):
    """0-1 评分归一：模型按百分制输出（>1）时 /100 归一（契约 news_value
    允许 0-100，百分制原文会"合法"地漏判）；非法值 → default。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f > 1.0:
        f = f / 100.0
    return min(1.0, max(0.0, f))


# ---------------------------------------------------------------------------
# §7.1 验收硬规则：注入指令条目 → 确定性 drop（不靠 LLM 自觉，post-hoc 兜底）
#
# 两层模式：
#  _HARD_INJ_RES    直接操纵本次判定的指令——管道内部词汇（verdict/
#                   ai_relevance/"mark this item"）、伪造 system/角色标签。
#                   真实新闻不会含这些字样 → 一律 drop。
#  _GENERIC_INJ_RES 通用注入措辞（"忽略上述指令"/"ignore previous
#                   instructions"等）——默认 drop（验收要求）；但当文本
#                   明显在"报道注入攻击"（含 prompt injection/提示注入/
#                   越狱 等词）时放行给 LLM，避免误杀 AI 安全新闻。
# ---------------------------------------------------------------------------
_HARD_INJ_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"\bmark\s+this\s+(item|article|entry)\b",
    r"\bverdict\s*[=:]",
    r"\b(ai_?relevance|news_?value)\s*[=:]\s*\d",
    r"\bsystem\s*override\b",
    r"<<\s*/?\s*SYS\s*>>",
    r"\[\s*/?\s*INST\s*\]",
    r"<\s*/?\s*(system|instruction|item_data)\b",
    r"判\s*(keep|drop|review)\b",
    r"(verdict|ai_relevance)\s*=\s*\S+",
)]
_GENERIC_INJ_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"忽略\s*(上述|以上|之前|先前|所有|一切|全部)\s*指令",
    r"忽略\s*(所有|一切|全部)\s*指令",
    r"\b(ignore|disregard|forget|override|bypass)\b\s+(all\s+|any\s+|the\s+)"
    r"?(previous|prior|above|earlier|preceding)\s+\w*\s*(instructions?|prompts?|rules?)\b",
    r"\b(ignore|disregard|forget)\s+all\b.{0,30}\b(instructions?|rules?)\b",
    r"\byour\s+(new\s+)?(instructions?|task|goal|objective)\s+(is|are)\b",
)]
_INJ_REPORT_CTX = re.compile(
    r"提示注入|prompt[- ]?injection|注入攻击|注入指令|越狱|jailbreak|"
    r"red[- ]?team|对抗样本|adversarial", re.IGNORECASE)


def _injection_hit(item: dict) -> str | None:
    """正文/title 中含注入指令 → 返回命中片段（审计用），否则 None。

    硬模式（操纵判定/伪造角色）一律命中；通用注入措辞在"报道注入攻击"
    语境（_INJ_REPORT_CTX）下放行——安全新闻引述攻击载荷不算攻击。
    """
    text = "\n".join(str(item.get(k) or "")
                     for k in ("title", "content_text", "summary"))
    if not text.strip():
        return None
    for rx in _HARD_INJ_RES:
        m = rx.search(text)
        if m:
            return m.group(0)[:40]
    if _INJ_REPORT_CTX.search(text):
        return None
    for rx in _GENERIC_INJ_RES:
        m = rx.search(text)
        if m:
            return m.group(0)[:40]
    return None


def _apply_injection_guard(rows: dict, items: list[dict]) -> tuple[dict, set]:
    """§7.1 验收：注入指令条目确定性 drop——覆盖 LLM/no-llm/coverage-miss
    全部路径（L0 已是 drop 不重复改写）。分数保留 LLM 原判：条目主题
    可以高度相关，drop 是安全处置而非相关性结论。

    -> (rows, guarded_keys)：guarded_keys = 本次实际被守卫改写的键，
    供池回写打 'injection-guard-v1' 标记（守卫判不以 LLM prompt 版本入账，
    下期照常重判，缓存永不喂守卫行）。"""
    guarded: set = set()
    for it in items:
        hit = _injection_hit(it)
        if not hit:
            continue
        row = rows.get(it["item_key"])
        if row and row["verdict"] == "drop":
            continue
        prov = (row or {}).get("prov") or {}
        reasons = [f"prompt-injection: 正文含面向评审的指令「{hit}」"]
        if row:
            reasons += list(row.get("reasons") or [])[:2]
        rows[it["item_key"]] = verdict_row(
            it["item_key"], "drop",
            (row or {}).get("ai_relevance"), (row or {}).get("news_value"),
            reasons,
            model=prov.get("model") or "injection-guard",
            prompt_tag=prov.get("prompt") or "injection-guard-v1",
            ts=prov.get("decided_at"))
        guarded.add(it["item_key"])
    return rows, guarded


def _prov(model: str, prompt_tag: str, item_key: str, ts: str | None = None) -> dict:
    """contracts.Provenance：input_sha 用 item_key（契约允许的输入指纹）。"""
    return {
        "model": model,
        "prompt": prompt_tag,
        "input_sha": hashlib.sha256(item_key.encode()).hexdigest()[:16],
        "decided_at": ts or _now(),
    }


def _jsonl(rows: list[dict]) -> str:
    return meta.dumps_jsonl(rows)


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

def _raw_check(obj) -> str | None:
    """iter_jsonl 的逐条校验钩子：返回 None 放行，错误描述串则该行按坏行处理。

    能解析出 _source.name 时把它带进错误串——坏行报告同时给文件行号和
    来源源名两个定位维度。
    """
    if not isinstance(obj, dict):
        return "行不是 JSON object"
    src = (obj.get("_source") or {}).get("name") \
        if isinstance(obj.get("_source"), dict) else None
    tag = f" [src={src}]" if src else ""
    try:
        RawItem.model_validate(obj)
    except Exception as e:
        errs = e.errors() if hasattr(e, "errors") else []
        if errs:
            loc = ".".join(str(x) for x in errs[0].get("loc", ()))
            return (f"raw_item/1 校验失败({len(errs)}处): "
                    f"{loc} {errs[0].get('msg')}{tag}")[:160]
        return f"raw_item/1 校验失败: {str(e).splitlines()[0]}{tag}"[:160]
    return None


def load_items(run_dir: Path, limit: int | None) -> tuple[list[dict], list]:
    """读 10_raw_items.jsonl -> (items, bad)。

    坏行策略（明确）：skip + 记账，不因一行崩掉全量——
      * JSON 解析失败 / raw_item/1 校验失败的行被跳过，定位
        ({file}:{lineno}: 原因) 收进 bad 并逐条打到 stderr；
      * 正常行继续处理。调用方把 len(bad) 记进 stage stats 与报告。
    meta.iter_jsonl 用文件迭代切行（只认 \\n/\\r\\n/\\r），不会被字符串内
    字面 U+2028/U+2029 截断（runs/2026-09-22 事故根因：splitlines 把
    3349 行拆成 3352 段 → Unterminated string col 4337）。
    """
    p = run_dir / RAW_IN
    if not p.exists():
        sys.exit(f"[filter] 缺少输入 {p}（先跑 collect）")
    items, bad, seen = [], [], set()
    for it in meta.iter_jsonl(p, errors=bad, check=_raw_check):
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
    if bad:
        print(f"[filter] WARN {p}: {len(bad)} 条坏行已跳过（不计入处理）",
              file=sys.stderr)
        for e in bad[:20]:
            print(f"  {e}", file=sys.stderr)
        if len(bad) > 20:
            print(f"  … 其余 {len(bad) - 20} 条略", file=sys.stderr)
    return items, bad


def l0_lookup(items: list[dict], db_path: Path | None) -> dict:
    """url_hash 精确命中 history → {item_key: 命中行标题}。库不存在 → 空集。"""
    if not db_path or not Path(db_path).exists():
        return {}
    conn = store.init_db(str(db_path))
    for it in items:
        it["_url_hash"] = store.url_hash(it["url_canon"])
    hashes = sorted({it["_url_hash"] for it in items if it["_url_hash"]})
    found: dict[str, str] = {}
    for i in range(0, len(hashes), 500):       # SQLite 变量上限 999，留余量
        chunk = hashes[i:i + 500]
        q = ("SELECT url_hash, title FROM items WHERE url_hash IN ("
             + ",".join("?" * len(chunk)) + ")")
        for r in conn.execute(q, chunk):
            found.setdefault(r["url_hash"], r["title"])
    conn.close()
    return {it["item_key"]: found[it["_url_hash"]]
            for it in items if it["_url_hash"] in found}


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
        "ai_relevance": _score01(ai, 0.5),
        "news_value": _score01(nv),
        "reasons": [str(r)[:60] for r in (reasons or [])][:3],
        "prov": _prov(model, prompt_tag, item_key, ts),
    }


def run_filter(items: list[dict], l0: dict, cfg: dict, rulebook: str,
               batch_size: int, no_llm: bool,
               pool_rows: dict | None = None, jobs: int = 1,
               p: prog.Prog | None = None) -> tuple[dict, dict]:
    """-> ({item_key: filter_verdict row} 按输入序, aux)。

    aux 键：
      judged_keys   本轮真 LLM outs 行对应的 item_key（含非法 verdict 归一的
                    review；coverage-miss/LLMError 占位、L0、no-llm 一律不算）
      guarded_keys  注入守卫改写的键（_apply_injection_guard 返回值）
      pool_known    pending 中池里已有的条数（跨期再现）
      cache_hits    判定缓存命中数（judged_content_sha+prompt+model 全同）
      cache_misses  pool_known 中未命中数（命中率金丝雀用）
    """
    rows: dict[str, dict] = {}
    ptag = prompts.PROMPT_VERSIONS["filter"]
    aux = {"judged_keys": set(), "guarded_keys": set(),
           "pool_known": 0, "cache_hits": 0, "cache_misses": 0}
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
        rows, aux["guarded_keys"] = _apply_injection_guard(rows, items)
        return rows, aux
    # ---- 条目池判定缓存：内容指纹 + prompt + model 全同 → 放行原判（原始 prov）
    if pool_rows:
        rest = []
        for it in pending:
            pr = pool_rows.get(it["item_key"])
            if pr is None:
                rest.append(it)
                continue
            aux["pool_known"] += 1
            if (pr.get("filter_verdict") is not None
                    and pr.get("judged_content_sha") == pool.content_sha(it)
                    and pr.get("verdict_prompt") == ptag
                    and pr.get("verdict_model") == cfg.get("model")):
                rows[it["item_key"]] = pool.to_verdict_row(pr)
                aux["cache_hits"] += 1
            else:
                rest.append(it)
        aux["cache_misses"] = aux["pool_known"] - aux["cache_hits"]
        pending = rest
    batches = [pending[i:i + batch_size]
               for i in range(0, len(pending), batch_size)]
    p = p or prog.Prog(None, "filter")
    p.total = len(batches)
    p.say(f"verdicts: {len(pending)} 条待判定 — {len(batches)} 批×{batch_size}")
    def _judge(b):                               # 批与批相互独立，可并行打网关
        return b, filter_batch(b, cfg, rulebook)
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(batches)))) as ex:
        results_iter = (ex.map(_judge, batches)
                        if jobs > 1 else map(_judge, batches))
        for bi, (batch, (outs, missing, prov, err)) in enumerate(results_iter):
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
                aux["judged_keys"].add(key)
            for it in missing:                   # 重批 2 轮仍缺 → review
                rows[it["item_key"]] = verdict_row(
                    it["item_key"], "review", 0.5, None,
                    [f"coverage-miss: 重批2轮仍缺，转人工"
                     + (f"；网关错误 {type(err).__name__}" if err else "")],
                    model=model, prompt_tag=ptag, ts=ts)
            p.tick(bi + 1, f"verdict batch out={len(outs)} miss={len(missing)}",
                   force=True)
    rows, aux["guarded_keys"] = _apply_injection_guard(rows, items)
    return rows, aux


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


def _pool_summary_row(prow: dict, aliases: dict) -> dict:
    """缓存命中：池行 → summary/1（原始 prov），再跑一遍别名归一——
    aliases.json 后续更新要传播到旧概要，不能被缓存冻住。"""
    row = pool.to_summary_row(prow)
    row["title_zh"] = normalize.apply_aliases(row["title_zh"], aliases)
    row["summary"] = normalize.apply_aliases(row["summary"], aliases)
    seen, ents = set(), []
    for e in (normalize.apply_aliases(str(e), aliases) for e in row["entities"]):
        if e and e not in seen:
            seen.add(e)
            ents.append(e)
    row["entities"] = ents
    return row


def _summary_llm_row(it: dict, cfg: dict, aliases: dict) -> tuple[dict, bool]:
    """单条 SUMMARY_PROMPT → (summary_row, real)。

    real=False 表示走了本地兜底（LLMError）——兜底行绝不写回条目池。
    l0/no-llm 的兜底分流在调用方，这里只管真调网关。"""
    sm, um = prompts.SUMMARY_PROMPT(it)
    provs: list = []
    try:
        out = llm.chat_json(prompts.messages(sm, um), prov_out=provs,
                            cfg=cfg, tag="summary")
    except llm.LLMError:
        out = None
    return (summary_row(it, out, provs, cfg, aliases, l0_hit=False),
            isinstance(out, dict))


_SUM_BATCH = 10     # 概要批量条数：输出 token（max_tokens≈24k，单条概要
                    # ~300-600 tok）与单批故障爆炸半径之间的折中


def summary_batch(batch: list[dict], cfg: dict,
                  aliases: dict) -> list[tuple[dict, dict, bool]]:
    """一批 → 一次 chat_json 批量概要 → [(item, row, real)] 与输入同序。

    网关层失败（LLMError，内部已重试）或批量覆盖缺失 → 缺失条目逐条回退
    _summary_llm_row：批量路径不放大失败半径。"""
    sm, um = prompts.SUMMARY_BATCH_PROMPT(batch)
    provs: list = []
    out = None
    try:
        out = llm.chat_json(prompts.messages(sm, um), prov_out=provs,
                            cfg=cfg, tag="summary")
    except llm.LLMError:
        pass
    by_key: dict[str, tuple[dict, bool]] = {}
    rest = batch
    if out is not None:
        rec = llm.coverage_reconcile(batch, _as_verdict_list(out), key=_out_id)
        in_by_id = {_out_id(it): it for it in batch}
        for o in rec["outputs"]:
            it = in_by_id.get(_out_id(o))
            if it is not None:
                by_key[it["item_key"]] = (
                    summary_row(it, o, provs, cfg, aliases, l0_hit=False), True)
        rest = rec["missing"]
    for it in rest:
        by_key[it["item_key"]] = _summary_llm_row(it, cfg, aliases)
    return [(it, *by_key[it["item_key"]]) for it in batch]


def run_summaries(items: list[dict], verdicts: dict, l0: dict, cfg: dict,
                  aliases: dict, jobs: int, no_llm: bool,
                  pool_rows: dict | None = None,
                  sum_batch: int = _SUM_BATCH,
                  p: prog.Prog | None = None) -> tuple[list, list, dict]:
    """-> (summary rows 按输入序, 未命中别名实体 [(entity,item_key)], aux)。

    aux: summary_hits = 池概要缓存命中数（summary_sha==content_sha 且
    prompt/model 未变，命中行重跑别名归一后原样放出）；
    fresh_rows = 本轮真 LLM 概要行（调用方回写 items.sqlite 用——本地
    兜底/no-llm 行不在其列）。
    sum_batch=1 退回逐条路径（调试用）。
    """
    todo = [it for it in items
            if verdicts[it["item_key"]]["verdict"] in ("keep", "review")
            or it["item_key"] in l0]
    rows: dict[str, dict] = {}
    unknown: list[tuple[str, str]] = []
    aux: dict = {"summary_hits": 0, "fresh_rows": []}
    known = {t.casefold() for canon, als in aliases.items()
             for t in [canon, *als]}
    ptag = prompts.PROMPT_VERSIONS["summary"]

    llm_todo: list[dict] = []
    for it in todo:
        k = it["item_key"]
        pr = (pool_rows or {}).get(k)
        if (pr is not None and k not in l0 and not no_llm
                and pr.get("summary") is not None
                and pr.get("summary_sha") == pool.content_sha(it)
                and pr.get("summary_prompt") == ptag
                and pr.get("summary_model") == cfg.get("model")):
            rows[k] = _pool_summary_row(pr, aliases)
            aux["summary_hits"] += 1
        else:
            llm_todo.append(it)

    # l0/no-llm 条目本地兜底先行——它们绝不能进批量 prompt（占位行也不回池）
    llm_items: list[dict] = []
    for it in llm_todo:
        if it["item_key"] in l0 or no_llm:
            rows[it["item_key"]] = summary_row(
                it, None, [], cfg, aliases, l0_hit=it["item_key"] in l0)
        else:
            llm_items.append(it)

    if llm_items:
        sb = max(1, int(sum_batch or _SUM_BATCH))
        sbatches = [llm_items[i:i + sb] for i in range(0, len(llm_items), sb)]
        p = p or prog.Prog(None, "filter")
        p.total = len(llm_items)
        p.say(f"summaries: {len(llm_items)} 条待 LLM "
              f"(池命中 {aux['summary_hits']}) — {len(sbatches)} 批×{sb}")
        done = 0

        def _work(b):
            return summary_batch(b, cfg, aliases)

        with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(sbatches)))) as ex:
            results_iter = (ex.map(_work, sbatches)
                            if jobs > 1 else map(_work, sbatches))
            for res in results_iter:
                for it, row, real in res:
                    rows[it["item_key"]] = row
                    if real:
                        aux["fresh_rows"].append(row)
                done += len(res)
                p.tick(done, "summary")
    for it in todo:
        for e in rows[it["item_key"]]["entities"]:
            if e.casefold() not in known:
                unknown.append((e, it["item_key"]))
    return [rows[it["item_key"]] for it in todo], unknown, aux


def _repair_summaries(pconn, episode: str, cfg: dict, aliases: dict,
                      jobs: int, batch_keys: set, cfg_doc: dict,
                      p: prog.Prog | None = None,
                      sum_batch: int = _SUM_BATCH) -> int:
    """结转条目补概要：池内 keep|review + 窗口超集 + summary 缺席的行，
    走同一条 LLM 概要路径写回 items.sqlite——pool-only，不进
    30_summaries（那些键不在当期批）。返回实际写入行数。"""
    wfrom, wto = pool.window_bounds(episode)
    grace_days = int((cfg_doc.get("pool") or {}).get("arrival_grace_days") or 2)
    _sd = (cfg_doc.get("pool") or {}).get("carry_stale_max_days")
    stale_days = 14 if _sd is None else int(_sd)   # 显式 0 = 关闭子句 C
    try:
        ep = datetime.strptime(episode, "%Y-%m-%d").date()
    except ValueError:
        ep = datetime.now(pool.TZ).date()
    grace_from = (ep - timedelta(days=grace_days)).isoformat()
    sfloor = pool.stale_floor(wfrom, stale_days)
    due = [r for r in pool.needs_summary(pconn, episode, wfrom, wto,
                                         grace_from, sfloor, limit=48)
           if r["item_key"] not in batch_keys]
    if not due:
        return 0
    rep_items = [pool.to_raw_item(r) for r in due]
    by_key = {r["item_key"]: it for r, it in zip(due, rep_items)}
    rbatches = [rep_items[i:i + sum_batch]
                for i in range(0, len(rep_items), sum_batch)]
    p = p or prog.Prog(None, "filter")
    p.total = len(rep_items)
    p.say(f"repair: {len(rep_items)} 行结转概要 — {len(rbatches)} 批")
    fresh: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(rbatches)))) as ex:
        results_iter = (ex.map(lambda b: summary_batch(b, cfg, aliases), rbatches)
                        if jobs > 1
                        else map(lambda b: summary_batch(b, cfg, aliases), rbatches))
        for res in results_iter:
            for it, row, real in res:
                if real:
                    fresh.append(row)
            done += len(res)
            p.tick(done, "repair")
    if not fresh:
        return 0
    return pool.write_summaries(pconn, by_key, fresh, episode=episode)


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
    ap.add_argument("--jobs", type=int, default=4, help="LLM 并发数（判定批/概要/修复共用）")
    ap.add_argument("--no-llm", action="store_true", help="不调网关：全部 review（冒烟用）")
    ap.add_argument("--items-db", default=None,
                    help="items.sqlite 路径（默认 config.storage.items_db > state/）")
    ap.add_argument("--no-pool", action="store_true",
                    help="禁用条目池读写（池缓存完全断开，同 --no-llm 的池行为）")
    ap.add_argument("--summary-batch", type=int, default=_SUM_BATCH,
                    help="概要批量条数（默认 10；1=逐条旧路径）")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    t0 = time.time()

    # 输入读全部移进 run_lock：collect 持锁写 10_raw_items 期间 filter 若先读
    # 会拿到旧版/缺席 artifact；锁内读保证见到的是上游完整产物。
    with meta.run_lock(run_dir):
        meta.stage_begin(run_dir, "filter")
        items, bad = load_items(run_dir, args.limit or None)
        rulebook = RULEBOOK_PATH.read_text(encoding="utf-8")
        aliases = normalize.load_aliases()
        cfg = ({"model": "no-llm", "batch_size": 24} if args.no_llm
               else llm.load_cfg(args.config))
        batch_size = args.batch_size or int(cfg.get("batch_size") or 24)

        cfg_path = Path(args.config) if args.config else REPO / "config.yaml"
        if not cfg_path.exists():
            cfg_path = REPO / "config.example.yaml"
        cfg_doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if args.db:
            db_path = Path(args.db)
        else:
            db_path = REPO / ((cfg_doc.get("storage") or {}).get("history_db")
                              or "state/history.sqlite")

        episode = run_dir.name
        is_date = bool(_DATE_RE.fullmatch(episode))
        # --no-llm / --no-pool → conn=None：池零读零写，冒烟判定绝不污染缓存。
        # 池打开失败只告警（file-first：文件管线是真源，同 dedup 约定）。
        pconn = None
        if not (args.no_llm or args.no_pool):
            try:
                pconn = pool.init_db(pool.resolve_path(args.items_db, cfg_doc))
            except Exception as e:
                print(f"[filter] WARN items.sqlite 打开失败，池缓存停用: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        pool_rows = (pool.get_many(pconn, [it["item_key"] for it in items])
                     if pconn is not None else {})
        by_key = {it["item_key"]: it for it in items}

        l0 = l0_lookup(items, db_path)
        fp = prog.Prog(run_dir, "filter")
        verdicts, faux = run_filter(items, l0, cfg, rulebook, batch_size,
                                    args.no_llm, pool_rows, args.jobs, p=fp)
        ordered = [verdicts[it["item_key"]] for it in items]
        for r in ordered:                        # 契约 lint（写完即调 §4）
            FilterVerdict.model_validate(r)
        # 金丝雀：pool-known 跨期再现 ≥20 且过半未命中 → 内容指纹口径疑似漂移
        if (faux["pool_known"] >= 20
                and faux["cache_misses"] > faux["pool_known"] / 2):
            print(f"[filter] WARN content_sha divergence suspected: "
                  f"{faux['pool_known']} pool-known re-arrivals, "
                  f"{faux['cache_misses']} cache misses (>50%)",
                  file=sys.stderr)
        # 判定回写先于 20_filtered 落盘：池里的判定是主记录，文件是可重放投影。
        # 守卫覆盖键以 injection-guard-v1 入账——不冒充 LLM prompt 版本，
        # 下期必重判（守卫行永不喂缓存）。
        if pconn is not None and is_date and faux["judged_keys"]:
            try:
                guarded = faux["guarded_keys"]
                vrows = []
                for k in faux["judged_keys"]:
                    r = verdicts[k]
                    if k in guarded:
                        r = {**r, "prov": {**r["prov"],
                                           "prompt": "injection-guard-v1"}}
                    vrows.append(r)
                n_v = pool.write_verdicts(
                    pconn, by_key, vrows,
                    lambda k: ("injection-guard-v1" if k in guarded
                               else prompts.PROMPT_VERSIONS["filter"]),
                    episode=episode)
                if n_v:
                    print(f"[filter] pool: {n_v} 行判定回写")
            except Exception as e:
                print(f"[filter] WARN items.sqlite 判定回写失败（不影响文件管线）: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        meta.atomic_write(run_dir / OUT_FILTER, _jsonl(ordered))
        meta.stage_done(run_dir, "filter", OUT_FILTER,
                        extra={"n_items": len(items), "l0_hits": len(l0),
                               "bad_lines": len(bad),
                               "kept": sum(1 for r in ordered if r["verdict"] == "keep"),
                               "dropped": sum(1 for r in ordered if r["verdict"] == "drop"),
                               "review": sum(1 for r in ordered if r["verdict"] == "review"),
                               "cache_hits": faux["cache_hits"],
                               "cache_misses": faux["cache_misses"],
                               "pool_known": faux["pool_known"]})

        # 第二相位（概要+结转修补）重新登记 running key——上面的 stage_done
        # 已清掉 "filter"，而概要是最长的一段，没登记会让 TUI 显示"已完成"
        # 且崩溃不留墓碑。相位末尾 stage_done("summaries") 自动清这个 key。
        meta.stage_begin(run_dir, "summaries")
        sp = prog.Prog(run_dir, "filter")
        summaries, unknown, saux = run_summaries(
            items, verdicts, l0, cfg, aliases, args.jobs, args.no_llm, pool_rows,
            sum_batch=args.summary_batch, p=sp)
        for r in summaries:
            Summary.model_validate(r)
        # 概要回写先于 30_summaries 落盘（同上：池是主记录）；只写真 LLM 行。
        if pconn is not None and is_date and saux["fresh_rows"]:
            try:
                n_s = pool.write_summaries(pconn, by_key, saux["fresh_rows"],
                                           episode=episode)
                if n_s:
                    print(f"[filter] pool: {n_s} 行概要回写")
            except Exception as e:
                print(f"[filter] WARN items.sqlite 概要回写失败（不影响文件管线）: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        meta.atomic_write(run_dir / OUT_SUMMARY, _jsonl(summaries))
        # 结转修补：池内 keep|review 无概要的条目补概要（pool-only，不进 30）
        repair_n = 0
        if pconn is not None and is_date:
            try:
                repair_n = _repair_summaries(
                    pconn, episode, cfg, aliases, args.jobs, set(by_key),
                    cfg_doc, p=sp, sum_batch=args.summary_batch)
                if repair_n:
                    print(f"[filter] pool: repair {repair_n} 行结转概要")
            except Exception as e:
                print(f"[filter] WARN 结转概要修补失败（不影响文件管线）: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
        meta.stage_done(run_dir, "summaries", OUT_SUMMARY,
                        extra={"n": len(summaries),
                               "alias_suggestions": len(unknown),
                               "summary_hits": saux["summary_hits"],
                               "repair_n": repair_n})
        if unknown:
            ALIAS_SUGGESTIONS.parent.mkdir(parents=True, exist_ok=True)
            seen, lines = set(), []
            for e, k in unknown:
                if (e, k) in seen:
                    continue
                seen.add((e, k))
                lines.append(meta.dumps_jsonl_row(
                    {"ts": _now(), "entity": e,
                     "item_key": k, "run": run_dir.name}))
            # 读旧+整写 tmp+replace：崩溃不留半行（journal 体量小，重写代价
            # 可忽略）；旧 journal 若有历史坏行则跳过，不阻塞主产物。
            old_lines = ([meta.dumps_jsonl_row(o) for o in
                          meta.iter_jsonl(ALIAS_SUGGESTIONS, errors=[])]
                         if ALIAS_SUGGESTIONS.exists() else [])
            meta.atomic_write(ALIAS_SUGGESTIONS,
                              "\n".join(old_lines + lines) + "\n")
        if pconn is not None:
            pconn.close()

    # ---- 报告 ----
    n = len(items)
    cnt = {v: sum(1 for r in ordered if r["verdict"] == v) for v in VERDICTS}
    print(f"[filter] {run_dir.name}: {n} items in {time.time()-t0:.0f}s | "
          f"L0={len(l0)} keep={cnt['keep']} drop={cnt['drop']} review={cnt['review']} "
          f"| summaries={len(summaries)} alias_new={len(unknown)}"
          + (f" bad_lines={len(bad)}" if bad else "")
          + (f" | pool verdict_cache={faux['cache_hits']}hit/"
             f"{faux['cache_misses']}miss of {faux['pool_known']} "
             f"summary_cache={saux['summary_hits']} repair={repair_n}"
             if pconn is not None else ""))
    for r in ordered:
        it = by_key[r["item_key"]]
        print(f"  {r['verdict']:6s} ai={r['ai_relevance']:.2f} "
              f"nv={r['news_value'] if r['news_value'] is not None else '-':>4} "
              f"{r['item_key'][:8]} {it['title'][:52]} | {';'.join(r['reasons'])[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
