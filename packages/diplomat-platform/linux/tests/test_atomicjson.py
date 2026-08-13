"""The atomic-JSON reader and writer, held to one contract across two copies.

Small state files on both sides of the library boundary go through this pair: the
node's identity, peer + onion caches, trust store, ban list, usage stats and public
snapshot (:mod:`szpontnet.atomicjson`), and the applet's shared
``~/.diplomat/config.json`` (:mod:`diplomat_runtime.atomicjson`, its own copy, because
resolving the repo the agents work in must not depend on the mesh add-on being
installed).

The write tests pin the two behaviours every call site relies on: the write is
atomic (no lingering ``.tmp``, target replaced whole) and ``indent`` controls the
on-disk shape (the snapshot and peer cache use ``indent=1``, the rest ``indent=2``),
so each file keeps its exact format.

The read tests pin the contract that makes a corrupt state file survivable — every
way a file can fail to hold a JSON object collapses to ``None``. That is the point
worth running against both copies: ``(OSError, json.JSONDecodeError)`` looks
exhaustive and is not — it misses the ``UnicodeDecodeError`` that ``read_text``
raises on a non-UTF-8 file, and a reader that catches only those two propagates a
corrupt cache out of a loader and takes a startup path down with it.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from diplomat_runtime import atomicjson as diplomat_atomicjson  # noqa: E402
from szpontnet import atomicjson as szpontnet_atomicjson  # noqa: E402


@pytest.fixture(params=[diplomat_atomicjson, szpontnet_atomicjson],
                ids=["diplomat", "szpontnet"])
def aj(request):
    return request.param


@pytest.fixture
def write_atomic(aj):
    return aj.write_atomic


@pytest.fixture
def read_object(aj):
    return aj.read_object


def test_the_two_copies_are_not_the_same_module():
    """Anti-vacuity for the parametrisation: if one ever re-exported the other,
    every case here would assert one implementation twice while the second rotted."""
    assert diplomat_atomicjson is not szpontnet_atomicjson
    assert diplomat_atomicjson.read_object is not szpontnet_atomicjson.read_object


def test_writes_and_reads_back(tmp_path, write_atomic, read_object):
    p = tmp_path / "state.json"
    write_atomic(p, {"a": 1, "b": ["x", "y"]})
    assert json.loads(p.read_text()) == {"a": 1, "b": ["x", "y"]}


def test_default_indent_is_2_with_trailing_newline(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    write_atomic(p, {"k": 1})
    assert p.read_text() == '{\n  "k": 1\n}\n'


def test_indent_1_matches_prior_snapshot_format(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    write_atomic(p, {"k": 1}, indent=1)
    assert p.read_text() == '{\n "k": 1\n}\n'


def test_creates_parent_directories(tmp_path, write_atomic, read_object):
    p = tmp_path / "nested" / "deep" / "s.json"
    write_atomic(p, {"ok": True})
    assert json.loads(p.read_text()) == {"ok": True}


def test_leaves_no_tmp_file_behind(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    write_atomic(p, {"ok": True})
    assert p.exists()
    assert not any(f.name.endswith(".tmp") for f in tmp_path.iterdir())


def test_replaces_existing_file_atomically(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    p.write_text("stale")
    write_atomic(p, {"fresh": 1})
    assert json.loads(p.read_text()) == {"fresh": 1}


def test_unwritable_target_is_swallowed(tmp_path, write_atomic, read_object):
    # A directory where the file should be — os.replace/mkdir fails; the write is
    # best-effort and must never raise into the caller (an unwritable HOME still
    # gets an in-memory identity, a rendered-but-unsaved snapshot, etc.).
    p = tmp_path / "s.json"
    p.mkdir()
    write_atomic(p, {"x": 1})  # must not raise
    assert p.is_dir()


# ---- read_object: every failure collapses to None ------------------------


def test_read_object_round_trips_a_written_file(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    write_atomic(p, {"a": 1, "b": ["x"]})
    assert read_object(p) == {"a": 1, "b": ["x"]}


def test_read_object_returns_none_for_a_missing_file(tmp_path, write_atomic, read_object):
    assert read_object(tmp_path / "never-written.json") is None


def test_read_object_returns_none_for_an_unreadable_path(
        tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    p.mkdir()  # a directory where the file should be: read_text raises OSError
    assert read_object(p) is None


def test_read_object_returns_none_for_malformed_json(tmp_path, write_atomic, read_object):
    p = tmp_path / "s.json"
    p.write_text('{"truncated": ')
    assert read_object(p) is None


def test_read_object_returns_none_for_non_utf8_bytes(tmp_path, write_atomic, read_object):
    """``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a
    ``ValueError``, *not* a ``json.JSONDecodeError``) on a corrupt file, so a reader
    that catches only ``JSONDecodeError`` propagates it out of a loader and takes a
    startup path down with it. One corrupt byte in a cache is enough."""
    p = tmp_path / "s.json"
    p.write_bytes(b'{"addr": "10.0.0.1\xff"}')
    assert read_object(p) is None


def test_read_object_returns_none_for_valid_json_that_is_not_an_object(
        tmp_path, write_atomic, read_object):
    """A bare scalar/array decodes fine but has no ``.get`` / ``.items``, so handing
    one back would crash the caller a line later - a hand-edited file is enough."""
    for body in ("[1, 2, 3]", '"a string"', "42", "null", "true"):
        p = tmp_path / "s.json"
        p.write_text(body)
        assert read_object(p) is None, body


def test_read_object_keeps_an_empty_object_distinct_from_absent(
        tmp_path, write_atomic, read_object):
    """``{}`` is a real object, not a failure — callers that want them merged
    write ``read_object(p) or {}``, and ``statefile`` relies on the distinction."""
    p = tmp_path / "s.json"
    p.write_text("{}")
    assert read_object(p) == {}
