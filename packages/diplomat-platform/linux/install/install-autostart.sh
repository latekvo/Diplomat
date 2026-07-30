#!/usr/bin/env bash
# Install the Linux Diplomat applet as an XDG autostart entry, so the tray
# wrench reappears on every login (XFCE, KDE, GNOME, …). The .desktop file is
# the cross-desktop analogue of the macOS LaunchAgent.
set -euo pipefail

LINUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${LINUX_DIR}/diplomat"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="${AUTOSTART_DIR}/diplomat.desktop"

chmod +x "$LAUNCHER"
mkdir -p "$AUTOSTART_DIR"

# Build the Swift prompt engine (diplomat-core) the applet shells out to for the
# Review/Conflicts/Audit prompts. Soft-fail: the tray/UI still runs without it,
# but those actions need the binary (build later with install/build-core.sh once a
# Swift toolchain is installed).
if ! "${LINUX_DIR}/install/build-core.sh"; then
    echo "warning: diplomat-core not built (need a Swift toolchain) — Review/Conflicts/" >&2
    echo "         Audit spawning is unavailable until install/build-core.sh succeeds." >&2
fi

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Diplomat
Comment=software-mansion/argent triage tools in the system tray
Exec=${LAUNCHER}
Icon=applications-development
Terminal=false
Categories=Development;Utility;
X-GNOME-Autostart-enabled=true
EOF

echo "Installed autostart entry: ${DESKTOP}"

# Retire the pre-rename (Argent Utils) autostart entry, if still present.
rm -f "${AUTOSTART_DIR}/argent-utils.desktop"

# Also schedule the daily 6AM self-update (soft-fail: the tray and the manual
# Update button work without it; only the unattended schedule needs systemd).
if ! "${LINUX_DIR}/install/install-autoupdate.sh"; then
    echo "warning: daily auto-update timer not installed — update manually from" >&2
    echo "         the Settings ▸ UPDATE button, or install a systemd user timer." >&2
fi

echo "Starting Diplomat now…"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/diplomat"
mkdir -p "$LOG_DIR"
nohup "$LAUNCHER" >"$LOG_DIR/diplomat.log" 2>&1 &
echo "Started (log: $LOG_DIR/diplomat.log). Quit from the tray ⏻ button."
