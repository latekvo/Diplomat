#!/usr/bin/env bash
# A small real filesystem that a test can fill to ENOSPC and drain again, so
# "the disk is full" is exercised against the kernel rather than a mocked error.
#
#   full-volume.sh mount   <dir> [size]  mount an empty volume at <dir> (default 8m)
#   full-volume.sh fill    <dir>         fill it until no byte of data can land
#   full-volume.sh drain   <dir>         free the space fill took
#   full-volume.sh unmount <dir>         detach it and delete its backing store
#
# macOS: an APFS disk image (no root needed). APFS keeps reserves that a single
# dd leaves behind, so fill keeps going in smaller pieces until even a rename of
# an existing file fails - which is how the real disk failed. Linux: a tmpfs,
# which needs root or passwordless sudo; there a new file or a rename still
# succeeds (it costs an inode, not data), and only the data write fails.
#
# Exits 77 when this machine cannot mount one. A caller treats that as a skip,
# unless DIPLOMAT_FULL_VOLUME_REQUIRED=1 (CI sets it), when it is a failure.
set -euo pipefail

cmd="${1:-}"; dir="${2:-}"
[ -n "$cmd" ] && [ -n "$dir" ] || { echo "usage: $0 mount|fill|drain|unmount <dir> [size]" >&2; exit 2; }
ballast="$dir/.ballast"
image="$dir.dmg"   # macOS backing store, beside the mount point

unsupported() {
  echo "full-volume: $1" >&2
  [ "${DIPLOMAT_FULL_VOLUME_REQUIRED:-}" = 1 ] && exit 1
  exit 77
}

as_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n "$@"; fi; }

case "$cmd" in
  mount)
    size="${3:-8m}"
    mkdir -p "$dir"
    case "$(uname -s)" in
      Darwin)
        command -v hdiutil >/dev/null || unsupported "no hdiutil"
        hdiutil create -size "$size" -fs APFS -volname dfull "$image" -ov -quiet
        hdiutil attach "$image" -nobrowse -noautoopen -mountpoint "$dir" -quiet
        ;;
      Linux)
        { [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; } || unsupported "mounting a tmpfs needs root or passwordless sudo"
        as_root mount -t tmpfs -o "size=$size,mode=0777" tmpfs "$dir"
        ;;
      *) unsupported "no full-volume backend for $(uname -s)" ;;
    esac
    ;;
  fill)
    mkdir -p "$ballast"
    # Big pieces first for speed, then ever smaller ones for the tail. APFS hands
    # back blocks as pending writes and frees are flushed, so passes repeat, each
    # after a sync, until a probe write fails.
    probe="$dir/.full-volume-probe.$$"
    for pass in 1 2 3 4 5 6; do
      for bs in 65536 4096 512 1; do
        dd if=/dev/zero bs="$bs" >> "$ballast/0" 2>/dev/null || true
      done
      # Directory entries and metadata live in space dd cannot reach.
      i=0
      while [ "$i" -lt 10000 ] && mkdir "$ballast/d$pass.$i" 2>/dev/null; do i=$((i + 1)); done
      i=0
      while [ "$i" -lt 10000 ] && { printf x > "$ballast/f$pass.$i"; } 2>/dev/null; do i=$((i + 1)); done
      sync
      if [ "$pass" -ge 2 ] && ! { { printf x > "$probe"; } 2>/dev/null && [ -s "$probe" ]; }; then
        rm -f "$probe" 2>/dev/null || true
        exit 0
      fi
      rm -f "$probe" 2>/dev/null || true
    done
    echo "full-volume: $dir still takes writes after fill" >&2
    exit 1
    ;;
  drain)
    rm -rf "$ballast"
    ;;
  unmount)
    case "$(uname -s)" in
      Darwin)
        hdiutil detach "$dir" -force -quiet 2>/dev/null || true
        rm -f "$image"
        ;;
      Linux) as_root umount "$dir" 2>/dev/null || true ;;
    esac
    rmdir "$dir" 2>/dev/null || true
    ;;
  *) echo "full-volume: unknown command $cmd" >&2; exit 2 ;;
esac
