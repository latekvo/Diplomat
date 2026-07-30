#!/usr/bin/env bash
# Build a double-clickable, menu-bar-only Diplomat.app (LSUIElement, no Dock icon).
# Usage: ./install/build-app.sh     then: open Diplomat.app
#        (drag into /Applications and add to Login Items to keep it around)
#
# Everything is built and written inside this package, never at the repo root:
# the bundle is a build artifact of the macOS front-end and belongs beside it.
set -euo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG_DIR"

APP_NAME="Diplomat"
echo "Building release…"
swift build -c release

BIN="$(swift build -c release --show-bin-path)/$APP_NAME"
APP="$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/$APP_NAME"

# Bundle the shared assets (GraphQL queries, tool catalog, filter constants,
# review prompt fragments) out of the diplomat-core package, so CoreAssets
# resolves them via Bundle.main.resourceURL/assets inside the packaged .app.
cp -R "$PKG_DIR/../../diplomat-core/assets" "$APP/Contents/Resources/assets"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Diplomat</string>
  <key>CFBundleDisplayName</key><string>Diplomat</string>
  <key>CFBundleIdentifier</key><string>com.ignacy.diplomat</string>
  <key>CFBundleExecutable</key><string>Diplomat</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

echo "Built $PKG_DIR/$APP"
echo "Launch:  open $PKG_DIR/$APP"
echo "Keep it: drag into /Applications, then System Settings → General → Login Items → add it."
