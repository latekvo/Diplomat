"""Asking an OpenCode agent what it is doing, instead of reading it off its screen.

The screen was never evidence. ``esc interrupt`` is a string from someone else's UI,
and every agent reads as idle the moment they reword it — the cap empties, the
monitors burst, and nothing anywhere says so. A run that serves its own session can
be asked instead, and the answer is a completion stamp: present means the turn ended,
absent means it did not.

Three things have to hold for that to be worth having, and each is a group below:

* the answer must be **read correctly** — including that "no messages yet" is not
  "idle", which would retire an agent seconds after it launched;
* the run must be matched to **its own** session — OpenCode keeps one store for the
  whole machine, so a run's own server lists the box's recent history, and the
  applet's task cap makes "two agents in one checkout" the ordinary case;
* a run that **cannot** be reached must cost the older evidence and never a verdict.

The HTTP cases run against a real server on a real socket rather than a stubbed
fetch: the request shapes are half of what could be wrong here, and a stub that
answers whatever we ask proves nothing about the query we send.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from diplomat_runtime import agentregistry, opencodeapi, review, runner
from diplomat_app import probes
from diplomat_runtime.agentstate import RunRecord, SessionState

T0 = 1_000_000.0
PROMPT = "Review PR #7 in o/r"

#: One session, mid-turn: an assistant message with no completion stamp.
WORKING = [{"info": {"role": "assistant", "time": {"created": 1.0}}}]

#: The same message once the turn ended — verbatim in shape from a real OpenCode
#: 1.4.3 run, including the token split the applet has to be selective about.
FINISHED = [{"info": {
    "role": "assistant",
    "time": {"created": 1.0, "completed": 2.0},
    "cost": 0.0032179,
    "tokens": {"total": 30505, "input": 7, "output": 8, "reasoning": 4,
               "cache": {"read": 30384, "write": 106}},
}}]


def opening(text: str) -> list[dict]:
    """A session's first message, as ``--prompt`` lands it."""
    return [{"info": {"role": "user", "time": {"created": 1.0}},
             "parts": [{"type": "text", "text": text}]}]


# MARK: - Reading the answer


def test_a_turn_with_no_completion_stamp_is_still_in_flight():
    assert opencodeapi.state_of(WORKING) == SessionState(busy=True)


def test_a_completed_turn_reads_as_back_at_the_prompt():
    assert opencodeapi.state_of(FINISHED) == SessionState(busy=False)


def test_a_session_with_no_messages_is_not_idle():
    """It has not started, which is not the same as having finished. Reading it as
    idle would hand a bay back — and let a second agent onto the PR — seconds after
    the first one launched."""
    assert opencodeapi.state_of([]) is None


@pytest.mark.parametrize("payload", [
    [{"info": {"role": "assistant", "time": {"completed": "soon"}}}],
    [{"info": {"role": "assistant"}}],
    [{"nope": 1}],
    ["not a message"],
])
def test_a_malformed_message_never_reads_as_finished(payload):
    """The one direction that must not be reachable by accident: every gap has to cost
    a bay, not end a run."""
    state = opencodeapi.state_of(payload)
    assert state is None or state.busy is True


# MARK: - Which session is this run's


SESSIONS = [
    {"id": "ses_a", "directory": "/repo", "time": {"created": 2000}},
    {"id": "ses_b", "directory": "/repo", "time": {"created": 3000}},
    {"id": "ses_old", "directory": "/repo", "time": {"created": 500}},
    {"id": "ses_elsewhere", "directory": "/other", "time": {"created": 3000}},
]


def test_only_sessions_this_run_could_own_are_candidates():
    """Another checkout's sessions are not ours, and neither is one that already
    existed when the run was dispatched — the machine's store holds months of them."""
    assert opencodeapi.candidates(SESSIONS, "/repo", 1000, set()) == ["ses_a", "ses_b"]


def test_a_session_another_run_already_owns_is_never_taken_twice():
    assert opencodeapi.candidates(SESSIONS, "/repo", 1000, {"ses_a"}) == ["ses_b"]


def test_candidates_come_back_oldest_first():
    """Runs are matched in dispatch order, so the oldest unclaimed session is the
    oldest unmatched run's. Newest-first would hand the second run's session to the
    first and then leave the second unmatchable."""
    shuffled = [SESSIONS[1], SESSIONS[0]]
    assert opencodeapi.candidates(shuffled, "/repo", 1000, set()) == ["ses_a", "ses_b"]


def test_the_prompt_is_what_identifies_a_session():
    """``--prompt`` lands verbatim as the opening message, so this is equality, not
    resemblance. It is what tells two runs apart when both work in the same checkout
    at the same time — which the task cap makes ordinary, not rare."""
    assert opencodeapi.is_ours(opening(PROMPT), PROMPT) is True
    assert opencodeapi.is_ours(opening("Review PR #8 in o/r"), PROMPT) is False
    assert opencodeapi.is_ours([], PROMPT) is False


def test_a_session_whose_first_message_is_not_the_users_is_not_ours():
    assert opencodeapi.is_ours(WORKING, PROMPT) is False


# MARK: - Reaching the server


class _Canned(BaseHTTPRequestHandler):
    """Answers the two routes the probe uses, and records what it was asked."""

    routes: dict = {}
    seen: list = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        type(self).seen.append(self.path)
        body = type(self).routes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    """A real OpenCode-shaped server on a real port, so the request shapes are under
    test too and not just the parsing."""
    _Canned.routes, _Canned.seen = {}, []
    httpd = ThreadingHTTPServer((opencodeapi.HOST, 0), _Canned)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_the_last_message_is_asked_for_by_itself(server):
    """A review's transcript carries every tool call's output inline. Pulling all of
    it across on every tick, for the sake of one field on the last message, is the
    difference between a probe and a problem."""
    port = server.server_address[1]
    _Canned.routes["/session/ses_a/message?limit=1"] = FINISHED
    assert opencodeapi.messages(port, "ses_a", limit=1) == FINISHED
    assert _Canned.seen == ["/session/ses_a/message?limit=1"]


def test_a_response_too_large_to_hold_reads_as_unreachable(server):
    """Bounded rather than trusted: one agent that cats a large file would otherwise
    pull it through this probe on every tick forever."""
    port = server.server_address[1]
    _Canned.routes["/session"] = b"[" + b" " * (opencodeapi.MAX_BYTES + 1) + b"]"
    assert opencodeapi.sessions(port) is None


def test_a_port_nothing_answers_on_reads_as_unreachable():
    """A server still starting, a window already closed, a port taken by something
    that is not OpenCode — one answer, because the only useful response to any of
    them is to read the screen instead."""
    port = opencodeapi.free_port()
    assert port is not None
    assert opencodeapi.sessions(port) is None


def test_a_reserved_port_is_actually_free():
    """Taken by binding zero and letting the kernel choose. An OpenCode that cannot
    bind exits rather than picking another port, so a port that merely looked free
    would be a run that reports success and does nothing."""
    import socket

    port = opencodeapi.free_port()
    with socket.socket() as s:
        s.bind((opencodeapi.HOST, port))  # would raise if it were taken


# MARK: - The probe, end to end


def staged(run_id: str, port: int, prompt: str = PROMPT, dispatched: float = T0):
    """A run in the registry, spawned on ``port`` — what the store leaves behind."""
    record = RunRecord(run_id=run_id, dispatched_at=dispatched, pr_number=7,
                       pid=4242, tty="pts/3")
    agentregistry.create_run(record, prompt)
    agentregistry.runner_path(run_id).write_text(runner.OPENCODE, encoding="utf-8")
    agentregistry.port_path(run_id).write_text(str(port), encoding="utf-8")
    return record


def test_a_run_finds_its_own_session_and_remembers_it(server, monkeypatch):
    port = server.server_address[1]
    _Canned.routes["/session"] = [
        {"id": "ses_theirs", "directory": "/repo", "time": {"created": T0 * 1000 + 1}},
        {"id": "ses_ours", "directory": "/repo", "time": {"created": T0 * 1000 + 2}},
    ]
    _Canned.routes["/session/ses_theirs/message"] = opening("Review PR #8 in o/r")
    _Canned.routes["/session/ses_ours/message"] = opening(PROMPT)
    _Canned.routes["/session/ses_ours/message?limit=1"] = FINISHED
    record = staged("r1", port)

    obs = probes.agent_sessions([record], "/repo", T0)

    assert obs.ok and obs.value["r1"] == SessionState(busy=False)
    # Written down, so the search — which reads a session's opening message — happens
    # once per run rather than once per tick. Asked far enough past the first tick to
    # be past the cache too, which is what leaves the memo as the only reason the
    # search is not repeated.
    assert agentregistry.bound_session("r1") == "ses_ours"
    _Canned.seen.clear()
    probes.agent_sessions([record], "/repo", T0 + probes._CACHE_SECS + 1)
    assert _Canned.seen == ["/session/ses_ours/message?limit=1"]


def test_a_repaint_moments_later_is_answered_without_dialling_again(server):
    """This probe dials a socket and the resolver re-runs for every question the applet
    asks — two per panel repaint. Uncached, one unresponsive port would cost a repaint
    two full timeouts on the Qt thread."""
    port = server.server_address[1]
    _Canned.routes["/session"] = [
        {"id": "ses_ours", "directory": "/repo", "time": {"created": T0 * 1000 + 2}},
    ]
    _Canned.routes["/session/ses_ours/message"] = opening(PROMPT)
    _Canned.routes["/session/ses_ours/message?limit=1"] = FINISHED
    record = staged("r1", port)
    first = probes.agent_sessions([record], "/repo", T0)
    _Canned.seen.clear()

    again = probes.agent_sessions([record], "/repo", T0 + probes._CACHE_SECS - 0.1)

    assert _Canned.seen == [], "the cached window still went to the network"
    assert again.value == first.value
    # A run dispatched since the last pass is not in the answer the cache holds, so
    # the key is the runs as well as the clock.
    _Canned.routes["/session/ses_2/message"] = opening("Review PR #8 in o/r")
    _Canned.routes["/session/ses_2/message?limit=1"] = WORKING
    _Canned.routes["/session"] = _Canned.routes["/session"] + [
        {"id": "ses_2", "directory": "/repo", "time": {"created": T0 * 1000 + 3}},
    ]
    fresh = staged("r2", port, prompt="Review PR #8 in o/r")

    both = probes.agent_sessions([record, fresh], "/repo",
                                 T0 + probes._CACHE_SECS - 0.1)

    assert both.value["r2"].busy is True, (
        "a newer run was answered from a sweep that never asked about it"
    )


def test_two_runs_in_one_checkout_do_not_take_each_others_session(server):
    """The case the directory and dispatch-time filters cannot separate on their own,
    and the case the applet's own task cap makes ordinary: both agents are working in
    the same repo, seconds apart. Getting it wrong shows each run the other's state
    and prices each against the other's tokens."""
    port = server.server_address[1]
    _Canned.routes["/session"] = [
        {"id": "ses_1", "directory": "/repo", "time": {"created": T0 * 1000 + 1}},
        {"id": "ses_2", "directory": "/repo", "time": {"created": T0 * 1000 + 2}},
    ]
    _Canned.routes["/session/ses_1/message"] = opening("Review PR #8 in o/r")
    _Canned.routes["/session/ses_2/message"] = opening(PROMPT)
    _Canned.routes["/session/ses_1/message?limit=1"] = WORKING
    _Canned.routes["/session/ses_2/message?limit=1"] = FINISHED
    mine = staged("r1", port)
    theirs = staged("r2", port, prompt="Review PR #8 in o/r")

    obs = probes.agent_sessions([mine, theirs], "/repo")

    assert agentregistry.bound_session("r1") == "ses_2"
    assert agentregistry.bound_session("r2") == "ses_1"
    assert obs.value["r1"].busy is False and obs.value["r2"].busy is True


def test_a_run_whose_session_cannot_be_found_is_simply_absent(server):
    """Absent, not wrong: the resolver reads its screen instead, so a run this cannot
    reach costs the older evidence and never a verdict."""
    port = server.server_address[1]
    _Canned.routes["/session"] = []
    obs = probes.agent_sessions([staged("r1", port)], "/repo")
    assert obs.ok and "r1" not in obs.value


def test_a_session_older_than_the_run_is_never_taken_for_it(server):
    """The store is the whole machine's, so a *previous* agent's session on the same
    prompt in the same checkout is an ordinary thing to find — the same PR reviewed
    twice. Only time separates the two, and the two clocks are not the same one:
    OpenCode stamps a session in milliseconds while a run records its dispatch in
    seconds. Compared unconverted, every session ever created outranks every run and
    the oldest stale one wins."""
    port = server.server_address[1]
    _Canned.routes["/session"] = [
        {"id": "ses_last_run", "directory": "/repo",
         "time": {"created": (T0 - 3600) * 1000}},
    ]
    _Canned.routes["/session/ses_last_run/message"] = opening(PROMPT)
    _Canned.routes["/session/ses_last_run/message?limit=1"] = FINISHED

    obs = probes.agent_sessions([staged("r1", port)], "/repo", T0)

    assert obs.ok and "r1" not in obs.value
    assert not agentregistry.bound_session("r1"), (
        "a stale session was written onto the row, so no later sweep would look again"
    )


def test_a_machine_with_no_such_runs_is_unsupported_not_silent():
    """Every Claude Code machine is this one. Reported as unsupported so the
    probe-health warning never fires for an ordinary install."""
    record = RunRecord(run_id="r1", dispatched_at=T0, pid=4242)
    agentregistry.create_run(record, PROMPT)
    obs = probes.agent_sessions([record], "/repo")
    assert obs.status == "unsupported"


# MARK: - The spawn that makes any of it possible


@pytest.fixture
def opencode(monkeypatch, tmp_path):
    from diplomat_runtime import appconfig

    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))


def test_the_port_reaches_the_agents_command(opencode):
    """Spelled out here and identically in ``DiplomatCoreSmoke``: one config file
    picks the runner for both front-ends, and a machine can hand a mesh job to the
    other platform. Two sides that agree on the idea and differ by a byte spawn two
    different servers."""
    assert runner.agent_command("/tmp/p.txt", port=47910) == (
        'OPENCODE_PERMISSION=\'{"edit":"allow","bash":"allow","webfetch":"allow",'
        '"external_directory":"allow","doom_loop":"allow"}\' '
        'opencode --port 47910 --prompt "$(cat /tmp/p.txt)"'
    )


def test_a_run_without_a_port_spawns_exactly_as_it_did_before(opencode):
    """A port that cannot be reserved must not fail the spawn — the agent still runs,
    and is read off its screen like a Claude Code one."""
    assert "--port" not in runner.agent_command("/tmp/p.txt")
    assert "--port" not in runner.agent_command("/tmp/p.txt", port=0)


def test_the_claude_runner_is_given_no_port(monkeypatch):
    """It serves no session, so a port would be a flag its CLI does not have."""
    assert runner.agent_command("/tmp/p.txt", port=47910) == \
        'claude "$(cat /tmp/p.txt)"'


def test_staging_a_port_records_it_where_the_probe_looks():
    agentregistry.create_run(RunRecord(run_id="r1", dispatched_at=T0), PROMPT)
    port = agentregistry.stage_port("r1")
    assert port is not None
    assert agentregistry.port("r1") == port
    assert agentregistry.port_path("r1").read_text() == str(port)


def test_a_port_that_cannot_be_written_down_is_no_port_at_all():
    """Recording it is what makes it useful — the probe has no other way to learn it —
    so an unrecorded port has to read as none, and the spawn go on without one."""
    assert agentregistry.stage_port("never-created") is None


def test_a_run_with_no_port_file_has_no_port():
    assert agentregistry.port("never-staged") is None
