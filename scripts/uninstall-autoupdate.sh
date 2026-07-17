#!/usr/bin/env bash
# Remove the launchd auto-update agent installed by install-autoupdate.sh.
set -euo pipefail
LABEL="com.ignacy.co-maintainer.autoupdate"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Auto-update agent removed."
