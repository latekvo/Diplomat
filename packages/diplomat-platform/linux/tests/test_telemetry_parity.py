"""Cross-platform parity for the telemetry arithmetic.

``DiplomatCore.Telemetry`` (diplomat-core/Sources/DiplomatCore/Telemetry.swift) and
``diplomat_app.telemetry`` are two implementations of the same maths, and both draw a
screen the operator reads as fact: what share of a rate-limit window a task costs,
how wide the confidence interval on that is, how much work was owed a fortnight ago.
Neither can delegate to the other — the Linux screen repaints on a range flip and a
shell-out per repaint is not an option — so the only thing standing between the two
is a test that folds one ledger through both and diffs every field.

The comparison is exact ``==`` on the whole payload, floats included. Both sides
round to 6 decimal places before printing precisely so that is possible: a
difference that survives that is a difference in the arithmetic, not in how the two
languages format a double.

The fixture is a hand-written ledger rather than a generated one, so each line is
there for a reason — a retry, a reset gap between two quota samples, a task that ran
on a peer, a bad line, a task that was cleared before anyone started it. Every one
of them is a case where the two implementations could plausibly disagree.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from diplomat_app import telemetry

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")

pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)",
)

#: A fixed instant, so neither side reads its own clock. Sits at a whole second so a
#: float round-trip through JSON cannot land the two on different sides of a step
#: boundary in the pending series.
NOW = 1_785_000_000.0
DAY = 86_400.0

DAYS = 14.0
STEPS = 56
BINS = 12
Z = 1.96


def _ledger_lines() -> list[str]:
    """One ledger exercising every branch both implementations have."""
    events: list[dict] = []

    # --- quota samples -------------------------------------------------------
    # Four intervals. Two are ordinary burn-downs and price the window; the third
    # spans a RESET (session left goes UP), which must be skipped by both sides or
    # the window comes out negative; the fourth has no quota reading at all, which
    # still carries token counters for the repo/other split.
    events += [
        # OUTSIDE the range, and the token counters have already moved by the time
        # the range opens. The split is measured from here, so an implementation
        # that baselines on the first sample INSIDE the range instead reports less.
        {"at": NOW - 20 * DAY, "ev": "sample", "sessionLeft": None, "weekLeft": None,
         "repoTokens": 0.0, "otherTokens": 0.0},
        {"at": NOW - 5 * DAY, "ev": "sample", "sessionLeft": 1.0, "weekLeft": 1.0,
         "repoTokens": 250_000.0, "otherTokens": 100_000.0},
        {"at": NOW - 5 * DAY + 3600, "ev": "sample", "sessionLeft": 0.9,
         "weekLeft": 0.98, "repoTokens": 400_000.0, "otherTokens": 200_000.0},
        {"at": NOW - 5 * DAY + 7200, "ev": "sample", "sessionLeft": 0.75,
         "weekLeft": 0.95, "repoTokens": 1_000_000.0, "otherTokens": 500_000.0},
        # The 5-hour window rolled: more left than before, so this interval prices
        # nothing.
        {"at": NOW - 5 * DAY + 10800, "ev": "sample", "sessionLeft": 1.0,
         "weekLeft": 0.94, "repoTokens": 1_200_000.0, "otherTokens": 600_000.0},
        {"at": NOW - 4 * DAY, "ev": "sample", "sessionLeft": 0.5, "weekLeft": 0.9,
         "repoTokens": 4_200_000.0, "otherTokens": 1_800_000.0},
        {"at": NOW - 2 * DAY, "ev": "sample", "sessionLeft": 0.2, "weekLeft": 0.8,
         "repoTokens": 5_000_000.0, "otherTokens": 2_000_000.0},
        # The NEWEST sample carries no quota reading (the probe was offline, or
        # Claude Code was logged out). Deliberately last: the "what is left right
        # now" figures must fall back to the newest reading that carried a value,
        # and the chart must break its line here rather than plunge to zero.
        {"at": NOW - 1 * DAY, "ev": "sample", "sessionLeft": None, "weekLeft": None,
         "repoTokens": 6_000_000.0, "otherTokens": 2_400_000.0},
    ]

    # --- work ----------------------------------------------------------------
    # An ordinary review: queued, started a while later, finished with a cost.
    events += [
        {"at": NOW - 10 * DAY, "ev": "queued", "key": "review:h/o/r#1@aa",
         "duty": "review", "pr": 1},
        {"at": NOW - 10 * DAY + 900, "ev": "started", "key": "review:h/o/r#1@aa",
         "remote": False, "attempt": 1},
        {"at": NOW - 10 * DAY + 3000, "ev": "done", "key": "review:h/o/r#1@aa",
         "tokens": 120_000.0},
    ]
    # A retry: two `started` events for one key. First-wins, so the wait is measured
    # to the FIRST start and the second attempt must not move it.
    events += [
        {"at": NOW - 9 * DAY, "ev": "queued", "key": "review:h/o/r#2@bb",
         "duty": "review", "pr": 2},
        {"at": NOW - 9 * DAY + 600, "ev": "started", "key": "review:h/o/r#2@bb",
         "remote": False, "attempt": 1},
        {"at": NOW - 9 * DAY + 9000, "ev": "started", "key": "review:h/o/r#2@bb",
         "remote": False, "attempt": 2},
        {"at": NOW - 9 * DAY + 12000, "ev": "done", "key": "review:h/o/r#2@bb",
         "tokens": 260_000.0},
    ]
    # Ran on a mesh peer: started but never finished here, and its cost belongs to
    # the peer's quota — so it counts as work started and nothing else.
    events += [
        {"at": NOW - 8 * DAY, "ev": "queued", "key": "conflicts:h/o/r#3@cc",
         "duty": "conflicts", "pr": 3},
        {"at": NOW - 8 * DAY + 120, "ev": "started", "key": "conflicts:h/o/r#3@cc",
         "remote": True, "attempt": 1},
    ]
    # Finished with NO token attribution (the applet restarted mid-agent) — the
    # unattributed count, and a run time that still measures.
    events += [
        {"at": NOW - 7 * DAY, "ev": "queued", "key": "conflicts:h/o/r#4@dd",
         "duty": "conflicts", "pr": 4},
        {"at": NOW - 7 * DAY + 300, "ev": "started", "key": "conflicts:h/o/r#4@dd",
         "remote": False, "attempt": 1},
        {"at": NOW - 7 * DAY + 1500, "ev": "done", "key": "conflicts:h/o/r#4@dd"},
    ]
    # Finished under a foreign runner: billed by whichever provider OpenCode is logged
    # into, so its tokens are a task cost like any other and are not a share of the
    # Anthropic window. The figure is deliberately enormous — counted against the
    # window it would move every moment of the distribution, so a side that keeps it
    # cannot match one that drops it.
    events += [
        {"at": NOW - 5.5 * DAY, "ev": "queued", "key": "review:h/o/r#26@kk",
         "duty": "review", "pr": 26},
        {"at": NOW - 5.5 * DAY + 200, "ev": "started", "key": "review:h/o/r#26@kk",
         "remote": False, "attempt": 1},
        {"at": NOW - 5.5 * DAY + 2600, "ev": "done", "key": "review:h/o/r#26@kk",
         "tokens": 9_000_000.0, "runner": "opencode"},
    ]
    # The same field naming Claude Code explicitly, which every run does from here on:
    # it is the account the window belongs to, so this one counts. A blank and this
    # have to read alike, and only a case that spells it out can say so.
    events += [
        {"at": NOW - 5.4 * DAY, "ev": "queued", "key": "review:h/o/r#27@ll",
         "duty": "review", "pr": 27},
        {"at": NOW - 5.4 * DAY + 200, "ev": "started", "key": "review:h/o/r#27@ll",
         "remote": False, "attempt": 1},
        {"at": NOW - 5.4 * DAY + 2600, "ev": "done", "key": "review:h/o/r#27@ll",
         "tokens": 175_000.0, "runner": "claude"},
    ]
    # Owed, then cleared before anyone took it (the reviewer resolved it themselves):
    # pending for a while, then not, and never a run.
    events += [
        {"at": NOW - 6 * DAY, "ev": "queued", "key": "review:h/o/r#5@ee",
         "duty": "review", "pr": 5},
        {"at": NOW - 6 * DAY + 4 * 3600, "ev": "cleared", "key": "review:h/o/r#5@ee"},
    ]
    # Still owed right now — the series has to end above zero.
    events += [
        {"at": NOW - 3600, "ev": "queued", "key": "review:h/o/r#6@ff",
         "duty": "review", "pr": 6},
        {"at": NOW - 1800, "ev": "queued", "key": "conflicts:h/o/r#7@gg",
         "duty": "conflicts", "pr": 7},
    ]
    # Older than the 14-day lookback: folded, but outside every range figure.
    events += [
        {"at": NOW - 40 * DAY, "ev": "queued", "key": "review:h/o/r#8@hh",
         "duty": "review", "pr": 8},
        {"at": NOW - 40 * DAY + 60, "ev": "started", "key": "review:h/o/r#8@hh",
         "remote": False, "attempt": 1},
        {"at": NOW - 40 * DAY + 900, "ev": "done", "key": "review:h/o/r#8@hh",
         "tokens": 90_000.0},
    ]
    # Enough finished, costed local tasks that the histogram has something to bin
    # and the confidence interval is not degenerate.
    for i in range(9, 25):
        key = f"review:h/o/r#{i}@{i:02x}"
        base = NOW - (13 - i * 0.4) * DAY
        events += [
            {"at": base, "ev": "queued", "key": key, "duty": "review", "pr": i},
            {"at": base + 60 * i, "ev": "started", "key": key, "remote": False,
             "attempt": 1},
            {"at": base + 60 * i + 400 + 37 * i, "ev": "done", "key": key,
             "tokens": 60_000.0 + 23_000.0 * (i % 7)},
        ]

    lines = [json.dumps(e, sort_keys=True) for e in events]
    # Junk both sides must skip identically: not JSON, JSON that isn't an object,
    # an unknown verb, an event with no `at`, and a work event with no key.
    lines += [
        "{not json",
        "[1, 2, 3]",
        json.dumps({"at": NOW, "ev": "teleported", "key": "review:h/o/r#99@zz"}),
        json.dumps({"ev": "queued", "key": "review:h/o/r#98@yy", "duty": "review"}),
        json.dumps({"at": NOW, "ev": "queued", "duty": "review", "pr": 97}),
        "",
    ]
    return lines


def _swift(lines: list[str]) -> dict:
    payload = {"now": NOW, "days": DAYS, "steps": STEPS, "bins": BINS, "z": Z,
               "lines": lines}
    proc = subprocess.run(
        [CORE_BIN, "telemetry"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, (
        f"diplomat-core telemetry failed: {proc.stderr.decode('utf-8', 'replace')}"
    )
    return json.loads(proc.stdout)


def _python(lines: list[str]) -> dict:
    ledger = telemetry.fold(lines)
    summary = telemetry.summarize(ledger, now=NOW, days=DAYS, steps=STEPS,
                                  bin_count=BINS, z=Z)
    return json.loads(json.dumps(telemetry.parity_payload(ledger, summary)))


@pytest.fixture(scope="module")
def both():
    lines = _ledger_lines()
    return _swift(lines), _python(lines)


def test_the_whole_payload_matches(both):
    """One assertion over every number and every formatted string. A failure means
    the two screens would show the operator different facts about the same ledger."""
    swift, python = both
    assert python == swift, (
        "the Swift core and the Linux applet disagree about the same ledger\n"
        f"  swift:  {json.dumps(swift, indent=2, sort_keys=True)}\n"
        f"  python: {json.dumps(python, indent=2, sort_keys=True)}"
    )


def test_the_fixture_exercises_every_figure(both):
    """A ledger that folded to nothing would make the comparison above vacuous —
    two empty payloads match. Each assertion here names a figure on the screen that
    the fixture must actually populate."""
    _swift, p = both
    assert p["sessionLimitTokens"], "the window was never priced — calibration untested"
    assert p["weekLimitTokens"], "the 7-day window was never priced"
    assert p["perTask"]["count"] >= 5, "too few costed tasks to shape a distribution"
    assert p["perTask"]["ciHigh"] > p["perTask"]["ciLow"], "degenerate interval"
    assert any(b["count"] for b in p["perTask"]["bins"]), "empty histogram"
    assert any(v > 0 for v in p["perTask"]["curve"]), "flat fitted normal"
    assert p["avgRunSecs"] > 0 and p["avgWaitSecs"] > 0
    assert p["remoteCount"] == 1, "the mesh-placed task is missing"
    assert p["unattributedCount"] == 1, "the uncosted completion is missing"
    assert {"", "claude", "opencode"} <= {t["runner"] for t in p["tasks"]}, (
        "the fixture has no foreign-runner completion beside the Anthropic ones, so "
        "an implementation that charged every runner to the same window would pass"
    )
    assert p["quota"], "no quota readings — the rate-limit chart has nothing to draw"
    assert any(q["sessionPct"] is None for q in p["quota"]), (
        "the fixture has no probe-offline gap, so an implementation that dropped the "
        "gap (or interpolated across it) would pass"
    )
    assert p["sessionLeftPct"] == 20 and p["weekLeftPct"] == 80, (
        "the headline must be the last reading that CARRIED a value, not the last "
        "sample — the fixture's newest sample is a probe-offline one"
    )
    assert p["peakReviews"] > 0 and p["peakConflicts"] > 0
    assert p["pendingReviewsNow"] > 0 and p["pendingConflictsNow"] > 0, (
        "nothing owed at `now` — the series ends flat and its tail is untested"
    )
    assert p["repoTokens"] > 0 and p["otherTokens"] > 0, "the token split is one-sided"
    assert p["repoTokens"] == 6_000_000 and p["otherTokens"] == 2_400_000, (
        "the split must run from the reading BEFORE the range to the newest one — "
        "these are the fixture's outermost counters, and anything less means the "
        "interval straddling the range boundary was dropped"
    )


def test_a_foreign_runners_task_is_priced_but_never_charged_to_the_window(both):
    """What it cost is a token count; what share of a five-hour Anthropic window it
    cost is nothing, because it never drew on one.

    The fixture's OpenCode task is the largest in the ledger by an order of magnitude,
    so counting it would move the distribution's every moment — and both sides have to
    drop it identically or the two screens report different spend for one ledger."""
    _swift, p = both
    counted = [t for t in p["tasks"]
               if t["tokens"] and not t["remote"] and t["startedAt"] is not None
               and t["startedAt"] >= NOW - DAYS * DAY and t["runner"] != "opencode"]
    foreign = [t for t in p["tasks"] if t["runner"] == "opencode"]
    assert len(foreign) == 1 and foreign[0]["tokens"] == 9_000_000
    assert p["perTask"]["count"] == len(counted), (
        "the window's percentages counted a task billed to another provider"
    )
    # It is still the same work, and the tokens-per-task figure is where it belongs.
    priced = [t["tokens"] for t in counted] + [foreign[0]["tokens"]]
    assert p["perTaskTokensMean"] == pytest.approx(sum(priced) / len(priced), abs=1e-6)


def test_a_retry_does_not_move_the_measured_wait(both):
    """First-wins on every instant is what makes a retried job read correctly: the
    wait is until work actually began, not until the attempt that stuck."""
    _swift, p = both
    task = next(t for t in p["tasks"] if t["key"] == "review:h/o/r#2@bb")
    assert task["waitSecs"] == 600, "the second `started` overwrote the first"


def test_the_junk_lines_produced_no_tasks(both):
    """Both sides skip a malformed line and keep the rest. If one of them invented a
    task out of the keyless or timestamp-less events, the counts would differ — but
    that failure is much clearer named."""
    _swift, p = both
    keys = {t["key"] for t in p["tasks"]}
    assert "review:h/o/r#99@zz" not in keys, "an unknown event verb created a task"
    assert "review:h/o/r#98@yy" not in keys, "an event with no `at` created a task"
    assert "" not in keys
