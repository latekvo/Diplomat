"""The durable book of dispatched agent runs — one record per run, on disk.

What this replaces, and why it had to be on disk: the Linux applet kept its
in-flight list in memory only, so every restart forgot which agents were running
while the agents themselves ran on. The panel then redrew them as anonymous
"untracked" rows, a click-spawned agent started counting against the automatic cap
because its ``source`` was gone with the record, and the telemetry ledger never
priced the run because its ledger key went too. The applet rebuilds and relaunches
itself on every update, so this was not a rare state.

Layout, under ``~/.diplomat/agents`` (``$DIPLOMAT_AGENTS_DIR`` overrides, which is
also how the tests get an isolated one):

    runs.json          the records — the book itself
    <run-id>/prompt.txt  what the agent was asked (also its transcript's first message)
    <run-id>/pid         the agent's real pid, written by the agent's own shell
    <run-id>/done        its exit code, written when it returns

The per-run directory is what makes identity exact. The shell that runs the agent
writes its own ``$$`` into ``pid`` and then ``exec``s the agent, so the pid in that
file IS the agent's — not a wrapper's, not a tmux client's (see
``review.shell_command``). Before this, a run was identified by matching
``PR #<n> in <owner>/<repo>`` against prompts in ``ps`` output, which could not tell
two runs on one PR apart and matched any unrelated session that mentioned the number.

Writes are atomic (:mod:`.atomicjson`) and guarded by a process-local lock: the poll
worker, a panel click and the free-slot sweep all reach this. Cross-process readers
(the mesh node asking whether this box has room) only ever read, and ``os.replace``
means they never see a torn file.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from . import atomicjson
from .agentstate import Observation, RunRecord

#: Bumped only if the on-disk shape changes incompatibly. A file from the future is
#: ignored rather than misread — an older applet must not act on records whose fields
#: it does not understand, and the ``ps`` fallback still covers whatever is running.
SCHEMA_VERSION = 1

_lock = threading.Lock()


def agents_dir() -> Path:
    override = os.environ.get("DIPLOMAT_AGENTS_DIR")
    return Path(override) if override else Path.home() / ".diplomat" / "agents"


def runs_path() -> Path:
    return agents_dir() / "runs.json"


def run_dir(run_id: str) -> Path:
    return agents_dir() / run_id


def prompt_path(run_id: str) -> Path:
    return run_dir(run_id) / "prompt.txt"


def pid_path(run_id: str) -> Path:
    return run_dir(run_id) / "pid"


def done_path(run_id: str) -> Path:
    return run_dir(run_id) / "done"


def new_run_id(now: float) -> str:
    """A run's identity: the dispatch second, then random.

    The timestamp leads so a directory listing sorts into dispatch order while
    debugging, and the random tail is what actually makes it unique — two jobs of one
    poll are dispatched inside the same second.
    """
    return f"{int(now)}-{uuid.uuid4().hex[:8]}"


# MARK: - The book


def load() -> list[RunRecord]:
    """Every persisted record. Empty on anything unreadable — a corrupt book must
    degrade to "this applet has forgotten", which the ``ps`` fallback still covers,
    rather than taking the applet down on startup."""
    data = atomicjson.read_object(runs_path()) or {}
    if data.get("version") != SCHEMA_VERSION:
        return []
    raw = data.get("runs")
    if not isinstance(raw, list):
        return []
    return [RunRecord.from_json(r) for r in raw if isinstance(r, dict) and r.get("runId")]


def save(records: list[RunRecord]) -> None:
    """Replace the book with ``records``."""
    with _lock:
        atomicjson.write_atomic(
            runs_path(),
            {"version": SCHEMA_VERSION, "runs": [r.to_json() for r in records]})


def add(record: RunRecord) -> None:
    """Append one run, read-modify-write under the lock.

    Under the lock for the whole cycle, not just the write: a spawn registering
    against a list that a concurrent sweep has already copied would be dropped,
    leaving an agent that nothing counts — a bay of the cap the machine can then
    spend twice.
    """
    with _lock:
        data = atomicjson.read_object(runs_path()) or {}
        runs = data.get("runs") if data.get("version") == SCHEMA_VERSION else None
        runs = list(runs) if isinstance(runs, list) else []
        runs.append(record.to_json())
        atomicjson.write_atomic(runs_path(), {"version": SCHEMA_VERSION, "runs": runs})


# MARK: - The per-run directory


def create_run(record: RunRecord, prompt: str) -> RunRecord:
    """Stage a run's directory and register it. Returns the record as stored.

    The prompt is written here rather than to a temp file because it is what ties the
    run back to its Claude transcript when it finishes (``usagescan.task_tokens`` —
    the transcript's opening user message IS this text), and a run directory that
    outlives ``/tmp`` cleanup keeps that link.
    """
    d = run_dir(record.run_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        prompt_path(record.run_id).write_text(prompt, encoding="utf-8")
        # The prompt can name a private repo and quote its contents; /tmp and $HOME
        # are both readable by other local users under a default umask.
        os.chmod(prompt_path(record.run_id), 0o600)
    except OSError:
        pass
    add(record)
    return record


def adopt_pids(records: list[RunRecord]) -> list[RunRecord]:
    """Fill in the pid of every run whose shell has written one since we last looked.

    A run is `starting` until this succeeds, so the read happens every tick until it
    does. A malformed or absent file leaves the pid unset, which keeps the run in the
    spawn grace rather than declaring anything about it.
    """
    out = []
    for r in records:
        if r.pid is not None or r.untracked:
            out.append(r)
            continue
        pid = _read_pid(r.run_id)
        out.append(r if pid is None else RunRecord(**{**r.__dict__, "pid": pid}))
    return out


def _read_pid(run_id: str) -> int | None:
    try:
        raw = pid_path(run_id).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def sentinels(records: list[RunRecord]) -> Observation:
    """The run ids whose agent has written its exit code.

    Always PRESENT: this reads our own directory, and a run whose sentinel is absent
    is positively "has not exited yet" rather than unknown. An unreadable directory
    would make every run look unfinished, which is the safe direction — the pid probe
    is what actually ends a local run, and this only ever ends one earlier.
    """
    found = set()
    for r in records:
        try:
            if done_path(r.run_id).exists():
                found.add(r.run_id)
        except OSError:
            continue
    return Observation.present(found)


def finished_at(run_id: str) -> float | None:
    """When the agent actually exited, from the sentinel's mtime.

    Not "when a poll got round to noticing", which is up to a poll period later and
    would inflate every recorded run time by a random few minutes.
    """
    try:
        return done_path(run_id).stat().st_mtime
    except OSError:
        return None


def forget(run_ids: set[str]) -> None:
    """Drop these runs from the book and delete their directories.

    Called with what the resolver found retirable — positive evidence the agent ended
    — never on a timer.
    """
    if not run_ids:
        return
    save([r for r in load() if r.run_id not in run_ids])
    for run_id in run_ids:
        _remove_dir(run_dir(run_id))


def _remove_dir(d: Path) -> None:
    try:
        for child in d.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        d.rmdir()
    except OSError:
        pass
