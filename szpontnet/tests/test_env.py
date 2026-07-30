"""The library's environment namespace, and the bridge from the old one.

Two things worth a test rather than a convention. The first is that the knobs are
spelled ``SZPONTNET_*`` *everywhere*: they are part of the spec — the conformance
tester configures a candidate through them and has no other channel — so one read
left under another spelling is a node the tester silently cannot drive.

The second is the fallback to the pre-rename names, which exists for exactly one
dangerous case and is otherwise invisible: nothing else in this repository sets an
old name any more, so without this file the bridge would be dead code that stopped
working the moment someone tidied it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from szpontnet import config, env, identity

PACKAGE = Path(__file__).resolve().parents[1] / "szpontnet"


def test_the_new_name_is_read(monkeypatch):
    monkeypatch.setenv("SZPONTNET_SECRET", "shibboleth")
    assert env.get("SECRET") == "shibboleth"
    assert config.secret() == "shibboleth"


def test_the_old_name_still_works(monkeypatch):
    """A join token exported from a shell profile written before the rename.

    Reading nothing here does not fail loudly — it comes up as an **open** mesh
    that any machine on the LAN can join, on a box whose operator believes it is
    fenced. That is the whole reason the fallback exists."""
    monkeypatch.delenv("SZPONTNET_SECRET", raising=False)
    monkeypatch.setenv("DIPLOMAT_MESH_SECRET", "shibboleth")
    assert config.secret() == "shibboleth"


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("SZPONTNET_SECRET", "current")
    monkeypatch.setenv("DIPLOMAT_MESH_SECRET", "stale")
    assert config.secret() == "current"


def test_an_empty_new_name_is_a_deliberate_empty(monkeypatch):
    """``SZPONTNET_SECRET=""`` means "no token, on purpose" — a mesh someone
    deliberately opened, or a test pinning the unfenced path. Falling through to
    the old name there would resurrect a value the operator just cleared."""
    monkeypatch.setenv("SZPONTNET_SECRET", "")
    monkeypatch.setenv("DIPLOMAT_MESH_SECRET", "stale")
    assert config.secret() == ""


def test_the_default_applies_only_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("SZPONTNET_SECRET", raising=False)
    monkeypatch.delenv("DIPLOMAT_MESH_SECRET", raising=False)
    assert env.get("SECRET", "fallback") == "fallback"
    assert env.get("SECRET") is None


def test_the_bridge_covers_the_knobs_that_are_not_plain_strings(monkeypatch):
    """The protocol overrides are read through a different path (a suffix table,
    parsed to the type of the default they replace) and the state dir through
    another still, so "SECRET works" says nothing about either."""
    monkeypatch.delenv("SZPONTNET_MCAST_PORT", raising=False)
    monkeypatch.setenv("DIPLOMAT_MESH_MCAST_PORT", "41999")
    assert config.protocol()["multicastPort"] == 41999

    monkeypatch.delenv("SZPONTNET_DIR", raising=False)
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", "/var/tmp/old-spelling")
    assert str(identity.mesh_dir()) == "/var/tmp/old-spelling"


# ---- the namespace, as a property of the source ---------------------------


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _env_reads(path: Path) -> list[str]:
    """Every environment lookup in a module, as the literal key it asks for.

    All three spellings, because a scan that knows only the common one reports a
    clean module and means "I did not look": ``os.environ.get("X")``,
    ``os.environ["X"]``, and ``os.getenv("X")``.
    """
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            if called.endswith("environ.get") or called.endswith("getenv"):
                key = _literal(node.args[0]) if node.args else None
                if key is not None:
                    out.append(key)
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
            key = _literal(node.slice)
            if key is not None:
                out.append(key)
    return out


MODULES = sorted(p.name for p in PACKAGE.glob("*.py"))


@pytest.mark.parametrize("name", MODULES)
def test_no_module_reads_the_environment_behind_the_accessor(name):
    """``env.get`` is where the prefix and the fallback live, so a direct
    ``os.environ`` read is a knob that answers to neither: spelled by hand, and
    invisible to anyone still exporting the old name.

    ``PYTHONPATH`` and ``HOME`` are exempt — they are the operating system's, not
    ours, and prefixing them would be nonsense."""
    allowed = {"PYTHONPATH", "HOME"}
    reads = [k for k in _env_reads(PACKAGE / name) if k not in allowed]
    assert reads == [], f"{name} reads {reads} directly instead of through env.get"


def test_the_source_scan_would_actually_catch_one(tmp_path):
    """Anti-vacuity: the check above passes on a package that reads no environment
    at all, which is also what it would report if the scan simply never matched."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os\n"
        "a = os.environ.get('SZPONTNET_SECRET')\n"
        "b = os.environ['SZPONTNET_DIR']\n"
        "c = os.getenv('SZPONTNET_TIER')\n"
        "d = os.environ.get('HOME')\n",
        encoding="utf-8")
    assert sorted(_env_reads(probe)) == [
        "HOME", "SZPONTNET_DIR", "SZPONTNET_SECRET", "SZPONTNET_TIER"]
