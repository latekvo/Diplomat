"""Diplomat — Linux (Qt6/PySide6) front-end.

A thin Qt renderer over :mod:`diplomat_runtime`, the platform-neutral half both
front-ends run (config, PR triage, token accounting, the agent run book, the mesh
host). Only what is Qt-shaped lives here; the macOS SwiftUI app renders the same
logic from ``diplomat-core``.
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

# Put Diplomat behind any SzpontNet node running in this process. Done here rather
# than at each entry point because the cost of missing it is silent: the library's
# defaults are all valid answers, so a node would come up on the canonical v1 duty
# catalog, keep its state somewhere else and drop its log on the floor without a
# word. Importing this package is exactly the condition under which Diplomat is the
# host, so this is where it goes — and it is skipped, not failed, when the add-on
# isn't installed.
from . import szpont as _szpont  # noqa: E402 - after the path bootstrap

if _szpont.AVAILABLE:
    from diplomat_runtime import szponthost as _szponthost  # noqa: E402

    _szponthost.install()
