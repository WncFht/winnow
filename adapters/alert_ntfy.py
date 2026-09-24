# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""ntfy alert adapter — PLAN.md §7.9 / §9, decision D9.

    from adapters import alert_ntfy as alert
    alert.push("采集完成", "勾选闸开放，死线 08:30", config=cfg)
    alert.push("compose 崩溃", str(err), priority="urgent",
               tags=["rotating_light"], config=cfg)
    alert.fatal("compose 崩溃", str(err), config=cfg)   # ≡ urgent + rotating_light
    alert.notify("采集降级", "proxy 不可达", config=cfg)  # ≡ default 通道

Contract:
  * POSTs `msg` (utf-8 body) to `config.alerts.ntfy_url` — an ntfy.sh or
    self-hosted topic URL — with `Title` / `Priority` / `Tags` headers.
  * Empty/missing ntfy_url -> log a warning, return False.
  * NEVER raises: alerting must not kill the pipeline. Every failure
    (bad config, DNS, refused, non-2xx) returns False.
  * `config` may be: dict, any object with attributes (pydantic model),
    a path to a yaml file, or None -> loads config.yaml, falling back to
    config.example.yaml, from the repo root (same rule as justfile `cfg`).
  * priority: min|low|default|high|max|urgent (or 1-5). §9 告警分级:
    fatal -> "urgent"; degraded / flags -> "default".
  * 两通道封装（§9）：fatal(title, msg, config, **kw) = push(priority
    "urgent", tags ["rotating_light"]) —— 管道炸/阻断性事件；
    notify(...) = push(priority "default") —— 降级/flags 类提醒。
    kwargs 可再覆盖默认 priority/tags。
  * proxy: explicit `proxy=` kwarg > config alerts.proxy > proxy.http >
    http_proxy/https_proxy env vars (urllib default behavior).

Non-ASCII titles are sent as UTF-8 bytes on the wire (latin-1 smuggle);
ntfy's Go server reads header bytes as UTF-8 so Chinese titles render
correctly — stdlib urllib rejects raw non-latin-1 header strs.

CLI:
    uv run adapters/alert_ntfy.py --selftest   # offline, no real push
    uv run adapters/alert_ntfy.py --send-test  # real push to configured URL
"""

from __future__ import annotations

import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional, Union

log = logging.getLogger("adapters.alert_ntfy")

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CANDIDATES = ("config.yaml", "config.example.yaml")

PRIORITY_NAMES = {"min", "low", "default", "high", "max", "urgent"}
PRIORITY_NUMS = {"1", "2", "3", "4", "5"}

DEFAULT_TIMEOUT = 10.0  # matches justfile doctor `curl -m 10`


# ---------------------------------------------------------------------------
# config plumbing (kept stdlib-only; duplicated in deadman.py on purpose —
# adapters stay self-contained single files)
# ---------------------------------------------------------------------------

def _cfg_get(config: Any, dotted: str, default: Any = "") -> Any:
    """Dotted lookup ('alerts.ntfy_url') over dicts or attribute objects."""
    cur = config
    for part in dotted.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return default if cur is None else cur


def _mini_yaml(text: str) -> dict:
    """Dependency-free fallback parser for 2-level `key: value` maps.

    Only a safety net for when pyyaml is unavailable and yaml itself is
    broken/missing — enough to read `alerts:\\n  ntfy_url: "..."` blocks.
    """
    root: dict = {}
    stack: list = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().strip("'\"")
        if not key:
            continue
        val = val.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if not val:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            continue
        if len(val) >= 2 and val[0] in "\"'":
            end = val.find(val[0], 1)
            val = val[1:end] if end != -1 else val.strip("\"'")
        elif " #" in val:  # strip unquoted inline comment
            val = val.split(" #", 1)[0].rstrip()
        parent[key] = val
    return root


def load_config(path: Union[str, Path, None] = None) -> dict:
    """Load pipeline config. Never raises — returns {} on any failure.

    With path=None tries config.yaml then config.example.yaml at repo root.
    """
    cands = [Path(path)] if path is not None else [REPO_ROOT / n for n in CONFIG_CANDIDATES]
    for cand in cands:
        try:
            if not cand.is_file():
                continue
            text = cand.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("cannot read config %s: %s", cand, e)
            continue
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
        except ImportError:
            data = _mini_yaml(text)
        except Exception as e:
            log.warning("yaml parse failed for %s (%s) — mini parser fallback", cand, e)
            data = _mini_yaml(text)
        if isinstance(data, dict):
            return data
        log.warning("config %s parsed to non-mapping — ignoring", cand)
        return {}
    return {}


def _resolve_config(config: Any) -> Any:
    """None -> repo config files; str/Path -> yaml file at that path; else as-is."""
    if config is None:
        return load_config()
    if isinstance(config, (str, Path)):
        return load_config(config)
    return config


def _resolve_proxy(cfg: Any, proxy: Optional[str]) -> Optional[str]:
    if proxy:
        return proxy
    for dotted in ("alerts.proxy", "proxy.http"):
        val = str(_cfg_get(cfg, dotted, "") or "").strip()
        if val:
            return val
    return None


# ---------------------------------------------------------------------------
# HTTP (stdlib urllib: honors *_proxy env vars; explicit proxy via ProxyHandler)
# ---------------------------------------------------------------------------

def _h(value: str) -> str:
    """Smuggle UTF-8 bytes through latin-1 so urllib accepts them; ntfy (Go)
    interprets the wire bytes as UTF-8, so Chinese titles display correctly."""
    return value.encode("utf-8").decode("latin-1")


def _opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()  # env proxies honored by default


def push(
    title: str,
    msg: str,
    config: Any = None,
    *,
    priority: Union[str, int] = "default",
    tags: Union[Iterable[str], str, None] = None,
    timeout: float = DEFAULT_TIMEOUT,
    proxy: Optional[str] = None,
) -> bool:
    """POST an ntfy notification. Returns True on HTTP 2xx, False otherwise.

    Never raises — alerting must not kill the pipeline.
    """
    try:
        cfg = _resolve_config(config)
        url = str(_cfg_get(cfg, "alerts.ntfy_url", "") or "").strip()
        if not url:
            log.warning("alerts.ntfy_url unset — ntfy push skipped: %s", title)
            return False

        pr = str(priority).strip().lower()
        if pr not in PRIORITY_NAMES and pr not in PRIORITY_NUMS:
            log.warning("unknown ntfy priority %r — using 'default'", priority)
            pr = "default"

        if tags is None:
            tag_list: list = []
        elif isinstance(tags, str):
            tag_list = [tags]
        else:
            tag_list = [str(t) for t in tags]

        headers = {"Title": _h(str(title)), "Priority": pr}
        if tag_list:
            headers["Tags"] = _h(",".join(tag_list))
        token = str(_cfg_get(cfg, "alerts.ntfy_token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(
            url, data=str(msg).encode("utf-8"), headers=headers, method="POST"
        )
        with _opener(_resolve_proxy(cfg, proxy)).open(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                log.warning("ntfy push -> HTTP %s for %s", resp.status, url)
            return ok
    except urllib.error.HTTPError as e:
        log.warning("ntfy push -> HTTP %s (%s)", e.code, e.reason)
        return False
    except Exception as e:
        log.warning("ntfy push failed: %s", e)
        return False


# convenience wrappers matching §9 告警分级
def fatal(title: str, msg: str, config: Any = None, **kw: Any) -> bool:
    """fatal 级告警 — ntfy urgent priority."""
    kw.setdefault("priority", "urgent")
    kw.setdefault("tags", ["rotating_light"])
    return push(title, msg, config, **kw)


def notify(title: str, msg: str, config: Any = None, **kw: Any) -> bool:
    """degraded/flags 级告警 — default priority."""
    kw.setdefault("priority", "default")
    return push(title, msg, config, **kw)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _selftest() -> int:
    """Offline self-test: empty configs -> False without exception; the only
    traffic goes to a throwaway http.server on 127.0.0.1. No real pings."""
    import http.server
    import threading

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    hits: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def _rec(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            hits.append((self.command, self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        do_GET = _rec
        do_POST = _rec

        def log_message(self, *a: Any) -> None:  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    failures = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'PASS' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    try:
        # --- empty config paths: must return False, never raise ---
        check("empty dict -> False", push("t", "m", config={}) is False)
        check("empty alerts -> False", push("t", "m", config={"alerts": {}}) is False)
        check("blank url -> False",
              push("t", "m", config={"alerts": {"ntfy_url": "   "}}) is False)
        check("malformed url -> False",
              push("t", "m", config={"alerts": {"ntfy_url": "not a url"}}) is False)
        check("refused conn -> False",
              push("t", "m", timeout=3.0,
                   config={"alerts": {"ntfy_url": "http://127.0.0.1:1/x"}}) is False)
        check("missing cfg file -> False",
              push("t", "m", config="/nonexistent/definitely.yaml") is False)
        check("load_config() no-raise", isinstance(load_config(), dict))
        check("mini yaml fallback",
              _mini_yaml('alerts:\n  ntfy_url: "https://x/t" # c\n').get("alerts", {}).get("ntfy_url")
              == "https://x/t")

        # --- real code path against local throwaway server ---
        ok = push("标题：采集完成", "正文：10 条待审，死线 08:30",
                  priority="urgent", tags=["rotating_light", "memo"],
                  config={"alerts": {"ntfy_url": f"http://127.0.0.1:{port}/topic"}})
        check("local POST -> True", ok is True)
        if hits:
            method, path, hdrs, body = hits[-1]
            check("method POST", method == "POST")
            check("path /topic", path == "/topic")
            title_hdr = hdrs.get("Title", "")
            try:
                title_dec = title_hdr.encode("latin-1").decode("utf-8")
            except Exception:
                title_dec = ""
            check("title utf8 round-trip", title_dec == "标题：采集完成")
            check("priority urgent", hdrs.get("Priority") == "urgent")
            check("tags joined", "rotating_light" in hdrs.get("Tags", "")
                  and "memo" in hdrs.get("Tags", ""))
            check("body utf8", body == "正文：10 条待审，死线 08:30".encode("utf-8"))
        else:
            check("server got a hit", False)

        bad = push("t", "m", priority="bogus",
                   config={"alerts": {"ntfy_url": f"http://127.0.0.1:{port}/t2"}})
        check("bogus priority still sends (default)", bad is True
              and hits[-1][2].get("Priority") == "default")
    finally:
        srv.shutdown()
        srv.server_close()

    print(f"alert_ntfy selftest: {'FAIL ' + str(failures) if failures else 'all pass'}")
    return 1 if failures else 0


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="ntfy alert adapter")
    ap.add_argument("--selftest", action="store_true",
                    help="offline self-test (no real push)")
    ap.add_argument("--send-test", action="store_true",
                    help="send ONE real test push to configured ntfy_url")
    ap.add_argument("--title", default="winnow test")
    ap.add_argument("--msg", default="alert_ntfy --send-test smoke ping")
    ap.add_argument("--priority", default="default")
    ap.add_argument("--tag", action="append", dest="tags", default=[])
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.send_test:
        ok = push(args.title, args.msg, priority=args.priority,
                  tags=args.tags or ["test_tube"])
        print(f"push -> {ok}")
        return 0 if ok else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
