#!/usr/bin/env bash
# Lay out Diplomat.app around an already built binary.
# Usage: ./install/assemble-app.sh <binary> <assets-dir> <checkout> <dest.app>
# build-app.sh calls it after the compile; tests/full-disk.sh calls it directly.
set -euo pipefail
[ "$#" -eq 4 ] || { echo "usage: $0 <binary> <assets-dir> <checkout> <dest.app>" >&2; exit 2; }
BIN="$1"; ASSETS="$2"; CHECKOUT="$3"; APP="$4"
APP_NAME="Diplomat"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/$APP_NAME"

# Bundle the shared assets (GraphQL queries, tool catalog, filter constants,
# review prompt fragments) out of the diplomat-core package, so CoreAssets
# resolves them via Bundle.main.resourceURL/assets inside the packaged .app.
cp -R "$ASSETS" "$APP/Contents/Resources/assets"

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
# A copy of the bundle kept outside the checkout finds it through this record;
# RepoPaths.root reads it back.
plutil -insert DiplomatCheckout -string "$CHECKOUT" "$APP/Contents/Info.plist"
