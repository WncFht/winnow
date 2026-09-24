"""stages/lib/selkit.py — 40_selected.json 工具箱（gate_select ↔ review_server 共用件）。

斩断全仓唯一一条 stage→stage import 边（review_server 曾 `from stages import
gate_select as gs` 拿它当工具箱）。这里是纯函数/常量层：候选/选定文件名、
slug 化、selected/1 校验（lint + 可选 pydantic）、run_dir 解析、本阶段
config、items.sqlite used 回写。候选构建/写盘/CLI 仍留在
stages/gate_select.py。
"""
from __future__ import annotations

import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stages.lib import meta, pool

REPO = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")

CAND_NAME = "40_candidates.json"
SEL_NAME = "40_selected.json"

SLUG_RE = re.compile(r"^[a-z0-9-]{2,24}$")
KEY_RE = re.compile(r"^[0-9a-f]{16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def eprint(*a):
    print(*a, file=sys.stderr)


def resolve_run_dir(s: str) -> Path:
    """'YYYY-MM-DD' -> runs/<date>；否则按路径解析（相对路径基于 repo 根）。"""
    p = Path(s)
    if DATE_RE.match(s):
        return meta.ensure_run(s)
    return meta.ensure_run(p)


def _daily_map() -> dict:
    """sources.yaml -> {source_name: daily(bool)}；文件缺失/解析失败 -> {}
    （全员 False = fail-open，daily 死区判定不生效）。"""
    try:
        import yaml  # lazy：同 load_config 约定
        data = yaml.safe_load((REPO / "sources.yaml").read_text(encoding="utf-8"))
    except Exception as e:
        eprint(f"[gate_select] warn: sources.yaml 读取失败 {e} — daily_map 按空集")
        return {}
    if not isinstance(data, list):
        return {}
    return {str(s["name"]): bool(s.get("daily"))
            for s in data if isinstance(s, dict) and s.get("name")}


def load_config(items_db: str | None = None) -> dict:
    """config.yaml > config.example.yaml；取本阶段需要的 schedule/storage/pool
    字段 + sources.yaml 的 daily_map。--items-db CLI 覆盖 storage.items_db
    （与 collect/filter/dedup 的 --items-db 约定相同）。"""
    cfg = meta.load_config()
    sch = cfg.get("schedule") or {}
    sto = cfg.get("storage") or {}
    pcfg = cfg.get("pool") or {}
    return {
        "topk_autopick": int(sch.get("topk_autopick") or 14),
        "max_items": int(sch.get("max_items") or 20),
        "gate1_deadline": str(sch.get("gate1_deadline") or "08:30"),
        "items_db": str(items_db or sto.get("items_db") or "state/items.sqlite"),
        "history_db": str(sto.get("history_db") or "state/history.sqlite"),
        "arrival_grace_days": int(pcfg.get("arrival_grace_days") or 2),
        # None 缺省→14；显式 0 保留（=wfrom 下限，子句 C 整体关闭）
        "carry_stale_max_days": int(pcfg["carry_stale_max_days"])
            if pcfg.get("carry_stale_max_days") is not None else 14,
        "daily_map": _daily_map(),
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
    """stages.lib.prompts.SECTION_VOCAB（lazy import——本模块被 review_server 当工具箱用时也要能跑）。"""
    try:
        from stages.lib.prompts import SECTION_VOCAB
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


def mark_used(run_dir: Path, episode: str, keys,
              items_db: str | None = None) -> "int | None":
    """出片标记：kept keys -> items.sqlite used_in_episode=episode（只标记不清除，
    force 重提交不会抹掉既有标记）。

    items_db 直给时跳过 config 读取（--items-db 覆盖路径，同 build_candidates
    口径）。40_selected 已落盘且为权威——空 kept / 池未启用（库不存在）静默
    跳过，池写异常只 WARN。返回新标记行数；失败返回 None（调用方据此追加
    warning）。
    """
    ks = [k for k in dict.fromkeys(keys or []) if k]
    if not ks:
        return 0
    try:
        if items_db is None:
            try:
                items_db = load_config().get("items_db")
            except ImportError:
                items_db = None     # review_server 环境无 yaml：退默认池路径
        db = pool.resolve_path(items_db)
        if not db.exists():
            return 0                    # 池未启用：无物可标
        return pool.mark_used(db, str(episode), ks)
    except Exception as e:
        eprint(f"[gate_select] WARN items.sqlite used 标记失败（{SEL_NAME} 已写，"
               f"权威不受影响）: {type(e).__name__}: {e}")
        return None
