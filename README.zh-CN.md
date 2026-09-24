# ai-news-pipeline

每日《AI 早报》生产线 —— 从 161 个信息源到成片 mp4 + 标题/封面/QA。
2 段自动块夹 2 个人工闸（各带死线自动放行），全部经 justfile 驱动。
设计/实施唯一真源是 `PLAN.md`，本文件只做门面。

## Pipeline 概览

```
collect      sources.yaml 161 源 → 原始抓取
filter       L0 规则 + LLM verdict/summary（state/items.sqlite 条目池跨期缓存）
dedup        同日聚类 + 跨天级联去重（state/history.sqlite）
── gate 1 人工选稿 ──────────────────────────────────────────────
  just pick 起 review_server（0.0.0.0:8923，一次性 token URL ?t=…，手机可开）
  死线 08:30 未提交 → pick-auto 按 news_value 取 top-K
digest       Call A：选稿 → 50_issue.json + 50_review.md
── gate 2 人工编辑 ──────────────────────────────────────────────
  just edit 改 50_review.md → just edit-import 回灌再校验
  死线 09:30 自动 import 未提交的编辑（sha 漂移探测）
callb        Call B：投影 voice/cards/video.shot_sentences + 合规 pass
voice        TTS 逐句合成 → 音频 + 时间轴
cards        upstream/juya-news-card 模板渲染 + 来源网页截图叠加卡
subs         字幕 pill PNG（ffmpeg 合成路径用）
render-plan  70_render_plan.json（绝对时间轴，ffmpeg/Remotion 共用）
compose      ffmpeg → out/final.mp4（composer/ Remotion 为备选引擎）
meta         标题/封面/QA → 90_qa.json
```

驱动是 justfile（~40 配方）：`gather` `pick` `produce` `status` `watch`
`tail` `from` `resume` `exp` `doctor` `test` 等。配方不跨人工闸串链；
`runs/<date>/.just.lock` 串行化同一日期桶的 just 调用。
`ops/*.timer`（systemd）：06:30 collect、08:30 gate-1 死线、09:30 gate-2
死线 —— 无人值守时全自动放行出片。

每期状态：`state/{history,items}.sqlite`（跨期）+ `runs/YYYY-MM-DD/`
（当日全量 artifact + logs/ + 00_meta.json 阶段簿记）。

## Quickstart

依赖：python ≥3.11、uv、node/npm、just、flock、ffmpeg（libx264）。
chromium、node_modules、lychee 由 setup-toolchain 补齐。

```bash
cp secrets.env.example secrets.env    # 填 SWE2MAX_API_KEY（本地 LLM 网关）
cp config.example.yaml config.yaml    # 按需改 alerts/proxy/schedule

just setup-toolchain   # 一次性：工具检查 + 目录 + npm install + playwright
just fetch-embed       # 拉 Qwen3-Embedding-0.6B-ONNX 到 ~/.cache/embed（dedup 必需，embed.py 不自动下载）
just setup-breeze      # Breeze TTS worker（clone + venvs/breeze；本机生产默认引擎，留 edge 可跳过）
just doctor            # 联网冒烟全组件（LLM/卡片/TTS/proxy/告警）

just gather            # collect → filter → dedup
just pick              # 起选稿 UI；用终端打印的 http://<ip>:8923/?t=… 打开
                       # （无 token 一律 403），勾选提交即写 40_selected.json
just produce           # digest → callb → voice → cards → subs → render-plan
                       # → compose → meta → runs/<date>/out/final.mp4
```

要走 gate-2 编辑：pick 之后先 `just digest` 出 50_review.md，`just edit`
改完 `just edit-import`，再 `just produce`（50_issue.json 已存在时自动
跳过 Call A，编辑不丢）。完全不管也行：死线到点 timer 自动放行。

- 跑别的日期桶：`just DATE=2026-09-20 gather`
- 断了续跑：`just resume`（按 00_meta.json 校验补跑缺失阶段）
- 强制重跑某段：`just from <stage>`（跑到所属 block 末尾）
- 盯进度：`just status` / `just watch` / `just tail [stage]`
- 沙箱实验：`just exp <name> <stage> [args]`（runs/_exp-\<name\>）
- 离线回归：`just test`

## 目录结构

```
stages/            12 个 PEP 723 自含阶段脚本（uv run stages/xx.py --run-dir …）
stages/lib/        公共库：http / store / pool / embed / simhash / prompts /
                   shotlib / prog / meta / normalize / ttsnorm / *_collect …
contracts/         artifact pydantic 模型 + JSON schemas
adapters/          LLM 网关、edge-tts、ntfy/deadman 告警、X 付费适配
tools/watch.py     status / watch 仪表盘
ops/               prelude.sh（_jlock）+ systemd service/timer + install.sh
sources.yaml       161 源（method/tier/proxy/SLA；just lint-sources 校验）
config.yaml        本机配置（不入库；模板 config.example.yaml）
secrets.env        本机密钥（不入库；模板 secrets.env.example）
state/             history.sqlite + items.sqlite + backups/ + 运行时小状态
runs/<date>/       每期全量 artifact（NN_*.json*）+ logs/
upstream/          vendored juya-news-card 卡片渲染器（见 VENDORED.md）
composer/          Remotion 合成器（备选引擎，见 NOTES.md）
experiments/       调研/选型实验目录（PLAN 的证据层）
repro/ evidence/   复刻调研产物（见「调研背景」）
```

## 文档地图

| 文件 | 内容 |
|---|---|
| `PLAN.md` | **设计/实施唯一真源**：D1–D12 已拍板决策、各阶段详设、验收口径 |
| `rulebook.md` | filter/digest 筛选规则手册（由每日人工反馈蒸馏，版本化维护） |
| `repro/README.md` | 复刻 pipeline：各环节 ↔ 原版环节对应表 + 复跑流程 |
| `upstream/VENDORED.md` | juya-news-card 定格 SHA、本地 patch 清单、重新同步上游方法 |
| `composer/NOTES.md` | Remotion 合成器用法（plan 输入优先级、字幕 live-text 约定）+ 可行性实验记录 |
| `adapters/sami-tts.md` | 剪映 SAMI TTS 逆向接口说明（备选 TTS 通道） |

## 调研背景

本仓库起点是对 UP 主 橘鸦 Juya《AI 早报》日更视频生产线（BV1NqeY6dEPP，
2026-09-20 期）的调研与完整复刻；现仓库即其产线化形态，调研产物留存：

- `evidence/` —— 拆解证据：`original/` 正片 video.mp4 + 字幕 srt + 抽帧；
  `makingof/` UP 主工作流揭秘视频 BV1JmdhYqEoy + 转写；`opensource/`
  工具开源介绍 BV199AUzHE8q；`web/` daily.juya.uk RSS / 文字版日报页 /
  GitHub Pages 存档。
- `upstream/juya-news-card` —— UP 主开源卡片渲染器真身（MIT fork
  `Mappedinfo/juya-news-card`；原 imjuya 仓库已删号）。Next.js+React+TS，
  174 套模板；现由 cards 阶段经 `scripts/render-batch.ts` 批渲染调用。
- `repro/` —— 单期静态复刻 pipeline（fetch_shots → render_chrome →
  composite_frames → tts → compose → out.mp4 268.5s），各环节与原版
  对应关系及复跑流程见 `repro/README.md`；`repro/v1/` 是被上游取代的
  初版自造卡片模板。
- `experiments/` —— PLAN 成形前的选型/可行性实验（artifact-contracts、
  dedup-llm、remotion-feas、factcheck-layer 等）。

环节归属（复刻口径）：内容卡片为纯上游实现（`generateTemplateHtml` +
`claudeStyle` 模板逐像素渲染）；导航/面包屑叠加层、截图弹卡、字幕 pill、
TTS 逐句对轨、ffmpeg 合成器为自造（原版必有对应物但未开源）；复刻期
内容数据手写结构化（取自 UP 主当日 RSS），产线化后由 collect/filter/
digest 的 LLM 环节取代。
