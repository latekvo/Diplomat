"""The run book's on-disk format, written by one language and read by the other.

``~/.diplomat/agents/runs.json`` is read and written by BOTH front-ends, and read by
the mesh node deciding whether this machine has room. That makes its field names a
cross-language contract rather than an implementation detail of either side.

A drift here fails silently in the worst possible way: nothing errors, the other side
simply reads a run with no label, no source and no ledger key — which is exactly what
"this applet has forgotten about that agent" looks like. So: the book is written by
each side and read by the other, field for field.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from diplomat_runtime import agentregistry as R
from diplomat_runtime import atomicjson, completion
from diplomat_runtime import agentstate as A

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)")


def _records() -> list[A.RunRecord]:
    """One record per shape the book has to carry, with every field populated —
    a fixture of defaults would agree across a drift in any field it left empty."""
    return [
        A.RunRecord(run_id="1786000000-aaaaaaaa", dispatched_at=1786000000.5,
                    pr_number=337, pr_url="https://github.com/o/r/pull/337",
                    kind="review", label="Auto · Review-req · #337 (@octocat)",
                    source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL,
                    ledger_key="review:github.com/o/r#337@beef", pid=4242,
                    tty="pts/3", quiet_digest="9f86d081884c7d65",
                    quiet_since=1786000020.5, reap_refused_at=1786000040.25),
        A.RunRecord(run_id="1786000001-bbbbbbbb", dispatched_at=1786000001.25,
                    pr_number=508, pr_url="https://github.com/o/r/pull/508",
                    kind="conflicts", label="Resolve · #508",
                    source=A.SOURCE_PANEL, placement=A.PLACEMENT_MESH_PEER,
                    node="brick", work_key="conflicts:github.com/o/r#508@cafe",
                    ledger_key="conflicts:github.com/o/r#508@cafe",
                    claim_seen_at=1786000030.75),
        A.RunRecord(run_id="1786000002-cccccccc", dispatched_at=1786000002.0,
                    pr_number=None, kind="", label="", source=A.SOURCE_AUTO,
                    placement=A.PLACEMENT_MESH_HERE, pid=None, tty=""),
        # A run nobody dispatched. Both applets keep one in the book — the stillness
        # backstop measures a screen against the last one seen, and a record re-derived
        # every tick remembers none — so a field only one side wrote here would restart
        # that clock on every hand-over, and would still survive both round-trips below
        # (each reads its own omission back as the default).
        A.RunRecord(run_id="untracked:404", dispatched_at=1786000003.0, pr_number=404,
                    source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL, tty="pts/8",
                    untracked=True),
        # The agent an ended run left at its prompt. A side that dropped `released` would
        # read it back as an untracked run holding a bay and its PR.
        A.RunRecord(run_id="untracked:405", dispatched_at=1786000004.0, pr_number=405,
                    source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL, pid=4343,
                    tty="pts/9", untracked=True, released=True),
    ]


def _swift(payload: dict) -> list[dict]:
    proc = subprocess.run([CORE_BIN, "agent-registry"],
                          input=json.dumps(payload).encode("utf-8"),
                          capture_output=True, timeout=60, check=False,
                          env={**os.environ})
    assert proc.returncode == 0, \
        f"diplomat-core agent-registry failed: {proc.stderr.decode('utf-8', 'replace')}"
    return json.loads(proc.stdout)["runs"]


def test_swift_reads_every_field_python_wrote():
    """The direction that matters on a machine running the Linux applet: a record it
    wrote must still be a whole record to the other front-end."""
    R.save(_records())
    got = _swift({"mode": "read"})
    assert [A.RunRecord.from_json(r) for r in got] == _records()


def test_python_reads_every_field_swift_wrote():
    """And back the other way."""
    _swift({"mode": "write", "runs": [r.to_json() for r in _records()]})
    assert R.load() == _records()


def test_the_two_write_the_same_fields():
    """Not merely mutually readable — the same book. A field one side omits entirely
    would survive both round-trips above (each reads back its own omission as a default)
    and only show up here.

    Parsed, not compared byte for byte: the two encoders order and escape keys their own
    way (Swift sorts them and escapes ``/``), and nothing reads this file as bytes.
    """
    R.save(_records())
    python_book = json.loads(R.runs_path().read_text())
    _swift({"mode": "write", "runs": [r.to_json() for r in _records()]})
    swift_book = json.loads(R.runs_path().read_text())
    assert python_book == swift_book


def test_the_fixture_leaves_no_field_at_its_default():
    """Anti-vacuity: a field this fixture never populates is a field the diff above
    cannot see drift in."""
    populated = set()
    for r in _records():
        for key, value in r.to_json().items():
            if value not in (None, "", 0, 0.0, False):
                populated.add(key)
    missing = set(_records()[0].to_json()) - populated
    assert not missing, f"never exercised by the fixture: {sorted(missing)}"


def test_a_record_with_unusable_fields_reads_the_same_on_both_sides():
    """A hand edit or a foreign writer can leave one field in a shape neither encoder
    emits. Both read the same record back - a default per field - rather than one
    side dropping the record or raising out of its poll. The run id is the one field
    with no default, being the record's identity: a record whose id is not a string
    is dropped by both."""
    from diplomat_runtime import atomicjson
    atomicjson.write_atomic(R.runs_path(), {"version": R.SCHEMA_VERSION, "runs": [
        {"runId": "r1", "dispatchedAt": None, "pid": "x", "prNumber": "7",
         "claimSeenAt": [], "quietSince": True, "reapRefusedAt": "soon"},
        {"runId": "r2", "tty": 5, "workKey": ["x"], "label": None, "placement": 1,
         "source": False, "kind": {}, "quietDigest": 0},
        {"runId": "r3", "placement": "elsewhere"},
        {"runId": 7, "tty": "pts/7"}]})
    ours = [r.to_json() for r in R.load()]
    assert ours == _swift({"mode": "read"})
    assert [r["runId"] for r in ours] == ["r1", "r2", "r3"]
    assert ours[0]["dispatchedAt"] == 0 and ours[0]["pid"] is None
    assert ours[0]["prNumber"] is None and ours[0]["quietSince"] is None
    assert (ours[1]["tty"], ours[1]["workKey"], ours[1]["label"], ours[1]["kind"],
            ours[1]["quietDigest"]) == ("", "", "", "", "")
    assert (ours[1]["placement"], ours[1]["source"]) == ("local", "auto")
    assert ours[2]["placement"] == "local"


#: Integers no field may hold. The parsers diverge on the first two before either
#: decoder runs: ``NSNumber.intValue`` saturates on ``1e300`` and wraps on a 20-digit
#: literal, where Python reads a 301-digit int and ``10**20`` out of the same bytes.
#: ``1e19`` both parse faithfully, and it sits between Int64.max and the next power
#: of ten: the one value that tells the bound apart from a looser one.
WIDE = ["1e300", "99999999999999999999", "1e19"]


@pytest.mark.parametrize("wide", WIDE)
def test_a_number_the_two_parsers_disagree_on_is_unusable_on_both(wide):
    """An integer field is usable only inside Int64 - and a finite double stays
    what it is."""
    R.runs_path().parent.mkdir(parents=True, exist_ok=True)
    R.runs_path().write_text(
        f'{{"version": {R.SCHEMA_VERSION}, "runs": [{{"runId": "r1", '
        f'"claimSeenAt": 1e300, "pid": {wide}, "prNumber": {wide}}}]}}')
    ours = [r.to_json() for r in R.load()]
    assert ours == _swift({"mode": "read"})
    assert ours[0]["pid"] is None and ours[0]["prNumber"] is None
    assert ours[0]["claimSeenAt"] == 1e300


@pytest.mark.parametrize("wide", WIDE)
def test_the_write_mode_holds_the_same_bound_as_the_book(wide):
    """``mode: write`` decodes its payload by a hand of its own, and nothing the Linux
    applet hands it carries a number it did not normalize first - so the bound above
    is pinned here on a payload built raw."""
    raw = json.loads(f'{{"runId": "r1", "claimSeenAt": 1e300, "quietSince": 1e300, '
                     f'"pid": {wide}, "prNumber": {wide}}}')
    _swift({"mode": "write", "runs": [raw]})
    ours = [r.to_json() for r in R.load()]
    assert ours == [A.RunRecord.from_json(raw).to_json()]
    assert ours[0]["pid"] is None and ours[0]["prNumber"] is None
    assert ours[0]["claimSeenAt"] == ours[0]["quietSince"] == 1e300


def test_an_infinity_reaches_no_record_on_either_side():
    """Darwin's ``JSONSerialization`` parses ``-1e999`` to ``-inf`` - bare, a quiet
    clock past every timeout, and an uncatchable exception at the next save - while
    corelibs and :mod:`jsoninput` refuse the document. So on Linux neither side has a
    book, and on Darwin the Swift side alone keeps the record, every field that held
    one at its default. Text, because ``json.dumps`` cannot spell ``-1e999``."""
    R.runs_path().parent.mkdir(parents=True, exist_ok=True)
    R.runs_path().write_text(
        f'{{"version": {R.SCHEMA_VERSION}, "runs": [{{"runId": "r1", '
        f'"dispatchedAt": -1e999, "quietSince": -1e999}}]}}')
    assert R.load() == []
    swift = [(r["runId"], r["dispatchedAt"], r["quietSince"])
             for r in _swift({"mode": "read"})]
    assert swift == ([("r1", 0, None)] if sys.platform == "darwin" else [])


def test_a_schema_the_other_side_does_not_know_is_ignored_by_both():
    """Both must refuse a book from the future rather than misread it — an applet
    acting on records whose fields it does not understand is worse than one that has
    forgotten, because the process scan covers forgetting."""
    atomicjson.write_atomic(R.runs_path(),
                            {"version": R.SCHEMA_VERSION + 99,
                             "runs": [r.to_json() for r in _records()]})
    assert R.load() == []
    assert _swift({"mode": "read"}) == []


# MARK: - The hooks both sides stage


def _swift_hooks(activity: str, done: str | None) -> dict:
    proc = subprocess.run([CORE_BIN, "agent-registry"],
                          input=json.dumps({"mode": "hooks", "activityPath": activity,
                                            "donePath": done}).encode("utf-8"),
                          capture_output=True, timeout=60, check=False,
                          env={**os.environ})
    assert proc.returncode == 0, \
        f"diplomat-core agent-registry failed: {proc.stderr.decode('utf-8', 'replace')}"
    return json.loads(proc.stdout)["settings"]


@pytest.mark.parametrize("done", [None, "/home/a b/.diplomat/agents/r/done"])
def test_both_sides_stage_the_same_hook_commands(done):
    """The settings are shell commands that WRITE the activity file, and both sides
    READ it — a run this applet spawned is resolved by the other one after a hand-over.
    One byte of drift is a run whose turns are reported differently depending on which
    applet happened to spawn it, and nothing about that fails loudly.

    Compared as parsed JSON so key order is free, but the commands themselves are
    string values and so are compared byte for byte, quoting and all.
    """
    activity = "/home/a b/.diplomat/agents/r/activity"
    assert _swift_hooks(activity, done) == completion.hook_settings(activity, done)


# MARK: - A hand-edited book


#: One whole record as JSON, for the scenarios below to spoil one field of.
_WHOLE = _records()[0].to_json()

#: (field, value a hand edit or a torn write left there, what both sides must read).
#: `untracked: 1` is the one that matters: `JSONSerialization` bridges a JSON number to
#: `NSNumber`, and `NSNumber(1) as? Bool` is `true` on Darwin and corelibs alike, so a
#: Swift reader on plain `JSONSerialization` takes a dispatched run for an untracked one,
#: which the run deadline never looks at. The rest is the same rule in every direction:
#: a value of the wrong JSON type is the field's default, never a coercion of it.
_HAND_EDITED = [
    ("untracked", 1, False),
    ("untracked", 1.0, False),
    ("untracked", "true", False),
    ("untracked", True, True),
    ("dispatchedAt", True, 0.0),
    ("dispatchedAt", "1786000000", 0.0),
    ("prNumber", True, None),
    ("prNumber", "337", None),
    ("prNumber", 337.9, 337),
    ("pid", True, None),
    ("pid", 4242.5, 4242),
    ("claimSeenAt", False, None),
    ("quietSince", "1786000020", None),
    ("reapRefusedAt", True, None),
    ("label", 7, ""),
    ("tty", True, ""),
    ("source", 1, A.SOURCE_AUTO),
    ("placement", "moon", A.PLACEMENT_LOCAL),
    ("placement", True, A.PLACEMENT_LOCAL),
]


def _write_book(book: dict) -> None:
    atomicjson.write_atomic(R.runs_path(), book)


@pytest.mark.parametrize("key,value,expected", _HAND_EDITED,
                         ids=[f"{k}={v!r}" for k, v, _ in _HAND_EDITED])
def test_a_hand_edited_field_reads_the_same_on_both_sides(key, value, expected):
    _write_book({"version": R.SCHEMA_VERSION, "runs": [{**_WHOLE, key: value}]})
    python = [r.to_json() for r in R.load()]
    swift = [A.RunRecord.from_json(r).to_json() for r in _swift({"mode": "read"})]
    assert python == swift
    got = python[0][key]
    # `type` too: `True == 1`, so equality alone cannot tell a flag from a number.
    assert (got, type(got)) == (expected, type(expected))


def test_the_real_values_survive_the_strict_readers():
    """Anti-vacuity for the table above: readers that dropped every field would agree
    with each other just as well."""
    _write_book({"version": R.SCHEMA_VERSION, "runs": [_WHOLE]})
    assert R.load() == [_records()[0]]
    assert [A.RunRecord.from_json(r) for r in _swift({"mode": "read"})] == [_records()[0]]


#: Whole-book spoils: (name, book, the run ids both sides must read out of it).
_HAND_EDITED_BOOKS = [
    ("a boolean version", {"version": True, "runs": [_WHOLE]}, []),
    ("a run id that is not a string",
     {"version": R.SCHEMA_VERSION, "runs": [{**_WHOLE, "runId": 7}]}, []),
    ("one entry that is not a record",
     {"version": R.SCHEMA_VERSION, "runs": [_WHOLE, 5]}, [_WHOLE["runId"]]),
]


@pytest.mark.parametrize("book,expected", [(b, e) for _, b, e in _HAND_EDITED_BOOKS],
                         ids=[n for n, _, _ in _HAND_EDITED_BOOKS])
def test_a_hand_edited_book_reads_the_same_on_both_sides(book, expected):
    _write_book(book)
    assert [r.run_id for r in R.load()] == expected
    assert [r["runId"] for r in _swift({"mode": "read"})] == expected


#: Spoils at the parser: (name, the book's bytes, the run ids both sides must read out
#: of it). `json` takes every one of the refused ones and `JSONSerialization` none, so
#: each is a book only one front-end could see runs in, or a raise out of the read.
_HAND_EDITED_TEXT = [
    ("Infinity", b'{"version": 1, "runs": [{"runId": "r", "pid": Infinity}]}', []),
    ("NaN", b'{"version": 1, "runs": [{"runId": "r", "dispatchedAt": NaN}]}', []),
    ("1e400", b'{"version": 1, "runs": [{"runId": "r", "dispatchedAt": 1e400}]}', []),
    ("a 400-digit integer",
     b'{"version": 1, "runs": [{"runId": "r", "claimSeenAt": 1%s}]}' % (b"0" * 400), []),
    ("a lone surrogate",
     b'{"version": 1, "runs": [{"runId": "r", "label": "\\ud800"}]}', []),
    ("UTF-16", '{"version": 1, "runs": [{"runId": "r"}]}'.encode("utf-16"), []),
    ("a byte-order mark", b'\xef\xbb\xbf{"version": 1, "runs": [{"runId": "r"}]}', []),
    ("a surrogate pair",
     b'{"version": 1, "runs": [{"runId": "r", "label": "\\ud83d\\ude00"}]}', ["r"]),
]


@pytest.mark.parametrize("body,expected", [(b, e) for _, b, e in _HAND_EDITED_TEXT],
                         ids=[n for n, _, _ in _HAND_EDITED_TEXT])
def test_a_book_one_parser_refuses_is_refused_by_both(body, expected):
    R.runs_path().parent.mkdir(parents=True, exist_ok=True)
    R.runs_path().write_bytes(body)
    assert [r.run_id for r in R.load()] == expected
    assert [r["runId"] for r in _swift({"mode": "read"})] == expected


@pytest.mark.parametrize("first,second", [("true", "1"), ("1", "true")])
def test_a_duplicated_key_keeps_its_first_value_on_both_sides(first, second):
    """`json` keeps the last; `JSONSerialization` on Darwin keeps the first."""
    R.runs_path().parent.mkdir(parents=True, exist_ok=True)
    R.runs_path().write_text('{"version": 1, "runs": [{"runId": "r", '
                             f'"untracked": {first}, "untracked": {second}}}]}}')
    expected = [first == "true"]
    assert [r.untracked for r in R.load()] == expected
    assert [r.get("untracked", False) for r in _swift({"mode": "read"})] == expected
