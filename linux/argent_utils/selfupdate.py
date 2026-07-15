"""Self-update: fast-forward the checkout, rebuild argent-core, relaunch.

The Linux applet runs straight out of its git checkout (the autostart .desktop
Exec points at ``linux/argent-utils``), so "update" is three steps: fast-forward
the checkout to its upstream, re-run ``scripts/build-core.sh`` to refresh the
installed argent-core binary, and start the launcher again. Relaunching is all
the "reinstall" needed — the singleton is newest-wins (see ``singleton.py``),
so the fresh instance asks the old one to quit the moment it starts.

Everything here is synchronous and shell-based; the Store wraps it in daemon
threads (``refresh_update_status_async`` / ``update_applet_async``) the same
way it wraps the device-allocator installer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class UpdateError(RuntimeError):
    """A self-update step failed; str(exc) is the user-facing reason."""


def repo_root() -> Path:
    """The checkout the running applet lives in (env-overridable for tests)."""
    env = os.environ.get("ARGENT_UTILS_SELF_REPO")
    if env:
        return Path(env)
    # This file is <repo>/linux/argent_utils/selfupdate.py, so parents[2] = <repo>.
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str, timeout: float = 120.0) -> str:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        raise UpdateError(f"git {args[0]}: {lines[-1] if lines else f'exit {proc.returncode}'}")
    return proc.stdout.strip()


def _upstream(root: Path) -> str:
    """The ref we update to: the branch's upstream, else the default branch."""
    try:
        return _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    except UpdateError:
        return "origin/main"


def check() -> dict:
    """Fetch origin and report where the checkout stands vs its upstream.

    Never raises (same contract as ``deviceallocator.check``): an unreachable
    remote still yields the local commit plus an ``error`` string.
    """
    root = repo_root()
    out: dict = {
        "root": str(root),
        "commit": None,
        "branch": None,
        "upstream": None,
        "behind": None,
        "error": None,
    }
    try:
        out["commit"] = _git(root, "rev-parse", "--short", "HEAD")
        out["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(root, "fetch", "--quiet", "origin")
        up = _upstream(root)
        out["upstream"] = up
        out["behind"] = int(_git(root, "rev-list", "--count", f"HEAD..{up}"))
    except (UpdateError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        out["error"] = str(exc)
    return out


def pull() -> str:
    """Fast-forward the checkout to its upstream; returns the new short SHA.

    Refuses on local changes or a diverged branch (``--ff-only``) — an update
    must never discard work in the checkout it runs from.
    """
    root = repo_root()
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("checkout has local changes — commit or stash them first")
    _git(root, "fetch", "--quiet", "origin")
    _git(root, "merge", "--ff-only", _upstream(root))
    return _git(root, "rev-parse", "--short", "HEAD")


def build_core() -> None:
    """Run the (freshly pulled) checkout's build-core.sh.

    Rebuilds the static argent-core CLI and installs it to
    ``~/.local/share/argent-utils/argent-core`` — the actual "reinstall" step;
    the applet's Python code needs no install, it runs from the checkout.
    """
    root = repo_root()
    script = root / "linux" / "scripts" / "build-core.sh"
    proc = subprocess.run(  # noqa: S603 — our own build script
        ["bash", str(script)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=1800,  # a cold swift release build can take a while
    )
    if proc.returncode != 0:
        lines = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
        raise UpdateError(
            f"build-core.sh: {lines[-1] if lines else f'exit {proc.returncode}'}"
        )


def relaunch() -> None:
    """Start the updated launcher detached, logging where autostart logs.

    The new instance's newest-wins singleton terminates this process once it's
    up, so the caller only reports "restarting…" and waits to be replaced.
    """
    root = repo_root()
    launcher = root / "linux" / "argent-utils"
    log_dir = (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / "argent-utils"
    )
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "argent-utils.log").open("ab") as log:
            subprocess.Popen(  # noqa: S603 — relaunch ourselves, detached
                ["bash", str(launcher)],
                cwd=str(root),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        raise UpdateError(f"could not relaunch the applet: {exc}") from exc
