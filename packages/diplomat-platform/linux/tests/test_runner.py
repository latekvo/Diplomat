"""The agent-CLI seam: which binary a spawn runs, and what still has to be true of
the spawn afterwards.

Everything the applet does to an agent after it starts — count it against the task
cap, adopt it by pid, read whether it is working, reap it — is written against a
process and a pane it did not create. Swapping the CLI changes both. These tests
pin the parts that would otherwise fail silently: an agent nobody can see in ``ps``
still burns quota, and an agent that always reads as idle empties the cap under a
machine that is full.

The OpenCode pane fixtures are captured verbatim from a real ``opencode`` TUI
(v1.4.3) driven through tmux, not composed here — the whole point of the busy
marker is that it is someone else's string, so a fixture we wrote ourselves would
only ever prove we are consistent with ourselves.
"""

from __future__ import annotations

import shlex

import pytest

from diplomat_app import apiwatch, appconfig, autofix, probes, review, runner
from diplomat_app.agentstate import RunRecord

# MARK: - Real captured panes


#: The live status bar of an OpenCode TUI mid-turn. Claude Code's spelling of the
#: same hint is "esc to interrupt"; neither string contains the other.
OPENCODE_BUSY = (
    "  ┃  Build  Claude Haiku 4.5 Anthropic\n"
    "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
    "   ⬝⬝⬝⬝⬝⬝⬝⬝  esc interrupt              "
    "                   tab agents  ctrl+p commands"
)

#: The same pane once the turn ended and it is back at its prompt.
OPENCODE_IDLE = (
    "  ┃  Build  Claude Haiku 4.5 Anthropic\n"
    "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
    "                          30.5K (15%) · $0.04  ctrl+p commands"
)

#: How OpenCode surfaces a rejected turn: the provider's own JSON, verbatim.
OPENCODE_AUTH_ERROR = (
    '  ┃  Unauthorized: {"error":{"message":"Unauthorized",'
    '"type":"api_error","param":null,"code":null}}\n'
    "  ┃  Build  gpt-oss:120b Bad\n"
    "                          tab agents  ctrl+p commands"
)


@pytest.fixture
def opencode(monkeypatch, tmp_path):
    """Select the OpenCode runner the way Settings does — through the shared config
    file, not a monkeypatched accessor, so the read path itself is under test."""
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    return runner.OPENCODE


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    """The same, for Hermes."""
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.HERMES)
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    return runner.HERMES


# MARK: - Which CLI a spawn runs


def test_an_unset_runner_is_claude_code():
    """Every existing install has no such key, and each one is mid-flight on agents
    spawned by the old spelling. Defaulting anywhere else would retarget them all on
    the first poll after an update."""
    assert runner.selected() == runner.CLAUDE
    assert runner.agent_command("/tmp/p.txt").startswith("claude ")


def test_an_unrecognised_runner_degrades_to_claude_code_rather_than_failing():
    """A config from a newer applet (or a hand-edit) must not leave this one unable to
    dispatch at all — the failure mode of a typo here is every spawn raising."""
    appconfig.set_value(appconfig.AGENT_RUNNER, "gpt-cli")
    assert runner.selected() == runner.CLAUDE


def test_the_opencode_runner_runs_opencode_on_the_staged_prompt(opencode):
    cmd = runner.agent_command("/tmp/p.txt")
    assert cmd.endswith('opencode --prompt "$(cat /tmp/p.txt)"')


def test_a_configured_model_is_passed_and_an_unset_one_is_not(opencode):
    """OpenCode already remembers a model per install. Passing a guess when the user
    picked none would silently move them off the model their own picker selected."""
    assert " -m " not in runner.agent_command("/tmp/p.txt")
    appconfig.set_value(appconfig.AGENT_MODEL, "openrouter/moonshotai/kimi-k2")
    assert runner.agent_command("/tmp/p.txt").endswith(
        'opencode -m openrouter/moonshotai/kimi-k2 --prompt "$(cat /tmp/p.txt)"')


def test_the_hermes_runner_opens_a_window_the_operator_can_watch(hermes):
    """Spelled out rather than built from parts, and spelled out identically in
    ``DiplomatCoreSmoke``: one config file picks the runner for both front-ends, and a
    machine can hand a mesh job to the other platform.

    ``--tui`` is what makes this a windowed agent rather than a headless one — the
    operator can watch it and type into it, exactly as with the other two. ``--yolo``
    is the autonomy the Claude alias carries and ``OPENCODE_PERMISSION`` grants; an
    agent that stops to ask in an unwatched window holds a bay of the task cap until a
    human notices. ``-q`` is what stores the prompt verbatim as the session's opening
    message, which is the key :mod:`hermesstore` matches a run to its session by."""
    assert runner.agent_command("/tmp/p.txt") == (
        'hermes chat --tui --yolo -q "$(cat /tmp/p.txt)"')


def test_hermes_takes_the_configured_model_and_no_port(hermes):
    """A port would be meaningless — Hermes serves no per-run server, and answers the
    same question from its own session store."""
    appconfig.set_value(appconfig.AGENT_MODEL, "openai/gpt-5.2")
    cmd = runner.agent_command("/tmp/p.txt", port=47910)
    assert cmd == 'hermes chat --tui --yolo -m openai/gpt-5.2 -q "$(cat /tmp/p.txt)"'


def test_each_runner_hands_the_user_to_its_own_provider_wizard(hermes):
    """Diplomat holds no API key for either runner: each ships a wizard that knows its
    whole provider catalog and writes the credential to its own store. Sending a Hermes
    user to OpenCode's wizard would connect a provider the agent never reads."""
    assert runner.setup_command() == "hermes setup; hermes status"
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    assert runner.setup_command() == "opencode providers login; opencode providers list"


def test_a_prompt_path_with_a_space_survives_the_hand_off(opencode):
    """The prompt reaches the agent through ``$(cat …)``, so an unquoted path would
    split into two arguments and the agent would run on an empty prompt."""
    cmd = runner.agent_command("/tmp/my runs/p.txt")
    assert "'/tmp/my runs/p.txt'" in cmd


# MARK: - What the spawn still has to look like


def test_the_agent_is_the_last_command_of_the_inner_shell(opencode):
    """The pid the applet identifies a run by is written by the inner shell, which then
    execs the agent over itself. That only happens for the shell's LAST command — an
    agent with anything after it inside those quotes records the wrapper's pid, and
    every later probe then reads the wrong process."""
    cmd = review.shell_command("/tmp/p.txt", done_path="/tmp/d", pid_path="/tmp/pid")
    # The inner shell's `-c` argument, unquoted the way the outer shell will unquote it.
    inner = shlex.split(cmd.split("2>/dev/null; ", 1)[1])[3].rstrip(";")
    assert inner.startswith("printf %s $$ > /tmp/pid; ")
    assert inner.endswith('opencode --prompt "$(cat /tmp/p.txt)"')


def test_the_exec_is_never_spelled_out(opencode):
    """Written as the `exec` keyword it is the first word of the command, which under
    Claude Code stops the user's alias expanding (costing the agent its permissions).
    The spelling is shared, so it must not creep in on the OpenCode branch either."""
    cmd = review.shell_command("/tmp/p.txt", pid_path="/tmp/pid")
    assert "exec opencode" not in cmd and "exec claude" not in cmd


def test_an_opencode_spawn_carries_its_own_permission_grant(opencode):
    """Claude Code gets its autonomy from the user's own alias. OpenCode has no alias
    to carry one, and an agent that stops to ask in an unwatched window holds a bay of
    the task cap until a human notices.

    Spelled out rather than built from the constants, and spelled out identically in
    ``DiplomatCoreSmoke``: one config file picks the runner for both front-ends, and a
    machine can hand a mesh job to the other platform. Two sides that agree on the
    idea and differ by a byte here are two applets granting different permissions."""
    assert runner.agent_command("/tmp/p.txt") == (
        'OPENCODE_PERMISSION=\'{"edit":"allow","bash":"allow","webfetch":"allow",'
        '"external_directory":"allow","doom_loop":"allow"}\' '
        'opencode --prompt "$(cat /tmp/p.txt)"'
    )


def test_the_grant_travels_in_the_command_not_the_launchers_environment(opencode):
    """``tmux new-session`` hands the command to a server that already exists, and the
    session runs with the SERVER's environment — a variable set on the launcher never
    arrives. The macOS spawner has no environment channel at all. Put it in the
    environment and the agent silently stops at its first permission prompt."""
    assert runner.OPENCODE_PERMISSION_ENV not in review.spawn_env()
    assert runner.OPENCODE_PERMISSION_ENV in review.shell_command("/tmp/p.txt")


def test_a_claude_spawn_is_left_exactly_as_it_was(monkeypatch, tmp_path):
    """The default runner's command is the one every existing install is mid-flight on;
    this is the string that must not move."""
    monkeypatch.setattr(review, "repo_path", lambda: "/repo")
    monkeypatch.setattr(review, "user_shell", lambda: "/bin/zsh")
    assert review.shell_command("/tmp/p.txt", done_path="/tmp/d") == (
        "cd /repo 2>/dev/null; claude \"$(cat /tmp/p.txt)\"; "
        "printf %s $? > /tmp/d; exec \"$SHELL\" -i"
    )
    assert runner.OPENCODE_PERMISSION_ENV not in review.spawn_env()


# MARK: - Seeing the agent afterwards


@pytest.mark.parametrize("line, seen", [
    ("2 pts/1 30 opencode --prompt Review PR #7 in o/r", True),
    ("2 pts/1 30 claude Review PR #7 in o/r", True),
    ("2 pts/1 30 hermes chat --tui --yolo -q Review PR #7 in o/r", True),
    ("2 pts/1 30 vim notes.txt", False),
])
def test_every_runner_is_visible_to_the_scans_that_count_agents(line, seen):
    """An agent the applet can spawn but no scan can see runs outside the task cap
    entirely: it burns quota, holds no bay, and the panel redraws it as untracked on
    every tick."""
    assert runner.is_agent_line(line) is seen


def test_an_opencode_agent_is_counted_by_the_nodes_capacity_hook():
    """``agent_lines`` is the mesh node's answer to "is this box full?", and the node
    is a separate stdlib-only process — a runner it cannot see is a machine that keeps
    accepting routed work it has no room for."""
    ps = "pts/3 opencode --prompt Review PR #41 in software-mansion/argent\n"
    assert list(autofix.agent_lines(ps, "software-mansion", "argent")) == [("pts/3", 41)]


def test_an_opencode_pane_mid_turn_reads_as_busy():
    """OpenCode writes "esc interrupt" where Claude Code writes "esc to interrupt" —
    neither contains the other. Matching only Claude's spelling reads every working
    OpenCode agent as idle, which hands its bay back and lets the monitor dispatch
    over the top of it."""
    assert apiwatch.looks_busy(OPENCODE_BUSY) is True


def test_an_opencode_pane_back_at_its_prompt_reads_as_idle():
    """The other half: a finished agent sits at its prompt for hours, and reading that
    as busy would hold a bay of the cap until someone closed the window."""
    assert apiwatch.looks_busy(OPENCODE_IDLE) is False


def test_a_rejected_opencode_turn_is_not_mistaken_for_a_nudgeable_stall():
    """The error patterns were read off Claude Code's banners. OpenCode surfaces the
    provider's raw JSON, and the one shape observed is an auth rejection — permanent,
    so nudging it would churn forever exactly as nudging a quota banner would."""
    assert apiwatch.looks_like_api_error(OPENCODE_AUTH_ERROR) is False
    assert apiwatch.looks_busy(OPENCODE_AUTH_ERROR) is False


def test_the_marker_count_rises_for_an_opencode_screen(monkeypatch):
    """The stale-marker warning is the only thing that would ever say the busy string
    stopped matching. Counting an OpenCode screen as "read but never matched" would
    make it cry wolf on a machine whose agents are all fine."""
    probes.reset_cache()
    monkeypatch.setattr(probes.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(probes.tmuxwatch, "pane_tails_for_ttys",
                        lambda ttys: {"pts/1": OPENCODE_BUSY})
    record = RunRecord(run_id="a", dispatched_at=0.0, pr_number=1, kind="review",
                       tty="pts/1")
    probes.pane_tails([record], now=1000.0)
    assert probes.marker_stats() == (1, 1)
