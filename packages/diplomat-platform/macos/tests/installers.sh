#!/usr/bin/env bash
# What install-autostart.sh and install-autoupdate.sh write. Runs a copy of install/
# laid out as a scratch checkout, with HOME in it too: `swift` is a stub, so
# build-app.sh lays the bundle out without a build; launchctl and pkill are stubs
# that log their arguments; and `rm` refuses anything outside the scratch dir, which
# keeps the run off this box's /Applications and is the failure the installer's
# "delete it by hand" arm is for. The Linux twin is linux/tests/test_install_autoupdate.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRATCH="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/diplomat-installers.XXXXXX")" && pwd -P)"
trap 'rm -rf "$SCRATCH"' EXIT
export SCRATCH

PKG="$SCRATCH/packages/diplomat-platform/macos"
BIN="$PKG/Diplomat.app/Contents/MacOS/Diplomat"
AGENTS="$SCRATCH/home/Library/LaunchAgents"
mkdir -p "$PKG" "$SCRATCH/packages/diplomat-core/assets" "$SCRATCH/home" "$SCRATCH/bin" "$SCRATCH/build"
cp -R "$HERE/../install" "$PKG/install"
printf '#!/bin/sh\n' > "$SCRATCH/build/Diplomat"

cat > "$SCRATCH/bin/launchctl" <<'EOF'
#!/bin/sh
echo "launchctl $*" >> "$SCRATCH/calls"
EOF
cat > "$SCRATCH/bin/pkill" <<'EOF'
#!/bin/sh
echo "pkill $*" >> "$SCRATCH/calls"
EOF
cat > "$SCRATCH/bin/swift" <<'EOF'
#!/bin/sh
case "$*" in *--show-bin-path*) echo "$SCRATCH/build" ;; esac
EOF
cat > "$SCRATCH/bin/rm" <<'EOF'
#!/bin/sh
for a; do
  case "$a" in -*) continue ;; /*) p="$a" ;; *) p="$PWD/$a" ;; esac
  case "$p" in "$SCRATCH"/*) ;; *) echo "rm: $a: refused, outside the scratch dir" >&2; exit 1 ;; esac
done
exec /bin/rm "$@"
EOF
chmod +x "$SCRATCH"/bin/* "$SCRATCH/build/Diplomat"

run() { PATH="$SCRATCH/bin:$PATH" HOME="$SCRATCH/home" bash "$PKG/install/$1" > "$SCRATCH/out" 2>&1; }
check() {   # $1 = what is proven, the rest = the command that proves it
  local name="$1"; shift
  if "$@"; then echo "  ok    $name"; return; fi
  echo "  FAIL  $name" >&2
  cat "$SCRATCH/out" "$SCRATCH/calls" >&2
  exit 1
}

echo "installers: what a login install writes"
check "install-autostart.sh runs to the end past a copy it cannot remove" run install-autostart.sh
check "…and says so" grep -q "delete it by hand" "$SCRATCH/out"
check "build-app.sh writes the bundle the agents name" test -x "$BIN"
check "the login agent starts the bundle beside the package" \
  grep -qF "<string>$BIN</string>" "$AGENTS/com.ignacy.diplomat.plist"
check "the auto-update agent runs that same bundle" \
  grep -qF "<string>$BIN</string>" "$AGENTS/com.ignacy.diplomat.autoupdate.plist"
check "…in self-update mode" \
  grep -qF "<key>DIPLOMAT_SELF_UPDATE</key><string>1</string>" "$AGENTS/com.ignacy.diplomat.autoupdate.plist"
for label in com.ignacy.diplomat com.ignacy.diplomat.autoupdate; do
  check "$label is loaded once written" \
    grep -qF "launchctl bootstrap gui/$(id -u) $AGENTS/$label.plist" "$SCRATCH/calls"
done

echo "installers: the auto-update installer on its own"
/bin/rm -f "$AGENTS/com.ignacy.diplomat.autoupdate.plist"
check "install-autoupdate.sh runs with no argument" run install-autoupdate.sh
check "…and defaults to the bundle beside the package" \
  grep -qF "<string>$BIN</string>" "$AGENTS/com.ignacy.diplomat.autoupdate.plist"
echo "installers: all passed"
