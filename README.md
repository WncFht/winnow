# ai-news-pipeline

对 橘鸦 Juya《AI 早报》日更视频（BV1NqeY6dEPP，2026-09-20 期）生产线的调研与完整复刻。

## 目录结构

```
ai-news-pipeline/
├── evidence/            # 调研素材：拆解原视频留下的证据
│   ├── original/        #   正片 video.mp4 + 字幕 srt + 抽帧 frames/ + 转场/卡片帧 + 音频段
│   ├── makingof/        #   UP主工作流揭秘视频 BV1JmdhYqEoy + 转写 + 关键帧
│   ├── opensource/      #   工具开源介绍视频 BV199AUzHE8q + 转写 + 网格图
│   └── web/             #   daily.juya.uk RSS、文字版日报页、GitHub Pages 存档页
├── upstream/
│   └── juya-news-card/  # UP主开源卡片渲染器真身（MIT fork Mappedinfo/juya-news-card；
│                        #   原 imjuya 仓库已删号）。Next.js+React+TS，174 套模板，
│                        #   scripts/render-batch.ts 是本次加的批渲染驱动
├── repro/               # 复刻 pipeline（详见 repro/README.md）
│   ├── items.json           # 选题+口播稿+截图时机（内容取自 UP主当日 RSS）
│   ├── items_upstream.json  # 上游 GeneratedContent 契约的结构化卡片数据
│   ├── fetch_shots.py       # 来源网页截图 → shots/
│   ├── render_chrome.py     # 导航/面包屑/截图弹卡 → 透明叠加层 chrome/
│   ├── composite_frames.py  # 上游卡片 + chrome → frames_v2/
│   ├── tts.py               # edge-tts 逐句合成 → audio/ + timeline.json + 字幕 subs/
│   ├── compose.py           # ffmpeg 合成 → out.mp4（≈ UP主说的 "SmartPage"）
│   ├── cards_upstream/      # 上游 claudeStyle 渲染的 14 张内容卡片
│   ├── assets/ shots/ audio/ subs/ chrome/ frames_v2/   # 中间产物
│   ├── out.mp4              # 最终成片 268.5s
│   └── v1/                  # 初版自造卡片模板（render_cards.py + cards/），已被上游取代
└── repro-venv/          # repro 脚本的 python venv（playwright / edge-tts）
```

## 各环节归属

- **纯上游实现**：14 张内容卡片（`generateTemplateHtml` + `claudeStyle` 模板逐像素渲染）
- **自造、原版必有对应物但未开源**：导航/面包屑叠加层、截图弹卡、字幕 pill、
  intro 概览列表、TTS 逐句对轨、ffmpeg 合成器
- **内容数据**：手写结构化（新闻事实来自 UP 主 RSS），上游的 LLM 生成环节未跑

复跑流程见 `repro/README.md`。
