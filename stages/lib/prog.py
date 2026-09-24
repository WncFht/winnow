"""阶段进度协议 —— 一个 Prog 实例同时喂两个消费者：

1) stderr 人类可读行  `[stage HH:MM:SS] <msg>` / `[stage HH:MM:SS] 320/1332 <msg>`
   —— 经 just 的 tee 自动落 logs/<stage>.log；
2) 结构化事件 JSONL   <run_dir>/logs/<stage>.prog.jsonl
   —— TUI (just status/watch) 直接读最后一条拿 N/M 与速率，不用解析日志文本。

用法::

    p = Prog(run_dir, "filter", total=len(todo))
    p.tick(done=i, msg="summary")      # 节流：到 step 或 interval 才写
    p.say("repair pass 48 rows")       # 立即写一行

协议只增不改：文件、字段、节流参数都版本内自生自灭，TUI 缺失文件即视为
"该阶段未上报进度"。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Union


# 与 meta.dumps_jsonl_row 同一口径：json.dumps 不转义、而 splitlines 会断行
# 的字符（msg 里可能带条目标题）。转义后按行读取器不断行，loads 往返等价。
_JSONL_UNSAFE = str.maketrans({
    "": "\\u0085",
    " ": "\\u2028",
    " ": "\\u2029",
})


class Prog:
    def __init__(self, run_dir: Union[str, Path, None], stage: str,
                 total: int = 0, step: int = 50, interval: float = 30.0):
        """total=0 表示总量未知（tick 只报 done 不报分母）。
        step: 每前进 N 个至少写一行；interval: 距上次写超过 N 秒也写一行。"""
        self.stage = stage
        self.total = int(total or 0)
        self.step = max(1, int(step))
        self.interval = float(interval)
        self._last_done = 0
        self._last_ts = time.time()
        self._fp = None
        if run_dir:
            try:
                lp = Path(run_dir) / "logs" / f"{stage}.prog.jsonl"
                lp.parent.mkdir(parents=True, exist_ok=True)
                self._fp = open(lp, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._fp = None            # 进度失败绝不拖垮阶段

    # -- writers -----------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        ev["ts"] = round(ev.get("ts") or time.time(), 2)
        ev["stage"] = self.stage
        if self.total:
            ev["total"] = self.total
        line = f"[{self.stage} {time.strftime('%H:%M:%S', time.localtime(ev['ts']))}] "
        done = ev.get("done")
        if done is not None:
            line += f"{done}/{self.total or '?'} "
        line += ev.get("msg", "")
        print(line, file=sys.stderr, flush=True)
        if self._fp:
            try:
                self._fp.write(
                    json.dumps(ev, ensure_ascii=False).translate(_JSONL_UNSAFE)
                    + "\n")
            except OSError:
                self._fp = None

    # -- public ------------------------------------------------------------
    def say(self, msg: str) -> None:
        """立即输出一行（阶段里程碑、警告、小计）。"""
        self._emit({"kind": "say", "msg": str(msg)})

    def retune(self, total: int = 0, step: int = 1) -> None:
        """相位切换：换 total/step 并复位节流基线——否则上一相位的
        _last_done/_last_ts 会吞掉新相位开头的 tick。"""
        self.total = int(total or 0)
        self.step = max(1, int(step))
        self._last_done = 0
        self._last_ts = 0.0

    def tick(self, done: int, msg: str = "", force: bool = False) -> None:
        """进度更新。默认节流：距上次输出 >= step 或 >= interval 秒才写。
        结束调用方应 tick(total, force=True) 或 say() 收尾。"""
        now = time.time()
        if not force and done - self._last_done < self.step \
                and now - self._last_ts < self.interval:
            return
        self._last_done, self._last_ts = done, now
        self._emit({"kind": "tick", "done": int(done), "msg": str(msg)})

    def close(self) -> None:
        if self._fp:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def tail_events(run_dir: Union[str, Path], stage: str,
                last: int = 20) -> list[dict]:
    """读某阶段 prog 边车最后 N 条事件（TUI 用）。无文件 → []。
    走文件迭代（universal newlines）而非 splitlines——与 meta.iter_jsonl
    同理由：写入端已转义 U+2028/9，但防御性一致更省心。"""
    p = Path(run_dir) / "logs" / f"{stage}.prog.jsonl"
    try:
        f = open(p, "r", encoding="utf-8", errors="replace", newline=None)
    except OSError:
        return []
    lines: list[str] = []
    with f:
        for ln in f:
            lines.append(ln)
            if len(lines) > last:
                lines.pop(0)
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def selftest() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Prog(td, "demo", total=200, step=50, interval=999)
        p.say("start")
        for i in range(1, 201):
            p.tick(i, "work")
        p.close()
        evs = tail_events(td, "demo")
        kinds = [e["kind"] for e in evs]
        assert kinds.count("say") == 1 and kinds[0] == "say"
        ticks = [e for e in evs if e["kind"] == "tick"]
        # 节流: 1..200 步进 50 → 应写出 50,100,150,200(强制) 共 4 条
        assert [t["done"] for t in ticks] == [50, 100, 150, 200], \
            [t["done"] for t in ticks]
        # 无 run_dir 的 Prog 不崩
        Prog(None, "x").say("ok")
    print("prog selftest ok")


if __name__ == "__main__":
    selftest()
