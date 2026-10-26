"""pytest's handle on ``full-volume.sh``: a real filesystem a test fills to ENOSPC.

Shared by every Python suite that tests a process surviving a full disk. Import the
fixture into a test module (``from full_volume import full_volume``) after putting
this directory on ``sys.path``; it yields a :class:`FullVolume` mounted on a fresh
empty directory and unmounts it however the test ends.

A machine that cannot mount one (exit 77) skips, unless
``DIPLOMAT_FULL_VOLUME_REQUIRED=1`` — CI sets it, so a runner that silently lost the
ability fails rather than going green on nothing.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "full-volume.sh"


class FullVolume:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True,
                              timeout=300)

    def mount(self, size: str) -> None:
        proc = self._run("mount", str(self.path), size)
        if proc.returncode == 77:
            pytest.skip(f"no full-volume backend here: {proc.stderr.strip()}")
        assert proc.returncode == 0, proc.stderr

    def fill(self) -> None:
        proc = self._run("fill", str(self.path))
        assert proc.returncode == 0, proc.stderr

    def drain(self) -> None:
        proc = self._run("drain", str(self.path))
        assert proc.returncode == 0, proc.stderr

    def unmount(self) -> None:
        self._run("unmount", str(self.path))

    def leftovers(self) -> list[str]:
        """Every ``*.tmp`` under the volume, as paths relative to it."""
        return sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*.tmp"))


@pytest.fixture
def full_volume(tmp_path_factory):
    """An empty 16 MB volume, mounted for the test and gone after it.

    Its mount point is a fresh directory under the session's temp root rather than
    ``tmp_path``, which a test is free to fill with things of its own.
    """
    root = tmp_path_factory.getbasetemp()
    vol = FullVolume(root / f"vol-{uuid.uuid4().hex[:8]}")
    vol.mount("16m")
    try:
        yield vol
    finally:
        vol.unmount()
