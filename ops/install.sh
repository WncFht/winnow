#!/usr/bin/env bash
# ops/install.sh — install the winnow systemd *user* timers (PLAN §9)
#
#   bash ops/install.sh
#
# Renders the *.service templates (__REPO__ / __HOME__ -> this machine's real
# paths) and copies everything to ~/.config/systemd/user/, reloads the user
# manager, and enable --now's the three timers:
#   winnow-collect.timer  06:30 -> just gather
#   winnow-gate1.timer    08:30 -> just deadline1
#   winnow-gate2.timer    09:30 -> just deadline2
#
# Run as the pipeline user (never root — these are --user units). Requires
# lingering for the timers to fire while logged out:
#   sudo loginctl enable-linger "$USER"
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(
  winnow-collect.service winnow-collect.timer
  winnow-gate1.service   winnow-gate1.timer
  winnow-gate2.service   winnow-gate2.timer
)

# escape sed replacement specials (& and the | delimiter) in injected paths
_re() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }

mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
  case "$u" in
    *.service)
      # service files ship as templates — inject this machine's repo + $HOME
      sed -e "s|__REPO__|$(_re "$REPO")|g" -e "s|__HOME__|$(_re "$HOME")|g" \
        "$REPO/ops/$u" > "$UNIT_DIR/$u"
      chmod 0644 "$UNIT_DIR/$u"
      ;;
    *)
      install -m 0644 "$REPO/ops/$u" "$UNIT_DIR/$u"
      ;;
  esac
  echo "installed $UNIT_DIR/$u"
done

systemctl --user daemon-reload
systemctl --user enable --now \
  winnow-collect.timer winnow-gate1.timer winnow-gate2.timer

echo
systemctl --user list-timers 'winnow-*' --no-pager || true
echo
echo "done. Inspect runs with:"
echo "  journalctl --user -u winnow-collect.service -u winnow-gate1.service -u winnow-gate2.service"
