"""Unit tests for the shared atomic-JSON reader and writer.

The app's small state files (identity, peer + onion caches, trust, bans, stats,
the public snapshot, the shared app config) each used to carry a hand-copied
body for both directions; they share
:func:`diplomat_app.mesh.atomicjson.write_atomic` and
:func:`~diplomat_app.mesh.atomicjson.read_object` now.

The write tests pin the two behaviours the call sites relied on: the write is
atomic (no lingering ``.tmp``, target replaced whole) and ``indent`` controls
the on-disk shape (snapshot + peer cache used ``indent=1``, the rest ``indent=2``),
so the files keep the exact format they had before the extraction.

The read tests pin the contract that makes a corrupt state file survivable —
every way a file can fail to hold a JSON object collapses to ``None``. The
copies had drifted on exactly that point: all but one caught
``(OSError, json.JSONDecodeError)``, which does not include the
``UnicodeDecodeError`` that ``read_text`` raises on a non-UTF-8 file, so a
corrupt cache propagated out of the loader and crashed the node's startup
instead of resetting to empty.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diplomat_app.mesh.atomicjson import read_object, write_atomic  # noqa: E402


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


# ---- read_object: every failure collapses to None ------------------------


def test_read_object_round_trips_a_written_file(tmp_path):
    p = tmp_path / "s.json"
    write_atomic(p, {"a": 1, "b": ["x"]})
    assert read_object(p) == {"a": 1, "b": ["x"]}


def test_read_object_returns_none_for_a_missing_file(tmp_path):
    assert read_object(tmp_path / "never-written.json") is None


def test_read_object_returns_none_for_an_unreadable_path(tmp_path):
    p = tmp_path / "s.json"
    p.mkdir()  # a directory where the file should be: read_text raises OSError
    assert read_object(p) is None


def test_read_object_returns_none_for_malformed_json(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"truncated": ')
    assert read_object(p) is None


def test_read_object_returns_none_for_non_utf8_bytes(tmp_path):
    """The drift that made this shared: ``read_text(encoding="utf-8")`` raises
    ``UnicodeDecodeError`` (a ``ValueError``, *not* a ``json.JSONDecodeError``) on a
    corrupt file, so the readers that caught only ``JSONDecodeError`` propagated it
    out of a loader and took the node's startup down with it."""
    p = tmp_path / "s.json"
    p.write_bytes(b'{"addr": "10.0.0.1\xff"}')
    assert read_object(p) is None


def test_read_object_returns_none_for_valid_json_that_is_not_an_object(tmp_path):
    """A bare scalar/array decodes fine but has no ``.get`` / ``.items``, which is
    how a hand-edited file used to crash a caller one line later."""
    for body in ("[1, 2, 3]", '"a string"', "42", "null", "true"):
        p = tmp_path / "s.json"
        p.write_text(body)
        assert read_object(p) is None, body


def test_read_object_keeps_an_empty_object_distinct_from_absent(tmp_path):
    """``{}`` is a real object, not a failure — callers that want them merged
    write ``read_object(p) or {}``, and ``statefile`` relies on the distinction."""
    p = tmp_path / "s.json"
    p.write_text("{}")
    assert read_object(p) == {}
