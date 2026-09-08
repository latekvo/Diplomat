"""Where ``gh`` is looked for, and for how long the answer is kept."""

from __future__ import annotations

import stat

import pytest

from diplomat_runtime import gh
from diplomat_runtime.gh import run as real_run  # bound before conftest swaps gh.run out


def _install(path, name):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {name}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_a_gh_that_no_longer_launches_is_looked_for_again(tmp_path, monkeypatch):
    """The path found is kept for the process, so gh moving or being uninstalled
    would otherwise fail every call until a restart: a launch failure forgets it
    and the next call looks afresh (GH.swift's `forget` is the twin)."""
    first, second = tmp_path / "one" / "gh", tmp_path / "two" / "gh"
    monkeypatch.setattr(gh, "_CANDIDATES", [str(first), str(second)])
    monkeypatch.setattr(gh, "_cached_path", None)
    _install(first, "one")
    assert real_run([]) == b"one\n"
    first.unlink()
    with pytest.raises(gh.GHError, match="could not execute gh"):
        real_run([])
    _install(second, "two")
    assert real_run([]) == b"two\n"
