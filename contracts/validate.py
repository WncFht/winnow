#!/usr/bin/env python3
"""Validate every artifact in a run dir: pydantic schema + cross-field rules.

唯一活入口 = validate_run(run_dir)——PLAN §4 run 级跨字段校验器，各 stage
写完产物即调、meta_qa 全量审计复用；幂等、只读。CLI 薄壳：
`uv run contracts/validate.py runs/<date> [--items-db P]`。
"""
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from contracts.models import (AudioManifest, BuildManifest, Cards, DedupVerdict,
                    FilterVerdict, FramesManifest, Issue, RawItem, RawManifest,
                    RenderPlan, RunMeta, Selected, Summary, Timeline, VoiceSeg)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ======================================================================
# PLAN §4 — validate_run(run_dir): run 级跨字段校验器
#
# 供各 stage 写完产物即调；幂等、只读。返回:
#   {"ok": bool,                 # True ⇔ 无 error 级 violation
#    "violations": [{"rule","level","where","msg"}],   # level: error|warn
#    "checked": [lint 过的 artifact 文件名],
#    "skipped": [应存在但缺失、其校验被跳过的 artifact]}
#
# 规则（§4）:
#   schema-lint     每个存在的 artifact 过 pydantic（50_issue/90_* 无 models 定义 → 结构 lint）
#   id-closure      40.kept.item_key ⊆ (30∪35∪38_pool_summaries) 且 ⊆ (10∪38_pool_items);
#                   50.items.id == 40.kept.id;
#                   60.seg.item ⊆ 50.items.id∪{intro,outro};
#                   61.files.seg_id == 60.seg_id; 64.files.item ⊆ 63 ids∪{intro,outro,cover};
#                   (+ 20/30/35/40 item_key ⊆ 10_raw_items∪38_pool_items 引用完整)
#   url-membership  50.items.sources.url ⊆ kept 条目原始 url 集合(url∪url_canon)  [warn]
#   digit-whitelist 50 body/60 text 数字 ⊆ facts[]∪{期号,年份,常见量词}豁免  [warn]
#   coverage        LLM 批式输入条数 == 输出条数（双侧都在场才查）
#   timeline-consistency
#                   62.items ⊆ 50.ids∪{intro,outro}；segs 单调不重叠、
#                   dur==end-start、seg.item 有 span、file 名==seg_id、
#                   segs/overlays ⊂ item span（overlay 越界 warn）、末句 end≤total
#   audio-manifest  61.files↔62.segs：file 一致、dur 差>50ms warn、
#                   text_sha==sha256(60.text)[:16]
#   render-plan     70.video_track 平铺 [0,total] 无洞无重叠（写时
#                   render_plan.validate_plan 之外的事后复核）；三轨 src 均可解析
#   frames-manifest 62.overlays 每 (item,kind) ∈ 64.files（src 存在）∪ missing
#   manifest-hash   63_cards/64_frames/61_audio manifest 所列文件 sha256 复验
#   hash-chain      80.inputs.* == "sha256:"+上游文件当前哈希（陈旧=上游改了没重建）
#   db-consistency  items_db（validate_run 参数 > config.storage.items_db >
#                   state/items.sqlite，与 stages 各 --items-db 约定同）在场且
#                   run_dir 为日期名才查：kept 条目 items.used_in_episode==episode
#                   [error]；>20% 10_raw key 缺席 items 表 → collect-upsert
#                   健康度告警 [warn]
# ======================================================================

PSEUDO_ITEMS = {"intro", "outro", "cover"}  # 非内容伪 item id（序场/尾场/封面帧）

_LINT_JSONL = {
    "10_raw_items.jsonl": RawItem,
    "20_filtered.jsonl": FilterVerdict,
    "30_summaries.jsonl": Summary,
    "35_dedup.jsonl": DedupVerdict,
    "38_pool_items.jsonl": RawItem,       # 条目池结转投影（缺席→空集，老 run 目录不受影响）
    "38_pool_summaries.jsonl": Summary,
    "60_voice_script.jsonl": VoiceSeg,
}
_LINT_JSON = {
    "00_meta.json": RunMeta,
    "11_raw_manifest.json": RawManifest,
    "40_selected.json": Selected,
    "61_audio_manifest.json": AudioManifest,
    "62_timeline.json": Timeline,
    "63_cards.json": Cards,
    "63_cards_manifest.json": FramesManifest,
    "64_frames_manifest.json": FramesManifest,
    "70_render_plan.json": RenderPlan,
    "80_build_manifest.json": BuildManifest,
}
_LINT_GENERIC_JSON = ["90_title_candidates.json", "90_qa.json"]  # meta/1, qa/1 无 models
_LINT_BINARY = ["90_cover.png"]

_SLUG_RE = re.compile(r"^[a-z0-9-]{2,24}$")
# 千分位逗号归并优先；3.14159 / 2.8 / 424,911 都按一个 token 取
_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_IDENT_EDGE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_MAGN = {"万": 1e4, "亿": 1e8, "k": 1e3, "K": 1e3,
         "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9}
# 常见量词（数字紧跟这些字符 → 豁免）：计数/日期/序数语境，非事实 claim
_QUANT_CHARS = set("个只条款位名次种类型套天周月日号倍层部台件篇则起例版代季轮档"
                   "元角分秒页行字段节章集站校院厂省市县人岁期")
_ISSUE_ITEM_REQ = ("id", "section", "headline", "tldr", "body", "sources")


def _iter_numbers(text):
    """yield (value, raw, start, end)；逗号分位已归并为单值。"""
    for m in _NUM_RE.finditer(text):
        yield float(m.group(0).replace(",", "")), m.group(0), m.start(), m.end()


def _facts_numbers(fact_strs):
    """facts[] 片段中的数字 → 允许值集合（含 万/亿/k/m/b 量级展开值）。"""
    vals = set()
    for f in fact_strs:
        for v, _raw, _s, e in _iter_numbers(f):
            vals.add(v)
            tail = f[e:e + 1]
            if tail in _MAGN:
                vals.add(v * _MAGN[tail])
    return vals


def _num_allowed(text, v, s, e, allowed, meta_nums):
    """单处数字是否豁免：facts 值 / 年份 / 期号等元数据 / 第N 序数 /
    标识符内嵌(GPT-6,Qwen3.8,29B) / 常见量词语境。"""
    close = lambda a: math.isclose(v, a, rel_tol=1e-9, abs_tol=1e-12)
    if any(close(a) for a in allowed):
        return True
    if v.is_integer() and 1900 <= v <= 2100:          # 年份
        return True
    if any(close(a) for a in meta_nums):              # 期号/issue_url/date 元数据
        return True
    j = s - 1                                          # 第N 条/期/名…（跳过 md 强调符）
    while j >= 0 and text[j] in " *`":
        j -= 1
    if j >= 0 and text[j] == "第":
        return True
    a, b = s, e                                        # 标识符 token 内嵌数字
    while a > 0 and text[a - 1] in _IDENT_EDGE:
        a -= 1
    while b < len(text) and text[b] in _IDENT_EDGE:
        b += 1
    if any(c.isascii() and c.isalpha() for c in text[a:b]):
        return True
    k = e                                              # 常见量词语境
    while k < len(text) and text[k] in " *`":
        k += 1
    return k < len(text) and text[k] in _QUANT_CHARS


def _load_jsonl(run, name, V):
    """读 JSONL → (rows, parse_ok)；解析失败记 lint error。"""
    rows, ok = [], True
    with open(run / name, encoding="utf-8", newline=None) as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as ex:
                V("schema-lint", "error", name, f"第{i}行 JSON 解析失败: {ex}")
                ok = False
    return rows, ok


def _resolve_items_db(items_db=None) -> Path:
    """显式参数 > config.storage.items_db（config.yaml > config.example.yaml）
    > state/items.sqlite；相对路径基于 repo 根（stages --items-db 同约定）。

    yaml 惰性 import 且全 try 兜底——contracts 只硬依赖 pydantic，无 yaml /
    无配置文件时安静落到默认池路径。"""
    if items_db:
        p = Path(items_db)
        return p if p.is_absolute() else REPO_ROOT / p
    for name in ("config.yaml", "config.example.yaml"):
        f = REPO_ROOT / name
        if not f.is_file():
            continue
        rel = None
        try:
            import yaml
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            rel = (doc.get("storage") or {}).get("items_db")
        except Exception:
            rel = None
        if rel:
            p = Path(str(rel))
            return p if p.is_absolute() else REPO_ROOT / p
        break
    return REPO_ROOT / "state" / "items.sqlite"


def validate_run(run_dir, items_db=None):
    """run 级跨字段校验。items_db 显式给定时 db-consistency 查该池
    （scratch 池/非默认配置用），否则按 _resolve_items_db 解析。"""
    run = Path(run_dir)
    violations, checked, skipped = [], [], []
    if not run.is_dir():
        return {"ok": False,
                "violations": [{"rule": "io", "level": "error",
                                "where": str(run), "msg": "run_dir 不存在"}],
                "checked": [], "skipped": []}

    def V(rule, level, where, msg):
        violations.append({"rule": rule, "level": level,
                           "where": where, "msg": msg})

    # ---------- schema lint（每个存在的 artifact） ----------
    models, raw = {}, {}
    for name, m in _LINT_JSONL.items():
        if not (run / name).exists():
            skipped.append(name)
            continue
        checked.append(name)
        rows, ok = _load_jsonl(run, name, V)
        raw[name] = rows
        parsed = []
        for i, r in enumerate(rows, 1):
            try:
                parsed.append(m.model_validate(r))
            except Exception as ex:
                V("schema-lint", "error", name,
                  f"第{i}行 schema 校验失败: {str(ex)[:200]}")
        models[name] = parsed
        if not ok or len(parsed) != len(rows):
            models[name] = parsed  # 保留可解析部分继续跨字段检查
    for name, m in _LINT_JSON.items():
        if not (run / name).exists():
            skipped.append(name)
            continue
        checked.append(name)
        try:
            doc = json.loads((run / name).read_text())
            raw[name] = doc
        except json.JSONDecodeError as ex:
            V("schema-lint", "error", name, f"JSON 解析失败: {ex}")
            continue
        try:
            models[name] = m.model_validate(doc)
        except Exception as ex:
            V("schema-lint", "error", name, f"schema 校验失败: {str(ex)[:300]}")

    issue = None
    if (run / "50_issue.json").exists():
        checked.append("50_issue.json")
        try:
            issue = json.loads((run / "50_issue.json").read_text())
        except json.JSONDecodeError as ex:
            V("schema-lint", "error", "50_issue.json", f"JSON 解析失败: {ex}")
        if isinstance(issue, dict):
            if issue.get("schema") not in ("issue/1", "issue/v1"):
                V("schema-lint", "error", "50_issue.json",
                  f"schema={issue.get('schema')!r} ≠ issue/1|issue/v1")
            elif issue.get("schema") == "issue/1":
                try:
                    Issue.model_validate(issue)
                except Exception as ex:
                    V("schema-lint", "error", "50_issue.json",
                      f"schema 校验失败: {str(ex)[:300]}")
            else:   # legacy issue/v1：浅校验
                for req in ("sections", "items"):
                    if req not in issue:
                        V("schema-lint", "error", "50_issue.json", f"缺顶层字段 {req}")
                for it in issue.get("items", []):
                    iid = it.get("id", "?")
                    for k in _ISSUE_ITEM_REQ:
                        if k not in it:
                            V("schema-lint", "error", "50_issue.json",
                              f"item {iid} 缺字段 {k}")
                    if not _SLUG_RE.fullmatch(str(iid)):
                        V("schema-lint", "error", "50_issue.json",
                          f"item id {iid!r} 不符合 slug 规范")
    else:
        skipped.append("50_issue.json")

    for name in _LINT_GENERIC_JSON:
        if not (run / name).exists():
            skipped.append(name)
            continue
        checked.append(name)
        try:
            doc = json.loads((run / name).read_text())
            if not (isinstance(doc, dict) and isinstance(doc.get("schema"), str)):
                V("schema-lint", "error", name, "缺 \"schema\":\"<name>/<v>\" 首字段")
        except json.JSONDecodeError as ex:
            V("schema-lint", "error", name, f"JSON 解析失败: {ex}")
    for name in _LINT_BINARY:
        if (run / name).exists():
            checked.append(name)
            if (run / name).stat().st_size == 0:
                V("schema-lint", "error", name, "文件为空")
        else:
            skipped.append(name)

    def M(name):
        return models.get(name, [])

    raws, filts, sums, deds = (M("10_raw_items.jsonl"), M("20_filtered.jsonl"),
                             M("30_summaries.jsonl"), M("35_dedup.jsonl"))
    pool_raws, pool_sums = M("38_pool_items.jsonl"), M("38_pool_summaries.jsonl")
    sel, vs, am = (models.get("40_selected.json"), M("60_voice_script.jsonl"),
                   models.get("61_audio_manifest.json"))
    cards, fm = models.get("63_cards.json"), models.get("64_frames_manifest.json")
    tl, rp = models.get("62_timeline.json"), models.get("70_render_plan.json")
    cm, bm = models.get("63_cards_manifest.json"), models.get("80_build_manifest.json")

    # ---------- id closure ----------
    ten_keys = {r.item_key for r in raws}
    raw_keys = ten_keys | {r.item_key for r in pool_raws}
    if len(ten_keys) != len(raws):
        V("id-closure", "error", "10_raw_items.jsonl", "item_key 不唯一")
    for name, rows in (("20_filtered.jsonl", filts), ("30_summaries.jsonl", sums),
                       ("35_dedup.jsonl", deds)):
        for r in rows:
            if r.item_key not in raw_keys:
                V("id-closure", "error", name,
                  f"item_key {r.item_key} 不在 10_raw_items∪38_pool_items")
    if sel is not None:
        kept_keys = {k.item_key for k in sel.kept}
        pool = ({r.item_key for r in sums} | {r.item_key for r in deds}
                | {r.item_key for r in pool_sums})
        for kk in sorted(kept_keys - pool):
            V("id-closure", "error", "40_selected.json",
              f"kept.item_key {kk} 不在 30∪35∪38_pool_summaries")
        for k in sel.kept:
            if k.item_key not in raw_keys:
                V("id-closure", "error", "40_selected.json",
                  f"kept.item_key {k.item_key} 不在 10∪38_pool_items")
        kept_ids = [k.id for k in sel.kept]
        if len(set(kept_ids)) != len(kept_ids):
            V("id-closure", "error", "40_selected.json", "kept.id 重复")
        if issue is not None:
            issue_ids = [i["id"] for i in issue.get("items", [])]
            if set(issue_ids) != set(kept_ids):
                V("id-closure", "error", "50_issue.json",
                  f"items.id 与 kept.id 不等: "
                  f"缺 {sorted(set(kept_ids) - set(issue_ids))} "
                  f"多 {sorted(set(issue_ids) - set(kept_ids))}")
    issue_ids = {i["id"] for i in issue.get("items", [])} if issue else set()
    for s in vs:
        if s.item not in issue_ids and s.item not in ("intro", "outro"):
            V("id-closure", "error", "60_voice_script.jsonl",
              f"{s.seg_id} item {s.item} 不在 50.items.id∪intro/outro")
    if am is not None and vs:
        am_ids, vs_ids = {f.seg_id for f in am.files}, {s.seg_id for s in vs}
        if am_ids != vs_ids:
            V("id-closure", "error", "61_audio_manifest.json",
              f"files.seg_id 与 60.seg_id 不等: 缺 {sorted(vs_ids - am_ids)[:8]} "
              f"多 {sorted(am_ids - vs_ids)[:8]}")
    if fm is not None and cards is not None:
        card_ids = {c.id for c in cards.items}
        for f in fm.files:
            if f.item not in card_ids and f.item not in PSEUDO_ITEMS:
                V("id-closure", "error", "64_frames_manifest.json",
                  f"files.item {f.item} 不在 63.items.id∪{sorted(PSEUDO_ITEMS)}")

    # ---------- url membership ----------
    if issue is not None and sel is not None and (raws or pool_raws):
        kept_keys = {k.item_key for k in sel.kept}
        urls = set()
        for r in (*raws, *pool_raws):
            if r.item_key in kept_keys:
                urls.update({r.url, r.url_canon,
                             r.url.rstrip("/"), r.url_canon.rstrip("/")})
        for it in issue.get("items", []):
            for src in it.get("sources", []):
                u = src.get("url")
                if u and u not in urls:
                    V("url-membership", "warn", f"50_issue:{it['id']}",
                      f"sources.url 不在 kept 条目原始 url 集合: {u}")

    # ---------- digit whitelist ----------
    if issue is not None:
        fact_strs = [f for s in sums for f in s.facts]
        fact_strs += [f for s in pool_sums for f in s.facts]
        fact_strs += [f for it in issue.get("items", []) for f in it.get("facts", [])]
        allowed = _facts_numbers(fact_strs)
        meta_txt = " ".join(str(issue.get(k, ""))
                            for k in ("issue_url", "date", "episode"))
        meta_nums = {v for v, _r, _s, _e in _iter_numbers(meta_txt)}
        seen = set()

        def scan(text, where):
            for v, rawtok, s, e in _iter_numbers(text):
                if _num_allowed(text, v, s, e, allowed, meta_nums):
                    continue
                key = (where, rawtok)
                if key in seen:
                    continue
                seen.add(key)
                ctx = text[max(0, s - 12):min(len(text), e + 12)]
                V("digit-whitelist", "warn", where,
                  f"数字 {rawtok} ∉ facts[]∪豁免集 (…{ctx}…)")

        for it in issue.get("items", []):
            w = f"50_issue:{it['id']}"
            scan(it.get("headline", ""), w)
            scan(it.get("tldr", ""), w)
            for b in it.get("body", []):
                scan(b, w)
            for t in it.get("voice", []):
                scan(t, w)
        for r in vs:
            scan(r.text, f"60:{r.seg_id}")

    # ---------- coverage（双侧在场才查） ----------
    if raws and filts:
        fkeys = {r.item_key for r in filts}
        # coverage 只覆盖当期采集批（10_raw）；38_pool_* 是结转投影，不进此比较
        if len(filts) != len(raws) or fkeys != ten_keys:
            V("coverage", "error", "20_filtered.jsonl",
              f"输入 {len(raws)} 条 vs 输出 {len(filts)} 条; "
              f"缺 {sorted(ten_keys - fkeys)[:8]} 多 {sorted(fkeys - ten_keys)[:8]}")
    if filts and sums:
        expect = {r.item_key for r in filts if r.verdict in ("keep", "review")}
        skeys = {r.item_key for r in sums}
        if expect - skeys:
            V("coverage", "error", "30_summaries.jsonl",
              f"keep|review 输入 {len(expect)} 条 vs 输出 {len(skeys)} 条; "
              f"缺 {sorted(expect - skeys)[:8]}")
        if skeys - expect:
            V("coverage", "warn", "30_summaries.jsonl",
              f"存在非 keep|review 条目的 summary: {sorted(skeys - expect)[:8]}")
    if sums and deds:
        dkeys = {r.item_key for r in deds}
        miss = {r.item_key for r in sums} - dkeys
        if miss:
            V("coverage", "error", "35_dedup.jsonl",
              f"30 输入条目无 dedup verdict: {sorted(miss)[:8]}")
    if issue is not None and vs:
        want = {it["id"]: len(it.get("voice", [])) for it in issue.get("items", [])}
        want["intro"] = len(issue.get("intro", {}).get("voice", []))
        want["outro"] = len(issue.get("outro", {}).get("voice", []))
        got = Counter(s.item for s in vs)
        for iid, n in want.items():
            if got.get(iid, 0) != n:
                V("coverage", "error", "60_voice_script.jsonl",
                  f"item {iid}: 50.voice {n} 句 vs 60.seg {got.get(iid, 0)} 句")
    if issue is not None and cards is not None:
        if {c.id for c in cards.items} != issue_ids:
            V("coverage", "error", "63_cards.json",
              f"items.id 与 50.items.id 不等: 缺 {sorted(issue_ids - {c.id for c in cards.items})} "
              f"多 {sorted({c.id for c in cards.items} - issue_ids)}")
    if cards is not None and fm is not None:
        have = {(f.item, f.kind) for f in fm.files}
        miss = set(fm.missing)
        for c in cards.items:
            if (c.id, "card") not in have and f"{c.id}.card" not in miss:
                V("coverage", "warn", "64_frames_manifest.json",
                  f"{c.id}.card 既无帧文件也不在 missing[]")

    # ---------- timeline-consistency（62 内部自洽；全部在场才查） ----------
    if tl is not None:
        tl_ids = {i.id for i in tl.items}
        if issue is not None:
            extra = tl_ids - issue_ids - {"intro", "outro"}
            if extra:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"items 超出 50.items∪intro/outro: {sorted(extra)[:8]}")
        for i in tl.items:
            if i.visual and i.visual not in tl_ids:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"item {i.id} visual 绑定 {i.visual} 不存在")
        span = {i.id: i for i in tl.items}
        tprev = -1.0
        for s in tl.segs:
            if s.start < tprev:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"seg {s.seg_id} start {s.start} < 上一句 end {tprev}"
                  "（重叠/乱序）")
            if abs((s.end - s.start) - s.dur) > 0.001:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"seg {s.seg_id} dur 字段与 end-start 不一致")
            sp = span.get(s.item)
            if sp is None:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"seg {s.seg_id} item {s.item} 无 item span")
            elif not (sp.start <= s.start and s.end <= sp.end + 1e-6):
                V("timeline-consistency", "error", "62_timeline.json",
                  f"seg {s.seg_id} 越出 item span {s.item}")
            if s.file.split("/")[-1].rsplit(".", 1)[0] != s.seg_id:
                V("timeline-consistency", "error", "62_timeline.json",
                  f"seg {s.seg_id} file 名 {s.file} 与 seg_id 不同构")
            tprev = s.end
        for o in tl.overlays:
            sp = span.get(o.item)
            if not sp or not (sp.start <= o.start and o.end <= sp.end + 1e-6):
                V("timeline-consistency", "warn", "62_timeline.json",
                  f"overlay {o.item}.{o.kind} 窗口越出 item span")
        if tl.segs and tl.segs[-1].end > tl.total:
            V("timeline-consistency", "error", "62_timeline.json",
              "末句 end > total")

    # ---------- audio-manifest ↔ timeline（61 file/dur/text_sha 对账） ----------
    if tl is not None and am is not None:
        am_files = {f.seg_id: f for f in am.files}
        for s in tl.segs:
            f = am_files.get(s.seg_id)
            if not f:
                V("audio-manifest", "error", "61_audio_manifest.json",
                  f"seg {s.seg_id} 无对应 audio 文件条目")
                continue
            if f.file != s.file:
                V("audio-manifest", "error", "61_audio_manifest.json",
                  f"seg {s.seg_id} file 字段与 timeline 不一致: "
                  f"{s.file} vs {f.file}")
            if abs(f.dur - s.dur) > 0.05:
                V("audio-manifest", "warn", "61_audio_manifest.json",
                  f"seg {s.seg_id} manifest dur {f.dur} vs timeline "
                  f"dur {s.dur} 差 >50ms")
            if f.text_sha != hashlib.sha256(s.text.encode()).hexdigest()[:16]:
                V("audio-manifest", "error", "61_audio_manifest.json",
                  f"seg {s.seg_id} 文本 hash 与 voice_script/timeline 不符")

    # ---------- render-plan（70 平铺复核 + 三轨 src 可解析） ----------
    if rp is not None:
        cur = 0.0
        for vseg in sorted(rp.video_track, key=lambda x: x.start):
            if vseg.start - cur > 0.05:
                V("render-plan", "error", "70_render_plan.json",
                  f"video_track 在 {cur:.3f}-{vseg.start:.3f} 有洞")
            if vseg.start < cur - 0.001:
                V("render-plan", "error", "70_render_plan.json",
                  f"video_track 在 {vseg.start:.3f} 重叠")
            cur = max(cur, vseg.end)
        if abs(cur - rp.total) > 0.05:
            V("render-plan", "error", "70_render_plan.json",
              f"video_track 覆盖到 {cur:.3f} ≠ total {rp.total}")
        seen_src = set()
        for tr in (*rp.video_track, *rp.audio_track, *rp.overlay_track):
            if tr.src in seen_src:
                continue
            seen_src.add(tr.src)
            if not (run / tr.src).exists():
                V("render-plan", "error", "70_render_plan.json",
                  f"引用 {tr.src} 不存在")

    # ---------- frames-manifest ↔ timeline overlays ----------
    if tl is not None and fm is not None:
        fm_by_kind = {(f.item, f.kind): f for f in fm.files}
        fm_missing = set(fm.missing)
        for o in tl.overlays:
            if (o.item, o.kind) in fm_by_kind:
                if not (run / o.src).exists():
                    V("frames-manifest", "error", "62_timeline.json",
                      f"overlay {o.item}.{o.kind} src {o.src} 不存在")
            elif f"{o.item}.{o.kind}" not in fm_missing:
                V("frames-manifest", "error", "62_timeline.json",
                  f"overlay {o.item}.{o.kind} 既不在 frames manifest "
                  "也不在 missing")

    # ---------- manifest-hash（清单所列文件 sha256 复验） ----------
    for name, mf in (("63_cards_manifest.json", cm),
                     ("64_frames_manifest.json", fm)):
        if mf is None:
            continue
        for f in mf.files:
            p = run / f.path
            if not p.exists():
                V("manifest-hash", "error", name, f"{f.path} 不存在")
                continue
            if _sha256_file(p) != f.sha256:
                V("manifest-hash", "error", name, f"{f.path} sha256 不匹配")
    if am is not None:
        for f in am.files:
            p = run / f.file
            if not p.exists():
                V("manifest-hash", "error", "61_audio_manifest.json",
                  f"audio {f.file} 不存在")
            elif _sha256_file(p) != f.sha256:
                V("manifest-hash", "error", "61_audio_manifest.json",
                  f"audio {f.file} sha256 不匹配")

    # ---------- hash-chain（80.inputs 与上游文件当前 sha 对齐） ----------
    if bm is not None:
        for k, vv in bm.inputs.items():
            target = run / {"render_plan": "70_render_plan.json",
                            "timeline": "62_timeline.json",
                            "audio_manifest": "61_audio_manifest.json",
                            "frames_manifest": "64_frames_manifest.json"}.get(k, k)
            if target.exists() and vv != "sha256:" + _sha256_file(target):
                V("hash-chain", "error", "80_build_manifest.json",
                  f"inputs.{k} 哈希陈旧（上游改了没重建）")

    # ---------- db-consistency（条目池在场才查；只读连接） ----------
    episode = run.name
    db = _resolve_items_db(items_db)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", episode) and db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True,
                                   timeout=5)
            try:
                if sel is not None:
                    for k in sel.kept:
                        row = conn.execute(
                            "SELECT used_in_episode FROM items"
                            " WHERE item_key=?", (k.item_key,)).fetchone()
                        if row is None or row[0] != episode:
                            V("db-consistency", "error", "40_selected.json",
                              f"kept.item_key {k.item_key} items.used_in_episode="
                              f"{row[0] if row else None!r} ≠ {episode}")
                if ten_keys:
                    ks, present = sorted(ten_keys), set()
                    for i in range(0, len(ks), 500):
                        q = ",".join("?" * len(ks[i:i + 500]))
                        present.update(
                            r[0] for r in conn.execute(
                                "SELECT item_key FROM items"
                                f" WHERE item_key IN ({q})", ks[i:i + 500]))
                    miss = len(ten_keys) - len(present)
                    if miss / len(ten_keys) > 0.2:
                        V("db-consistency", "warn", str(db),
                          f"10_raw {miss}/{len(ten_keys)} key 不在 items 表"
                          "（collect-upsert 健康度超 20% 缺席线）")
            finally:
                conn.close()
        except sqlite3.Error as ex:
            V("db-consistency", "warn", str(db),
              f"池读取失败，本组检查跳过: {ex}")

    ok = not any(v["level"] == "error" for v in violations)
    return {"ok": ok, "violations": violations,
            "checked": checked, "skipped": skipped}


def _print_report(run_dir, rep):
    errs = [v for v in rep["violations"] if v["level"] == "error"]
    wrns = [v for v in rep["violations"] if v["level"] == "warn"]
    print(f"validate_run {run_dir}")
    print(f"checked: {len(rep['checked'])} artifacts"
          + (f" | skipped(absent): {', '.join(rep['skipped'])}" if rep["skipped"] else ""))
    print(f"== {len(errs)} errors, {len(wrns)} warnings ==")
    for v in errs:
        print(f"ERR  [{v['rule']}] {v['where']}: {v['msg']}")
    for v in wrns:
        print(f"WARN [{v['rule']}] {v['where']}: {v['msg']}")
    print("OK" if rep["ok"] else "NOT OK")


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(
        description="run 级跨字段校验器（PLAN §4）")
    _ap.add_argument("run_dir", help="runs/<date> 目录")
    _ap.add_argument("--items-db", default=None, metavar="P",
                     help="条目池 items.sqlite 路径"
                          "（默认 config.storage.items_db > state/items.sqlite）")
    _args = _ap.parse_args()
    _rep = validate_run(_args.run_dir, items_db=_args.items_db)
    _print_report(_args.run_dir, _rep)
    sys.exit(0 if _rep["ok"] else 1)
