"""``linux/diplomat`` picks the interpreter the applet runs on.

Four things start the applet through this script - ``szpont``'s launch step, the
XDG autostart entry, the systemd auto-update timer and the self-update relaunch -
and only the first puts the venv holding PySide6 in front of PATH. The script has
to find that venv on its own, or three of the four start an applet without Qt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "diplomat"


def stub_python(path: Path, marker: Path, tag: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{tag} $*" >> {marker}\n', encoding="utf-8")
    path.chmod(0o755)


def stamped(home: Path) -> None:
    """What szpont's deps step leaves once it has installed into the venv."""
    (home / ".diplomat" / "venv" / ".szpont-requirements").write_text("", encoding="utf-8")


def launched(home: Path) -> str:
    marker = home / "marker"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(LAUNCHER), "--dump"], check=True, timeout=60,
        env={"HOME": str(home), "PATH": f"{home / 'bin'}:/usr/bin:/bin"},
    )
    return marker.read_text(encoding="utf-8")


def test_the_venv_szpont_made_is_preferred_over_the_paths_python3(tmp_path):
    stub_python(tmp_path / ".diplomat" / "venv" / "bin" / "python3", tmp_path / "marker", "venv")
    stamped(tmp_path)
    stub_python(tmp_path / "bin" / "python3", tmp_path / "marker", "path")
    assert launched(tmp_path) == "venv -m diplomat_app --dump\n"


def test_a_venv_szpont_never_finished_is_passed_over(tmp_path):
    """Debian without python3-venv leaves bin/python3 behind and stops before pip,
    so nothing was ever installed into it - least of all PySide6."""
    stub_python(tmp_path / ".diplomat" / "venv" / "bin" / "python3", tmp_path / "marker", "venv")
    stub_python(tmp_path / "bin" / "python3", tmp_path / "marker", "path")
    assert launched(tmp_path) == "path -m diplomat_app --dump\n"


def test_a_venv_whose_interpreter_is_gone_is_passed_over(tmp_path):
    """``bin/python3`` is a symlink to the interpreter the venv was made from, and
    dangles once that one is uninstalled."""
    venv = tmp_path / ".diplomat" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(tmp_path / "gone")
    stamped(tmp_path)
    stub_python(tmp_path / "bin" / "python3", tmp_path / "marker", "path")
    assert launched(tmp_path) == "path -m diplomat_app --dump\n"


def test_without_that_venv_the_applet_runs_on_the_paths_python3(tmp_path):
    stub_python(tmp_path / "bin" / "python3", tmp_path / "marker", "path")
    assert launched(tmp_path) == "path -m diplomat_app --dump\n"
