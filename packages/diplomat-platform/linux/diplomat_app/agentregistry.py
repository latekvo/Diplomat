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
    <run-id>/runner      which agent CLI was spawned into it
    <run-id>/port        the loopback port its OpenCode server answers on
    <run-id>/session     which of that runner's sessions turned out to be this run's

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


def runner_path(run_id: str) -> Path:
    return run_dir(run_id) / "runner"


def port_path(run_id: str) -> Path:
    return run_dir(run_id) / "port"


def session_path(run_id: str) -> Path:
    return run_dir(run_id) / "session"


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
    run back to its Claude transcript when it finishes (``usagescan.task_run`` —
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


# MARK: - Which runner ran it, and how to reach it

#: Longest a session id may be. Both runners' are well under 64; the cap is only what
#: stops a stray file in a run directory becoming an id every later tick queries.
_MAX_SESSION_ID = 128


def stage_runner(run_id: str) -> str:
    """Record which agent CLI is being spawned into this run, and return it.

    Written down rather than re-read from the setting later, because the setting is
    what the NEXT spawn will use: a run started under one runner and asked about after
    the operator switched to another would otherwise be interrogated through the wrong
    store, and answer nothing about itself.
    """
    from . import runner

    chosen = runner.selected()
    try:
        runner_path(run_id).write_text(chosen, encoding="utf-8")
    except OSError:
        pass
    return chosen


def run_runner(run_id: str) -> str:
    """Which agent CLI ran this run, or "" for one that predates the record.

    Empty is not Claude Code: it is "unknown", and the probes read it as a run they
    cannot ask about — which falls back to the screen, the only evidence such a run
    ever had.
    """
    try:
        return runner_path(run_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def stage_port(run_id: str) -> int | None:
    """Reserve the port this run's OpenCode server will answer on, and record it.

    The applet picks it rather than the agent, because the applet is the one that
    puts it on the agent's command line — a port only discoverable once the server
    is up is a port nothing can ask about while the run is starting, which is when
    the first questions are asked.

    ``None`` means the run is spawned without a port and read off its screen instead.
    Both failures land here: no port could be reserved, and one was reserved but
    could not be written down. Recording it is what makes it useful, so an
    unrecorded port is the same as none.
    """
    from . import opencodeapi

    port = opencodeapi.free_port()
    if port is None:
        return None
    try:
        port_path(run_id).write_text(str(port), encoding="utf-8")
    except OSError:
        return None
    return port


def port(run_id: str) -> int | None:
    """The port this run's OpenCode server answers on, or ``None`` for a run that
    has none — every Claude Code run, and any OpenCode run whose port could not be
    reserved."""
    try:
        raw = port_path(run_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value < 65536 else None


def bound_session(run_id: str) -> str:
    """Which of its runner's sessions was found to be this run's, or "" before one was.

    Kept on disk rather than in memory so the search survives the applet restart
    this whole module exists for — and because the search is the expensive half:
    matching a session to a run reads its opening message, while asking a bound one
    what it is doing reads a single message.

    Every runner spells an id its own way — ``ses_00d61ec0…`` under OpenCode,
    ``20260812_002140_b0e4d4`` under Hermes — so what is checked is the shape any id
    has and no torn or hand-edited file does: one bounded, non-empty token.
    """
    try:
        value = session_path(run_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(value) > _MAX_SESSION_ID or len(value.split()) != 1:
        return ""
    return value


def bind_session(run_id: str, session_id: str) -> None:
    """Record which session is this run's."""
    try:
        session_path(run_id).write_text(session_id, encoding="utf-8")
    except OSError:
        pass


def sentinels(records: list[RunRecord]) -> Observation:
    """The run ids whose agent has written its exit code — the runs this applet
    spawned itself, which are the only ones pointed at a sentinel in here.

    Always PRESENT: this reads our own directory, so a missing sentinel is
    positively "has not exited yet" rather than unknown. An unreadable directory
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

    None for a run the mesh placed: the executor points the agent at a sentinel of
    its own under the mesh directory and unlinks it the moment it fires, so nothing
    is ever written here. :func:`telemetry.record_completion` dates those from the
    agent's transcript rather than from the poll.
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
