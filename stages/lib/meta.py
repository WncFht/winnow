"""Shared run-metadata helpers for every pipeline stage.

Implements the PLAN.md contract:
- runs/<date>/ artifacts (§4): 00_meta.json is schema "run_manifest/1" —
  {schema, episode, created_at, stages{}}; stages{} -> {artifact, sha256,
  status∈done|pending|failed|skipped, produced_at, producer}（extra=forbid）;
  it is the basis for breakpoint resume (§9 幂等).
- Artifacts are written to "<name>.tmp" then os.replace() so a crash never
  leaves a half-written file.
- Every stage takes an exclusive flock on runs/<date>/.lock at start.

Stages use it like:

    from lib import meta
    run_dir = meta.ensure_run(args.date)
    with meta.run_lock(run_dir):
        ...
        meta.atomic_write(run_dir / "10_raw_items.jsonl", payload)
        meta.stage_done(run_dir, "collect", "10_raw_items.jsonl")

Stdlib only — safe to import from any PEP 723 stage script.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Union

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs"
META_NAME = "00_meta.json"
META_SCHEMA = "run_manifest/1"
LOCK_NAME = ".lock"
# StageEntry extra=forbid → stage_done(extra=) 的簿记分流到这个非契约文件；
# contracts.validate 只 lint 已知 artifact 名，stats 文件不参与 schema 校验。
STATS_NAME = "00_stage_stats.json"

_EPISODE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_STATUSES = ("done", "pending", "failed", "skipped")   # contracts.StageEntry
_ENTRY_KEYS = ("artifact", "sha256", "status", "produced_at", "producer")
_ENTRY_KEYSET = frozenset(_ENTRY_KEYS)
_STATS_OWN_KEYS = frozenset({"recorded_at"})           # 侧车自有字段，不并回 entry
# 非日期名 run 目录（冒烟/fixture）的 episode 兜底来源：按 DAG 序探测
# artifact 自带的 date/episode 字段，再退回 10_raw_items.date_fetched。
_EPISODE_SOURCES = (
    ("50_issue.json", "date"),
    ("40_selected.json", "episode"),
    ("70_render_plan.json", "episode"),
    ("62_timeline.json", "episode"),
    ("80_build_manifest.json", "episode"),
    ("61_audio_manifest.json", "episode"),
    ("63_cards.json", "episode"),
    ("11_raw_manifest.json", "episode"),
)

_CHUNK = 1 << 20  # 1 MiB


def sha256_file(path: Union[str, Path]) -> str:
    """Return the hex sha256 of a file, streamed in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Union[str, Path], data: Any) -> Path:
    """Write data to "<path>.tmp" then os.replace() onto path.

    data: bytes written as-is; str encoded utf-8; dict/list serialized to
    JSON (utf-8, ensure_ascii=False, trailing newline); anything else is
    str()-ified and encoded utf-8.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, (dict, list)):
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = str(data).encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def ensure_run(date: Union[str, Path], base: Union[str, Path, None] = None) -> Path:
    """Create (idempotently) runs/<date>/ and return its Path.

    `date` is normally a YYYY-MM-DD string joined under `base` (default
    RUNS_DIR). A Path is used as-is when absolute, or resolved against
    REPO_ROOT when relative.
    """
    base_dir = Path(base) if base is not None else RUNS_DIR
    if isinstance(date, Path):
        run_dir = date if date.is_absolute() else REPO_ROOT / date
    else:
        run_dir = base_dir / str(date)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


@contextmanager
def run_lock(run_dir: Union[str, Path], blocking: bool = True) -> Iterator[int]:
    """Exclusive fcntl.flock on run_dir/.lock. Yields the lock fd.

    blocking=False uses LOCK_NB and raises BlockingIOError if another stage
    already holds the lock.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _meta_path(run_dir: Union[str, Path]) -> Path:
    return Path(run_dir) / META_NAME


def _episode_from_artifacts(run_dir: Path) -> str | None:
    """从 run_dir 已有 artifact 中探测期号（非日期名冒烟目录用）。"""
    for fname, key in _EPISODE_SOURCES:
        p = run_dir / fname
        if not p.is_file():
            continue
        try:
            v = json.loads(p.read_text(encoding="utf-8")).get(key)
        except (json.JSONDecodeError, OSError):
            continue
        m = _EPISODE_RE.search(str(v or ""))
        if m:
            return m.group(0)
    raw = run_dir / "10_raw_items.jsonl"      # 采集日 = 期号语义
    if raw.is_file():
        try:
            with open(raw, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    m = _EPISODE_RE.search(
                        str(json.loads(line).get("date_fetched") or ""))
                    return m.group(0) if m else None
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _episode_for(run_dir: Path, meta: dict) -> str:
    """RunMeta.episode 必填且须为 YYYY-MM-DD。依次取：旧 meta.episode、
    legacy run_date 键、目录名里的日期、run 内 artifact 的 date/episode、
    created_at 的日期段；都没有（如 runs/verify-* 冒烟目录）→ 兜底
    Asia/Shanghai 今日（与 collect.py run_date 兜底口径一致，不编造既有日期）。"""
    for cand in (meta.get("episode"), meta.get("run_date"), run_dir.name):
        m = _EPISODE_RE.search(str(cand or ""))
        if m:
            return m.group(0)
    ep = _episode_from_artifacts(run_dir)
    if ep:
        return ep
    m = _EPISODE_RE.search(str(meta.get("created_at") or ""))
    if m:
        return m.group(0)
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _normalize_meta(meta: dict, run_dir: Path) -> dict:
    """把载入的 meta（或 {}）归一到 run_manifest/1 契约：

    - 顶层只留 schema/episode/created_at/stages（extra=forbid；
      旧版的 run_date 键迁移进 episode 后删除）；
    - stages{} 每条只留 StageEntry 五键（旧版并入的 extra 簿记键剔除），
      status 归一到 {done,pending,failed,skipped}，artifact None→""
      （契约 artifact: str 必填，"" 表示无产物）。
    归一是自愈式的：legacy meta 在下一次 stage_done 落盘时即变合法。
    """
    out: dict = {
        "schema": META_SCHEMA,
        "episode": _episode_for(run_dir, meta),
        "created_at": str(meta.get("created_at") or _utcnow()),
        "stages": {},
    }
    stages = meta.get("stages")
    if isinstance(stages, dict):
        for name, e in stages.items():
            if not isinstance(e, dict):
                continue
            entry = {k: e.get(k) for k in _ENTRY_KEYS}
            if entry["status"] not in _STATUSES:
                entry["status"] = "done"
            if entry["artifact"] is None:
                entry["artifact"] = ""
            out["stages"][str(name)] = entry
    return out


def _load_stats(run_dir: Path) -> dict:
    """读 00_stage_stats.json 侧车（{stage: {簿记键…, recorded_at}}）。"""
    p = run_dir / STATS_NAME
    try:
        if p.exists():
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                return m
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _load_meta(run_dir: Union[str, Path]) -> dict:
    """载入并归一 00_meta，返回**读视图**：entry 除契约五键外还并回
    非契约簿记键（00_meta 内残留的 pre-split extra + 00_stage_stats 侧车），
    供 meta_status 消费（digest 的 review_sha256、meta_qa 的 prov token 统计）。
    写盘一律走 stage_done → _normalize_meta，侧车键不会回流进 00_meta。"""
    run_dir = Path(run_dir)
    p = _meta_path(run_dir)
    raw: dict = {}
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict):
                raw = m
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/partial meta -> start fresh rather than crash a stage
    meta = _normalize_meta(raw, run_dir)
    raw_stages = raw.get("stages")
    raw_stages = raw_stages if isinstance(raw_stages, dict) else {}
    stats = _load_stats(run_dir)
    for name, entry in meta["stages"].items():
        for src in (raw_stages.get(name), stats.get(name)):
            if isinstance(src, dict):
                for k, v in src.items():
                    if k not in _ENTRY_KEYSET and k not in _STATS_OWN_KEYS:
                        entry[k] = v
    return meta


def _resolve_artifact(run_dir: Path, artifact: Union[str, Path]) -> tuple[str, Path]:
    """Return (stored_name, absolute_path) for an artifact in run_dir."""
    a = Path(artifact)
    if a.is_absolute():
        try:
            stored = a.relative_to(run_dir)
        except ValueError:
            stored = a.name
        return str(stored), a
    return str(artifact), run_dir / a


def _infer_producer() -> str | None:
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 is None:
        return None
    try:
        return str(argv0.relative_to(REPO_ROOT))
    except ValueError:
        return argv0.name


def _record_stats(run_dir: Path, stage: str, extra: dict) -> None:
    """stage_done 的 extra 簿记分流：00_stage_stats.json = {stage: {...,
    recorded_at}}。非契约文件——StageEntry extra=forbid 不允许并进
    stages{}，簿记信息留这里（metrics/人工排查可读，schema lint 不覆盖）。
    写失败不阻塞主流程（meta 已落盘）。"""
    p = Path(run_dir) / STATS_NAME
    stats: dict = {}
    try:
        if p.exists():
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                stats = m
        cur = stats.get(stage)
        merged = dict(cur) if isinstance(cur, dict) else {}
        merged.update(extra)
        merged["recorded_at"] = _utcnow()
        stats[stage] = merged
        atomic_write(p, stats)
    except OSError as e:
        print(f"[meta] warn: {STATS_NAME} 写入失败（簿记丢失，不影响产物）: {e}",
              file=sys.stderr)


def stage_done(
    run_dir: Union[str, Path],
    stage: str,
    artifact: Union[str, Path, None],
    status: str = "done",
    producer: Union[str, None] = None,
    extra: Union[dict, None] = None,
) -> dict:
    """Record a finished stage into 00_meta.json stages{} and return the entry.

    Entry shape (§4 contract, StageEntry extra=forbid): {artifact, sha256,
    status, produced_at, producer}. `artifact` is stored relative to
    run_dir ("" when absent — 契约必填 str)；sha256 is the file hash, or
    null when the artifact is absent/a directory/None. `status` 归一到
    {done,pending,failed,skipped}，非法值按 "done" 处理。
    `extra` 不并入 entry（会违 extra=forbid），分流到 00_stage_stats.json。

    Callers normally hold run_lock() already; the meta file itself is still
    written atomically (.tmp + os.replace).
    """
    run_dir = Path(run_dir)
    meta = _load_meta(run_dir)

    sha256 = None
    stored = None
    if artifact is not None:
        stored, apath = _resolve_artifact(run_dir, artifact)
        if apath.is_file():
            sha256 = sha256_file(apath)

    entry = {
        "artifact": stored or "",
        "sha256": sha256,
        "status": status if status in _STATUSES else "done",
        "produced_at": _utcnow(),
        "producer": producer if producer is not None else _infer_producer(),
    }

    meta["stages"][stage] = entry
    # _load_meta 的读视图含并回的簿记键——写盘前必须再归一，否则侧车键回流
    atomic_write(_meta_path(run_dir), _normalize_meta(meta, run_dir))
    if extra:
        _record_stats(run_dir, stage, extra)
    return entry


def meta_status(run_dir: Union[str, Path], verify: bool = False) -> dict:
    """Return the parsed 00_meta.json dict (fresh skeleton if absent/corrupt).

    verify=True annotates each stages{} entry with "_verify":
    "ok" | "missing" | "sha_mismatch" | "no_artifact" — so resume logic can
    skip only stages whose declared artifact is still intact.
    """
    meta = _load_meta(run_dir)
    if not verify:
        return meta
    run_dir = Path(run_dir)
    for entry in meta.get("stages", {}).values():
        art = entry.get("artifact")
        if not art:
            entry["_verify"] = "no_artifact"
            continue
        apath = run_dir / art
        if not apath.is_file():
            entry["_verify"] = "missing"
        elif entry.get("sha256") and sha256_file(apath) != entry["sha256"]:
            entry["_verify"] = "sha_mismatch"
        else:
            entry["_verify"] = "ok"
    return meta
