#!/usr/bin/env bash
# Install a systemd *user* timer that checks every 5 minutes that the tray is up,
# and launches it if it died (a crash, a kill, a failed update). It runs the
# launcher in its headless watchdog mode (DIPLOMAT_WATCHDOG=1), which launches
# nothing while a tray runs or after the operator quit it from the tray. Idempotent;
# safe to re-run.
set -euo pipefail

LINUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${LINUX_DIR}/diplomat"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${UNIT_DIR}/diplomat-watchdog.service"
TIMER="${UNIT_DIR}/diplomat-watchdog.timer"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — cannot install the watchdog timer." >&2
    echo "A tray that dies stays down until the next login." >&2
    exit 1
fi

chmod +x "$LAUNCHER"
mkdir -p "$UNIT_DIR"

cat > "$SERVICE" <<EOF
[Unit]
Description=Diplomat liveness check (launch the tray if it died)

[Service]
Type=oneshot
# The tray it launches is a detached child in this unit's cgroup; the default
# KillMode=control-group would kill it the moment this oneshot finishes.
KillMode=process
Environment=DIPLOMAT_WATCHDOG=1
ExecStart=/bin/bash ${LAUNCHER}
EOF

cat > "$TIMER" <<EOF
[Unit]
Description=Check every 5 minutes that the Diplomat tray is up

[Timer]
OnCalendar=*:0/5

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now diplomat-watchdog.timer

echo "Installed watchdog timer: ${TIMER}"
