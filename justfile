# =============================================================================
# AI 早报 pipeline — thin driver (PLAN.md §9)
#
#   just gather            collect -> filter -> dedup          (auto block A)
#   just pick              gate 1: serve review UI             (HUMAN)
#   just pick-auto         gate 1: deadline auto top-K         (non-interactive)
#   just produce           digest -> callb -> voice -> cards -> subs -> render-plan -> compose -> meta
#   just edit              gate 2: open 50_review.md in $EDITOR (HUMAN)
#   just edit-import       gate 2: import edited review.md -> 50_issue.json
#   just deadline1         gate-1 deadline watcher (ops/*.timer 08:30)
#   just deadline2         gate-2 deadline watcher (ops/*.timer 09:30)
#   just all               = gather (recipes never chain across human gates)
#   just resume [date]     re-run missing/failed stages from 00_meta.json
#   just from <stage>      force-run from a stage to end of its block — kills
#                          the "just a && just b" antipattern（跨 just 进程在
#                          .just.lock 上排队会饿死；同一 just 调用内的多配方
#                          是顺序执行不排队）
#
#   just status            rich 仪表盘：阶段状态 + 运行中进度 + 锁持有者
#   just watch             同上但 Live 实时刷新（只读，不占 .just.lock）
#   just tail [stage]      tail -F run 日志（缺省全量交错）
#
#   just test              离线回归：全部 --selftest 并行 + compileall
#   just exp <name> <stage> [args]   实验沙箱 runs/_exp-<name>（非日期目录
#                          → 池写自动豁免），透传阶段参数
#   just doctor            联网冒烟全组件（PLAN §3.6，与 test 互补）
#
#   just setup-toolchain   one-time machine setup (PLAN §3)
#   just lint-sources      sources.yaml lint (PLAN §5.1)
#   just judge-eval        dedup judge accuracy (PENDING — flag 未实现)
#   just shot-test         screenshot-pipeline self test (PLAN §7.6)
#
# Run a past/future date bucket:  just DATE=2026-09-20 gather
#
# Locking (§9): every stage line is prefixed with _jlock on
# runs/<d>/.just.lock — a file *separate* from the stage-internal `.lock`
# (stages/lib/meta.py run_lock). flock(1) exec's the stage while holding an
# OFD lock; if both used the same inode the stage's own flock would block on
# its parent forever -> guaranteed deadlock. .just.lock serializes only
# just-level invocations for the same run bucket.
#
# _jlock 实现移进 ops/prelude.sh（两段式：-n 试探 → 占用时经 lslocks 打印
# 持锁者 stage/pid/elapsed 再 -w 3600 排队；超时 exit 200）。残留锁文件
# 无害：flock 绑定打开 inode，锁随持锁进程释放。
#
# Progress protocol: 阶段内长循环统一经 stages/lib/prog.py 上报——stderr
# 人类行 + logs/<stage>.prog.jsonl 结构化事件（just watch 消费）；阶段入口
# meta.stage_begin() 登记 00_running.json，stage_done 自动清除，崩溃残留
# 按 /proc 判活显示为 stale。
#
# File-is-dependency-edge: a stage fails fast if its input artifact is
# missing ("run `just gather` first"); the recipe graph stays shallow.
# =============================================================================

set shell := ["bash", "-c"]

DATE        := `TZ='Asia/Shanghai' date +%F`
RUN         := "runs/" + DATE
REVIEW_PORT := "8923"
PROXY       := "http://127.0.0.1:7890"

# 配方前缀：SH = pipefail + secrets.env 导出（无 run-dir 配方用）；
# STAGE = SH + PIPELINE_RUN + logs/ 目录 + _jlock 等锁（全部运行阶段用）。
SH    := "source ops/prelude.sh; "
STAGE := "export PIPELINE_RUN=" + RUN + "; source ops/prelude.sh; _jlock "

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
    [ -f ~/.cache/embed/model_int8.onnx ] || echo "  ! ~/.cache/embed/model_int8.onnx absent — 跑 just fetch-embed（embed.py 无自动下载）"
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
    keyenv_bg=$(cfg llm.api_key_env_bg SWE2MAX_BG_API_KEY)
    temp=$(cfg llm.temperature 0.2)   # NOTE: swe-2-max 502s on temperature:0 — use configured value
    key="${!keyenv_bg:-${!keyenv:-}}"
    if [ -z "$key" ]; then skip "llm ping" "$keyenv_bg/$keyenv unset"
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
        if "daily" in s and not isinstance(s["daily"], bool):
            warns.append(f"{n}: 'daily' present but not a bool ({s['daily']!r})")
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
    {{STAGE}}uv run stages/collect.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/collect.log

filter:
    {{STAGE}}uv run stages/filter.py --run-dir {{RUN}} --jobs 24 2>&1 | tee -a {{RUN}}/logs/filter.log

dedup:
    {{STAGE}}uv run stages/dedup.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/dedup.log

# --------------------------------------------------------------------------
# gate 1 (HUMAN): 40_candidates -> 40_selected
# --------------------------------------------------------------------------

# review_server intentionally NOT flocked — it is a long-lived UI server;
# holding the run lock would block the pick-auto deadline watcher.
# manual pick: prepare candidates, then serve the checkbox UI until submit
pick:
    {{STAGE}}uv run stages/gate_select.py --run-dir {{RUN}} --prepare 2>&1 | tee -a {{RUN}}/logs/gate_select.log
    export PIPELINE_RUN={{RUN}}; source ops/prelude.sh; REVIEW_PORT={{REVIEW_PORT}} uv run stages/review_server.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/review_server.log

# prepare-only: rebuild 40_candidates.json without serving the UI (resume uses
# this — launching review_server would block the queue on a human)
pick-prepare:
    {{STAGE}}uv run stages/gate_select.py --run-dir {{RUN}} --prepare 2>&1 | tee -a {{RUN}}/logs/gate_select.log

# non-interactive pick: top-K by news_value, decided_by:auto (deadline path)
pick-auto:
    {{STAGE}}uv run stages/gate_select.py --run-dir {{RUN}} --auto 2>&1 | tee -a {{RUN}}/logs/gate_select.log

# --------------------------------------------------------------------------
# auto block B (after gate 1 + optional gate 2 edit)
# --------------------------------------------------------------------------

# Call A is skipped when 50_issue.json already exists so `just produce` is safe
# to re-run after `just edit` + `just edit-import` (edits are preserved; Call B
# still projects voice/cards/video from the imported issue).
# produce = digest(Call A) -> callb -> voice -> cards -> subs -> render-plan -> compose -> meta
produce:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f {{RUN}}/50_issue.json ]; then
      echo "[produce] 50_issue.json exists — skipping Call A (digest)"
    else
      just DATE={{DATE}} digest
    fi
    just DATE={{DATE}} callb voice cards subs render-plan compose meta

digest:
    {{STAGE}}uv run stages/digest.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/digest.log

# gate 2 (HUMAN): edit the exported review doc, then `just edit-import`
edit:
    @${EDITOR:-vi} "{{RUN}}/50_review.md"

# import edited 50_review.md -> re-validated 50_issue.json
edit-import:
    {{STAGE}}uv run stages/digest.py --run-dir {{RUN}} --import 2>&1 | tee -a {{RUN}}/logs/digest_import.log

# digest Call B: project voice/cards/video.shot_sentences into 50_issue.json +
# compliance pass. Runs AFTER the gate-2 edit (edit-import), BEFORE voice/cards.
# digest Call B projection + compliance pass (after edit-import, before voice)
callb:
    {{STAGE}}uv run stages/digest.py --run-dir {{RUN}} --callb 2>&1 | tee -a {{RUN}}/logs/digest_callb.log

voice:
    {{STAGE}}uv run stages/voice.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/voice.log

cards:
    {{STAGE}}uv run stages/cards.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/cards.log

# per-seg subtitle pill PNGs for the ffmpeg compose path (Remotion renders
# live-text pills itself and does not need this stage)
subs:
    {{STAGE}}uv run stages/subs.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/subs.log

render-plan:
    {{STAGE}}uv run stages/render_plan.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/render_plan.log

# TMPDIR pinned to a real disk — /tmp is a small tmpfs; chrome OOMs there (§7.8)
compose:
    mkdir -p state/compose-tmp
    export PIPELINE_RUN={{RUN}}; source ops/prelude.sh; TMPDIR="$PWD/state/compose-tmp" _jlock uv run stages/compose.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/compose.log

meta:
    {{STAGE}}uv run stages/meta_qa.py --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/meta_qa.log

# all = gather only — never chain across the human gates (§9)
all: gather

# --------------------------------------------------------------------------
# deadline watchers (PLAN §9) — fired by ops/*.timer; also safe to run manually
# --------------------------------------------------------------------------

# gate-1 deadline (schedule.gate1_deadline, default 08:30): auto-release top-K
# when no human pick landed, then run digest so 50_review.md exists inside the
# edit window. gate_select --deadline-check already no-ops before the deadline
# and never overwrites an existing 40_selected.json (human pick wins).
# gate-1 deadline watcher: auto top-K if unsubmitted past 08:30, then digest
deadline1:
    #!/usr/bin/env bash
    set -euo pipefail
    export PIPELINE_RUN={{RUN}}; source ops/prelude.sh
    dl=$(uv run -q --with pyyaml python3 - <<'PY'
    import os, yaml
    for f in ("config.yaml", "config.example.yaml"):
        if os.path.exists(f):
            v = ((yaml.safe_load(open(f)) or {}).get("schedule") or {}).get("gate1_deadline")
            print(v or "08:30")
            break
    else:
        print("08:30")
    PY
    )
    _jlock uv run stages/gate_select.py --run-dir {{RUN}} --deadline-check "$dl" 2>&1 | tee -a {{RUN}}/logs/gate_select.log \
      || { rc=$?; echo "[deadline1] gate_select failed (rc=$rc) — see {{RUN}}/logs/gate_select.log" >&2; exit "$rc"; }
    if [ ! -f {{RUN}}/40_selected.json ]; then
      echo "[deadline1] no 40_selected.json (before deadline $dl or gather incomplete) — nothing to do"
      exit 0
    fi
    if [ -f {{RUN}}/50_issue.json ]; then
      echo "[deadline1] 50_issue.json already present — done"
    else
      just DATE={{DATE}} digest
    fi

# gate-2 deadline (09:30): take 50_issue.json as it stands. If the human edited
# 50_review.md but never ran `just edit-import` (sha differs from the last
# export/import record in 00_meta.json), import once — a failed import leaves
# the last valid issue in place. Then run the rest of auto block B.
# gate-2 deadline watcher: lock issue (auto-import pending edits), then produce
deadline2:
    #!/usr/bin/env bash
    set -euo pipefail
    export PIPELINE_RUN={{RUN}}; source ops/prelude.sh
    # gate-1 safety net: if the 08:30 timer never fired, force 40+50 into being
    if [ ! -f {{RUN}}/40_selected.json ] || [ ! -f {{RUN}}/50_issue.json ]; then
      echo "[deadline2] upstream artifacts missing — running deadline1 first"
      just DATE={{DATE}} deadline1
    fi
    if [ ! -f {{RUN}}/50_issue.json ]; then
      echo "[deadline2] still no 50_issue.json — cannot continue" >&2
      exit 1
    fi
    # review_sha256 属于 stage_done(extra=) 簿记——分流在 00_stage_stats.json
    # 侧车，00_meta.json 里永远读不到；必须走 lib.meta.meta_status 的合并读视图，
    # 否则"人工改过 50_review.md"探测恒为假、自动 edit-import 永远不触发。
    if python3 - {{RUN}} <<'PY'
    import sys, os, hashlib
    sys.path.insert(0, "stages")
    rd = sys.argv[1]
    try:
        from lib.meta import meta_status
        st = meta_status(rd).get("stages") or {}
        cur = hashlib.sha256(open(os.path.join(rd, "50_review.md"), "rb").read()).hexdigest()
        base = (st.get("digest_import") or {}).get("review_sha256") \
            or (st.get("digest_export") or {}).get("review_sha256")
        sys.exit(0 if base and cur != base else 1)
    except Exception:
        sys.exit(1)
    PY
    then
      echo "[deadline2] 50_review.md modified since last import — auto edit-import"
      just DATE={{DATE}} edit-import || echo "[deadline2] WARN edit-import failed — continuing with last valid 50_issue.json" >&2
    fi
    just DATE={{DATE}} callb voice cards subs render-plan compose meta

# --------------------------------------------------------------------------
# resume / inspect
# --------------------------------------------------------------------------

# gate_select is re-run as --auto — resume never blocks on a human.
# re-run missing/failed stages for runs/<date> in DAG order (default today)
resume date=DATE:
    #!/usr/bin/env bash
    set -euo pipefail
    RD="runs/{{date}}"
    mkdir -p "$RD/logs"
    todo=$(python3 - "$RD" <<'PY'
    import sys, os
    sys.path.insert(0, "stages")
    rd = sys.argv[1]
    # stage key -> (recipe, fallback artifact)；同 recipe 去重（filter 产
    # 20+30 两个 artifact；digest 系列共享 50_issue.json）。
    # digest_export/digest_import 不在列——50_review.md 是人工编辑面，
    # resume 不该替人重生成（sha 漂移是编辑的正常状态，不是缺损）。
    ORDER = [
        ("collect",       "collect",      "11_raw_manifest.json"),
        ("filter",        "filter",       "20_filtered.jsonl"),
        ("summaries",     "filter",       "30_summaries.jsonl"),
        ("dedup",         "dedup",        "35_dedup.jsonl"),
        ("gate_prepare",  "pick-prepare", "40_candidates.json"),
        ("gate_select",   "pick-auto",    "40_selected.json"),
        ("digest",        "digest",       "50_issue.json"),
        # callb shares 50_issue.json — use voice's artifact as the fallback:
        # 62_timeline.json can only exist if Call B already projected voice[].
        ("digest_callb",  "callb",        "62_timeline.json"),
        ("voice",         "voice",        "62_timeline.json"),
        ("cards",         "cards",        "64_frames_manifest.json"),
        ("subs",          "subs",         "65_subs"),
        ("render_plan",   "render-plan",  "70_render_plan.json"),
        ("compose",       "compose",      "80_build_manifest.json"),
        ("meta_qa",       "meta",         "90_qa.json"),
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

    def intact(e) -> bool:
        return bool(e) and e.get("status") in OK \
            and e.get("_verify", "ok") == "ok"

    # 50_issue.json 被三个写者依次重写：Call A → edit-import → callb。
    # "digest" 行的 sha 只覆盖 Call A 那次写——后两者写完必 sha_mismatch。
    # 判定口径 = 任一写者的记录在文件上 verify ok 即视为产物完好；
    # 否则 resume 会在每个正常跑完的 run 上重跑 Call A，冲掉人工编辑与投影。
    digest_intact = any(intact(stages.get(k)) for k in
                        ("digest", "digest_import", "digest_callb"))

    seen, out = set(), []
    for name, recipe, art in ORDER:
        e = stages.get(name)
        ok = intact(e)
        if name == "digest" and digest_intact:
            ok = True
        if not ok and e is None and os.path.exists(os.path.join(rd, art)):
            ok = True                             # artifact exists, meta lost
        if not ok and recipe not in seen:
            out.append(recipe)
            seen.add(recipe)
    print("\n".join(out))
    PY
    )
    if [ -z "$todo" ]; then echo "[resume] all stages done for {{date}}"; exit 0; fi
    echo "[resume] pending:"; echo "$todo" | sed 's/^/  /'
    # 逐阶段回调 just 配方——统一走 prelude 的 _jlock（等锁有持锁者提示、
    # secrets 已导出），不再自行 flock。
    while read -r recipe; do
      [ -z "$recipe" ] && continue
      echo "[resume] === $recipe ==="
      if ! just DATE={{date}} "$recipe"; then
        echo "[resume] $recipe FAILED — fix and re-run: just resume {{date}}"
        exit 1
      fi
    done <<< "$todo"
    echo "[resume] complete for {{date}}"

# rich 仪表盘（一次性快照）：阶段 DAG 状态 + 运行中进度 + 锁持有者
status:
    {{SH}}uv run tools/watch.py --run-dir {{RUN}} --once

# 同上但 Live 实时刷新（Ctrl-C 退出；只读，不占 .just.lock）
watch:
    {{SH}}uv run tools/watch.py --run-dir {{RUN}}

# tail -F run 日志；`just tail filter` 跟单阶段，缺省全量交错
tail stage="":
    #!/usr/bin/env bash
    if [ -n "{{stage}}" ]; then
      exec tail -n 60 -F "{{RUN}}/logs/{{stage}}.log"
    fi
    shopt -s nullglob
    logs=({{RUN}}/logs/*.log)
    if [ ${#logs[@]} -eq 0 ]; then echo "[tail] {{RUN}}/logs 暂无日志"; exit 0; fi
    exec tail -n 20 -F "${logs[@]}"

# 从某阶段强制重跑到其所属 block 末尾（不用 && 串 just——跨进程排队会饿死）
# 例：just from dedup  → dedup 为止（block A 末）；just from cards → cards..meta
from stage:
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{stage}}" in
      collect)            rest="collect filter dedup" ;;
      filter)             rest="filter dedup" ;;
      dedup)              rest="dedup" ;;
      pick|gate_select)   rest="pick" ;;        # gate-1 人工：到 pick 为止
      pick-auto)          rest="pick-auto" ;;
      digest)             rest="digest callb voice cards subs render-plan compose meta" ;;
      callb|digest_callb) rest="callb voice cards subs render-plan compose meta" ;;
      edit-import)        rest="edit-import callb voice cards subs render-plan compose meta" ;;
      voice)              rest="voice cards subs render-plan compose meta" ;;
      cards)              rest="cards subs render-plan compose meta" ;;
      subs)               rest="subs render-plan compose meta" ;;
      render-plan|render_plan) rest="render-plan compose meta" ;;
      compose)            rest="compose meta" ;;
      meta|meta_qa)       rest="meta" ;;
      *) echo "[from] 未知阶段 '{{stage}}' —— collect filter dedup pick digest callb edit-import voice cards subs render-plan compose meta" >&2; exit 2 ;;
    esac
    echo "[from] {{stage}} → $rest"
    # shellcheck disable=SC2086
    just DATE={{DATE}} $rest

# 实验沙箱：runs/_exp-<name>（非日期目录 → pool 写自动豁免），参数透传阶段
# 例：just exp batchsize filter --summary-batch 4
exp name stage *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # name/stage 白名单：name 注入路径（_exp-x/../<date> 逃逸），stage 拼脚本路径
    [[ "{{name}}" =~ ^[A-Za-z0-9._-]+$ ]] \
      || { echo "[exp] name 须匹配 [A-Za-z0-9._-]+" >&2; exit 2; }
    [[ "{{stage}}" =~ ^[A-Za-z0-9_]+(/[A-Za-z0-9_]+)?$ ]] \
      || { echo "[exp] stage 须为 <name> 或 lib/<name>" >&2; exit 2; }
    RD="runs/_exp-{{name}}"
    if [ ! -d "$RD" ]; then
      mkdir -p "$RD"
      # 沙箱默认复制当日上游产物做输入（可用 --run-dir 覆盖）；无当日则空目录起跑
      for f in {{RUN}}/1*.json* {{RUN}}/20_filtered.jsonl {{RUN}}/35_dedup.jsonl {{RUN}}/40_selected.json {{RUN}}/50_issue.json; do
        [ -e "$f" ] && cp -n "$f" "$RD/" 2>/dev/null || true
      done
      echo "[exp] seeded $RD from {{RUN}}"
    fi
    mkdir -p "$RD/logs"
    export PIPELINE_RUN="$RD"; source ops/prelude.sh
    LOG="$RD/logs/$(echo '{{stage}}' | tr / _).log"
    _jlock uv run "stages/{{stage}}.py" --run-dir "$RD" {{args}} 2>&1 | tee -a "$LOG"

# 离线回归：全部 --selftest 并行 + py_compile；结果落 runs/_test/logs/*.rc
test:
    #!/usr/bin/env bash
    set -u
    SCRATCH="runs/_test"; mkdir -p "$SCRATCH/logs"
    rm -f "$SCRATCH"/logs/*.rc   # 上轮残留 .rc 会掩盖本轮没写 rc 的死掉的 subshell
    pass=0; fail=0; pids=(); names=()
    # run <名字> <cmd...> —— 名字里的 / 压成 _ 做日志文件名
    run() { local n="$1" f; shift; f="${n//\//_}"; names+=("$n")
            ( "$@" >"$SCRATCH/logs/$f.log" 2>&1; echo $? >"$SCRATCH/logs/$f.rc" ) & pids+=($!); }
    # --selftest 矩阵（离线）——加新阶段时同步这里
    for s in lib/simhash lib/normalize lib/prompts lib/ttsnorm lib/store lib/pool lib/embed lib/prog meta_qa gate_select render_plan compose; do
      run "$s" uv run "stages/$s.py" --selftest
    done
    run "lib/layout_d2" uv run stages/lib/layout_d2.py --selftest "$SCRATCH/layout-d2"
    run "digest" uv run stages/digest.py --selftest --run-dir "$SCRATCH"
    run "dedup-nojudge" uv run stages/dedup.py --selftest --no-judge
    for s in lib/http lib/shotlib lib/reddit_collect lib/weibo_collect lib/x_ssr lib/x_nitter lib/x_synd; do
      run "$s" uv run "stages/$s.py" --offline
    done
    run "pycompile" uv run -q python3 -m compileall -q stages tools
    for p in "${pids[@]}"; do wait "$p"; done
    for n in "${names[@]}"; do
      f="${n//\//_}"
      rc=$(cat "$SCRATCH/logs/$f.rc" 2>/dev/null || echo 9)
      if [ "$rc" = 0 ]; then printf "  \033[32mPASS\033[0m %s\n" "$n"; pass=$((pass+1));
      else printf "  \033[31mFAIL\033[0m %s (rc=%s) — %s\n" "$n" "$rc" "$(tail -2 "$SCRATCH/logs/$f.log" 2>/dev/null | tr '\n' ' ' | cut -c1-140)"; fail=$((fail+1)); fi
    done
    echo "test: $pass pass / $fail fail (logs: $SCRATCH/logs/)"
    [ "$fail" -eq 0 ]

ls-run:
    @ls -la {{RUN}}

# daily history.sqlite + items.sqlite backup, keep newest 14 each (§9)
backup-state:
    @mkdir -p state/backups
    @[ -f state/history.sqlite ] && cp state/history.sqlite "state/backups/history-$(date +%F-%H%M).sqlite" && echo "backed up" || echo "no history.sqlite yet"
    @[ -f state/items.sqlite ] && cp state/items.sqlite "state/backups/items-$(date +%F-%H%M).sqlite" && echo "items backed up" || echo "no items.sqlite yet"
    @ls -t state/backups/history-*.sqlite 2>/dev/null | tail -n +15 | xargs -r rm -v
    @ls -t state/backups/items-*.sqlite 2>/dev/null | tail -n +15 | xargs -r rm -v

# 拉 Qwen3-Embedding-0.6B-ONNX int8 到 ~/.cache/embed（embed.py 只读不下载；
# dedup-history/model 已出库，此配方是新克隆唯一获取路径）
fetch-embed:
    #!/usr/bin/env bash
    set -euo pipefail
    d="$HOME/.cache/embed"; mkdir -p "$d"
    base="https://hf-mirror.com/onnx-community/Qwen3-Embedding-0.6B-ONNX/resolve/main"
    [ -f "$d/model_int8.onnx" ] || curl -fL --retry 3 -o "$d/model_int8.onnx" "$base/onnx/model_int8.onnx"
    [ -f "$d/tokenizer.json" ] || curl -fL --retry 3 -o "$d/tokenizer.json" "$base/tokenizer.json"
    echo "fetch-embed: $d ready ($(du -h "$d/model_int8.onnx" | cut -f1))"

# data/raw_cache 保留 N 天：文件名 <sha8>.<ext> 无时间戳 → 一律按 mtime 判龄；
# 顶层桶不全是日期名（exp 沙箱同目录写入），故按文件清再删空目录。
# dedup 冷启动回填只读近 7 日（stages/lib/store.py），默认与其对齐。
gc-cache days="7":
    #!/usr/bin/env bash
    set -euo pipefail
    [ -d data/raw_cache ] || { echo "gc-cache: no data/raw_cache"; exit 0; }
    n=$(find data/raw_cache -type f -mtime +{{days}} | wc -l)
    find data/raw_cache -type f -mtime +{{days}} -delete
    find data/raw_cache -mindepth 1 -depth -type d -empty -delete
    echo "gc-cache: deleted $n files older than {{days}}d; pruned empty dirs"

# --------------------------------------------------------------------------
# item pool (state/items.sqlite — 跨期条目池, PLAN §5.6)
# --------------------------------------------------------------------------

# backfill pool from all runs/<date>/ dirs (idempotent; verdicts/summaries/dedup/used)
pool-import:
    uv run stages/lib/pool.py --db state/items.sqlite --import runs/ --sources sources.yaml

# row-count distribution (operator debug)
pool-stats:
    uv run stages/lib/pool.py --db state/items.sqlite --stats

# prune unjudged rows idle >90d (NULL verdict + last_seen 过期), then VACUUM
pool-vacuum:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f state/items.sqlite ]; then echo "no items.sqlite yet"; exit 0; fi
    python3 - <<'PY'
    import sqlite3
    from datetime import date, timedelta
    conn = sqlite3.connect("state/items.sqlite", timeout=10)
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    with conn:
        n = conn.execute(
            "DELETE FROM items WHERE filter_verdict IS NULL AND last_seen < ?",
            (cutoff,)).rowcount
        conn.execute("DELETE FROM item_runs WHERE item_key NOT IN"
                     " (SELECT item_key FROM items)")
    print(f"pruned {n} NULL-verdict rows with last_seen < {cutoff}")
    conn.execute("VACUUM")
    conn.close()
    print("vacuum done")
    PY

# --------------------------------------------------------------------------
# eval / dev tools (not run artifacts — no flock)
# --------------------------------------------------------------------------

# dedup judge accuracy on fixtures — PENDING: dedup.py --judge-eval 未实现，
# 此配方目前必挂；fixtures 在 experiments/dedup-llm/clusters.json
judge-eval:
    @echo "judge-eval 未实现（dedup.py 无 --judge-eval flag）。" >&2
    @echo "fixtures: experiments/dedup-llm/clusters.json — 待补 flag 后恢复" >&2
    @exit 2

# screenshot pipeline self test: domain-policy table + playwright path (§7.6)
shot-test:
    {{STAGE}}uv run stages/cards.py --shot-test --run-dir {{RUN}} 2>&1 | tee -a {{RUN}}/logs/shot_test.log
