#!/usr/bin/env bash
# The macOS app's window and focus behaviour, end to end, inside a tart VM - never on
# the machine you are working on. Builds Diplomat.app from this checkout on the host,
# boots the VM headless, and drives the real bundle there: keys, clicks and
# screenshots go through the VM's own VNC server (its virtual keyboard and mouse, so
# no Accessibility grant is needed and nothing reaches the host's screen), commands
# through `tart exec`.
#
#   scripts/macos-vm-test.sh            run every check; exit code = verdict
#   KEEP_VM=1 scripts/macos-vm-test.sh  leave the VM running afterwards
#
# DIPLOMAT_VM names the VM (default diplomat-ui-test), cloned on first use from
# DIPLOMAT_VM_IMAGE (default ghcr.io/cirruslabs/macos-sequoia-base:latest, ~25 GB).
# TART is the tart binary (default: tart on PATH). Screenshots land in
# DIPLOMAT_VM_OUT (default a fresh temp dir), named after the check that took them.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TART="${TART:-$(command -v tart || true)}"
VM="${DIPLOMAT_VM:-diplomat-ui-test}"
IMAGE="${DIPLOMAT_VM_IMAGE:-ghcr.io/cirruslabs/macos-sequoia-base:latest}"
OUT="${DIPLOMAT_VM_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/diplomat-vm.XXXXXX")}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/diplomat-vm"
[ -x "$TART" ] || { echo "tart not found: https://tart.run (or set TART=)" >&2; exit 2; }
mkdir -p "$OUT" "$CACHE"
# tart opens a VNC viewer on the host unless told it runs unattended.
export CI=1

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/diplomat-vm-stage.XXXXXX")"
TART_PID=""
cleanup() {
  if [ -n "$TART_PID" ] && [ -z "${KEEP_VM:-}" ]; then
    "$TART" stop "$VM" >/dev/null 2>&1 || kill "$TART_PID" 2>/dev/null || true
  fi
  rm -rf "$STAGE"
}
trap cleanup EXIT

echo "== host: build the bundle and the helpers"
# Assembled in the stage dir rather than by build-app.sh, which swaps the bundle the
# checkout's own LaunchAgents run.
MACOS="$ROOT/packages/diplomat-platform/macos"
(cd "$MACOS" && swift build -c release) >"$OUT/build.log" 2>&1 \
  || { tail -20 "$OUT/build.log" >&2; exit 1; }
"$MACOS/install/assemble-app.sh" "$(cd "$MACOS" && swift build -c release --show-bin-path)/Diplomat" \
  "$ROOT/packages/diplomat-core/assets" "$ROOT" "$STAGE/Diplomat.app"
swiftc -O -o "$STAGE/winlist" "$ROOT/scripts/vm/winlist.swift"
swiftc -O -o "$CACHE/ocr" "$ROOT/scripts/vm/ocr.swift"
if [ ! -x "$CACHE/venv/bin/vncdo" ]; then
  python3 -m venv "$CACHE/venv" && "$CACHE/venv/bin/pip" -q install vncdotool
fi

echo "== host: boot $VM headless"
if ! "$TART" list --quiet 2>/dev/null | grep -qx "$VM"; then
  "$TART" clone "$IMAGE" "$VM"
  "$TART" set "$VM" --display 1920x1200
fi
"$TART" run "$VM" --no-graphics --vnc-experimental --dir="stage:$STAGE:ro" >"$OUT/tart.log" 2>&1 &
TART_PID=$!
VNC_URL=""
for _ in $(seq 1 120); do
  VNC_URL="$(grep -oE 'vnc://[^ ]+' "$OUT/tart.log" | head -1 || true)"
  [ -n "$VNC_URL" ] && "$TART" exec "$VM" true >/dev/null 2>&1 && break
  kill -0 "$TART_PID" 2>/dev/null || { cat "$OUT/tart.log" >&2; exit 1; }
  sleep 2
done
[ -n "$VNC_URL" ] || { echo "the VM never came up" >&2; cat "$OUT/tart.log" >&2; exit 1; }
# vnc://:<password>@<host>:<port>
VNC_PASS="$(sed -E 's|vnc://:([^@]*)@.*|\1|' <<<"$VNC_URL")"
VNC_HOST="$(sed -E 's|.*@([^:]+):.*|\1|' <<<"$VNC_URL")"
VNC_PORT="$(sed -E 's|.*:([0-9]+)/?$|\1|' <<<"$VNC_URL")"

g() { "$TART" exec "$VM" /bin/bash -lc "$*"; }
vnc() { "$CACHE/venv/bin/vncdo" -s "$VNC_HOST::$VNC_PORT" -p "$VNC_PASS" "$@"; }
shot() { vnc capture "$OUT/$1.png"; }
# Click the first line of text containing $2 on a fresh screenshot named $1.
click_text() {
  shot "$1"
  local at; at="$("$CACHE/ocr" "$OUT/$1.png" "$2")" || return 1
  vnc move ${at% *} ${at#* } click 1
}

FAILED=0
check() {   # $1 = what is proven, the rest = the command that proves it
  local name="$1"; shift
  if "$@"; then echo "  ok    $name"; else echo "  FAIL  $name"; FAILED=$((FAILED + 1)); fi
}
# Poll a predicate for up to $1 seconds.
within() { local t="$1"; shift; for _ in $(seq 1 $((t * 2))); do "$@" && return 0; sleep 0.5; done; return 1; }

STAGED="/Volumes/My Shared Files/stage"
# A layout RepoPaths takes for a checkout, so the watchdog finds the bundle it launches.
APP='$HOME/chk/packages/diplomat-platform/macos/Diplomat.app'
BIN="$APP/Contents/MacOS/Diplomat"

echo "== guest: install the bundle"
g "pkill -x Diplomat; launchctl bootout gui/\$(id -u)/com.ignacy.diplomat 2>/dev/null; \
   rm -rf ~/chk ~/.diplomat/operator-quit && mkdir -p ~/chk/packages/diplomat-platform/macos && touch ~/chk/.git && \
   cp -R '$STAGED/Diplomat.app' $APP && cp '$STAGED/winlist' ~/winlist && \
   defaults write com.ignacy.diplomat didTriggerTerminalAutomation -bool true"

pid() { g "pgrep -x Diplomat | head -1" 2>/dev/null || true; }
front() { g 'lsappinfo info -only name "$(lsappinfo front)"' | sed -E 's/.*"name"="([^"]*)".*/\1/'; }
app_type() { g "lsappinfo info -only ApplicationType Diplomat" | sed -E 's/.*="([^"]*)".*/\1/'; }
# Diplomat's on-screen windows at least as wide as the panel.
windows() { g "~/winlist" | awk '$2 == "Diplomat" { split($3, s, "x"); if (s[1] >= 1100) print }'; }
has_window() { [ -n "$(windows)" ]; }
no_window() { [ -z "$(windows)" ]; }
front_is() { [ "$(front)" = "$1" ]; }
finder_front() { g "open -a Finder" && within 5 front_is Finder; }
settings_shown() { shot "$1" && "$CACHE/ocr" "$OUT/$1.png" "AGENT RUNNER" >/dev/null; }
running() { [ -n "$(pid)" ]; }
gone() { [ -z "$(pid)" ]; }
same_pid() { [ "$(pid)" = "$1" ]; }
quit_marked() { g "test -e ~/.diplomat/operator-quit"; }

echo "== an attended launch opens the window and takes focus"
check "Finder is frontmost to begin with" finder_front
g "open -n $APP"
check "a window as wide as the panel comes up" within 20 has_window
check "Diplomat is frontmost" within 5 front_is Diplomat
check "it is a regular app, with a Dock icon" [ "$(app_type)" = "Foreground" ]
shot 1-attended
check "the main panel shows, not Settings" bash -c '! "$0" "$1" "AGENT RUNNER" >/dev/null' "$CACHE/ocr" "$OUT/1-attended.png"
FIRST="$(pid)"

echo "== the menu bar: Settings, close, reopen"
vnc key super-comma
check "⌘, opens Settings in the window" within 5 settings_shown 2-settings
vnc key super-w
check "⌘W closes the window" within 5 no_window
check "…and the app keeps running" same_pid "$FIRST"
shot 3-closed
g "open $APP"
check "launching the running app brings the window back" within 10 has_window
check "…in the same process" same_pid "$FIRST"
check "…frontmost" within 5 front_is Diplomat
vnc key super-w
check "closed again" within 5 no_window
vnc key super-0
check "Window ▸ Diplomat (⌘0) brings it back too" within 5 has_window

echo "== quitting asks first"
vnc key super-q
sleep 1
shot 4-quit-alert
check "⌘Q asks before quitting" "$CACHE/ocr" "$OUT/4-quit-alert.png" "Quit Diplomat?"
vnc key enter
sleep 1
check "Return cancels, the app stays" same_pid "$FIRST"
vnc key super-q
sleep 1
check "the Quit button is there to click" click_text 4-quit-alert-2 "=Quit"
check "Quit quits" within 10 gone
check "…and marks the quit deliberate, so nothing revives it" quit_marked

echo "== an unattended launch takes no focus and opens no window"
check "Finder is frontmost" finder_front
g "cat > ~/Library/LaunchAgents/com.ignacy.diplomat.plist <<PL
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>com.ignacy.diplomat</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>EnvironmentVariables</key><dict><key>DIPLOMAT_BACKGROUND</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
PL
launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.ignacy.diplomat.plist"
check "the login agent starts it" within 20 running
sleep 5
check "no window comes up" no_window
check "Finder stays frontmost" front_is Finder
check "it still has its Dock icon" [ "$(app_type)" = "Foreground" ]
shot 5-login
LOGIN="$(pid)"
g "open $APP"
check "launching it opens the window it never showed" within 10 has_window
check "…in the login instance" same_pid "$LOGIN"
check "…frontmost" within 5 front_is Diplomat

echo "== the watchdog's revival takes no focus either"
g "launchctl bootout gui/\$(id -u)/com.ignacy.diplomat; pkill -9 -x Diplomat; rm -f ~/.diplomat/operator-quit"
check "the app is down" within 10 gone
check "Finder is frontmost" finder_front
g "DIPLOMAT_WATCHDOG=1 $BIN; tail -1 ~/Library/Logs/diplomat-autoupdate.log"
check "the watchdog brings it back" within 20 running
sleep 5
check "no window comes up" no_window
check "Finder stays frontmost" front_is Finder
shot 6-watchdog
g "open $APP"
check "launching it opens the window" within 10 has_window

echo
if [ "$FAILED" -eq 0 ]; then echo "vm: all passed  (screenshots: $OUT)"; else echo "vm: FAILED $FAILED  (screenshots: $OUT)"; fi
[ "$FAILED" -eq 0 ]
