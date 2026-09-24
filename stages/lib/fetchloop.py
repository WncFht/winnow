#!/usr/bin/env python3
"""stages/lib/fetchloop.py — 多端点轮换抓取原语（docs/PLAN.md §5.3 族）。

同一骨架的两种参数化：
  * x_synd：全 host 轮换 + x-rate-limit-reset 感知等待 + 指数退避整轮重试
    （max_rounds>0, max_wait_s=预算上限）
  * x_nitter：健康分排序后单遍 fail-fast（max_rounds=0, max_wait_s=0）

run(endpoints, attempt, ...) -> Attempt（首个 kind="ok" 的尝试，.value 载
调用方 payload）。attempt(ep) 自行分类结果：

    kind="ok"           成功 → run 立即返回
    kind="rate_limited" 429 类：reset_at=最早可重试 epoch（缺省调用方给
                        now+900 兜底）；一轮结束若全灭于限流 → 睡到
                        min(reset)+reset_buffer_s，累计等待超 max_wait_s
                        抛 RateLimitExhausted
    kind="retryable"    传输层失败（timeout/dns/conn）→ 指数退避整轮重试，
                        sleep=backoff_base_s*2^round 封顶 backoff_cap_s，
                        退避轮数上限 max_rounds（0=单遍）
    kind="fatal"        确定失败（http_4xx/解析错）——一轮若全是 fatal
                        （无 retryable 无限流）立即抛 AllEndpointsFailed

on_attempt(att) 每端点试完即回调（x_nitter 用来落健康分）。
异常均带 .attempts（末轮 Attempt 列表）；RateLimitExhausted 另带
.retry_after=min(resets)。attempt 自身异常不吞（程序错误直接冒泡）。

_sleep/_mono 为自测注入点（_mono 替代 time.monotonic 算等待预算）。

Smoke:  uv run stages/lib/fetchloop.py   # 纯逻辑自检，无网络
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


@dataclass
class Attempt:
    """一次端点尝试结果。kind ∈ ok | rate_limited | retryable | fatal。"""

    endpoint: str
    kind: str
    value: Any = None                      # ok 时的返回载荷
    reset_at: Optional[float] = None       # rate_limited：可重试 epoch
    detail: str = ""
    extra: dict = field(default_factory=dict)  # 调用方附带的原始记录


class LoopFailed(RuntimeError):
    """轮换抓取耗尽基类。.attempts = 末轮各端点 Attempt。"""

    def __init__(self, msg: str, *, attempts: Optional[list] = None):
        super().__init__(msg)
        self.attempts: list[Attempt] = attempts or []


class AllEndpointsFailed(LoopFailed):
    """全端点失败：纯 fatal 立即抛，或退避轮数耗尽。"""


class RateLimitExhausted(LoopFailed):
    """限流等待超出 max_wait_s 预算。retry_after=最早 reset epoch。"""

    def __init__(self, msg: str, *, retry_after: Optional[float] = None,
                 attempts: Optional[list] = None):
        super().__init__(msg, attempts=attempts)
        self.retry_after = retry_after


def run(endpoints: Iterable[str], attempt: Callable[[str], Attempt], *,
        max_rounds: int = 3, max_wait_s: float = 0.0,
        backoff_base_s: float = 2.0, backoff_cap_s: float = 60.0,
        reset_buffer_s: float = 1.5,
        on_attempt: Optional[Callable[[Attempt], None]] = None,
        label: str = "endpoints",
        _sleep=time.sleep, _mono=time.monotonic) -> Attempt:
    """按序轮换 endpoints 调 attempt，首个 ok 返回；耗尽抛 LoopFailed。"""
    endpoints = list(endpoints)
    deadline = _mono() + max_wait_s
    round_no = 0
    while True:
        resets: list[float] = []
        round_atts: list[Attempt] = []
        for ep in endpoints:
            att = attempt(ep)
            round_atts.append(att)
            if on_attempt is not None:
                on_attempt(att)
            if att.kind == "ok":
                return att
            if att.kind == "rate_limited":
                resets.append(att.reset_at or (time.time() + 900))
        if resets:
            wait = max(1.0, min(resets) - time.time() + reset_buffer_s)
            if _mono() + wait > deadline:
                raise RateLimitExhausted(
                    f"{label}: all rate-limited; reset in {int(wait)}s "
                    f"> budget {max_wait_s}s",
                    retry_after=min(resets), attempts=round_atts)
            _sleep(wait)
            round_no += 1
            continue
        fatal = [a for a in round_atts if a.kind == "fatal"]
        retry = [a for a in round_atts if a.kind == "retryable"]
        if fatal and not retry:
            raise AllEndpointsFailed(
                f"{label}: {len(fatal)}/{len(round_atts)} endpoints failed: "
                + "; ".join(a.detail for a in round_atts if a.detail),
                attempts=round_atts)
        round_no += 1
        if round_no > max_rounds:
            raise AllEndpointsFailed(
                f"{label}: exhausted after {round_no - 1} backoff rounds: "
                + "; ".join(a.detail for a in round_atts if a.detail),
                attempts=round_atts)
        _sleep(min(backoff_cap_s, backoff_base_s * (2 ** round_no)))


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":
    slept: list[float] = []
    fake_now = [1000.0]

    def _sl(s):
        slept.append(s)
        fake_now[0] += s

    def _mo():
        return fake_now[0]

    # 1) 第二端点成功 → 立即返回
    seq = {"a": "retryable", "b": "ok"}
    att = run(["a", "b"], lambda ep: Attempt(ep, seq[ep], value=ep.upper()),
              max_rounds=0, _sleep=_sl, _mono=_mo)
    assert att.value == "B" and att.endpoint == "b"

    # 2) 单遍全灭 → AllEndpointsFailed（max_rounds=0 不睡）
    try:
        run(["a", "b"], lambda ep: Attempt(ep, "fatal", detail=f"{ep} dead"),
            max_rounds=0, _sleep=_sl, _mono=_mo)
        raise AssertionError("should raise")
    except AllEndpointsFailed as e:
        assert len(e.attempts) == 2 and "a dead" in str(e)

    # 3) retryable 耗尽 → 退避两轮后抛（max_rounds=2 → base*2+base*4）
    slept.clear()
    try:
        run(["a"], lambda ep: Attempt(ep, "retryable", detail="timeout"),
            max_rounds=2, backoff_base_s=1.0, _sleep=_sl, _mono=_mo)
        raise AssertionError("should raise")
    except AllEndpointsFailed:
        pass
    assert slept == [2.0, 4.0], slept

    # 4) 限流在预算内 → 睡到 reset+buffer 后下一轮成功
    slept.clear()
    calls = [0]

    def rl_att(ep):
        calls[0] += 1
        if calls[0] == 1:
            return Attempt(ep, "rate_limited",
                           reset_at=time.time() + 0.01, detail="429")
        return Attempt(ep, "ok", value="win")

    att = run(["a"], rl_att, max_wait_s=600, _sleep=_sl, _mono=_mo)
    assert att.value == "win" and slept and slept[0] >= 1.0

    # 5) 限流超预算 → RateLimitExhausted(retry_after=最早 reset)
    fake_now[0] = 1000.0
    try:
        run(["a"], lambda ep: Attempt(ep, "rate_limited",
                                      reset_at=time.time() + 9999,
                                      detail="429"),
            max_wait_s=1, _sleep=_sl, _mono=_mo)
        raise AssertionError("should raise")
    except RateLimitExhausted as e:
        assert e.retry_after and e.attempts[0].detail == "429"

    # 6) fatal+retryable 混合 → 走退避而非立即抛
    slept.clear()
    mix = {"a": "fatal", "b": "retryable"}
    try:
        run(["a", "b"], lambda ep: Attempt(ep, mix[ep], detail=mix[ep]),
            max_rounds=0, _sleep=_sl, _mono=_mo)
        raise AssertionError("should raise")
    except AllEndpointsFailed as e:
        assert "exhausted" in str(e)   # 轮数耗尽路径而非 fatal 路径

    # 7) on_attempt 回调每个端点都被叫到
    seen = []
    run(["x", "y"], lambda ep: Attempt(ep, "ok"),
        max_rounds=0, on_attempt=seen.append, _sleep=_sl, _mono=_mo)
    assert [a.endpoint for a in seen] == ["x"]

    print("fetchloop.py self-test OK")
