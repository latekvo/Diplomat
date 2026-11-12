"""The shared runtime reaches back into neither front-end.

:mod:`diplomat_runtime` is Diplomat below the UI, on the standard library alone - no
Qt, no applet. Nothing about a checkout makes a breach visible: both packages are
always present here, so an ``import diplomat_app`` added to the runtime resolves fine
under pytest, and the layering is inverted without one failure.

So this reads the source rather than the runtime. One rule covers both halves: every
absolute import in the package is standard library. `diplomat_app` and `PySide6` fail
it for the same reason anything else would.
"""

from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGES = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
PACKAGE = os.path.join(_PACKAGES, "diplomat-runtime", "diplomat_runtime")

MODULES = sorted(f for f in os.listdir(PACKAGE) if f.endswith(".py"))


def _imported_roots(source: str) -> set[str]:
    """Every top-level package this source imports absolutely. Relative imports are
    skipped because they cannot leave the package — which is the whole point."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _outside(name: str) -> list[str]:
    with open(os.path.join(PACKAGE, name), encoding="utf-8") as fh:
        return sorted(_imported_roots(fh.read()) - sys.stdlib_module_names)


def test_there_are_modules_to_check():
    """Anti-vacuity: a renamed package would turn the test below into a loop over
    nothing that passes forever."""
    assert len(MODULES) > 20, MODULES
    assert "agentstate.py" in MODULES


def test_no_runtime_module_imports_a_front_end():
    offenders = {name: out for name in MODULES if (out := _outside(name))}
    assert not offenders, (
        f"the shared runtime may import the standard library, nothing else: {offenders}"
    )


def test_the_scan_would_actually_catch_one():
    """The rule above is only worth its line if the scan sees the import, in whichever
    form it arrives."""
    for source in ("import diplomat_app",
                   "from diplomat_app import store",
                   "import diplomat_app.store as s",
                   "from PySide6.QtWidgets import QWidget"):
        found = _imported_roots(source) - sys.stdlib_module_names
        assert found, source
