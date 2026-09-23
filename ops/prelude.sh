# just 配方公共前奏。用法（单行配方内联）：
#   export PIPELINE_RUN=runs/<date>; source ops/prelude.sh; _jlock uv run stages/x.py ...
# 提供：pipefail、logs/ 目录、secrets.env 导出、_jlock() 两段式等锁。
# PIPELINE_RUN 未设时跳过 run-dir 相关逻辑（供非 run 配方复用 secrets 导出）。

set -o pipefail
set -a; [ -f secrets.env ] && . ./secrets.env; set +a

# rep query embed 走 ONNX CPU，默认 4 线程跑不满；12 核机拉满。
export EMBED_THREADS="${EMBED_THREADS:-12}"

# justfile 内联检查 / collect preflight 读 PIPELINE_PROXY；systemd 环境下
# secrets.env 不一定带，给个与 justfile PROXY 一致的兜底。
export PIPELINE_PROXY="${PIPELINE_PROXY:-http://127.0.0.1:7890}"

if [ -n "${PIPELINE_RUN:-}" ]; then
  mkdir -p "$PIPELINE_RUN/logs"
fi

# 两段式等锁：先 -n 试探，占用则打印持锁者（阶段名+pid+已持时长）再 -w 排队。
# 以前排队完全静默，排查 "dedup 没日志" 类问题时只能靠 ps 反推。
_jlock() {
  local L="$PIPELINE_RUN/.just.lock" rc AP H W E
  if ! flock -n "$L" true 2>/dev/null; then
    AP=$(readlink -f "$L")
    # 探测分支全部 || true：grep/ps 无匹配返回非零，set -e 下会把排队
    # 中的配方直接杀死（deadline1/exp 都在 set -euo pipefail 里调 _jlock）
    H=$(lslocks -n -o PID,MODE,PATH 2>/dev/null \
        | awk -v p="$AP" '$3==p && $2=="WRITE" {print $1; exit}' || true)
    if [ -n "$H" ]; then
      W=$(tr '\0' ' ' </proc/"$H"/cmdline 2>/dev/null \
          | grep -oE 'stages/[a-z_]+\.py( [^ ]+)*' | head -1 | cut -c1-60 \
          || true)
      E=$(ps -o etime= -p "$H" 2>/dev/null | xargs || true)
      echo "[just] 等锁: ${W:-pid $H} 已持锁 ${E:-?} — 本阶段排队 ≤1h" >&2
    else
      echo "[just] 等锁: $L 被占用（持锁者不明）— 排队 ≤1h" >&2
    fi
  fi
  flock -E 200 -w 3600 "$L" "$@"; rc=$?
  if [ "$rc" -eq 200 ]; then
    echo "[just] $PIPELINE_RUN 有运行进行中 — .just.lock 等满 1h 未拿到" \
         "（残留锁文件无害，锁随持锁进程释放）" >&2
  fi
  return "$rc"
}
