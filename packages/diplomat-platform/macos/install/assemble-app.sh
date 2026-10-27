#!/usr/bin/env bash
# Lay out Diplomat.app around an already built binary.
# Usage: ./install/assemble-app.sh <binary> <assets-dir> <checkout> <dest.app>
# build-app.sh calls it after the compile; tests/full-disk.sh calls it directly.
#
# The new bundle is assembled beside <dest.app>, on the same volume, and swapped
# into place only once complete. On a failure - a full disk above all - it exits
# non-zero with the old bundle still at <dest.app>: the watchdog and the
# self-updater relaunch exactly <dest.app>, so losing it leaves nothing to run.
set -euo pipefail
[ "$#" -eq 4 ] || { echo "usage: $0 <binary> <assets-dir> <checkout> <dest.app>" >&2; exit 2; }
BIN="$1"; ASSETS="$2"; CHECKOUT="$3"; APP="${4%/}"
APP_NAME="Diplomat"

STAGE="$(mktemp -d "$(dirname "$APP")/.$(basename "$APP").XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$BIN" "$STAGE/Contents/MacOS/$APP_NAME"

# Bundle the shared assets (GraphQL queries, tool catalog, filter constants,
# review prompt fragments) out of the diplomat-core package, so CoreAssets
# resolves them via Bundle.main.resourceURL/assets inside the packaged .app.
cp -R "$ASSETS" "$STAGE/Contents/Resources/assets"

cat > "$STAGE/Contents/Info.plist" <<'PLIST'
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
plutil -insert DiplomatCheckout -string "$CHECKOUT" "$STAGE/Contents/Info.plist"

# One rename: the path holds the old bundle or the complete new one, never neither.
# A running app keeps its executable through it.
if [ ! -e "$APP" ]; then
  mv "$STAGE" "$APP"
  exit 0
fi
# renamex_np(RENAME_SWAP) trades the two in one step, leaving the old bundle at $STAGE
# for the trap. mv has no swap; /usr/bin/python3, which comes with the developer tools
# every `swift build` here needs, can call it.
/usr/bin/python3 -I -c '
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.renamex_np(os.fsencode(sys.argv[1]), os.fsencode(sys.argv[2]), 2) != 0:  # RENAME_SWAP
    sys.exit("assemble-app.sh: swap failed: " + os.strerror(ctypes.get_errno()))
' "$STAGE" "$APP"
