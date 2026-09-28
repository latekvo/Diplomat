"""Where the token half of the telemetry comes from: the agents' own transcripts.

Claude Code's are read off disk and are most of this module; an OpenCode run keeps
its own elsewhere, and "The other runner's transcript" below is where it is priced.

Claude Code appends every turn to ``~/.claude/projects/<munged-cwd>/<session>.jsonl``
with a ``usage`` block, and stamps each record with the ``cwd`` it ran in. That is
enough to answer both token questions the Telemetry screen asks, and neither needs
anything Anthropic doesn't already write to disk:

* **how much of this machine's spend went on the monitored repo** — sum every
  turn, split by whether its ``cwd`` is inside the repo the agents work in;
* **what one auto-task cost** — find the transcript whose opening user message *is*
  the prompt we staged for that agent, and sum that file alone.

Two rules keep this cheap enough to run on a monitor poll:

* **cursors, not rescans.** Transcripts are append-only, so the scanner remembers a
  byte offset per file and reads only what has been added since. The totals it
  returns are cumulative counters; the ledger stores them per sample and the screen
  takes differences (see :mod:`telemetry`).
* **the first scan reads nothing.** A machine can hold gigabytes of transcripts, and
  reading them all would hang the poll that triggered it — for history that predates
  the ledger and can never be attributed to a task anyway. So an unseen file that is
  older than our first scan is seeded at EOF; only what happens from now on counts.

Stdlib-only and best-effort throughout: an unreadable file, a truncated line or a
missing ``HOME`` costs that file, never the poll.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .atomicjson import read_object, write_atomic

#: Token fields that count toward a rate-limit window. Cache *reads* are excluded
#: deliberately — they are huge and cheap, and counting them would swamp the signal
#: (the same three fields ``szpontnet.usage._token_cost`` sums, so a machine running
#: the mesh add-on prices its quota the same way this does).
_COST_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")


def claude_dir() -> Path:
    """Claude Code's home. ``DIPLOMAT_CLAUDE_DIR`` overrides it, which is how the
    tests point the scanner at a fixture instead of the developer's real logs."""
    override = os.environ.get("DIPLOMAT_CLAUDE_DIR")
    return Path(override) if override else Path.home() / ".claude"


def projects_dir() -> Path:
    return claude_dir() / "projects"


def cursor_path() -> Path:
    from . import activity, core

    name = "usage-cursor.json"
    try:
        name = core.telemetry().get("cursorFile", name)
    except Exception:  # noqa: BLE001 — a missing asset must not stop a poll
        pass
    return activity._dir() / name


# MARK: - What counts as this repo


def repo_roots() -> list[Path]:
    """The directories whose Claude sessions count as work on this repo.

    The checkout the agents ``cd`` into, plus its worktree siblings at
    ``<root>-worktrees/*`` — a branch worked on in a worktree is the same project
    by any honest reading, and every agent this applet dispatches through a
    worktree would otherwise land in "everything else" and make the split lie.
    """
    from . import review

    try:
        root = Path(review.repo_path()).expanduser().resolve()
    except (OSError, RuntimeError):
        return []
    return [root, root.parent / f"{root.name}-worktrees"]


def _is_repo_cwd(cwd: str, roots: list[Path]) -> bool:
    """Whether a transcript record's ``cwd`` sits under one of the repo roots.

    Compared as paths, not string prefixes: ``/x/Diplomat-old`` starts with
    ``/x/Diplomat`` and is a different project. Not resolved per record — that
    would be a syscall per line — so a session started through a symlink counts as
    "other"; the roots are resolved once, which covers the common case of a
    symlinked home.
    """
    if not cwd:
        return False
    try:
        p = Path(cwd)
    except (TypeError, ValueError):
        return False
    for root in roots:
        if p == root or root in p.parents:
            return True
    return False


# MARK: - Reading one transcript


def _token_cost(usage: dict) -> float:
    total = 0.0
    for key in _COST_FIELDS:
        try:
            total += float(usage.get(key, 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
    return total


def _usage_of(rec: dict) -> dict | None:
    """The usage block of one transcript record, wherever this Claude Code version
    puts it (nested under ``message`` for assistant turns, top-level for some
    synthetic records)."""
    message = rec.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    usage = rec.get("usage")
    return usage if isinstance(usage, dict) else None


def _scan_chunk(data: bytes, roots: list[Path]) -> tuple[float, float, int]:
    """Sum the tokens in a chunk of transcript, split repo vs other.

    Returns ``(repo, other, consumed)`` where ``consumed`` is the number of bytes
    that formed COMPLETE lines. A poll can land mid-write, so the trailing partial
    line is left for the next scan rather than parsed and lost — that is the whole
    reason the cursor advances by ``consumed`` and not by ``len(data)``.
    """
    repo = other = 0.0
    consumed = 0
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break  # partial trailing line — leave the cursor before it
        consumed += len(raw)
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        usage = _usage_of(rec)
        if usage is None:
            continue
        cost = _token_cost(usage)
        if cost <= 0:
            continue
        if _is_repo_cwd(rec.get("cwd") or "", roots):
            repo += cost
        else:
            other += cost
    return repo, other, consumed


# MARK: - Cumulative totals


@dataclass(frozen=True)
class Totals:
    """Cumulative tokens since the scanner's first run, split by project.

    Monotonic within a run of the cursor file. If that file is lost the counters
    restart at zero, which every consumer detects as a drop and treats as a
    segment boundary rather than a negative delta.
    """

    repo: float
    other: float


def totals() -> Totals:
    """Advance every transcript's cursor and return the cumulative counters.

    Safe to call on a poll: it stats each transcript and reads only appended
    bytes. Never raises.
    """
    state = read_object(cursor_path()) or {}
    files = state.get("files")
    if not isinstance(files, dict):
        files = {}
    stored = state.get("totals")
    if not isinstance(stored, dict):
        stored = {}
    repo = float(stored.get("repo") or 0.0)
    other = float(stored.get("other") or 0.0)
    # A first run has no horizon to compare against, so nothing is "new" and every
    # existing transcript is seeded at EOF.
    first_run = "scannedAt" not in state
    scanned_at = float(state.get("scannedAt") or 0.0)
    roots = repo_roots()

    root_dir = projects_dir()
    seen: set[str] = set()
    if root_dir.is_dir():
        for path in sorted(root_dir.rglob("*.jsonl")):
            key = str(path)
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            entry = files.get(key)
            if entry is None:
                # Unknown file. One that predates our first sighting is history we
                # can never attribute, so start at its end; one written since is a
                # session that began under our watch, so read it whole.
                if first_run or st.st_mtime < scanned_at:
                    files[key] = {"offset": st.st_size, "mtime": st.st_mtime}
                    continue
                entry = {"offset": 0, "mtime": 0.0}
            offset = int(entry.get("offset") or 0)
            # Truncated or replaced (a session id reused, a log rotated): the file
            # is shorter than where we stopped, so our offset points past the end
            # and every byte in it is unread. Start over rather than skip it.
            if st.st_size < offset:
                offset = 0
            if st.st_size == offset:
                files[key] = {"offset": offset, "mtime": st.st_mtime}
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read()
            except OSError:
                continue
            d_repo, d_other, consumed = _scan_chunk(data, roots)
            repo += d_repo
            other += d_other
            files[key] = {"offset": offset + consumed, "mtime": st.st_mtime}

    # Drop cursors for transcripts that are gone, so the state file tracks what is
    # on disk rather than growing forever. Deliberately NOT pruned by age: Claude
    # Code appends to an old transcript when a session is resumed, and a forgotten
    # cursor would make the scanner re-read that file from byte zero and
    # double-count everything in it.
    files = {k: v for k, v in files.items() if k in seen}

    write_atomic(cursor_path(), {
        "startedAt": state.get("startedAt") or time.time(),
        "scannedAt": time.time(),
        "totals": {"repo": repo, "other": other},
        "files": files,
    })
    return Totals(repo=repo, other=other)


# MARK: - Per-task attribution


#: How long past ``ended_at`` an agent's transcript may still have been written.
#: That bound is at or after the agent's exit and the final turn is already on disk
#: by then; the slack covers a slow flush and a clock that isn't perfectly monotonic.
_MTIME_SLACK_SECS = 600.0


@dataclass(frozen=True)
class TaskRun:
    """One finished agent, recovered from the transcript it wrote."""

    tokens: float
    #: When its last turn was written, which is seconds before the agent exits.
    #: The only exit evidence left by a run whose completion sentinel nothing kept.
    last_turn_at: float


def task_run(prompt: str, started_at: float, ended_at: float) -> TaskRun | None:
    """The agent that ran ``prompt``, or None if its transcript can't be found.

    The link is the prompt itself. A Claude Code agent is launched as
    ``claude "$(cat <staged prompt>)"``, so the transcript's opening user message
    is that prompt verbatim — an exact identity, needing no new CLI flag on the
    spawn path (where a wrong guess would break the applet's actual job, not just
    its bookkeeping) and no guessing at how Claude Code mangles a cwd into a
    directory name.

    Only transcripts touched during the agent's life are opened, and only their
    first few lines until one matches, so the search is bounded by how many
    sessions ran alongside it. Returning None is normal and expected — the applet
    restarting mid-agent loses the prompt the match needs. An OpenCode run writes no
    such transcript at all, keeping its sessions in a store of its own, and is
    priced by :func:`opencode_task_tokens`; the screen reports whatever neither can
    attribute as unattributed rather than pretending it was free.
    """
    wanted = (prompt or "").strip()
    if not wanted:
        return None
    root = projects_dir()
    if not root.is_dir():
        return None
    roots = repo_roots()
    for path, mtime in _candidates(root, started_at, ended_at):
        if _opening_prompt(path) != wanted:
            continue
        return TaskRun(tokens=_file_tokens(path, roots), last_turn_at=mtime)
    return None


def _candidates(root: Path, started_at: float,
                ended_at: float) -> list[tuple[Path, float]]:
    """Transcripts that could belong to a run spanning ``[started_at, ended_at]``
    with their mtimes, newest first. A transcript is still being appended to while
    its agent works, so its mtime lands at or after the agent's last turn — never
    before it started."""
    out: list[tuple[float, Path]] = []
    for path in root.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if started_at <= mtime <= ended_at + _MTIME_SLACK_SECS:
            out.append((mtime, path))
    out.sort(key=lambda pair: -pair[0])
    return [(p, mtime) for mtime, p in out]


#: Lines read while looking for a transcript's first user message. The session
#: header (mode, permission mode, a file-history snapshot, attachments) sits above
#: it; a couple of dozen lines is generous and bounds the cost of a non-match.
_HEADER_LINES = 40


def _opening_prompt(path: Path) -> str | None:
    """The text of a transcript's first user message, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(_HEADER_LINES):
                line = fh.readline()
                if not line:
                    return None
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "user":
                    continue
                message = rec.get("message")
                if not isinstance(message, dict):
                    return None
                return _message_text(message.get("content"))
    except OSError:
        return None
    return None


def _message_text(content: object) -> str | None:
    """A user message's text, whether Claude Code wrote it as a bare string or as
    a list of content blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip() if parts else None
    return None


def _file_tokens(path: Path, roots: list[Path]) -> float:
    """Every token in one transcript, both halves of the split summed — a task's
    cost is its cost wherever it ran."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0.0
    repo, other, _consumed = _scan_chunk(data, roots)
    return repo + other


# MARK: - The other runner's transcript


#: How long the session export may take (``opencode export`` on 1.x, ``opencode
#: session export`` on 2.x). It reads one session out of a local store, so this is
#: generous — but it runs on the poll that retires a run, and a wedged CLI must cost
#: that run its price rather than the poll.
_EXPORT_TIMEOUT = 20.0

#: How long ``opencode --version`` may take. Measured at 0.03 s on 2.0.18 and 0.5-0.8 s
#: on 1.4.3 and 1.18.33, so this only bites a CLI that is wedged, and a wedged one is
#: answered as 1.x — the spelling every install had before 2.x existed.
_VERSION_TIMEOUT = 5.0

#: The first dotted triple in the version output: ``opencode v2.0.18`` on 2.x, a bare
#: ``1.18.33`` on 1.x.
_VERSION = re.compile(r"(\d+)\.\d+\.\d+")

#: How long the user's shell may take to say where the CLI is. It sources their profile
#: and rc, which can be slow — a version manager, a prompt framework — so it gets its own
#: budget rather than the export's, and the two together bound the pricing path.
_RESOLVE_TIMEOUT = 10.0

#: How long a resolved install is trusted before the shell is asked again. The usual
#: 1.x → 2.x upgrade MOVES the binary (``~/.opencode/bin`` or Homebrew to npm's
#: ``@opencode/cli``), so a path remembered for good would keep answering for a CLI
#: that has since been replaced — and every spawn after it would take the wrong
#: version's command. A path that no longer exists is asked again at once.
_RESOLVE_TTL = 60.0

#: What the user's shell is asked, in one pass: where ``opencode`` is, and where its
#: state lives. The second line is marked so an rc that prints can never be mistaken
#: for it. Mirrored byte for byte by ``OpenCodeCLI`` in ``diplomat-core``.
_SHELL_PROBE = ("command -v opencode; "
                "printf '\\n@@XDG_STATE_HOME=%s\\n' \"$XDG_STATE_HOME\"")
_STATE_MARKER = "@@XDG_STATE_HOME="


@dataclass(frozen=True)
class OpenCodeInstall:
    """The ``opencode`` a spawned agent would run, and the state directory its shell
    gives it — where a 2.x service writes ``opencode/service.json``."""

    binary: str
    state_home: str
    resolved_at: float


#: The last resolution that found a binary. A miss is never remembered: a CLI
#: installed while the applet runs is found by the next caller.
_install: OpenCodeInstall | None = None

#: ``--version`` answers, keyed on the binary's identity on disk — path, inode, size
#: and mtime — so an upgrade in place is a new key and a steady state costs a stat. A
#: binary that could not answer — a non-zero exit, a timeout — is not remembered.
_majors: dict[tuple, int] = {}


def _reset_cache() -> None:
    """Forget where the CLI was and what it said it was. For tests, which stand a
    different one up per case."""
    global _install

    _install = None
    _majors.clear()


def opencode_install() -> OpenCodeInstall | None:
    """The ``opencode`` executable, found the way the spawn finds it, with the state
    directory the agent's shell gives it.

    An agent runs in a terminal whose shell is the user's LOGIN shell, and inside it
    Diplomat's own interactive one (:func:`review.shell_command`). So that is what is
    asked: ``<shell> -l -c '<shell> -i -c "<probe>"'`` — the login pass for a PATH set
    only in a profile (Homebrew's ``~/.zprofile`` on Apple silicon), the interactive
    one for a PATH set only in an rc (nvm's ``~/.bashrc``). The macOS front-end asks
    the identical question, so the two platforms cannot resolve different binaries.

    The shell first, and this process's ``PATH`` only when the shell names nothing:
    this process's environment is whatever launched the applet — a desktop entry, a
    Dock icon — and an rc can put a different install ahead of anything on it (a 1.x
    under ``~/.opencode/bin`` beside a 2.x on the system ``PATH``).

    The state directory comes from the same shell because the service is started by
    the agent's shell, under THAT shell's ``$XDG_STATE_HOME``. Empty there is
    ``~/.local/state``; a shell that did not answer at all leaves this process's.

    What comes back is a path, exec'd directly rather than through the shell, because
    the rc that put it on ``PATH`` is equally free to print a banner and the export's
    stdout has to stay parseable JSON.
    """
    global _install

    now = time.time()
    cached = _install
    if (cached is not None and now - cached.resolved_at < _RESOLVE_TTL
            and os.path.exists(cached.binary)):
        return cached
    binary, state_home = _shell_probe()
    binary = binary or shutil.which("opencode")
    if state_home is None:
        state_home = os.environ.get("XDG_STATE_HOME", "")
    if not binary:
        return None
    _install = OpenCodeInstall(
        binary=binary,
        state_home=state_home or os.path.join(os.path.expanduser("~"), ".local", "state"),
        resolved_at=now)
    return _install


def opencode_binary() -> str | None:
    """Where the ``opencode`` a spawn would run is (:func:`opencode_install`)."""
    install = opencode_install()
    return install.binary if install else None


def opencode_state_home() -> str:
    """The state directory an OpenCode agent's shell gives it — where a 2.x service
    keeps ``opencode/service.json``. This process's own when no ``opencode`` resolves."""
    install = opencode_install()
    if install is not None:
        return install.state_home
    return (os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "state"))


def opencode_major() -> int:
    """The major version of the ``opencode`` a spawn would run: 2 for 2.x, 1 otherwise.

    The two take different spawns, are asked what they are doing in different places
    and export a finished session under different commands, so each of those seams
    asks this first. Asked with ``--version`` (cached in :data:`_majors`), and every
    failure — no CLI, a timeout, output with no version in it — answers 1, the
    behaviour every install had before 2.x existed.
    """
    binary = opencode_binary()
    return _major(binary) if binary else 1


def opencode_is_v2() -> bool:
    """Whether the ``opencode`` a spawn would run is 2.x (:func:`opencode_major`)."""
    return opencode_major() >= 2


def _major(binary: str) -> int:
    try:
        st = os.stat(binary)
    except OSError:
        return 1
    key = (binary, st.st_ino, st.st_size, st.st_mtime_ns)
    if key in _majors:
        return _majors[key]
    try:
        out = subprocess.run(  # noqa: S603 - resolved absolute path, no shell
            [binary, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return 1
    if out.returncode != 0:
        return 1
    found = _VERSION.search(out.stdout)
    major = 2 if found is not None and int(found.group(1)) >= 2 else 1
    _majors[key] = major
    return major


def _shell_probe() -> tuple[str | None, str | None]:
    """``(where opencode is, the shell's $XDG_STATE_HOME)`` as the user's shell
    answers :data:`_SHELL_PROBE` — each ``None`` when it did not say.

    The path is the last executable one before the marker, because an rc is free to
    print above the answer; an alias or a shell function fails the test — ``command
    -v`` describes those rather than locating them — and reads the same as not
    installed. stdin is ``/dev/null``: an interactive shell that inherited a terminal
    would try to drive it.
    """
    from . import review

    shell = review.user_shell()
    inner = f"{shlex.quote(shell)} -i -c {shlex.quote(_SHELL_PROBE)}"
    try:
        out = subprocess.run(  # noqa: S603 - the user's own shell, quoted argument
            [shell, "-l", "-c", inner], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=_RESOLVE_TIMEOUT)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None, None
    lines = out.stdout.splitlines()
    marks = [i for i, line in enumerate(lines) if line.startswith(_STATE_MARKER)]
    state_home = lines[marks[-1]][len(_STATE_MARKER):].strip() if marks else None
    for line in reversed(lines[:marks[-1]] if marks else lines):
        path = line.strip()
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path, state_home
    return None, state_home


def opencode_task_tokens(session_id: str) -> float | None:
    """Tokens spent by one OpenCode session, or None if it cannot be read.

    An OpenCode run leaves nothing in ``~/.claude``, so :func:`task_run` cannot see
    it and every such run used to land in the ledger unpriced. Its own transcript is
    reachable through the CLI's session export — ``opencode export <id>`` on 1.x,
    ``opencode session export <id>`` on 2.x, which has no top-level ``export`` (and
    1.x no ``session export``) — asked for rather than read off disk: the store behind
    it is an internal SQLite schema, while the command is part of the CLI's published
    surface and already knows where the store lives. Both print ``{"info": …,
    "messages": [...]}`` with each message's tokens in the one shape the sum reads.

    Read at retirement, not on the poll — a turn's price is per-message, so a run's is
    a sum over every message it produced, and the live probe
    (:mod:`diplomat_runtime.opencodeapi`) deliberately fetches one. By then a 1.x run's
    own server is gone, which is why this goes through the CLI rather than the port.

    How a session's messages add up is :func:`opencodeapi.session_tokens`, shared with
    the Swift front-end so the two cannot price the same run differently.
    """
    from . import opencodeapi

    if not session_id:
        return None
    binary = opencode_binary()
    if not binary:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - resolved absolute path, no shell
            [binary, *(("session", "export") if _major(binary) >= 2 else ("export",)),
             session_id],
            capture_output=True, text=True, timeout=_EXPORT_TIMEOUT)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if out.returncode != 0:
        return None
    try:
        session = json.loads(out.stdout)
    except ValueError:
        return None
    messages = session.get("messages") if isinstance(session, dict) else None
    if not isinstance(messages, list):
        return None
    return opencodeapi.session_tokens(messages)
