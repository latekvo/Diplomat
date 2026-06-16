#!/usr/bin/env bash
# Build a double-clickable, menu-bar-only ArgentUtils.app (LSUIElement, no Dock icon).
# Usage: ./scripts/build-app.sh        then: open ArgentUtils.app
#        (drag into /Applications and add to Login Items to keep it around)
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="ArgentUtils"
echo "Building release…"
swift build -c release

BIN=".build/release/$APP_NAME"
APP="$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/$APP_NAME"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Argent Utils</string>
  <key>CFBundleDisplayName</key><string>Argent Utils</string>
  <key>CFBundleIdentifier</key><string>com.ignacy.argent-utils</string>
  <key>CFBundleExecutable</key><string>ArgentUtils</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

echo "Built ./$APP"
echo "Launch:  open ./$APP"
echo "Keep it: drag into /Applications, then System Settings → General → Login Items → add it."
