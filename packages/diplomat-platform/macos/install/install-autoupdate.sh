#!/usr/bin/env bash
# Install a launchd agent that self-updates Diplomat daily at 06:00 — the macOS
# analogue of the Linux systemd user timer. It launches the app binary in headless
# self-update mode (DIPLOMAT_SELF_UPDATE=1): merge upstream if behind, rebuild
# the bundle, and relaunch only if the app is running. Re-runnable.
#
# Arg 1 (optional): the Diplomat binary to run. Defaults to the bundle beside this
# script, where build-app.sh writes it and launchd starts it.
set -euo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LABEL="com.ignacy.diplomat.autoupdate"
APP="Diplomat.app"

BIN="${1:-$PKG_DIR/$APP/Contents/MacOS/Diplomat}"
if [ ! -x "$BIN" ]; then
  echo "Diplomat binary not found at $BIN — build the app first (install/build-app.sh)." >&2
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
  <dict><key>DIPLOMAT_SELF_UPDATE</key><string>1</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/diplomat-autoupdate.err.log</string>
</dict>
</plist>
PL
echo "Wrote $PLIST"

# Retire the pre-rename (Argent Utils) auto-update agent, if still present.
launchctl bootout "gui/$(id -u)/com.ignacy.argent-utils.autoupdate" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.ignacy.argent-utils.autoupdate.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded auto-update agent — runs daily at 06:00."
