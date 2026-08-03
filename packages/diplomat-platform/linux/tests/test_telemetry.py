"""The telemetry ledger and the two gatherers that fill it.

The arithmetic over the ledger is covered by ``test_telemetry_parity.py``, which
diffs it against the Swift implementation field by field. What that test cannot
reach is everything around the maths: which events get appended and when, what a
poll does to work that stopped being owed, and how the two gatherers turn Claude
Code's own state into the numbers the ledger stores.

Those are exactly the parts where a mistake is silent — a screen drawn from a
ledger nobody wrote to looks the same as a screen drawn from a quiet fortnight.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from diplomat_app import quota, telemetry, usagescan


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A ledger in a temp dir. ``activity._dir`` is already redirected by the
    autouse fixture in conftest; this just names the resulting path."""
    telemetry._reset_cache()
    return telemetry.ledger_path()


# MARK: - Appending and reading


def test_events_round_trip_through_the_file(ledger):
    telemetry.record_queued("review:h/o/r#1@aa", "review", 1)
    telemetry.record_started("review:h/o/r#1@aa", remote=False, attempt=1)
    telemetry.record_done("review:h/o/r#1@aa", at=1_785_000_000.0, tokens=1234.0)

    folded = telemetry.load()
    assert [t.key for t in folded.tasks] == ["review:h/o/r#1@aa"]
    task = folded.tasks[0]
    assert task.duty == "review" and task.pr == 1
    assert task.queued_at and task.started_at
    assert task.done_at == 1_785_000_000.0
    assert task.tokens == 1234.0


def test_the_fold_is_recomputed_when_the_file_changes(ledger):
    telemetry.record_queued("review:h/o/r#1@aa", "review", 1)
    assert len(telemetry.load().tasks) == 1
    telemetry.record_queued("review:h/o/r#2@bb", "review", 2)
    assert len(telemetry.load().tasks) == 2, "the fold cache outlived its ledger"


def test_a_concurrent_appender_cannot_lose_a_line(ledger):
    """Two writers, interleaved. ``O_APPEND`` is what makes this safe — the applet
    and a mesh node on one home directory both append to this file."""
    for i in range(50):
        telemetry.append({"at": 1.0 + i, "ev": "queued", "key": f"review:k#{i}",
                          "duty": "review", "pr": i})
    assert len(telemetry.read_lines()) == 50


def test_rotation_keeps_what_the_longest_lookback_can_still_reach(ledger, monkeypatch):
    """The cap is a size, but the rewrite is by age: the file is the only record of
    what was owed and when, so a plain truncate would silently shorten the 60-day
    chart."""
    now = time.time()
    old = json.dumps({"at": now - 90 * 86400, "ev": "queued",
                      "key": "review:old", "duty": "review", "pr": 1})
    recent = json.dumps({"at": now - 3600, "ev": "queued",
                         "key": "review:new", "duty": "review", "pr": 2})
    with open(ledger, "w", encoding="utf-8") as fh:
        for _ in range(200):
            fh.write(old + "\n")
        fh.write(recent + "\n")
    monkeypatch.setattr(telemetry, "MAX_LEDGER_BYTES", 100)

    telemetry.record_queued("review:newer", "review", 3)

    telemetry._reset_cache()
    keys = {t.key for t in telemetry.load().tasks}
    assert "review:old" not in keys, "rotation kept events past the retention horizon"
    assert {"review:new", "review:newer"} <= keys, "rotation dropped reachable events"


def test_a_partial_tail_line_costs_only_itself(ledger):
    telemetry.record_queued("review:h/o/r#1@aa", "review", 1)
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write('{"at": 1.0, "ev": "que')   # a write caught mid-flight
    telemetry._reset_cache()
    assert [t.key for t in telemetry.load().tasks] == ["review:h/o/r#1@aa"]


# MARK: - What a poll records


def test_owed_work_is_queued_once_and_only_once(ledger):
    owed = {"review:h/o/r#1@aa": 1, "review:h/o/r#2@bb": 2}
    telemetry.observe_owed("review", "review", owed)
    telemetry.observe_owed("review", "review", owed)   # the next poll, same answer
    tasks = telemetry.load().tasks
    assert len(tasks) == 2, "a second poll re-queued work it had already seen"
    assert all(t.queued_at is not None for t in tasks)


def test_work_that_stops_being_owed_before_anyone_took_it_is_cleared(ledger):
    telemetry.observe_owed("review", "review", {"review:h/o/r#1@aa": 1})
    telemetry.observe_owed("review", "review", {})     # the reviewer resolved it
    task = telemetry.load().tasks[0]
    assert task.cleared_at is not None
    assert not task.pending(time.time()), "cleared work is still charted as a backlog"


def test_work_an_agent_took_is_never_marked_cleared(ledger):
    """A started item has an outcome of its own; calling it "cleared" as well would
    read as the monitor dropping work it actually did."""
    telemetry.observe_owed("conflicts", "conflicts", {"conflicts:h/o/r#1@aa": 1})
    telemetry.record_started("conflicts:h/o/r#1@aa")
    telemetry.observe_owed("conflicts", "conflicts", {})
    assert telemetry.load().tasks[0].cleared_at is None


def test_one_polls_sweep_does_not_clear_another_polls_work(ledger):
    """Reviews reach the ledger from two independent polls — replies owed on my own
    PRs, and reviews requested of me — and both chart as "reviews". Scoping the
    sweep by duty alone would make each poll declare the other's backlog resolved on
    every tick."""
    telemetry.observe_owed("review", "review", {"review:h/o/r#1@aa": 1})
    telemetry.observe_owed("review-reply", "review", {"review-reply:h/o/r#2@bb": 2})

    # The review-request poll runs again and still sees only its own item.
    telemetry.observe_owed("review", "review", {"review:h/o/r#1@aa": 1})

    by_key = {t.key: t for t in telemetry.load().tasks}
    assert by_key["review-reply:h/o/r#2@bb"].cleared_at is None, (
        "the review-request poll cleared the my-PRs poll's pending work"
    )
    assert by_key["review:h/o/r#1@aa"].cleared_at is None


def test_a_future_event_verb_does_not_suppress_the_queue_record(ledger):
    """The ledger is append-only and read by two platforms, so a build can meet an
    event it doesn't know. Folding that into an empty task would make the key look
    already-recorded, and the work would never be queued at all."""
    telemetry.append({"at": time.time(), "ev": "teleported", "key": "review:h/o/r#1@aa"})
    telemetry._reset_cache()
    telemetry.observe_owed("review", "review", {"review:h/o/r#1@aa": 1})
    tasks = telemetry.load().tasks
    assert len(tasks) == 1 and tasks[0].queued_at is not None


def test_sampling_is_paced_by_the_ledger_not_by_the_caller(ledger):
    """An applet that restarts every few minutes must not sample every launch — the
    pacing is the last sample's own timestamp."""
    assert telemetry.sample_due(), "an empty ledger should take its first sample"
    telemetry.record_sample(0.5, 0.9, 1000.0, 500.0)
    telemetry._reset_cache()
    assert not telemetry.sample_due()
    assert telemetry.sample_due(time.time() + telemetry.SAMPLE_INTERVAL_SECS + 1)


def test_a_completion_with_no_matching_transcript_is_still_recorded(ledger):
    """Attribution fails whenever the applet restarted mid-agent. Recording that as
    a completion with no cost keeps the run time honest and lets the screen say how
    many tasks it could not price; skipping it would hide the agent entirely."""
    telemetry.record_started("review:h/o/r#1@aa")
    telemetry.record_completion("review:h/o/r#1@aa", "a prompt no transcript holds",
                                time.time() - 60, time.time())
    task = telemetry.load().tasks[0]
    assert task.done_at is not None
    assert task.tokens is None


# MARK: - The transcript scanner


def _transcript(path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _turn(cwd: str, tokens: int) -> dict:
    return {"type": "assistant", "cwd": cwd,
            "message": {"usage": {"input_tokens": tokens, "output_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 999_999}}}


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    """A Claude-Code home with a known repo root, and the cursor beside the ledger."""
    repo = tmp_path / "argent"
    repo.mkdir()
    monkeypatch.setattr(usagescan, "repo_roots", lambda: [
        repo, repo.parent / "argent-worktrees"])
    projects = usagescan.projects_dir()
    projects.mkdir(parents=True)
    return repo, projects


def test_the_first_scan_reads_nothing(scanner):
    """A machine can hold gigabytes of transcripts, and reading them all would stall
    the poll that triggered it — for history that predates the ledger and can never
    be attributed to a task. Everything already on disk is seeded at EOF."""
    repo, projects = scanner
    _transcript(projects / "old" / "a.jsonl", [_turn(str(repo), 5000)])
    assert usagescan.totals() == usagescan.Totals(repo=0.0, other=0.0)


def test_only_appended_bytes_are_counted_on_the_next_scan(scanner):
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _transcript(path, [_turn(str(repo), 5000)])
    usagescan.totals()                                  # seeds at EOF
    _transcript(path, [_turn(str(repo), 700)])
    assert usagescan.totals().repo == 700
    assert usagescan.totals().repo == 700, "a re-scan double-counted the same turn"


def test_cache_reads_are_left_out_of_the_cost(scanner):
    """They are huge and nearly free, and counting them would swamp the signal — the
    same three fields the mesh add-on's probe sums, so a machine running SzpontNet
    prices its quota the same way."""
    repo, projects = scanner
    usagescan.totals()
    _transcript(projects / "s" / "a.jsonl", [_turn(str(repo), 100)])
    assert usagescan.totals().repo == 100


def test_the_split_follows_the_cwd_each_turn_was_run_in(scanner):
    repo, projects = scanner
    usagescan.totals()
    _transcript(projects / "s" / "a.jsonl", [
        _turn(str(repo), 100),
        _turn(str(repo / "packages" / "core"), 50),
        _turn(str(repo.parent / "argent-worktrees" / "feature"), 30),
        _turn(str(repo.parent / "something-else"), 400),
        # A sibling whose name merely STARTS with the repo's: a string-prefix test
        # would call this the same project.
        _turn(f"{repo}-old", 7),
        _turn("", 9),
    ])
    totals = usagescan.totals()
    assert totals.repo == 180, "the worktree or a subdirectory fell out of the repo"
    assert totals.other == 416


def test_a_transcript_written_mid_scan_keeps_its_partial_line(scanner):
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("")
    usagescan.totals()
    line = json.dumps(_turn(str(repo), 250))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[:20])                             # caught mid-write
    assert usagescan.totals().repo == 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[20:] + "\n")                      # the writer finished
    assert usagescan.totals().repo == 250, "the half-written turn was lost"


def test_a_truncated_transcript_is_read_from_the_start(scanner):
    """A reused session id or a rotated log leaves the cursor past the end. Skipping
    it would silently stop counting that session forever."""
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _transcript(path, [_turn(str(repo), 5000)])
    usagescan.totals()
    path.write_text("")
    _transcript(path, [_turn(str(repo), 42)])
    assert usagescan.totals().repo == 42


def test_a_resumed_session_is_not_re_read_from_zero(scanner):
    """Cursors are pruned by what is on disk, never by age: Claude Code appends to an
    old transcript when a session is resumed, and a forgotten cursor would count the
    whole file again."""
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _transcript(path, [_turn(str(repo), 5000)])
    usagescan.totals()
    _transcript(path, [_turn(str(repo), 11)])
    assert usagescan.totals().repo == 11
    os.utime(path, (time.time() - 400 * 86400, time.time() - 400 * 86400))
    _transcript(path, [_turn(str(repo), 3)])
    assert usagescan.totals().repo == 14


def test_a_deleted_transcript_drops_its_cursor(scanner):
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _transcript(path, [_turn(str(repo), 5000)])
    usagescan.totals()
    path.unlink()
    usagescan.totals()
    state = json.loads(usagescan.cursor_path().read_text(encoding="utf-8"))
    assert state["files"] == {}


# MARK: - Per-task attribution


def _session(path, prompt: str, cwd: str, turns: list[int]) -> None:
    _transcript(path, [{"type": "user", "message": {"content": prompt}}]
                + [_turn(cwd, t) for t in turns])


def test_a_task_is_costed_from_its_own_transcript(scanner):
    """Concurrency is the whole reason this exists: differencing global quota over
    one agent's lifetime charges it for every other agent running beside it."""
    repo, projects = scanner
    started = time.time() - 300
    _session(projects / "s" / "mine.jsonl", "review PR #41 please", str(repo),
             [1000, 500])
    _session(projects / "s" / "other.jsonl", "a different job entirely", str(repo),
             [9_000_000])
    assert usagescan.task_tokens("review PR #41 please", started, time.time()) == 1500


def test_a_prompt_written_as_content_blocks_still_matches(scanner):
    """Claude Code writes the opening message either way depending on how it was
    invoked; the spawn path this matches uses one of them today."""
    repo, projects = scanner
    _transcript(projects / "s" / "a.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "review PR #41 please"}]}},
        _turn(str(repo), 250),
    ])
    assert usagescan.task_tokens("review PR #41 please",
                                 time.time() - 60, time.time()) == 250


def test_a_transcript_outside_the_agents_lifetime_is_not_its_own(scanner):
    """A transcript is appended to while its agent works, so its mtime lands at or
    after the last turn — never before the agent started. Without that bound an
    identical prompt from last week would be charged to today's run."""
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _session(path, "review PR #41 please", str(repo), [250])
    stale = time.time() - 30 * 86400
    os.utime(path, (stale, stale))
    assert usagescan.task_tokens("review PR #41 please",
                                 time.time() - 60, time.time()) is None


def test_an_unmatched_prompt_reports_nothing_rather_than_zero(scanner):
    repo, projects = scanner
    _session(projects / "s" / "a.jsonl", "some other agent's prompt", str(repo), [250])
    assert usagescan.task_tokens("review PR #41 please",
                                 time.time() - 60, time.time()) is None
    assert usagescan.task_tokens("", time.time() - 60, time.time()) is None


# MARK: - The quota probe


def test_the_probe_makes_no_request_when_it_is_switched_off(monkeypatch):
    """``DIPLOMAT_QUOTA_PROBE=0`` is what keeps this suite (and CI) off the network
    and out of the operator's credentials — the conftest sets it for every test, so
    a probe that ignored it would spend a real token on a real request from here."""
    monkeypatch.setattr(quota, "_fetch", lambda: pytest.fail(
        "the probe reached the endpoint with DIPLOMAT_QUOTA_PROBE=0"))
    assert quota.fractions_left() == (None, None)


def test_the_probe_answers_when_it_is_switched_on(monkeypatch):
    """The control for the test above: same call, only the switch flipped. Without
    it, a `fractions_left` that always returned (None, None) would pass."""
    monkeypatch.setenv("DIPLOMAT_QUOTA_PROBE", "1")
    monkeypatch.setattr(quota, "_fetch", lambda: {
        "five_hour": {"utilization": 40}, "seven_day": {"utilization": 10}})
    quota._reset_cache()
    assert quota.fractions_left() == (0.6, 0.9)


@pytest.mark.parametrize("util, left", [
    (0, 1.0),
    (25, 0.75),
    (100, 0.0),
    # Utilization can exceed 100 during a burst. A negative fraction would price the
    # window backwards and show up as a negative task cost.
    (140, 0.0),
])
def test_a_windows_utilization_becomes_the_fraction_left(util, left):
    assert quota._fraction_left({"utilization": util}) == left


def test_a_body_that_is_not_a_window_prices_nothing():
    """Every one of these would otherwise become a bogus sample in the ledger, and
    the ledger is what the whole screen is computed from."""
    assert quota._fraction_left(None) is None
    assert quota._fraction_left({}) is None
    assert quota._fraction_left({"utilization": "80"}) is None
    assert quota._fraction_left({"utilization": True}) is None
