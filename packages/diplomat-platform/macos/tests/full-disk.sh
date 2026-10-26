#!/usr/bin/env bash
# Diplomat on a full disk, against a real volume filled to ENOSPC by
# scripts/full-volume.sh: assemble-app.sh keeps the last good Diplomat.app when it
# cannot finish the new one, and the app and the diplomat-core CLI exit with their
# own codes, not an abort, when their stdout or stderr is a file on that volume
# (launchd appends Diplomat's stderr to ~/Library/Logs/diplomat.err.log).
#
# Needs `swift build` in this package and in diplomat-core first. Exits 77 where
# no volume can be mounted, unless DIPLOMAT_FULL_VOLUME_REQUIRED=1.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PKG="$(cd "$HERE/.." && pwd -P)"
REPO="$(cd "$PKG/../../.." && pwd -P)"
FV="$REPO/scripts/full-volume.sh"
ASSEMBLE="$PKG/install/assemble-app.sh"
APP_BIN="$PKG/.build/debug/Diplomat"
CORE_BIN="$REPO/packages/diplomat-core/.build/debug/diplomat-core"
ASSETS="$REPO/packages/diplomat-core/assets"
for b in "$APP_BIN" "$CORE_BIN"; do
  [ -x "$b" ] || { echo "full-disk: $b not built; run swift build first" >&2; exit 1; }
done

SCRATCH="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/diplomat-full-disk.XXXXXX")" && pwd -P)"
VOL="$SCRATCH/vol"
APP="$VOL/Diplomat.app"
trap 'bash "$FV" unmount "$VOL" >/dev/null 2>&1 || true; rm -rf "$SCRATCH"' EXIT

code=0
bash "$FV" mount "$VOL" 16m || code=$?
if [ "$code" = 77 ]; then echo "full-disk: skipped, this machine cannot mount a volume to fill"; exit 77; fi
[ "$code" = 0 ] || exit "$code"

check() {   # $1 = what is proven, the rest = the command that proves it
  local name="$1"; shift
  if "$@"; then echo "  ok    $name"; return; fi
  echo "  FAIL  $name" >&2
  for f in "$SCRATCH"/*.txt; do [ -f "$f" ] && { echo "--- $f" >&2; cat "$f" >&2; }; done
  exit 1
}
exits() {   # $1 = expected status, the rest = the command; its output goes to $SCRATCH/run.txt
  local want="$1" got=0; shift
  "$@" > "$SCRATCH/run.txt" 2>&1 || got=$?
  echo "exit $got" >> "$SCRATCH/run.txt"
  [ "$got" = "$want" ]
}
fails() { ! "$@" > "$SCRATCH/run.txt" 2>&1; }
# Every file of a bundle with its checksum, and every directory, so a missing,
# added or changed file all show.
manifest() { (cd "$1" && find . -type d | sort && find . -type f -print0 | sort -z | xargs -0 shasum); }
# The volume holds the bundle, the logs, the fill and its helpers: nothing assembly left.
only_expected() {
  ls -A "$VOL" | grep -vxE 'Diplomat\.app|err\.log|out\.txt|probe\.log|sink|spacer|\.ballast' \
    > "$SCRATCH/leftovers.txt" && return 1
  return 0
}
# A file whose last block is full, so the next byte appended needs a new one.
block_full() { head -c 4096 /dev/zero > "$1"; }
size() { wc -c < "$1" | tr -d ' '; }

head -c 262144 /dev/urandom > "$SCRATCH/old-bin"
head -c 2097152 /dev/urandom > "$SCRATCH/new-bin"   # a release that grew
printf '{"kind": "audit"}' > "$SCRATCH/audit.json"

echo "full-disk: the bundle assembly"
check "assemble-app.sh lays out a bundle on an empty volume" \
  exits 0 "$ASSEMBLE" "$SCRATCH/old-bin" "$ASSETS" /checkout "$APP"
manifest "$APP" > "$SCRATCH/old.manifest"
for f in err.log out.txt probe.log sink; do block_full "$VOL/$f"; done
head -c 1048576 /dev/urandom > "$VOL/spacer"
bash "$FV" fill "$VOL"
# APFS can hand back a few blocks after fill as it flushes; take them until a
# flush gives nothing back.
settle() {
  local round taken
  for round in 1 2 3 4 5 6 7 8 9 10; do
    sync
    taken=0
    while [ "$taken" -lt 1000 ] && head -c 4096 /dev/zero >> "$VOL/sink" 2>/dev/null; do
      taken=$((taken + 1))
    done
    [ "$taken" = 0 ] && return 0
  done
  return 1
}
check "the filled volume settles" settle
check "the filled volume refuses an append to a log" fails sh -c 'printf x >> "$1"' _ "$VOL/probe.log"
check "…and a new directory" fails mkdir "$VOL/probe"

check "with no room at all, assembly fails" \
  fails "$ASSEMBLE" "$SCRATCH/new-bin" "$ASSETS" /checkout "$APP"
manifest "$APP" > "$SCRATCH/after.manifest" 2>/dev/null || true
check "…and the last good bundle is byte-identical" cmp -s "$SCRATCH/old.manifest" "$SCRATCH/after.manifest"
check "…and nothing it staged is left" only_expected

echo "full-disk: output to a full volume"
check "a refused Diplomat mode exits 64 with stderr on the full volume" \
  exits 64 sh -c 'DIPLOMAT_NO_SUCH_TEST=1 perl -e "alarm 60; exec @ARGV" "$1" 2>> "$2"' \
  _ "$APP_BIN" "$VOL/err.log"
check "diplomat-core's usage error exits 1 with stderr on the full volume" \
  exits 1 sh -c '"$1" 2>> "$2"' _ "$CORE_BIN" "$VOL/err.log"
check "diplomat-core build-prompt exits 1 when its stdout is on the full volume" \
  exits 1 sh -c '"$1" build-prompt < "$2" >> "$3"' _ "$CORE_BIN" "$SCRATCH/audit.json" "$VOL/out.txt"
check "…and says why on stderr" grep -q "could not write" "$SCRATCH/run.txt"
check "…with stderr on the full volume too" \
  exits 1 sh -c '"$1" build-prompt < "$2" >> "$3" 2>> "$4"' \
  _ "$CORE_BIN" "$SCRATCH/audit.json" "$VOL/out.txt" "$VOL/err.log"
export DIPLOMAT_AGENTS_DIR="$SCRATCH/agents"   # agent-registry reads its book there, not ~/.diplomat
for job in 'tool-data {}' 'telemetry {}' 'agent-state {}' 'agent-registry {"mode": "read"}' \
           'agent-registry {"mode": "hooks", "activityPath": "/a", "donePath": null}'; do
  printf '%s' "${job#* }" > "$SCRATCH/job.json"
  check "diplomat-core $job exits 1 when its stdout is on the full volume" \
    exits 1 sh -c '"$1" "$2" < "$3" >> "$4"' _ "$CORE_BIN" "${job%% *}" "$SCRATCH/job.json" "$VOL/out.txt"
done
check "…and every one of those writes really failed" \
  test "$(size "$VOL/err.log") $(size "$VOL/out.txt")" = "4096 4096"

rm "$VOL/spacer"
check "with room for part of the new bundle, assembly fails" \
  fails "$ASSEMBLE" "$SCRATCH/new-bin" "$ASSETS" /checkout "$APP"
manifest "$APP" > "$SCRATCH/after.manifest" 2>/dev/null || true
check "…and the last good bundle is byte-identical" cmp -s "$SCRATCH/old.manifest" "$SCRATCH/after.manifest"
check "…and nothing it staged is left" only_expected

echo "full-disk: once the disk has room again"
bash "$FV" drain "$VOL"
check "assembly succeeds" exits 0 "$ASSEMBLE" "$SCRATCH/new-bin" "$ASSETS" /checkout "$APP"
check "…the new binary is in the bundle" cmp -s "$SCRATCH/new-bin" "$APP/Contents/MacOS/Diplomat"
check "…with the assets" diff -r "$ASSETS" "$APP/Contents/Resources/assets"
check "…and the checkout it was built from" \
  test "$(plutil -extract DiplomatCheckout raw "$APP/Contents/Info.plist")" = /checkout
check "…and nothing it staged is left" only_expected
check "diplomat-core build-prompt writes the prompt" \
  exits 0 sh -c '"$1" build-prompt < "$2" >> "$3"' _ "$CORE_BIN" "$SCRATCH/audit.json" "$VOL/out.txt"
check "…to the file" test "$(size "$VOL/out.txt")" -gt 4096
echo "full-disk: all passed"
