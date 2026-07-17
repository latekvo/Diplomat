#!/usr/bin/env bash
# Remove the autostart LaunchAgent and stop the app. Leaves the .app bundle in place.
set -euo pipefail
LABEL="com.ignacy.co-maintainer"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Tear down the daily auto-update agent too (best-effort).
"$HERE/uninstall-autoupdate.sh" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -x CoMaintainer 2>/dev/null || true
echo "Autostart removed and app stopped. (Delete CoMaintainer.app from /Applications - or ~/Applications if the install fell back there - to fully uninstall.)"
