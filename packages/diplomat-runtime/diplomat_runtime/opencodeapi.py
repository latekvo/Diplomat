"""What an OpenCode agent is doing, asked of the agent instead of read off its screen.

OpenCode serves its sessions over HTTP on loopback, and that server answers the
question the applet has always had to guess at: **is this run working, or back at its
prompt?** Which server depends on the major version, and so does nearly everything
about asking it:

* **1.x** — a TUI given ``--port`` serves its own session on that port while it works,
  unauthenticated. The run is matched to its session by its prompt. Most of this
  module, and every section below but the last.
* **2.x** — there is no server per TUI. Every client talks to one per-user service
  (``opencode serve --service``), found through :func:`service_file` and
  authenticated with the password in it. The session is minted and bound at spawn
  (:func:`review.stage_opencode_session`); only a run spawned elsewhere is matched.
  See "OpenCode 2.x" below.

A 1.x server keeps a status per session — ``busy``, ``retry`` or idle — the same one
its own TUI draws from, and it stamps each message it finishes. Neither is an
inference from how a status bar happened to be drawn.

Both are read, and a turn is over only when they agree: the server is running no turn
in this session AND the last thing it wrote was a finished message. Each covers the
other's one blind spot. The status alone calls a session idle in the moment between
its server coming up and its first turn starting, which would retire a run seconds
after it launched. The stamp alone calls a turn over between every two STEPS of one:
OpenCode writes an assistant message per step, each stamped as it completes, and the
gaps between them are short but there are hundreds of them in a long review (1.4.3:
164 gaps in one 2.5-hour session, up to 757ms each) — enough that a poll lands in one.

What the run SPENT is not asked here. A turn's price is per-message, so a run's is a
sum over its whole transcript, and this poll reads one message — :mod:`usagescan`
prices a finished run from the CLI's session export instead, once, when it ends.

The screen is still read for a run this cannot reach — a Claude Code agent, a 1.x
agent spawned without a port, a server or service that will not answer.
:mod:`agentstate` takes whichever answer it gets and says which one it used.

Which session is this run's (1.x)
--------------------------------
Every run gets its own server, but not its own session store: whichever port it is
asked on, ``GET /session`` answers out of the store OpenCode keeps per project — every
agent that has worked in this checkout or a worktree of it — most recently touched
first, and cut off at a hundred rows unless the fetch asks for more. So the fetch asks
for both halves of the narrowing the server can do (:func:`session_path`): this run's
directory, which it matches exactly, and a limit one checkout's history does not reach.
Otherwise a busier neighbour holds every row of the answer and this run's session is
not in it at all.

A run is matched to its session the only way that is exact — by the prompt.
:func:`candidates` narrows the list to sessions that could be this run's (its
directory, created no earlier than its dispatch, not already another run's), and
:func:`is_ours` confirms one by comparing the session's opening user message against
the prompt file the applet staged. The answer is written into the run directory, so
the search happens once per run rather than once per tick.

Loopback, and unauthenticated (1.x)
-----------------------------------
The server binds ``127.0.0.1``. It is NOT password-protected, and that is forced
rather than chosen: OpenCode's server does support a password, but its own TUI does
not send one, so a run started with ``OPENCODE_SERVER_PASSWORD`` set dies on
``Unauthorized`` before it does any work (verified against 1.4.3). So the port is
reachable by any other user on the machine, and driving it runs commands as this
user. On a shared box that is a real exposure and this seam is where it would be
closed — by not passing ``--port`` at all, at the cost of going back to reading the
screen.

OpenCode 2.x
------------
One service per user, so a run's session is asked for by id: ``GET
/api/session/{id}`` for the session and ``GET /api/session/active`` for the map of
sessions running a turn (:func:`service_state`). A run Diplomat spawned carries the
id it minted, so there is no search and nothing to confirm — and a session the
service no longer knows is a 404, which reads as unreachable. Every
request carries the service's password, and every failure reads as unreachable,
exactly as a 1.x port that will not answer does.

A 2.x run Diplomat did not spawn itself — one the mesh placed here — has no id bound
at spawn. Its TUI's argv is ``opencode --session <id>``, so its session is found from
the process table and matched to the run by the prompt it was opened on
(:func:`opening_prompt`), the one exact key, as a 1.x run's is.

The service outlives the window: a 2.x run's turn goes on after its TUI is gone, so
whatever closes a run's window or retires a run also calls :func:`interrupt`.

Stdlib-only, like the rest of the spawn path, and nothing here raises: a probe that
cannot answer says so and the tick continues.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .agentstate import SessionState

#: The interface the run's server binds — OpenCode's own default, restated because
#: it is also the address the probe dials.
HOST = "127.0.0.1"

#: Per-request budget. This runs on the panel's tick, once per OpenCode run, so it
#: has to fail faster than the tick rather than hold it up: a wedged server must cost
#: one unavailable answer, not a frozen panel.
TIMEOUT = 2.0

#: Most a single response may be. The last-message poll is one message and the binding
#: fetch is :data:`SESSION_LIMIT` session rows at some 500 bytes each — under a
#: sixteenth of this between them — but a message carries its tool output inline, and
#: one agent that cats a large file would otherwise pull it through this probe on every
#: tick forever. Over the cap reads as unavailable, which falls back to the screen.
MAX_BYTES = 8 * 1024 * 1024

#: Most sessions a listing may hold. The server cuts the least recently touched, so this
#: is how many of one checkout's sessions must be touched between a run's dispatch and
#: its binding for its own to be cut too — far past what the task cap can produce in the
#: seconds that takes.
SESSION_LIMIT = 1000


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
    return _fetch(urllib.request.Request(f"http://{HOST}:{port}{path}"))


def _fetch(request: urllib.request.Request):
    """One request, its JSON answer decoded — ``None`` on anything at all, for the
    reasons :func:`_get` gives. Anything but a 200 is one of those too: an error status
    raises out of ``urlopen``, and the 2.x service answers ``text/html`` 200 for a path
    it does not route outside ``/api``, which fails the decode."""
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:  # noqa: S310 - loopback
            if resp.status != 200:
                return None
            raw = resp.read(MAX_BYTES + 1)
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def session_path(directory: str) -> str | None:
    """Where to ask for one directory's sessions, or ``None`` for no directory.

    ``?directory=`` narrows the listing only while it has a value: sent empty it is not
    a filter that matches nothing but no filter at all, and the shared store comes back
    whole and cut to the limit — the answer the parameter is here to avoid. So an empty
    directory is nothing to ask, and reads as a server that would not answer.

    Neither parameter is in the OpenAPI document the server publishes; both are read off
    what a 1.4.3 server answers (``?limit=abc`` is a 400, ``?limit=0`` is zero rows
    rather than no limit, a trailing slash on the directory matches nothing).
    """
    if not directory:
        return None
    filtered = urllib.parse.quote(directory, safe="/")
    return f"/session?directory={filtered}&limit={SESSION_LIMIT}"


def sessions(port: int, directory: str) -> list[dict] | None:
    """That directory's sessions, most recently touched first, as this run's server
    reports them.

    The filter is the server's own and is the same comparison :func:`candidates` makes
    over the answer — exact string equality on the session's directory — so it changes
    nothing about which sessions match, only how many rows the answer has to hold them.

    Ordered by last touch, so what :data:`SESSION_LIMIT` cuts is the least recently
    touched — every one of which a session created since the run was dispatched
    outranks.

    It has to be a directory OpenCode has worked in: one it has no project or sandbox
    for answers empty however many sessions the store holds against it — a checkout
    deleted since, say. A run's own never is, because the server being asked is the one
    running in it.
    """
    path = session_path(directory)
    if path is None:
        return None
    data = _get(port, path)
    return data if isinstance(data, list) else None


def statuses(port: int) -> dict | None:
    """What this run's server is working on, session id → its status.

    Scoped to the server asked, not to the machine: unlike ``GET /session``, which
    answers out of the shared store, this is the live state of the process holding the
    port — a run's own turn and its subagents', and nothing another run is doing.

    An idle session is simply absent, so this is a small response whatever the agent is
    up to. ``None`` when the server could not answer, which is never "idle".
    """
    data = _get(port, "/session/status")
    return data if isinstance(data, dict) else None


def messages(port: int, session_id: str, limit: int = 0) -> list[dict] | None:
    """A session's messages, oldest first. ``limit`` keeps only the last that many.

    The tick wants one message and the binding wants the first, so both spellings are
    here rather than at two call sites: ``limit=1`` is what stops a long review's
    whole transcript being pulled across every few seconds.
    """
    suffix = f"?limit={int(limit)}" if limit > 0 else ""
    data = _get(port, f"/session/{session_id}/message{suffix}")
    return data if isinstance(data, list) else None


# MARK: - The 2.x service


def service_file() -> str:
    """Where OpenCode 2.x says which service is running and how to reach it:
    ``opencode/service.json`` under the state directory the agent's shell gives it
    (:func:`usagescan.opencode_state_home`) — the service is started by that shell, so
    its ``$XDG_STATE_HOME`` is the one that counts, not this process's. Written mode
    0600 by the service itself, as ``{"id", "version", "url", "pid", "password"}`` —
    the password may be absent."""
    from . import usagescan

    return os.path.join(usagescan.opencode_state_home(), "opencode", "service.json")


def _service() -> tuple[str, dict[str, str]] | None:
    """The service's base URL and the headers that authenticate to it, or ``None``
    when there is no readable discovery file naming a URL.

    Every ``/api`` route answers 401 without the password, sent as HTTP Basic under
    the user name ``opencode``.
    """
    try:
        with open(service_file(), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(url, str) or not url:
        return None
    headers = {}
    password = info.get("password")
    if isinstance(password, str) and password:
        token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return url.rstrip("/"), headers


def _service_call(path: str, method: str = "GET"):
    """One request to the 2.x service, its JSON answer decoded; ``None`` on anything
    at all, including there being no service to ask."""
    service = _service()
    if service is None:
        return None
    base, headers = service
    body = b"" if method == "POST" else None
    return _fetch(urllib.request.Request(base + path, data=body, headers=headers,
                                         method=method))


def _session_route(session_id: str) -> str:
    return "/api/session/" + urllib.parse.quote(session_id, safe="")


def active_sessions() -> dict | None:
    """The service's map of sessions running a turn — ``GET /api/session/active``'s
    ``data``, idle sessions absent — or ``None`` when it will not say.

    One answer for every run: a probe pass asks this once and reads every 2.x run
    against it (:func:`service_state`), so a hung service costs a pass one timeout
    rather than one per run.
    """
    active = _service_call("/api/session/active")
    active = active.get("data") if isinstance(active, dict) else None
    return active if isinstance(active, dict) else None


def service_state(session_id: str, active: dict | None) -> SessionState | None:
    """What a 2.x run's session is doing, asked of the service; ``None`` — "ask the
    screen instead" — when either question goes unanswered, a session the service
    does not know (404) among them. ``active`` is this pass's :func:`active_sessions`,
    and ``None`` there asks nothing more. See :func:`service_state_of` for the
    reading."""
    if active is None:
        return None
    info = _service_call(_session_route(session_id))
    info = info.get("data") if isinstance(info, dict) else None
    if not isinstance(info, dict):
        return None
    return service_state_of(info, active, session_id)


def interrupt(session_id: str) -> None:
    """Stop the turn a 2.x session is running, if it is running one.

    Closing a 2.x run's window does not do this: the TUI is only a client, and the
    turn goes on in the service after it is gone — measured on 2.0.18, a SIGTERM to the
    TUI mid-turn left its tool call to finish 20 s later. So whatever closes a run's
    window or retires a run sends this too, or the agent keeps working headless after
    Diplomat has let its bay go. An idle session answers ``{"interrupted": false}`` and
    is left as it was; a running one ends its turn with outcome ``interrupted``.
    Best-effort and never raises, and answers nothing: no caller could act on it.
    """
    _service_call(_session_route(session_id) + "/interrupt", "POST")


#: A 2.x TUI's argv: its binary — ``opencode`` or ``opencode.exe``, by any path — then
#: ``--session <id>`` and nothing else. Whole-argv: the terminal, the tmux client and
#: server and the pane's shells all carry the spawn command with this inside it, and
#: none of them is the TUI or on its tty.
SESSION_ARG = re.compile(r"(?:\S*/)?opencode(?:\.exe)?\s+--session\s+(ses[0-9A-Za-z_-]+)")

#: Opening prompts already read, by session id. A prompt never changes once written,
#: so a hit is kept for the life of the process; a miss is not — the list is empty
#: until ``session.prompt`` lands, a moment after the TUI's argv appears.
_opening_prompts: dict[str, str] = {}


def session_arg(argv: str) -> str | None:
    """The session a 2.x TUI's argv attaches to, or ``None`` for any other argv
    (:data:`SESSION_ARG`)."""
    found = SESSION_ARG.fullmatch(argv.strip())
    return found.group(1) if found else None


def opening_prompt(session_id: str) -> str | None:
    """A 2.x session's opening user message — the prompt it was started on — or
    ``None`` while the service has none to give (not yet prompted, unknown, or not
    answering).

    ``GET /api/session/{id}/message?limit=1&order=asc`` answers
    ``{"data": [{"type": "user", "text": …}]}`` (2.0.18). One request per session
    ever, for a session that has one; see :data:`_opening_prompts`.
    """
    if session_id in _opening_prompts:
        return _opening_prompts[session_id]
    page = _service_call(_session_route(session_id) + "/message?limit=1&order=asc")
    rows = page.get("data") if isinstance(page, dict) else None
    first = rows[0] if isinstance(rows, list) and rows else None
    if not (isinstance(first, dict) and first.get("type") == "user"
            and isinstance(first.get("text"), str)):
        return None
    _opening_prompts[session_id] = first["text"]
    return first["text"]


def scan_text(argv: str) -> str:
    """What a ``ps`` prompt scan should read for one process: its argv, and for a 2.x
    TUI the prompt its session was opened on.

    Every other agent carries its prompt in its argv, which is the whole of what the
    scan for ``PR #<n> in <owner>/<repo>`` rests on. A 2.x TUI carries only
    ``--session <id>`` — its prompt was submitted through the service — so without
    this a 2.x agent the applet holds no pid for (one the mesh placed here, one it has
    no record of) reads as no agent at all, and is retired mid-turn.
    """
    session_id = session_arg(argv)
    if session_id is None:
        return argv
    return f"{argv}\n{opening_prompt(session_id) or ''}"


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


def is_running(session_statuses: dict, session_id: str) -> bool:
    """Is a turn in flight in this session, per :func:`statuses`?

    A session the server is not working on is absent from the map, so absence is the
    ordinary way to be idle. An entry that names itself ``idle`` is read as idle too,
    rather than as "present, therefore busy" — the two spellings mean one thing, and
    the ladder above must not hold a run open because its server chose the other.

    Every other entry is a turn in flight, ``retry`` included: an agent waiting out a
    provider's backoff is not back at its prompt and nothing may be dispatched over it.
    An entry of a shape this does not know is one too — being listed at all is the
    server tracking the session, and only the two readings above are safe to end a run
    on.
    """
    if session_id not in session_statuses:
        return False
    entry = session_statuses[session_id]
    return not (isinstance(entry, dict) and entry.get("type") == "idle")


def state_of(session_messages: list[dict], running: bool | None) -> SessionState | None:
    """Whether this session's turn is still in flight — from its server's status and
    its last message together, which is the whole of what makes the answer safe.

    ``None`` — "ask the screen instead" — for either half being missing: a status the
    server would not report, and a session created but not yet written to. Neither is
    "idle". A run whose turn has not started has not finished either, and saying so
    would retire an agent seconds after it launched.

    Busy while the server says a turn is running, and busy again for a last message
    with no completion stamp. It takes both to call a turn over: the status is what
    holds a run open across the sub-second gaps between the steps of one turn, and the
    stamp is what holds it open before its first turn has begun.
    """
    if running is None or not session_messages:
        return None
    last = session_messages[-1]
    if not isinstance(last, dict):
        return None
    info = last.get("info")
    if not isinstance(info, dict):
        return None
    completed = _sub(info, "time").get("completed")
    return SessionState(busy=running or not isinstance(completed, (int, float)))


def service_state_of(info: dict, active: dict, session_id: str) -> SessionState:
    """Whether a 2.x session's turn is in flight, from the session and the service's
    map of running sessions.

    Busy while the session is in the map. 2.x keeps a session in it from the start of a
    turn to its end with no gap between steps (polled at 50 ms across a three-tool-call
    turn on 2.0.18), so the map holds a run open alone — the blind spot the 1.x stamp
    covers is not there. The other one is: a session created but not yet running its
    first turn is absent from the map too, and reading that as idle would retire an
    agent in the moment between ``session.create`` and its turn starting. So absent
    reads idle only once the session carries ``time.idle``, which the service stamps
    when a turn ends.
    """
    if session_id in active:
        return SessionState(busy=True)
    idle = _sub(info, "time").get("idle")
    return SessionState(busy=not (isinstance(idle, (int, float))
                                  and not isinstance(idle, bool)))


def session_tokens(session_messages: list) -> float:
    """What a whole session spent, from the messages its export returns — each 1.x
    message's tokens are under its ``info``, each 2.x one's at its top level.

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
