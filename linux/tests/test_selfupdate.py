"""Self-update: the git plumbing, and the Settings button end-to-end.

Builds a throwaway "GitHub" (a local origin repo) plus a clone standing in for
the running checkout (pointed at via ``ARGENT_UTILS_SELF_REPO``), so nothing
touches the network or the real install: the synthetic origin ships stub
``build-core.sh`` / ``argent-utils`` scripts that drop marker files instead of
running swift or spawning a tray app. The E2E clicks the real Update button in
the real SettingsView (offscreen Qt) and asserts the clone fast-forwarded, the
build ran, and the relaunch fired.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from argent_utils import selfupdate  # noqa: E402


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
    (origin / "linux" / "scripts").mkdir(parents=True)
    (origin / "linux" / "scripts" / "build-core.sh").write_text(
        "#!/usr/bin/env bash\ntouch \"$MARKER_DIR/built\"\n"
    )
    (origin / "linux" / "argent-utils").write_text(
        "#!/usr/bin/env bash\ntouch \"$MARKER_DIR/relaunched\"\n"
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
    monkeypatch.setenv("ARGENT_UTILS_SELF_REPO", str(clone))
    monkeypatch.setenv("MARKER_DIR", str(marker))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Keep the E2E hermetic: SettingsView also fires the allocator check on
    # open; point it at nothing so no real Node installer runs.
    monkeypatch.setenv("ARGENT_DEVICE_ALLOCATOR_DIR", str(tmp_path / "no-allocator"))
    return origin, clone, marker


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


def test_update_button_pulls_builds_and_relaunches(repos):
    """The real Settings UPDATE section, driven by a real button click."""
    origin, clone, marker = repos
    _advance_origin(origin)

    from PySide6.QtWidgets import QApplication

    from argent_utils.settingsview import SettingsView
    from argent_utils.store import Store

    qapp = QApplication.instance() or QApplication([])
    store = Store()
    view = SettingsView(store)

    def pump_until(phases: tuple[str, ...], seconds: float = 30.0) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            qapp.processEvents()
            phase = (store.update_state or {}).get("phase")
            if phase in phases:
                return phase
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {phases}, at {store.update_state}")

    # The view kicks a check on open; it must land on "update available".
    assert pump_until(("idle",)) == "idle"
    assert store.update_state["behind"] == 1
    assert view._update_btn.isEnabled()
    assert "Update available" in view._update_status.text()

    view._update_btn.click()
    assert pump_until(("restarting", "error")) == "restarting"

    assert _git(clone, "rev-parse", "HEAD") == _git(origin, "rev-parse", "HEAD")
    assert (marker / "built").exists()
    # The relaunch is detached; give the stub launcher a beat to run.
    deadline = time.monotonic() + 10
    while not (marker / "relaunched").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (marker / "relaunched").exists()
    assert "Restarting" in view._update_status.text()

    view.deleteLater()
