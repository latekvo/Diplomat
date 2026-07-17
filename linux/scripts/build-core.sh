#!/usr/bin/env bash
# Build the co-maintainer-core CLI (the Swift prompt engine the applet shells out to)
# and install it to ~/.local/share/co-maintainer/co-maintainer-core.
#
# co-maintainer-core is a statically-linked, self-contained binary (Swift stdlib + core
# baked in): its only non-glibc deps are libstdc++/libgcc_s, so it runs on any
# glibc Linux without a Swift toolchain present. Building it, however, needs a
# Swift toolchain (https://swift.org/install — swiftly is the easy path).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/co-maintainer"
DEST="$DEST_DIR/co-maintainer-core"

# Local library shims first, when present (unsupported-distro setups — e.g.
# Arch, whose ncurses/libxml2 sonames differ from what the toolchain links —
# keep compat symlinks + LD_LIBRARY_PATH in this env; it sources swiftly too).
# Matters when the applet's Update button runs this from a minimal session env.
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
if [ -f "$HOME/.local/swift-compat/env.sh" ]; then
    . "$HOME/.local/swift-compat/env.sh"
fi
# Pick up a swiftly-managed toolchain if swift isn't already on PATH.
if ! command -v swift >/dev/null 2>&1; then
    if [ -f "$HOME/.local/share/swiftly/env.sh" ]; then
        . "$HOME/.local/share/swiftly/env.sh"
    fi
fi
if ! command -v swift >/dev/null 2>&1; then
    echo "error: no 'swift' toolchain found. Install one from https://swift.org/install" >&2
    echo "       (e.g. swiftly), then re-run this script." >&2
    exit 1
fi

echo "Building co-maintainer-core (static) with $(swift --version 2>/dev/null | head -1)…"
cd "$REPO_ROOT"
swift build --product co-maintainer-core --static-swift-stdlib -c release

BIN="$(swift build --product co-maintainer-core -c release --show-bin-path)/co-maintainer-core"
mkdir -p "$DEST_DIR"
install -m 0755 "$BIN" "$DEST"
echo "Installed: $DEST"
"$DEST" build-prompt <<<'{"kind":"audit"}' >/dev/null && echo "Smoke check: OK"
