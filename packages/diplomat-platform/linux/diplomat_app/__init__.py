"""Diplomat — Linux (Qt6/PySide6) front-end.

A thin Qt renderer over :mod:`diplomat_runtime`, the platform-neutral half below the
UI (config, PR triage, token accounting, the agent run book). Only what is Qt-shaped
lives here; the macOS SwiftUI app renders the same logic from ``diplomat-core``.
"""

import sys
from pathlib import Path

__all__ = ["app", "store"]

#: packages/diplomat-platform/linux/diplomat_app/__init__.py, so parents[3] = packages/.
RUNTIME_DIR = str(Path(__file__).resolve().parents[3] / "diplomat-runtime")

# Before anything else here: every module below imports the runtime at its own top
# level, and this package runs out of a checkout rather than an install, so nothing else
# has put its sibling on the path for it.
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)
