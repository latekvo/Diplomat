"""Diplomat's one door to SzpontNet.

The mesh is an **add-on**: Diplomat reviews pull requests perfectly well on a
single machine, and everything that spans machines — the topology screen, routing
a job to whoever has budget left — is what the library adds on top. So this module
is the only place that answers "is it here?", and no module imports the library at
its own top level, where a missing add-on would take the whole applet down.

Two shapes of gate hang off :data:`AVAILABLE`, and which one a call site wants
depends on whether the user could have got there:

* Most of the applet asks :attr:`Store.mesh_enabled`, which is False without the
  add-on — one lever covering every "should this run through the mesh?" path,
  because a machine with no library is not on the mesh whatever its preference
  says. Those call sites put that check *before* their ``from szpontnet import``.
* A control round-trip (the mesh commands, the ⬡ render) calls :func:`require`
  instead. Nothing builds those controls without the library, so arriving there
  is a bug in the caller and reads as one rather than as an ImportError.

:mod:`diplomat_app.meshview` is the exception that proves it: the Mesh screen is
*made of* SzpontNet, so it imports the library normally at its top level and the
Panel imports the screen only behind the gate.

Resolution mirrors :func:`deviceallocator.package_dir`: an installed
``szpontnet`` wins, and failing that the ``szpontnet-core`` package in this
checkout is put on the path — so a clone runs the mesh with no install step,
exactly like the applet itself runs out of its own package directory.
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
    # packages/diplomat-platform/linux/diplomat_app/szpont.py, so parents[3] = packages/.
    return str(Path(__file__).resolve().parents[3] / "szpontnet-core")


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

    The ``origin`` check is load-bearing, not belt and braces: any directory named
    ``szpontnet`` that happens to sit on the path resolves as a **namespace
    package** — it has no ``__init__`` and exports nothing, so ``find_spec`` alone
    reports the library present and the first real import dies on "cannot import
    name 'host' from 'szpontnet' (unknown location)". A namespace package is the
    one spec with no origin, which is what tells the two apart.
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
