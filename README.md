# ai-news-pipeline

[中文版 README → README.zh-CN.md](README.zh-CN.md)

A daily "AI Morning News" production line — from ~160 sources to a finished
mp4 plus title/cover/QA, with two automated blocks sandwiching two human
gates (each with a deadline auto-release). Everything is driven by `just`.
`PLAN.md` is the single source of truth for design decisions; this file is
the front door.

## Pipeline overview

```
collect      sources.yaml ~160 sources → raw fetch (JSON Feed 1.1 + pipe fields)
filter       L0 rules + LLM verdict/summary (state/items.sqlite pool cache)
dedup        same-day clustering + cross-day cascade (state/history.sqlite)
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

~40 `just` recipes drive it: `gather` `pick` `produce` `status` `watch`
`tail` `from` `resume` `exp` `doctor` `test` … Recipes never chain across
human gates; `runs/<date>/.just.lock` serializes same-day invocations.
`ops/*.timer` (systemd): 06:30 collect, 08:30 gate-1 deadline, 09:30 gate-2
deadline — fully unattended when nobody shows up.

Per-episode state: `state/{history,items}.sqlite` (cross-episode) +
`runs/YYYY-MM-DD/` (full artifacts + logs/ + `00_meta.json` stage ledger).

## Quickstart

Requires: python ≥3.11, uv, node/npm, just, flock, ffmpeg (libx264).
Chromium, node_modules, lychee are installed by `setup-toolchain`.

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

Gate-2 editing: after `pick`, run `just digest` for `50_review.md`, edit via
`just edit`, then `just edit-import`, then `just produce` (Call A is skipped
when `50_issue.json` exists). Or ignore both gates — timers auto-release.

- Other date bucket: `just DATE=2026-09-20 gather`
- Resume after a break: `just resume` (validates `00_meta.json`, re-runs gaps)
- Force a stage: `just from <stage>`
- Progress: `just status` / `just watch` / `just tail [stage]`
- Sandbox runs: `just exp <name> <stage> [args]` → `runs/_exp-<name>`
- Offline regression: `just test`

## Layout

```
stages/            12 PEP-723 self-contained stage scripts (uv run stages/xx.py)
stages/lib/        shared libs: http/store/pool/embed/simhash/prompts/
                   shotlib/prog/meta/normalize/ttsnorm/*_collect …
contracts/         artifact pydantic models + emitted JSON schemas
adapters/          LLM gateway, edge-tts, ntfy/deadman alerts, X paid adapter
tools/watch.py     status / watch dashboards
ops/               prelude.sh (_jlock) + systemd service/timer + install.sh
sources.yaml       ~160 sources (method/tier/proxy/SLA; just lint-sources)
config.yaml        local config (untracked; template config.example.yaml)
secrets.env        local secrets (untracked; template secrets.env.example)
state/             history.sqlite + items.sqlite + backups/ + runtime state
runs/<date>/       full per-episode artifacts (NN_*.json*) + logs/
upstream/          vendored juya-news-card renderer (see VENDORED.md)
composer/          Remotion composer — alternative engine (see NOTES.md)
experiments/       research/selection lab archive (evidence layer for PLAN.md)
repro/ evidence/   original-pipeline teardown artifacts (see below)
```

## Docs map

| File | Contents |
|---|---|
| `PLAN.md` | **single source of truth**: decisions D1–D12, per-stage design, acceptance criteria |
| `rulebook.md` | filter/digest rulebook (distilled from daily human feedback) |
| `repro/README.md` | replica pipeline: stage↔original mapping + re-run flow |
| `upstream/VENDORED.md` | juya-news-card pinned SHA, local patches, re-sync procedure |
| `composer/NOTES.md` | Remotion composer usage + feasibility notes |
| `adapters/sami-tts.md` | SAMI TTS reverse-engineered API (alt TTS channel) |

## Research background

This repo began as a teardown & full re-implementation of 橘鸦Juya's daily
《AI早报》production line (BV1NqeY6dEPP, 2026-09-20 episode); the current
repo is the productionized form. Teardown artifacts kept for provenance:

- `evidence/` — original episode video + srt + frames; workflow-reveal video
  BV1JmdhYqEoy + transcript; tooling intro BV199AUzHE8q; daily.juya.uk RSS /
  text daily / GitHub Pages archives under `web/`.
- `upstream/juya-news-card` — the author's open-sourced card renderer
  (MIT fork `Mappedinfo/juya-news-card`; original imjuya repo deleted).
  Next.js+React+TS, 174 templates; driven by `scripts/render-batch.ts`.
- `repro/` — single-episode static replica pipeline (fetch_shots →
  render_chrome → composite_frames → tts → compose → out.mp4 268.5s).
- `experiments/` — pre-PLAN selection/feasibility labs (artifact-contracts,
  dedup-llm, remotion-feas, factcheck-layer, …); each marked
  adopted / superseded / snapshot.

## License & third-party notices

MIT — see `LICENSE`. Notable boundaries:

- `upstream/juya-news-card` is vendored under its own MIT license.
- **Breeze TTS** (`tools/tts_workers/breeze-tts` + HF `BreezeBlue/Breeze-TTS-2`
  weights) is a *non-commercial research* model — weights are never vendored,
  `just setup-breeze` pulls them to your HF cache. If your use is commercial,
  keep `tts.engine: edge` (the online fallback engine) in `config.yaml`.
- The LLM stage talks to an OpenAI-compatible endpoint (`base_url`/`api_key`
  in `secrets.env`); nothing is hardcoded to a specific provider.
- `sources.yaml` content feeds belong to their publishers; this repo only
  fetches public RSS/API/ HTML for personal pipeline use.
