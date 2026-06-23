"""Headless UI render — snapshot a panel state to PNG and exit.

The Linux analogue of macOS Render.swift. Lets us verify the rendered UI without
a real display by grabbing the widget's own pixels:

    ARGENT_UTILS_RENDER=panel ARGENT_UTILS_RENDER_OUT=/tmp/p.png \
        QT_QPA_PLATFORM=offscreen python -m argent_utils

what ∈ {panel, lookup, wizard, settings}. With ARGENT_UTILS_RENDER_LIVE=1 it
fetches real data first; otherwise it uses a small synthetic fixture.
"""

from __future__ import annotations

import os
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


def run(what: str, out: str) -> int:
    app = QApplication.instance() or QApplication([])
    store = Store()
    if os.environ.get("ARGENT_UTILS_RENDER_LIVE") == "1":
        store.refresh()
    else:
        _fixture(store)

    panel = Panel(store)
    if what == "lookup":
        panel.search.setText("389")
        panel._update_results()
    elif what == "wizard":
        panel._open_wizard()
    elif what == "settings":
        panel._toggle_settings()
    else:  # panel
        panel._rebuild_grid()
        panel._update_results()

    panel.show()
    app.processEvents()
    app.processEvents()
    ok = panel.grab().save(out)
    panel.hide()
    print(f"rendered {what} -> {out} ({'ok' if ok else 'FAILED'})")
    return 0 if ok else 1
