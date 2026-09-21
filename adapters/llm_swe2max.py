# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "pyyaml>=6"]
# ///
"""llm.chat adapter — swe-2-max via local OpenAI-compatible gateway (PLAN.md §6, D1).

实测特性（experiments/swe2max-sufficiency-refute/，2026-09-21 复测）：
- reasoning 模型小 max_tokens 会烧光预算返回空 → 默认 24000（config.llm.max_tokens）。
- 不支持/常无视 response_format 约束（json_schema 返回散文）→ JSON 靠 prompt 约束 +
  extract_json() 本地剥 ```json 围栏；want_json=True 时仍发 response_format
  {"type":"json_object"}（实测 HTTP 200 无害，兼容后端可受益）。
- 429/502/超时是共享池毛刺 → adapter 内指数退避 1s/2s/4s 重试后抛 LLMError，
  无跨模型 fallback（D1：可靠性由调用方容错层保证）。

API:
    load_cfg()                                -> llm 配置 dict（含 api_key）
    chat(messages, *, max_tokens, temperature, want_json, tag)
                                              -> {"text": str, "prov": {...}}
    extract_json(text)                        -> 首个 JSON obj/array
    chat_json(messages, retries=2, **kw)      -> obj（解析失败追 "只输出JSON对象" 重试）
    coverage_reconcile(inputs, outputs, key)  -> {"outputs", "missing"}（重批由调用方做）

prov = {model, ts, prompt_tokens, completion_tokens, tag}——stage 侧再并入
contracts.Provenance(model/prompt/input_sha/decided_at) 写进 artifact（§4）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import yaml

REPO = Path(__file__).resolve().parents[1]

_BACKOFF = (1.0, 2.0, 4.0)            # §6：指数退避 1s/2s/4s，上限 3 次重试
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
_WAIT_CAP = 30.0                      # Retry-After/reset-in 提示的等待上限（秒）
_JSON_RETRY_HINT = "只输出JSON对象"


class LLMError(RuntimeError):
    """adapter 内重试耗尽后的网关失败。跨模型/降级由调用方容错层决定（D1）。"""

    def __init__(self, msg: str, *, status: int | None = None,
                 retryable: bool = True, body: str = ""):
        super().__init__(msg)
        self.status = status          # HTTP code，传输层错误为 None
        self.retryable = retryable    # False = 4xx 类/解析类，重试无意义
        self.body = body[:500]


# ---------- config ----------

def load_cfg(path: str | Path | None = None) -> dict:
    """config.yaml 的 llm 段（缺省回退 config.example.yaml）+ api_key_env 指向的环境变量。"""
    p = Path(path) if path else REPO / "config.yaml"
    if not p.exists():
        p = REPO / "config.example.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = dict(doc.get("llm") or {})
    env = cfg.get("api_key_env", "SWE2MAX_API_KEY")
    key = os.environ.get(env, "")
    if not key:
        raise LLMError(f"llm api key env ${env} 未设置（见 secrets.env.example）",
                       retryable=False)
    cfg["api_key"] = key
    cfg.setdefault("base_url", "http://127.0.0.1:3033/v1")
    cfg.setdefault("model", "swe-2-max")
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("max_tokens", 24000)   # 勿调小：reasoning 烧预算→空响应（实测 164s 空）
    cfg.setdefault("batch_size", 24)
    cfg.setdefault("timeout", 300)
    return cfg


# ---------- core call ----------

def _wait_s(headers: httpx.Headers, body: str, attempt: int) -> float:
    """退避 1/2/4s；尊重 Retry-After 头与网关 'reset in N second' 体提示，封顶 _WAIT_CAP。"""
    w = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
    ra = headers.get("Retry-After")
    if ra:
        try:
            w = max(w, min(_WAIT_CAP, float(ra)))
        except ValueError:
            pass
    else:
        m = re.search(r"reset in (\d+) second", body or "")
        if m:
            w = max(w, min(_WAIT_CAP, int(m.group(1)) + 1))
    return w


def chat(messages: list[dict], *, max_tokens: int | None = None,
         temperature: float | None = None, want_json: bool = False,
         tag: str = "", cfg: dict | None = None,
         timeout: float | None = None, retries: int = 3) -> dict:
    """一次 /chat/completions 调用 → {"text", "prov"}。

    max_tokens/temperature 缺省取 cfg；want_json=True 发 response_format json_object
    （swe-2-max 实测无害，仍须 prompt 约束 + extract_json 兜底）。
    429/5xx/超时按 1s/2s/4s 退避重试 `retries` 次后抛 LLMError；4xx 立即抛。
    """
    c = cfg or load_cfg()
    body = {
        "model": c["model"],
        "messages": list(messages),
        "temperature": c["temperature"] if temperature is None else temperature,
        "max_tokens": c["max_tokens"] if max_tokens is None else max_tokens,
    }
    if want_json:
        body["response_format"] = {"type": "json_object"}
    url = c["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {c['api_key']}"}
    proxy = c.get("proxy") or None
    tmo = float(timeout or c.get("timeout", 300))
    # loopback 网关不走环境代理（clash 等会劫持 127.0.0.1 连接）；远端 base_url 仍尊重 env proxy。
    host = re.sub(r"^https?://", "", c["base_url"]).split("/")[0].split(":")[0]
    loopback = host in ("127.0.0.1", "localhost", "::1")

    with httpx.Client(proxy=proxy, trust_env=(proxy is None and not loopback),
                      timeout=httpx.Timeout(tmo)) as client:
        for attempt in range(retries + 1):
            try:
                r = client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = LLMError(f"llm transport/timeout: {e}", retryable=True)
                if attempt < retries:
                    time.sleep(_wait_s(httpx.Headers(), "", attempt))
                    continue
                raise err from e
            if r.status_code == 200:
                try:
                    data = r.json()
                except json.JSONDecodeError as e:
                    err = LLMError("llm 200 but non-JSON body", status=200,
                                   retryable=True, body=r.text)
                    if attempt < retries:
                        time.sleep(_wait_s(r.headers, r.text, attempt))
                        continue
                    raise err from e
                ch = (data.get("choices") or [{}])[0]
                text = (ch.get("message") or {}).get("content") or ""
                u = data.get("usage") or {}
                prov = {
                    "model": c["model"],
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "prompt_tokens": u.get("prompt_tokens"),
                    "completion_tokens": u.get("completion_tokens"),
                    "tag": tag,
                }
                return {"text": text, "prov": prov}
            retryable = r.status_code in _RETRYABLE_HTTP or r.status_code >= 500
            err = LLMError(f"llm http {r.status_code}", status=r.status_code,
                           retryable=retryable, body=r.text)
            if retryable and attempt < retries:
                time.sleep(_wait_s(r.headers, r.text, attempt))
                continue
            raise err
    raise LLMError("unreachable", retryable=False)  # pragma: no cover


# ---------- JSON extraction ----------

def extract_json(text: str):
    """从网关文本剥出首个 JSON obj/array：先试整体，再 ```json 围栏，再首个 {/[ → 末个 }/]。"""
    if not text or not text.strip():
        raise LLMError("empty LLM response (reasoning 烧光 max_tokens?)",
                       retryable=True)
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"```(?:json)?[ \t]*\r?\n(.*?)```", s, re.S):
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
    i = min((i for i in (s.find("{"), s.find("[")) if i >= 0), default=-1)
    if i >= 0:
        j = s.rfind("}" if s[i] == "{" else "]")
        if j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError("no parseable JSON in LLM response", retryable=False,
                   body=s)


def chat_json(messages: list[dict], retries: int = 2, *,
              prov_out: list | None = None, **kw):
    """chat(want_json=True) + extract_json；解析失败追加「只输出JSON对象」重试。

    prov_out: 传入 list 则每次底层 chat 的 prov 追加进去（stage 落 artifact 用）。
    """
    kw.setdefault("want_json", True)
    msgs = list(messages)
    last: LLMError | None = None
    for _ in range(retries + 1):
        res = chat(msgs, **kw)
        if prov_out is not None:
            prov_out.append(res["prov"])
        try:
            return extract_json(res["text"])
        except LLMError as e:
            last = e
            msgs = msgs + [
                {"role": "assistant", "content": res["text"] or "(empty)"},
                {"role": "user", "content": _JSON_RETRY_HINT},
            ]
    raise LLMError(f"chat_json: 连续 {retries + 1} 次未返回可解析 JSON: {last}",
                   retryable=False, body=(last.body if last else ""))


# ---------- coverage reconcile ----------

def coverage_reconcile(inputs: list, outputs: list, key) -> dict:
    """批式调用覆盖核对（§6）：返回 {"outputs", "missing"}，missing 子集的重批由调用方做。

    key: 输出/输入 dict 里的同名字段名，或 callable item->id；标量条目直接用自身。
    outputs 中键不属于 inputs、或重复出现的条目被丢弃（保留首次）。
    """
    kof = key if callable(key) else (
        lambda x: x.get(key) if isinstance(x, dict) else x)
    keys_in = [kof(it) for it in inputs]
    in_set = set(keys_in)
    seen, kept = set(), []
    for o in outputs or []:
        k = kof(o)
        if k in in_set and k not in seen:
            seen.add(k)
            kept.append(o)
    missing = [it for it, k in zip(inputs, keys_in) if k not in seen]
    return {"outputs": kept, "missing": missing}


# ---------- doctor selftest（PLAN §3.6）----------

if __name__ == "__main__":
    cfg = load_cfg()
    provs: list = []
    t0 = time.time()
    out = chat_json(
        [{"role": "user", "content":
          '只输出JSON对象 {"ok": true, "sum": 1+1的结果}，不要输出任何其他内容。'}],
        prov_out=provs, tag="selftest", cfg=cfg)
    dt = time.time() - t0
    assert isinstance(out, dict) and out.get("ok") is True, f"bad reply: {out!r}"
    p = provs[-1]
    print(f"selftest ok in {dt:.1f}s model={p['model']} "
          f"prompt_tokens={p['prompt_tokens']} completion_tokens={p['completion_tokens']} "
          f"-> {out}")
