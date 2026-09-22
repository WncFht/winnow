# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""gate_select — 人工闸 1（PLAN.md §7.3）。

输入 20_filtered.jsonl + 30_summaries.jsonl + 35_dedup.jsonl
（+ 10_raw_items.jsonl 取 url/源名）→ `40_candidates.json`（UI 数据源，非契约）。
人工/自动勾选 → `40_selected.json`（契约 selected/1）。

候选集 = filter verdict∈{keep,review} 且 dedup verdict∉{suppressed}
（dedup 的 gray/gray_pending 自动进列表并打灰区标记）。

用法：
  gate_select.py --run-dir runs/<date>            # = --prepare：重建 40_candidates.json
  gate_select.py --run-dir R --prepare           # 同上
  gate_select.py --run-dir R --serve             # prepare + 启动 review_server 勾选 UI
  gate_select.py --run-dir R --auto [--force]    # top-K by news_value → decided_by:auto
  gate_select.py --run-dir R --deadline-check HH:MM  # 死线已过且未提交 → 自动放行（供 timer 调用）
  gate_select.py --selftest                      # fixture 端到端自测（含 schema 断言）

不覆盖原则：40_selected.json 已存在时 --auto/--deadline-check 直接跳过（人工已拍板），
--force 可强制重判。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root -> contracts/adapters
from lib import meta  # stages/lib/meta.py（stages/ 即 sys.path 脚本目录）

REPO = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")

RAW_NAME = "10_raw_items.jsonl"
FILT_NAME = "20_filtered.jsonl"
SUMS_NAME = "30_summaries.jsonl"
DED_NAME = "35_dedup.jsonl"
CAND_NAME = "40_candidates.json"
SEL_NAME = "40_selected.json"

GRAY = {"gray", "gray_pending"}
SUPPRESSED = {"suppressed"}
SLUG_RE = re.compile(r"^[a-z0-9-]{2,24}$")
KEY_RE = re.compile(r"^[0-9a-f]{16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def eprint(*a):
    print(*a, file=sys.stderr)


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


def resolve_run_dir(s: str) -> Path:
    """'YYYY-MM-DD' -> runs/<date>；否则按路径解析（相对路径基于 repo 根）。"""
    p = Path(s)
    if DATE_RE.match(s):
        return meta.ensure_run(s)
    return meta.ensure_run(p)


def load_config() -> dict:
    """config.yaml > config.example.yaml；只取本阶段需要的 schedule.* 字段。"""
    import yaml  # lazy：review_server import 本模块时无 yaml 也能跑

    cfg = {}
    for name in ("config.yaml", "config.example.yaml"):
        f = REPO / name
        if f.exists():
            try:
                cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception as e:
                eprint(f"[gate_select] warn: {name} 解析失败 {e} — 用默认值")
            break
    sch = cfg.get("schedule") or {}
    return {
        "topk_autopick": int(sch.get("topk_autopick") or 14),
        "max_items": int(sch.get("max_items") or 20),
        "gate1_deadline": str(sch.get("gate1_deadline") or "08:30"),
    }


def slugify_id(text: str, item_key: str = "") -> str:
    """任意文本 -> ^[a-z0-9-]{2,24}$；中文/空文本回退 'n'+item_key[:8]。"""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    t = re.sub(r"-{2,}", "-", t)
    if len(t) > 24:
        t = t[:24].rstrip("-")
    if len(t) < 2:
        t = ("n" + item_key[:8]) if item_key else "item-0"
    return t


def unique_slug(base: str, item_key: str, taken: set[str]) -> str:
    s = base
    if s in taken:
        s = f"{base[:19]}-{item_key[:4]}"
    i = 2
    while s in taken:
        s = f"{base[:20]}-{i}"
        i += 1
    taken.add(s)
    return s


def _section_vocab() -> list:
    """lib.prompts.SECTION_VOCAB（lazy import——本模块被 review_server 当工具箱用时也要能跑）。"""
    try:
        from lib.prompts import SECTION_VOCAB
        return SECTION_VOCAB
    except Exception:
        return []


def section_slug(text: str, item_key: str = "") -> str:
    """任意分区文本 -> 合法 slug（下游 issue.sections[].slug 要求 ^[a-z0-9-]+$）。

    SECTION_VOCAB 的 slug / 中文名原样映射（'模型发布'->'model-release'）；
    其余文本走 slugify_id；纯非 ASCII 文本（slugify 只剩 key 兜底）归 'misc'。
    """
    t = str(text or "").strip()
    if not t:
        return "misc"
    for slug, name in _section_vocab():
        if t == slug or t == name:
            return slug
    s = slugify_id(t, item_key)
    if not SLUG_RE.match(s) or s in ("item-0", "n" + item_key[:8]):
        return "misc"
    return s


def lint_selected(doc: dict) -> list[str]:
    """轻量 selected/1 校验（review_server 复用，无需 pydantic）。返回错误列表。"""
    errs = []
    if doc.get("schema") != "selected/1":
        errs.append("schema != selected/1")
    if not DATE_RE.match(str(doc.get("episode", ""))):
        errs.append("episode 非 YYYY-MM-DD")
    if not doc.get("decided_at"):
        errs.append("缺 decided_at")
    if doc.get("decided_by") not in ("human", "auto"):
        errs.append("decided_by 非 human|auto")
    kept = doc.get("kept")
    if not isinstance(kept, list) or len(kept) < 1:
        errs.append("kept 为空（§11 零条目停刊路径）")
    ids = set()
    for k in kept or []:
        if not KEY_RE.match(str(k.get("item_key", ""))):
            errs.append(f"kept.item_key 非法: {k.get('item_key')!r}")
        if not SLUG_RE.match(str(k.get("id", ""))):
            errs.append(f"kept.id 非 slug: {k.get('id')!r}")
        elif k["id"] in ids:
            errs.append(f"kept.id 重复: {k['id']}")
        ids.add(k.get("id"))
        if not isinstance(k.get("section"), str) or not k["section"]:
            errs.append(f"kept.section 空: {k.get('id')}")
        if "note" not in k:
            errs.append(f"kept.note 字段缺失: {k.get('id')}")
    if not isinstance(doc.get("dropped"), list):
        errs.append("dropped 非数组")
    return errs


def pydantic_validate(doc: dict) -> list[str]:
    """contracts.models.Selected 严格校验；pydantic 不可用则跳过。返回错误列表。"""
    try:
        from contracts.models import Selected
    except Exception:
        return []
    try:
        Selected.model_validate(doc)
        return []
    except Exception as e:
        return [str(e).splitlines()[0] if str(e) else "pydantic validation failed"]


# ---------------------------------------------------------- build candidates

def build_candidates(run_dir: Path) -> dict:
    """20+30+35(+10) -> candidates envelope。缺输入即 fail-fast（文件即依赖边）。"""
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

    cfg = load_config()
    episode = run_dir.name
    candidates, suppressed, skipped = [], [], []
    for f in filts:
        key = f.get("item_key", "")
        fv = f.get("verdict")
        d = ded.get(key)
        dv = (d or {}).get("verdict") or "fresh"  # 无 dedup 行按 fresh
        if dv in SUPPRESSED:
            suppressed.append({"item_key": key,
                               "cluster_id": (d or {}).get("cluster_id"),
                               "match_cos": (d or {}).get("match_cos")})
            continue
        if fv not in ("keep", "review"):
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
                  "n_dropped_by_filter": len(skipped)},
        "candidates": candidates,
        "suppressed": suppressed,
    }


def cmd_prepare(run_dir: Path) -> int:
    env = build_candidates(run_dir)
    meta.atomic_write(run_dir / CAND_NAME, env)
    meta.stage_done(run_dir, "gate_prepare", CAND_NAME, status="done")
    st = env["stats"]
    print(f"[gate_select] {CAND_NAME}: {st['n_candidates']} 候选 "
          f"(suppressed {st['n_suppressed']}, filter-drop {st['n_dropped_by_filter']})")
    return 0


# ------------------------------------------------------------- write selected

def write_selected(run_dir: Path, kept_cands: list[dict], dropped_cands: list[dict],
                   decided_by: str, drop_reason: str) -> int:
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
    print(f"[gate_select] {SEL_NAME}: kept={len(kept_cands)} "
          f"dropped={len(dropped_cands)} by={decided_by}"
          + (" [kept 为空 — §11 no_items]" if not kept_cands else ""))
    return 0


def cmd_auto(run_dir: Path, force: bool = False, topk: int | None = None) -> int:
    sel = run_dir / SEL_NAME
    if sel.exists() and not force:
        try:
            by = json.loads(sel.read_text(encoding="utf-8")).get("decided_by")
        except Exception:
            by = "?"
        print(f"[gate_select] {SEL_NAME} 已存在 (decided_by={by}) — 跳过 (--force 可覆盖)")
        return 0
    env = build_candidates(run_dir)
    meta.atomic_write(run_dir / CAND_NAME, env)  # 同步刷新 UI 数据源
    cfg = env["config"]
    k = min(topk or cfg["topk_autopick"], cfg["max_items"])
    order = sorted(env["candidates"],
                   key=lambda c: (-(c["news_value"] if isinstance(c.get("news_value"), (int, float)) else -1),
                                  -(c.get("ai_relevance") or 0), c["item_key"]))
    kept, dropped = order[:k], order[k:]
    return write_selected(run_dir, kept, dropped, "auto", "below_topk")


def cmd_deadline_check(run_dir: Path, hhmm: str) -> int:
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
    return cmd_auto(run_dir)


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
    import hashlib
    url = f"https://example.com/news/{i}{url_suffix}"
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    return key, {
        "schema": "raw_item/1", "item_key": key, "id": key, "url": url,
        "url_canon": url, "title": f"GPT-{i} released with benchmark {10+i}%",
        "content_text": f"fixture body {i}", "date_published": None,
        "date_fetched": "2099-01-01T01:00:00+08:00",
        "_source": {"name": f"Src{i}", "feed_url": "https://example.com/feed", "kind": "rss"},
        "_fetch": {"status": 200, "via": "direct", "reachable": True},
    }


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
    ap.add_argument("--selftest", action="store_true", help="fixture 端到端自测")
    args = ap.parse_args(argv)

    if args.selftest:
        return cmd_selftest()
    if not args.run_dir:
        ap.error("--run-dir 必填（--selftest 除外）")
    run_dir = resolve_run_dir(args.run_dir)

    if args.deadline_check:
        with _run_lock(run_dir):
            return cmd_deadline_check(run_dir, args.deadline_check)
    if args.auto:
        with _run_lock(run_dir):
            return cmd_auto(run_dir, force=args.force, topk=args.topk)
    if args.serve:
        with _run_lock(run_dir):
            rc = cmd_prepare(run_dir)
        if rc != 0:
            return rc
        # 服务器持锁会阻塞 pick-auto 死线 watcher —— 锁外启动（justfile 同款约定）
        return cmd_serve_no_prepare(run_dir)
    # 默认 = --prepare
    with _run_lock(run_dir):
        return cmd_prepare(run_dir)


def cmd_serve_no_prepare(run_dir: Path) -> int:
    server = REPO / "stages" / "review_server.py"
    print("[gate_select] 启动 review_server（Ctrl-C 退出）…")
    return subprocess.call([sys.executable, str(server), "--run-dir", str(run_dir)],
                           env=dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
