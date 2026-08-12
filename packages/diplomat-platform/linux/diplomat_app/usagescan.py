"""Where the token half of the telemetry comes from: Claude Code's own transcripts.

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

    The link is the prompt itself. Every agent is launched as
    ``claude "$(cat <staged prompt>)"``, so the transcript's opening user message
    is that prompt verbatim — an exact identity, needing no new CLI flag on the
    spawn path (where a wrong guess would break the applet's actual job, not just
    its bookkeeping) and no guessing at how Claude Code mangles a cwd into a
    directory name.

    Only transcripts touched during the agent's life are opened, and only their
    first few lines until one matches, so the search is bounded by how many
    sessions ran alongside it. Returning None is normal and expected — the applet
    restarting mid-agent loses the prompt — and the screen reports those as
    unattributed rather than pretending they were free.
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
