"""Atomic JSON reads and writes for the node's small state files.

Every one — the local identity (``node.json``), the peer-address and onion caches,
the trust store, the ban list, per-plan usage stats and the public topology
snapshot — is persisted the same way: serialise to JSON, write a sibling
``*.json.tmp``, then ``os.replace`` it over the target so a concurrent reader
never sees a torn file. A failed write (a full disk, an unwritable ``HOME``)
removes the tmp and raises ``OSError``: whether that is a request to refuse or a
write to retry later is the caller's to decide, and only the caller knows which.

Reading never raises: none of these files is a correctness dependency (a cache
is an accelerator, a corrupt store means "no entries"), so a file that is missing,
unreadable, not valid UTF-8, not valid JSON, or valid JSON that is not an *object*
must degrade to "nothing here" rather than propagate out of a loader into the
node's startup path.

One body per direction, because the read guard is easy to get subtly wrong:
``(OSError, json.JSONDecodeError)`` looks exhaustive and is not — it misses the
``UnicodeDecodeError`` that ``read_text`` raises on a non-UTF-8 file, which is
enough to crash the node on startup instead of resetting. ``ValueError`` is the
base class that covers both.

``indent`` is a parameter because the snapshot and peer cache historically
serialised with ``indent=1`` and the rest with ``indent=2`` — the default keeps
each file's on-disk shape unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def write_atomic(path: Path, obj: object, *, indent: int = 2) -> None:
    """Serialise ``obj`` to ``path`` via a tmp file + rename.

    Raises ``OSError`` when the file could not be written, with ``path`` untouched and
    the tmp file removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=indent) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_object(path: Path) -> dict | None:
    """``path`` decoded as a JSON object, or ``None`` when it holds no usable one.

    ``None`` covers every way the file can fail to be one — absent, unreadable,
    not UTF-8, not JSON, or JSON that decodes to a scalar/array — so a caller
    never has to distinguish them. Callers wanting "empty" instead of "absent"
    spell it ``read_object(p) or {}``. Never raises.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers json.JSONDecodeError *and* the UnicodeDecodeError a
        # non-UTF-8 file raises out of read_text.
        return None
    return data if isinstance(data, dict) else None
