#!/usr/bin/env bash
# Install a systemd *user* timer that self-updates the applet daily at 06:00 —
# the Linux analogue of a launchd StartCalendarInterval. The timer runs the
# launcher in its headless self-update mode (DIPLOMAT_SELF_UPDATE=1): fetch,
# merge upstream if behind, rebuild diplomat-core, and relaunch the tray only if
# one is running. Persistent=true so a 6AM missed while the machine was off runs
# at the next boot. Idempotent; safe to re-run.
set -euo pipefail

LINUX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${LINUX_DIR}/diplomat"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${UNIT_DIR}/diplomat-update.service"
TIMER="${UNIT_DIR}/diplomat-update.timer"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — cannot install the auto-update timer." >&2
    echo "The Update button still works; only the 6AM schedule is unavailable." >&2
    exit 1
fi

chmod +x "$LAUNCHER"
mkdir -p "$UNIT_DIR"

cat > "$SERVICE" <<EOF
[Unit]
Description=Diplomat daily self-update (merge upstream, rebuild, relaunch)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# When the checkout is behind AND a tray is running, run_scheduled relaunches the GUI
# (relaunch(): subprocess.Popen(..., start_new_session=True)) so it swaps onto the fresh
# build via acquire_newest_wins. setsid()/start_new_session creates a new session but does
# NOT move the child out of THIS unit's cgroup, so under the default KillMode=control-group
# systemd SIGTERM/SIGKILLs it the instant this oneshot deactivates — killing the relaunched
# tray mid-startup and silently defeating the update swap. KillMode=process kills only the
# (already-exited) main ExecStart process, sparing the detached tray. NOT RemainAfterExit=yes:
# that leaves the oneshot 'active (exited)', making the daily timer's `start` a no-op.
KillMode=process
Environment=DIPLOMAT_SELF_UPDATE=1
ExecStart=/bin/bash ${LAUNCHER}
EOF

cat > "$TIMER" <<EOF
[Unit]
Description=Run Diplomat self-update daily at 06:00

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Retire the pre-rename (Argent Utils) units, if this machine still has them.
systemctl --user disable --now argent-utils-update.timer 2>/dev/null || true
rm -f "${UNIT_DIR}/argent-utils-update.service" "${UNIT_DIR}/argent-utils-update.timer"

systemctl --user daemon-reload
systemctl --user enable --now diplomat-update.timer

echo "Installed auto-update timer: ${TIMER}"
echo "Next run:"
systemctl --user list-timers diplomat-update.timer --no-pager 2>/dev/null || true
