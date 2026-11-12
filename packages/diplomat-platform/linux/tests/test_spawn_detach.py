"""The detached-launch contract the agent spawner keeps.

An agent is launched fire-and-forget by the applet's terminal spawner, which wants
two properties from ``Popen``, both load-bearing rather than cosmetic:

* ``start_new_session=True`` - the agent must outlive the applet that spawned it.
  Sharing the parent's process group means a tray quit SIGTERMs a review mid-flight.
* stdio pinned to ``DEVNULL`` - the parent's stdin/stdout may be a closed pipe, a
  tty it no longer owns, or the journal; a child inheriting it can block on a full
  pipe or scribble over the parent's own output.

Dropping either kwarg fails silently - the child still starts, and only dies later,
under a signal or a full pipe - so the contract is asserted here.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from diplomat_runtime import review

# ``conftest.no_host_agent_spawn`` replaces this with a refusing stub, because a test
# that reaches it *unstubbed* turns a stub prompt loose in the operator's real
# checkout. These tests are about that very function and stub ``Popen`` underneath it
# instead, so they hold the real callable - captured at import, before the autouse
# fixture swaps the module attribute.
_real_spawn = review.spawn


@pytest.fixture
def popen_spy(monkeypatch):
    """Record every ``Popen`` call instead of starting a real process. Returns the
    call list."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(review.subprocess, "Popen", fake_popen)
    return calls


# ---- the contract ---------------------------------------------------------


def test_detached_launch_starts_its_own_session(popen_spy):
    review.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["start_new_session"] is True


def test_detached_launch_inherits_no_stdio(popen_spy):
    review.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_detached_launch_passes_argv_without_a_shell(popen_spy):
    review.popen_detached(["/bin/echo", "hi there"])
    (args, kwargs) = popen_spy[0]
    assert args[0] == ["/bin/echo", "hi there"]
    assert kwargs["shell"] is False


def test_detached_launch_can_run_a_shell_template(popen_spy):
    review.popen_detached("run --flag /tmp/p", shell=True)
    (args, kwargs) = popen_spy[0]
    assert args[0] == "run --flag /tmp/p"
    assert kwargs["shell"] is True


def test_detached_launch_replaces_the_environment_when_given(popen_spy):
    review.popen_detached(["/bin/true"], env={"ONLY": "this"})
    (_args, kwargs) = popen_spy[0]
    assert kwargs["env"] == {"ONLY": "this"}


def test_detached_launch_inherits_the_environment_by_default(popen_spy):
    review.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["env"] is None


# ---- a launch failure names the terminal ----------------------------------


def test_applet_spawner_reports_the_terminal_it_could_not_launch(monkeypatch, tmp_path):
    """``review.spawn`` names the terminal in its :class:`SpawnError`, so the
    wizard's status line says which emulator failed."""
    monkeypatch.setattr(review, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))

    def refuse(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(review, "popen_detached", refuse)
    term = review.default_terminal()

    with pytest.raises(review.SpawnError) as exc:
        _real_spawn("prompt", term)

    assert term.title in str(exc.value)


# ---- a launch that fails takes its staged prompt back ----------------------
#
# A prompt is a job's whole private brief, staged 0600 in the shared temp dir; a
# launch that never started an agent has no reader left for it.


@pytest.fixture
def staging(monkeypatch, tmp_path):
    """The temp dir prompts are staged in, and the launcher refusing to start."""
    import tempfile

    def refuse(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "popen_detached", refuse)
    return tmp_path / "tmp"


def test_the_applet_spawner_takes_back_the_prompt_it_staged(staging):
    with pytest.raises(review.SpawnError):
        _real_spawn("prompt", review.default_terminal())
    assert list(staging.iterdir()) == []


def test_the_applet_spawner_leaves_a_callers_prompt_to_the_caller(staging):
    mine = staging / "run-prompt.txt"
    mine.write_text("prompt")
    with pytest.raises(review.SpawnError):
        _real_spawn("prompt", review.default_terminal(), prompt_file=str(mine))
    assert list(staging.iterdir()) == [mine]


# ---- the agent survives the user's own rc ---------------------------------
#
# The spawner runs the agent under the user's INTERACTIVE shell so a `claude` alias
# resolves — which also hands the user's rc a chance to take the process over before
# it reaches the `-c` command. The common "start every terminal inside tmux" snippet
# does exactly that: guarded on an empty `$TMUX`, it ends in `exec tmux new-session`.
# Under a bare `$SHELL -i -c` the exec wins, the agent never starts, and the window
# shows an empty shell — while the spawn reports success and the task leaves the
# queue. Opening the session ourselves satisfies that guard.


@pytest.fixture
def with_tmux(monkeypatch):
    """A machine with tmux installed, whatever the one running the suite has."""
    real_which = review.shutil.which
    monkeypatch.setattr(review.shutil, "which",
                        lambda n: "/usr/bin/tmux" if n == "tmux" else real_which(n))


def test_the_agent_runs_under_an_interactive_shell_inside_its_own_tmux_session(
    with_tmux, monkeypatch, tmp_path
):
    """The session is ours, and the interactive shell runs *inside* it — an rc that
    hands off to tmux finds ``$TMUX`` already set and runs on to the aliases."""
    import shlex

    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_SHELL", "/usr/bin/zsh")

    argv = review.agent_argv("/tmp/p.txt", "/tmp/done")

    assert argv[:2] == ["tmux", "new-session"]
    # `new-session` takes ONE shell-command argument and hands it to `sh -c`; the
    # split is what that `sh` will do, so the whole command has to survive it intact.
    assert len(argv) == 3
    assert shlex.split(argv[2]) == [
        "/usr/bin/zsh", "-i", "-c", review.shell_command("/tmp/p.txt", "/tmp/done")
    ]


def test_a_named_session_is_the_runs_handle_on_its_own_window(
    with_tmux, monkeypatch, tmp_path
):
    """Without ``-s`` the session is auto-named and the only route back to the window is
    the tty the agent happens to be on — which a run whose pid was never adopted does
    not have. Named, the reaper computes the same name from the run id and closes the
    window with no tty at all."""
    import shlex

    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_SHELL", "/usr/bin/zsh")

    argv = review.agent_argv("/tmp/p.txt", "/tmp/done", session="diplomat-17-abc")

    assert argv[:4] == ["tmux", "new-session", "-s", "diplomat-17-abc"]
    # The command is still ONE argument after the name, or `sh -c` gets a fragment.
    assert len(argv) == 5
    assert shlex.split(argv[4]) == [
        "/usr/bin/zsh", "-i", "-c", review.shell_command("/tmp/p.txt", "/tmp/done")
    ]


def test_a_window_nobody_has_to_find_again_is_left_unnamed(with_tmux, monkeypatch,
                                                           tmp_path):
    """The provider-login wizard opens a window through the same seam and is not a run:
    nothing books it, nothing reaps it, and a name would only invite a reap to match
    it."""
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_SHELL", "/usr/bin/zsh")

    assert "-s" not in review.terminal_argv("echo hi")


def test_without_tmux_the_agent_runs_under_the_bare_interactive_shell(
    monkeypatch, tmp_path
):
    """No tmux is no session to open and no pane for :mod:`tmuxwatch` to read, so
    the shape that was there before stands rather than a broken `tmux` argv."""
    real_which = review.shutil.which
    monkeypatch.setattr(review.shutil, "which",
                        lambda n: None if n == "tmux" else real_which(n))
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_SHELL", "/usr/bin/zsh")

    assert review.agent_argv("/tmp/p.txt") == [
        "/usr/bin/zsh", "-i", "-c", review.shell_command("/tmp/p.txt")
    ]


def test_a_spawn_never_hands_the_new_session_the_spawners_own_pane(monkeypatch):
    """``tmux new-session`` refuses to nest, so a ``$TMUX`` inherited from whoever
    launched the applet would close the window on the spot."""
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4321,7")
    monkeypatch.setenv("TMUX_PANE", "%7")
    monkeypatch.setenv("DIPLOMAT_KEEP_ME", "kept")

    env = review.spawn_env()

    assert "TMUX" not in env
    assert "TMUX_PANE" not in env
    # Everything else is passed through — this replaces the child's environment.
    assert env["DIPLOMAT_KEEP_ME"] == "kept"
    assert env["PATH"] == os.environ["PATH"]


def test_the_terminal_is_handed_the_session_argv_and_a_pane_free_env(
    with_tmux, popen_spy, monkeypatch, tmp_path
):
    """End of the wire: what the emulator is actually exec'd with."""
    monkeypatch.setattr(review, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4321,7")
    term = review.default_terminal()

    _real_spawn("prompt", term)

    (args, kwargs) = popen_spy[0]
    assert args[0][0] == term.exec_name
    assert args[0][len(term.prefix) + 1:len(term.prefix) + 3] == ["tmux", "new-session"]
    assert "TMUX" not in kwargs["env"]

