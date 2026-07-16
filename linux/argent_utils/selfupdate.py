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
        "ahead": None,
        "error": None,
    }
    try:
        out["commit"] = _git(root, "rev-parse", "--short", "HEAD")
        out["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(root, "fetch", "--quiet", "origin")
        up = _upstream(root)
        out["upstream"] = up
        # left = commits only on HEAD (ahead), right = only on upstream (behind).
        ahead, behind = _git(
            root, "rev-list", "--left-right", "--count", f"HEAD...{up}"
        ).split()
        out["ahead"] = int(ahead)
        out["behind"] = int(behind)
    except (UpdateError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        out["error"] = str(exc)
    return out


def _has_git_identity(root: Path) -> bool:
    """Whether a committer name+email is configured (a merge commit needs one)."""
    for key in ("user.name", "user.email"):
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "config", "--get", key],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
    return True


def pull() -> str:
    """Integrate the checkout's upstream; returns the resulting short SHA.

    Fast-forwards when the checkout is strictly behind, and creates a merge
    commit when it has diverged (local commits origin doesn't have) — so an
    update still lands when you're *ahead*, which ``--ff-only`` refused to do.

    A *real* conflict is never resolved unattended: the merge is aborted, the
    checkout is left byte-for-byte as it was, and a readable ``UpdateError``
    says it needs a manual merge. Uncommitted local changes still block the
    merge outright — an update must never clobber work in flight.
    """
    root = repo_root()
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("checkout has local changes — commit or stash them first")
    _git(root, "fetch", "--quiet", "origin")
    up = _upstream(root)

    # A diverged checkout needs a merge commit; give the auto-merge a committer
    # identity if the environment has none (a stripped 6AM service env might),
    # but never override the user's own identity when it's configured.
    ident: list[str] = []
    if not _has_git_identity(root):
        ident = ["-c", "user.name=Argent Utils updater",
                 "-c", "user.email=argent-utils@localhost"]
    merge = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-C", str(root), *ident, "merge", "--no-edit", up],
        capture_output=True, text=True, timeout=120,
    )
    if merge.returncode != 0:
        blob = "\n".join(p for p in (merge.stdout, merge.stderr) if p).strip()
        # Leave nothing half-merged behind, whatever went wrong. Harmless
        # (just errors) if no merge was actually started.
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "merge", "--abort"],
            capture_output=True, text=True, timeout=120,
        )
        if "conflict" in blob.lower():
            raise UpdateError(
                "update conflicts with your local commits — merge origin by hand "
                "in the checkout, then update again"
            )
        last = blob.splitlines()[-1] if blob else f"exit {merge.returncode}"
        raise UpdateError(f"git merge: {last}")
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


def _state_dir() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        / "argent-utils"
    )


def relaunch(extra_env: dict[str, str] | None = None) -> None:
    """Start the updated launcher detached, logging where autostart logs.

    The new instance's newest-wins singleton terminates this process once it's
    up, so the caller only reports "restarting…" and waits to be replaced.

    ``extra_env`` is merged over the current environment for the child — the 6AM
    updater uses it to hand the relaunched GUI the display env (DISPLAY / Wayland
    / D-Bus) of the tray it's replacing, which a bare systemd service env lacks.
    """
    root = repo_root()
    launcher = root / "linux" / "argent-utils"
    log_dir = _state_dir()
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
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
                env=env,
            )
    except OSError as exc:
        raise UpdateError(f"could not relaunch the applet: {exc}") from exc


# MARK: unattended (6AM timer) path

# The display/session vars a relaunched GUI needs but a systemd/launchd service
# env doesn't carry; we lift them off the running tray's process environment.
_DISPLAY_ENV_KEYS = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
)


def _display_env_of(pid: int) -> dict[str, str]:
    """The display/session env of a running process (via ``/proc/<pid>/environ``)."""
    out: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return out
    for entry in raw.split(b"\0"):
        key, sep, val = entry.partition(b"=")
        if sep and key.decode("utf-8", "replace") in _DISPLAY_ENV_KEYS:
            out[key.decode("utf-8", "replace")] = val.decode("utf-8", "replace")
    return out


def _sched_log(message: str) -> None:
    """Append a timestamped line to the auto-update log (best-effort)."""
    from datetime import datetime

    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / "autoupdate.log").open("a") as fh:
            fh.write(line)
    except OSError:
        pass


def run_scheduled() -> int:
    """Headless daily update for the 6AM timer. Never raises; returns an exit code.

    Fetches, and if the checkout is behind, merges upstream and rebuilds
    argent-core — then relaunches the tray only if one is actually running (so it
    never pops a GUI on a session that has none). Quiet no-op when already
    current; a conflict or an unreachable origin is logged and left for a human
    rather than retried destructively.
    """
    st = check()
    if st["error"]:
        _sched_log(f"skip: cannot reach origin ({st['error']})")
        return 0
    if not st["behind"]:
        extra = f" ({st['ahead']} local ahead)" if st.get("ahead") else ""
        _sched_log(f"up to date at {st['commit']}{extra}")
        return 0

    _sched_log(f"{st['behind']} behind at {st['commit']} — merging {st['upstream']}")
    try:
        commit = pull()
    except UpdateError as exc:
        # A conflict/dirty tree is not a transient failure to hammer on; wait
        # for the user to sort it, then the next 6AM tick picks it up.
        _sched_log(f"skip: {exc}")
        return 0

    _sched_log(f"merged to {commit} — rebuilding argent-core")
    try:
        build_core()
    except UpdateError as exc:
        _sched_log(f"build failed: {exc}")
        return 1

    from .singleton import SingleInstance

    pid = SingleInstance.running_pid()
    if pid:
        relaunch(_display_env_of(pid))
        _sched_log(f"relaunched running tray (was pid {pid}) onto {commit}")
    else:
        _sched_log(f"updated to {commit} in place (tray not running)")
    return 0
