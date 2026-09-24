# golden run fixture — runs/2026-09-20

完整一天的契约产物样本（10_raw → 80_build_manifest），供离线自测复用：

- `stages/render_plan.py --selftest` T3 golden：重编译本目录并逐字段比对 `70_render_plan.json`；T1/T2/T4 也从 `61_audio/`、`64_frames/`、`65_subs/` 取媒体搭合成 run dir。
- `stages/compose.py --selftest`：复制本目录跑 ffmpeg `--max-t 10` 冒烟。
- `experiments/artifact-contracts/validate.py`：schema + 交叉字段 + manifest sha256 全量复验（`uv run --with pydantic python3 ../validate.py`）。

媒体文件（`61_audio/*.mp3`、`64_frames/*.png`、`65_subs/*.png`、 `63_cards/*.png`）是真物出库进 git——manifest 里的 `sha256` 逐字节钉死，请勿转码/压缩/改名。最初由 `../../build_samples.py` 从 `repro/` 生成（当时是绝对 symlink；现已落为实体文件以便 fresh clone 可用）。

注意 `61_audio/037_kimi_2.mp3` 是 `037_outro_0.mp3` 的别名副本：fixture 时间线把 kimi 尾句重映射成 outro（`037_outro_0`），而 `repro/timeline.json` 仍引用原始名 `037_kimi_2.mp3`——render_plan selftest 用 repro 侧 timeline 搭 run dir 时需要这个名字存在。

重新生成：`python3 ../../build_samples.py`（需要 `repro/` 生成物与 `experiments/issue-contract/issue-2026-09-20.json` 在场）。
