"""What a Hermes agent is doing, asked of its own store instead of read off its screen.

The same question :mod:`opencodeapi` asks, answered from a different place. Hermes
serves no per-run port — its ``serve`` is one machine-level gateway, not a server per
agent — but it writes every session and every message to SQLite at ``~/.hermes/state.db``
as it goes, mid-turn, and that is enough for both things the applet needs:

* **is this run working, or back at its prompt?** — the session's last message. An
  assistant message with a ``finish_reason`` of ``stop`` ended the turn; one with
  ``tool_calls``, a tool result, or a user message that has not been answered yet all
  mean a turn is still in flight. Positive evidence either way, rather than an
  inference from whether Hermes' status bar happened to read ``ready`` when we looked.
* **what did it cost?** — the session row carries running token counts, so a finished
  run is priced by the agent that ran it. This one is cumulative, unlike OpenCode's
  per-message figures, so it is simply read.

Which session is this run's
---------------------------
The store is machine-wide, so a run is matched to its session the only way that is
exact — by the prompt. ``hermes chat -q`` stores the query verbatim as the session's
opening user message, so :func:`candidates` narrows by directory and dispatch time and
:func:`is_ours` confirms one against the prompt the applet staged. The answer is
written into the run directory, so the search happens once per run.

Read-only, and never in the way
-------------------------------
Every connection is opened read-only and with a short busy timeout: the agent owns
this database, and a probe that blocked its writer — or worse, wrote to it — would be
a tracking mechanism that damaged the thing it tracks. Nothing here raises; a store
that cannot be read is a run the applet reads off its screen instead.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .agentstate import SessionState

#: How long a query waits on the agent's own writer before giving up. This runs on the
#: panel's tick, so it has to fail faster than the tick rather than hold it up.
BUSY_TIMEOUT = 2.0

#: ``finish_reason`` values that mean the turn is over rather than continuing.
#: ``tool_calls`` is deliberately absent: the agent asked for a tool and is waiting on
#: it, which is the middle of a turn and not the end of one.
TURN_OVER = frozenset({"stop", "end_turn", "length", "content_filter", "error"})


def db_path() -> Path:
    """Hermes' session store. ``DIPLOMAT_HERMES_DB`` overrides it, which is how the
    tests point the reader at a fixture instead of the developer's real sessions."""
    override = os.environ.get("DIPLOMAT_HERMES_DB")
    return Path(override) if override else Path.home() / ".hermes" / "state.db"


def _query(sql: str, params: tuple) -> list[tuple] | None:
    """One read-only query, or ``None`` if the store could not answer.

    Opened through a ``file:…?mode=ro`` URI rather than a plain path so the
    connection cannot create a database where none exists and cannot write to one
    that does. The agent is this file's owner; the applet is a reader in its house.
    """
    path = db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               timeout=BUSY_TIMEOUT)
    except sqlite3.Error:
        return None
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# MARK: - Which session is this run's


def candidates(directory: str, since: float, taken: set[str]) -> list[str]:
    """Sessions that could be a run's, oldest first.

    Three filters, each of which a run's own session always passes: it was started in
    the directory the agent was spawned into, no earlier than the run was dispatched,
    and it has not already been claimed by another run. What survives is ordinarily
    one session; :func:`is_ours` settles the rest.

    ``source`` is left alone deliberately. Hermes tags a session by how it was started
    and gates which toolsets load on that tag, so narrowing by it here would mean
    passing one at spawn time and quietly changing what the agent can do.
    """
    rows = _query(
        "SELECT id FROM sessions WHERE cwd = ? AND started_at >= ? "
        "ORDER BY started_at, id",
        (directory, since))
    if rows is None:
        return []
    return [r[0] for r in rows if r[0] not in taken]


def is_ours(session_id: str, prompt: str) -> bool:
    """Is this the session our prompt was submitted to?

    ``-q`` stores the query verbatim as the opening user message, so this is an
    equality test rather than a resemblance one. It is what makes the match exact when
    two runs are working in the same checkout at the same time — the case the
    directory and dispatch-time filters cannot separate, and the case the applet's own
    task cap makes ordinary rather than rare.
    """
    rows = _query(
        "SELECT role, content FROM messages WHERE session_id = ? "
        "ORDER BY id LIMIT 1", (session_id,))
    if not rows:
        return False
    role, content = rows[0]
    return role == "user" and (content or "") == prompt


# MARK: - What it says


def state_of(session_id: str) -> SessionState | None:
    """What the session's last message says: mid-turn, or back at the prompt.

    ``None`` when there is no message to read — a session created but not yet written
    to. That is not "idle": a run whose turn has not started has not finished either,
    and saying so would retire an agent seconds after it launched.

    Anything that is not a finished assistant message is a turn in flight, which is
    the right reading of all three ways that happens: the agent is mid tool call, a
    tool result is waiting to be answered, or the query has not been picked up yet.
    """
    rows = _query(
        "SELECT role, finish_reason FROM messages WHERE session_id = ? "
        "ORDER BY id DESC LIMIT 1", (session_id,))
    if not rows:
        return None
    role, finish_reason = rows[0]
    return SessionState(busy=not (role == "assistant" and finish_reason in TURN_OVER))


def session_tokens(session_id: str) -> float | None:
    """What one Hermes session spent, or ``None`` if it cannot be read.

    Input, output and cache *writes*, never cache reads — the same three
    :mod:`usagescan` sums for Claude Code, so one ledger holds every runner in one
    unit. Hermes keeps these cumulative on the session row, so unlike OpenCode there
    is nothing to sum across messages.
    """
    rows = _query(
        "SELECT input_tokens, output_tokens, cache_write_tokens FROM sessions "
        "WHERE id = ?", (session_id,))
    if not rows:
        return None
    return float(sum(v for v in rows[0]
                     if isinstance(v, (int, float)) and not isinstance(v, bool)
                     and v >= 0))


def session_price(session_id: str) -> tuple[float | None, str]:
    """``(dollars, model)`` for one session — what it cost, and what it ran on.

    The other unit a task can be priced in, and the one an OpenRouter-billed run is
    actually held to (:mod:`spend`). Tokens alone cannot answer it: the same hundred
    thousand tokens are cents on a small model and dollars on a frontier one, so the
    model is read alongside the money and travels with it into the ledger.

    Hermes prices each session itself, against the provider's published rates for the
    model it ran on, and settles that figure when the provider reports the real one:
    ``actual_cost_usd`` is preferred where it exists and the estimate answers until
    it does. Both are cumulative on the session row, like the token counts beside
    them.

    ``(None, "")`` where there is nothing to read — a session row that has not been
    priced yet, or a Hermes build older than the columns. That is a completion
    recorded without a price, exactly as an unattributable transcript is, and the
    gate falls back to its reserve rather than to a made-up figure.
    """
    rows = _query(
        "SELECT actual_cost_usd, estimated_cost_usd, model FROM sessions "
        "WHERE id = ?", (session_id,))
    if not rows:
        return None, ""
    actual, estimated, model = rows[0]
    usd = next((float(v) for v in (actual, estimated)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0),
               None)
    return usd, model if isinstance(model, str) else ""
