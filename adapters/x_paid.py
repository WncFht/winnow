#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""X 平台采集器 ④ 付费 adapter —— D12：只留接口 + 配置开关，不买不实现。

决策（docs/PLAN.md D12 / §5.3）：连续失败告警后再决策是否付费。本文件提供与
`lib.x_synd` 同形的接口占位，使 x_collect 四路编排可以无条件调用；
未启用即抛 NotConfigured，调用方按"此路不可用"处理转上一路结果。

Gate 逻辑（每次调用都过闸）：
    1. config.x_collector.paid_adapter.enabled != true → NotConfigured
    2. enabled 但 api_key_env（默认 X_PAID_KEY）未设置 → NotConfigured
    3. enabled + key 存在 → XPaidError（vendor 未定，D12 占位；
       接入真实付费 API 时在 _call 落实现，勿动闸口）

API（与 stages/lib/x_synd.py 同形，产出也应是 raw_item/1 dict）：
    fetch_user(handle, cfg=None, *, timeout=20, **kw) -> list[dict]
    fetch_tweet(tweet_id, cfg=None, **kw)              -> dict
    is_enabled(cfg)  -> bool
    load_cfg(path)   -> paid_adapter 配置（含 api_key 若 env 已设）

CLI:  uv run adapters/x_paid.py            # selftest（offline 闸口断言）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import yaml  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class NotConfigured(RuntimeError):
    """付费路未启用或缺 key —— x_collect 应静默跳过此路（非故障）。"""


class XPaidError(RuntimeError):
    """enabled+key 但调用失败（含"实现未接入"占位）。"""


def load_cfg(path: str | Path | None = None) -> dict:
    """config.yaml（缺省 config.example.yaml）的 x_collector.paid_adapter 段。"""
    p = Path(path) if path else REPO / "config.yaml"
    if not p.exists():
        p = REPO / "config.example.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return resolve_cfg(doc)


def resolve_cfg(cfg: Any) -> dict:
    """全 config / x_collector / paid_adapter 子树 → {enabled, api_key_env,
    api_key?, ...}；api_key 仅在 env 已设时填入。"""
    d = dict(cfg or {})
    xc = d.get("x_collector") if isinstance(d.get("x_collector"), dict) else d
    pa = xc.get("paid_adapter") if isinstance(xc.get("paid_adapter"), dict) \
        else xc
    out = {"enabled": bool(pa.get("enabled", False)),
           "api_key_env": pa.get("api_key_env") or "X_PAID_KEY",
           "timeout": float(pa.get("timeout", 20))}
    key = os.environ.get(out["api_key_env"], "")
    if key:
        out["api_key"] = key
    return out


def is_enabled(cfg: Any) -> bool:
    try:
        return resolve_cfg(cfg)["enabled"] if isinstance(cfg, dict) \
            else load_cfg(cfg)["enabled"]
    except Exception:
        return False


def _gate(cfg: Any) -> dict:
    """过闸：未启用/缺 key → NotConfigured；返回 resolved paid_adapter 配置。"""
    pa = resolve_cfg(cfg) if isinstance(cfg, dict) else load_cfg(cfg)
    if not pa["enabled"]:
        raise NotConfigured(
            "x_collector.paid_adapter.enabled=false（D12：接口预留，"
            "连续失败告警后再决策付费）")
    if not pa.get("api_key"):
        raise NotConfigured(
            f"paid_adapter enabled 但 ${pa['api_key_env']} 未设置 "
            f"（见 secrets.env.example）")
    return pa


def _call(endpoint: str, pa: dict) -> Any:
    """真实付费 API 调用点 —— vendor 未定（D12），此处即占位抛错。

    接入时：按 vendor 文档实现 → 返回原生 JSON；转换为 raw_item/1 由
    fetch_user/fetch_tweet 负责，字段对齐 stages/lib/x_synd.py 输出。
    """
    raise XPaidError(
        f"x paid adapter 未接入 vendor（endpoint={endpoint!r}，D12 占位）；"
        f"接口/闸口已就绪，填入实现即可")


def fetch_user(handle: str, cfg: Any = None, *, timeout: float | None = None,
               **kw) -> list[dict]:
    """@handle 时间线 → raw_item/1 dict 列表（未启用 → NotConfigured）。"""
    pa = _gate(cfg)
    _call("user_timeline", pa)
    return []  # pragma: no cover


def fetch_tweet(tweet_id: str, cfg: Any = None, **kw) -> dict:
    """单条推文 enrich → tweet dict（未启用 → NotConfigured）。"""
    pa = _gate(cfg)
    _call("tweet_lookup", pa)
    return {}  # pragma: no cover


# ------------------------------------------------------------- self test ----

if __name__ == "__main__":
    # 默认（example）配置：enabled=false → NotConfigured
    for fn in (fetch_user, fetch_tweet):
        try:
            fn("OpenAI" if fn is fetch_user else "123", {})
            raise AssertionError("expected NotConfigured")
        except NotConfigured as e:
            print(f"{fn.__name__} disabled -> NotConfigured OK: {e}")
    assert is_enabled({}) is False
    assert is_enabled({"x_collector": {"paid_adapter": {"enabled": True}}})

    # enabled 但无 key → NotConfigured
    cfg_on = {"x_collector": {"paid_adapter":
                              {"enabled": True, "api_key_env": "X_PAID_KEY"}}}
    os.environ.pop("X_PAID_KEY", None)
    try:
        fetch_user("OpenAI", cfg_on)
        raise AssertionError("expected NotConfigured (no key)")
    except NotConfigured as e:
        print(f"enabled-no-key -> NotConfigured OK: {e}")

    # enabled + key → XPaidError（占位实现未接入）
    os.environ["X_PAID_KEY"] = "dummy"
    try:
        fetch_user("OpenAI", cfg_on)
        raise AssertionError("expected XPaidError (stub)")
    except XPaidError as e:
        print(f"enabled+key -> XPaidError OK: {e}")
    finally:
        os.environ.pop("X_PAID_KEY", None)

    # load_cfg 走 config.example.yaml：enabled=false
    assert load_cfg()["enabled"] is False
    print("x_paid.py self-test OK")
