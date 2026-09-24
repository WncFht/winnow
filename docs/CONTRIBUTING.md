# Contributing

`PLAN.md` is the single source of truth for design; `README.md` is the front door. Keep both in sync when you change behavior.

## Project shape

- **Stages are PEP 723 single-file scripts.** `stages/*.py` declare their deps in the `# /// script` header and run via `uv run stages/<stage>.py --run-dir runs/<date>` — no project-wide venv. `stages/lib/*.py` are shared modules imported after each stage's `sys.path` wiring; most have a `__main__` self-check (`--selftest`, or `--offline` to skip live-network asserts).
- **justfile drives everything.** Pipeline blocks, human gates, sandbox (`just exp`), state maintenance — all are `just` recipes. Recipes never chain across the two human gates; each stage fail-fasts when its input artifacts are missing and tells you which target to run first.
- **run_dir convention.** Every stage takes `--run-dir runs/YYYY-MM-DD` and reads/writes numbered contract artifacts (`10_*` … `90_*` plus the `00_meta.json` ledger; `ls` is the DAG). `runs/_exp-*`, `runs/_test`, `runs/_doctor` are non-contract scratch dirs.
- **Contracts.** Artifact shapes are pydantic models in `contracts/models.py`; `contracts/validate.py` holds cross-field checks. When you change a model, re-emit the JSON schemas and commit `contracts/schemas/` together with the change:

  ```bash
  uv run --with pydantic contracts/models.py
  ```

- **No secrets in code.** Keys/URLs/vendor specifics live in `config.yaml` + `secrets.env` (both gitignored); mirror any new knob in `config.example.yaml` / `secrets.env.example`.

## Before you commit

```bash
just test      # offline regression: all --selftest/--offline checks in parallel + compileall
just doctor    # optional: online smoke of every component (LLM/cards/TTS/proxy/alerts)
```
