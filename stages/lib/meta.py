"""Shared run-metadata helpers for every pipeline stage.

Implements the PLAN.md contract:
- runs/<date>/ artifacts (§4): 00_meta.json is schema "run_manifest/1" with
  stages{} -> {artifact, sha256, status, produced_at, producer}; it is the
  basis for breakpoint resume (§9 幂等).
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
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Union

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs"
META_NAME = "00_meta.json"
META_SCHEMA = "run_manifest/1"
LOCK_NAME = ".lock"

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


def _load_meta(run_dir: Union[str, Path]) -> dict:
    p = _meta_path(run_dir)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if isinstance(meta, dict) and isinstance(meta.get("stages"), dict):
                return meta
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/partial meta -> start fresh rather than crash a stage
    run_dir = Path(run_dir)
    return {
        "schema": META_SCHEMA,
        "run_date": run_dir.name,
        "created_at": _utcnow(),
        "stages": {},
    }


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


def stage_done(
    run_dir: Union[str, Path],
    stage: str,
    artifact: Union[str, Path, None],
    status: str = "ok",
    producer: Union[str, None] = None,
    extra: Union[dict, None] = None,
) -> dict:
    """Record a finished stage into 00_meta.json stages{} and return the entry.

    Entry shape (§4 contract): {artifact, sha256, status, produced_at,
    producer}. `artifact` is stored relative to run_dir; sha256 is the file
    hash, or null when the artifact is absent/a directory/None. `extra`
    keys are merged into the entry for stage-specific bookkeeping.

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
        "artifact": stored,
        "sha256": sha256,
        "status": status,
        "produced_at": _utcnow(),
        "producer": producer if producer is not None else _infer_producer(),
    }
    if extra:
        entry.update(extra)

    meta["stages"][stage] = entry
    atomic_write(_meta_path(run_dir), meta)
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
