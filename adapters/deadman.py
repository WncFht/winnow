# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Dead-man switch ping — PLAN.md §7.9 / §9 (healthchecks.io style).

    from adapters import deadman
    deadman.ping(config=cfg)            # 全部成功 -> GET ping_url
    deadman.ping(config=cfg, suffix="/fail")   # report failure state

Contract:
  * GETs `config.alerts.deadman_ping_url` (+ optional suffix such as
    "/start" or "/fail" — healthchecks.io semantics; keep "" for the
    plain success ping).
  * Empty/missing URL -> log a warning, return False.
  * NEVER raises: a dead-man ping must not kill the pipeline. Every
    failure (bad config, DNS, refused, non-2xx) returns False.
  * `config` may be: dict, any object with attributes, a path to a yaml
    file, or None -> loads config.yaml, falling back to
    config.example.yaml, from the repo root (same rule as justfile `cfg`).
  * proxy: explicit `proxy=` kwarg > config alerts.proxy > proxy.http >
    http_proxy/https_proxy env vars (urllib default).

CLI:
    uv run adapters/deadman.py --selftest   # offline, no real ping
    uv run adapters/deadman.py --send-test  # real GET to configured URL
"""

from __future__ import annotations

import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger("adapters.deadman")

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CANDIDATES = ("config.yaml", "config.example.yaml")

DEFAULT_TIMEOUT = 10.0  # matches justfile doctor `curl -m 10`


# ---------------------------------------------------------------------------
# config plumbing (stdlib-only; duplicated in alert_ntfy.py on purpose —
# adapters stay self-contained single files)
# ---------------------------------------------------------------------------

def _cfg_get(config: Any, dotted: str, default: Any = "") -> Any:
    """Dotted lookup ('alerts.deadman_ping_url') over dicts or attr objects."""
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
    """Dependency-free fallback parser for 2-level `key: value` maps."""
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
    """Load pipeline config. Never raises — returns {} on any failure."""
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


def _opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()  # env proxies honored by default


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def ping(
    config: Any = None,
    *,
    suffix: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    proxy: Optional[str] = None,
) -> bool:
    """GET the deadman ping URL. Returns True on HTTP 2xx, False otherwise.

    `suffix` ("" | "/start" | "/fail") is appended verbatim to the
    configured URL, matching healthchecks.io signal semantics.
    Never raises.
    """
    try:
        cfg = _resolve_config(config)
        url = str(_cfg_get(cfg, "alerts.deadman_ping_url", "") or "").strip()
        if not url:
            log.warning("alerts.deadman_ping_url unset — deadman ping skipped")
            return False
        if suffix:
            url = url.rstrip("/") + "/" + suffix.lstrip("/")

        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "ai-news-pipeline/deadman"})
        with _opener(_resolve_proxy(cfg, proxy)).open(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                log.warning("deadman ping -> HTTP %s for %s", resp.status, url)
            return ok
    except urllib.error.HTTPError as e:
        log.warning("deadman ping -> HTTP %s (%s)", e.code, e.reason)
        return False
    except Exception as e:
        log.warning("deadman ping failed: %s", e)
        return False


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
        def do_GET(self) -> None:
            hits.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

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
        check("empty dict -> False", ping(config={}) is False)
        check("empty alerts -> False", ping(config={"alerts": {}}) is False)
        check("blank url -> False",
              ping(config={"alerts": {"deadman_ping_url": "  "}}) is False)
        check("malformed url -> False",
              ping(config={"alerts": {"deadman_ping_url": "not a url"}}) is False)
        check("refused conn -> False",
              ping(timeout=3.0,
                   config={"alerts": {"deadman_ping_url": "http://127.0.0.1:1/x"}}) is False)
        check("missing cfg file -> False", ping(config="/nonexistent/definitely.yaml") is False)
        check("load_config() no-raise", isinstance(load_config(), dict))
        check("mini yaml fallback",
              _mini_yaml('alerts:\n  deadman_ping_url: "https://hc/x"\n').get("alerts", {}).get("deadman_ping_url")
              == "https://hc/x")

        # --- real code path against local throwaway server ---
        ok = ping(config={"alerts": {"deadman_ping_url": f"http://127.0.0.1:{port}/uuid-abc"}})
        check("local GET -> True", ok is True)
        check("method+path", hits and hits[-1] == ("GET", "/uuid-abc"))

        ok2 = ping(suffix="/fail",
                   config={"alerts": {"deadman_ping_url": f"http://127.0.0.1:{port}/uuid-abc"}})
        check("suffix /fail -> True", ok2 is True)
        check("suffix path joined", hits and hits[-1] == ("GET", "/uuid-abc/fail"))
    finally:
        srv.shutdown()
        srv.server_close()

    print(f"deadman selftest: {'FAIL ' + str(failures) if failures else 'all pass'}")
    return 1 if failures else 0


def main(argv: Optional[list] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="dead-man switch ping adapter")
    ap.add_argument("--selftest", action="store_true",
                    help="offline self-test (no real ping)")
    ap.add_argument("--send-test", action="store_true",
                    help="send ONE real GET to configured deadman_ping_url")
    ap.add_argument("--suffix", default="", help='e.g. "/start" or "/fail"')
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.send_test:
        ok = ping(suffix=args.suffix)
        print(f"ping -> {ok}")
        return 0 if ok else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
