"""Atomic JSON reads and writes for the applet's own state files.

Today that means the shared ``~/.diplomat/config.json`` (:mod:`.appconfig`), which
resolves the repo the agents work in — so it has to work on a machine with no mesh
add-on installed, which is why this is Diplomat's own copy rather than a borrow
from :mod:`szpontnet.atomicjson`. The two are pinned against each other by
behaviour in ``diplomat-platform/linux/tests/test_atomicjson.py``.

Writing: serialise to JSON, write a sibling ``*.json.tmp``, then ``os.replace`` it
over the target so a concurrent reader never sees a torn file. A failed write (a
full disk, an unwritable ``HOME``) removes the tmp and raises ``OSError``, so a
caller can never report a write that did not happen.

Reading never raises: none of these files is a correctness dependency, so one that
is missing, unreadable, not valid UTF-8, not valid JSON, or valid JSON that is not
an *object* degrades to "nothing here" rather than propagating out of a loader into
a startup path. The guard is easy to get subtly wrong: ``(OSError,
json.JSONDecodeError)`` looks exhaustive and is not — it misses the
``UnicodeDecodeError`` that ``read_text`` raises on a non-UTF-8 file. ``ValueError``
is the base class that covers both.
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
