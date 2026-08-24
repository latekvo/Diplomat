"""Asking a Hermes agent what it is doing, out of the store it writes as it works.

The same question :mod:`test_opencode_api` asks over a port, answered from SQLite:
Hermes serves no per-run server, but it writes every session and every message to
``~/.hermes/state.db`` mid-turn, and a turn is over exactly when the agent stamps its
own message ``finish_reason``.

The same three things have to hold, and each is a group below:

* the answer must be **read correctly** — including that a tool call in flight is the
  middle of a turn, that "no messages yet" is not "idle", and that a turn the agent
  ended is not the end of a run a background subagent still owes a result to;
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

from diplomat_runtime import agentregistry, hermesstore, runner
from diplomat_app import probes
from diplomat_runtime.agentstate import RunRecord, SessionState

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
  cache_write_tokens INTEGER, model TEXT, estimated_cost_usd REAL,
  actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT, content TEXT, finish_reason TEXT);
CREATE TABLE async_delegations (delegation_id TEXT PRIMARY KEY, origin_session TEXT,
  parent_session_id TEXT, state TEXT, dispatched_at REAL, delivery_state TEXT);
"""

#: A store from before Hermes could delegate in the background, which has no such
#: table at all. Kept whole for the same reason :data:`SCHEMA_UNPRICED` is: the reader
#: is held to a shape that really existed rather than to a subset this file invented.
SCHEMA_UNDELEGATED = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, started_at REAL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER, model TEXT, estimated_cost_usd REAL,
  actual_cost_usd REAL);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT, content TEXT, finish_reason TEXT);
"""

#: The pre-pricing schema, which a store written by an older Hermes still has. Kept
#: whole rather than derived from the one above, so the reader is tested against a
#: shape that really existed instead of against a subset this file invented.
SCHEMA_UNPRICED = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, cwd TEXT, started_at REAL,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  cache_write_tokens INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
  role TEXT, content TEXT, finish_reason TEXT);
"""

#: What the sessions Hermes writes here are actually priced against.
MODEL = "deepseek/deepseek-v4-flash-0731"


def store(sessions: dict, cwds: dict | None = None,
          delegations: list[tuple] | None = None, schema: str = SCHEMA) -> None:
    """Write a Hermes store where the fenced-off reader is looking.

    ``sessions`` maps a session id to its messages — ``(role, content,
    finish_reason)`` triples, oldest first — in the order they were started, one
    second apart. That is the arrangement the applet's task cap makes ordinary:
    several agents in one checkout, seconds apart. ``cwds`` overrides where one was
    started, and ``delegations`` are ``(parent_session_id, origin_session,
    delivery_state)`` rows of the background fan-outs those sessions dispatched.
    """
    path = hermesstore.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        for i, (parent, origin, delivery) in enumerate(delegations or []):
            conn.execute(
                "INSERT INTO async_delegations (delegation_id, origin_session, "
                "parent_session_id, state, dispatched_at, delivery_state) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (f"deleg_{i}", origin, parent, T0, delivery))
        for i, (session_id, messages) in enumerate(sessions.items()):
            conn.execute(
                "INSERT INTO sessions (id, source, cwd, started_at, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens, model) "
                "VALUES (?, 'tui', ?, ?, 100, 20, 9000, 5, ?)",
                (session_id, (cwds or {}).get(session_id, REPO), T0 + i, MODEL))
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


# MARK: - What the turn left running behind it


def test_a_background_subagent_that_has_not_reported_holds_its_run_open():
    """``delegate_task(background=true)`` hands the turn straight back and reports
    later as a fresh user turn, so the agent sits at its prompt with the fan-out still
    spending. Read as a finished run, its bay and its PR go to another agent while
    this one is about to wake up and keep working on the same review."""
    store({OURS: [user(PROMPT), FINISHED]}, delegations=[(OURS, "", "pending")])
    assert hermesstore.state_of(OURS) == SessionState(busy=True)


def test_a_result_already_folded_back_in_holds_nothing_open():
    """Delivered is the whole point of the state: the completion has been handed to
    the agent, so whatever it did with it is in the messages this reads. ``dropped`` is
    Hermes giving up on one, which will never wake anybody either."""
    store({OURS: [user(PROMPT), FINISHED]},
          delegations=[(OURS, OURS, "delivered"), (OURS, OURS, "dropped")])
    assert hermesstore.state_of(OURS) == SessionState(busy=False)


def test_another_session_s_fan_out_says_nothing_about_this_one():
    """The store is machine-wide, and the box the applet runs on has every agent's
    delegations in it — including the ones its own subagents dispatch."""
    store({OURS: [user(PROMPT), FINISHED], THEIRS: [user(OTHER_PROMPT), WORKING]},
          delegations=[(THEIRS, THEIRS, "pending")])
    assert hermesstore.state_of(OURS) == SessionState(busy=False)


def test_a_delegation_stamped_only_with_its_routing_key_still_counts():
    """``parent_session_id`` is read off the agent and is nullable; the routing key
    beside it is stamped from that same id on the ``--tui`` spawn this applet makes.
    Either column naming the session is the fan-out being this one's."""
    store({OURS: [user(PROMPT), FINISHED]}, delegations=[(None, OURS, "pending")])
    assert hermesstore.state_of(OURS) == SessionState(busy=True)


def test_a_store_too_old_to_delegate_in_the_background_still_ends_a_turn():
    """A Hermes that cannot dispatch one owes nothing, so the missing table is an
    answer rather than a failure — otherwise every run on such a build falls back to
    its screen, which is the inference this whole module exists to replace."""
    store({OURS: [user(PROMPT), FINISHED]}, schema=SCHEMA_UNDELEGATED)
    assert hermesstore.delegating(OURS) is False
    assert hermesstore.state_of(OURS) == SessionState(busy=False)


def test_a_delegation_table_this_build_cannot_read_leaves_the_turn_unjudged():
    """The other way the store goes quiet: the table is there but not in a shape this
    knows. Nothing is proved either way, so the run keeps its older evidence — reading
    it as a finished turn would end a run on a delegation nobody could read."""
    store({OURS: [user(PROMPT), FINISHED], THEIRS: [user(OTHER_PROMPT), WORKING]})
    conn = sqlite3.connect(hermesstore.db_path())
    try:
        conn.executescript("DROP TABLE async_delegations;"
                           "CREATE TABLE async_delegations (delegation_id TEXT);")
        conn.commit()
    finally:
        conn.close()

    assert hermesstore.delegating(OURS) is None
    assert hermesstore.state_of(OURS) is None
    # A turn still in flight is answered by the message alone, so the same broken
    # table costs it nothing: only the end of a turn has to ask what is outstanding.
    assert hermesstore.state_of(THEIRS) == SessionState(busy=True)


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
    assert hermesstore.session_price(THEIRS) == (None, "")


def _charge(session_id: str, *, estimated=None, actual=None) -> None:
    """Price a session the way Hermes does as it runs: an estimate from the
    provider's published rates, settled later if the provider reports a real figure."""
    conn = sqlite3.connect(hermesstore.db_path())
    try:
        conn.execute("UPDATE sessions SET estimated_cost_usd = ?, actual_cost_usd = ? "
                     "WHERE id = ?", (estimated, actual, session_id))
        conn.commit()
    finally:
        conn.close()


def test_a_run_is_priced_in_money_against_the_model_it_ran_on():
    """The unit an OpenRouter-billed run is actually held to. The model travels with
    it because the money means nothing without it — the same task is cents on this
    model and dollars on a frontier one."""
    store({OURS: [user(PROMPT), FINISHED]})
    _charge(OURS, estimated=0.067540928)

    assert hermesstore.session_price(OURS) == (0.067540928, MODEL)


def test_a_settled_charge_is_preferred_to_the_estimate_that_stood_in_for_it():
    store({OURS: [user(PROMPT), FINISHED]})
    _charge(OURS, estimated=0.067540928, actual=0.071)

    assert hermesstore.session_price(OURS)[0] == 0.071


def test_a_zero_in_the_settled_column_is_the_column_being_empty():
    """Not a free task: reading it as one would put a 0 into the distribution the next
    task is gated on, and drag the bound below what every run really costs. The same
    case is asserted against the Swift twin in ``DiplomatCoreSmoke``."""
    store({OURS: [user(PROMPT), FINISHED]})
    _charge(OURS, estimated=0.067540928, actual=0)

    assert hermesstore.session_price(OURS)[0] == 0.067540928


def test_a_session_hermes_has_not_priced_yet_carries_no_money():
    """A row written before the provider answered. Not a free task — a task with no
    price, which the budget gate must not average in as a zero."""
    store({OURS: [user(PROMPT), FINISHED]})

    assert hermesstore.session_price(OURS) == (None, MODEL)


def test_a_store_older_than_the_price_columns_is_read_without_them():
    """Hermes gained these columns; a store written before it did still answers every
    other question. Losing the money must not lose the run."""
    path = hermesstore.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_UNPRICED)
        conn.execute(
            "INSERT INTO sessions (id, source, cwd, started_at, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens) "
            "VALUES (?, 'tui', ?, ?, 100, 20, 9000, 5)", (OURS, REPO, T0))
        conn.commit()
    finally:
        conn.close()

    assert hermesstore.session_price(OURS) == (None, "")
    assert hermesstore.session_tokens(OURS) == 100 + 20 + 5


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


def test_a_run_still_owed_a_fan_out_reads_as_working_through_the_probe():
    """The whole path a tick takes: the run's runner and prompt out of its directory,
    the session matched by prompt, and both halves of the answer read from the store
    the agent is writing."""
    store({OURS: [user(PROMPT), FINISHED]}, delegations=[(OURS, OURS, "pending")])
    record = staged("r1")

    obs = probes.agent_sessions([record], REPO)

    assert obs.ok and obs.value["r1"] == SessionState(busy=True)


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
