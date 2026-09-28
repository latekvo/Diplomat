"""Bridge to the ``diplomat-core`` Swift CLI — the single source of truth for prompt
assembly.

The Review/Issues/Conflicts/Audit prompts are built by ``DiplomatCore`` (the same code
the macOS app uses). The Linux applet shells out to the compiled ``diplomat-core``
binary instead of re-implementing that logic in Python, so the two front-ends can
never drift. Build the binary with ``packages/diplomat-platform/linux/install/build-core.sh``.
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
        "diplomat-core not found — run packages/diplomat-platform/linux/install/build-core.sh "
        "(or set DIPLOMAT_CORE_BIN)."
    )


#: Tells ``diplomat-core`` which OpenCode major is installed (``1`` or ``2``), so the
#: model attribution it builds into a prompt need not resolve and ask the CLI itself.
OPENCODE_MAJOR_ENV = "DIPLOMAT_OPENCODE_MAJOR"


def opencode_major_env() -> dict[str, str]:
    """``{DIPLOMAT_OPENCODE_MAJOR: "1" | "2"}`` while OpenCode is the selected runner,
    else nothing.

    Every prompt build is a fresh ``diplomat-core`` process, so an answer it found for
    itself would be a shell resolve and a ``--version`` per build — on the Qt thread
    for a wizard spawn, under the poll lock for an automatic one. This process keeps
    the answer (:func:`usagescan.opencode_major`), and hands it over.
    """
    from . import runner, usagescan

    if runner.selected() != runner.OPENCODE:
        return {}
    return {OPENCODE_MAJOR_ENV: str(usagescan.opencode_major())}


def build_prompt(config: dict) -> str:
    """Assemble a prompt by shelling out to diplomat-core. ``config`` is the JSON
    payload whose ``kind`` is ``review`` | ``conflicts`` | ``audit``."""
    binary = core_bin()
    env = dict(os.environ)
    env.setdefault("DIPLOMAT_CORE", str(core.assets_dir()))
    env.update(opencode_major_env())
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
