#!/usr/bin/env bash
# Build a double-clickable, menu-bar-only CoMaintainer.app (LSUIElement, no Dock icon).
# Usage: ./scripts/build-app.sh        then: open CoMaintainer.app
#        (drag into /Applications and add to Login Items to keep it around)
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="CoMaintainer"
echo "Building release…"
swift build -c release

BIN=".build/release/$APP_NAME"
APP="$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/$APP_NAME"

# Bundle the shared core/ assets (GraphQL queries, tool catalog, filter
# constants, review prompt fragments) so CoreAssets resolves them via
# Bundle.main.resourceURL/core inside the packaged .app.
cp -R core "$APP/Contents/Resources/core"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Co-Maintainer</string>
  <key>CFBundleDisplayName</key><string>Co-Maintainer</string>
  <key>CFBundleIdentifier</key><string>com.ignacy.co-maintainer</string>
  <key>CFBundleExecutable</key><string>CoMaintainer</string>
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
