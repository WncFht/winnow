#!/usr/bin/env bash
# ops/install.sh — install the ai-news systemd *user* timers (PLAN §9)
#
#   bash ops/install.sh
#
# Copies the unit files to ~/.config/systemd/user/, reloads the user manager,
# and enable --now's the three timers:
#   ai-news-collect.timer  06:30 -> just gather
#   ai-news-gate1.timer    08:30 -> just deadline1
#   ai-news-gate2.timer    09:30 -> just deadline2
#
# Run as the pipeline user (never root — these are --user units). Requires
# lingering for the timers to fire while logged out:
#   sudo loginctl enable-linger "$USER"
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(
  ai-news-collect.service ai-news-collect.timer
  ai-news-gate1.service   ai-news-gate1.timer
  ai-news-gate2.service   ai-news-gate2.timer
)

mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
  install -m 0644 "$REPO/ops/$u" "$UNIT_DIR/$u"
  echo "installed $UNIT_DIR/$u"
done

systemctl --user daemon-reload
systemctl --user enable --now \
  ai-news-collect.timer ai-news-gate1.timer ai-news-gate2.timer

echo
systemctl --user list-timers 'ai-news-*' --no-pager || true
echo
echo "done. Inspect runs with:"
echo "  journalctl --user -u ai-news-collect.service -u ai-news-gate1.service -u ai-news-gate2.service"
