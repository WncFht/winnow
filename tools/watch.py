# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13"]
# ///
"""just status / just watch —— run 目录只读仪表盘。

数据源（全部只读，不碰 .just.lock / .lock）：
  00_meta.json        lib.meta.meta_status(verify=True) → 阶段状态 + artifact 校验
  00_running.json     lib.meta.running_stages → 运行中阶段 + /proc 判活
  logs/<s>.prog.jsonl lib.prog.tail_events → N/M 进度 + 速率 + ETA
  .just.lock          lslocks → 持锁者/排队者
  logs/*.log          尾部面板（运行中阶段优先）

用法:
  uv run tools/watch.py --run-dir runs/2026-09-23 [--once]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stages"))

from lib import meta, prog  # noqa: E402

from rich import box  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

# DAG 行: (meta 主键, 显示名, prog.jsonl 探测名候选)
DAG = [
    ("collect",      "collect",     ["collect"]),
    ("filter",       "filter",      ["filter"]),
    ("dedup",        "dedup",       ["dedup"]),
    ("gate_select",  "pick",        ["gate_select"]),
    ("digest",       "digest",      ["digest"]),
    ("digest_callb", "callb",       ["digest"]),
    ("voice",        "voice",       ["voice"]),
    ("cards",        "cards",       ["cards"]),
    ("subs",         "subs",        ["subs"]),
    ("render_plan",  "render-plan", ["render_plan"]),
    ("compose",      "compose",     ["compose"]),
    ("meta_qa",      "meta",        ["meta_qa"]),
]

# 簿记副键（fold 进宿主行 detail，不单独占行）
BOOKKEEP = {
    "summaries": "filter", "gate_prepare": "gate_select",
    "digest_export": "digest", "digest_import": "digest",
    "digest_flags": "digest", "cards_json": "cards",
    "cards_manifest": "cards", "meta_title": "meta_qa", "meta_cover": "meta_qa",
}


def _elapsed(iso: str) -> str:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        s = max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
    except (ValueError, TypeError):
        return "?"
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "—"


def _lock_info(run_dir: Path) -> tuple[str, str, dict | None]:
    """(.just.lock 持锁者描述, 排队数, 持锁者信息)。无锁 → ("", "", None)。

    持锁者信息 {pid, script, argv, etime} 兼作 running-state 兜底：早于
    00_running.json 登记机制启动的进程不写侧车，但锁永远知道真相。"""
    try:
        out = subprocess.run(
            ["lslocks", "-n", "-o", "PID,MODE,PATH"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "", "", None
    ap = str((run_dir / ".just.lock").resolve())
    holder, waiters = None, 0
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[2] == ap:
            if parts[1] == "WRITE":
                holder = parts[0]
            else:
                waiters += 1
    desc, info = "", None
    if holder:
        try:
            cmd = Path(f"/proc/{holder}/cmdline").read_bytes() \
                .replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        import re
        m = re.search(r"stages/[a-z_]+\.py( [^ ]+)*", cmd)
        desc = m.group(0)[:60] if m else f"pid {holder}"
        et = ""
        try:
            et = subprocess.run(["ps", "-o", "etime=", "-p", holder],
                                capture_output=True, text=True,
                                timeout=3).stdout.strip()
            if et:
                desc += f" ({et})"
        except (OSError, subprocess.TimeoutExpired):
            pass
        m2 = re.search(r"stages/([a-z_]+)\.py", cmd)
        info = {"pid": holder, "script": m2.group(1) if m2 else "",
                "argv": cmd, "etime": et}
    return desc, (f"+{waiters} 排队" if waiters else ""), info


def _prog_info(run_dir: Path, candidates: list[str]) -> dict | None:
    """候选 prog 文件里取最新事件 → {done,total,msg,rate,eta}。"""
    best = None
    for name in candidates:
        evs = prog.tail_events(run_dir, name, last=8)
        if evs and (best is None or evs[-1].get("ts", 0) > best[0].get("ts", 0)):
            best = (evs[-1], evs)
    if not best:
        return None
    last, evs = best
    ticks = [e for e in evs if e.get("kind") == "tick"]
    rate = None
    if len(ticks) >= 2:
        a, b = ticks[-2], ticks[-1]
        dt = b.get("ts", 0) - a.get("ts", 0)
        dd = (b.get("done") or 0) - (a.get("done") or 0)
        if dt > 0 and dd > 0:
            rate = dd / dt
    total = last.get("total") or 0
    done = last.get("done")
    eta = None
    if rate and total and done is not None and done < total:
        eta = int((total - done) / rate)
    return {"done": done, "total": total, "msg": last.get("msg", ""),
            "rate": rate, "eta": eta, "ts": last.get("ts", 0)}


def _tail(path: Path, n: int = 8, chunk: int = 65536) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def _gate_hints(run_dir: Path, stages: dict) -> list[str]:
    hints = []
    if (run_dir / "40_candidates.json").exists() \
            and not (run_dir / "40_selected.json").exists():
        hints.append("gate-1 待人工 pick（或 pick-auto / deadline1）")
    rev = run_dir / "50_review.md"
    if rev.exists() and (run_dir / "50_issue.json").exists():
        import hashlib
        try:
            cur = hashlib.sha256(rev.read_bytes()).hexdigest()
        except OSError:
            cur = None                    # exists()→read 之间被重写（TOCTOU）
        base = (stages.get("digest_import") or {}).get("review_sha256") \
            or (stages.get("digest_export") or {}).get("review_sha256")
        if base and cur and cur != base:
            hints.append("50_review.md 有未导入编辑 —— just edit-import")
    return hints


def _verify_ok(run_dir: Path, e: dict) -> str:
    """meta._verify + 目录 artifact 补丁（isdir 也算 ok，与 resume 口径一致）。"""
    v = e.get("_verify", "ok")
    if v == "missing" and e.get("artifact") \
            and (run_dir / e["artifact"]).is_dir():
        return "ok"
    return v


# artifact sha 缓存: path -> (mtime_ns, size, verdict)。
# Live 每秒 build() 一次，meta_status(verify=True) 会把每个 artifact 重新
# 哈希一遍（compose 产物可达几十 MB）——文件 mtime/size 未变则结果必然相同。
_VERIFY_CACHE: dict[str, tuple[int, int, str]] = {}


def _verify_all(run_dir: Path, m: dict) -> None:
    """meta_status(verify=True) 等价标注，sha 结果按 (mtime,size) 缓存。"""
    for entry in (m.get("stages") or {}).values():
        art = entry.get("artifact")
        if not art:
            entry["_verify"] = "no_artifact"
            continue
        ap = run_dir / art
        if ap.is_dir():
            entry["_verify"] = "ok"
            continue
        try:
            st = ap.stat()
        except OSError:
            entry["_verify"] = "missing"
            continue
        if not ap.is_file():
            entry["_verify"] = "missing"
            continue
        key = str(ap)
        rec = _VERIFY_CACHE.get(key)
        if rec and rec[0] == st.st_mtime_ns and rec[1] == st.st_size:
            entry["_verify"] = rec[2]
            continue
        v = "ok"
        if entry.get("sha256") and meta.sha256_file(ap) != entry["sha256"]:
            v = "sha_mismatch"
        _VERIFY_CACHE[key] = (st.st_mtime_ns, st.st_size, v)
        entry["_verify"] = v


def build(run_dir: Path) -> Group:
    m = meta.meta_status(run_dir)
    _verify_all(run_dir, m)
    stages = m.get("stages") or {}
    running = meta.running_stages(run_dir)
    holder, queued, hinfo = _lock_info(run_dir)

    head = Table.grid(padding=(0, 2))
    head.add_column(style="bold cyan")
    head.add_column()
    head.add_row("run", f"{run_dir}  episode {m.get('episode', '?')}")
    if holder:
        head.add_row("lock", f"持锁: {holder} {queued}")
    elif running:
        head.add_row("lock", "空闲（运行项持 stage 内 .lock）")
    for h in _gate_hints(run_dir, stages):
        head.add_row("gate", f"[yellow]{h}[/]")

    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    tbl.add_column("stage", style="bold", no_wrap=True)
    tbl.add_column("状态", no_wrap=True)
    tbl.add_column("进度/详情", overflow="fold")
    tbl.add_column("artifact", overflow="fold", style="dim")
    tbl.add_column("时间", no_wrap=True, style="dim")

    for key, label, pnames in DAG:
        e = stages.get(key)
        run = running.get(key)
        # digest argv 分流：--callb 注册在 digest 名下 → 归到 callb 行
        if key == "digest_callb" and not run:
            r = running.get("digest")
            if r and "--callb" in (r.get("argv") or ""):
                run = r
        if key == "digest" and run and "--callb" in (run.get("argv") or ""):
            run = None
        # filter phase-2 注册 "summaries" 键——不回退则摘要跑着时 filter 行
        # 显示 done/空闲，看起来像卡死（上一个 "dedup 没日志" 事故的同款坑）
        if key == "filter" and not run:
            run = running.get("summaries")
        # gate-1: --prepare 注册 gate_prepare；review_server 长跑 → serving
        if key == "gate_select" and not run:
            run = running.get("gate_prepare") or running.get("review_server")
        # 兜底：早于 running-state 机制启动的进程不写 00_running.json——
        # 不兜底则顶栏显示持锁 N 小时而该行显示 ·（看起来"没日志/卡死"）
        if not run and hinfo and hinfo.get("script"):
            s = hinfo["script"]
            if s == "digest":
                hit = (key == "digest" and "--callb" not in hinfo["argv"]) \
                    or (key == "digest_callb" and "--callb" in hinfo["argv"])
            else:
                hit = key == s
            if hit:
                run = {"alive": True, "pid": hinfo["pid"],
                       "argv": hinfo["argv"], "etime": hinfo["etime"]}
        pi = _prog_info(run_dir, pnames)

        if run and run.get("alive"):
            el = run.get("etime") or _elapsed(run.get("started_at", ""))
            st = Text("● running", style="cyan bold")
            det = Text(f"pid {run.get('pid')} 已跑 {el}")
            if pi and pi.get("done") is not None:
                frac = f"{pi['done']}/{pi['total'] or '?'}"
                extra = f" {pi['msg']}" if pi.get("msg") else ""
                if pi.get("rate"):
                    extra += f"  {pi['rate']:.1f}/s"
                if pi.get("eta") is not None:
                    extra += f"  eta {pi['eta'] // 60}:{pi['eta'] % 60:02d}"
                det = Text(f"{frac}{extra}  ({el})", style="cyan")
            elif pi and pi.get("msg"):
                det = Text(f"{pi['msg']}  ({el})", style="cyan")
            art, t = e.get("artifact") if e else "", "…"
        elif run and not run.get("alive"):
            st = Text("⚠ stale", style="yellow bold")
            det = Text(f"登记 pid {run.get('pid')} 已死 —— 上次崩溃残留", style="yellow")
            art = e.get("artifact") if e else ""
            t = _hm(e.get("produced_at")) if e else "—"
        elif e:
            ok = e.get("status") in ("done", "skipped")
            v = _verify_ok(run_dir, e)
            if ok and v == "ok":
                st = Text("✓ done", style="green")
            elif ok and v == "no_artifact":
                st = Text("✓ done", style="green")  # 无产物阶段（簿记型）
            elif ok:
                st = Text(f"✗ {v}", style="red bold")
            elif e.get("status") == "failed":
                st = Text("✗ failed", style="red bold")
            else:
                st = Text(e.get("status", "?"), style="dim")
            bits = []
            for bk, host in BOOKKEEP.items():
                if host == key and bk in stages:
                    bits.append(bk.replace("digest_", "").replace("cards_", ""))
            det = Text(" ".join(bits), style="dim")
            art = e.get("artifact") or "—"
            t = _hm(e.get("produced_at"))
        else:
            st = Text("·", style="dim")
            det = Text("")
            art, t = "—", "—"
        tbl.add_row(label, st, det, str(art), t)

    # 日志尾部：优先第一个 alive 运行项，否则最新 .log
    logs = run_dir / "logs"
    tail_src, lines = None, []
    alive = [s for s, r in running.items() if r.get("alive")]
    cand = (logs / f"{alive[0]}.log") if alive else None
    if cand and cand.exists():
        tail_src = cand
    elif logs.is_dir():
        lg = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime,
                    reverse=True)
        if lg:
            tail_src = lg[0]
    if tail_src:
        lines = _tail(tail_src)
    footer = Panel(
        "\n".join(lines) if lines else "(no logs)",
        title=f"tail: {tail_src.name}" if tail_src else "tail",
        border_style="dim", padding=(0, 1))
    return Group(head, tbl, footer)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--once", action="store_true", help="打印一次快照后退出")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = REPO / run_dir
    con = Console()
    if args.once:
        con.print(build(run_dir))
        return 0
    with Live(build(run_dir), console=con, refresh_per_second=4,
              transient=False) as live:
        try:
            while True:
                time.sleep(args.interval)
                live.update(build(run_dir))
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
