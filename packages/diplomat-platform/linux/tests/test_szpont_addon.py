"""Finding the mesh add-on: :mod:`diplomat_app.szpont`.

Everything mesh-shaped in the applet is gated on one answer — is SzpontNet here?
— so getting that answer wrong is expensive in both directions: a false negative
silently drops the whole topology feature, and a false positive lets the first
real import take the applet down at startup.
"""

from __future__ import annotations

import sys
from importlib import invalidate_caches
from pathlib import Path

from diplomat_app import szpont


def test_the_checkouts_own_library_is_found_without_an_install():
    """A clone runs the mesh with no install step, the same way the applet itself
    runs out of its own package directory."""
    assert szpont.AVAILABLE

    import szpontnet

    assert szpontnet.__file__ is not None
    # Resolved, so this pins *which package* was imported rather than the spelling of
    # whichever sys.path entry got there first.
    assert (Path(szpontnet.__file__).resolve()
            == Path(szpont.package_dir(), "szpontnet", "__init__.py").resolve())


def test_a_namespace_directory_does_not_count_as_the_library(monkeypatch, tmp_path):
    """Any directory named ``szpontnet`` that happens to sit on the path is
    importable as a namespace package. It resolves, has no ``__init__`` and exports
    nothing, so treating "a spec exists" as "the library is here" reports it present
    and then dies on the first real import with "unknown location"."""
    (tmp_path / "szpontnet").mkdir()  # a bare directory, with no package inside it
    for name in [m for m in sys.modules if m == "szpontnet" or m.startswith("szpontnet.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    invalidate_caches()

    assert szpont._found() is False


def test_require_names_where_it_looked(monkeypatch):
    """The failure a mesh control hits when the add-on is missing has to say what
    is missing and where it was expected, not just fail."""
    monkeypatch.setattr(szpont, "AVAILABLE", False)

    try:
        szpont.require()
    except szpont.Unavailable as exc:
        assert szpont.package_dir() in str(exc)
    else:
        raise AssertionError("require() accepted a missing add-on")


def test_require_is_silent_when_the_add_on_is_present():
    szpont.require()


def test_the_package_dir_is_overridable(monkeypatch):
    """For a checkout that doesn't keep the two side by side."""
    monkeypatch.setenv("DIPLOMAT_SZPONTNET_DIR", "/opt/szpontnet")
    assert szpont.package_dir() == "/opt/szpontnet"
