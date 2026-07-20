"""One-time migration of the pre-rename state directory.

Diplomat's mesh state used to live under ``~/.argent/mesh`` (back when the app was
"Argent Utils"); the rename moved it to ``~/.diplomat/mesh``. The mesh identity
(``device.key`` + ``node.json``) is what *other* nodes pin their trust to, so a
fresh, empty ``~/.diplomat/mesh`` would mint a NEW identity and silently break this
node's standing across the whole fleet. This preserves the keypair by moving it
once, at applet startup, before the node comes up.

Scope is deliberately narrow:

* Only the **mesh** subdir is moved here — it is owned exclusively by the applet's
  own node. The ``device-allocator`` and ``pr-monitor`` subdirs belong to the Node
  daemon and are migrated by ``device-allocator/src/install.js``, which stops the
  daemon first so it never moves a dir out from under a live writer.
* ``~/.argent`` itself is left alone — it is shared with the separate Argent
  (device-control) tool, whose ``tool-server.json`` lives there.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def migrate_legacy_state_dir() -> None:
    """Move ``~/.argent/mesh`` to ``~/.diplomat/mesh`` once, preserving identity.

    Idempotent and best-effort: a no-op on a clean machine or once migrated, and a
    failure never prevents the app from launching.
    """
    # A DIPLOMAT_MESH_DIR override means the caller owns the path (tests, custom
    # deploys) — never migrate under it.
    if os.environ.get("DIPLOMAT_MESH_DIR"):
        return
    home = Path.home()
    _merge_move(home / ".argent" / "mesh", home / ".diplomat" / "mesh")


def _merge_move(src: Path, dst: Path) -> None:
    """Move entries from ``src`` into ``dst`` without overwriting anything already
    present, so a partially-created new dir neither blocks the move nor clobbers
    newer data. Drops ``src`` once emptied."""
    try:
        if not src.is_dir():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            target = dst / entry.name
            if target.exists():
                continue  # keep the newer copy
            try:
                os.rename(entry, target)
            except OSError:
                # cross-device or a race — copy then drop the source.
                if entry.is_dir():
                    shutil.copytree(entry, target)
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    shutil.copy2(entry, target)
                    entry.unlink(missing_ok=True)
        try:
            src.rmdir()  # only succeeds if we emptied it
        except OSError:
            pass
    except Exception:  # noqa: BLE001 — migration must never break startup
        pass
