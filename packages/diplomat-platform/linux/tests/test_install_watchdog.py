"""What the watchdog installers write, on both platforms, and the order the autostart
scripts stop things in.

The watchdog launches the app whenever none is up, so an installer that stops the app
while the watchdog is still loaded can have it back within the same run; the order is
checked from the scripts' text. The macOS installer is run too - it is plain bash, with
``launchctl`` stubbed - and its plist parsed, so this job checks the agent it would hand
launchd without needing launchd.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[2]
LINUX_INSTALL = PLATFORM / "linux" / "install"
MACOS_INSTALL = PLATFORM / "macos" / "install"


def _stub(bin_dir: Path, name: str, record: Path) -> None:
    """A stand-in for ``name`` that appends its argv to ``record`` and succeeds."""
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{record}"\nexit 0\n')
    stub.chmod(0o755)


def _run(script: Path, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir, record = tmp_path / "bin", tmp_path / "calls"
    for name in ("systemctl", "launchctl"):
        _stub(bin_dir, name, record)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    proc = subprocess.run(["bash", str(script), *args], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "command not found" not in proc.stderr, proc.stderr
    return proc, record.read_text() if record.exists() else ""


# ---- Linux: the systemd user units -------------------------------------------


@pytest.fixture
def linux_units(tmp_path: Path) -> tuple[str, str, str]:
    _proc, calls = _run(LINUX_INSTALL / "install-watchdog.sh", tmp_path)
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    return ((unit_dir / "diplomat-watchdog.service").read_text(),
            (unit_dir / "diplomat-watchdog.timer").read_text(), calls)


def _directives(unit: str) -> list[str]:
    return [ln.strip() for ln in unit.splitlines() if ln.strip() and not ln.startswith("#")]


def test_the_linux_service_runs_the_launcher_in_watchdog_mode(linux_units) -> None:
    service, _timer, _calls = linux_units
    d = _directives(service)
    assert "Environment=DIPLOMAT_WATCHDOG=1" in d
    exec_line = next(ln for ln in d if ln.startswith("ExecStart="))
    launcher = Path(exec_line.split("/bin/bash ", 1)[1])
    assert launcher == PLATFORM / "linux" / "diplomat" and os.access(launcher, os.X_OK)
    # The tray it launches is in this oneshot's cgroup: the default KillMode would end
    # it with the oneshot.
    assert "Type=oneshot" in d and "KillMode=process" in d
    assert not any(ln.startswith("RemainAfterExit") for ln in d)


def test_the_linux_timer_fires_every_five_minutes(linux_units) -> None:
    _service, timer, calls = linux_units
    assert "OnCalendar=*:0/5" in _directives(timer)
    assert "systemctl --user enable --now diplomat-watchdog.timer" in calls


def test_the_linux_uninstaller_removes_both_units(tmp_path: Path) -> None:
    _run(LINUX_INSTALL / "install-watchdog.sh", tmp_path)
    _proc, calls = _run(LINUX_INSTALL / "uninstall-watchdog.sh", tmp_path)
    assert not list((tmp_path / ".config" / "systemd" / "user").glob("diplomat-watchdog.*"))
    assert "systemctl --user disable --now diplomat-watchdog.timer" in calls


# ---- macOS: the launchd agent ------------------------------------------------


@pytest.fixture
def macos_agent(tmp_path: Path) -> tuple[dict, str, Path]:
    binary = tmp_path / "Diplomat.app" / "Contents" / "MacOS" / "Diplomat"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    _proc, calls = _run(MACOS_INSTALL / "install-watchdog.sh", tmp_path, str(binary))
    plist = tmp_path / "Library" / "LaunchAgents" / "com.ignacy.diplomat.watchdog.plist"
    return plistlib.loads(plist.read_bytes()), calls, binary


def test_the_macos_agent_runs_the_binary_in_watchdog_mode_every_five_minutes(macos_agent) -> None:
    agent, calls, binary = macos_agent
    assert agent["Label"] == "com.ignacy.diplomat.watchdog"
    assert agent["ProgramArguments"] == [str(binary)]
    assert agent["EnvironmentVariables"] == {"DIPLOMAT_WATCHDOG": "1"}
    assert agent["StartInterval"] == 300
    # launchd restarting the app itself is what would fight the singleton; the agent
    # must be a periodic check, never a keeper of a process.
    assert "KeepAlive" not in agent and "RunAtLoad" not in agent
    assert calls.splitlines()[-1].startswith("launchctl bootstrap gui/")
    assert calls.splitlines()[-1].endswith("com.ignacy.diplomat.watchdog.plist")


def test_the_macos_uninstaller_removes_the_agent(macos_agent, tmp_path: Path) -> None:
    _proc, calls = _run(MACOS_INSTALL / "uninstall-watchdog.sh", tmp_path)
    assert not (tmp_path / "Library" / "LaunchAgents" / "com.ignacy.diplomat.watchdog.plist").exists()
    assert "bootout gui/" in calls.splitlines()[-1]


# ---- both: the watchdog is gone before the app is stopped ---------------------


@pytest.mark.parametrize("script, unload, stop", [
    (MACOS_INSTALL / "install-autostart.sh",
     'launchctl bootout "gui/$(id -u)/com.ignacy.diplomat.watchdog"', "pkill -x Diplomat"),
    (MACOS_INSTALL / "uninstall-autostart.sh", '"$HERE/uninstall-watchdog.sh"', "pkill -x Diplomat"),
    (LINUX_INSTALL / "uninstall-autostart.sh", '"${HERE}/uninstall-watchdog.sh"', "pkill -f"),
])
def test_the_watchdog_is_unloaded_before_the_app_is_stopped(script: Path, unload: str, stop: str) -> None:
    text = script.read_text()
    assert unload in text and stop in text
    assert text.index(unload) < text.index(stop)


@pytest.mark.parametrize("script, install", [
    (MACOS_INSTALL / "install-autostart.sh", '"$HERE/install-watchdog.sh" "$BIN"'),
    (LINUX_INSTALL / "install-autostart.sh", '"${LINUX_DIR}/install/install-watchdog.sh"'),
])
def test_autostart_installs_the_watchdog(script: Path, install: str) -> None:
    assert install in script.read_text()
