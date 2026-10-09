#!/usr/bin/env bash
# Install a launchd agent that checks every 5 minutes that Diplomat is up, and
# launches it if it died (a crash, a force-quit, a kill, a failed update). It runs
# the app binary in its headless watchdog mode (DIPLOMAT_WATCHDOG=1), which
# launches nothing while an instance runs or after the operator quit the app.
# Not KeepAlive on the app itself: the newest-wins singleton and the updater's
# relaunch both end instances on purpose, and launchd would start those again.
# Re-runnable.
#
# Arg 1 (optional): the Diplomat binary to run. Defaults to the bundle beside this
# script, where build-app.sh writes it and launchd starts it.
set -euo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.ignacy.diplomat.watchdog"
APP="Diplomat.app"

BIN="${1:-$PKG_DIR/$APP/Contents/MacOS/Diplomat}"
if [ ! -x "$BIN" ]; then
  echo "Diplomat binary not found at $BIN - build the app first (install/build-app.sh)." >&2
  exit 1
fi

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
  <key>EnvironmentVariables</key>
  <dict><key>DIPLOMAT_WATCHDOG</key><string>1</string></dict>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/diplomat-watchdog.err.log</string>
</dict>
</plist>
PL
echo "Wrote $PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded watchdog agent — checks every 5 minutes that the app is up."
