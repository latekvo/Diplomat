"""Tests for the detached-launch contract every agent spawner shares.

An agent is launched fire-and-forget from three places — the applet's terminal
spawner, the mesh's personal macOS/override runners, and the confined foreign
runner. All three want the same two properties from ``Popen``, and both are
load-bearing rather than cosmetic:

* ``start_new_session=True`` — the agent must outlive the applet or node that
  spawned it. Sharing the parent's process group means a tray quit (or a mesh
  node restart, which the singleton does on every start) SIGTERMs a review
  mid-flight.
* stdio pinned to ``DEVNULL`` — the parent's stdin/stdout may be a closed pipe,
  a tty it no longer owns, or the journal; a child inheriting it can block on a
  full pipe or scribble over the parent's own output.

All three launch paths go through one helper (:func:`review.popen_detached`),
because dropping any one of those kwargs fails silently - the child still starts,
and only dies later, under a signal or a full pipe. These pin the helper, plus
each caller's translation of a launch failure into its own error type.
"""

from __future__ import annotations

import subprocess

import pytest

from diplomat_app import review
from diplomat_app.mesh import spawnjob

# ``conftest.no_host_agent_spawn`` replaces these two with a refusing stub, because
# a test that reaches them *unstubbed* turns a stub prompt loose in the operator's
# real checkout. These tests are about those very functions and stub ``Popen``
# underneath them instead, so they hold the real callables — captured at import,
# before the autouse fixture swaps the module attributes.
_real_spawn = review.spawn
_real_spawn_macos = spawnjob._spawn_macos


@pytest.fixture
def popen_spy(monkeypatch):
    """Record every ``Popen`` call instead of starting a real process."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(review.subprocess, "Popen", fake_popen)
    return calls


# ---- the shared helper ---------------------------------------------------


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
    """The mesh translates a launch failure into :class:`JobSpawnError`, which is
    what makes the dispatcher fail over to the next candidate node instead of
    reporting the job taken."""
    def refuse(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(review, "popen_detached", refuse)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        spawnjob._detached("some-runner /tmp/p", "DIPLOMAT_MESH_SPAWN")

    assert "DIPLOMAT_MESH_SPAWN" in str(exc.value)


def test_mesh_macos_runner_failure_becomes_a_job_spawn_error(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise OSError("osascript missing")

    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "popen_detached", refuse)

    with pytest.raises(spawnjob.JobSpawnError) as exc:
        _real_spawn_macos(str(tmp_path / "p.txt"))

    assert "osascript" in str(exc.value)


# ---- the mesh runners keep the detachment contract too --------------------


def test_mesh_override_runner_is_detached(popen_spy):
    spawnjob._detached("some-runner /tmp/p", "DIPLOMAT_MESH_SPAWN")
    (args, kwargs) = popen_spy[0]
    assert args[0] == "some-runner /tmp/p"
    assert kwargs["shell"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == subprocess.DEVNULL


def test_mesh_macos_runner_is_detached(popen_spy, monkeypatch, tmp_path):
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    _real_spawn_macos(str(tmp_path / "p.txt"))
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
    monkeypatch.setattr(review, "write_prompt", lambda prompt: str(tmp_path / "p.txt"))

    spawnjob.spawn_confined("do a thing", str(tmp_path / "r.txt"))

    (_args, kwargs) = popen_spy[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["DIPLOMAT_MESH_CONFINED"] == "1"
