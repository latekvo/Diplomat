"""Bridge to the ``co-maintainer-core`` Swift CLI — the single source of truth for prompt
assembly.

The Review/Conflicts/Audit prompts are built by ``CoMaintainerCore`` (the same code
the macOS app uses). The Linux applet shells out to the compiled ``co-maintainer-core``
binary instead of re-implementing that logic in Python, so the two front-ends can
never drift. Build the binary with ``linux/scripts/build-core.sh``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import core


class CoreBinaryMissing(RuntimeError):
    """Raised when the co-maintainer-core binary can't be located."""


def core_bin() -> str:
    """Locate the co-maintainer-core binary: ``$CO_MAINTAINER_CORE_BIN``, then ``PATH``, then the
    XDG install location (``~/.local/share/co-maintainer/co-maintainer-core``)."""
    override = os.environ.get("CO_MAINTAINER_CORE_BIN")
    if override and os.path.exists(override):
        return override
    found = shutil.which("co-maintainer-core")
    if found:
        return found
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    candidate = data / "co-maintainer" / "co-maintainer-core"
    if candidate.exists():
        return str(candidate)
    raise CoreBinaryMissing(
        "co-maintainer-core not found — run linux/scripts/build-core.sh "
        "(or set CO_MAINTAINER_CORE_BIN)."
    )


def build_prompt(config: dict) -> str:
    """Assemble a prompt by shelling out to co-maintainer-core. ``config`` is the JSON
    payload whose ``kind`` is ``review`` | ``conflicts`` | ``audit``."""
    binary = core_bin()
    env = dict(os.environ)
    env.setdefault("CO_MAINTAINER_CORE", str(core.core_dir()))
    proc = subprocess.run(  # noqa: S603 — argv is a literal list, not a shell string
        [binary, "build-prompt"],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"co-maintainer-core failed: {proc.stderr.strip()}")
    return proc.stdout
