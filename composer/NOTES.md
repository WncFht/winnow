# composer — Remotion 合成器（消费 70_render_plan.json）

> 本目录由 experiments 区 remotion 可行性实验整目录提升而来（种子路径见
> PLAN.md §2/§12；下方为原始实验记录）。
> 定位：**手工/冒烟路径**——生产合成不经过本目录；justfile `compose` 配方
> 恒走 `stages/compose.py`（ffmpeg 图谱，PLAN §7.8）出 `out/final.mp4`。
> render.sh/Remotion 保留作手工渲染、冒烟与备选路线验证；
> `smoke/` 是冒烟 fixture（~93 帧小 plan + audio/frames_v2/subs 配套）。

## 用法（手工/冒烟）

```bash
./render.sh <run_dir|70_render_plan.json> [out.mp4] [--frames=A-B ...]
# 例: ./render.sh runs/2026-09-22 runs/2026-09-22/out/final.mp4
#     REMOTION_PLAN=runs/x/70_render_plan.json ./render.sh ignored out.mp4
```

- **plan 输入优先级**（`src/plan.ts::resolvePlan`）：
  `--props '{"plan":{…}}'` 内嵌 > `REMOTION_PLAN` env（remotion.config.ts 经
  DefinePlugin 注入 JSON）> `--props '{"planUrl":"…"}'`（经 `--public-dir` fetch）>
  public dir 根下 `render_plan.json`/`70_render_plan.json` 默认候选。
- render.sh 走 env 路 + `--public-dir=<plan 所在 run dir>`，契约相对 src
  （`64_frames/`、`61_audio/`、`65_subs/`）由 `staticFile` 命中。
- **字幕 live-text 约定**：`overlay_track[].src` 是 PNG pill（契约要求文件存在）；
  同 basename `.txt` sidecar（如 `65_subs/000.txt`）存口播文本——fetch 到即渲染
  live-text pill（bottom:60 向上生长、maxWidth 1600 wrap），否则退回 `<Img>` PNG
  按 `xy` 表达式定位。→ `stages/render_plan.py` 产 plan 时应同时吐 `.txt` sidecar。
- TMPDIR=$PWD/.tmp（/tmp tmpfs OOM 坑）；`--concurrency=4`；**不开** hw-accel。
- chrome-headless-shell 在 `node_modules/.remotion/`；若缺失且自动下载失败：
  `--browser-executable ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell`

# 原始实验记录（remotion 可行性实验）

日期：2026-09-21 · 主机：Arch Linux, 12 cores, 31G RAM, RTX 4070 SUPER 12G · 网络：中国大陆直连

## 实验内容

`src/FullDaily.tsx` 是 `repro/compose.py` 的逐点移植：

| compose.py 语义 | Remotion 对应物 |
|---|---|
| `-loop 1 -t dur -i card.png` 逐段 concat | `<Sequence from dur><Img staticFile>` |
| shot 窗口内换 `<id>_shot.png` | ItemVisual 内三段嵌套 Sequence（card/shot/card） |
| `overlay y=930` 逐句字幕 PNG | 同窗口 `<Sequence>` 内 live-text pill（Alibaba PuHuiTi） |
| `adelay=ms + amix` 逐句 mp3 | 无界 `<Sequence from>` 内 `<Audio>`，自然播完 |
| libx264 crf19 + aac 192k | `--codec h264 --crf --audio-bitrate`（CLI 全部有对应 flag） |

实验期输入为 timeline.json + items.json 原样消费（8055 frames @30fps,
1920×1080, 268.5s）——**现行输入已改**：composer 唯一输入是 render_plan/1
的 `70_render_plan.json`（`src/plan.ts`，timeline/items 由 render_plan.py
编译期消费、不再进 Remotion）；`src/` 下残留的 timeline.json/items.json
是旧 fixture，无任何引用。

## 实测结果

- **安装**：`npm i remotion@4.0.526`（=npm latest，alpha 线 4.1.0-alpha12）17s 装完。
  esbuild postinstall 被本机 allowScripts 拦截 → package.json 加 `"allowScripts"` 解决。
- **浏览器**：首次渲染自动下载 chrome-headless-shell 到 `node_modules/.remotion/`
  —— googleapis 下载在本机直连**成功**（本实验与 remotion-probe 各下一次）。
  失败时退路：`browserExecutable` 指到 `~/.cache/ms-playwright/chromium_headless_shell-*/`。
- **冒烟**（--frames=0-300）：out/smoke.mp4 抽帧验证卡片+字幕 pill 位置正确，aac 音轨在。
- **OOM 坑**：默认并发=min(8,cores/2)=6 个 Chrome tab → 本机（可用内存仅 ~9G，
  与其他 agent 共享）报 "Google Chrome ran out of memory"。`--concurrency=4` 通过。
- **字幕自适应**：首版 `nowrap` pill 长句溢出画面两缘；改 maxWidth+wrap 后又因
  `top:915` 锚点向下长而冲出底缘 → 终版 `bottom:60` 向上生长，4 行长句完整入框。
  （compose.py 的 PNG pill 是离线预排版好的，live text 要自己管 wrap/锚点——
  这是换成"活字幕"时唯一真正多出来的工程点，一次性的。）

## RESULTS（实测，同机同时段）

| 路线 | 命令 | 墙钟 | 输出 |
|---|---|---|---|
| ffmpeg 基线 | `python3 compose.py`（x264 medium crf19+aac192k） | **203s** | 11.2MB, 268.500s |
| Remotion | `remotion render --concurrency=4`（sw x264） | **282s** | 18.9MB, 268.544s |
| Remotion | `--concurrency=4 --hardware-acceleration=if-possible` | **OOM @98s**（见下） | — |
| Remotion | `--concurrency=2 --hardware-acceleration=if-possible` | **282s** | 23.7MB, 268.544s |

- Remotion 只慢 ~1.4×（203→282s），不是数量级差距：8085 帧 1080p 的
  x264/nvenc 编码本身就是大头，Chrome 截图即便 2 tab 也没拖住编码器。
  两者都 ≈1× 实时、都是编码受限。
- **NVENC 实测可用**：`--hardware-acceleration=if-possible` 让 Remotion
  自带的 `@remotion/compositor-linux-x64-gnu/ffmpeg` 走 `h264_nvenc`
  （nvidia-smi 里看到 6 路 1080p H.264 session，40-48fps/路）。
  但总时长没变 → 瓶颈在 Chrome 逐帧截图侧，不在编码侧；文件大 25%。
- **OOM 根因不是并发本身**：/tmp 是 16G tmpfs、被其他任务吃到 95%（剩 ~900M），
  Chrome 报 "ran out of memory or disk space" 实为 tmpfs 写爆。
  conc=4+nvenc 在 98s 死、conc=2+nvenc 撑过（其 temp 目录只到 ~80M）。
  生产环境建议 `TMPDIR` 指到真盘（/ 还剩 248G）而不是默认 tmpfs。
- 验证：t=30s 抽帧卡片+4 行 wrap 字幕完整入框；t=200s 帧正常；
  音轨 mean -29dB / max -11dB 有声正常。

## 与 juya-news-card 共用组件（上游证据：experiments/remotion-probe）

- 模板是 `.tsx` React 组件：`claudeStyleTemplate.render(data, scale)` 直接返回
  `<ClaudeStyle>` 元素 —— probe 的 `src/Upstream.tsx` 跨目录 import 后渲出
  `out/upstream-live.mp4`（抽帧验证：真 claudeStyle 卡、暖米色主题、图标格子）。
- 注意点：模板 `useLayoutEffect` 里做 DOM 量测+1.5s settle 字体适配，
  Remotion 里要用 `delayRender/continueRender` 等它稳，或接受未完全 fit 的版面
  （probe 帧里卡片偏上、内容略超 1080——需要外层 scale/尺寸约定）。
- 模板 import 仅依赖本地 utils（layout-calculator/template/text-spacing），
  claudeStyle 无 MUI/emotion 依赖 —— 耦合面小。
- 三档集成深度：A) PNG 级（FullDaily 现状，零耦合）；B) 组件级（probe 已证可 mount，
  能上 spring 动画、省掉 PNG 中间产物）；C) HTML 字符串级（generateTemplateHtml →
  dangerouslySetInnerHTML，可行但不如 B 干净）。

## 结论要点

可行且已实测通过。速度代价仅 ~1.4×（282s vs 203s，本机内存被其他任务占用
的背景下、并发压到 4 测得；机器空闲时差距可能更小），远低于预期。
选 Remotion 的价值不在速度，在：字幕变 live text（逐词高亮/样式即改即得，
可直接吃 @remotion/captions）、卡片可动画化（spring/transition）、
上游模板组件可直挂（probe 已证）、时间轴即代码（items/segs JSON → Sequence
映射，去掉 filter_complex 字符串拼接）。ffmpeg 路线仍是**最快兜底**与
零依赖备份；两者可同时保留（compose.py 照跑，Remotion comp 平替/升级版）。

风险清单：① /tmp tmpfs 余量 <1G 时 Chrome 渲染会中途死 → 生产设 TMPDIR 到真盘 +
`--concurrency=2~4` 作稳妥基线（默认并发 6 在本机必死）；
② 上游模板直挂时字体自适应要 delayRender 等 settle，版面尺寸需约定；
③ License 对个人免费（公司主体到一定规模要付费）；
④ 维护面：多一个 node/React 工具链（但 juya-news-card 本来就是 Next.js，
依赖栈反而收敛：卡片服务与视频合成同为 React）。
