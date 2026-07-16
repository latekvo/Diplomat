#!/usr/bin/env bash
# Remove the XDG autostart entry and stop any running applet instance.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/argent-utils.desktop"

# Tear down the daily auto-update timer too (best-effort).
"${HERE}/uninstall-autoupdate.sh" || true

if [[ -f "$DESKTOP" ]]; then
  rm -f "$DESKTOP"
  echo "Removed autostart entry: ${DESKTOP}"
else
  echo "No autostart entry at ${DESKTOP}"
fi

if pkill -f "python3? -m argent_utils" 2>/dev/null; then
  echo "Stopped running Argent Utils instance(s)."
else
  echo "No running instance to stop."
fi
