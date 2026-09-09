"""Self-update: the git plumbing, and the Settings button end-to-end.

Builds a throwaway "GitHub" (a local origin repo) plus a clone standing in for
the running checkout (pointed at via ``DIPLOMAT_SELF_REPO``), so nothing
touches the network or the real install: the synthetic origin ships stub
``build-core.sh`` / ``diplomat`` scripts that drop marker files instead of
running swift or spawning a tray app. The E2E clicks the real Update button in
the real SettingsView (offscreen Qt) and asserts the clone fast-forwarded, the
build ran, and the relaunch fired.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from diplomat_app import selfupdate  # noqa: E402


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git(cwd: Path, *args: str) -> str:
    return _run(cwd, "git", *args)


def _commit_all(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.com",
        "commit", "-q", "-m", msg,
    )


def _make_origin(tmp_path: Path) -> Path:
    """A fake upstream whose build/launch scripts just drop marker files."""
    origin = tmp_path / "origin"
    linux_pkg = origin / "packages" / "diplomat-platform" / "linux"
    (linux_pkg / "install").mkdir(parents=True)
    (linux_pkg / "install" / "build-core.sh").write_text(
        "#!/usr/bin/env bash\ntouch \"$MARKER_DIR/built\"\n"
    )
    # Stays up, as a launcher that exits inside the relaunch's watch window is a
    # failed relaunch; the pid it records is what the fixture ends.
    (linux_pkg / "diplomat").write_text(
        "#!/usr/bin/env bash\necho $$ > \"$MARKER_DIR/relaunched\"\nexec sleep 10\n"
    )
    (origin / "VERSION").write_text("1\n")
    _git(origin, "init", "-q", "-b", "main")
    _commit_all(origin, "v1")
    return origin


def _advance_origin(origin: Path) -> None:
    (origin / "VERSION").write_text("2\n")
    _commit_all(origin, "v2")


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """(origin, clone) with the clone claimed as the running checkout."""
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    marker = tmp_path / "markers"
    marker.mkdir()
    monkeypatch.setenv("DIPLOMAT_SELF_REPO", str(clone))
    monkeypatch.setenv("MARKER_DIR", str(marker))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Isolate the singleton pidfile so running_pid() can't see a real tray.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    # Keep the E2E hermetic: SettingsView also fires the allocator check on
    # open; point it at nothing so no real Node installer runs.
    monkeypatch.setenv("DIPLOMAT_DEVICE_ALLOCATOR_DIR", str(tmp_path / "no-allocator"))
    yield origin, clone, marker
    try:
        os.kill(int((marker / "relaunched").read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_check_reports_up_to_date_then_behind(repos):
    origin, clone, _ = repos
    s = selfupdate.check()
    assert s["error"] is None
    assert s["behind"] == 0
    assert s["branch"] == "main"
    assert s["upstream"] == "origin/main"

    _advance_origin(origin)
    s = selfupdate.check()
    assert s["behind"] == 1
    assert s["commit"] == _git(clone, "rev-parse", "--short", "HEAD")


def test_pull_fast_forwards_to_origin(repos):
    origin, clone, _ = repos
    _advance_origin(origin)
    new = selfupdate.pull()
    assert new == _git(origin, "rev-parse", "--short", "HEAD")
    assert (clone / "VERSION").read_text() == "2\n"


def test_pull_refuses_local_changes(repos):
    origin, clone, _ = repos
    _advance_origin(origin)
    (clone / "VERSION").write_text("hacked\n")
    with pytest.raises(selfupdate.UpdateError, match="local changes"):
        selfupdate.pull()
    # Nothing was merged over the dirty file.
    assert (clone / "VERSION").read_text() == "hacked\n"


def test_check_surfaces_unreachable_remote(repos, tmp_path):
    origin, clone, _ = repos
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    s = selfupdate.check()
    assert s["error"]
    assert s["commit"]  # local facts still reported


def test_check_reports_ahead_when_diverged(repos):
    origin, clone, _ = repos
    (clone / "LOCAL.txt").write_text("mine\n")
    _commit_all(clone, "local work")
    _advance_origin(origin)
    s = selfupdate.check()
    assert s["ahead"] == 1
    assert s["behind"] == 1


def test_pull_merges_diverged_local_commit(repos):
    """A local commit origin doesn't have no longer blocks the update — it merges.

    This is the exact case that made the button 'fail' under the old --ff-only.
    """
    origin, clone, _ = repos
    (clone / "LOCAL.txt").write_text("mine\n")  # touches a different file → no conflict
    _commit_all(clone, "local work")
    _advance_origin(origin)

    new = selfupdate.pull()

    # Both sides survive: upstream's change and the local commit.
    assert (clone / "VERSION").read_text() == "2\n"
    assert (clone / "LOCAL.txt").read_text() == "mine\n"
    # HEAD is a real merge commit (two parents), not a fast-forward.
    parents = _git(clone, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3  # commit + 2 parents
    assert new == _git(clone, "rev-parse", "--short", "HEAD")


def test_pull_aborts_on_conflict_and_leaves_checkout_untouched(repos):
    """A real conflict is never auto-resolved: abort clean, keep the checkout."""
    origin, clone, _ = repos
    (clone / "VERSION").write_text("local-3\n")  # same file as upstream will change
    _commit_all(clone, "local bump")
    _advance_origin(origin)  # origin sets VERSION to "2\n" → conflicts
    before = _git(clone, "rev-parse", "HEAD")

    with pytest.raises(selfupdate.UpdateError, match="conflict"):
        selfupdate.pull()

    assert _git(clone, "rev-parse", "HEAD") == before  # HEAD unmoved
    assert (clone / "VERSION").read_text() == "local-3\n"  # our content intact
    assert _git(clone, "status", "--porcelain") == ""  # nothing half-merged


def test_run_scheduled_noop_when_current(repos):
    _origin, _clone, marker = repos
    assert selfupdate.run_scheduled() == 0
    assert not (marker / "built").exists()  # already current → no rebuild


def test_run_scheduled_updates_in_place_when_tray_not_running(repos):
    origin, clone, marker = repos
    _advance_origin(origin)
    assert selfupdate.run_scheduled() == 0
    assert _git(clone, "rev-parse", "HEAD") == _git(origin, "rev-parse", "HEAD")
    assert (marker / "built").exists()
    assert not (marker / "relaunched").exists()  # no GUI spawned on a dead session


def test_run_scheduled_relaunches_a_running_tray(repos, monkeypatch):
    origin, clone, marker = repos
    _advance_origin(origin)
    # Pretend a tray is live: claim the singleton pidfile with this process' PID, and
    # satisfy the identity gate — running_pid() now verifies the recorded pid is a GUI
    # tray of the applet, not merely alive (a recycled stale pid must not count).
    from diplomat_app import singleton
    from diplomat_app.singleton import _pidfile

    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == os.getpid())
    _pidfile().write_text(str(os.getpid()))

    assert selfupdate.run_scheduled() == 0
    assert (marker / "built").exists()
    deadline = time.monotonic() + 10  # relaunch is detached; let the stub run
    while not (marker / "relaunched").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (marker / "relaunched").exists()


def test_run_scheduled_reports_a_relaunched_applet_that_dies(repos, monkeypatch):
    """A launcher that cannot start the applet (no PySide6 on its interpreter, a
    checkout that no longer imports) exits at once; that is a failed update, not
    a "relaunched running tray"."""
    origin, clone, marker = repos
    launcher = origin / "packages" / "diplomat-platform" / "linux" / "diplomat"
    launcher.write_text("#!/usr/bin/env bash\nexit 3\n")
    _advance_origin(origin)  # the pull brings the broken launcher over
    from diplomat_app import singleton
    from diplomat_app.singleton import _pidfile

    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == os.getpid())
    _pidfile().write_text(str(os.getpid()))

    assert selfupdate.run_scheduled() == 1
    assert (marker / "built").exists()  # the update itself landed


def test_run_scheduled_reports_a_relaunched_applet_that_exits_cleanly(repos, monkeypatch, tmp_path):
    """The launcher execs the applet, so its exit is the applet's: one that exited 0
    inside the window ended without taking over the tray, exactly as one that
    crashed did. A clean exit is a failed swap, not a hand-off."""
    origin, clone, marker = repos
    launcher = origin / "packages" / "diplomat-platform" / "linux" / "diplomat"
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    _advance_origin(origin)
    from diplomat_app import singleton
    from diplomat_app.singleton import _pidfile

    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == os.getpid())
    _pidfile().write_text(str(os.getpid()))

    assert selfupdate.run_scheduled() == 1
    log = (tmp_path / "state" / "diplomat" / "autoupdate.log").read_text()
    assert "the relaunched applet exited 0" in log


def test_relaunch_failure_is_any_exit_inside_the_window():
    """Every exit code inside the window is reported, 0 included; only a child still
    running at the end of it is the applet coming up."""
    ended = subprocess.Popen(["true"])
    assert selfupdate.relaunch_failure(ended) == 0
    running = subprocess.Popen(["sleep", "30"])
    try:
        assert selfupdate.relaunch_failure(running, window=0.3) is None
    finally:
        running.kill()
        running.wait()


def test_run_scheduled_survives_a_subprocess_timeout(repos, monkeypatch):
    """A black-holed network (git fetch timeout) or a hung swift build raises
    subprocess.TimeoutExpired — NOT an UpdateError — inside pull()/build_core().
    run_scheduled must catch it and return an exit code (its documented 'never raises'
    contract), not let the traceback abort the headless 6AM job."""
    import subprocess

    monkeypatch.setattr(selfupdate, "check", lambda: {
        "error": None, "behind": 1, "ahead": 0, "commit": "abc", "upstream": "origin/main"})
    monkeypatch.setattr(selfupdate, "pull", lambda: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=120)))
    assert selfupdate.run_scheduled() == 0  # pull timeout → skip, exit 0 (no raise)

    # A build timeout is a build failure → exit 1, still no raise, no stuck state.
    monkeypatch.setattr(selfupdate, "pull", lambda: "abc123")
    monkeypatch.setattr(selfupdate, "build_core", lambda: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd=["bash", "build-core.sh"], timeout=1800)))
    assert selfupdate.run_scheduled() == 1


def test_run_scheduled_survives_a_relaunch_failure(repos, monkeypatch):
    """run_scheduled is documented "never raises". relaunch() re-raises an OSError (a
    read-only or full XDG_STATE_HOME on the log open, a missing bash) as UpdateError;
    the scheduled path must catch it exactly like it already catches pull()/build_core()
    failures. The update itself landed on disk, so a relaunch failure is logged and
    returns an exit code — never a traceback out of the headless 6AM job."""
    origin, clone, marker = repos
    _advance_origin(origin)
    from diplomat_app import singleton
    from diplomat_app.singleton import _pidfile

    # Pretend a GUI tray is live (so run_scheduled takes the relaunch branch).
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == os.getpid())
    _pidfile().write_text(str(os.getpid()))
    monkeypatch.setattr(selfupdate, "relaunch", lambda *a, **k: (_ for _ in ()).throw(
        selfupdate.UpdateError(
            "could not relaunch the applet: [Errno 30] Read-only file system")))

    assert selfupdate.run_scheduled() == 1   # relaunch failed → exit 1, no raise
    assert (marker / "built").exists()       # the merge+build still landed on disk


def test_relaunch_does_not_inherit_headless_markers(tmp_path, monkeypatch):
    """relaunch() must launch a GUI tray. The 6AM job runs with DIPLOMAT_SELF_UPDATE=1 in
    its env, and a copied env would make the relaunched child re-enter __main__.main's
    headless updater (find itself up-to-date, exit) instead of the GUI — so newest-wins
    never swaps the applet onto the new code. Every marker the singleton spares a
    process for must be stripped from the child env, under the legacy prefix too, or
    the next newest-wins spares the tray it started; the display env is still handed
    through."""
    from diplomat_app import singleton

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))   # isolate the relaunch log write
    for marker in singleton.headless_markers():
        monkeypatch.setenv(marker, "1")
    captured = {}

    def fake_popen(*a, **k):
        captured["env"] = dict(k.get("env", {}))
        raise RuntimeError("stop-before-spawn")

    monkeypatch.setattr(selfupdate.subprocess, "Popen", fake_popen)
    raised = False
    try:
        selfupdate.relaunch({"DISPLAY": ":0"})
    except RuntimeError:
        raised = True
    assert raised and "env" in captured
    env = captured["env"]
    assert "DIPLOMAT_AGENTS" in singleton.headless_markers()
    assert not set(env) & singleton.headless_markers()
    assert env.get("DISPLAY") == ":0"   # the display env is still handed through


def _settings_view():
    """The real Settings screen over a real Store, with the Qt loop to drive it."""
    from PySide6.QtWidgets import QApplication

    from diplomat_app.settingsview import SettingsView
    from diplomat_app.store import Store

    qapp = QApplication.instance() or QApplication([])
    store = Store()
    return qapp, store, SettingsView(store)


def _pump_until(qapp, store, ready, what: str, seconds: float = 30.0) -> None:
    """Pump the Qt loop until ``ready()``.

    The worker thread assigns ``store.update_state`` and only then emits
    ``update_changed``, which reaches the view queued: a phase readable off
    the store is not one the view has drawn. So a widget is waited for, never
    read once the phase has landed — and since the slot runs to completion
    inside one ``processEvents``, reaching one of its widgets is reaching all
    of them. That lag is a turn of the loop, hence the short deadline on the
    widget waits; the ones on a phase cover a real ``git fetch``.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if ready():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}, at {store.update_state}")


def _click_update(qapp, store, view) -> str:
    """Wait for the check the view kicks on open to land on "update available", click
    Update, and return the phase the update ended in."""
    def phase() -> str | None:
        return (store.update_state or {}).get("phase")

    _pump_until(qapp, store, lambda: phase() == "idle", "the update check to land")
    assert store.update_state["behind"] == 1
    _pump_until(qapp, store, lambda: view._update_btn.isEnabled(),
                "the Update button to enable", 5.0)
    assert view._update_pill.text() == "1 behind"

    view._update_btn.click()
    _pump_until(qapp, store, lambda: phase() in ("restarting", "error"),
                "the update to finish")
    return phase()


def test_update_button_pulls_builds_and_relaunches(repos):
    """The real Settings UPDATE section, driven by a real button click."""
    origin, clone, marker = repos
    _advance_origin(origin)
    qapp, store, view = _settings_view()

    assert _click_update(qapp, store, view) == "restarting", store.update_state

    assert _git(clone, "rev-parse", "HEAD") == _git(origin, "rev-parse", "HEAD")
    assert (marker / "built").exists()
    # The relaunch is detached; give the stub launcher a beat to run.
    deadline = time.monotonic() + 10
    while not (marker / "relaunched").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (marker / "relaunched").exists()
    _pump_until(qapp, store, lambda: view._update_pill.text() == "restarting…",
                "the view to show the handover", 5.0)
    assert "handing over" in view._update_row.summary()

    view.deleteLater()


def test_update_button_reports_a_relaunched_applet_that_dies(repos):
    """The button watches its relaunch the way the 6AM job does: a launcher that
    exits inside the window is a failed update and is shown as one, rather than
    "restarting…" outliving a handover that never comes."""
    origin, clone, marker = repos
    launcher = origin / "packages" / "diplomat-platform" / "linux" / "diplomat"
    launcher.write_text("#!/usr/bin/env bash\nexit 3\n")
    _advance_origin(origin)
    qapp, store, view = _settings_view()

    assert _click_update(qapp, store, view) == "error", store.update_state
    assert store.update_state["error"] == \
        "the relaunched applet exited 3; this one is still the old build"
    _pump_until(qapp, store, lambda: "exited 3" in view._update_row.summary(),
                "the view to show the failure", 5.0)

    view.deleteLater()


def test_autoupdate_unit_spares_the_relaunched_tray_from_cgroup_teardown():
    """The 6AM systemd oneshot relaunches a running tray (run_scheduled -> relaunch, a
    detached subprocess.Popen(start_new_session=True)) so it swaps onto the freshly built
    code via acquire_newest_wins. setsid does NOT move that child out of the unit's cgroup,
    so under the DEFAULT KillMode=control-group systemd SIGTERM/SIGKILLs it the instant the
    oneshot deactivates — killing the relaunched tray mid-startup and silently defeating the
    update swap (the on-disk merge+build lands, but the live tray never moves to new code).
    The generated unit must set KillMode=process (kill only the exited main process, spare
    the detached tray), and must NOT use RemainAfterExit (which would leave the oneshot
    'active (exited)' and make the daily timer's `start` a no-op — no more updates).

    NOTE: the systemd cgroup teardown is documented, deterministic behavior but cannot be
    exercised on non-systemd CI/dev (macOS launchd has no cgroups and is unaffected), so
    this guards the generated unit's content — the fix — rather than the live kill."""
    script = (Path(__file__).resolve().parents[1] / "install" / "install-autoupdate.sh").read_text()
    # Inspect actual directive lines only — skip '#' comments (the installer's own comment
    # explains why NOT to use RemainAfterExit, and must not itself trip the check).
    directives = [ln.strip() for ln in script.splitlines() if not ln.strip().startswith("#")]
    assert "Type=oneshot" in directives
    assert "KillMode=process" in directives, "the oneshot must spare the detached relaunched tray"
    assert not any(ln.startswith("RemainAfterExit") for ln in directives), \
        "RemainAfterExit would make the daily timer a no-op"
