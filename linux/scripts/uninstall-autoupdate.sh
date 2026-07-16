#!/usr/bin/env bash
# Remove the systemd user timer installed by install-autoupdate.sh.
set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${UNIT_DIR}/argent-utils-update.service"
TIMER="${UNIT_DIR}/argent-utils-update.timer"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now argent-utils-update.timer 2>/dev/null || true
fi

removed=0
for f in "$TIMER" "$SERVICE"; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        echo "Removed ${f}"
        removed=1
    fi
done
[[ "$removed" == 1 ]] || echo "No auto-update timer installed."

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
fi
