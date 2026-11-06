#!/usr/bin/env bash
# Remove the XDG autostart and application-menu entries and stop any running
# instance.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/diplomat.desktop"
MENU_ENTRY="${XDG_DATA_HOME:-$HOME/.local/share}/applications/diplomat.desktop"

# Tear down the daily auto-update timer too (best-effort), and the watchdog before
# the app is stopped below, or it would launch it again.
"${HERE}/uninstall-autoupdate.sh" || true
"${HERE}/uninstall-watchdog.sh" || true

if [[ -f "$DESKTOP" ]]; then
  rm -f "$DESKTOP"
  echo "Removed autostart entry: ${DESKTOP}"
else
  echo "No autostart entry at ${DESKTOP}"
fi

if [[ -f "$MENU_ENTRY" ]]; then
  rm -f "$MENU_ENTRY"
  echo "Removed application-menu entry: ${MENU_ENTRY}"
fi

if pkill -f "python3? -m diplomat_app" 2>/dev/null; then
  echo "Stopped running Diplomat instance(s)."
else
  echo "No running instance to stop."
fi
