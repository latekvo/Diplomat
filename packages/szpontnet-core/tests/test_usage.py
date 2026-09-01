"""The token-budget probe, and what it costs the machine it runs on.

The OAuth usage endpoint is not a private resource: it answers out of one small
per-account bucket that every Claude Code session on the box spends too, and a
refusal is the ordinary answer rather than an error. So the interesting properties
here are about *rate* — how often the node asks, what one ask is willing to sit
through, and what else on the node stops while it does.
"""

from __future__ import annotations

import threading
import time

import pytest

from szpontnet import node as nodemod
from szpontnet import statefile, usage


@pytest.fixture
def refusals(monkeypatch):
    """A probe whose endpoint refuses the first ``n`` attempts, as the real one does
    when another Claude Code session on the machine reached the shared bucket first.

    Yields a setter returning the attempt log, and takes the waits out of the clock
    so a test costs nothing to run.
    """
    monkeypatch.delenv("SZPONTNET_OAUTH_PROBE", raising=False)
    monkeypatch.setattr(usage, "_oauth_tokens", lambda: ["oat-test"])
    monkeypatch.setattr(usage.time, "sleep", lambda _: None)
    usage._reset_probe_cache()

    def refuse(n: int) -> list[int]:
        log: list[int] = []

        def fetch(token):
            log.append(token)
            if len(log) <= n:
                return None
            return {"five_hour": {"utilization": 40},
                    "seven_day": {"utilization": 10}}

        monkeypatch.setattr(usage, "_fetch_usage_payload", fetch)
        return log

    try:
        yield refuse
    finally:
        usage._reset_probe_cache()  # never leak a fake reading into another test


# MARK: - one refresh


def test_one_refusal_costs_an_insisting_probe_an_attempt_not_the_reading(refusals):
    """A single attempt comes back empty more often than not on a busy machine, and
    an empty refresh puts the node back on the rough local-log heuristic — the
    signal peers rank on, dropped for the whole cadence over a busy minute."""
    log = refusals(2)
    session, _week = usage.windows(insist=True)
    assert session is not None and session.frac_left == 0.6
    assert len(log) == 3


def test_a_probe_that_is_not_insisting_takes_the_one_attempt(refusals):
    """The control, and what the seed in ``MeshNode.start`` takes: the first advert
    waits on it, so minutes of retries would trade a rough fraction for a node that
    is not on the mesh at all yet."""
    log = refusals(2)
    assert usage.windows() == (None, None)
    assert len(log) == 1


def test_an_insisting_probe_gives_up_after_its_last_attempt(refusals):
    """An endpoint refusing everything has to end the refresh, not hold the worker
    thread — and the loop behind it — indefinitely."""
    log = refusals(999)
    assert usage.windows(insist=True) == (None, None)
    assert len(log) == usage._INSIST_ATTEMPTS + 1


def test_a_logged_out_machine_is_not_worth_insisting_to(refusals, monkeypatch):
    """No token is the one failure retrying cannot fix. Without this a machine that
    is simply logged out would sleep out the whole schedule on every refresh."""
    log = refusals(999)
    monkeypatch.setattr(usage, "_oauth_tokens", lambda: [])
    waits: list[float] = []
    monkeypatch.setattr(usage.time, "sleep", waits.append)
    assert usage.windows(insist=True) == (None, None)
    assert waits == [], "the insist schedule was sat out with nothing to ask with"
    assert log == [], "a request was sent without a credential to send it under"


def test_a_refused_credential_is_not_the_last_word(refusals, monkeypatch):
    """The reason a node can be pinned to a dead credential. On macOS Claude Code
    refreshes the Keychain item and never rewrites a ``.credentials.json`` an older
    login left behind, so the file's token can be expired while the Keychain's is
    live — and the endpoint answers a refused token exactly as it answers a busy
    bucket. A probe that stops at the first credential therefore routes on the local
    heuristic forever on a machine where the real reading was one request away.
    """
    log = refusals(0)
    monkeypatch.setattr(usage, "_oauth_tokens", lambda: ["stale", "live"])

    def fetch(token):
        log.append(token)
        return None if token == "stale" else {"five_hour": {"utilization": 40},
                                              "seven_day": {"utilization": 10}}

    monkeypatch.setattr(usage, "_fetch_usage_payload", fetch)
    session, week = usage.windows()
    assert session is not None and session.frac_left == 0.6
    assert log == ["stale", "live"], "the refused credential ended the round"


def test_a_credential_that_answers_costs_the_round_nothing_extra(refusals):
    """The whole list is tried only where the probe was already failing. The bucket
    is shared with every Claude Code session on the box, so a round that already has
    its reading must not spend a second request proving the point."""
    log = refusals(0)
    usage.windows()
    assert log == ["oat-test"]


def test_a_reading_inside_the_ttl_is_answered_from_the_cache(refusals):
    """What bounds the node's draw on the shared bucket. The refresh loop ticks far
    more often than the probe samples, so every tick but the one past the TTL has to
    cost nothing — insisting or not."""
    log = refusals(0)
    assert usage.windows(insist=True)[0].frac_left == 0.6
    assert usage.windows(insist=True)[0].frac_left == 0.6
    assert usage.windows()[0].frac_left == 0.6
    assert len(log) == 1


def test_a_stop_lands_inside_a_wait_rather_than_after_it(refusals, monkeypatch):
    """What a node's shutdown rides on. The schedule runs in a worker thread, and
    cancelling the task awaiting that thread does not end it — the interpreter joins
    the thread on the way out either way, so a stop arriving mid-refusal would hold
    the process for the rest of the two and a half minutes. The wait has to see the
    stop while it is in it, not between attempts."""
    log = refusals(999)
    monkeypatch.setattr(usage, "_INSIST_WAIT_SECS", 30.0)  # a wait worth cutting short
    stopping = threading.Event()
    threading.Timer(0.2, stopping.set).start()

    started = time.monotonic()
    assert usage.windows(insist=True, stopping=stopping) == (None, None)
    assert time.monotonic() - started < 5.0, "the stop waited out the attempt it was in"
    assert len(log) == 1, "attempts kept going after the stop"


# MARK: - the node's own loop


#: How long the stand-in probe below sits in its worker thread, and how much of that
#: stall the state file is then measured across. The real one blocks for up to two and
#: a half minutes; the margin between the two is what keeps the measurement off the
#: edge of the stall on a loaded runner.
_STALL_SECS = 3.0
_MEASURE_SECS = 1.0


def _probe_recording(calls: list[bool], block: threading.Event | None = None):
    """A stand-in for ``usage.token_state`` that records how each caller asked, and
    optionally sits there once the way an insisting probe does through a refusal."""
    def token_state(_plan, _now=None, *, insist=False, stopping=None):
        calls.append(insist)
        if block is not None and insist and not block.is_set():
            block.set()
            # Sits there as an insisting probe does, and gives up when the node stops
            # — so the node's own teardown is not what a test ends up measuring.
            stopping.wait(_STALL_SECS)
        return "ok", 1.0, None, None, 1.0

    return token_state


def test_the_seed_asks_once_and_the_loop_behind_it_insists(simnet, monkeypatch):
    """The two callers want opposite things from the same probe. ``start`` is on the
    critical path to the first advert and takes whatever one attempt gives it; the
    loop has nobody waiting on it and can afford to sit out a refusal, which is what
    makes a fifteen-minute cadence enough to keep a reading."""
    calls: list[bool] = []
    monkeypatch.setattr(nodemod, "_TOKEN_REFRESH_SECS", 0.05)
    monkeypatch.setattr(usage, "token_state", _probe_recording(calls))

    async def scenario():
        await simnet.node("a")
        assert calls == [False], "the seed insisted, and held up the first advert"
        await simnet.until(lambda: True in calls, 4.0,
                           "the refresh loop never asked, or never insisted")

    simnet.run(scenario())


def test_a_probe_that_sits_out_a_refusal_does_not_stop_the_state_file(
        simnet, monkeypatch):
    """Why the refresh is a loop of its own rather than a branch of the snapshot one.

    An insisting probe blocks for up to two and a half minutes. Awaited from inside
    the snapshot loop that would stop `state.json` being written for as long — every
    peer row, every claim, the whole of what a front-end and `--status` read, frozen
    while a node waits its turn at somebody else's rate limiter.
    """
    blocked = threading.Event()
    writes: list[int] = []
    real_write = statefile.write_state
    monkeypatch.setattr(nodemod, "_TOKEN_REFRESH_SECS", 0.05)
    monkeypatch.setattr(usage, "token_state", _probe_recording([], blocked))
    monkeypatch.setattr(statefile, "write_state",
                        lambda snap: writes.append(1) or real_write(snap))

    async def scenario():
        await simnet.node("a")
        await simnet.until(blocked.is_set, 4.0, "the probe never stalled")
        # Measured DURING the stall, not after it: a loop that blocks and then
        # catches up passes any assertion made once the probe has returned.
        mark = len(writes)
        await simnet.quiet(_MEASURE_SECS)
        assert len(writes) - mark >= 3, \
            "the stalled probe took the state file with it"

    simnet.run(scenario())


def test_stopping_a_node_lets_go_of_a_probe_mid_schedule(simnet, monkeypatch):
    """The other half of the same problem, on the node's side. A probe two attempts
    into its schedule is sitting in a worker thread, and the interpreter joins that
    thread on the way out no matter that the task awaiting it was cancelled — so a
    stop that does not tell the probe to give up is a ``--stop`` that appears to hang,
    and a node the next one reaps with SIGKILL instead of the SIGTERM it slept through."""
    stalled, released = threading.Event(), threading.Event()
    monkeypatch.setattr(nodemod, "_TOKEN_REFRESH_SECS", 0.05)

    def token_state(_plan, _now=None, *, insist=False, stopping=None):
        if insist and not stalled.is_set():
            stalled.set()
            if stopping.wait(_STALL_SECS):   # False when it timed out un-stopped
                released.set()
        return "ok", 1.0, None, None, 1.0

    monkeypatch.setattr(usage, "token_state", token_state)

    async def scenario():
        node = await simnet.node("a")
        await simnet.until(stalled.is_set, 4.0, "the probe never stalled")
        await node.stop()
        await simnet.until(released.is_set, 2.0,
                           "the stopped node still sat out the probe's schedule")

    simnet.run(scenario())
