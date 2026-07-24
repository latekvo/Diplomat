"""Bridge to the ``diplomat-core`` Swift CLI — the single source of truth for prompt
assembly.

The Review/Conflicts/Audit prompts are built by ``DiplomatCore`` (the same code
the macOS app uses). The Linux applet shells out to the compiled ``diplomat-core``
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
    """Raised when the diplomat-core binary can't be located."""


def core_bin() -> str:
    """Locate the diplomat-core binary: ``$DIPLOMAT_CORE_BIN``, then ``PATH``, then the
    XDG install location (``~/.local/share/diplomat/diplomat-core``)."""
    override = os.environ.get("DIPLOMAT_CORE_BIN")
    if override and os.path.exists(override):
        return override
    found = shutil.which("diplomat-core")
    if found:
        return found
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    candidate = data / "diplomat" / "diplomat-core"
    if candidate.exists():
        return str(candidate)
    raise CoreBinaryMissing(
        "diplomat-core not found — run linux/scripts/build-core.sh "
        "(or set DIPLOMAT_CORE_BIN)."
    )


def build_prompt(config: dict) -> str:
    """Assemble a prompt by shelling out to diplomat-core. ``config`` is the JSON
    payload whose ``kind`` is ``review`` | ``conflicts`` | ``audit``."""
    binary = core_bin()
    env = dict(os.environ)
    env.setdefault("DIPLOMAT_CORE", str(core.core_dir()))
    try:
        proc = subprocess.run(  # noqa: S603 — argv is a literal list, not a shell string
            [binary, "build-prompt"],
            input=json.dumps(config),
            capture_output=True,
            text=True,
            env=env,
            # Prompt assembly is local string work (milliseconds). A bound is mandatory:
            # this runs synchronously on the Qt UI thread (wizard "Spawn") and inside the
            # auto-fix poll worker while `_poll_lock` is held — an unbounded hang (a wedged
            # or misbuilt core binary) would freeze the tray forever or silently no-op
            # every future poll. Every other subprocess in the app is likewise timed.
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface as RuntimeError, which callers already catch (releasing the poll lock),
        # rather than letting TimeoutExpired escape a Qt slot / wedge the worker.
        raise RuntimeError("diplomat-core timed out assembling the prompt") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"diplomat-core failed: {proc.stderr.strip()}")
    return proc.stdout
