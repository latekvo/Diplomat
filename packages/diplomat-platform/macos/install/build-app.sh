#!/usr/bin/env bash
# Build a double-clickable, menu-bar-only Diplomat.app (LSUIElement, no Dock icon).
# Usage: ./install/build-app.sh     then: open -n Diplomat.app
#        (install/install-autostart.sh to have it back at every login, in place)
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
"$PKG_DIR/install/assemble-app.sh" "$BIN" "$PKG_DIR/../../diplomat-core/assets" \
  "$(cd "$PKG_DIR/../../.." && pwd)" "$PKG_DIR/$APP"

echo "Built $PKG_DIR/$APP"
echo "Launch:  open $PKG_DIR/$APP"
echo "Keep it: ./install/install-autostart.sh starts this bundle at login (and deletes any copy in /Applications or ~/Applications)."
