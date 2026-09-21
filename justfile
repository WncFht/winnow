# =============================================================================
# AI 早报 pipeline — thin driver (PLAN.md §9)
#
#   just gather            collect -> filter -> dedup          (auto block A)
#   just pick              gate 1: serve review UI             (HUMAN)
#   just pick-auto         gate 1: deadline auto top-K         (non-interactive)
#   just produce           digest -> voice -> cards -> render-plan -> compose -> meta
#   just edit              gate 2: open 50_review.md in $EDITOR (HUMAN)
#   just edit-import       gate 2: import edited review.md -> 50_issue.json
#   just all               = gather (recipes never chain across human gates)
#   just resume [date]     re-run missing/failed stages from 00_meta.json
#   just status / ls-run   inspect the run bucket
#
#   just setup-toolchain   one-time machine setup (PLAN §3)
#   just doctor            smoke-test every component (PLAN §3.6)
#   just lint-sources      sources.yaml lint (PLAN §5.1)
#   just judge-eval        dedup judge accuracy report (PLAN §11)
#   just shot-test         screenshot-pipeline self test (PLAN §7.6)
#
# Run a past/future date bucket:  just DATE=2026-09-20 gather
#
# Locking (§9): every stage line is prefixed with flock(1) on
# runs/<d>/.just.lock — a file *separate* from the stage-internal `.lock`
# (stages/lib/meta.py run_lock). flock(1) exec's the stage while holding an
# OFD lock; if both used the same inode the stage's own flock would block on
# its parent forever -> guaranteed deadlock. .just.lock serializes only
# just-level invocations for the same run bucket.
#
# File-is-dependency-edge: a stage fails fast if its input artifact is
# missing ("run `just gather` first"); the recipe graph stays shallow.
# =============================================================================

set shell := ["bash", "-c"]

DATE        := `TZ='Asia/Shanghai' date +%F`
RUN         := "runs/" + DATE
REVIEW_PORT := "8923"
PROXY       := "http://127.0.0.1:7890"

# line prefixes (see header): pipefail+log dir, dotenv export, per-run flock
PREP := "set -o pipefail; mkdir -p " + RUN + "/logs; "
ENV  := "set -a; [ -f secrets.env ] && . ./secrets.env; set +a; "
LOCK := "flock " + RUN + "/.just.lock "

default:
    @just --list --unsorted

# --------------------------------------------------------------------------
# toolchain (PLAN §3)
# --------------------------------------------------------------------------

# one-time setup: verify tools, create dirs, vendor lychee, install node deps
setup-toolchain:
    #!/usr/bin/env bash
    set -u
    echo "== toolchain check (PLAN §3) =="
    miss=0
    for c in "python3 -V" "uv -V" "node -v" "npm -v" "just -V" "git --version" "flock --version" "ffmpeg -version"; do
      printf "  %-18s" "$c"
      if ! out=$($c 2>&1 | head -1); then echo "MISSING"; miss=$((miss+1)); else echo "$out"; fi
    done
    printf "  %-18s" "libx264"
    if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264; then echo "ok"; else echo "MISSING"; miss=$((miss+1)); fi

    echo "== dirs =="
    mkdir -p runs state state/backups data/raw_cache adapters/bin
    echo "  ok runs/ state/ state/backups/ data/raw_cache/ adapters/bin/"

    echo "== lychee =="
    if [ -x adapters/bin/lychee ]; then
      echo "  ok $(adapters/bin/lychee --version | head -1)"
    else
      src=$(find experiments/factcheck-layer -maxdepth 3 -name lychee -type f 2>/dev/null | head -1)
      if [ -n "$src" ]; then cp "$src" adapters/bin/lychee && chmod +x adapters/bin/lychee && echo "  ok copied $src"; else echo "  MISS no lychee binary under experiments/factcheck-layer"; miss=$((miss+1)); fi
    fi

    echo "== node deps =="
    if [ -f upstream/juya-news-card/package.json ]; then
      if [ -d upstream/juya-news-card/node_modules ]; then echo "  ok upstream/juya-news-card node_modules"
      else (cd upstream/juya-news-card && npm install) && echo "  ok upstream npm install" || { echo "  FAIL upstream npm install"; miss=$((miss+1)); }; fi
      [ -f upstream/juya-news-card/assets/htmlFont.ttf ] || [ -f upstream/juya-news-card/public/assets/htmlFont.ttf ] \
        && echo "  ok htmlFont.ttf" || echo "  ! htmlFont.ttf missing (render-batch injects CustomPreviewFont)"
      [ -d upstream/juya-news-card/public/vendor ] || echo "  ! public/vendor/ absent — CDN self-host patch (§7.6) pending"
    else
      echo "  SKIP upstream/juya-news-card not vendored"
    fi
    if [ -f composer/package.json ]; then
      if [ -d composer/node_modules ]; then echo "  ok composer node_modules"
      else (cd composer && npm install) && echo "  ok composer npm install" || { echo "  FAIL composer npm install"; miss=$((miss+1)); }; fi
    elif [ -d experiments/remotion-feas ] && { [ ! -d composer ] || [ -z "$(ls -A composer 2>/dev/null)" ]; }; then
      mkdir -p composer && cp -r experiments/remotion-feas/. composer/ && (cd composer && npm install) \
        && echo "  ok seeded composer/ from experiments/remotion-feas" \
        || { echo "  FAIL composer seed"; miss=$((miss+1)); }
    else
      echo "  SKIP composer/ (non-empty, owned elsewhere or seed absent)"
    fi

    echo "== playwright browsers =="
    if ls ~/.cache/ms-playwright/chromium-* ~/.cache/ms-playwright/chromium_headless_shell-* >/dev/null 2>&1; then
      echo "  ok ~/.cache/ms-playwright"
    else
      echo "  installing chromium ..."
      uv run -q --with playwright python -m playwright install chromium \
        && echo "  ok playwright install" || { echo "  FAIL playwright install"; miss=$((miss+1)); }
    fi

    echo "== notes =="
    [ -f config.yaml ] || echo "  ! config.yaml absent — copy config.example.yaml and fill alerts/proxy"
    [ -f secrets.env ] || echo "  ! secrets.env absent — copy secrets.env.example (SWE2MAX_API_KEY)"
    [ -d ~/.cache/embed ] || echo "  ! ~/.cache/embed absent — Qwen3-Embedding-0.6B-ONNX downloads on first embed use"
    echo "== setup-toolchain done: $miss missing =="
    [ "$miss" -eq 0 ]

# smoke test every component (PLAN §3.6); exit 1 when any hard check fails
doctor:
    #!/usr/bin/env bash
    set -u
    ROOT=$PWD
    SCRATCH="$ROOT/runs/_doctor"; mkdir -p "$SCRATCH/tmp" "$SCRATCH/cards"
    set -a; [ -f secrets.env ] && . ./secrets.env; set +a
    P=0; F=0; S=0
    pass(){ P=$((P+1)); echo "  PASS $1"; }
    fail(){ F=$((F+1)); echo "  FAIL $1 :: ${2:-}"; }
    skip(){ S=$((S+1)); echo "  SKIP $1 :: ${2:-}"; }
    cfg(){ uv run -q --with pyyaml python3 - "$1" "$2" <<'PY'
    import sys, yaml, os
    key, default = sys.argv[1], sys.argv[2]
    cfg = {}
    for f in ("config.yaml", "config.example.yaml"):
        if os.path.exists(f):
            cfg = yaml.safe_load(open(f)) or {}
            break
    cur = cfg
    for k in key.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
    print(cur if cur not in (None, "") else default)
    PY
    }

    echo "== stages =="
    if [ -f stages/collect.py ]; then
      if uv run stages/collect.py --selftest >"$SCRATCH/collect.log" 2>&1; then pass "collect --selftest"; else fail "collect --selftest" "$(tail -2 "$SCRATCH/collect.log" | tr '\n' ' ')"; fi
    else skip "collect --selftest" "stages/collect.py absent"; fi

    echo "== llm adapter (swe-2-max) =="
    base=$(cfg llm.base_url http://127.0.0.1:3033/v1)
    model=$(cfg llm.model swe-2-max)
    keyenv=$(cfg llm.api_key_env SWE2MAX_API_KEY)
    temp=$(cfg llm.temperature 0.2)   # NOTE: swe-2-max 502s on temperature:0 — use configured value
    key="${!keyenv:-}"
    if [ -z "$key" ]; then skip "llm ping" "$keyenv unset"
    else
      http=$(curl -m 120 -s -o "$SCRATCH/llm.json" -w '%{http_code}' "$base/chat/completions" \
        -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
        -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: ok\"}],\"max_tokens\":2048,\"temperature\":$temp}")
      if [ "$http" = 200 ] && python3 -c "import json,sys; d=json.load(open('$SCRATCH/llm.json')); assert (d['choices'][0]['message'].get('content') or '').strip()" 2>/dev/null
      then pass "llm ping ($model)"; else fail "llm ping" "http=$http $(head -c 160 "$SCRATCH/llm.json" 2>/dev/null)"; fi
    fi

    echo "== card renderer (upstream render-batch) =="
    if [ -f upstream/juya-news-card/scripts/render-batch.ts ]; then
      cat > "$SCRATCH/cards.json" <<'JSON'
    [{"id":"doctor","template":"claudeStyle","mainTitle":"AI 早报","cards":[{"title":"冒烟测试","desc":"<strong>doctor</strong> 卡片渲染自检 <code>render-batch</code> 管线","icon":"bolt"}]}]
    JSON
      if (cd upstream/juya-news-card && npx tsx scripts/render-batch.ts "$SCRATCH/cards.json" "$SCRATCH/cards" >"$SCRATCH/cards.log" 2>&1) && [ -s "$SCRATCH/cards/doctor.png" ]
      then pass "render-batch -> cards/doctor.png"
      else fail "render-batch" "$(tail -3 "$SCRATCH/cards.log" | tr '\n' ' ')"; fi
    else skip "render-batch" "upstream/juya-news-card absent"; fi

    echo "== remotion (composer) =="
    if [ -f composer/package.json ]; then
      echo "  (first run downloads chrome-headless-shell — may take minutes)"
      if (cd composer && TMPDIR="$SCRATCH/tmp" npx remotion render src/index.ts FullDaily "$SCRATCH/smoke.mp4" --frames=0-60 >"$SCRATCH/remotion.log" 2>&1) && [ -s "$SCRATCH/smoke.mp4" ]
      then pass "remotion render -> smoke.mp4"
      else fail "remotion render" "$(tail -3 "$SCRATCH/remotion.log" | tr '\n' ' ')"; fi
    else skip "remotion render" "composer/ not seeded"; fi

    echo "== misc tools =="
    if [ -x adapters/bin/lychee ]; then
      if adapters/bin/lychee --version >/dev/null 2>&1; then pass "lychee --version"; else fail "lychee" "not runnable"; fi
    else skip "lychee" "adapters/bin/lychee absent (run just setup-toolchain)"; fi

    if ffmpeg -hide_banner -f lavfi -i anullsrc -t 1 -c:a aac -f null - >/dev/null 2>&1
    then pass "ffmpeg anullsrc->aac"; else fail "ffmpeg" "aac encode failed"; fi

    echo "== playwright screenshot =="
    if https_proxy={{PROXY}} http_proxy={{PROXY}} uv run -q --with playwright python3 - "$SCRATCH/example.png" >"$SCRATCH/pw.log" 2>&1 <<'PY'
    import sys
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto("https://example.com", timeout=30000)
        pg.screenshot(path=sys.argv[1])
        b.close()
    PY
    then
      if [ -s "$SCRATCH/example.png" ]; then pass "playwright example.com -> png"; else fail "playwright" "empty png"; fi
    else fail "playwright" "$(tail -2 "$SCRATCH/pw.log" | tr '\n' ' ')"; fi

    echo "== embed (Qwen3-0.6B-ONNX) =="
    if [ -f stages/lib/embed.py ]; then
      if (cd stages && uv run lib/embed.py --selftest >"$SCRATCH/embed.log" 2>&1); then pass "embed --selftest"
      elif (cd stages && uv run -q --with onnxruntime --with tokenizers --with numpy python3 -c "import sys; sys.path.insert(0,'.'); import lib.embed" >>"$SCRATCH/embed.log" 2>&1); then pass "embed import"
      else fail "embed" "$(tail -2 "$SCRATCH/embed.log" | tr '\n' ' ')"; fi
    else skip "embed" "stages/lib/embed.py absent"; fi

    echo "== edge-tts =="
    if timeout 90 uv run -q --with edge-tts edge-tts --text "AI 早报冒烟测试" --voice zh-CN-YunyangNeural --write-media "$SCRATCH/tts.mp3" >"$SCRATCH/tts.log" 2>&1 \
    || timeout 90 uv run -q --with edge-tts edge-tts --text "AI 早报冒烟测试" --voice zh-CN-YunyangNeural --proxy {{PROXY}} --write-media "$SCRATCH/tts.mp3" >>"$SCRATCH/tts.log" 2>&1; then
      if [ -s "$SCRATCH/tts.mp3" ]; then pass "edge-tts -> tts.mp3"; else fail "edge-tts" "empty mp3"; fi
    else fail "edge-tts" "$(tail -2 "$SCRATCH/tts.log" | tr '\n' ' ')"; fi

    echo "== proxy / alerts =="
    if code=$(curl -x {{PROXY}} -m 10 -s -o /dev/null -w '%{http_code}' https://api.ipify.org 2>/dev/null) && [ "$code" = 200 ]
    then pass "proxy_ok ({{PROXY}})"; else fail "proxy_ok" "ipify via {{PROXY}} -> $code"; fi

    ntfy=$(cfg alerts.ntfy_url "")
    if [ -n "$ntfy" ]; then
      if curl -m 10 -s -o /dev/null -d "ai-news doctor smoke" "$ntfy"; then pass "ntfy push"; else fail "ntfy push" "$ntfy"; fi
    else skip "ntfy push" "alerts.ntfy_url unset"; fi
    deadman=$(cfg alerts.deadman_ping_url "")
    if [ -n "$deadman" ]; then
      if curl -m 10 -sf -o /dev/null "$deadman"; then pass "deadman ping"; else fail "deadman ping" "$deadman"; fi
    else skip "deadman ping" "alerts.deadman_ping_url unset"; fi

    echo "== doctor: $P pass / $F fail / $S skip =="
    [ "$F" -eq 0 ]

# --------------------------------------------------------------------------
# sources lint (PLAN §5.1)
# --------------------------------------------------------------------------

# validate sources.yaml: schema, enums, dups, sla bounds, reachability sample
lint-sources:
    #!/usr/bin/env bash
    set -u
    uv run -q --with pyyaml python3 - <<'PY'
    import sys, yaml, subprocess, urllib.parse, collections, os
    fails, warns = [], []
    try:
        docs = yaml.safe_load(open("sources.yaml", encoding="utf-8"))
    except FileNotFoundError:
        print("FAIL sources.yaml missing"); sys.exit(1)
    except yaml.YAMLError as e:
        print(f"FAIL yaml parse: {e}"); sys.exit(1)
    if not isinstance(docs, list):
        fails.append("top-level must be a list"); docs = []
    METHODS = {"rss","atom","json_api","sitemap_diff","changelog_diff","x","reddit","weibo","wechat","youtube_rss","manual"}
    TIERS = {"A","B"}; PROXIES = {"required","prefer","direct_only"}
    NO_FEED_OK = {"manual","x","weibo","wechat","reddit"}
    names, urls, domains = collections.Counter(), collections.Counter(), collections.Counter()
    enabled = []
    for i, s in enumerate(docs):
        if not isinstance(s, dict):
            fails.append(f"#{i} not a mapping"); continue
        n = s.get("name") or f"#{i}"
        for req in ("name","method","tier"):
            if req not in s: fails.append(f"{n}: missing '{req}'")
        if s.get("method") not in METHODS: fails.append(f"{n}: bad method {s.get('method')!r}")
        if s.get("tier") not in TIERS: fails.append(f"{n}: bad tier {s.get('tier')!r}")
        if "proxy" in s and s["proxy"] not in PROXIES: fails.append(f"{n}: bad proxy {s['proxy']!r}")
        sla = s.get("freshness_sla_h")
        if sla is not None:
            try:
                if not (0 < float(sla) <= 168): fails.append(f"{n}: freshness_sla_h {sla} not in (0,168]")
            except (TypeError, ValueError): fails.append(f"{n}: freshness_sla_h {sla!r} not numeric")
        mi = s.get("max_items_per_source")
        if mi is not None:
            try:
                if not (0 < int(mi) <= 200): fails.append(f"{n}: max_items_per_source {mi} >200")
            except (TypeError, ValueError): fails.append(f"{n}: max_items_per_source {mi!r} not int")
        fu = s.get("feed_url")
        if s.get("method") not in NO_FEED_OK and not fu: fails.append(f"{n}: missing feed_url")
        if fu:
            urls[fu] += 1; domains[urllib.parse.urlparse(str(fu)).netloc] += 1
        fo = s.get("failover") or []
        if not isinstance(fo, list): fails.append(f"{n}: failover not a list")
        for x in fo:
            if not (isinstance(x, str) and x.startswith("http")): fails.append(f"{n}: bad failover {x!r}")
        names[n] += 1
        if s.get("enabled") and fu: enabled.append(s)
    for k, v in names.items():
        if v > 1: fails.append(f"dup name: {k} x{v}")
    for k, v in urls.items():
        if v > 1: fails.append(f"dup feed_url: {k} x{v}")
    for k, v in domains.items():
        if v > 1: warns.append(f"dup domain: {k} x{v} (multiple feeds share host)")
    proxy = os.environ.get("PIPELINE_PROXY", "http://127.0.0.1:7890")
    print(f"-- reachability sample ({min(5,len(enabled))}/{len(enabled)} enabled) --")
    for s in enabled[:5]:
        def probe(extra):
            return subprocess.run(["curl","-m","10","-s","-o","/dev/null","-w","%{http_code}","-L",*extra,str(s["feed_url"])],
                                  capture_output=True, text=True).stdout.strip()
        code, via = probe([]), "direct"
        if s.get("proxy") == "required" or not code.startswith(("2","3")):
            c2 = probe(["-x", proxy])
            if c2.startswith(("2","3")): code, via = c2, "proxy"
        ok = code.startswith(("2","3"))
        if not ok: warns.append(f"unreachable: {s['name']} {s['feed_url']} -> {code}")
        print(f"  {'ok' if ok else 'DOWN':4} {s['name']:<28} {code} via {via}")
    for w in warns: print(f"WARN {w}")
    for f in fails: print(f"FAIL {f}")
    print(f"lint-sources: {len(docs)} sources, {len(fails)} fail / {len(warns)} warn")
    sys.exit(1 if fails else 0)
    PY

# --------------------------------------------------------------------------
# auto block A (no human): collect -> filter -> dedup
# --------------------------------------------------------------------------

# gather = collect + filter + dedup (chain-safe, no human gate)
gather: collect filter dedup

collect:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/collect.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/collect.log

filter:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/filter.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/filter.log

dedup:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/dedup.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/dedup.log

# --------------------------------------------------------------------------
# gate 1 (HUMAN): 40_candidates -> 40_selected
# --------------------------------------------------------------------------

# review_server intentionally NOT flocked — it is a long-lived UI server;
# holding the run lock would block the pick-auto deadline watcher.
# manual pick: prepare candidates, then serve the checkbox UI until submit
pick:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/gate_select.py --run-dir {{RUN}} --prepare 2>&1 | tee -a {{RUN}}/logs/gate_select.log
    {{PREP}}{{ENV}}REVIEW_PORT={{REVIEW_PORT}} uv run stages/review_server.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/review_server.log

# non-interactive pick: top-K by news_value, decided_by:auto (deadline path)
pick-auto:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/gate_select.py --run-dir {{RUN}} --auto 2>&1 | tee -a {{RUN}}/logs/gate_select.log

# --------------------------------------------------------------------------
# auto block B (after gate 1 + optional gate 2 edit)
# --------------------------------------------------------------------------

# produce = digest -> voice -> cards -> render-plan -> compose -> meta
produce: digest voice cards render-plan compose meta

digest:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/digest.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/digest.log

# gate 2 (HUMAN): edit the exported review doc, then `just edit-import`
edit:
    @${EDITOR:-vi} "{{RUN}}/50_review.md"

# import edited 50_review.md -> re-validated 50_issue.json
edit-import:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/digest.py --run-dir {{RUN}} --import 2>&1 | tee -a {{RUN}}/logs/digest_import.log

voice:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/voice.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/voice.log

cards:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/cards.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/cards.log

render-plan:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/render_plan.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/render_plan.log

# TMPDIR pinned to a real disk — /tmp is a small tmpfs; chrome OOMs there (§7.8)
compose:
    mkdir -p state/compose-tmp
    {{PREP}}{{ENV}}TMPDIR="$PWD/state/compose-tmp" {{LOCK}}uv run stages/compose.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/compose.log

meta:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/meta_qa.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/meta_qa.log

# all = gather only — never chain across the human gates (§9)
all: gather

# --------------------------------------------------------------------------
# resume / inspect
# --------------------------------------------------------------------------

# gate_select is re-run as --auto — resume never blocks on a human.
# re-run missing/failed stages for runs/<date> in DAG order (default today)
resume date=DATE:
    #!/usr/bin/env bash
    set -uo pipefail
    RD="runs/{{date}}"
    mkdir -p "$RD/logs"
    todo=$(python3 - "$RD" <<'PY'
    import sys, os
    sys.path.insert(0, "stages")
    rd = sys.argv[1]
    # stage key -> (script, fallback artifact, extra args)
    ORDER = [
        ("collect",     "collect.py",     "11_raw_manifest.json",    []),
        ("filter",      "filter.py",      "20_filtered.jsonl",       []),
        ("dedup",       "dedup.py",       "35_dedup.jsonl",          []),
        ("gate_select", "gate_select.py", "40_selected.json",        ["--auto"]),
        ("digest",      "digest.py",      "50_issue.json",           []),
        ("voice",       "voice.py",       "62_timeline.json",        []),
        ("cards",       "cards.py",       "64_frames_manifest.json", []),
        ("render_plan", "render_plan.py", "70_render_plan.json",     []),
        ("compose",     "compose.py",     "80_build_manifest.json",  []),
        ("meta_qa",     "meta_qa.py",     "90_qa.json",              []),
    ]
    try:
        from lib.meta import meta_status          # authoritative verify (sha)
        meta = meta_status(rd, verify=True)
    except Exception:
        import json
        try:    meta = json.load(open(os.path.join(rd, "00_meta.json")))
        except Exception: meta = {}
    stages = meta.get("stages") if isinstance(meta, dict) else {}
    stages = stages or {}
    OK = {"ok", "done", "success", "passed"}
    out = []
    for name, script, art, extra in ORDER:
        e = stages.get(name)
        if e and e.get("status") in OK and e.get("_verify", "ok") == "ok":
            continue                                # recorded + artifact intact
        if e is None and os.path.exists(os.path.join(rd, art)):
            continue                                # artifact exists, meta lost
        out.append("\t".join([name, script] + extra))
    print("\n".join(out))
    PY
    )
    if [ -z "$todo" ]; then echo "[resume] all stages done for {{date}}"; exit 0; fi
    echo "[resume] pending:"; echo "$todo" | sed 's/^/  /'
    set -o pipefail
    set -a; [ -f secrets.env ] && . ./secrets.env; set +a
    while IFS=$'\t' read -r name script extra; do
      [ -z "$name" ] && continue
      echo "[resume] === $name ==="
      flock "$RD/.just.lock" uv run "stages/$script" --run-dir "$RD" $extra 2>&1 | tee -a "$RD/logs/$name.log" \
        || { echo "[resume] $name FAILED — fix and re-run: just resume {{date}}"; exit 1; }
    done <<< "$todo"
    echo "[resume] complete for {{date}}"

# show recorded stage status of runs/<date>
status:
    #!/usr/bin/env bash
    python3 - "{{RUN}}" <<'PY'
    import json, os, sys
    p = os.path.join(sys.argv[1], "00_meta.json")
    try:
        m = json.load(open(p))
    except Exception:
        print(f"{sys.argv[1]}: no 00_meta.json — run `just gather` first"); raise SystemExit(0)
    for k, v in (m.get("stages") or {}).items():
        print(f"  {k:14} {v.get('status','?'):8} {v.get('artifact') or '-':32} {v.get('produced_at','')}")
    if not m.get("stages"): print("  (stages{} empty)")
    PY

ls-run:
    @ls -la {{RUN}}

# daily history.sqlite backup, keep newest 14 (§9)
backup-state:
    @mkdir -p state/backups
    @[ -f state/history.sqlite ] && cp state/history.sqlite "state/backups/history-$(date +%F-%H%M).sqlite" && echo "backed up" || echo "no history.sqlite yet"
    @ls -t state/backups/history-*.sqlite 2>/dev/null | tail -n +15 | xargs -r rm -v

# --------------------------------------------------------------------------
# eval / dev tools (not run artifacts — no flock)
# --------------------------------------------------------------------------

# dedup judge accuracy on fixtures (experiments/dedup-llm + dedup-lab seed)
judge-eval:
    {{ENV}}uv run stages/dedup.py --judge-eval

# screenshot pipeline self test: domain-policy table + playwright path (§7.6)
shot-test:
    {{PREP}}{{ENV}}{{LOCK}}uv run stages/cards.py --shot-test --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/shot_test.log
