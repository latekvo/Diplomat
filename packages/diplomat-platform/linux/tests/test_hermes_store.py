"""Asking a Hermes agent what it is doing, out of the store it writes as it works.

The same question :mod:`test_opencode_api` asks over a port, answered from SQLite:
Hermes serves no per-run server, but it writes every session and every message to
``~/.hermes/state.db`` mid-turn, and a turn is over exactly when the agent stamps its
own message ``finish_reason``.

The same three things have to hold, and each is a group below:

* the answer must be **read correctly** — including that a tool call in flight is the
  middle of a turn, and that "no messages yet" is not "idle";
* the run must be matched to **its own** session — the store is machine-wide, so it
  holds every session on the box, and the task cap makes "two agents in one checkout"
  the ordinary case;
* a run that **cannot** be reached must cost the older evidence and never a verdict.

Every case runs against a real SQLite file written here rather than a stubbed reader:
the SQL is half of what could be wrong, and the schema is Hermes' (0.20.0), reduced to
the columns this depends on so the test states its own dependency.
"""

from __future__ import annotations

import sqlite3

from diplomat_app import agentregistry, hermesstore, probes, runner
from diplomat_app.agentstate import RunRecord, SessionState

T0 = 1_000_000.0
PROMPT = "Review PR #7 in o/r"
OTHER_PROMPT = "Review PR #8 in o/r"
REPO = "/repo"

# Session ids in Hermes' own spelling — `<date>_<time>_<hex>`, nothing like OpenCode's
# `ses_00d61ec0…`. Written out in full rather than shortened, because a reader that
# quietly accepted only one runner's spelling would pass every fixture that borrowed
# the other's.
OURS = "20260812_002140_b0e4d4"
THEIRS = "20260812_002139_8c907e"
OLDER = "20260811_222041_ddbfdc"

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, started_at REAL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT, content TEXT, finish_reason TEXT);
"""


def store(sessions: dict, cwds: dict | None = None) -> None:
    """Write a Hermes store where the fenced-off reader is looking.

    ``sessions`` maps a session id to its messages — ``(role, content,
    finish_reason)`` triples, oldest first — in the order they were started, one
    second apart. That is the arrangement the applet's task cap makes ordinary:
    several agents in one checkout, seconds apart. ``cwds`` overrides where one was
    started.
    """
    path = hermesstore.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        for i, (session_id, messages) in enumerate(sessions.items()):
            conn.execute(
                "INSERT INTO sessions (id, source, cwd, started_at, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens) "
                "VALUES (?, 'tui', ?, ?, 100, 20, 9000, 5)",
                (session_id, (cwds or {}).get(session_id, REPO), T0 + i))
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, finish_reason) "
                "VALUES (?, ?, ?, ?)",
                [(session_id, *m) for m in messages])
        conn.commit()
    finally:
        conn.close()


def user(text: str) -> tuple:
    """A session's opening message, as ``hermes chat -q`` stores it."""
    return ("user", text, None)


WORKING = ("assistant", "", "tool_calls")
FINISHED = ("assistant", "posted the review", "stop")


# MARK: - Reading the answer


def test_a_turn_the_agent_has_not_ended_is_still_in_flight():
    """``tool_calls`` is the agent asking for a tool and waiting on it — the middle of
    a turn. Reading it as finished would hand the run's bay to another agent while it
    is still spending tokens."""
    store({OURS: [user(PROMPT), WORKING]})
    assert hermesstore.state_of(OURS) == SessionState(busy=True)


def test_a_turn_the_agent_ended_reads_as_back_at_the_prompt():
    store({OURS: [user(PROMPT), FINISHED]})
    assert hermesstore.state_of(OURS) == SessionState(busy=False)


def test_a_tool_result_nobody_has_answered_yet_is_not_an_idle_agent():
    """The last row is the tool's own, so there is no ``finish_reason`` to read at
    all. Anything that is not a finished assistant message is a turn in flight."""
    store({OURS: [user(PROMPT), WORKING, ("tool", '{"exit_code": 0}', None)]})
    assert hermesstore.state_of(OURS).busy is True


def test_a_query_not_picked_up_yet_is_not_a_finished_turn():
    """A ``stop`` on a user row is not the agent saying anything. Reading the reason
    without the role would retire an agent in the second before it starts."""
    store({OURS: [("user", PROMPT, "stop")]})
    assert hermesstore.state_of(OURS).busy is True


def test_a_session_with_no_messages_has_not_started_rather_than_finished():
    """``None``, not idle: a run whose turn has not begun has not ended either, and
    saying so would retire an agent seconds after it launched."""
    store({OURS: []})
    assert hermesstore.state_of(OURS) is None


def test_a_store_that_is_not_there_answers_nothing_at_all():
    """Every machine without Hermes installed is this one. Absent, not idle."""
    assert hermesstore.state_of(OURS) is None
    assert hermesstore.candidates(REPO, T0, set()) == []
    assert hermesstore.session_tokens(OURS) is None


# MARK: - Which session is this run's


def test_candidates_exclude_another_checkout_a_stale_session_and_a_taken_one():
    store({OLDER: [user(PROMPT)], OURS: [user(PROMPT)], THEIRS: [user(PROMPT)]},
          cwds={THEIRS: "/other"})

    # OLDER started before the run was dispatched; THEIRS is in another checkout.
    assert hermesstore.candidates(REPO, T0 + 1, set()) == [OURS]
    assert hermesstore.candidates(REPO, T0, {OURS}) == [OLDER]


def test_the_prompt_is_what_makes_the_match_exact():
    """``-q`` stores the query verbatim, so this is equality rather than
    resemblance — the only thing separating two agents working in one checkout."""
    store({OURS: [user(PROMPT)]})
    assert hermesstore.is_ours(OURS, PROMPT)
    assert not hermesstore.is_ours(OURS, OTHER_PROMPT)


def test_a_session_whose_opening_message_is_not_a_query_is_nobody_s():
    store({OURS: [("assistant", PROMPT, "stop")]})
    assert not hermesstore.is_ours(OURS, PROMPT)


# MARK: - The price


def test_a_run_is_priced_from_its_own_session_row():
    """Input + output + cache WRITES, never the cache reads beside them: the same
    three the Claude Code scan sums, so one ledger holds every runner in one unit.
    The same numbers are asserted in ``DiplomatCoreSmoke``."""
    store({OURS: [user(PROMPT), FINISHED]})
    assert hermesstore.session_tokens(OURS) == 100 + 20 + 5


def test_a_session_the_store_never_heard_of_is_unpriced_not_free():
    store({OURS: [user(PROMPT)]})
    assert hermesstore.session_tokens(THEIRS) is None


# MARK: - The probe, end to end


def staged(run_id: str, prompt: str = PROMPT, dispatched: float = T0):
    """A Hermes run in the registry — what the store leaves behind at spawn."""
    record = RunRecord(run_id=run_id, dispatched_at=dispatched, pr_number=7,
                       pid=4242, tty="pts/3")
    agentregistry.create_run(record, prompt)
    agentregistry.runner_path(run_id).write_text(runner.HERMES, encoding="utf-8")
    return record


def test_a_hermes_run_finds_its_own_session_and_remembers_it():
    """Written down, so the search — which reads a session's opening message —
    happens once per run rather than once per tick, and so the run can still be
    priced from that session after its directory is the only thing left of it."""
    store({THEIRS: [user(OTHER_PROMPT), WORKING], OURS: [user(PROMPT), FINISHED]})
    record = staged("r1")

    obs = probes.agent_sessions([record], REPO)

    assert obs.ok and obs.value["r1"] == SessionState(busy=False)
    assert agentregistry.bound_session("r1") == OURS


def test_two_hermes_runs_in_one_checkout_do_not_take_each_others_session():
    store({THEIRS: [user(OTHER_PROMPT), WORKING], OURS: [user(PROMPT), FINISHED]})
    mine = staged("r1")
    theirs = staged("r2", prompt=OTHER_PROMPT)

    obs = probes.agent_sessions([mine, theirs], REPO)

    assert agentregistry.bound_session("r1") == OURS
    assert agentregistry.bound_session("r2") == THEIRS
    assert obs.value["r1"].busy is False and obs.value["r2"].busy is True


def test_a_hermes_run_whose_session_cannot_be_found_is_simply_absent():
    """Absent, not wrong: the resolver reads its screen instead, so a run this cannot
    reach costs the older evidence and never a verdict."""
    store({THEIRS: [user(OTHER_PROMPT)]})
    obs = probes.agent_sessions([staged("r1")], REPO)
    assert obs.ok and "r1" not in obs.value


def test_the_runner_a_run_started_under_is_what_decides_who_is_asked():
    """The setting is what the NEXT spawn uses. A run recorded as Claude Code — or one
    started before runs recorded a runner at all — is read off its screen, even with a
    matching session sitting in Hermes' store."""
    store({OURS: [user(PROMPT), FINISHED]})
    record = staged("r1")
    agentregistry.runner_path("r1").write_text(runner.CLAUDE, encoding="utf-8")
    assert probes.agent_sessions([record], REPO).status == "unsupported"

    agentregistry.runner_path("r1").unlink()
    assert probes.agent_sessions([record], REPO).status == "unsupported"
