#!/usr/bin/env bash
# Remove the launchd watchdog agent installed by install-watchdog.sh.
set -euo pipefail
LABEL="com.ignacy.diplomat.watchdog"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Watchdog agent removed."
