#!/usr/bin/env bash
# Remove both LaunchAgents and stop the app. Leaves the .app bundle in place.
set -euo pipefail
LABEL="com.ignacy.diplomat"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Tear down the daily auto-update agent too (best-effort).
"$HERE/uninstall-autoupdate.sh" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -x Diplomat 2>/dev/null || true
echo "Autostart removed and app stopped. (The bundle stays beside this checkout's macOS package; delete it, or the checkout, to fully uninstall.)"
