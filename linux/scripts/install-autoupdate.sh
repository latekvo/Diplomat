#!/usr/bin/env bash
# Install a systemd *user* timer that self-updates the applet daily at 06:00 —
# the Linux analogue of a launchd StartCalendarInterval. The timer runs the
# launcher in its headless self-update mode (ARGENT_UTILS_SELF_UPDATE=1): fetch,
# merge upstream if behind, rebuild argent-core, and relaunch the tray only if
# one is running. Persistent=true so a 6AM missed while the machine was off runs
# at the next boot. Idempotent; safe to re-run.
set -euo pipefail

LINUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${LINUX_DIR}/argent-utils"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${UNIT_DIR}/argent-utils-update.service"
TIMER="${UNIT_DIR}/argent-utils-update.timer"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — cannot install the auto-update timer." >&2
    echo "The Update button still works; only the 6AM schedule is unavailable." >&2
    exit 1
fi

chmod +x "$LAUNCHER"
mkdir -p "$UNIT_DIR"

cat > "$SERVICE" <<EOF
[Unit]
Description=Argent Utils daily self-update (merge upstream, rebuild, relaunch)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=ARGENT_UTILS_SELF_UPDATE=1
ExecStart=/bin/bash ${LAUNCHER}
EOF

cat > "$TIMER" <<EOF
[Unit]
Description=Run Argent Utils self-update daily at 06:00

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now argent-utils-update.timer

echo "Installed auto-update timer: ${TIMER}"
echo "Next run:"
systemctl --user list-timers argent-utils-update.timer --no-pager 2>/dev/null || true
