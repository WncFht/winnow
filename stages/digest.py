# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6", "jsonschema>=4.20", "httpx>=0.27"]
# ///
"""stages/digest.py — PLAN.md §7.4：勾选条目 → issue/1 + 编辑闸往返 + 投影 + 合规。

CLI（文件即依赖边，缺输入即 fail-fast 提示先跑哪个 just 目标。三个分支
是独立 stage 调用——just 目标 digest/edit-import/callb 依次进，00_meta
登记名分别 digest/digest_import/digest_callb，另侧写 digest_export/
digest_flags）：

    uv run stages/digest.py --run-dir runs/<date>            # Call A + 导出 50_review.md
    uv run stages/digest.py --run-dir runs/<date> --import   # 人改后回编 → 锁 50_issue.json
    uv run stages/digest.py --run-dir runs/<date> --callb    # 投影 voice/cards/video + 合规 → 90_qa.flags
    uv run stages/digest.py --run-dir runs/<date> --force    # Call A 且强制重导 review.md（覆盖人工编辑，破坏性）
    uv run stages/digest.py --run-dir runs/<date> --selftest # 离线自检（不打 LLM）
    # 公共 flag：--config PATH（llm 配置，缺省 config.yaml→example）

Call A（spine）：CALLA_PROMPT(merged kept + summaries + raw content + facts + rulebook)
  → llm.chat_json（max_tokens 24000；kept>14 → 32000）→ issue/1 骨架。
  id-indirection：prompt 只给 <item_data id=slug> + "链接: uN=label"，LLM 回
  sources[]={"item":slug}|{"ref":"slug#uN"}；本文件把 slug→真实 URL 回填进
  sources[]（reachable 留给 link-check，不写）。数字白名单：body 数字 ⊆
  NUM(emitted facts ∪ 输入 facts/summary/title/content) → 违例进 flags（非硬错）。
  覆盖：set(items.id)==set(kept.id)，不等 → 带差异提示重试一次；仍不等 → fail。

review.md 往返（experiments/issue-contract 定型格式）：
  <!-- issue DATE | ... --> 头；## item:<id> <!-- section|confidence -->；
  ### headline/tldr/body/voice/cards。--import 重跑全部校验 + 数字白名单复检；
  edited 标志 = 当前 md sha256 ≠ 导出时记录值（存 00_meta stages.digest_export）。
  Call A 重跑时已被编辑的 review.md 默认保留不覆盖；--force 才强制重导
  （破坏性：人工编辑丢失）。

Call B（--callb，编辑闸之后的独立 stage 调用：just callb → 00_meta
  digest_callb）：CALLB_PROMPT → intro/outro voice + 每条
  voice[]/cards{mainTitle→title_short,cards[title→label,desc→body,icon]}/
  video.shot_sentences。口播 deterministic 修："字母-数字"连字符拆开（GPT-6→GPT 6）；
  cards desc <strong>/<code> 转 markdown **/`（issue 存 md 方言）。
  shot_sentences 按"卡定场→证据→卡收尾"收口：首句恒留给信息卡、≥3 句时
  末句也留卡；LLM 漏条用 tldr 兜底口播并记 flag。
  合规 pass：sensitive_words.txt 确定性扫 + COMPLIANCE_PROMPT → 90_qa.flags seed。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import jsonschema

from adapters import llm_swe2max as llm
from lib import meta, prog, prompts

try:
    from lib import normalize
except Exception:  # pragma: no cover - aliases are optional polish
    normalize = None

REPO = Path(__file__).resolve().parents[1]
RULEBOOK_PATH = REPO / "rulebook.md"
ALIASES_PATH = REPO / "aliases.json"
SENSITIVE_PATH = REPO / "sensitive_words.txt"

ISSUE_SCHEMA_CANDIDATES = [
    REPO / "contracts" / "schemas" / "issue.schema.json",
    REPO / "experiments" / "issue-contract" / "issue.schema.json",
]

# artifacts this stage owns / reads
F_SELECTED = "40_selected.json"
F_SUMMARIES = "30_summaries.jsonl"
F_RAW = "10_raw_items.jsonl"
F_POOL_ITEMS = "38_pool_items.jsonl"      # 结转池 raw 投影（可选输入）
F_POOL_SUMS = "38_pool_summaries.jsonl"   # 结转池 summary 投影（可选输入）
F_ISSUE = "50_issue.json"
F_REVIEW = "50_review.md"
F_QA = "90_qa.json"

NUM = re.compile(r"\d+(?:\.\d+)?")
RAW_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")
MD_BLOCK = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|>\s|\d+\.\s|```)", re.M)
ITEM_RE = re.compile(r"^## item:([a-z0-9-]+).*$", re.M)
FIELD_RE = re.compile(r"^### (\w+)\s*$", re.M)
# "字母-数字"连字符（TTS 会读成"杠"）；字母-字母保留（Thinker-Talker）
LETTER_DIGIT_HYPHEN = re.compile(r"([A-Za-z])-(\d)|(\d)-([A-Za-z])")
CONFIDENCES = {"confirmed", "reported", "rumor", "speculation"}
REVIEW_FIELDS = ("headline", "tldr", "body", "voice", "cards")

_SECTION_NAME = {slug: name for slug, name in prompts.SECTION_VOCAB}
# source kind 猜测（LLM 给的 kind 优先，非法值回退到这里再退 "other"）
_KIND_BY_HOST = [
    (("github.com", "gitlab.com", "gitee.com"), "repo"),
    (("arxiv.org", "doi.org", "openreview.net", "aclanthology.org"), "paper"),
    (("x.com", "twitter.com", "weibo.com", "weibo.cn", "reddit.com",
      "threads.net", "bsky.app", "zhihu.com", "v2ex.com", "linux.do",
      "t.me", "discord"), "social"),
    (("huggingface.co", "modelscope.cn", "kaggle.com"), "repo"),
    (("news.ycombinator.com", "lobste.rs", "producthunt.com"), "community"),
]


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _load_jsonl(p: Path) -> list:
    return meta.load_jsonl(p)


def _load_jsonl_opt(p: Path) -> list:
    """可选 JSONL（38_pool_* 等）：缺文件 → []；坏行计数告警、不炸整批。"""
    if not p.exists():
        return []
    errs: list = []
    rows = meta.load_jsonl(p, errors=errs)
    if errs:
        sys.stderr.write(f"WARN {p.name}: {len(errs)} 坏行已跳过\n")
    return [r for r in rows if isinstance(r, dict)]


def _die(msg: str, hint: str = "") -> "SystemExit":
    sys.stderr.write(f"ERROR: {msg}\n")
    if hint:
        sys.stderr.write(f"HINT: {hint}\n")
    return SystemExit(2)


def _need(run_dir: Path, name: str, hint: str) -> Path:
    p = run_dir / name
    if not p.exists():
        raise _die(f"{run_dir}/{name} 缺失", hint)
    return p


def _numbers(text: str) -> set:
    return set(NUM.findall(text or ""))


def _weekday_cn(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return ""
    return "周" + "一二三四五六日"[d.weekday()]


def _strip_inline_html(text: str) -> str:
    """<strong>/<code> → markdown 方言（issue 只存 **粗** `码`；其余标签剥壳）。"""
    t = re.sub(r"</?strong>", "**", text)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r"</?(em|b|i|s|u)>", "**", t)
    return t


def _flag(item: str, kind: str, severity: str, snippet: str, reason: str) -> dict:
    return {"id": item, "kind": kind, "severity": severity,
            "snippet": str(snippet)[:80], "reason": str(reason)[:60],
            "stage": "digest", "at": _utcnow()}


# ---------------------------------------------------------------------------
# inputs: merge 40_selected + 30_summaries + 10_raw_items → prompt dicts
# ---------------------------------------------------------------------------

def _guess_kind(url: str, src_kind: str = "") -> str:
    if src_kind == "manual":
        return "other"
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    for doms, kind in _KIND_BY_HOST:
        if any(d in host for d in doms):
            return kind
    if src_kind in ("rss", "atom", "api"):
        return "media"
    return "other"


def _load_aliases() -> dict:
    if normalize and ALIASES_PATH.exists():
        try:
            return normalize.load_aliases(ALIASES_PATH)
        except Exception:
            pass
    return {}


def build_kept(run_dir: Path) -> tuple[list[dict], dict]:
    """kept[] 有序 → merged dicts（prompt 输入 + URL 回填表 + 输入侧数字池）。"""
    sel_p = _need(run_dir, F_SELECTED, "先跑 `just pick` 或 `just pick-auto`（40 勾选闸）")
    _need(run_dir, F_RAW, "先跑 `just gather`（collect 产出 10_raw_items.jsonl）")
    _need(run_dir, F_SUMMARIES, "先跑 `just gather`（filter 产出 30_summaries.jsonl）")
    selected = _load_json(sel_p)
    raws = {r["item_key"]: r for r in _load_jsonl(run_dir / F_RAW)}
    sums = {s["item_key"]: s for s in _load_jsonl(run_dir / F_SUMMARIES)}
    # 结转池兜底（可选）：38_pool_* 提供往期结转条目的 raw/summary；
    # 键冲突时当期文件赢（同 item_key 当期版本更新）
    raws = {**{r["item_key"]: r for r in _load_jsonl_opt(run_dir / F_POOL_ITEMS)
               if r.get("item_key")}, **raws}
    sums = {**{s["item_key"]: s for s in _load_jsonl_opt(run_dir / F_POOL_SUMS)
               if s.get("item_key")}, **sums}
    aliases = _load_aliases()

    kept = []
    for k in selected.get("kept", []):
        ik, slug = k["item_key"], k["id"]
        raw = raws.get(ik)
        if raw is None:
            raise _die(f"kept.item_key {ik} ({slug}) 不在 10_raw_items.jsonl / "
                       f"{F_POOL_ITEMS}",
                       "raw_items/pool_items 与 selected 不一致——重跑 `just pick`")
        s = sums.get(ik, {})
        title = s.get("title_zh") or raw.get("title") or ""
        summary = s.get("summary") or ""
        facts = list(s.get("facts") or [])
        entities = list(s.get("entities") or [])
        content = raw.get("content_text") or ""
        if aliases and normalize:
            title = normalize.apply_aliases(title, aliases)
            summary = normalize.apply_aliases(summary, aliases)
            content = normalize.apply_aliases(content, aliases)
        src_name = (raw.get("_source") or {}).get("name") or "来源"
        src_kind = (raw.get("_source") or {}).get("kind") or ""
        links = [{"label": str(src_name), "kind": _guess_kind(raw.get("url", ""), src_kind),
                  "url": raw.get("url", "")}]
        kept.append({
            "id": slug, "item_key": ik, "section": k.get("section") or s.get("section_guess") or "",
            "note": k.get("note"), "title": title, "title_zh": title,
            "summary": summary, "facts": facts, "entities": entities,
            "content_text": content, "links": links,
            "image": raw.get("image"), "raw_url": raw.get("url", ""),
            "url_canon": raw.get("url_canon", ""),
            "src_kind": src_kind, "date_published": raw.get("date_published"),
            "confidence_hint": (raw.get("tags") or [None])[-1],
        })
    ctx = {"selected": selected, "episode": selected.get("episode") or run_dir.name,
           "aliases": aliases}
    return kept, ctx


def _input_num_pool(item: dict) -> set:
    """该条输入材料里的全部数字（facts+summary+title+content+links label）。"""
    pool = set()
    texts = ([item.get("title", ""), item.get("summary", ""), item.get("content_text", "")]
             + list(item.get("facts") or []))
    for t in texts:
        pool |= _numbers(t)
    return pool


# ---------------------------------------------------------------------------
# issue/1 validation: schema + cross-field + digit whitelist -> (errors, warns, flags)
# ---------------------------------------------------------------------------

_ISSUE_SCHEMA = None


def _issue_schema() -> dict | None:
    global _ISSUE_SCHEMA
    if _ISSUE_SCHEMA is None:
        for p in ISSUE_SCHEMA_CANDIDATES:
            if p.exists():
                _ISSUE_SCHEMA = _load_json(p)
                break
    return _ISSUE_SCHEMA


def validate_issue(doc: dict, kept_by_id: dict, *, check_digits: bool = True
                   ) -> tuple[list, list, list]:
    """errors=阻断写盘；warns=打印；flags=进 90_qa.flags（数字白名单等软违例）。"""
    errors, warns, flags = [], [], []
    schema = _issue_schema()
    if schema:
        for e in jsonschema.Draft202012Validator(schema).iter_errors(doc):
            errors.append("schema: /" + "/".join(map(str, e.absolute_path)) + " -> " + e.message)
    else:
        warns.append("issue.schema.json 未找到，跳过 schema 层校验")

    slugs = [s.get("slug") for s in doc.get("sections", [])]
    ids = [it.get("id") for it in doc.get("items", [])]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"duplicate item id: {dup}")

    kept_urls = {it.get("raw_url") for it in kept_by_id.values()} - {""}
    kept_canon = {it.get("url_canon") for it in kept_by_id.values()} - {""}

    for it in doc.get("items", []):
        iid = it.get("id", "?")
        if it.get("section") not in slugs:
            errors.append(f"{iid}: section {it.get('section')!r} 不在 sections[]")
        srcs = it.get("sources", [])
        if not srcs:
            warns.append(f"{iid}: sources 为空（源已删？编辑标记）")
        if sum(1 for s in srcs if s.get("primary")) > 1:
            errors.append(f"{iid}: 多于一个 primary source")
        for s in srcs:
            u = s.get("url", "")
            if not re.match(r"^https?://", u):
                errors.append(f"{iid}: bad source url {u!r}")
            elif u not in kept_urls and u not in kept_canon:
                errors.append(f"{iid}: sources.url {u!r} 不在该期勾选条目的原始 url 集合")
            if s.get("kind") and s["kind"] not in prompts.SOURCE_KINDS:
                errors.append(f"{iid}: source kind {s['kind']!r} 非法")
            if s.get("reachable") is False:
                warns.append(f"{iid}: source unreachable: {u}")

        spine_text = " ".join([it.get("headline", ""), it.get("tldr", "")]
                              + it.get("body", []) + it.get("facts", []))
        spine = _numbers(spine_text)
        for j, sent in enumerate(it.get("voice", []) or []):
            if not sent.strip():
                errors.append(f"{iid}: voice[{j}] 空句")
            if RAW_TAG.search(sent) or MD_BLOCK.search(sent) or "**" in sent or "`" in sent:
                errors.append(f"{iid}: voice[{j}] 含标记（口播须纯文本）")
            for n in _numbers(sent) - spine:
                flags.append(_flag(iid, "number_not_in_spine", "medium",
                                   sent[:60], f"voice[{j}] 数字 {n} 不在 spine+facts"))
        for j, c in enumerate(it.get("cards", []) or []):
            for n in _numbers(c.get("body", "")) - spine:
                flags.append(_flag(iid, "number_not_in_spine", "medium",
                                   c.get("body", "")[:60], f"cards[{j}] 数字 {n} 不在 spine+facts"))
            if RAW_TAG.search(c.get("body", "")):
                errors.append(f"{iid}: cards[{j}].body 含裸 HTML（存 markdown 方言）")
            if len(c.get("label", "")) > 7:
                warns.append(f"{iid}: cards[{j}].label >7 字")
        for ss in (it.get("video") or {}).get("shot_sentences", []) or []:
            if ss < 1 or ss > len(it.get("voice", []) or []):
                errors.append(f"{iid}: shot_sentences {ss} 越界（voice {len(it.get('voice', []))} 句）")
        for fld in [it.get("tldr", "")] + list(it.get("body", []) or []):
            if RAW_TAG.search(fld):
                errors.append(f"{iid}: tldr/body 含裸 HTML 标签")
        if "\n" in it.get("tldr", ""):
            errors.append(f"{iid}: tldr 含换行")

        # 数字白名单：spine 数字 ⊆ NUM(emitted facts ∪ 输入材料) —— 违例只 flag。
        # 注意 spine 本身含 body，不能把 body 并入 pool（否则检查退化成恒真）。
        if check_digits:
            src = kept_by_id.get(iid)
            if src:
                in_pool = _input_num_pool(src)
                # emitted facts 数字也应能在输入材料中找到（LLM 不许新造 claim）
                for n in _numbers(" ".join(it.get("facts", []))) - in_pool:
                    flags.append(_flag(iid, "fact_not_in_input", "high",
                                       n, "emitted facts 数字不在输入材料，疑似新造 claim"))
                pool = in_pool | _numbers(" ".join(it.get("facts", [])))
                for n in _numbers(" ".join([it.get("headline", ""), it.get("tldr", "")]
                                           + it.get("body", []))) - pool:
                    flags.append(_flag(iid, "number_not_in_facts", "high",
                                       n, "headline/tldr/body 数字不在 facts ∪ 输入材料"))
    return errors, warns, flags


# ---------------------------------------------------------------------------
# Call A — issue/1 spine
# ---------------------------------------------------------------------------

def _strip_unknown_keys(doc: dict) -> dict:
    """按 schema additionalProperties:false 剥掉 LLM 多输出的键（确定性修复）。"""
    top = {"schema", "date", "weekday", "lang", "issue_url", "video_links",
           "cover", "sections", "intro", "outro", "items", "degraded"}
    for k in list(doc.keys()):
        if k not in top:
            doc.pop(k)
    item_keys = {"id", "section", "nav", "headline", "title_short", "tldr",
                 "body", "sources", "media", "confidence", "entities", "facts",
                 "voice", "cards", "video"}
    sec_keys = {"slug", "name", "icon"}
    src_keys = {"url", "label", "kind", "primary", "reachable"}
    for s in doc.get("sections", []) or []:
        for k in list(s.keys()):
            if k not in sec_keys:
                s.pop(k)
    for it in doc.get("items", []) or []:
        for k in list(it.keys()):
            if k not in item_keys:
                it.pop(k)
        for s in it.get("sources", []) or []:
            for k in list(s.keys()):
                if k not in src_keys:
                    s.pop(k)
    return doc


def _map_sources(emitted: list, item: dict, kept_by_id: dict,
                 flags: list) -> list:
    """LLM 的 {"item":slug}|{"ref":"slug#uN"} → 真 URL；裸 URL 输出按幻觉处理。"""
    iid = item["id"]
    out = []

    def _urls_for(slug: str) -> list:
        tgt = kept_by_id.get(slug) or item
        return tgt.get("links") or []

    for s in emitted or []:
        if not isinstance(s, dict):
            continue
        ref = s.get("ref") or s.get("item") or ""
        slug, _, un = str(ref).partition("#")
        url = None
        if slug:
            links = _urls_for(slug)
            if un:
                try:
                    url = links[int(un[1:]) - 1]["url"] if un.startswith("u") else None
                except (ValueError, IndexError):
                    url = None
            elif links:
                url = links[0]["url"]
        elif s.get("url"):
            u = str(s["url"])
            if u == item.get("raw_url") or u == item.get("url_canon"):
                url = item["raw_url"]
            else:
                flags.append(_flag(iid, "url_hallucinated", "high", u,
                                   "sources 输出了输入之外的 URL，已用条目原始链接替换"))
                url = item.get("raw_url")
        if not url:
            url = item.get("raw_url")
        if not url:
            continue
        kind = s.get("kind") if s.get("kind") in prompts.SOURCE_KINDS else _guess_kind(url, item.get("src_kind", ""))
        entry = {"url": url, "kind": kind}
        if s.get("label"):
            entry["label"] = str(s["label"])
        if s.get("primary") is True:
            entry["primary"] = True
        out.append(entry)
    if not out and item.get("raw_url"):
        out.append({"url": item["raw_url"], "kind": _guess_kind(item["raw_url"], item.get("src_kind", "")),
                    "primary": True})
    # dedupe by url；保证恰好 ≤1 primary，0 个则首个补
    seen, dedup = set(), []
    for s in out:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        dedup.append(s)
    if dedup and not any(s.get("primary") for s in dedup):
        dedup[0]["primary"] = True
    elif sum(1 for s in dedup if s.get("primary")) > 1:
        first = True
        for s in dedup:
            if s.get("primary") and not first:
                s.pop("primary")
            elif s.get("primary"):
                first = False
    return dedup


def _normalize_item(raw_it: dict, item: dict, kept_by_id: dict, flags: list) -> dict:
    """LLM item → issue item：字段纠型 + sources 回填 + 确定性修复。"""
    iid = item["id"]
    it = dict(raw_it) if isinstance(raw_it, dict) else {}
    out = {
        "id": iid,
        "section": item["section"],   # kept.section 是人工闸的决定，强制沿用
        "nav": str(it.get("nav") or item.get("title", ""))[:12] or iid,
        "headline": " ".join(str(it.get("headline") or item.get("title", "")).split()),
        "tldr": " ".join(str(it.get("tldr") or item.get("summary", "")).split()),
        "body": [],
        "sources": _map_sources(it.get("sources"), item, kept_by_id, flags),
        "confidence": it.get("confidence") if it.get("confidence") in CONFIDENCES else "reported",
    }
    body = it.get("body")
    if isinstance(body, str):
        body = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not body:
        body = [item.get("summary") or item.get("title") or iid]
    out["body"] = [_strip_inline_html(str(p)) for p in body]
    out["tldr"] = _strip_inline_html(out["tldr"])
    out["headline"] = _strip_inline_html(out["headline"])
    if it.get("entities"):
        out["entities"] = [str(e) for e in it["entities"]][:20]
    elif item.get("entities"):
        out["entities"] = item["entities"][:20]
    if it.get("facts"):
        out["facts"] = [str(f) for f in it["facts"]][:30]
    elif item.get("facts"):
        out["facts"] = item["facts"][:30]
    if it.get("title_short"):
        out["title_short"] = str(it["title_short"])
    # media：确定性回填 og:image（不经 LLM）
    if item.get("image"):
        out["media"] = [{"kind": "image", "src": item["image"],
                         "origin": item.get("raw_url") or None}]
        out["media"] = [{k: v for k, v in m.items() if v} for m in out["media"]]
    if out["section"] != str(it.get("section") or out["section"]):
        flags.append(_flag(iid, "section_overridden", "low",
                           str(it.get("section")), "LLM 改了人工指定分区，已回写 kept.section"))
    return out


def _normalize_issue(doc: dict, kept: list, ctx: dict, flags: list) -> dict:
    kept_by_id = {it["id"]: it for it in kept}
    doc = _strip_unknown_keys(doc if isinstance(doc, dict) else {})
    out = {
        "schema": "issue/1",
        "date": ctx["episode"],
        "weekday": _weekday_cn(ctx["episode"]),
        "lang": doc.get("lang") or "zh-CN",
        "sections": [],
        "items": [],
    }
    if doc.get("issue_url"):
        out["issue_url"] = str(doc["issue_url"])
    # sections：LLM 声明优先，保证 kept.section 全覆盖（引用完整性）
    seen = set()
    for s in doc.get("sections", []) or []:
        if isinstance(s, dict) and s.get("slug") and s["slug"] not in seen:
            seen.add(s["slug"])
            out["sections"].append({"slug": str(s["slug"]),
                                    "name": str(s.get("name") or _SECTION_NAME.get(s["slug"], s["slug"]))})
    emitted = {it.get("id"): it for it in doc.get("items", []) or [] if isinstance(it, dict)}
    for it in kept:
        sec = it["section"] or "top-news"
        if sec not in seen:
            seen.add(sec)
            out["sections"].append({"slug": sec, "name": _SECTION_NAME.get(sec, sec)})
    for it in kept:
        raw_it = emitted.get(it["id"]) or {}
        out["items"].append(_normalize_item(raw_it, it, kept_by_id, flags))
    return out


def call_a(run_dir: Path, cfg: dict, *, force: bool = False) -> Path:
    kept, ctx = build_kept(run_dir)
    if not kept:
        # §11 零条目停刊路径
        doc = {"schema": "issue/1", "date": ctx["episode"],
               "weekday": _weekday_cn(ctx["episode"]), "lang": "zh-CN",
               "degraded": True, "sections": [{"slug": "none", "name": "停刊"}],
               "items": [{"id": "none", "section": "none", "nav": "停刊",
                          "headline": "今日无入选条目", "tldr": "今日无入选条目。",
                          "body": ["今日无入选条目。"], "sources": [],
                          "confidence": "confirmed"}]}
        meta.atomic_write(run_dir / F_ISSUE, doc)
        meta.stage_done(run_dir, "digest", F_ISSUE, status="done",
                        extra={"degraded": True, "kept": 0})
        print("kept==0 → degraded 停刊 issue（跳过 CallA/B）")
        return run_dir / F_ISSUE

    rulebook = RULEBOOK_PATH.read_text(encoding="utf-8") if RULEBOOK_PATH.exists() else ""
    episode = ctx["episode"]
    system, user = prompts.CALLA_PROMPT(kept, rulebook, episode=episode,
                                        weekday=_weekday_cn(episode))
    n = len(kept)
    max_tokens = 32000 if n > 14 else int(cfg.get("max_tokens", 24000))
    provs: list = []
    msgs = prompts.messages(system, user)
    kept_ids = {it["id"] for it in kept}
    doc, last_err = None, None
    p = prog.Prog(run_dir, "digest", total=n,
                  step=min(100, max(10, n // 40)), interval=30)

    for attempt in range(2):
        p.say(f"Call A → llm.chat_json attempt {attempt + 1}/2 "
              f"(kept={n}, max_tokens={max_tokens})")
        try:
            out = llm.chat_json(msgs, retries=2, prov_out=provs, tag="calla",
                                cfg=cfg, max_tokens=max_tokens)
        except llm.LLMError as e:
            raise _die(f"Call A LLM 调用失败: {e}",
                       "网关异常时稍后重跑 `just digest`（D1 无跨模型 fallback）") from e
        if not isinstance(out, dict):
            last_err = f"Call A 输出非对象: {type(out)}"
        else:
            # 覆盖校验查的是 LLM 输出的 id（normalize 后恒等于 kept，不能拿它查）
            got_ids = {i.get("id") for i in out.get("items", []) or []
                       if isinstance(i, dict)}
            if got_ids == kept_ids:
                flags0: list = []
                doc = _normalize_issue(out, kept, ctx, flags0)
                doc["_flags0"] = flags0
                break
            missing, extra = kept_ids - got_ids, got_ids - kept_ids
            last_err = f"coverage 不齐 缺{sorted(missing)} 多{sorted(extra)}"
            doc = None
        if attempt == 0:
            p.say(f"Call A 校验失败，带反馈重试: {last_err}")
            msgs = msgs + [{"role": "assistant", "content": "(invalid)"},
                           {"role": "user", "content":
                            f"上次输出错误：{last_err}。items[] 必须恰好覆盖 {n} 条，"
                            f"id 集合 = {sorted(kept_ids)}。只输出修正后的完整 JSON 对象。"}]
    if doc is None:
        raise _die(f"Call A 两次输出 coverage 校验失败: {last_err}",
                   "检查网关或缩小 kept 条数")
    flags0 = doc.pop("_flags0", [])

    errors, warns, flags = validate_issue(doc, {it["id"]: it for it in kept})
    flags = flags0 + flags
    if errors:
        for e in errors:
            sys.stderr.write("ERR  " + e + "\n")
        raise _die("Call A 产物未过校验（见上）", "人工查看后重跑 `just digest` 或直接修 JSON")
    for w in warns:
        sys.stderr.write("WARN " + w + "\n")

    meta.atomic_write(run_dir / F_ISSUE, doc)
    print(f"Call A ok: {len(doc['items'])} items, {len(doc['sections'])} sections, "
          f"{len(flags)} soft-flags")

    # 导出人编面 review.md；已存在且被改过则保留（--force 才覆盖）
    md = export_review(doc)
    review_p = run_dir / F_REVIEW
    prev_sha = meta.meta_status(run_dir).get("stages", {}).get(
        "digest_export", {}).get("review_sha256")
    cur_sha = _sha(review_p.read_text(encoding="utf-8")) if review_p.exists() else None
    if review_p.exists() and not force and prev_sha and cur_sha != prev_sha:
        sys.stderr.write("WARN 50_review.md 已被编辑，保留不覆盖（--force 重新导出）\n")
    else:
        meta.atomic_write(review_p, md)
    meta.stage_done(run_dir, "digest", F_ISSUE, status="done",
                    extra={"kept": n, "calla_prov": provs[-1] if provs else None,
                           "prompt": prompts.PROMPT_VERSIONS["call_a"],
                           "input_sha": _sha(user)[:16]})
    meta.stage_done(run_dir, "digest_export", F_REVIEW, status="done",
                    extra={"review_sha256": _sha((run_dir / F_REVIEW).read_text(encoding="utf-8"))})
    if flags:
        _append_qa_flags(run_dir, flags, episode=doc.get("date", ctx["episode"]))
    p.close()
    return run_dir / F_ISSUE


# ---------------------------------------------------------------------------
# review.md export/import（格式定型于 experiments/issue-contract）
# ---------------------------------------------------------------------------

def export_review(doc: dict) -> str:
    sec_name = {s["slug"]: s["name"] for s in doc.get("sections", [])}
    L = [f"<!-- issue {doc['date']} | edit fields, keep '## item:<id>' and '### <field>' headers intact -->\n"]
    for it in doc.get("items", []):
        L.append(f"## item:{it['id']}   <!-- section: {sec_name.get(it['section'])} | confidence: {it['confidence']} -->")
        L.append("### headline")
        L.append(it["headline"] + "\n")
        L.append("### tldr")
        L.append(it["tldr"] + "\n")
        L.append("### body")
        L.append("\n\n".join(it["body"]) + "\n")
        if it.get("voice") is not None:
            L.append("### voice")
            L.extend("- " + s for s in it.get("voice", []))
            L.append("")
        if it.get("cards"):
            L.append("### cards")
            L.extend(f"- {c['label']} | {c.get('icon', '')} | {c['body']}" for c in it["cards"])
            L.append("")
        L.append("")
    return "\n".join(L)


def import_review(doc: dict, md: str) -> dict:
    """md → doc：未出现字段无损；未知 id/字段/坏行 → 硬错。"""
    chunks = re.split(ITEM_RE, md)
    by_id = {it["id"]: it for it in doc.get("items", [])}
    seen = set()
    for i in range(1, len(chunks), 2):
        iid, block = chunks[i], chunks[i + 1]
        it = by_id.get(iid)
        if it is None:
            raise _die(f"review.md 引用未知 item id {iid!r}", "恢复 '## item:<id>' 头")
        seen.add(iid)
        fields = re.split(FIELD_RE, block)
        for j in range(1, len(fields), 2):
            name, text = fields[j], fields[j + 1].strip()
            if name not in REVIEW_FIELDS:
                raise _die(f"item {iid}: 未知字段 {name!r}",
                           f"只允许 {REVIEW_FIELDS}")
            if name == "voice":
                it["voice"] = [l[2:].strip() for l in text.splitlines()
                               if l.strip().startswith("- ")]
            elif name == "cards":
                cards = []
                for l in text.splitlines():
                    l = l.strip()
                    if not l.startswith("- "):
                        continue
                    parts = [p.strip() for p in l[2:].split("|")]
                    if len(parts) != 3:
                        raise _die(f"item {iid}: 卡片行格式错 {l!r}",
                                   "应为 'label | icon | body'")
                    cards.append({"label": parts[0], "icon": parts[1], "body": parts[2]})
                it["cards"] = cards
            elif name == "body":
                it["body"] = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            else:
                it[name] = " ".join(text.split())
    missing = set(by_id) - seen
    if missing:
        sys.stderr.write(f"WARN review.md 缺 items（保持原值）: {sorted(missing)}\n")
    return doc


def do_import(run_dir: Path) -> Path:
    issue_p = _need(run_dir, F_ISSUE, "先跑 `just digest`（Call A 产 50_issue.json）")
    review_p = _need(run_dir, F_REVIEW, "先跑 `just digest`（导出 50_review.md）")
    doc = _load_json(issue_p)
    md = review_p.read_text(encoding="utf-8")
    doc = import_review(doc, md)

    kept, ctx = build_kept(run_dir)
    errors, warns, flags = validate_issue(doc, {it["id"]: it for it in kept})
    for w in warns:
        sys.stderr.write("WARN " + w + "\n")
    if errors:
        for e in errors:
            sys.stderr.write("ERR  " + e + "\n")
        raise _die("编辑后复检未过（未写盘）", "修 50_review.md 后重跑 `just edit-import`")

    prev_sha = meta.meta_status(run_dir).get("stages", {}).get(
        "digest_export", {}).get("review_sha256")
    edited = _sha(md) != prev_sha if prev_sha else True
    meta.atomic_write(issue_p, doc)
    meta.stage_done(run_dir, "digest_import", F_ISSUE, status="done",
                    extra={"edited": edited, "review_sha256": _sha(md)})
    if flags:
        _append_qa_flags(run_dir, flags, episode=doc.get("date", run_dir.name))
    print(f"import ok: edited={edited}, {len(doc.get('items', []))} items, "
          f"{len(flags)} soft-flags → 锁 {F_ISSUE}")
    return issue_p


# ---------------------------------------------------------------------------
# Call B — projections: voice / cards(GeneratedContent) / video.shot_sentences
# ---------------------------------------------------------------------------

def _tts_text_fix(sent: str, iid: str, j: int, flags: list) -> str:
    s = " ".join(str(sent).split())
    if LETTER_DIGIT_HYPHEN.search(s):
        s2 = LETTER_DIGIT_HYPHEN.sub(
            lambda m: (m.group(1) or m.group(3)) + " " + (m.group(2) or m.group(4)), s)
        flags.append(_flag(iid, "voice_hyphen_fixed", "low", s[:50],
                           f"voice[{j}] 字母-数字连字符已拆开"))
        s = s2
    if len(s) > 45:
        flags.append(_flag(iid, "voice_too_long", "medium", s[:50],
                           f"voice[{j}] {len(s)} 字 >45"))
    return s


def _map_cards(gc: dict, it: dict, flags: list) -> None:
    """GeneratedContent → issue title_short + cards[{label,body(md),icon}]。"""
    iid = it["id"]
    if not isinstance(gc, dict):
        return
    mt = str(gc.get("mainTitle") or "").strip()
    if mt:
        if not (2 <= len(mt) <= 8):
            flags.append(_flag(iid, "mainTitle_len", "low", mt,
                               f"mainTitle {len(mt)} 字，spec 2-8"))
        it["title_short"] = mt
    cards = []
    for c in gc.get("cards", []) or []:
        if not isinstance(c, dict):
            continue
        title = " ".join(str(c.get("title") or "").split())
        desc = " ".join(str(c.get("desc") or "").split())
        icon = str(c.get("icon") or "").strip()
        if icon and icon not in prompts.ICON_ALLOWLIST:
            flags.append(_flag(iid, "icon_offlist", "low", icon,
                               "icon 不在 Material Symbols 白名单，回退 article"))
            icon = "article"
        body = _strip_inline_html(desc)
        if len(body) and not (20 <= len(body) <= 60):
            flags.append(_flag(iid, "card_desc_len", "low", body[:40],
                               f"card desc {len(body)} 字，spec 20-40(60上限)"))
        if title or body:
            cards.append({"label": title, "body": body, "icon": icon or "article"})
    if cards:
        it["cards"] = cards[:8]


def _fallback_voice(it: dict) -> list:
    """Call B 漏条兜底：tldr 去标记截断为一句，保证 60 号不空转。"""
    t = re.sub(r"\*\*|`", "", it.get("tldr") or it.get("headline") or it["id"])
    t = t.strip().rstrip("。") + "。"
    return [t]


def call_b(run_dir: Path, cfg: dict) -> Path:
    issue_p = _need(run_dir, F_ISSUE,
                    "先跑 `just digest`（Call A）+ `just edit-import`（编辑闸）")
    doc = _load_json(issue_p)
    kept, ctx = build_kept(run_dir)
    items = doc.get("items", [])
    if not items or doc.get("degraded"):
        print("degraded/空 issue：跳过 Call B")
        return issue_p

    system, user = prompts.CALLB_PROMPT(doc)
    n = len(items)
    provs: list = []
    msgs = prompts.messages(system, user)
    want_ids = {it["id"] for it in items}
    out, last_err = None, None
    p = prog.Prog(run_dir, "digest", total=n,
                  step=min(100, max(10, n // 40)), interval=30)
    for attempt in range(2):
        p.say(f"Call B → llm.chat_json attempt {attempt + 1}/2 (items={n})")
        try:
            out = llm.chat_json(msgs, retries=2, prov_out=provs, tag="callb",
                                cfg=cfg, max_tokens=32000 if n > 14 else int(cfg.get("max_tokens", 24000)))
        except llm.LLMError as e:
            raise _die(f"Call B LLM 调用失败: {e}",
                       "网关异常时稍后重跑 --callb（D1 无跨模型 fallback）") from e
        if isinstance(out, dict):
            got = {i.get("id") for i in out.get("items", []) or [] if isinstance(i, dict)}
            if want_ids <= got:
                break
            last_err = f"coverage 缺 {sorted(want_ids - got)}"
        else:
            last_err = f"输出非对象 {type(out)}"
        out = None
        if attempt == 0:
            p.say(f"Call B 校验失败，带反馈重试: {last_err}")
            msgs = msgs + [{"role": "assistant", "content": "(invalid)"},
                           {"role": "user", "content":
                            f"上次输出错误：{last_err}。items[] 必须覆盖全部 {n} 条，"
                            f"id 集合 = {sorted(want_ids)}。只输出修正后的完整 JSON 对象。"}]
    if out is None:
        raise _die(f"Call B 两次 coverage 失败: {last_err}", "网关异常时稍后重跑 --callb")

    flags: list = []
    emitted = {i.get("id"): i for i in out.get("items", []) or [] if isinstance(i, dict)}
    for i, it in enumerate(items):
        p.tick(i, it["id"])
        iid = it["id"]
        e = emitted.get(iid)
        if e is None:
            it["voice"] = _fallback_voice(it)
            it["video"] = {"shot_sentences": [2] if len(it["voice"]) >= 2 else []}
            flags.append(_flag(iid, "callb_missing", "high", "",
                               "Call B 漏条，已用 tldr 兜底口播"))
            continue
        voice = [_tts_text_fix(s, iid, j, flags)
                 for j, s in enumerate(e.get("voice", []) or []) if str(s).strip()]
        if not voice:
            voice = _fallback_voice(it)
            flags.append(_flag(iid, "callb_empty_voice", "medium", "",
                               "voice 为空，已用 tldr 兜底"))
        it["voice"] = voice
        _map_cards(e.get("cards"), it, flags)
        ss = []
        for x in (e.get("video") or {}).get("shot_sentences", []) or []:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= xi <= len(voice):
                ss.append(xi)
            else:
                flags.append(_flag(iid, "shot_out_of_range", "low", str(x),
                                   f"shot_sentence {xi} 越界 voice {len(voice)}，已丢"))
        # 卡定场→证据→卡收尾（prompts._CALLB_SYS 同款规则）：首句恒留给信息卡，
        # ≥3 句时末句也留卡；2 句条目仅可压第 2 句，1 句不压。
        nseg = len(voice)
        ss = sorted(x for x in set(ss) if x != 1 and not (nseg >= 3 and x == nseg))
        it["video"] = {"shot_sentences": ss or ([2] if nseg >= 2 and it.get("sources") else [])}
    p.tick(n, "voice/cards/video 回填完成", force=True)
    if isinstance(out.get("intro"), dict) and out["intro"].get("voice"):
        doc["intro"] = {"voice": [_tts_text_fix(s, "intro", j, flags)
                                  for j, s in enumerate(out["intro"]["voice"])]}
    if isinstance(out.get("outro"), dict) and out["outro"].get("voice"):
        doc["outro"] = {"voice": [_tts_text_fix(s, "outro", j, flags)
                                  for j, s in enumerate(out["outro"]["voice"])]}

    errors, warns, vflags = validate_issue(doc, {it["id"]: it for it in kept})
    flags += vflags
    for w in warns:
        sys.stderr.write("WARN " + w + "\n")
    if errors:
        for e in errors:
            sys.stderr.write("ERR  " + e + "\n")
        raise _die("Call B 产物校验未过（未写盘）", "检查输出或重跑 --callb")
    meta.atomic_write(issue_p, doc)
    meta.stage_done(run_dir, "digest_callb", F_ISSUE, status="done",
                    extra={"callb_prov": provs[-1] if provs else None,
                           "prompt": prompts.PROMPT_VERSIONS["call_b"],
                           "input_sha": _sha(user)[:16]})
    print(f"Call B ok: voice/cards/video 回填 {len(items)} items, {len(flags)} soft-flags")

    flags += compliance_pass(doc, cfg, p)
    _append_qa_flags(run_dir, flags, episode=doc.get("date", run_dir.name))
    p.close()
    return issue_p


# ---------------------------------------------------------------------------
# 合规 pass（D10）：确定性敏感词扫 + COMPLIANCE_PROMPT → 90_qa.flags
# ---------------------------------------------------------------------------

def _sensitive_words() -> list:
    if not SENSITIVE_PATH.exists():
        return []
    return [w.strip() for w in SENSITIVE_PATH.read_text(encoding="utf-8").splitlines()
            if w.strip() and not w.strip().startswith("#")]


def _item_texts(it: dict) -> list:
    texts = [("headline", it.get("headline", "")), ("tldr", it.get("tldr", ""))]
    texts += [("body", p) for p in it.get("body", []) or []]
    texts += [("voice", s) for s in it.get("voice", []) or []]
    texts += [("cards", c.get("body", "") + " " + c.get("label", ""))
              for c in it.get("cards", []) or []]
    return texts


def compliance_pass(doc: dict, cfg: dict, p: prog.Prog | None = None) -> list:
    flags: list = []
    words = _sensitive_words()
    hits = 0
    scan_items = list(doc.get("items", []))
    for seg_name in ("intro", "outro"):
        seg = doc.get(seg_name)
        if isinstance(seg, dict) and seg.get("voice"):
            scan_items.append({"id": seg_name, "voice": seg["voice"]})
    for it in scan_items:
        for field, text in _item_texts(it):
            for w in words:
                if w in text:
                    hits += 1
                    i = text.find(w)
                    flags.append(_flag(it["id"], "sensitive_word", "high",
                                       text[max(0, i - 15):i + len(w) + 15],
                                       f"{field} 命中敏感词 {w!r}"))
    print(f"sensitive scan: {len(words)} 词 × {len(scan_items)} 条 → {hits} hits")

    items = [it for it in doc.get("items", [])]
    if items:
        try:
            system, user = prompts.COMPLIANCE_PROMPT(items)
            provs: list = []
            if p is not None:
                p.say(f"compliance → llm.chat_json ({len(items)} items)")
            out = llm.chat_json(prompts.messages(system, user), retries=2,
                                prov_out=provs, tag="compliance", cfg=cfg)
            known = {it["id"] for it in items}
            for f in (out or {}).get("flags", []) or []:
                if not isinstance(f, dict):
                    continue
                iid = str(f.get("id") or "")
                if iid not in known:
                    iid = iid or "?"
                flags.append(_flag(iid, str(f.get("kind") or "other"),
                                   str(f.get("severity") or "low"),
                                   str(f.get("snippet") or ""),
                                   str(f.get("reason") or "llm compliance flag")))
        except llm.LLMError as e:
            flags.append(_flag("_compliance", "compliance_llm_unavailable", "low",
                               str(e)[:60], "合规 LLM pass 失败（确定性扫仍生效）"))
            sys.stderr.write(f"WARN compliance LLM pass failed: {e}\n")
    return flags


def _append_qa_flags(run_dir: Path, flags: list, *, episode: str = "") -> None:
    """90_qa.json flags seed：只替换 stage=='digest' 的旧 flags，其它 stage 的不动。"""
    qa_p = run_dir / F_QA
    qa = {"schema": "qa/1", "episode": episode or run_dir.name, "flags": []}
    if qa_p.exists():
        try:
            qa = _load_json(qa_p)
        except Exception:
            pass
        if episode:
            qa["episode"] = episode
    old = [f for f in qa.get("flags", []) if f.get("stage") != "digest"]
    qa["flags"] = old + flags
    qa["produced_at"] = _utcnow()
    qa.setdefault("checks", {})["digest"] = {
        "flags": len(flags), "at": _utcnow(),
        "kinds": sorted({f["kind"] for f in flags})}
    meta.atomic_write(qa_p, qa)
    meta.stage_done(run_dir, "digest_flags", F_QA, status="done",
                    extra={"n_flags": len(flags)})


# ---------------------------------------------------------------------------
# selftest（离线：export→edit→import 往返 + 数字白名单捕获）
# ---------------------------------------------------------------------------

def _selftest() -> None:
    doc = {
        "schema": "issue/1", "date": "2026-09-22", "weekday": "周二", "lang": "zh-CN",
        "sections": [{"slug": "model-release", "name": "模型发布"}],
        "items": [{
            "id": "qwen", "section": "model-release", "nav": "千问",
            "headline": "千问发布 Qwen3.8", "tldr": "**千问**发布 `Qwen3.8`。",
            "body": ["**千问**发布 `Qwen3.8`，参数 **29B**。"],
            "sources": [{"url": "https://qwen.ai/blog", "kind": "official", "primary": True}],
            "confidence": "confirmed", "facts": ["29B 参数"],
            "voice": ["千问发布 Qwen3.8。"],
            "cards": [{"label": "发布", "body": "千问发布 Qwen3.8", "icon": "article"}],
        }],
    }
    md = export_review(doc)
    assert "<!-- issue 2026-09-22" in md and "## item:qwen" in md
    md2 = md.replace("参数 **29B**。",
                     "参数 **29B**，今日上线。")
    doc2 = import_review(json.loads(json.dumps(doc)), md2)
    assert "今日上线" in doc2["items"][0]["body"][0]
    kept = {"qwen": {"id": "qwen", "raw_url": "https://qwen.ai/blog",
                     "url_canon": "https://qwen.ai/blog",
                     "facts": ["29B 参数"], "title": "千问发布 Qwen3.8",
                     "summary": "", "content_text": "29B", "links": []}}
    errs, warns, flags = validate_issue(doc2, kept)
    assert not errs, errs
    doc3 = json.loads(json.dumps(doc2))
    doc3["items"][0]["body"] = ["参数 999B。"]
    errs, warns, flags = validate_issue(doc3, kept)
    assert any(f["kind"] == "number_not_in_facts" for f in flags), "数字白名单未捕获 999"
    doc4 = json.loads(json.dumps(doc2))
    doc4["items"][0]["voice"] = ["参数 999B。"]
    errs, warns, flags = validate_issue(doc4, kept)
    assert any(f["kind"] == "number_not_in_spine" for f in flags), "spine 外数字未捕获"
    bad = md.replace("## item:qwen", "## item:nope")
    try:
        import_review(doc, bad)
        raise AssertionError("unknown id 未硬错")
    except SystemExit:
        pass
    print("digest selftest OK: export/import roundtrip + digit whitelist + hard errors")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="digest stage (PLAN §7.4)")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="回编 50_review.md → 锁 50_issue.json")
    ap.add_argument("--callb", action="store_true",
                    help="Call B 投影 voice/cards/video + 合规 pass")
    ap.add_argument("--force", action="store_true",
                    help="Call A 时强制重导 review.md（覆盖已编辑版本）")
    ap.add_argument("--selftest", action="store_true", help="离线自检，不打 LLM")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.selftest:
        _selftest()
        return

    with meta.run_lock(run_dir):
        # 运行态 key 与各分支的 stage_done 名对齐（digest_import/digest_callb/
        # digest）——错名的 key 永远清不掉，会留"崩溃残留"假墓碑。
        meta.stage_begin(run_dir, "digest_import" if args.do_import
                         else "digest_callb" if args.callb else "digest")
        if args.do_import:
            do_import(run_dir)          # 确定性路径，不需要 LLM key
        elif args.callb:
            call_b(run_dir, llm.load_cfg(args.config))
        else:
            call_a(run_dir, llm.load_cfg(args.config), force=args.force)


if __name__ == "__main__":
    main()
