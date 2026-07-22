"""Best-effort atomic JSON writes for the mesh's small state files.

Every mesh state file — the local identity (``node.json``), the peer-address
cache, the trust store, the ban list, per-plan usage stats and the public
topology snapshot — is persisted the same way: serialise to JSON, write a
sibling ``*.json.tmp``, then ``os.replace`` it over the target so a concurrent
reader never sees a torn file. The write is deliberately best-effort — an
unwritable ``HOME`` must never crash the node — so ``OSError`` is swallowed.

Six call sites carried a byte-for-byte copy of this body; they now share it.
``indent`` is a parameter because the snapshot and peer cache historically
serialised with ``indent=1`` and the rest with ``indent=2`` — the default keeps
each file's on-disk shape unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def write_atomic(path: Path, obj: object, *, indent: int = 2) -> None:
    """Serialise ``obj`` to ``path`` via a tmp file + rename. Never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=indent) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
