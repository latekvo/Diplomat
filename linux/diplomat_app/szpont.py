"""Diplomat's one door to SzpontNet.

The mesh is an **add-on**: Diplomat reviews pull requests perfectly well on a
single machine, and everything that spans machines — the topology screen, routing
a job to whoever has budget left — is what the library adds on top. So this module
is the only place that answers "is it here?", and the modules that reach for the
mesh import it inside the call that needs it and ask here first, rather than at
their own top level where a missing library takes the applet down with it.

One module does not yet: :mod:`diplomat_app.meshview` imports the library at
import time and :mod:`diplomat_app.panel` imports *it* at import time, so the
applet still needs the add-on to start. Closing that is what makes the gate
below worth anything.

Resolution mirrors :func:`deviceallocator.package_dir`: an installed
``szpontnet`` wins, and failing that the sibling module in this checkout is put on
the path — so a clone runs the mesh with no install step, exactly like the applet
itself runs from ``linux/``.
"""

from __future__ import annotations

import os
import sys
from importlib import invalidate_caches, util
from pathlib import Path


def package_dir() -> str:
    """Where the SzpontNet project lives; overridable for non-standard checkouts."""
    env = os.environ.get("DIPLOMAT_SZPONTNET_DIR")
    if env:
        return env
    # This file is <repo>/linux/diplomat_app/szpont.py, so parents[2] = <repo>.
    return str(Path(__file__).resolve().parents[2] / "szpontnet")


def _importable() -> bool:
    """Whether ``szpontnet`` can be imported, putting this checkout's copy on the
    path if it isn't already reachable.

    ``find_spec`` rather than an ``import``: this runs during ``diplomat_app``'s own
    import, and pulling the library in here would load it for every entry point
    including the ones that never touch the mesh.
    """
    if _found():
        return True
    sibling = package_dir()
    if not Path(sibling, "szpontnet", "__init__.py").is_file():
        return False
    sys.path.insert(0, sibling)
    invalidate_caches()  # the finders cached "no such package" a moment ago
    return _found()


def _found() -> bool:
    """Whether ``szpontnet`` resolves to a *real* package.

    The ``origin`` check is load-bearing, not belt and braces: run from the repo
    root, the project directory ``szpontnet/`` (which holds ``docs/`` and
    ``conformance/`` beside the package) is itself importable as a **namespace
    package**. That resolves, has no ``__init__``, and exports nothing — so
    ``find_spec`` alone reports the library present and the first real import dies
    on "cannot import name 'host' from 'szpontnet' (unknown location)". A namespace
    package is the one spec with no origin.
    """
    try:
        spec = util.find_spec("szpontnet")
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.origin is not None


AVAILABLE = _importable()
"""Whether the mesh add-on is present at all — the single answer every mesh-shaped
path is gated on, resolved once at import so the finders are walked one time
rather than per control."""


class Unavailable(RuntimeError):
    """SzpontNet is not installed, so there is no mesh to talk to."""


def require() -> None:
    """Raise :class:`Unavailable` unless the library is importable — for the code
    paths that only run behind a mesh control, so the failure names itself."""
    if not AVAILABLE:
        raise Unavailable(
            f"SzpontNet is not installed (looked for {package_dir()}); "
            "the mesh features are unavailable"
        )
