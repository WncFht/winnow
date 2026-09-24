# Winnow

> **风选 /wɪn.oʊ/** — throw the stream into the air; the wind takes the chaff, the grain falls. ~160 sources in, one narrated video out.

[![offline-test](../../actions/workflows/offline-test.yml/badge.svg)](../../actions/workflows/offline-test.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**[中文版 README → README.zh-CN.md](README.zh-CN.md)** · [Contributing](docs/CONTRIBUTING.md)

Winnow is a daily AI-news production line: it collects from ~160 sources, filters and deduplicates with an LLM, puts a human in the loop at two editorial gates (each with a deadline auto-release), then synthesizes a finished narrated mp4 — title, cover, QA included. Document output is planned next. Everything is driven by `just`; `docs/PLAN.md` is the single source of truth for design decisions.

## Pipeline overview

```
collect      sources.yaml ~160 sources → raw fetch (JSON Feed 1.1 + pipe fields)
filter       L0 rules + LLM verdict/summary (state/state.sqlite pool cache)
dedup        same-day clustering + cross-day cascade (state/state.sqlite dedup_*)
── gate 1: human pick ─────────────────────────────────────────────
  just pick serves review UI (0.0.0.0:8923, one-time token URL ?t=…)
  deadline 08:30 missed → pick-auto takes top-K by news_value
digest       Call A: selection → 50_issue.json + 50_review.md
── gate 2: human edit ────────────────────────────────────────────
  just edit 50_review.md → just edit-import re-validates & reloads
  deadline 09:30 auto-imports (sha drift detection)
callb        Call B: projects voice/cards/video.shot_sentences + compliance pass
voice        per-sentence TTS → audio + timeline (breeze local / edge fallback)
cards        upstream/juya-news-card templates + source-page shot overlays
subs         subtitle pill PNGs (ffmpeg compose path)
render-plan  70_render_plan.json — absolute timeline shared by both engines
compose      ffmpeg → out/final.mp4 (Remotion composer is the alt engine)
meta         title candidates / cover / QA → 90_qa.json
```

~40 `just` recipes drive it: `gather` `pick` `produce` `status` `watch` `tail` `from` `resume` `exp` `doctor` `test` … Recipes never chain across human gates; `runs/<date>/.just.lock` serializes same-day invocations. `ops/winnow-*.timer` (systemd): 06:30 collect, 08:30 gate-1 deadline, 09:30 gate-2 deadline — fully unattended when nobody shows up.

Per-episode state: `state/state.sqlite` (cross-episode: item pool + dedup history + source state + kv) + `runs/YYYY-MM-DD/` (full artifacts + logs/ + `00_meta.json` stage ledger).

## Quickstart

Requires: python ≥3.11, uv, node/npm, just, flock, ffmpeg (libx264). Chromium, node_modules, lychee are installed by `setup-toolchain`.

```bash
cp secrets.env.example secrets.env    # fill SWE2MAX_API_KEY (local LLM gateway)
cp config.example.yaml config.yaml    # tune alerts/proxy/schedule

just setup-toolchain   # one-shot: tool check + dirs + npm install + playwright
just fetch-embed       # Qwen3-Embedding-0.6B-ONNX → ~/.cache/embed (dedup needs it)
just setup-breeze      # Breeze TTS worker (clone + venvs/breeze; skip if using edge)
just doctor            # online smoke of every component (LLM/cards/TTS/proxy/alerts)

just gather            # collect → filter → dedup
just pick              # open the printed http://<ip>:8923/?t=… URL, submit picks
just produce           # digest → callb → voice → cards → subs → render-plan
                       # → compose → meta → runs/<date>/out/final.mp4
```

Gate-2 editing: after `pick`, run `just digest` for `50_review.md`, edit via `just edit`, then `just edit-import`, then `just produce` (Call A is skipped when `50_issue.json` exists). Or ignore both gates — timers auto-release.

- Other date bucket: `just DATE=2026-09-20 gather`
- Resume after a break: `just resume` (validates `00_meta.json`, re-runs gaps)
- Force a stage: `just from <stage>`
- Progress: `just status` / `just watch` / `just tail [stage]`
- Sandbox runs: `just exp <name> <stage> [args]` → `runs/_exp-<name>`
- Offline regression: `just test`

## Layout

```
stages/            12 stage scripts (uv project; uv run stages/xx.py)
stages/lib/        shared libs: http/store/pool/embed/simhash/prompts/
                   shotlib/prog/meta/normalize/ttsnorm/*_collect …
                   + fixtures/ selftest samples + seeds/ (x_nitter pool seeds)
contracts/         artifact pydantic models + emitted JSON schemas
contracts/fixtures/2026-09-20/   golden run fixture (validate + compose selftest)
adapters/          LLM gateway, edge-tts, ntfy/deadman alerts, X paid adapter
assets/fonts/      SmileySans-Oblique.ttf (chrome overlay cards)
tools/watch.py     status / watch dashboards
ops/               prelude.sh (_jlock) + winnow-* systemd units + install.sh
docs/              all project docs — index in docs/README.md
sources.yaml       ~160 sources (method/tier/proxy/SLA; just lint-sources)
config.yaml        local config (untracked; template config.example.yaml)
secrets.env        local secrets (untracked; template secrets.env.example)
state/             state.sqlite (pool + dedup + source_state + kv) + backups/
runs/<date>/       full per-episode artifacts (NN_*.json*) + logs/
upstream/          vendored juya-news-card renderer (docs/vendored-upstream.md)
composer/          Remotion composer — alternative engine (docs/composer.md)
```

## Docs map

All documentation lives under `docs/` (index: `docs/README.md`).

| File | Contents |
| --- | --- |
| `docs/PLAN.md` | **single source of truth**: decisions D1–D12, per-stage design, acceptance criteria |
| `rulebook.md` | filter/digest rulebook — stays at root: `stages/` reads it as a runtime input |
| `docs/CONTRIBUTING.md` | engineering conventions: uv project layout, just driver, test matrix |
| `docs/ops.md` | systemd user timers + prelude.sh/_jlock shared prelude |
| `docs/vendored-upstream.md` | juya-news-card pinned SHA, local patches, re-sync procedure |
| `docs/composer.md` | Remotion composer usage + feasibility notes |

## Research background

This repo began as a teardown & full re-implementation of 橘鸦Juya's daily 《AI早报》 production line (BV1NqeY6dEPP, 2026-09-20 episode); the current repo is the productionized form. The research archive behind it — teardown evidence (`evidence/`), the static replica pipeline (`repro/`), and the selection/feasibility labs (`experiments/`) — is kept private and _not_ shipped: `docs/PLAN.md` cites `experiments/…` paths as provenance markers, so those references will not resolve in a public clone.

- `upstream/juya-news-card` — the author's open-sourced card renderer (MIT fork `Mappedinfo/juya-news-card`; original imjuya repo deleted). Next.js+React+TS, 174 templates; driven by `scripts/render-batch.ts`.

## License & third-party notices

MIT — see `LICENSE`. Notable boundaries:

- `upstream/juya-news-card` is vendored under its own MIT license.
- **Breeze TTS** (`tools/tts_workers/breeze-tts` + HF `BreezeBlue/Breeze-TTS-2` weights) is a _non-commercial research_ model — weights are never vendored, `just setup-breeze` pulls them to your HF cache. If your use is commercial, keep `tts.engine: edge` (the online fallback engine) in `config.yaml`.
- The LLM stage talks to an OpenAI-compatible endpoint (`base_url`/`api_key` in `secrets.env`); nothing is hardcoded to a specific provider.
- `sources.yaml` content feeds belong to their publishers; this repo only fetches public RSS/API/HTML for personal pipeline use.
