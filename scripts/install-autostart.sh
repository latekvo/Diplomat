#!/usr/bin/env bash
# Install ArgentUtils as a per-user LaunchAgent so it autostarts on every login,
# and start it now. Re-runnable (it replaces any previous install).
set -euo pipefail
cd "$(dirname "$0")/.."

LABEL="com.ignacy.argent-utils"
APP="ArgentUtils.app"

# Always rebuild the bundle so the install reflects the current source. (A stale
# pre-existing ArgentUtils.app must NOT be deployed as-is — that silently ships old
# code.) build-app.sh rm -rf's and rebuilds, so this is idempotent.
./scripts/build-app.sh

# Install to /Applications (fall back to ~/Applications if not writable).
if [ -w /Applications ]; then
  DEST_DIR="/Applications"
else
  DEST_DIR="$HOME/Applications"; mkdir -p "$DEST_DIR"
fi
rm -rf "$DEST_DIR/$APP"
cp -R "$APP" "$DEST_DIR/"
BIN="$DEST_DIR/$APP/Contents/MacOS/ArgentUtils"
echo "Installed app → $DEST_DIR/$APP"

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
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/argent-utils.err.log</string>
</dict>
</plist>
PL
echo "Wrote $PLIST"

# Kill any running/old instance + old agent, then (re)load. RunAtLoad starts it now.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x ArgentUtils 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded. Autostarts on login and is running now (look for the wrench in your menu bar)."

# Also schedule the daily 6AM self-update (soft-fail: the manual Update button still
# works without it; only the unattended schedule needs this agent).
if ! ./scripts/install-autoupdate.sh "$BIN"; then
  echo "warning: daily auto-update agent not installed — update manually from Settings ▸ UPDATE." >&2
fi
