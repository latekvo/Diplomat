"""Cross-process app settings — ``~/.diplomat/config.json``.

Nearly every setting belongs to one front-end and lives in that front-end's own
store (``QSettings`` here, ``UserDefaults`` on macOS). The repo root can't: the
agent that consumes it is launched by whichever process picks the work up, and one
of those is a **mesh node** — a separate process that is stdlib-only by design (the
root README advertises joining a mesh with "no Qt needed") and that outlives the
applet, so it can neither read a Qt/UserDefaults store nor be handed the value in
its environment at spawn time.

So this one knob lives in the shared ``~/.diplomat`` tree, the way the ban list,
the activity feed and the mesh snapshot already cross process *and* front-end
boundaries. Readers re-read on use, so a change reaches a running node on its next
spawn. ``DIPLOMAT_CONFIG`` relocates the file (tests, self-checks) exactly as
``DIPLOMAT_MESH_DIR`` relocates the mesh state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The mesh's shared atomic writer rather than a seventh copy of tmp-file+rename
# (see the dedup that introduced it). stdlib-only, so the node keeps its Qt-free
# import graph.
from .mesh.atomicjson import write_atomic

# Keys. Kept in sync with Swift's `AppConfig` (Sources/Diplomat/AppConfig.swift).
REPO_ROOT = "repoRoot"


def path() -> Path:
    env = os.environ.get("DIPLOMAT_CONFIG")
    if not env:
        return Path.home() / ".diplomat" / "config.json"
    try:
        return Path(env).expanduser()
    except RuntimeError:  # e.g. "~nosuchuser/..." — no home to expand; use it verbatim
        return Path(env)


def read() -> dict:
    """The whole file, or ``{}`` when it's absent, unreadable or not a JSON object —
    a truncated or hand-edited file must degrade to defaults, never break a spawn."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: str = "") -> str:
    value = read().get(key, default)
    return value if isinstance(value, str) else default


def set_value(key: str, value: str) -> None:
    """Read-modify-write one key (empty value removes it), atomically, so a node
    reading concurrently never sees a torn file. Keys the file already holds survive a
    normal write; a file that failed to parse (see :func:`read`) is rewritten from
    defaults, so a *corrupt* file loses any other keys — acceptable while repo root is
    the only key, revisit if a second one lands here."""
    data = read()
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    write_atomic(path(), data)
