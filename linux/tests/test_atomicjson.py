"""Unit tests for the shared mesh atomic-JSON writer.

Six mesh state files (identity, peer cache, trust, bans, stats, the public
snapshot) previously each carried a byte-identical tmp-write + rename body;
they now share :func:`diplomat_app.mesh.atomicjson.write_atomic`. These tests
pin the two behaviours the call sites relied on: the write is atomic (no
lingering ``.tmp``, target replaced whole) and ``indent`` controls the on-disk
shape (snapshot + peer cache used ``indent=1``, the rest ``indent=2``), so the
files keep the exact format they had before the extraction.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diplomat_app.mesh.atomicjson import write_atomic  # noqa: E402


def test_writes_and_reads_back(tmp_path):
    p = tmp_path / "state.json"
    write_atomic(p, {"a": 1, "b": ["x", "y"]})
    assert json.loads(p.read_text()) == {"a": 1, "b": ["x", "y"]}


def test_default_indent_is_2_with_trailing_newline(tmp_path):
    p = tmp_path / "s.json"
    write_atomic(p, {"k": 1})
    assert p.read_text() == '{\n  "k": 1\n}\n'


def test_indent_1_matches_prior_snapshot_format(tmp_path):
    p = tmp_path / "s.json"
    write_atomic(p, {"k": 1}, indent=1)
    assert p.read_text() == '{\n "k": 1\n}\n'


def test_creates_parent_directories(tmp_path):
    p = tmp_path / "nested" / "deep" / "s.json"
    write_atomic(p, {"ok": True})
    assert json.loads(p.read_text()) == {"ok": True}


def test_leaves_no_tmp_file_behind(tmp_path):
    p = tmp_path / "s.json"
    write_atomic(p, {"ok": True})
    assert p.exists()
    assert not any(f.name.endswith(".tmp") for f in tmp_path.iterdir())


def test_replaces_existing_file_atomically(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("stale")
    write_atomic(p, {"fresh": 1})
    assert json.loads(p.read_text()) == {"fresh": 1}


def test_unwritable_target_is_swallowed(tmp_path):
    # A directory where the file should be — os.replace/mkdir fails; the write is
    # best-effort and must never raise into the caller (an unwritable HOME still
    # gets an in-memory identity, a rendered-but-unsaved snapshot, etc.).
    p = tmp_path / "s.json"
    p.mkdir()
    write_atomic(p, {"x": 1})  # must not raise
    assert p.is_dir()
