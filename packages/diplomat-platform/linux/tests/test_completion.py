"""The turn-report mechanism: how a run says its work is done.

An agent is spawned interactively, so finishing is not exiting — the exit sentinel
never fires, the pid probe sees the same live process before and after, and the screen
scrape reads a string off someone else's status bar. Nothing there can end a run, so
the CLI is asked to report its own turn boundaries through hooks it runs itself.

The tests here cover the three layers of that: the FORMAT (:mod:`completion`), the
WIRING that reaches a real spawn, and the shell snippet actually running in a real
shell — because a quoting bug in that snippet is invisible to every test that only
inspects the JSON.

The snippet is where the subtlety lives. ``Stop`` ends the model's turn rather than
the work, so the hook reads the payload it is handed and reports ``busy`` while any
subagent or backgrounded command is still outstanding; those cases are driven through
``/bin/sh`` with real payload shapes rather than asserted against the JSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from diplomat_runtime import agentregistry, agentstate, completion, review, runner


# MARK: - The format


def test_the_last_line_is_the_answer():
    """The file is a log of transitions, not a set of flags. A run that finished, was
    nudged and finished again is idle — reading it any other way answers a question
    about the run's history instead of its state."""
    assert completion.parse("busy 100\nidle 200\nbusy 300\nidle 400") == ("idle", 400.0)
    assert completion.parse("busy 100\nidle 200\nbusy 300") == ("busy", 300.0)


def test_a_file_that_says_nothing_yet_is_not_an_answer():
    """The seconds before a run's first hook fires, and every run spawned without
    hooks at all. ``None`` degrades to the evidence such a run always had; read as
    anything else it would end a run that just started."""
    assert completion.parse(None) is None
    assert completion.parse("") is None
    assert completion.parse("\n\n") is None


def test_a_torn_final_line_does_not_hide_the_good_state_under_it():
    """A hook killed mid-write leaves a partial line. Scanning past it finds the last
    line that IS a state; ending the scan there would freeze the run at whatever it
    last reported before the tear."""
    assert completion.parse("busy 100\nidle 200\nid") == ("idle", 200.0)
    assert completion.parse("busy 100\nidle 200\nidle") == ("idle", 200.0)
    assert completion.parse("busy 100\nidle 200\nidle not-a-number") == ("idle", 200.0)


def test_a_verb_nothing_writes_is_ignored_rather_than_guessed_at():
    assert completion.parse("busy 100\nfinished 200") == ("busy", 100.0)


def test_only_the_terminal_verbs_end_a_run():
    """``busy`` is the one verb that must never read as over, and ``None`` — the
    absence of an answer — must never end a run either."""
    assert completion.is_over(completion.IDLE)
    assert completion.is_over(completion.ENDED)
    assert not completion.is_over(completion.BUSY)
    assert not completion.is_over(None)


def test_every_verb_the_hooks_write_is_one_parse_can_read():
    """Anti-drift: the writer and the reader are the same module precisely so a verb
    cannot be added to one and not the other."""
    assert set(completion.EVENTS.values()) == set(completion.VERBS)


def test_a_subagent_finishing_is_not_the_task_finishing():
    """``SubagentStop`` fires whenever a delegated helper returns. Wired up, it would
    retire every run that delegated at the moment its first helper came back."""
    assert "SubagentStop" not in completion.EVENTS


def test_every_guarded_event_is_one_the_hooks_actually_write():
    """Anti-drift: a guard on an event nothing writes protects nothing, and would read
    as coverage this has."""
    assert completion.GUARDED <= set(completion.EVENTS)


def test_only_the_turn_boundary_is_guarded():
    """``SessionEnd`` fires when the process is going away, and background work does
    not outlive the process that started it — guarding it would leave a run that
    exited mid-subagent reporting ``busy`` until the stillness backstop."""
    assert completion.GUARDED == {"Stop"}


# MARK: - The shell snippet, run in a real shell


def _payload(*pending: str) -> str:
    """A hook payload as the CLI writes it: minified, with the fields around
    ``background_tasks`` that a naive match could trip over."""
    tasks = ",".join(json.dumps({"id": f"a{i}", "type": kind, "status": "running",
                                 "description": f"probe {i}"})
                     for i, kind in enumerate(pending))
    return ('{"session_id":"ac238dab","hook_event_name":"Stop","stop_hook_active":false,'
            f'"last_assistant_message":"working","background_tasks":[{tasks}],'
            '"session_crons":[]}')


def _fire(event: str, activity, done=None, payload: str = "") -> int:
    """Run one hook's command the way the CLI would, payload on stdin.

    Returned rather than asserted so the exit status is a fact each test can make its
    own claim about; a hook that exits non-zero is reported to the agent as failing.
    """
    cmd = completion.hook_settings(str(activity), done and str(done))
    snippet = cmd["hooks"][event][0]["hooks"][0]["command"]
    return subprocess.run(["/bin/sh", "-c", snippet], input=payload,
                          text=True).returncode


def test_the_hook_command_actually_writes_a_line_parse_can_read(tmp_path):
    """The end-to-end of the format: what the hook runs must produce what the reader
    expects. A quoting bug here is invisible to every test that only reads the JSON,
    and would leave every run reporting nothing at all."""
    activity = tmp_path / "activity"
    _fire("UserPromptSubmit", activity)
    _fire("Stop", activity)

    state = completion.parse(activity.read_text())
    assert state is not None and state[0] == completion.IDLE
    assert state[1] > 0, "the hook must stamp a real epoch, not an empty string"
    assert len(activity.read_text().splitlines()) == 2, "each hook appends one line"


def test_a_path_with_spaces_survives_the_snippet(tmp_path):
    """The run directory is under the operator's home, which is theirs to name."""
    activity = tmp_path / "a dir with spaces" / "activity"
    activity.parent.mkdir()
    _fire("Stop", activity)
    assert completion.parse(activity.read_text())[0] == completion.IDLE


def test_the_hooks_of_a_retired_run_do_not_fail_in_the_session_they_outlive(tmp_path):
    """The applet deletes a run's directory the moment it retires the run, and the
    report these hooks write is what retires one — so from its first turn onwards the
    agent is alive at its prompt with nowhere to write. Every later turn (a human
    typing, or the API-error watcher's nudge) then ends in a hook the CLI reports to
    the operator as failing, for the life of the session."""
    gone = tmp_path / "1787678987-65629fdf"
    activity, done = gone / "activity", gone / "done"
    gone.mkdir()
    _fire("Stop", activity, done, payload=_payload())
    shutil.rmtree(gone)

    assert _fire("UserPromptSubmit", activity, done) == 0
    assert _fire("Stop", activity, done, payload=_payload()) == 0
    assert _fire("SessionEnd", activity, done) == 0
    assert not gone.exists(), \
        "a report nothing reads must not rebuild the directory the applet dropped"


def test_a_report_that_cannot_land_is_silent_as_well_as_successful(tmp_path):
    """`2>/dev/null` precedes the redirect, which is the only order that also silences
    the shell's own `No such file or directory`: the open fails before a trailing one
    is in effect. Measured — the same snippet with the two the other way round exits 0
    and still writes that line to the hook's stderr, once per turn."""
    snippet = completion.hook_settings(str(tmp_path / "gone" / "activity"))
    snippet = snippet["hooks"]["Stop"][0]["hooks"][0]["command"]

    proc = subprocess.run(["/bin/sh", "-c", snippet], input="", text=True,
                          capture_output=True)

    assert proc.returncode == 0
    assert proc.stderr == ""


# MARK: - A turn that ended with work still outstanding


def test_a_turn_that_dispatched_subagents_is_not_over(tmp_path):
    """``Stop`` marks the end of the MODEL's turn, and a turn that dispatched subagents
    ends while they are still running — the CLI hands the turn back and re-enters when
    one reports.

    Read at face value that is a run finished seconds after dispatch, with three
    subagents still working, its bay freed and its run directory deleted under it.
    Measured against a real session: ``idle`` landed 6s in and stood for the next 50.
    """
    activity = tmp_path / "activity"
    assert _fire("Stop", activity, payload=_payload("subagent", "subagent")) == 0
    assert completion.parse(activity.read_text())[0] == completion.BUSY


def test_a_backgrounded_shell_holds_the_turn_open_too(tmp_path):
    """Same mechanism, other kind: a backgrounded command re-invokes the agent when it
    exits, so the turn it was started in is not the last one."""
    activity = tmp_path / "activity"
    _fire("Stop", activity, payload=_payload("shell"))
    assert completion.parse(activity.read_text())[0] == completion.BUSY


def test_a_turn_with_nothing_outstanding_is_over(tmp_path):
    """The other half, without which the guard would be a run that never finishes."""
    activity = tmp_path / "activity"
    assert _fire("Stop", activity, payload=_payload()) == 0
    assert completion.parse(activity.read_text())[0] == completion.IDLE


def test_the_mesh_key_is_held_until_the_subagents_are_done(tmp_path):
    """The sentinel releases a szpontnet work key. Writing it on a turn that only
    dispatched subagents would let a peer re-run work still in flight."""
    activity, done = tmp_path / "activity", tmp_path / "done"
    _fire("Stop", activity, done, payload=_payload("subagent"))
    assert not done.exists()

    _fire("Stop", activity, done, payload=_payload())
    assert done.read_text() == "0"


@pytest.mark.parametrize("payload", [
    "",                                        # stdin closed, or a hook run with none
    '{"hook_event_name":"Stop"}',              # a CLI that carries no such field
    '{"background_tasks":[]}',                 # the field, empty
    '{\n  "background_tasks": [\n  ]\n}',      # pretty-printed and empty
])
def test_a_payload_that_says_nothing_reports_the_turn_over(tmp_path, payload):
    """The one direction this guard can be wrong in, and the direction it is chosen to
    be wrong in: a payload it cannot read reports the turn over rather than holding a
    finished run open forever. The stillness backstop covers the miss; nothing covers a
    run that can never be retired."""
    activity = tmp_path / "activity"
    _fire("Stop", activity, payload=payload)
    assert completion.parse(activity.read_text())[0] == completion.IDLE


def test_pending_work_is_seen_through_a_reformatted_payload(tmp_path):
    """Whitespace is stripped before the match, so the guard does not quietly stop
    guarding if the CLI ever pretty-prints what it hands the hook."""
    activity = tmp_path / "activity"
    _fire("Stop", activity, payload='{\n  "background_tasks": [\n    {"type": '
                                    '"subagent", "status": "running"}\n  ]\n}')
    assert completion.parse(activity.read_text())[0] == completion.BUSY


def test_a_decoy_in_the_transcript_cannot_forge_pending_work(tmp_path):
    """``last_assistant_message`` carries whatever the agent last said, which can be
    this module's own source. To be valid JSON a decoy has to escape its quotes, and
    the escape is what breaks the match."""
    activity = tmp_path / "activity"
    decoy = json.dumps(completion.PENDING + ' and no real work')
    _fire("Stop", activity,
          payload=f'{{"last_assistant_message":{decoy},"background_tasks":[]}}')
    assert completion.parse(activity.read_text())[0] == completion.IDLE


# MARK: - The mesh's reader of the same report


def test_the_mesh_sentinel_is_written_by_the_terminal_verbs_only(tmp_path):
    """A szpontnet executor holds its claim until the exit sentinel exists, so writing
    it on a turn STARTING would free the work key the instant the agent began."""
    activity, done = tmp_path / "activity", tmp_path / "done"

    _fire("UserPromptSubmit", activity, done)
    assert not done.exists(), "a turn starting must not look like a finished job"

    _fire("Stop", activity, done)
    assert done.read_text() == "0"


def test_a_local_run_is_pointed_at_no_sentinel(tmp_path):
    """Only the mesh reads one. A local run is judged by the activity file, which
    survives a second turn — the sentinel is a latch and would retire a nudged run
    mid-work."""
    settings = completion.hook_settings(str(tmp_path / "activity"))
    for spec in settings["hooks"].values():
        assert "printf 0 >" not in spec[0]["hooks"][0]["command"]


# MARK: - Reaching a real spawn


def test_the_run_directory_carries_the_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path))
    record = agentregistry.create_run(
        agentstate.RunRecord(run_id="r1", dispatched_at=1.0), "do a thing")

    path = agentregistry.stage_hooks(record.run_id)

    assert path == str(agentregistry.hooks_path("r1"))
    settings = json.loads(agentregistry.hooks_path("r1").read_text())
    written = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str(agentregistry.activity_path("r1")) in written


def test_the_registry_reads_back_what_the_hooks_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path))
    records = [agentregistry.create_run(
        agentstate.RunRecord(run_id="r1", dispatched_at=1.0), "p")]
    agentregistry.stage_hooks("r1")
    _fire("Stop", agentregistry.activity_path("r1"))

    obs = agentregistry.activity(records)

    assert obs.ok, "reading our own directory is always a positive answer"
    assert obs.value["r1"][0] == completion.IDLE


def test_a_run_that_has_reported_nothing_is_absent_rather_than_finished(
        tmp_path, monkeypatch):
    """Absent means "ask the other evidence". Present-and-idle would retire a run in
    the seconds before its first hook fires."""
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path))
    records = [agentregistry.create_run(
        agentstate.RunRecord(run_id="r1", dispatched_at=1.0), "p")]

    obs = agentregistry.activity(records)

    assert obs.ok and obs.value == {}


def test_the_agent_word_stays_first_so_the_users_alias_still_expands(monkeypatch):
    """The `claude` alias is what carries ``--dangerously-skip-permissions``. Alias
    expansion applies to the first word of a simple command, so a flag put ahead of
    the agent word would silently drop the autonomy every spawned agent needs."""
    monkeypatch.setattr(runner, "selected", lambda: runner.CLAUDE)

    cmd = runner.agent_command("/tmp/p.txt", None, "/run/hooks.json")

    assert cmd.startswith("claude ")
    assert "--settings /run/hooks.json" in cmd


def test_a_spawn_with_no_hooks_is_still_a_valid_spawn(monkeypatch):
    """Staging them can fail, and two of the three runners have none. Such a run is
    read off its screen exactly as every run was before."""
    monkeypatch.setattr(runner, "selected", lambda: runner.CLAUDE)
    assert "--settings" not in runner.agent_command("/tmp/p.txt", None, None)


def test_the_hooks_reach_the_command_the_terminal_runs(monkeypatch):
    """The whole point of threading the path through: a settings file staged but
    never handed to the agent reports nothing."""
    monkeypatch.setattr(runner, "selected", lambda: runner.CLAUDE)

    cmd = review.shell_command("/tmp/p.txt", "/run/done", "/run/pid", None,
                               "/run/hooks.json")

    assert "--settings /run/hooks.json" in cmd


@pytest.mark.parametrize("chosen", [runner.OPENCODE, runner.HERMES])
def test_a_foreign_runner_is_handed_no_claude_settings(monkeypatch, chosen):
    """Neither takes the flag; passing it would fail the spawn outright. They are
    asked what they are doing through their own session stores instead."""
    monkeypatch.setattr(runner, "selected", lambda: chosen)
    monkeypatch.setattr(runner, "model", lambda: "")
    assert "--settings" not in runner.agent_command("/tmp/p.txt", None, "/run/h.json")
