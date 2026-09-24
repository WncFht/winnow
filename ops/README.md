# ops — systemd user units + just 配方公共前奏

PLAN.md §9 的落地层：三个 systemd **user** timer 把全自动块挂到墙钟上；
`prelude.sh` 是全部 just 配方共享的前奏（secrets 导出 + run 锁 `_jlock`）。

## 文件

| 文件 | 作用 |
|---|---|
| `install.sh` | 3 个 `.service` 模板 sed 注入本机路径（`__REPO__`/`__HOME__`）后连同 3 个 `.timer` 装到 `~/.config/systemd/user/`，daemon-reload 并 `enable --now` 三个 timer |
| `ai-news-collect.{service,timer}` | 06:30 → `just gather`（collect → filter → dedup，auto block A） |
| `ai-news-gate1.{service,timer}` | 08:30 → `just deadline1`（gate-1 无人选稿则 auto top-K 放行 + digest 出 50_review.md） |
| `ai-news-gate2.{service,timer}` | 09:30 → `just deadline2`（锁定 50_issue.json、必要时自动 edit-import，然后 callb → voice → cards → subs → render-plan → compose → meta） |
| `prelude.sh` | just 配方公共前奏（见下），不独立执行 |

## install.sh

```bash
bash ops/install.sh
```

- 只装 **user** units（不要用 root）；登出后仍要触发需
  `sudo loginctl enable-linger "$USER"`。
- `.service` 是**模板**而非成品：`WorkingDirectory`/`EnvironmentFile`/
  `ExecStart`/`PATH` 里的 `__REPO__`（仓库根，install.sh 按自身位置推得）
  与 `__HOME__`（`$HOME`）占位由 install.sh 在装的时候 sed 成本机真实路径。
  仓库克隆到任意目录都能用；手工拷文件会留下字面占位符，unit 必挂。
- 三个 timer 均 `Persistent=true`——关机错过时点会在开机后补跑；
  deadline 语义天然幂等（过了 deadline 检查即自动放行），补跑安全。
- 排障：`systemctl --user list-timers 'ai-news-*'`；
  `journalctl --user -u ai-news-collect.service -u ai-news-gate1.service -u ai-news-gate2.service`
  （各 service 设了 `SyslogIdentifier=ai-news-{collect,gate1,gate2}`）。

## unit 约定（EnvironmentFile / PATH / TZ）

- 模板占位：`__REPO__` = 仓库根（install.sh 取 `ops/../` 实路径），
  `__HOME__` = 安装用户的 `$HOME`；两个都在 install 时被 sed 替换，
  装出来的 unit 里只剩绝对路径。
- `EnvironmentFile=-__REPO__/secrets.env`：`-` 前缀容许缺失；SWE2MAX_API_KEY
  等密钥不进 unit 文本、secrets.env 本身 gitignored。
- `Environment=PATH=__HOME__/.local/bin:__HOME__/.cargo/bin:...`：user manager
  的默认 PATH 不含 uv（.local/bin）与 just（.cargo/bin），必须显式补。
- `Environment=TZ=Asia/Shanghai`：run 目录按上海日期分桶（`runs/<date>`），
  unit 与 justfile `DATE` 口径一致。
- `Type=oneshot` + `TimeoutStartSec`：collect/gate1 给 2h，gate2 给 4h
  （链上含 TTS + 卡片渲染 + 视频合成）。2h 是宽松上限而非 arbitrary cap——
  unit 注释原话 "gather can run long (161 sources + LLM batches)"。
- `ExecStart` 用绝对路径 `__HOME__/.cargo/bin/just`；`WorkingDirectory` 钉在
  仓库根，just 配方里的相对路径（ops/prelude.sh、secrets.env、runs/）才找得到。

## prelude.sh — just 配方公共前奏

用法（单行配方内联；justfile 里封装成 `SH`/`STAGE` 两个变量）：

```bash
export PIPELINE_RUN=runs/<date>; source ops/prelude.sh; _jlock uv run stages/x.py ...
```

提供：

- `set -o pipefail`；`set -a; . ./secrets.env; set +a`——secrets.env 全量
  导出进环境（文件缺失则跳过）。`PIPELINE_RUN` 未设时跳过 run-dir 逻辑，
  非 run 配方（status / watch / judge-eval）也能只复用 secrets 导出。
- `export EMBED_THREADS="${EMBED_THREADS:-12}"`：rep query embed 走 ONNX
  CPU，默认 4 线程跑不满 12 核机；已被外部环境占用时尊重原值。
- `mkdir -p $PIPELINE_RUN/logs`（各阶段 tee 落盘前提）。
- `_jlock <cmd...>` 两段式等锁，锁文件 `$PIPELINE_RUN/.just.lock`：
  1. `flock -n` 试探；已被占则经 `lslocks` + `/proc/<pid>/cmdline` 反查
     持锁者，stderr 打印「等锁： stage 名 + pid + 已持时长」——以前排队
     完全静默，排查 "dedup 没日志" 只能靠 ps 反推；
  2. `flock -E 200 -w 3600` 排队执行；等满 1h 未拿到返回 200 并提示
     （残留锁文件无害，flock 绑打开 inode，随持锁进程释放）。
  探测分支全部 `|| true`：grep/ps 无匹配返回非零，会把 `set -e` 下排队
  中的配方（deadline1/exp）直接杀死。
  `.just.lock` 与阶段内部 `.lock`（stages/lib/meta.py run_lock）是**不同
  inode**——同 inode 会让子进程 flock 永远等父进程，必死锁。
