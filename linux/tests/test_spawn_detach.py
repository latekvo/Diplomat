"""The detached-launch contract every agent spawner shares, on both sides of the
library boundary.

An agent is launched fire-and-forget from four places — the applet's terminal
spawner, Diplomat's answer to "run a mesh job here", the node's own
``DIPLOMAT_MESH_SPAWN`` runner, and the confined foreign runner. All four want the
same two properties from ``Popen``, and both are load-bearing rather than
cosmetic:

* ``start_new_session=True`` — the agent must outlive the applet or node that
  spawned it. Sharing the parent's process group means a tray quit (or a mesh
  node restart, which the singleton does on every start) SIGTERMs a review
  mid-flight.
* stdio pinned to ``DEVNULL`` — the parent's stdin/stdout may be a closed pipe,
  a tty it no longer owns, or the journal; a child inheriting it can block on a
  full pipe or scribble over the parent's own output.

Diplomat and SzpontNet each own a ``popen_detached``, because a library that has
to reach into its consumer to start a process is not one you can install on its
own. Dropping either kwarg from either copy fails silently — the child still
starts, and only dies later, under a signal or a full pipe — so the contract is
asserted against *both*, from one list, and a copy that drifts fails here.
"""

from __future__ import annotations

import subprocess

import pytest

from diplomat_app import review, szponthost
from szpontnet import launch, spawnjob

# ``conftest.no_host_agent_spawn`` replaces these two with a refusing stub, because
# a test that reaches them *unstubbed* turns a stub prompt loose in the operator's
# real checkout. These tests are about those very functions and stub ``Popen``
# underneath them instead, so they hold the real callables — captured at import,
# before the autouse fixture swaps the module attributes.
_real_spawn = review.spawn
_real_spawn_macos = szponthost._spawn_macos

# (name, module owning the launcher). Both modules bind ``subprocess`` themselves,
# so each is spied through its own module — which is also what proves the two are
# genuinely separate implementations rather than one aliased twice.
_LAUNCHERS = [
    pytest.param(review, id="diplomat"),
    pytest.param(launch, id="szpontnet"),
]


@pytest.fixture
def popen_spy(monkeypatch):
    """Record every ``Popen`` call, in either module, instead of starting a real
    process. Returns the shared call list."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    for module in (review, launch):
        monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    return calls


# ---- the contract, held by both copies -----------------------------------


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_starts_its_own_session(popen_spy, launcher):
    launcher.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_inherits_no_stdio(popen_spy, launcher):
    launcher.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_passes_argv_without_a_shell(popen_spy, launcher):
    launcher.popen_detached(["/bin/echo", "hi there"])
    (args, kwargs) = popen_spy[0]
    assert args[0] == ["/bin/echo", "hi there"]
    assert kwargs["shell"] is False


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_can_run_a_shell_template(popen_spy, launcher):
    launcher.popen_detached("run --flag /tmp/p", shell=True)
    (args, kwargs) = popen_spy[0]
    assert args[0] == "run --flag /tmp/p"
    assert kwargs["shell"] is True


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_replaces_the_environment_when_given(popen_spy, launcher):
    launcher.popen_detached(["/bin/true"], env={"ONLY": "this"})
    (_args, kwargs) = popen_spy[0]
    assert kwargs["env"] == {"ONLY": "this"}


@pytest.mark.parametrize("launcher", _LAUNCHERS)
def test_detached_launch_inherits_the_environment_by_default(popen_spy, launcher):
    launcher.popen_detached(["/bin/true"])
    (_args, kwargs) = popen_spy[0]
    assert kwargs["env"] is None


def test_the_two_launchers_are_not_the_same_object():
    """Anti-vacuity for the parametrisation above: if one module ever re-exported
    the other's function these tests would assert one implementation twice and go
    on passing while the second copy rotted."""
    assert review.popen_detached is not launch.popen_detached


# ---- each caller keeps its own failure translation ------------------------


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


def test_mesh_runner_failure_becomes_a_job_spawn_error(monkeypatch):
    """The node translates a launch failure into :class:`JobSpawnError`, which is
    what makes the dispatcher fail over to the next candidate node instead of
    reporting the job taken."""
    def refuse(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(launch, "popen_detached", refuse)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        launch.detached("some-runner /tmp/p", "DIPLOMAT_MESH_SPAWN")

    assert "DIPLOMAT_MESH_SPAWN" in str(exc.value)


def test_host_macos_runner_failure_becomes_a_job_spawn_error(monkeypatch, tmp_path):
    """Diplomat's own runner has to fail in the library's currency too, or a
    machine that can't open a terminal reports the job *taken* rather than
    declined, and the dispatcher never fails over."""
    def refuse(*args, **kwargs):
        raise OSError("osascript missing")

    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "popen_detached", refuse)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        _real_spawn_macos("prompt", None)

    assert "osascript" in str(exc.value)


# ---- the runners keep the detachment contract too -------------------------


def test_mesh_override_runner_is_detached(popen_spy):
    launch.detached("some-runner /tmp/p", "DIPLOMAT_MESH_SPAWN")
    (args, kwargs) = popen_spy[0]
    assert args[0] == "some-runner /tmp/p"
    assert kwargs["shell"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == subprocess.DEVNULL


def test_host_macos_runner_is_detached(popen_spy, monkeypatch, tmp_path):
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))
    _real_spawn_macos("prompt", None)
    (args, kwargs) = popen_spy[0]
    assert args[0][0] == "osascript"
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == subprocess.DEVNULL


def test_confined_runner_is_detached_under_a_scrubbed_env(popen_spy, monkeypatch, tmp_path):
    """The foreign path must keep both the detachment contract and its scrubbed
    environment — the env is the credential defence, the session is the lifetime."""
    monkeypatch.setenv("DIPLOMAT_MESH_FOREIGN_SPAWN", "sandbox {prompt_file} {result_file}")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(spawnjob, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))

    spawnjob.spawn_confined("do a thing", str(tmp_path / "r.txt"))

    (_args, kwargs) = popen_spy[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["DIPLOMAT_MESH_CONFINED"] == "1"
