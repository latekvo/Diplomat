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
import shlex
import time

import pytest

from diplomat_runtime import quota, telemetry, usagescan


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


def test_a_retried_task_is_priced_by_whichever_attempt_could_be_attributed(ledger):
    """Work that is still owed after an agent finishes is re-run, and that appends a
    SECOND completion under the same key — only one of which need be attributable,
    since an applet that restarted mid-agent loses that run's prompt. Priced
    first-wins, such a task counts as unattributed for good however often it is
    re-run, and the whole retry chain drops out of the spread."""
    unpriced = "conflicts:h/o/r#7@aa"
    telemetry.record_done(unpriced, at=1_785_000_000.0, tokens=None)
    telemetry.record_done(unpriced, at=1_785_009_000.0, tokens=4321.0)

    # A transcript that could not be read prices at zero, which is no more a
    # measurement than a missing one is.
    empty = "conflicts:h/o/r#8@bb"
    telemetry.record_done(empty, at=1_785_000_000.0, tokens=0.0)
    telemetry.record_done(empty, at=1_785_009_000.0, tokens=8765.0)

    priced = "conflicts:h/o/r#9@cc"
    telemetry.record_done(priced, at=1_785_000_000.0, tokens=1000.0)
    telemetry.record_done(priced, at=1_785_009_000.0, tokens=9999.0)

    # The runner is what says whether a price came out of the Anthropic window, so it
    # has to travel with the price it belongs to.
    foreign = "conflicts:h/o/r#10@dd"
    telemetry.record_done(foreign, at=1_785_000_000.0, tokens=None, agent_runner="claude")
    telemetry.record_done(foreign, at=1_785_009_000.0, tokens=5000.0,
                          agent_runner="opencode")

    tasks = {t.key: t for t in telemetry.load().tasks}
    assert tasks[unpriced].tokens == 4321.0, "the later attempt's price was discarded"
    assert tasks[empty].tokens == 8765.0, "a zero read as a price"
    assert tasks[priced].tokens == 1000.0, "a priced task was re-priced by its retry"
    assert tasks[unpriced].done_at == 1_785_000_000.0, "the first completion is when it finished"
    assert not tasks[foreign].anthropic, "a foreign runner's price was charged to the window"


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
    now = time.time()
    telemetry.record_started("review:h/o/r#1@aa")
    telemetry.record_completion("review:h/o/r#1@aa", "a prompt no transcript holds",
                                now - 60, None, now)
    task = telemetry.load().tasks[0]
    assert task.done_at == now, "with neither sentinel nor transcript, the poll is all"
    assert task.tokens is None


# MARK: - What a run is priced from, and when it is read


def _stub_opencode(tmp_path, monkeypatch, body: str) -> None:
    """A fake ``opencode`` on PATH — the exporter is reached by name, so a stub that
    answers to that name is what proves the argv is right as well as the parsing."""
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(body, encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ["PATH"])


EXPORTED = {"info": {"id": "ses_ours"}, "messages": [
    {"info": {"role": "user"}},
    {"info": {"role": "assistant", "cost": 0.038403,
              "tokens": {"total": 30000, "input": 3, "output": 84, "reasoning": 9,
                         "cache": {"read": 29000, "write": 40}}}},
    {"info": {"role": "assistant", "cost": 0.0032179,
              "tokens": {"total": 30505, "input": 7, "output": 8, "reasoning": 0,
                         "cache": {"read": 30384, "write": 106}}}},
]}


def test_an_opencode_run_is_priced_from_its_whole_session(tmp_path, monkeypatch):
    """Every message, not the last one: OpenCode reports a turn's spend per message,
    so a run's is the sum. Reading only the last would price a two-hour review at
    whatever its closing sentence cost.

    Input + output + cache WRITES, never cache reads — the same three the Claude Code
    scan sums. This session reports 60505 tokens of ``total``, almost all of it cache
    reads; counting those would make the per-task figure on the telemetry screen mean
    one thing for one runner and another for the other.

    The same numbers are asserted in ``DiplomatCoreSmoke``: both front-ends price runs
    into one ledger, and a machine can hand a mesh job to the other platform."""
    _stub_opencode(tmp_path, monkeypatch,
                   "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(EXPORTED) + "\nJSON\n")
    assert usagescan.opencode_task_tokens("ses_ours") == 3 + 84 + 40 + 7 + 8 + 106


def test_an_rc_only_opencode_still_prices_its_run(tmp_path, monkeypatch):
    """The exporter is found the way the spawn finds it — through the user's shell.

    An agent runs in a terminal window, so an install that only the rc puts on ``PATH``
    is on the agent's ``PATH``; the Settings hint promises exactly that. The applet's
    own environment comes from a desktop entry and has none of it, so a CLI reached by
    name alone would leave every run of that install unpriced — the case this pricing
    path exists for.

    The rc here also prints, because one that does is ordinary and its greeting lands
    on the same stdout as the answer.
    """
    exe = tmp_path / "opt" / "opencode"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\ncat <<'JSON'\n" + json.dumps(EXPORTED) + "\nJSON\n",
                   encoding="utf-8")
    exe.chmod(0o755)
    shell = tmp_path / "rcshell"
    shell.write_text("#!/bin/sh\n"
                     "echo 'welcome back!'\n"
                     f"export PATH={shlex.quote(str(exe.parent))}:$PATH\n"
                     'exec /bin/sh "$@"\n', encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("DIPLOMAT_SHELL", str(shell))
    # What a desktop launcher hands the applet: the system directories and nothing
    # the user's rc would have added.
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    assert usagescan.opencode_task_tokens("ses_ours") == 3 + 84 + 40 + 7 + 8 + 106


def test_a_session_the_exporter_cannot_produce_is_unpriced_not_free(tmp_path,
                                                                    monkeypatch):
    _stub_opencode(tmp_path, monkeypatch, "#!/bin/sh\necho 'no such session' >&2\nexit 1\n")
    assert usagescan.opencode_task_tokens("ses_gone") is None


def test_a_run_with_no_session_is_never_sent_to_the_exporter(tmp_path, monkeypatch):
    """A Claude Code run has no session id, and asking OpenCode about one would price
    every such run at nothing rather than by its own transcript."""
    _stub_opencode(tmp_path, monkeypatch, "#!/bin/sh\necho SHOULD_NOT_RUN >&2\nexit 3\n")
    assert usagescan.opencode_task_tokens("") is None


def test_an_opencode_completion_is_priced_by_the_exporter(ledger, tmp_path,
                                                          monkeypatch):
    _stub_opencode(tmp_path, monkeypatch,
                   "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(EXPORTED) + "\nJSON\n")
    now = time.time()
    telemetry.record_completion("review:h/o/r#1@aa", "a prompt no transcript holds",
                                now - 60, now, now, session_id="ses_ours",
                                agent_runner="opencode")
    assert telemetry.load().tasks[0].tokens == 248


def test_a_hermes_completion_is_priced_from_its_own_session_row(ledger, tmp_path,
                                                                monkeypatch):
    """The third arm of the same fork. Hermes keeps running totals on the session row,
    so nothing is summed and nothing is shelled out to — but sending it to OpenCode's
    exporter instead would price every Hermes run at nothing.

    In BOTH units: the tokens make it comparable to every other runner's task, and the
    money is what the budget gate holds an OpenRouter-billed machine to."""
    from test_hermes_store import FINISHED, MODEL, OURS, PROMPT, _charge, store, user

    _stub_opencode(tmp_path, monkeypatch, "#!/bin/sh\necho SHOULD_NOT_RUN >&2\nexit 3\n")
    store({OURS: [user(PROMPT), FINISHED]})
    _charge(OURS, estimated=0.0675)
    now = time.time()
    telemetry.record_completion("review:h/o/r#1@aa", "a prompt no transcript holds",
                                now - 60, now, now, session_id=OURS,
                                agent_runner="hermes")
    task = telemetry.load().tasks[0]
    assert task.tokens == 125
    assert (task.usd, task.model) == (0.0675, MODEL)


def test_a_claude_completion_carries_no_money_at_all(ledger, scanner, tmp_path,
                                                     monkeypatch):
    """Claude Code spends a rate-limit window, not an account. A dollar figure on one
    of its tasks would put it in a distribution the money gate prices the NEXT task
    from — and there is no rate it could have been charged at."""
    repo, projects = scanner
    _stub_opencode(tmp_path, monkeypatch, "#!/bin/sh\nexit 3\n")
    _session(projects / "s" / "mine.jsonl", "review PR #41 please", str(repo), [1000, 500])
    now = time.time()
    telemetry.record_completion("review:h/o/r#41@aa", "review PR #41 please",
                                now - 300, now, now)
    task = telemetry.load().tasks[0]
    assert task.tokens == 1500
    assert (task.usd, task.model) == (None, "")


def test_a_claude_completion_is_still_priced_by_its_own_transcript(ledger, scanner,
                                                                   tmp_path,
                                                                   monkeypatch):
    """The other half of the same fork, and the one that breaks quietly: a Claude Code
    run has no session id, so sending it to the exporter anyway prices every one of
    them at nothing — which looks exactly like the attribution failures the screen is
    already designed to report."""
    repo, projects = scanner
    _stub_opencode(tmp_path, monkeypatch, "#!/bin/sh\necho SHOULD_NOT_RUN >&2\nexit 3\n")
    _session(projects / "s" / "mine.jsonl", "review PR #41 please", str(repo), [1000, 500])
    now = time.time()
    telemetry.record_completion("review:h/o/r#41@aa", "review PR #41 please",
                                now - 300, now, now)
    assert telemetry.load().tasks[0].tokens == 1500


def test_a_run_is_priced_before_its_directory_is_deleted(ledger, monkeypatch):
    """Retiring a run deletes the directory its prompt and its completion sentinel
    live in. Read after the delete, the prompt comes back empty and the sentinel's
    mtime is gone — so a run is priced against a transcript it can no longer be
    matched to, and stamped with the moment a poll noticed instead of the moment the
    agent exited.

    Which store to price it FROM lives in the same directory: a foreign runner's run
    is priced from its own session, and neither the runner nor the matched session id
    survives the delete either. Lost, such a run falls through to the ``~/.claude``
    scan, which holds no transcript of it — and it lands unpriced, which is the whole
    defect the foreign-runner pricing path exists to close."""
    from diplomat_runtime import agentregistry, agentstate, runner
    from diplomat_app.store import Store
    from test_autofix import register_run

    exited_at = time.time() - 300
    record = register_run(7, ledger_key="review:h/o/r#7@aa", prompt="the real prompt")
    agentregistry.runner_path(record.run_id).write_text(runner.OPENCODE,
                                                        encoding="utf-8")
    agentregistry.bind_session(record.run_id, "ses_ours")
    done = agentregistry.done_path(record.run_id)
    done.write_text("0", encoding="utf-8")
    os.utime(done, (exited_at, exited_at))

    seen = {}
    monkeypatch.setattr(telemetry, "record_completion",
                        lambda key, prompt, started, at, noticed, session_id="",
                        agent_runner="": seen.update(
                            prompt=prompt, at=at, session_id=session_id,
                            agent_runner=agent_runner))
    tick = agentstate.tick([record], agentstate.Evidence(
        processes=agentstate.Observation.present({}),
        sentinels=agentstate.Observation.present({record.run_id})), time.time(), 2)
    Store._retire_finished(Store(), tick)

    assert seen["prompt"] == "the real prompt"
    assert seen["at"] == pytest.approx(exited_at, abs=1)
    assert seen["session_id"] == "ses_ours"
    assert seen["agent_runner"] == runner.OPENCODE


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
    assert usagescan.task_run("review PR #41 please", started, time.time()).tokens == 1500


def test_a_prompt_written_as_content_blocks_still_matches(scanner):
    """Claude Code writes the opening message either way depending on how it was
    invoked; the spawn path this matches uses one of them today."""
    repo, projects = scanner
    _transcript(projects / "s" / "a.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "review PR #41 please"}]}},
        _turn(str(repo), 250),
    ])
    assert usagescan.task_run("review PR #41 please",
                              time.time() - 60, time.time()).tokens == 250


def test_a_transcript_outside_the_agents_lifetime_is_not_its_own(scanner):
    """A transcript is appended to while its agent works, so its mtime lands at or
    after the last turn — never before the agent started. Without that bound an
    identical prompt from last week would be charged to today's run."""
    repo, projects = scanner
    path = projects / "s" / "a.jsonl"
    _session(path, "review PR #41 please", str(repo), [250])
    stale = time.time() - 30 * 86400
    os.utime(path, (stale, stale))
    assert usagescan.task_run("review PR #41 please",
                              time.time() - 60, time.time()) is None


def test_an_unmatched_prompt_reports_nothing_rather_than_zero(scanner):
    repo, projects = scanner
    _session(projects / "s" / "a.jsonl", "some other agent's prompt", str(repo), [250])
    assert usagescan.task_run("review PR #41 please",
                              time.time() - 60, time.time()) is None
    assert usagescan.task_run("", time.time() - 60, time.time()) is None


# MARK: - Retiring a run, which is what actually prices it


def test_retiring_a_run_prices_it_from_the_transcript_its_prompt_names(ledger, scanner):
    """The pricing inputs live in the run directory that retirement deletes, so both
    have to be read before it goes. Every figure on the screen that is per-task comes
    through here: a retirement that reads the directory afterwards prices nothing at
    all, and the ledger fills with completions the screen can only count."""
    from diplomat_runtime import agentregistry, agentstate
    from diplomat_app.store import Store

    repo, projects = scanner
    prompt = "review PR #41 please"
    dispatched_at = time.time() - 900
    exited_at = dispatched_at + 300
    transcript = projects / "s" / "mine.jsonl"
    _session(transcript, prompt, str(repo), [1000, 500])
    os.utime(transcript, (exited_at, exited_at))  # last turn written as the agent exits

    record = agentstate.RunRecord(run_id="r1", dispatched_at=dispatched_at,
                                  pr_number=41, kind="review",
                                  ledger_key="review:o/r#41@aa")
    agentregistry.create_run(record, prompt)
    sentinel = agentregistry.done_path("r1")
    sentinel.write_text("0", encoding="utf-8")
    os.utime(sentinel, (exited_at, exited_at))

    telemetry.record_started(record.ledger_key)
    Store()._retire_finished(agentstate.Tick(records=[], states={}, rows=[],
                                             cap_load=set(), retirable=[record],
                                             free_slots=0))

    telemetry._reset_cache()
    task = telemetry.load().tasks[0]
    assert task.tokens == 1500, "the run was retired without being priced"
    assert task.done_at == exited_at, "the exit time came from the poll, not the agent"


def test_a_run_the_mesh_placed_is_dated_from_its_transcript(ledger, scanner):
    """A mesh executor points its agent at a completion sentinel of its own under the
    mesh directory and unlinks it the moment it fires, so the run directory never
    gets one and ``finished_at`` has nothing to read. Left at the poll instant, every
    such run's measured duration stretches to wherever that poll happened to land —
    and on a machine running the mesh, every run is one of these."""
    from diplomat_runtime import agentregistry, agentstate
    from diplomat_app.store import Store

    repo, projects = scanner
    prompt = "resolve the conflicts on PR #9 please"
    dispatched_at = time.time() - 3600
    exited_at = dispatched_at + 300
    transcript = projects / "s" / "meshed.jsonl"
    _session(transcript, prompt, str(repo), [700])
    os.utime(transcript, (exited_at, exited_at))

    record = agentstate.RunRecord(run_id="m1", dispatched_at=dispatched_at,
                                  pr_number=9, kind="conflicts",
                                  placement=agentstate.PLACEMENT_MESH_HERE,
                                  ledger_key="conflicts:o/r#9@aa")
    agentregistry.create_run(record, prompt)  # and no sentinel: the mesh kept its own

    telemetry.record_started(record.ledger_key)
    Store()._retire_finished(agentstate.Tick(records=[], states={}, rows=[],
                                             cap_load=set(), retirable=[record],
                                             free_slots=0))

    telemetry._reset_cache()
    task = telemetry.load().tasks[0]
    assert task.tokens == 700, "the run was retired without being priced"
    assert task.done_at == exited_at, "the exit time came from the poll, not the agent"

# MARK: - What a range's token split measures


def _sample(at: float, repo: float, other: float = 0.0) -> telemetry.Sample:
    return telemetry.Sample(at=at, session_left=None, week_left=None,
                            repo_tokens=repo, other_tokens=other)


def _summarize(samples: list, *, now: float, days: float) -> telemetry.Summary:
    return telemetry.summarize(telemetry.Ledger(tasks=[], samples=samples),
                               now=now, days=days, steps=2, bin_count=4, z=1.96)


def test_a_range_counts_the_spend_since_the_reading_before_it_opened():
    """The counters are cumulative and the readings are 15 minutes apart, so a range
    boundary almost never lands on one. Measuring from the first reading INSIDE the
    range charges nothing for the interval that straddles the boundary — and that
    interval holds real spend, which on the live ledger was a sixth of a 1-day total.
    """
    now = 1_785_000_000.0
    s = _summarize([_sample(now - 7200, 1000.0),   # before the range opens
                    _sample(now - 1800, 5000.0),
                    _sample(now - 600, 6000.0)],
                   now=now, days=1 / 24)
    assert s.repo_tokens == 5000.0, "the straddling interval was dropped"


def test_a_range_with_no_earlier_reading_starts_at_its_first_one():
    """Nothing before the range means nothing to measure from, so the first reading
    in it is the baseline — the ledger's own beginning, not a boundary artefact."""
    now = 1_785_000_000.0
    s = _summarize([_sample(now - 1800, 1000.0), _sample(now - 600, 4000.0)],
                   now=now, days=1 / 24)
    assert s.repo_tokens == 3000.0


def test_a_range_with_no_readings_in_it_counts_nothing():
    now = 1_785_000_000.0
    s = _summarize([_sample(now - 8 * 86400, 1000.0)], now=now, days=1.0)
    assert s.repo_tokens == 0.0 and s.other_tokens == 0.0


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


# MARK: - Insisting


@pytest.fixture
def refusals(monkeypatch):
    """A probe whose endpoint refuses the first ``n`` attempts, as the real one does
    when another Claude Code session on the machine got to the shared bucket first.
    Yields a setter returning the attempt log, and takes the waits out of the clock so
    a test costs nothing to run."""
    monkeypatch.setenv("DIPLOMAT_QUOTA_PROBE", "1")
    monkeypatch.setattr(quota, "_oauth_token", lambda: "oat-test")
    monkeypatch.setattr(quota.time, "sleep", lambda _: None)
    quota._reset_cache()

    def refuse(n: int) -> list[int]:
        log: list[int] = []

        def fetch():
            log.append(len(log))
            if len(log) <= n:
                return None
            return {"five_hour": {"utilization": 40},
                    "seven_day": {"utilization": 10}}

        monkeypatch.setattr(quota, "_fetch", fetch)
        return log

    return refuse


def test_one_refusal_costs_an_insisting_probe_an_attempt_not_the_reading(refusals):
    """The whole point: the endpoint is one per-account bucket shared with every
    Claude Code session on the machine, so a refusal is routine. Settling for it
    leaves a hole in the ledger and a break in the quota chart."""
    log = refusals(2)
    assert quota.fractions_left(insist=True) == (0.6, 0.9)
    assert len(log) == 3


def test_a_probe_that_is_not_insisting_takes_the_one_attempt(refusals):
    """The control, and what still gates a dispatch: `AutoBudget` asks between
    deciding to spawn an agent and spawning it, where two and a half minutes of
    retries would cost more than the stale reading it already has."""
    log = refusals(2)
    assert quota.fractions_left() == (None, None)
    assert len(log) == 1


def test_an_insisting_probe_gives_up_after_its_last_attempt(refusals):
    """An endpoint that refuses everything must end the sample, not hold the worker
    (and, on macOS, the process poll behind it) indefinitely."""
    log = refusals(999)
    assert quota.fractions_left(insist=True) == (None, None)
    assert len(log) == quota._INSIST_ATTEMPTS + 1


def test_a_logged_out_machine_is_not_worth_insisting_to(refusals, monkeypatch):
    """No token is the one failure retrying cannot fix. Without this the sample of a
    machine that is simply logged out would sleep out the whole schedule, every
    quarter of an hour, for nothing."""
    log = refusals(999)
    monkeypatch.setattr(quota, "_oauth_token", lambda: None)
    assert quota.fractions_left(insist=True) == (None, None)
    assert len(log) == 1


def test_an_insisting_probe_reads_the_cache_before_it_spends_an_attempt(refusals):
    """The TTL still rules: a reading seconds old is answered from the cache, so an
    open panel cannot turn into a burst of requests against the shared bucket."""
    log = refusals(0)
    assert quota.fractions_left(insist=True) == (0.6, 0.9)
    assert quota.fractions_left(insist=True) == (0.6, 0.9)
    assert len(log) == 1
