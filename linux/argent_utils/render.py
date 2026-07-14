"""Headless UI render — snapshot a panel state to PNG and exit.

The Linux analogue of macOS Render.swift. Lets us verify the rendered UI without
a real display by grabbing the widget's own pixels:

    ARGENT_UTILS_RENDER=panel ARGENT_UTILS_RENDER_OUT=/tmp/p.png \
        QT_QPA_PLATFORM=offscreen python -m argent_utils

what ∈ {panel, lookup, wizard, conflicts, settings, devices, mesh}. With
ARGENT_UTILS_RENDER_LIVE=1 it fetches real data first; otherwise it uses a small
synthetic fixture.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import QApplication

from .models import OpenIssue, OpenPR
from .panel import Panel
from .store import Store


def _fixture(store: Store) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=15)
    store.me = "latekvo"
    store.prs = [
        OpenPR(389, "Refine device-interact skills", "https://github.com/x/389",
               False, "danieldunderfelt", now - timedelta(hours=5), None,
               ["skills/argent-device-interact/SKILL.md"], None, []),
        OpenPR(204, "Metro debugger polish", "https://github.com/x/204",
               True, "pFornagiel", now - timedelta(days=2), None,
               ["skills/argent-metro-debugger/SKILL.md"], None, []),
        OpenPR(395, "Bump dependencies", "https://github.com/x/395",
               False, "dependabot", now - timedelta(hours=9), None,
               ["packages/argent-cli/package.json"], None, []),
        OpenPR(38, "Long-stale ready PR", "https://github.com/x/38",
               False, "stachbial", old, old, ["skills/x/SKILL.md"], None, []),
    ]
    store.issues = [
        OpenIssue(391, "Crash on boot", "https://github.com/x/391", "t0tl",
                  "NONE", now - timedelta(hours=3), now, 1, [], ["bug"], False),
    ]
    store.has_loaded = True


def _device_fixture(store: Store) -> None:
    """Synthetic device-allocator pool so the Devices section can be eyeballed.
    In-use devices carry a recent `allocatedAt` (epoch ms) so the held duration shows."""
    now_ms = time.time() * 1000
    store.device_state = {
        "updatedAt": "now",
        "daemonPid": 4242,
        "devices": [
            {"key": "ios:99AD", "platform": "ios", "name": "iPhone 16 Pro Max",
             "version": "18.5", "handle": "99AD1D87-DA5F", "status": "ready",
             "owner": {"agentName": "bluesky e2e", "ownerPid": 4242},
             "allocatedAt": now_ms - 12 * 60000, "idleMs": 240000},
            {"key": "android:Pixel_6_API_34", "platform": "android", "name": "Pixel_6_API_34",
             "version": "14", "handle": "emulator-5554", "status": "booting",
             "owner": {"agentName": "checkout flow", "ownerPid": 4310},
             "allocatedAt": now_ms - 83 * 60000},
            {"key": "android:Pixel_3a_API_34", "platform": "android", "name": "Pixel_3a_API_34",
             "version": "14", "handle": None, "status": "repairing",
             "owner": {"agentName": "repair", "ownerPid": None}, "brokenReason": "boot timeout"},
            {"key": "ios:FREE1", "platform": "ios", "name": "iPhone 15", "version": "17.5",
             "handle": None, "status": "free", "owner": None},
            {"key": "android:FREE2", "platform": "android", "name": "Pixel_7_API_35",
             "version": "15", "handle": None, "status": "free", "owner": None},
        ],
    }


def _telemetry_fixture(store: Store) -> None:
    """Synthetic activity feed + ban list so the left telemetry pane can be eyeballed."""
    from datetime import datetime, timedelta, timezone

    from . import activity, bans

    now = datetime.now(timezone.utc)

    def iso(mins: float) -> str:
        return (now - timedelta(minutes=mins)).isoformat()

    store.audit_entries = [
        activity.AuditEntry(iso(1), "panel", "review", "Review · #389 · deep"),
        activity.AuditEntry(iso(4), "auto", "review-req", "Picked up review request on #402"),
        activity.AuditEntry(iso(9), "agent", "merge", "Merged #377 (2 approvals)"),
        activity.AuditEntry(iso(15), "auto", "nudge", "Nudged stalled agent on #389 (API error)"),
        activity.AuditEntry(iso(22), "panel", "conflicts", "Resolve conflicts · #360"),
        activity.AuditEntry(iso(31), "agent", "audit", "Full E2E audit dispatched"),
        activity.AuditEntry(iso(48), "auto", "ban", "Banned @sketchy-bot (prompt injection)"),
        activity.AuditEntry(iso(90), "auto", "kill-device", "Killed idle emulator-5554"),
    ]
    store.banned_authors = [
        bans.BannedAuthor("sketchy-bot", "prompt injection in PR body", "#391"),
        bans.BannedAuthor("evil-actor", "hidden instructions in the diff", None),
    ]


def _mesh_fixture(store: Store) -> None:
    """Synthetic mesh topology so the 🕸️ column can be eyeballed: a Linux self
    node, one strong healthy macOS peer, one weak dead macOS peer, and the three
    duties with one platform shortfall. Enables the mesh via the render-only
    override (never persisting to real QSettings, never starting a node)."""
    self_id = "n-self-linux"
    peer_ok = "n-mbp-strong"
    peer_dead = "n-mbp-weak"
    store._mesh_enabled_override = True
    store.mesh_state = {
        # os.getpid() → node_running() sees a live pid, so the column reads "live".
        "updatedAt": "now",
        "pid": os.getpid(),
        "tcpPort": 40878,
        "self": {
            "id": self_id, "name": "softoobox", "platform": "linux",
            "tier": 4, "tokens": "ok", "tcpPort": 40878, "epoch": 1, "seq": 12,
            "sees": [peer_ok], "dutiesEnabled": {}, "v": 1,
        },
        "peers": [
            {"id": peer_ok, "name": "mbp-strong", "platform": "macos",
             "tier": 1, "tokens": "ok", "tcpPort": 40879, "epoch": 1, "seq": 20,
             "sees": [self_id], "dutiesEnabled": {}, "v": 1,
             "link": "up", "addr": "192.168.1.21", "lastSeenSecsAgo": 1.2},
            {"id": peer_dead, "name": "mbp-weak", "platform": "macos",
             "tier": 5, "tokens": "low", "tcpPort": 40880, "epoch": 1, "seq": 8,
             "sees": [], "dutiesEnabled": {}, "v": 1,
             "link": "down", "addr": "192.168.1.37", "lastSeenSecsAgo": 42},
        ],
        "assignments": {
            "review": {"duty": "review", "assigned": [peer_ok], "shortfall": []},
            "conflicts": {"duty": "conflicts", "assigned": [self_id], "shortfall": []},
            "audit": {"duty": "audit", "assigned": [self_id],
                      "shortfall": [{"platform": "macos", "missing": 1}]},
        },
        "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
        "v": 1,
    }


def run(what: str, out: str) -> int:
    app = QApplication.instance() or QApplication([])
    store = Store()
    if os.environ.get("ARGENT_UTILS_RENDER_LIVE") == "1":
        store.refresh()
    else:
        _fixture(store)

    # The mesh fixture must land before Panel() — the panel reads mesh_enabled to
    # decide whether its 🕸️ column starts expanded.
    if what in ("mesh", "panel", "settings"):
        _mesh_fixture(store)

    panel = Panel(store)
    if what == "lookup":
        panel.search.setText("389")
        panel._update_results()
    elif what == "wizard":
        panel._open_action("review")
    elif what == "conflicts":
        panel._open_action("conflicts")
    elif what == "settings":
        panel._toggle_settings()
    elif what == "devices":
        _device_fixture(store)
        panel._rebuild_devices()
        panel._update_results()
    elif what == "mesh":
        # Fixture already applied above; refresh the column from it.
        store.mesh_changed.emit()
        panel._update_results()
    else:  # panel
        _device_fixture(store)
        _telemetry_fixture(store)
        panel._rebuild_grid()
        panel._rebuild_devices()
        panel._rebuild_telemetry()
        store.mesh_changed.emit()
        panel._update_results()

    panel.show()
    app.processEvents()
    app.processEvents()
    ok = panel.grab().save(out)
    panel.hide()
    print(f"rendered {what} -> {out} ({'ok' if ok else 'FAILED'})")
    return 0 if ok else 1
