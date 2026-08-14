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

from diplomat_runtime import apiwatch, appconfig, autofix, review, runner
from diplomat_app import probes
from diplomat_runtime.agentstate import RunRecord

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

#: A Hermes pane mid-turn. Its hint sits on the composer rather than a status bar, and
#: it is a third spelling again — no other runner's marker appears anywhere in it.
HERMES_BUSY = (
    " └─ ▾ Tool calls (1)\n"
    '   └─ ● ⡡ Terminal("sleep 45") (29s)\n'
    " ─ (°ロ°) contemplating… · 33s │ glm 5.2 xhigh │ 33s │ voice off │ 1 session\n"
    " ❯ Ctrl+C to interrupt…"
)

#: The same pane once the agent answered and went back to its prompt.
HERMES_IDLE = (
    " ┊  DONE\n"
    " ─ ready │ glm 5.2 xhigh │ 26k/1m │ [░░░░░░░░░░] 3% │ 53s │ ✓ 0s │ voice off\n"
    " ❯"
)

#: A Freebuff pane mid-turn. Its hint is a stop BUTTON rather than a sentence, drawn
#: on the right of the same status bar that carries the spinner's "working…".
#:
#: Reconstructed from freebuff 0.0.149's own bundle rather than captured, and the one
#: fixture here that is: Freebuff's backend answers 401 to every key its login mints
#: (verified in its own ``~/.config/manicode/projects/*/chats/*/log.jsonl``), so no
#: turn can be run at all. What is pinned is the CLI's literal and the condition it
#: renders under — ``kind === "waiting" || kind === "streaming"`` draws ``■ Esc``,
#: ``kind === "idle"`` draws ``✕ End session`` — and the surrounding layout is a
#: plausible frame around it, not evidence. Recapture from a live turn when the
#: account works.
FREEBUFF_BUSY = (
    "  enter a coding task or / for commands\n"
    "  working...                                                      ■ Esc"
)

#: The same bar once the turn ended: the stop button is replaced by the session's, and
#: the composer is back to its placeholder.
FREEBUFF_IDLE = (
    "  enter a coding task or / for commands\n"
    "  Buffy · unlimited                                       ✕ End session"
)

#: The two screens a Freebuff spawn lands on when it CANNOT take a prompt, both
#: captured verbatim from freebuff 0.0.149 driven through a pty on a real macOS box
#: (80×24, ANSI rendered to a screen the way ``capture-pane`` hands one over).
#:
#: These are what the readiness check has to reject, and they are the reason it looks
#: for the composer's placeholder rather than for the absence of anything: a spawn that
#: is parked here looks exactly as calm as one that is ready.
FREEBUFF_LOGIN_WALL = (
    " ⚠ We found an API key but it appears to be invalid. Please log in again to\n"
    " continue.\n"
    "        ███████╗██████╗ ███████╗███████╗██████╗ ██╗   ██╗███████╗███████╗\n"
    "        ██║     ██║  ██║███████╗███████╗██████╔╝╚██████╔╝██║     ██║\n"
    "                             Press ENTER to login..."
)

FREEBUFF_PROJECT_PICKER = (
    "           ▍Select project directory...\n"
    "          ┌──────────────────────────────────────────────────────────┐\n"
    "          │ 📂   ..                                                 ▀│\n"
    "          │ 📁   Applications                                        │\n"
    "          └──────────────────────────────────────────────────────────┘\n"
    "                                                            ┌────────┐\n"
    "          ~                                                 │  Open  │\n"
    "                                                            └────────┘"
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


@pytest.fixture
def freebuff(monkeypatch, tmp_path):
    """The same, for Freebuff. The repo root is stubbed like the others, and it is the
    one runner that reads it: the checkout goes on its command line as well as into the
    ``cd``."""
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.FREEBUFF)
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    return runner.FREEBUFF


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


def test_the_freebuff_runner_opens_on_the_repo_and_carries_no_prompt(freebuff, tmp_path):
    """The one spawn whose command does not hand the agent its prompt, because its CLI
    takes none: under the ``freebuff`` name the parser restricts its one positional to
    ``login`` and hard-codes the initial prompt to nothing. Passing the file anyway
    would be a run that starts by reading out its own instructions as a task.

    ``--cwd`` is what keeps a spawn off Freebuff's directory picker. The ``cd`` in
    ``shell_command`` is quiet by design, and where the other runners would then work
    in the wrong directory, Freebuff would open a picker and wait for a human forever
    while holding a bay of the task cap."""
    cmd = runner.agent_command("/tmp/p.txt")
    assert cmd == f"freebuff --cwd {shlex.quote(str(tmp_path))}"
    assert "p.txt" not in cmd


def test_freebuff_is_passed_neither_a_model_nor_a_port(freebuff):
    """Its CLI has neither flag: the free tier picks the model server-side, and its
    session lives on Freebuff's own machines rather than on a loopback port. Passing
    either would not be ignored — an unknown option is a CLI that exits, which the
    completion sentinel would record as a run that finished in a second."""
    appconfig.set_value(appconfig.AGENT_MODEL, "openai/gpt-5.2")
    cmd = runner.agent_command("/tmp/p.txt", port=47910)
    assert " -m " not in cmd and "47910" not in cmd


def test_freebuff_is_not_offered_a_model_field_at_all():
    """The other side of the same fact, and the one Settings reads: a runner absent
    from ``MODEL_RUNNERS`` gets no model field, rather than one whose value nothing
    between it and the agent would ever look at."""
    assert runner.FREEBUFF not in runner.MODEL_RUNNERS
    assert set(runner.MODEL_RUNNERS) == {runner.OPENCODE, runner.HERMES}


def test_only_freebuff_has_to_be_typed_its_prompt():
    """What every dispatch path asks before deciding a spawn is finished. Answering it
    for the wrong runner types a second prompt into an agent that already has one."""
    assert runner.takes_typed_prompt(runner.FREEBUFF)
    assert not any(runner.takes_typed_prompt(r) for r in runner.RUNNERS
                   if r != runner.FREEBUFF)


def test_the_typed_prompt_is_one_line_that_names_the_staged_file():
    """It goes in through channels that submit a LINE — tmux ``send-keys`` + Enter,
    iTerm ``write text``, Terminal ``do script … in tab`` — so a newline anywhere in it
    submits early and hands the agent a fragment of its own instructions. That is why
    it points at the prompt rather than being it."""
    line = runner.prompt_handoff("/home/u/.diplomat/agents/17-ab/prompt.txt")
    assert "\n" not in line
    assert "/home/u/.diplomat/agents/17-ab/prompt.txt" in line


def test_each_runner_hands_the_user_to_its_own_provider_wizard(hermes):
    """Diplomat holds no API key for either runner: each ships a wizard that knows its
    whole provider catalog and writes the credential to its own store. Sending a Hermes
    user to OpenCode's wizard would connect a provider the agent never reads."""
    assert runner.setup_command() == "hermes setup; hermes status"
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    assert runner.setup_command() == "opencode providers login; opencode providers list"
    # Freebuff has no provider catalog to list — one account, one login — so its wizard
    # is the whole command and the window is left on what it printed: a URL to open.
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.FREEBUFF)
    assert runner.setup_command() == "freebuff login"


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
    ("2 pts/1 30 freebuff --cwd /home/u/dev/argent", True),
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


def test_a_hermes_pane_mid_turn_reads_as_busy():
    """A third spelling, "Ctrl+C to interrupt…", which neither of the others matches.
    The screen is what a Hermes run is read by until its session is written and bound —
    and permanently, if that store cannot be read — so a marker missing here means an
    agent that reads as idle the whole time it works: its bay goes back to the cap and
    the monitor dispatches over the top of it."""
    assert apiwatch.looks_busy(HERMES_BUSY) is True


def test_a_hermes_pane_back_at_its_prompt_reads_as_idle():
    assert apiwatch.looks_busy(HERMES_IDLE) is False


def test_a_freebuff_pane_mid_turn_reads_as_busy():
    """A fourth spelling, and the only one that is not a sentence: Freebuff draws a
    stop button, "■ Esc". Missing it reads every working Freebuff agent as idle, which
    hands its bay back to the cap while it is still working."""
    assert apiwatch.looks_busy(FREEBUFF_BUSY) is True


def test_a_freebuff_pane_between_turns_reads_as_idle():
    assert apiwatch.looks_busy(FREEBUFF_IDLE) is False


def test_the_word_esc_alone_does_not_make_a_freebuff_screen_busy():
    """Why the marker is the square and the word together. Freebuff's pickers hint
    "Esc cancel", and one of them is a screen a spawn can sit on indefinitely — read as
    busy, it would hold a bay of the task cap for as long as its window stayed open,
    which is exactly the state this most needs to report correctly."""
    assert "esc" in ("↑↓ navigate · Enter select · Esc cancel").lower()
    assert apiwatch.looks_busy("↑↓ navigate · Enter select · Esc cancel") is False


# MARK: - Handing a Freebuff run its prompt


def test_a_freebuff_composer_is_what_marks_a_run_ready_for_its_prompt():
    """The gate on typing at all. The TUI discards input that arrives before it is up
    (measured: text sent 0.3s into a launch never appears, the same text at 5s lands in
    full), so a spawn typed at on a timer loses its prompt outright — and a run that
    never received one is an agent doing nothing that still holds a bay."""
    assert apiwatch.looks_ready_for_prompt(FREEBUFF_IDLE) is True


@pytest.mark.parametrize("screen", [FREEBUFF_LOGIN_WALL, FREEBUFF_PROJECT_PICKER])
def test_the_screens_a_spawn_can_be_stuck_on_are_not_mistaken_for_a_composer(screen):
    """Both are real captures, and both are what a broken spawn actually looks like: a
    logged-out account stops at the login wall, and a working directory that is not a
    project stops at the picker. Typing the prompt at either sends a page of review
    instructions into a password screen or a filename filter."""
    assert apiwatch.looks_ready_for_prompt(screen) is False


def test_an_agent_mid_turn_is_not_ready_for_a_prompt():
    """The composer is empty while a turn runs, too. Reading that as "ready" would
    queue the whole review a second time behind the one already in flight."""
    assert apiwatch.looks_ready_for_prompt(FREEBUFF_BUSY) is False


def test_the_hand_off_can_be_claimed_once_and_only_once(tmp_path, monkeypatch):
    """What stops a run being typed into on every tick: the composer it is waiting at
    still looks exactly as ready one tick after the prompt went in, and for as long as
    the turn takes to start."""
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path))
    from diplomat_runtime import agentregistry

    agentregistry.run_dir("r1").mkdir(parents=True)
    assert agentregistry.prompt_typed("r1") is False
    assert agentregistry.claim_prompt_typed("r1") is True
    assert agentregistry.prompt_typed("r1") is True


def test_a_hand_off_that_cannot_be_recorded_is_not_sent(tmp_path, monkeypatch):
    """The claim is written BEFORE the line is sent, and a claim that fails cancels the
    send. Recording it afterwards would leave a run whose directory has gone unwritable
    being typed into on every tick for as long as its window is open."""
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path))
    from diplomat_runtime import agentregistry

    # No run directory at all — `touch` cannot create the file under a missing parent.
    assert agentregistry.claim_prompt_typed("never-staged") is False


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


# MARK: - The pass that hands a Freebuff run its prompt


def _staged(monkeypatch, tmp_path, chosen: str, screen: str, tty: str = "pts/9"):
    """One registered run under ``chosen``, its pane showing ``screen``, and a Store
    holding that pane as this tick's evidence. Returns (store, tick, sent), where
    ``sent`` collects every line the applet types."""
    from diplomat_runtime import agentregistry, agentstate, tmuxwatch
    from diplomat_app.store import Store

    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path / "agents"))
    record = agentregistry.create_run(
        RunRecord(run_id="r1", dispatched_at=0.0, pr_number=7, kind="review", tty=tty),
        "Review PR #7 in o/r")
    agentregistry.runner_path("r1").write_text(chosen, encoding="utf-8")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(tmuxwatch, "send_line_to_tty",
                        lambda t, msg: bool(sent.append((t, msg))) or True)
    store = Store()
    store._tick_tails = agentstate.Observation.present({tty: screen})
    tick = agentstate.Tick(records=[record], states={}, rows=[(record, None)],
                           cap_load=set(), retirable=[], free_slots=0)
    return store, tick, sent


def test_a_freebuff_run_is_typed_its_prompt_when_its_composer_comes_up(monkeypatch, tmp_path):
    """The half of a Freebuff spawn that the command line cannot do. Without it the
    process is up, ``ps`` sees it, it holds a bay of the task cap — and it does nothing
    at all until a human notices and pastes the task in."""
    from diplomat_runtime import agentregistry

    store, tick, sent = _staged(monkeypatch, tmp_path, runner.FREEBUFF, FREEBUFF_IDLE)
    store._hand_off_typed_prompts(tick)
    assert [t for t, _ in sent] == ["pts/9"]
    assert str(agentregistry.prompt_path("r1")) in sent[0][1]


def test_the_same_composer_on_the_next_tick_is_not_typed_into_again(monkeypatch, tmp_path):
    """The pane looks exactly as ready one tick after the line went in, and for as long
    as the turn takes to start. Typing again queues the whole review a second time."""
    store, tick, sent = _staged(monkeypatch, tmp_path, runner.FREEBUFF, FREEBUFF_IDLE)
    store._hand_off_typed_prompts(tick)
    store._hand_off_typed_prompts(tick)
    assert len(sent) == 1


@pytest.mark.parametrize("screen", [FREEBUFF_LOGIN_WALL, FREEBUFF_PROJECT_PICKER,
                                    FREEBUFF_BUSY])
def test_nothing_is_typed_at_a_run_that_is_not_at_its_composer(monkeypatch, tmp_path, screen):
    """The two screens a broken spawn parks on, and the one an agent shows while it is
    already working. Each would take the line somewhere else: a password prompt, a
    filename filter, or a queue behind the turn in flight."""
    store, tick, sent = _staged(monkeypatch, tmp_path, runner.FREEBUFF, screen)
    store._hand_off_typed_prompts(tick)
    assert sent == []


@pytest.mark.parametrize("chosen", [runner.CLAUDE, runner.OPENCODE, runner.HERMES, ""])
def test_no_other_runner_is_ever_typed_into(monkeypatch, tmp_path, chosen):
    """They were handed their prompt on the command line and have been working on it
    since. The runner is read off the RUN, not from the setting, so an operator who
    switched to Freebuff after dispatching does not have the applet interrupt an agent
    that is an hour into a review. Empty is a record from before the applet wrote a
    runner down, which is Claude Code."""
    store, tick, sent = _staged(monkeypatch, tmp_path, chosen, FREEBUFF_IDLE)
    store._hand_off_typed_prompts(tick)
    assert sent == []


def test_a_run_on_a_peer_is_never_typed_into(monkeypatch, tmp_path):
    """Its agent is a process on somebody else's machine. The tty on the record would
    be read here as one of ours, and the line would go to whatever local session
    happens to hold it."""
    from diplomat_runtime import agentstate

    store, tick, sent = _staged(monkeypatch, tmp_path, runner.FREEBUFF, FREEBUFF_IDLE)
    peer = RunRecord(**{**tick.records[0].__dict__,
                        "placement": agentstate.PLACEMENT_MESH_PEER})
    store._hand_off_typed_prompts(
        agentstate.Tick(records=[peer], states={}, rows=[(peer, None)],
                        cap_load=set(), retirable=[], free_slots=0))
    assert sent == []


def test_a_screen_that_could_not_be_read_types_nothing(monkeypatch, tmp_path):
    """An unreadable pane is "we could not look", not "not ready". Typing on it would
    send the prompt to a tty on the strength of no evidence at all."""
    from diplomat_runtime import agentstate

    store, tick, sent = _staged(monkeypatch, tmp_path, runner.FREEBUFF, FREEBUFF_IDLE)
    store._tick_tails = agentstate.Observation.unavailable("tmux would not answer")
    store._hand_off_typed_prompts(tick)
    assert sent == []


# MARK: - A run the mesh placed


def _mesh_run(here: bool) -> str:
    """Book a mesh placement the way a dispatch does, and return its run id."""
    from diplomat_runtime import agentregistry
    from diplomat_app.store import Store

    Store._track_mesh_run(Store(), "https://github.com/o/r/pull/7", 7, "mesh",
                          "review:h/o/r#7@aa", "the prompt", "node-a",
                          "review:h/o/r#7@aa", here)
    return agentregistry.load()[0].run_id


def test_a_mesh_run_that_landed_here_records_which_runner_it_is_under(opencode):
    """The node spawns through the same seam a local dispatch does, so a placement it
    ran on THIS box is an ordinary local agent — it holds a bay and the untracked scan
    finds it. With no runner on its row the probes ask no store about it and it is
    priced from ``~/.claude``, which holds no transcript of an OpenCode run at all."""
    from diplomat_runtime import agentregistry

    assert agentregistry.run_runner(_mesh_run(here=True)) == runner.OPENCODE


def test_a_mesh_run_on_a_peer_records_no_runner_at_all(opencode):
    """Its process is on another machine and our stores hold nothing about it. A
    runner written down here would point the probe at somebody else's session."""
    from diplomat_runtime import agentregistry

    assert agentregistry.run_runner(_mesh_run(here=False)) == ""
