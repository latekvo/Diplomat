"""The library depends on nothing.

Two claims the README makes, both of which decay silently the moment someone adds
a convenient import: SzpontNet does not reach into the application hosting it, and
it needs nothing outside the standard library (Ed25519 identity being an explicit,
guarded extra). Neither shows up as a failure in a repository where the
application and its dependencies are always present — the node just quietly stops
being installable on its own.

So this reads the source rather than the runtime: every import in the package is
either relative, standard library, or one of the extras named here.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "szpontnet"

# Declared in pyproject as optional extras, and each imported behind a guard that
# degrades the node rather than failing it: `trust` (cryptography) leaves it keyless,
# `wan` (iroh) leaves it LAN-only. Nothing else may be added here without also adding
# it to the project's dependencies and saying so in the README.
OPTIONAL_EXTRAS = {"cryptography", "iroh"}

MODULES = sorted(p.name for p in PACKAGE.glob("*.py"))

# What a leak looks like in prose: the host application by name, or the asset file
# holding its half of the network model. The library reads that half through
# ``Host.model``; a docstring that names the file instead is how a reader learns to
# reach for it directly.
HOST_NEEDLES = ("diplomat", "assets/mesh.json")


def _imported_roots(path: Path) -> set[str]:
    """Every top-level package name this module imports absolutely."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_there_are_modules_to_check():
    """Anti-vacuity: a glob that stopped matching would make every test below pass
    over an empty list."""
    assert len(MODULES) > 15
    assert "node.py" in MODULES


@pytest.mark.parametrize("name", MODULES)
def test_a_module_imports_only_the_standard_library(name):
    outside = _imported_roots(PACKAGE / name) - sys.stdlib_module_names - OPTIONAL_EXTRAS
    assert not outside, f"{name} imports {sorted(outside)}"


def _host_mentions(text: str) -> list[str]:
    """Lines naming the application this library happens to ship beside.

    ``SZPONTNET_*`` env-var spellings are stripped first: they are the node's
    own configuration surface, still carrying the old namespace, and renaming them
    is its own change. Everything else — a file path, a state directory, a module
    — is the library reaching for something it must not know exists.

    Lines about the historical **rename** are kept: they name deployments that
    exist in the wild, whose ghosts a fresh node still has to recognise, so that
    knowledge cannot be generalised away without breaking the reaper.
    """
    hits = []
    for line in text.splitlines():
        if "rename" in line.lower() or "_MESH_MODULES" in line:
            continue
        bare = re.sub(r"DIPLOMAT_[A-Z0-9_]*", "", line).lower()
        if any(needle in bare for needle in HOST_NEEDLES):
            hits.append(line.strip())
    return hits


@pytest.mark.parametrize("name", MODULES)
def test_a_module_never_names_its_host(name):
    """Not even in a comment or a docstring: the point of the host seam is that the
    library cannot tell which application is behind it, and a reference to one by
    name is where that stops being true — a docstring pointing at the host's asset
    file is how the seam is quietly reintroduced after being taken out."""
    assert not _host_mentions((PACKAGE / name).read_text(encoding="utf-8")), name


def test_the_host_scan_would_actually_catch_one():
    """Anti-vacuity: the scan strips the env-var namespace and skips the rename
    notes, and either exemption is one careless widening away from swallowing every
    real hit."""
    assert _host_mentions("# see assets/mesh.json for the duty catalog")
    assert _host_mentions('DIR = Path.home() / ".diplomat" / "mesh"')
    assert _host_mentions("from diplomat_app import core")
    assert not _host_mentions('os.environ.get("SZPONTNET_DIR")')


def test_the_package_carries_its_own_network_model():
    """A library that reads its constants out of some application's asset
    directory is not one you can pip-install anywhere."""
    from szpontnet import host

    assert (PACKAGE / "netmodel.json").is_file()
    assert host.netmodel()["protocol"]["version"] == 1
    assert [d["id"] for d in host.netmodel()["duties"]]
