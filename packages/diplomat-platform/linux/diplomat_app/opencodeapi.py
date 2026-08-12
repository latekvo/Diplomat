"""What an OpenCode agent is doing, asked of the agent instead of read off its screen.

An OpenCode TUI given ``--port`` serves its own session over HTTP on loopback while
it works, and that server answers the question the applet has always had to guess at:
**is this run working, or back at its prompt?** Its last message carries a completion
stamp, set the instant the turn ends. A stamp is positive evidence the turn is over;
its absence is positive evidence it is still in flight. Neither is an inference from
how a status bar happened to be drawn.

What the run SPENT is not asked here. A turn's price is per-message, so a run's is a
sum over its whole transcript, and this poll reads one message — :mod:`usagescan`
prices a finished run from ``opencode export`` instead, once, when it ends.

The screen is still read for a run this cannot reach — a Claude Code agent, an
OpenCode agent spawned before the port was allocated, a server that will not answer.
:mod:`agentstate` takes whichever answer it gets and says which one it used.

Which session is this run's
--------------------------
Every run gets its own server, but not its own session store: OpenCode keeps one
global store, so ``GET /session`` on any port answers with the machine's own history
rather than this run's — its hundred most recent sessions, newest first.
So a run is matched to its session the only way that is exact — by the prompt.
:func:`candidates` narrows the list to sessions that could be this run's (its
directory, created no earlier than its dispatch, not already another run's), and
:func:`is_ours` confirms one by comparing the session's opening user message against
the prompt file the applet staged. The answer is written into the run directory, so
the search happens once per run rather than once per tick.

Loopback, and unauthenticated
-----------------------------
The server binds ``127.0.0.1``. It is NOT password-protected, and that is forced
rather than chosen: OpenCode's server does support a password, but its own TUI does
not send one, so a run started with ``OPENCODE_SERVER_PASSWORD`` set dies on
``Unauthorized`` before it does any work (verified against 1.4.3). So the port is
reachable by any other user on the machine, and driving it runs commands as this
user. On a shared box that is a real exposure and this seam is where it would be
closed — by not passing ``--port`` at all, at the cost of going back to reading the
screen.

Stdlib-only, like the rest of the spawn path, and nothing here raises: a probe that
cannot answer says so and the tick continues.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request

from .agentstate import SessionState

#: The interface the run's server binds — OpenCode's own default, restated because
#: it is also the address the probe dials.
HOST = "127.0.0.1"

#: Per-request budget. This runs on the panel's tick, once per OpenCode run, so it
#: has to fail faster than the tick rather than hold it up: a wedged server must cost
#: one unavailable answer, not a frozen panel.
TIMEOUT = 2.0

#: Most a single response may be. The last-message poll is one message and the
#: binding fetch is a session seconds old, so both are small — but a message carries
#: its tool output inline, and one agent that cats a large file would otherwise pull
#: it through this probe on every tick forever. Over the cap reads as unavailable,
#: which falls back to the screen.
MAX_BYTES = 8 * 1024 * 1024


# MARK: - Ports


def free_port() -> int | None:
    """A port nothing is listening on, or ``None`` if one cannot be had.

    Taken by binding zero and letting the kernel choose, then closing: the answer is
    a port that was genuinely free, rather than one that merely looked free. It can
    still be taken in the moment between here and the agent's own bind, and an
    OpenCode that cannot bind exits instead of choosing another port — so the caller
    treats ``None`` and a lost race the same way, by spawning without a port and
    reading the screen instead.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, 0))
            return int(s.getsockname()[1])
    except OSError:
        return None


# MARK: - The server


def _get(port: int, path: str):
    """One GET against a run's server, decoded. ``None`` on anything at all.

    Every failure collapses to one answer on purpose: a server still starting, a run
    whose window was closed, a port taken by something that is not OpenCode and a
    response too large to hold are all "this run cannot be reached", and the caller's
    only useful response to any of them is to fall back to the screen.

    ``HTTPException`` is caught beside the socket errors and not by accident: it is
    what a listener that speaks something other than HTTP raises, and it descends from
    ``Exception`` rather than ``OSError``, so a port the kernel handed to some other
    daemon between the reservation and the agent's own bind would otherwise raise
    through :func:`probes.gather` and cost every run its tick, not just this one.
    """
    url = f"http://{HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:  # noqa: S310 - loopback
            raw = resp.read(MAX_BYTES + 1)
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def sessions(port: int) -> list[dict] | None:
    """The machine's recent sessions, newest first, as this run's server reports them.

    A hundred of them, not the whole store (1.4.3) — a bound a run's own session is
    always inside, since it is matched within seconds of being created.
    """
    data = _get(port, "/session")
    return data if isinstance(data, list) else None


def messages(port: int, session_id: str, limit: int = 0) -> list[dict] | None:
    """A session's messages, oldest first. ``limit`` keeps only the last that many.

    The tick wants one message and the binding wants the first, so both spellings are
    here rather than at two call sites: ``limit=1`` is what stops a long review's
    whole transcript being pulled across every few seconds.
    """
    suffix = f"?limit={int(limit)}" if limit > 0 else ""
    data = _get(port, f"/session/{session_id}/message{suffix}")
    return data if isinstance(data, list) else None


# MARK: - Reading the answer (pure)


def _sub(obj: dict, key: str) -> dict:
    """A nested object, or an empty one for any other shape.

    ``(obj.get(k) or {})`` would do for a missing key or a null, and raises on the one
    that matters — a key whose value is a *list*, which is what a JSON payload from
    something that is not OpenCode looks like. These readers are called from the tick,
    where an exception costs every run its answer rather than this one.
    """
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def candidates(session_list: list[dict], directory: str, since_ms: float,
               taken: set[str]) -> list[str]:
    """Sessions that could be this run's, oldest first.

    Three filters, each of which a run's own session always passes: it is in the
    directory the agent was spawned into, it was created no earlier than the run was
    dispatched, and it has not already been claimed by another run. What survives is
    ordinarily one session; :func:`is_ours` settles the rest.
    """
    out = []
    for s in session_list:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or sid in taken:
            continue
        if s.get("directory") != directory:
            continue
        created = _sub(s, "time").get("created")
        if not isinstance(created, (int, float)) or created < since_ms:
            continue
        out.append((created, sid))
    return [sid for _created, sid in sorted(out)]


def is_ours(session_messages: list[dict], prompt: str) -> bool:
    """Is this the session our prompt was submitted to?

    ``--prompt`` lands verbatim as the opening user message, so this is an equality
    test rather than a resemblance one. It is what makes the match exact when two
    runs are working in the same checkout at the same time — the case the directory
    and dispatch-time filters cannot separate, and the case the applet's own task cap
    makes ordinary rather than rare.
    """
    if not session_messages:
        return False
    first = session_messages[0]
    if not isinstance(first, dict):
        return False
    if _sub(first, "info").get("role") != "user":
        return False
    texts = [p.get("text", "") for p in first.get("parts") or []
             if isinstance(p, dict) and p.get("type") == "text"]
    return "".join(texts) == prompt


def state_of(session_messages: list[dict]) -> SessionState | None:
    """What the last message says: working or done, and what it cost.

    ``None`` when there is no message to read — a session created but not yet
    written to. That is not "idle": a run whose turn has not started has not
    finished either, and saying so would retire an agent seconds after it launched.

    A message with no completion stamp is a turn in flight. That covers a provider
    retry as well as ordinary work, which is the right reading of both: the agent is
    not back at its prompt and nothing else may be dispatched over it.
    """
    if not session_messages:
        return None
    last = session_messages[-1]
    if not isinstance(last, dict):
        return None
    info = last.get("info")
    if not isinstance(info, dict):
        return None
    completed = _sub(info, "time").get("completed")
    return SessionState(busy=not isinstance(completed, (int, float)))


def session_tokens(session_messages: list) -> float:
    """What a whole session spent, from the messages ``opencode export`` returns.

    Every message, because OpenCode reports a turn's price per message: reading only
    the last would price a two-hour review at whatever its closing sentence cost.

    Input, output and cache *writes*, never cache reads. Cache reads are huge and
    cheap and :mod:`usagescan` leaves them out for Claude Code, so counting them would
    make the per-task figure on the telemetry screen mean one thing for one runner and
    another for the other.
    """
    return sum(_message_tokens(m) for m in session_messages)


def _message_tokens(message) -> float:
    if not isinstance(message, dict):
        return 0.0
    info = message.get("info")
    info = info if isinstance(info, dict) else message
    tokens = info.get("tokens")
    if not isinstance(tokens, dict):
        return 0.0
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    return sum(v for v in (tokens.get("input"), tokens.get("output"),
                           cache.get("write"))
               if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0)
