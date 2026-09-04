#!/usr/bin/env bash
# Install Diplomat as a per-user LaunchAgent so it autostarts on every login,
# and start it now. Re-runnable (it replaces any previous install).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$HERE/.." && pwd)"
cd "$PKG_DIR"

LABEL="com.ignacy.diplomat"
APP="Diplomat.app"

# Always rebuild the bundle so the install reflects the current source. (A stale
# pre-existing Diplomat.app must NOT be deployed as-is — that silently ships old
# code.) build-app.sh rm -rf's and rebuilds, so this is idempotent.
"$HERE/build-app.sh"

# launchd starts the bundle where build-app.sh writes it, so the login instance is
# the one Settings ▸ UPDATE and the 06:00 self-update rebuild and relaunch - as on
# Linux, whose autostart entry runs the checkout's launcher. A copy would keep
# starting the build it was made from.
BIN="$PKG_DIR/$APP/Contents/MacOS/Diplomat"
# Earlier installs copied the bundle here; a click on that copy would start stale
# code, whose newest-wins singleton then retires the login instance.
rm -rf "/Applications/$APP" "$HOME/Applications/$APP"

# Write the LaunchAgent.
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <!-- ~/Library/Logs, not /tmp: a predictable name in the shared, sticky /tmp can
       be pre-created by another user (breaking logging) and is purged periodically. -->
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/diplomat.err.log</string>
</dict>
</plist>
PL
echo "Wrote $PLIST"

# Kill any running/old instance + old agent, then (re)load. RunAtLoad starts it now.
# Also retire a pre-rename (Argent Utils) install: its agent, process, and bundle.
launchctl bootout "gui/$(id -u)/com.ignacy.argent-utils" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.ignacy.argent-utils.plist"
pkill -x ArgentUtils 2>/dev/null || true
rm -rf "/Applications/ArgentUtils.app" "$HOME/Applications/ArgentUtils.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x Diplomat 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded. Autostarts on login and is running now (look for the wrench in your menu bar)."

# Also schedule the daily 6AM self-update (soft-fail: the manual Update button still
# works without it; only the unattended schedule needs this agent).
if ! "$HERE/install-autoupdate.sh" "$BIN"; then
  echo "warning: daily auto-update agent not installed — update manually from Settings ▸ UPDATE." >&2
fi
