#!/usr/bin/env bash
# Install the Linux Argent Utils applet as an XDG autostart entry, so the tray
# wrench reappears on every login (XFCE, KDE, GNOME, …). The .desktop file is
# the cross-desktop analogue of the macOS LaunchAgent.
set -euo pipefail

LINUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${LINUX_DIR}/argent-utils"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP="${AUTOSTART_DIR}/argent-utils.desktop"

chmod +x "$LAUNCHER"
mkdir -p "$AUTOSTART_DIR"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Argent Utils
Comment=software-mansion/argent triage tools in the system tray
Exec=${LAUNCHER}
Icon=applications-development
Terminal=false
Categories=Development;Utility;
X-GNOME-Autostart-enabled=true
EOF

echo "Installed autostart entry: ${DESKTOP}"
echo "Starting Argent Utils now…"
nohup "$LAUNCHER" >/tmp/argent-utils.log 2>&1 &
echo "Started (log: /tmp/argent-utils.log). Quit from the tray ⏻ button."
