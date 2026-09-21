#!/usr/bin/env bash
# Remotion 渲染入口（PLAN §7.8 主引擎）。
#
# 用法:
#   ./render.sh <run_dir|70_render_plan.json> [out.mp4] [extra remotion flags]
#   REMOTION_PLAN=<plan.json> ./render.sh <同上>          # env 覆盖 $1 的 plan 选择
#
# plan 注入链（src/plan.ts resolvePlan）:
#   --props plan 对象 > REMOTION_PLAN env > --props planUrl > public dir 默认文件
# 本脚本走 env 路：export REMOTION_PLAN=$PLAN（remotion.config.ts 注入 JSON），
# 且 --public-dir 始终指向 plan 所在 run dir，契约相对 src 经 staticFile 命中。
#
# 生产命令形态（PLAN §7.8）:
#   TMPDIR=$PWD/.tmp npx remotion render FullDaily out/final.mp4 --concurrency=4
#   —— TMPDIR 必须真盘（/tmp tmpfs OOM 踩过）；勿加 --hardware-acceleration。
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "usage: $0 <run_dir|70_render_plan.json> [out.mp4] [extra remotion flags]" >&2
  exit 64
}

[ $# -ge 1 ] || usage
IN="$1"
OUT="out/final.mp4"
if [ $# -ge 2 ]; then OUT="$2"; shift 2; else shift 1; fi
# 剩余 "$@" 原样透传给 remotion render（如 --frames=0-90）

if [ -n "${REMOTION_PLAN:-}" ]; then
  # env 显式指定 → 以其为准，run dir = plan 所在目录
  PLAN="$(cd "$(dirname "$REMOTION_PLAN")" && pwd)/$(basename "$REMOTION_PLAN")"
elif [ -d "$IN" ]; then
  PLAN="$(cd "$IN" && pwd)/70_render_plan.json"
else
  PLAN="$(cd "$(dirname "$IN")" && pwd)/$(basename "$IN")"
fi
[ -f "$PLAN" ] || { echo "render.sh: plan not found: $PLAN" >&2; exit 66; }

RUN="$(dirname "$PLAN")"
export REMOTION_PLAN="$PLAN"

mkdir -p out .tmp
# chrome-headless-shell 已在 node_modules/.remotion（npm install 时下载）；
# 若被清掉且自动下载失败，可追加:
#   --browser-executable ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell
exec env TMPDIR="$PWD/.tmp" npx remotion render FullDaily "$OUT" \
  --public-dir "$RUN" --concurrency 4 "$@"
