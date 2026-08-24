"""Tests for the PR auto-fix monitor: the pure decision logic (autofix.py) and the
Store orchestration (poll → diff → dispatch → reconcile, with dedup + backoff)."""

from __future__ import annotations

import dataclasses
import math
import time

import pytest

from diplomat_runtime import autofix, review, telemetry
from diplomat_runtime.autofix import (
    PRFingerprint,
    PRSnapshot,
    ReviewAttempt,
    ReviewRequest,
    VerdictPolicy,
    compute_diff,
    decide,
    retry_delay,
)


# MARK: - compute_diff (edge trigger)


def _snap(number=1, mergeable="MERGEABLE", review_decision="", unresolved=0, i_owe=0):
    return PRSnapshot(
        number=number,
        title=f"PR {number}",
        url=f"https://x/pr/{number}",
        is_draft=False,
        mergeable=mergeable,
        review_decision=review_decision,
        threads_unresolved=unresolved,
        threads_i_owe=i_owe,
    )


def test_first_sighting_is_silent():
    events, fps = compute_diff({}, [_snap(mergeable="CONFLICTING", unresolved=3)])
    assert events == []  # never fire on a PR we've never seen before
    assert fps[1].mergeable == "CONFLICTING"


def test_conflict_transition_fires_once():
    prior = {1: PRFingerprint("MERGEABLE", "", 0)}
    events, _ = compute_diff(prior, [_snap(mergeable="CONFLICTING")])
    assert ("conflict", ) == tuple(k for k, _ in events)


def test_still_conflicting_does_not_refire():
    prior = {1: PRFingerprint("CONFLICTING", "", 0)}
    events, _ = compute_diff(prior, [_snap(mergeable="CONFLICTING")])
    assert events == []


def test_unknown_mergeable_carries_prior_forward():
    prior = {1: PRFingerprint("CONFLICTING", "", 0)}
    events, fps = compute_diff(prior, [_snap(mergeable="UNKNOWN")])
    assert events == []  # not re-fired
    assert fps[1].mergeable == "CONFLICTING"  # conflict state preserved, not lost


def test_more_threads_fires_review():
    prior = {1: PRFingerprint("MERGEABLE", "", 1)}
    events, _ = compute_diff(prior, [_snap(unresolved=2)])
    assert ("review", ) == tuple(k for k, _ in events)


def test_new_changes_requested_fires_review():
    prior = {1: PRFingerprint("MERGEABLE", "", 0)}
    events, _ = compute_diff(prior, [_snap(review_decision="CHANGES_REQUESTED")])
    assert ("review", ) == tuple(k for k, _ in events)


# MARK: - retry backoff + decide


def test_retry_delay_schedule():
    assert retry_delay(0) == 0.0
    assert retry_delay(1) == 5 * 60
    assert retry_delay(2) == 10 * 60
    assert retry_delay(3) == 20 * 60
    assert retry_delay(99) == autofix.RETRY_MAX_BACKOFF  # capped


def test_decide_banned_and_in_flight_short_circuit():
    assert decide(None, "s", in_flight=False, banned=True, now_ts=0)[0] == "banned"
    assert decide(None, "s", in_flight=True, banned=False, now_ts=0)[0] == "in_flight"


def test_decide_first_dispatch():
    assert decide(None, "s", False, False, 100.0) == ("dispatch", 1)


def test_decide_same_stamp_backoff_then_retry():
    prior = ReviewAttempt("s", last_dispatched_at=100.0, attempts=1)
    # 4 min after a 5-min backoff → still cooling
    action, remaining = decide(prior, "s", False, False, 100.0 + 4 * 60)
    assert action == "cooling" and remaining == pytest.approx(60)
    # 5+ min later → retry as attempt 2
    assert decide(prior, "s", False, False, 100.0 + 5 * 60 + 1) == ("dispatch", 2)


def test_decide_changed_stamp_cooldown():
    prior = ReviewAttempt("old", last_dispatched_at=100.0, attempts=1)
    # A different request stamp within the 1h cooldown → suppressed
    assert decide(prior, "new", False, False, 100.0 + 30 * 60)[0] == "cooling"
    # After the cooldown → fresh attempt 1
    assert decide(prior, "new", False, False, 100.0 + 60 * 60 + 1) == ("dispatch", 1)


# MARK: - ReviewRequest.owe_review


def _req(requested_at=None, my_last_review_at=None, author_association="MEMBER",
         files=None, number=7, my_last_comment_at=None):
    return ReviewRequest(
        number=number, title="t", url=f"https://x/pr/{number}", author="bob",
        author_association=author_association, files=files or [],
        requested_at=requested_at, my_last_review_at=my_last_review_at,
        my_last_comment_at=my_last_comment_at,
    )


def test_owe_review_rules():
    assert _req(requested_at=None).owe_review is True  # requested, no detail → owed
    assert _req("2026-01-02", None).owe_review is True  # never reviewed
    assert _req("2026-01-02", "2026-01-01").owe_review is True  # request newer
    assert _req("2026-01-01", "2026-01-02").owe_review is False  # already reviewed since


def test_owe_review_counts_a_soft_approve_comment_as_a_response():
    """A clean PR's auto-response is a friendly top-level comment, never a review
    verdict. It must clear the owed state or the monitor re-dispatches every backoff
    cycle forever (the real #516 regression: request 07-24 06:47 vs a soft-approve
    comment 24 min later, with the last formal review four days stale)."""
    # Comment posted AFTER the request → responded, not owed (even with no/old review).
    assert _req("2026-07-24T06:47:36Z", my_last_review_at="2026-07-20T11:37:37Z",
                my_last_comment_at="2026-07-24T07:11:35Z").owe_review is False
    assert _req("2026-01-02", my_last_review_at=None,
                my_last_comment_at="2026-01-03").owe_review is False
    # A comment OLDER than the request doesn't count — still owed.
    assert _req("2026-01-02", my_last_comment_at="2026-01-01").owe_review is True
    # A fresh re-request (newer than both my review and my comment) re-arms it.
    assert _req("2026-01-05", my_last_review_at="2026-01-03",
                my_last_comment_at="2026-01-04").owe_review is True
    # The later of review / comment wins when the review is the more recent response.
    assert _req("2026-01-02", my_last_review_at="2026-01-03",
                my_last_comment_at="2026-01-01").owe_review is False


def test_parse_review_requests_reads_my_comment_time():
    """The review-requests parser must lift MY latest top-level comment out of the
    `comments` connection (filtered to me, case-insensitive) so a soft-approve clears
    the owed state. Mirrors the #516 shape: an old review, a newer soft-approve
    comment, a request in between."""
    from diplomat_app import autofixmonitor

    env = {"data": {"search": {"nodes": [
        {
            "number": 516,
            "title": "feat",
            "url": "https://github.com/o/r/pull/516",
            "author": {"login": "j-piasecki"},
            "authorAssociation": "MEMBER",
            "headRefOid": "64d41d9",
            "timelineItems": {"nodes": [
                {"createdAt": "2026-07-24T06:47:36Z",
                 "requestedReviewer": {"__typename": "User", "login": "Latekvo"}},
            ]},
            "reviews": {"nodes": [
                {"author": {"login": "latekvo"}, "submittedAt": "2026-07-20T11:37:37Z"},
                {"author": {"login": "someone"}, "submittedAt": "2026-07-25T00:00:00Z"},
            ]},
            "comments": {"nodes": [
                {"author": {"login": "bob"}, "createdAt": "2026-07-24T09:00:00Z"},
                {"author": {"login": "LATEKVO"}, "createdAt": "2026-07-24T07:11:35Z"},
            ]},
        },
    ]}}}
    reqs = autofixmonitor._parse_review_requests(env, "latekvo")
    assert len(reqs) == 1
    r = reqs[0]
    assert r.requested_at == "2026-07-24T06:47:36Z"
    assert r.my_last_review_at == "2026-07-20T11:37:37Z"  # only MINE, not someone's
    assert r.my_last_comment_at == "2026-07-24T07:11:35Z"  # only MINE, case-insensitive
    assert r.owe_review is False  # the soft-approve comment answered the re-request


# MARK: - VerdictPolicy


def test_is_community():
    assert autofix.is_community("NONE") is True
    assert autofix.is_community("CONTRIBUTOR") is False  # trusted per filters.json
    assert autofix.is_community("member") is False  # case-insensitive


def test_verdict_withhold_reasons():
    pol = VerdictPolicy(withhold_skill=True, withhold_installer=True, withhold_community=True)
    assert pol.withhold_reasons([], "MEMBER") == []  # clean, trusted → verdict allowed
    assert pol.allows_verdict([], "MEMBER") is True
    assert "community PR" in pol.withhold_reasons([], "NONE")
    assert "touches a SKILL" in pol.withhold_reasons(["foo/bar.skill.md"], "MEMBER")
    assert "touches the installer" in pol.withhold_reasons(
        ["packages/argent-installer/x.ts"], "MEMBER"
    )
    # A disabled suppressor doesn't fire even on a matching PR.
    lax = VerdictPolicy(withhold_skill=False, withhold_installer=False, withhold_community=False)
    assert lax.allows_verdict(["a.skill.md"], "NONE") is True


# MARK: - Store orchestration


# The real probe layer, captured before any fixture replaces it — for the one test
# that is about a probe's own failure handling rather than about a machine's state.
from diplomat_app.probes import gather as REAL_GATHER  # noqa: E402

# Real CLI buffers: the interrupt hint on the live status bar means mid-turn, its
# absence means the session is back at its prompt.
WORKING = "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
AT_PROMPT = "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"


@pytest.fixture
def store(monkeypatch):
    from diplomat_app.store import Store

    st = Store()
    st.me = "alice"  # skip the gh viewer-login shell-out
    # Never run the diplomat-core CLI in a unit test: stub the prompt builder.
    monkeypatch.setattr(
        "diplomat_runtime.promptcore.build_prompt",
        lambda cfg: f"PROMPT:{cfg.get('kind')}:"
                    f"{cfg.get('specificPR') or cfg.get('specificIssue')}",
    )
    # The probes would read this MACHINE's real processes, tmux panes and mesh —
    # neutralize them so a test exercises the registry and the resolver rather than
    # the developer's box. The default is an empty machine that WAS successfully
    # looked at; a scan-specific test overrides with `fake_probes(...)`.
    fake_probes(monkeypatch)
    return st


def fake_probes(monkeypatch, *, processes=None, claims=None, merged=None,
                live_prs=None, idle_prs=(), tails=None, activity=None):
    """Replace the whole evidence layer with a literal.

    This is the seam the old `monkeypatch.setattr(Store, "_live_pr_agents", …)` became.
    It sits one layer lower and is typed: a test says what the machine looked like,
    including which probes could not answer, and the resolver does the rest. Anything
    not named is PRESENT-and-empty — a machine that was looked at and had nothing on
    it, which is the opposite of a probe that failed.

    ``live_prs`` are PRs with an agent this applet has no record of, as the legacy
    prompt-text scan finds them; each is given a tty and a screen, because without a
    screen such an agent can never be seen to finish its turn. ``idle_prs`` are the
    ones sitting at their prompt. An ``Observation`` passes straight through, which is
    how a test says the scan itself could not be read.
    """
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A
    from diplomat_app import probes

    def obs(v, empty):
        if v is None:
            return A.Observation.present(empty)
        return v if isinstance(v, A.Observation) else A.Observation.present(v)

    live = (live_prs if isinstance(live_prs, A.Observation)
            else None if live_prs is None
            else {pr: f"pts/{pr}" for pr in live_prs})
    screens = {} if not isinstance(live, dict) else {
        f"pts/{pr}": (AT_PROMPT if pr in idle_prs else WORKING) for pr in live}
    screens.update(tails or {})

    def gather(records, now, merged=None):
        return A.Evidence(
            processes=obs(processes, {}),
            # Real, because they read the run directories the test itself created.
            sentinels=agentregistry.sentinels(records),
            activity=(obs(activity, {}) if activity is not None
                      else agentregistry.activity(records)),
            tails=obs(screens, {}),
            claims=obs(claims, set()),
            merged_prs=obs(merged, set()),
            live_agents=obs(live, {}),
        )

    monkeypatch.setattr(probes, "gather", gather)


def register_run(number, *, source=autofix.SOURCE_AUTO, pid=None, tty="",
                 dispatched_at=None, label="", kind="review", ledger_key="",
                 placement=None, node="", work_key="", prompt="P"):
    """Put one run in the registry, as a dispatch would. Returns the record."""
    import time as _time

    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    now = _time.time() if dispatched_at is None else dispatched_at
    return agentregistry.create_run(
        A.RunRecord(run_id=agentregistry.new_run_id(now), dispatched_at=now,
                    pr_number=number, pr_url=f"https://github.com/o/r/pull/{number}",
                    kind=kind, label=label, source=source,
                    placement=placement or A.PLACEMENT_LOCAL, node=node,
                    work_key=work_key, ledger_key=ledger_key, pid=pid, tty=tty),
        prompt)


def agent_alive(pid, *, tty="pts/1", elapsed=5.0):
    """One live agent for `fake_probes(processes=…)`."""
    from diplomat_runtime import agentstate as A
    return {pid: A.ProcInfo(tty=tty, elapsed=elapsed, is_agent=True)}


def _spawn_recorder(monkeypatch, finish=False):
    """Patch review.spawn to record calls (and optionally create the done sentinel,
    simulating an agent that finished immediately so the in-flight guard clears)."""
    calls = []

    def fake_spawn(prompt, preferred, done_path=None, pid_path=None, prompt_file=None,
                   port=None, settings_file=None):
        calls.append({"prompt": prompt, "done": done_path, "pid": pid_path,
                      "prompt_file": prompt_file, "port": port,
                      "settings_file": settings_file})
        if finish and done_path:
            with open(done_path, "w") as fh:
                fh.write("0")
        return prompt_file or "/tmp/prompt.txt"

    monkeypatch.setattr(review, "spawn", fake_spawn)
    return calls


def test_an_unstubbed_spawn_is_refused_not_launched():
    """Guards the conftest backstop (``no_host_agent_spawn``). A dispatch test that
    forgets :func:`_spawn_recorder` must fail, not open a terminal running claude in
    the operator's own checkout — the spawn is fire-and-forget, so without this the
    test still passes green while a live agent is loose on their machine."""
    with pytest.raises(AssertionError, match="real agent launch"):
        review.spawn("prompt", None)


def test_the_poll_still_runs_with_both_monitors_off(store, monkeypatch):
    """Turning the monitors off stops the automatic START, not the looking: what my
    PRs owe is worth seeing either way, and the panel's queue is where it is seen. So
    the 3-minute GitHub poll keeps running and keeps listing — it just spawns
    nothing."""
    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fetched = []
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: fetched.append(1) or [_snap(number=8, mergeable="CONFLICTING")],
    )
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )

    store.run_autofix_poll_async()
    # The worker holds the poll-overlap guard for its whole run, so taking it back is
    # how a caller waits for the poll to finish.
    assert store._poll_lock.acquire(timeout=5.0), "the poll worker never finished"
    store._poll_lock.release()

    assert fetched == [1] and calls == []
    assert [t.id for t in store.queued_tasks] == ["conflicts:8"]


def test_conflict_dispatch_and_backoff(store, monkeypatch):
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=True)  # agent finishes → clears in-flight
    snaps = [_snap(number=42, mergeable="CONFLICTING")]
    # Seed a prior fingerprint so it's not a first-sighting (edge is a no-op for
    # conflicts anyway; the level-triggered reconciler does the work).
    store._save_fingerprints({42: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)

    store._poll_my_prs(snaps)
    assert len(calls) == 1
    assert "conflicts" in calls[0]["prompt"]  # kind=conflicts
    assert store.autofix_conflicts_handled == 1

    # An immediate second poll must NOT re-dispatch (ReviewReconcile 5-min backoff).
    store._poll_my_prs(snaps)
    assert len(calls) == 1
    assert store.autofix_conflicts_handled == 1


def test_in_flight_dedup(store, monkeypatch):
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)  # agent still running
    snaps = [_snap(number=9, mergeable="CONFLICTING")]
    store._save_fingerprints({9: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)

    store._poll_my_prs(snaps)
    store._poll_my_prs(snaps)  # sentinel still absent → still in flight
    assert len(calls) == 1  # not re-spawned while the first agent runs


def test_review_request_verdict_gating(store, monkeypatch):
    store.pr_autofix_enabled = False
    store.review_requests_enabled = True
    store.auto_approve_enabled = True
    calls = _spawn_recorder(monkeypatch, finish=True)
    # A clean PR by a trusted author → verdict allowed (final_pass=true in prompt).
    reqs = [_req(requested_at="2026-01-02", author_association="MEMBER", files=["a.py"])]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs)
    store._poll_review_requests("o", "r")
    assert len(calls) == 1 and "review" in calls[0]["prompt"]
    assert store.review_requests_handled == 1

    # A different SKILL PR → verdict withheld: still dispatched (comments-only).
    calls.clear()
    skill_reqs = [_req(number=8, requested_at="2026-02-02", author_association="MEMBER",
                       files=["foo.skill.md"])]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: skill_reqs)
    store._poll_review_requests("o", "r")
    assert len(calls) == 1  # dispatched (comments-only)


def test_unaddressed_count_and_ban_skip(store, monkeypatch):
    store.pr_autofix_enabled = False
    store.review_requests_enabled = True
    _spawn_recorder(monkeypatch, finish=True)
    reqs = [
        _req(requested_at="2026-01-02", author_association="MEMBER"),  # owed, dispatched
    ]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    # Dispatched + finished → no longer in-flight → counts as still unaddressed until
    # the reviewer resolves it (the reconciler will retry on the next poll).
    assert store.unaddressed_reviews == 1


def test_poll_error_surfaced_and_recovers(store, monkeypatch):
    store.review_requests_enabled = False
    # Both monitors fetch on every cycle whatever their toggles say, so the other
    # one is stubbed too — otherwise it reaches the real `gh` and answers for the
    # poll error this test is about.
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", boom)
    store._autofix_poll_once()
    assert store.autofix_poll_error and "gh exploded" in store.autofix_poll_error

    # Recovery clears it.
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    store._autofix_poll_once()
    assert store.autofix_poll_error is None


def test_build_prompt_failure_surfaces_as_poll_error_not_a_dead_worker(store, monkeypatch):
    """A diplomat-core build-prompt failure (subprocess non-zero / binary absent ->
    RuntimeError/CoreBinaryMissing) is raised EAGERLY while constructing AgentJob(prompt=...)
    in a _dispatch_* helper — before dispatch_agent's own guard. It must surface as a poll
    failure (light the pill) exactly like a fetch failure, and NEVER escape _autofix_poll_once
    to kill the daemon poll worker thread (after which every poll silently no-ops and a stale
    error never clears). Regression for the unguarded poll body (only fetch_* was wrapped)."""
    store.review_requests_enabled = False
    _spawn_recorder(monkeypatch, finish=True)  # the recovery poll DISPATCHES — never spawn for real
    monkeypatch.setattr(  # the other monitor fetches too; keep it off the network
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )
    snap = _snap(number=9, mergeable="CONFLICTING")           # a fresh conflict -> dispatch
    store._save_fingerprints({9: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [snap])

    def boom_build(cfg):
        raise RuntimeError("diplomat-core failed: build-prompt exit 1")
    monkeypatch.setattr("diplomat_runtime.promptcore.build_prompt", boom_build)

    store._autofix_poll_once()   # must NOT raise (a raise here would kill the worker thread)
    assert store.autofix_poll_error and "diplomat-core failed" in store.autofix_poll_error

    # Recovery: once build-prompt works again, the pill clears on the next clean poll.
    monkeypatch.setattr("diplomat_runtime.promptcore.build_prompt",
                        lambda cfg: f"PROMPT:{cfg.get('kind')}")
    store._autofix_poll_once()
    assert store.autofix_poll_error is None


# MARK: - Mesh coordination (work keys + assignment gate + monitor gating)
#
# The work-key / stand-down fixtures are PARITY fixtures: the Swift twin
# (AutofixMesh, asserted in DiplomatCoreSmoke) must produce byte-identical
# strings for the same inputs — the whole point of the key is that two nodes
# observing the same work agree on it (szpontnet-spec/docs/12).


def test_work_key_reference_convention():
    assert (
        autofix.work_key("review", "https://github.com/acme/app/pull/123", "abc123")
        == "review:github.com/acme/app#123@abc123"
    )
    assert (
        autofix.work_key("review-reply", "https://github.com/a/b/pull/9", "F00")
        == "review-reply:github.com/a/b#9@F00"
    )
    assert (
        autofix.work_key("conflicts", "https://github.com/a/b/pull/9", "F00")
        == "conflicts:github.com/a/b#9@F00"
    )
    # Host is case-normalized; owner/repo/sha case is preserved.
    assert (
        autofix.work_key("review", "https://GitHub.com/Acme/App/pull/5", "AbC")
        == "review:github.com/Acme/App#5@AbC"
    )


def test_work_key_safe_degradation():
    # No sha / not a PR URL / garbage → "" (claim gate skipped, pre-claims behavior).
    assert autofix.work_key("review", "https://github.com/acme/app/pull/123", "") == ""
    assert autofix.work_key("review", "https://github.com/acme/app/issues/5", "x") == ""
    assert autofix.work_key("review", "https://github.com/acme/app", "x") == ""
    assert autofix.work_key("review", "not a url", "x") == ""
    assert autofix.work_key("review", "", "x") == ""


def test_ledger_key_is_the_claim_key_whenever_there_is_one():
    """The mesh's claim and the telemetry ledger's task must name one job
    identically, or the ledger cannot tell that the work a peer claimed is the work
    this machine queued."""
    for kind, url, sha in (
        ("review", "https://github.com/acme/app/pull/123", "abc123"),
        ("review-reply", "https://github.com/a/b/pull/9", "F00"),
        ("conflicts", "https://GitHub.com/Acme/App/pull/5", "AbC"),
    ):
        assert autofix.ledger_key(kind, url, sha) == autofix.work_key(kind, url, sha)


def test_ledger_key_survives_an_unknown_head_sha():
    """Where the claim key degrades to "" the ledger key must not: skipping a claim
    is safe, and skipping the ledger entry would drop dispatched work off every
    figure on the Telemetry screen."""
    assert (
        autofix.ledger_key("review", "https://github.com/acme/app/pull/123", "")
        == "review:github.com/acme/app#123"
    )
    # Nothing to name, though, is still nothing to name.
    assert autofix.ledger_key("review", "https://github.com/acme/app/issues/5", "x") == ""
    assert autofix.ledger_key("review", "https://github.com/acme/app", "x") == ""
    assert autofix.ledger_key("review", "not a url", "x") == ""
    assert autofix.ledger_key("review", "", "x") == ""


def test_parse_work_key_round_trips_the_builder():
    # The executor's ps floor parses back exactly what work_key emits: kind,
    # owner, repo, pr — the sha is intentionally dropped (dedup is per-PR, so a
    # fresh push can't dodge an agent already reviewing the PR).
    for kind, url, sha, want in [
        ("review", "https://github.com/acme/app/pull/123", "abc123",
         ("review", "acme", "app", 123)),
        ("conflicts", "https://github.com/a/b/pull/9", "F00", ("conflicts", "a", "b", 9)),
        ("review-reply", "https://GitHub.com/Acme/App/pull/5", "AbC",
         ("review-reply", "Acme", "App", 5)),
    ]:
        key = autofix.work_key(kind, url, sha)
        assert autofix.parse_work_key(key) == want


def test_parse_work_key_rejects_non_pr_keys():
    # Anything work_key never emits parses to None → the ps floor is skipped and
    # the spawn proceeds (no false suppression on a malformed / empty key).
    for bad in ["", "review", "review:github.com/acme/app", "audit",
                "review:github.com/acme/app#nope@sha", "review:acme/app#1@s",
                # str.isdigit() is True but int() raises: a Unicode superscript, and a
                # decimal run past CPython's 4300-digit int() limit. Both must parse to
                # None, not raise — an escaped ValueError breaks the executor's fail-open
                # ps floor (_pr_agent_running) and tears the dispatching peer's link.
                "review:github.com/acme/app#²@sha",
                "review:github.com/acme/app#" + "1" * 4301 + "@sha"]:
        assert autofix.parse_work_key(bad) is None


# MARK: - Store routing (the monitor routes auto work through the mesh)


def _mesh_store(monkeypatch, store, dispatch=None):
    """Enable the mesh for `store` against a fake node snapshot. `dispatch` is the
    fake ``ctl.dispatch`` outcome — a list of slot-result dicts, an Exception to
    raise, or None to fail the test if the mesh is consulted at all. Returns the
    recorded ``(duty, work_key)`` dispatch calls."""
    from szpontnet import ctl, statefile

    store._mesh_enabled_override = True
    state = {"pid": 1, "tcpPort": 1, "self": {"id": "me-node", "name": "mac"},
             "peers": [{"id": "peer-node", "name": "softoobox"}]}
    monkeypatch.setattr(statefile, "read_state", lambda: state)
    monkeypatch.setattr(statefile, "node_running", lambda s=None: True)
    calls: list[tuple[str, str]] = []

    def fake_dispatch(duty, prompt, target=None, api_key="", work_key="", timeout=60.0):
        calls.append((duty, work_key))
        if dispatch is None:
            raise AssertionError("the mesh must not be consulted for this source")
        if isinstance(dispatch, Exception):
            raise dispatch
        return dispatch

    monkeypatch.setattr(ctl, "dispatch", fake_dispatch)
    return calls


def _spawned(node="mac"):
    return [{"slot": "any", "node": "n", "nodeName": node, "status": "spawned", "reason": ""}]


def _spawned_here():
    """The placement the mesh made back onto the machine that asked — the executor
    id in the result is the one ``_mesh_store``'s snapshot calls ourselves."""
    return [{"slot": "any", "node": "me-node", "nodeName": "mac", "status": "spawned",
             "reason": ""}]


def _suppressed(node="softoobox"):
    return [{"slot": "claim", "node": "p", "nodeName": node, "status": "suppressed",
             "reason": f"work already claimed by {node}"}]


def _mesh_req(number=7, sha="abc123"):
    return ReviewRequest(
        number=number, title="t", url=f"https://github.com/o/r/pull/{number}",
        author="bob", author_association="MEMBER", files=[],
        requested_at="2026-01-02", my_last_review_at=None, head_sha=sha,
    )


def _poll_one_review(store, monkeypatch):
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: [_mesh_req()]
    )
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")


def test_review_request_runs_on_the_mesh(store, monkeypatch):
    """An owed review is routed through the mesh (best-surplus placement), not
    spawned locally, and its attempt is recorded so retries back off."""
    calls = _mesh_store(monkeypatch, store, dispatch=_spawned("mac"))
    local = _spawn_recorder(monkeypatch)
    _poll_one_review(store, monkeypatch)
    assert local == []                                     # ran on the mesh, not here
    assert calls == [("review", "review:github.com/o/r#7@abc123")]
    assert list(store._load_attempts("reviewReqAttempts")) == ["7"]


def test_review_request_originates_without_assignment_standdown(store, monkeypatch):
    """The regression guard for the bug this branch fixes: there is NO duty-
    assignment stand-down anymore. Every machine scans and routes its finds through
    the mesh — a review request is never silently dropped because some other node
    happened to be 'assigned' the duty (the mesh places the run on the best node)."""
    calls = _mesh_store(monkeypatch, store, dispatch=_spawned("softoobox"))
    local = _spawn_recorder(monkeypatch)
    _poll_one_review(store, monkeypatch)
    assert calls == [("review", "review:github.com/o/r#7@abc123")]  # consulted, not stood down
    assert local == []
    assert list(store._load_attempts("reviewReqAttempts")) == ["7"]


def test_review_request_suppressed_when_a_peer_owns_it(store, monkeypatch):
    _mesh_store(monkeypatch, store, dispatch=_suppressed())
    local = _spawn_recorder(monkeypatch)
    _poll_one_review(store, monkeypatch)
    assert local == []                                     # a peer's agent owns the work
    # Recorded so we back off rather than re-poll the node every tick (still watching).
    assert list(store._load_attempts("reviewReqAttempts")) == ["7"]


def test_review_request_falls_back_to_local_when_mesh_unreachable(store, monkeypatch):
    from szpontnet import ctl

    _mesh_store(monkeypatch, store, dispatch=ctl.CtlError("node down"))
    local = _spawn_recorder(monkeypatch)
    _poll_one_review(store, monkeypatch)
    assert len(local) == 1                                 # fail-open: never leave a PR unhandled


def test_my_review_and_conflicts_route_their_own_duties_and_keys(store, monkeypatch):
    calls = _mesh_store(monkeypatch, store, dispatch=_spawned("mac"))
    _spawn_recorder(monkeypatch)
    snap = PRSnapshot(
        number=3, title="t", url="https://github.com/o/r/pull/3", is_draft=False,
        mergeable="CONFLICTING", review_decision="", threads_unresolved=1,
        threads_i_owe=1, head_sha="beef",
    )
    # Two PRs, not one: a second agent on a PR that already has one is refused by the
    # in-flight dedup (which now sees the peer's run too), and that rule is asserted
    # elsewhere. What is under test here is that each monitor routes its OWN duty and
    # derives its OWN work key.
    other = PRSnapshot(
        number=4, title="t", url="https://github.com/o/r/pull/4", is_draft=False,
        mergeable="CONFLICTING", review_decision="", threads_unresolved=1,
        threads_i_owe=1, head_sha="cafe",
    )
    assert store._dispatch_my_review(snap, 1) is True
    assert store._dispatch_conflict_fix(4, other.url, 1, "auto",
                                        head_sha=other.head_sha) is True
    assert calls == [
        ("review", "review-reply:github.com/o/r#3@beef"),
        ("conflicts", "conflicts:github.com/o/r#4@cafe"),
    ]


def test_panel_spawn_never_routes_to_the_mesh(store, monkeypatch):
    """A manual (panel) spawn is the operator's own action: it runs and is tracked
    locally, never routed through the mesh — whatever the mesh would decide."""
    _mesh_store(monkeypatch, store, dispatch=None)  # fails if the mesh is consulted
    local = _spawn_recorder(monkeypatch)
    assert store._dispatch_conflict_fix(4, "https://github.com/o/r/pull/4", 1, "panel") is True
    assert len(local) == 1


# MARK: - a mesh placement that lands back here is an agent on THIS machine


def test_a_mesh_run_placed_here_spends_a_slot(store, monkeypatch):
    """The mesh's best node can be the machine that asked, and that run is on this
    device's cap like any other — the applet only ever put the terminal on someone
    else's screen, not the load.

    Left unbooked, each dispatch of a poll measured the same idle machine: the cap
    compared 0 against 2 every time and held nothing back at all."""
    _mesh_store(monkeypatch, store, dispatch=_spawned_here())
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    local = _spawn_recorder(monkeypatch)
    store.auto_task_limit = 1

    assert store.dispatch_agent(_job(number=1, mesh=True), autofix.SOURCE_AUTO) == "spawned"
    assert local == []                       # the node opened it, not us
    assert store.auto_tasks_shown == 1 and store.free_auto_slots == 0
    assert (
        store.dispatch_agent(_job(number=2, mesh=True), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )


def test_a_mesh_run_placed_on_a_peer_leaves_the_slot_free(store, monkeypatch):
    """The other half of the same rule: a peer's machine runs it, so a slot HERE is
    still free. Counting every mesh placement would idle a device for work it isn't
    doing — which is the whole point of routing it away."""
    _mesh_store(monkeypatch, store, dispatch=_spawned())  # executor id ≠ ours
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.auto_task_limit = 1

    assert store.dispatch_agent(_job(number=1, mesh=True), autofix.SOURCE_AUTO) == "spawned"
    assert store.auto_tasks_shown == 0 and store.free_auto_slots == 1
    assert store.dispatch_agent(_job(number=2, mesh=True), autofix.SOURCE_AUTO) == "spawned"


def test_a_burst_of_owed_reviews_stops_at_the_cap_on_the_mesh(store, monkeypatch):
    """The failure as it was seen: six owed reviews in one poll, the mesh placing
    every one of them back on this machine, five terminals opening behind a cap of
    two. The cap is measured before the mesh is consulted, so the work that has no
    slot never even takes a claim — it waits in the panel's queue."""
    calls = _mesh_store(monkeypatch, store, dispatch=_spawned_here())
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_mesh_req(number=n, sha=f"s{n}") for n in range(1, 7)],
    )

    store._autofix_poll_once()

    assert len(calls) == store.auto_task_limit == 2   # two placed, four never offered
    assert len(store.queued_tasks) == 4
    assert store.free_auto_slots == 0


def test_a_mesh_run_is_judged_by_where_it_actually_runs(store, monkeypatch):
    """A placement that came back to this machine is a process here like any other, so
    its pid decides. A placement on a PEER is not: no probe on this box can see a
    process on that one, and judging it by a local process table — which this applet
    used to do, 120s after dispatch — retires every peer run the moment its grace
    expires, while the agent is still working."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    _mesh_store(monkeypatch, store, dispatch=_spawned_here())
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    _spawn_recorder(monkeypatch)

    assert store.dispatch_agent(_job(number=1, mesh=True), autofix.SOURCE_AUTO) == "spawned"
    (booked,) = agentregistry.load()
    assert booked.placement == A.PLACEMENT_MESH_HERE
    assert store._auto_tasks_running() == 1  # inside the spawn grace, nothing to see yet

    # It landed here, so it is judged by its pid — and an empty process table we DID
    # read is positive evidence that it is over.
    agentregistry.save([A.RunRecord(**{**booked.__dict__, "pid": 4242,
                                       "dispatched_at": time.time() - 300})])
    fake_probes(monkeypatch, processes={4242: A.ProcInfo(tty="pts/1", elapsed=300.0,
                                                         is_agent=True)})
    assert store._auto_tasks_running() == 1
    fake_probes(monkeypatch, processes={})
    assert store._auto_tasks_running() == 0
    # Retiring it is the settle pass's job, not a read's — the panel asks for the cap
    # on every repaint, and a read with consequences retires records off a screenshot.
    store.refresh_auto_task_count()
    assert agentregistry.load() == []
    # ...and what it cost is booked, like any other agent that ran on this machine.
    assert telemetry.load().tasks[0].done_at is not None


def test_a_peer_run_is_never_retired_by_this_machines_process_table(store, monkeypatch):
    """The bug the placement split exists for. `ps` here says nothing whatever about a
    process there, so only the executor's origination claim can end the run."""
    from diplomat_runtime import agentstate as A

    register_run(1, placement=A.PLACEMENT_MESH_PEER, node="brick",
                 work_key="review:1:sha", dispatched_at=time.time() - 3600)

    # An empty local process table, and the claim still held: still running.
    fake_probes(monkeypatch, processes={}, claims={"review:1:sha"})
    assert store._auto_tasks_running() == 0, "a peer's agent spends the peer's budget"
    (row,) = store.running_tasks
    assert row.state == "running" and "brick" in row.reason
    assert store._in_flight("https://github.com/o/r/pull/1")

    # The node we ask is down: unknown, and still not retired.
    fake_probes(monkeypatch, processes={},
                claims=A.Observation.unavailable("the mesh node is not running"))
    (row,) = store.running_tasks
    assert row.state == "unknown"
    assert store._in_flight("https://github.com/o/r/pull/1")

    # The claim is released. Absence is measured from the last sighting — which the
    # assertions above just refreshed — so it is over once the settle window passes.
    from diplomat_runtime import agentregistry
    (held,) = agentregistry.load()
    agentregistry.save([A.RunRecord(**{**held.__dict__,
                                       "claim_seen_at": time.time() - 120})])
    fake_probes(monkeypatch, processes={}, claims=set())
    assert store.running_tasks == []
    assert not store._in_flight("https://github.com/o/r/pull/1")


def test_where_the_mesh_placed_a_run_is_what_prices_it(store, monkeypatch):
    """The ledger keeps mesh work out of this machine's cost figures because a peer's
    quota paid for it. That reasoning is about WHERE it ran, not about the mesh: a
    placement that came back here spent ours."""
    _mesh_store(monkeypatch, store, dispatch=_spawned_here())
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    _spawn_recorder(monkeypatch)
    assert store.dispatch_agent(_job(number=1, mesh=True), autofix.SOURCE_AUTO) == "spawned"

    _mesh_store(monkeypatch, store, dispatch=_spawned())  # this one lands on a peer
    assert store.dispatch_agent(_job(number=2, mesh=True), autofix.SOURCE_AUTO) == "spawned"

    priced = {t.key: t.remote for t in telemetry.load().tasks}
    assert priced == {"review:github.com/o/r#1@sha": False,
                      "review:github.com/o/r#2@sha": True}


# MARK: - live-agent ps fallback (tracking-independent in-flight)


def test_live_pr_numbers_parses_agents_only():
    dump = "\n".join(
        [
            # The spawning shell holds the unexpanded $(cat …), never the prompt.
            "/bin/zsh -i -c cd '/x'; claude \"$(cat '/tmp/p.txt')\"; printf %s $? > '/tmp/d'",
            "claude Review PR #436 in software-mansion/argent. Use the `gh` CLI to fetch it.",
            "claude Take PR #369 in software-mansion/argent. Use the `gh` CLI to"
            " fetch it and check out its branch.",
            "claude Review PR #99 in other-org/other-repo. Use the `gh` CLI to fetch it.",
            "grep PR #123 in software-mansion/argent",
            "claude --dangerously-skip-permissions",
        ]
    )
    assert autofix.live_pr_numbers(dump, "software-mansion", "argent") == {436, 369}
    assert autofix.live_pr_numbers("", "software-mansion", "argent") == set()


def test_in_flight_falls_back_to_live_ps_agents(store, monkeypatch):
    """An applet restart wipes the in-memory in-flight list while its agents run
    on — the ps live-agent scan must still dedup, or the retry backoff re-spawns
    onto a working PR."""
    from diplomat_app.store import Store

    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    snap = _snap(number=9, mergeable="CONFLICTING")
    object.__setattr__(snap, "url", "https://github.com/o/r/pull/9")
    store._save_fingerprints({9: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [snap]
    )
    from diplomat_runtime import agentregistry
    assert agentregistry.load() == []  # nothing remembered locally…
    fake_probes(monkeypatch, live_prs={9})
    store._poll_my_prs([snap])
    assert calls == []  # …yet the agent visible in ps suppressed the dispatch
    # And with no live agent either, the dispatch goes through.
    fake_probes(monkeypatch, live_prs=set())
    store._poll_my_prs([snap])
    assert len(calls) == 1


def test_an_undecodable_ps_dump_leaves_runs_unknown_rather_than_wedging(store,
                                                                          monkeypatch):
    """`ps` renders every process's argv; a single process on the box with a non-UTF-8
    byte in its arguments makes text=True raise UnicodeDecodeError — a ValueError, NOT
    an OSError/SubprocessError. Uncaught it escapes the probe and wedges the autofix
    poll worker every cycle.

    Caught, it becomes an UNAVAILABLE observation, and the difference from the old
    fail-open-to-empty is the whole point: empty would have retired every run on the
    machine at once."""
    from diplomat_runtime import agentregistry
    from diplomat_app import probes

    register_run(512, pid=4242, tty="pts/3", dispatched_at=time.time() - 600)

    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(probes, "gather", REAL_GATHER)  # the `store` fixture stubs it
    monkeypatch.setattr(probes.subprocess, "run", boom)
    probes.reset_cache()

    store.refresh_auto_task_count()  # must not raise

    (row,) = store.running_tasks
    assert row.state == "unknown"
    assert agentregistry.load(), "an unreadable table must not retire anything"


# MARK: - unified dispatch pipeline (buttons and monitors are triggers, not paths)


def test_dispatch_gate_matrix_parity():
    """The behavior matrix of the ONE pipeline both interfaces ride - PARITY with
    the Swift smoke's AgentDispatchGate assertions: any new source asymmetry must
    be added there AND here first, or it's a bug."""
    for src in (autofix.SOURCE_PANEL, autofix.SOURCE_AUTO):
        assert (
            autofix.dispatch_decide(src, True, True, True, True)
            == autofix.VERDICT_BANNED
        )
        assert (
            autofix.dispatch_decide(src, False, True, True, True)
            == autofix.VERDICT_IN_FLIGHT
        )
        assert (
            autofix.dispatch_decide(src, False, False, False, False)
            == autofix.VERDICT_PROCEED
        )
    # The documented trigger asymmetries - and ONLY these:
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, True, False)
        == autofix.VERDICT_STAND_DOWN
    )
    assert (
        autofix.dispatch_decide(autofix.SOURCE_PANEL, False, False, True, False)
        == autofix.VERDICT_PROCEED
    )  # a human's click already decided placement
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, False, True)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert (
        autofix.dispatch_decide(autofix.SOURCE_PANEL, False, False, False, True)
        == autofix.VERDICT_PROCEED
    )  # a human's click is one deliberate agent, not a queue being emptied
    # Capacity outranks mesh: a saturated device must not take the claim for work
    # it is about to refuse to start.
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, True, True)
        == autofix.VERDICT_AT_CAPACITY
    )
    # The rate-limit budget: auto only, below capacity, above mesh.
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, False, False, True)
        == autofix.VERDICT_UNAFFORDABLE
    )
    assert (
        autofix.dispatch_decide(autofix.SOURCE_PANEL, False, False, False, False, True)
        == autofix.VERDICT_PROCEED
    )  # spending your own last of the limit is the operator's call
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, False, True, True)
        == autofix.VERDICT_AT_CAPACITY
    )  # no free bay to spend a budget on — the probe is not worth taking
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, True, False, True)
        == autofix.VERDICT_UNAFFORDABLE
    )
    assert (
        autofix.dispatch_label(autofix.SOURCE_AUTO, "Review · #7", 2)
        == "Auto · Review · #7 · retry 2"
    )
    assert autofix.dispatch_label(autofix.SOURCE_PANEL, "Review · #7") == "Review · #7"
    # A review the operator asked for is dispatched as auto work — it waits for the
    # cap and holds a bay — but the prefix answers who found the work, and that was
    # them. Without this it reads identically to the review-reply monitor's own row.
    assert (autofix.dispatch_label(autofix.SOURCE_AUTO, "Review · #7", requested=True)
            == "Review · #7")
    assert (autofix.dispatch_label(autofix.SOURCE_AUTO, "Review · #7", 2, requested=True)
            == "Review · #7 · retry 2")
    assert autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 1)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 2)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_PANEL, 1)


def _job(number=9, author=None, counter=None, stamp="", mesh=False, action="review"):
    """One dispatchable job. ``mesh`` gives it the work + ledger keys a job needs to
    be routed through the mesh at all (both are minted from the PR's head sha).
    ``action`` is the activity-feed verb, which is also what the queue keys and bands
    a task by."""
    return autofix.AgentJob(
        kind="review",
        audit_action=action,
        label=f"Review · #{number}",
        prompt="PROMPT",
        pr_url=f"https://github.com/o/r/pull/{number}",
        pr_number=number,
        author_login=author,
        duty="review",
        work_key=f"review:github.com/o/r#{number}@sha" if mesh else "",
        ledger_key=f"review:github.com/o/r#{number}@sha" if mesh else "",
        counter=counter,
        attempt_stamp=stamp,
    )


def test_panel_and_auto_dedup_against_each_other(store, monkeypatch):
    """A manual spawn registers exactly like an auto one, so EITHER interface
    refuses while the other's agent is on the PR - the 2026-07-20 class of dupes
    can't cross the interface boundary."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    assert store.dispatch_agent(_job(), autofix.SOURCE_PANEL) == "spawned"
    assert len(calls) == 1
    # The monitor now sees the manual agent as in-flight...
    assert store.dispatch_agent(_job(), autofix.SOURCE_AUTO) == autofix.VERDICT_IN_FLIGHT
    # ...and a second click is refused the same way.
    assert store.dispatch_agent(_job(), autofix.SOURCE_PANEL) == autofix.VERDICT_IN_FLIGHT
    assert len(calls) == 1


def test_banned_author_blocks_both_interfaces(store, monkeypatch):
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: ["evil"])
    monkeypatch.setattr("diplomat_app.bans.is_banned", lambda login, b: login in b)
    for src in (autofix.SOURCE_PANEL, autofix.SOURCE_AUTO):
        assert store.dispatch_agent(_job(author="evil"), src) == autofix.VERDICT_BANNED
    assert calls == []


def test_mesh_routes_only_auto_source(store, monkeypatch):
    """An AUTO job routes through the mesh (a peer may already own it → stand down);
    a PANEL (manual) spawn is the operator's own action and always runs locally,
    never routed — the human already decided placement."""
    dispatch = _mesh_store(monkeypatch, store, dispatch=_suppressed())
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    job = autofix.AgentJob(
        kind="review", audit_action="review", label="Review · #11", prompt="P",
        pr_url="https://github.com/o/r/pull/11", pr_number=11, duty="review",
        work_key="review:github.com/o/r#11@sha",
    )
    assert store.dispatch_agent(job, autofix.SOURCE_AUTO) == autofix.VERDICT_STAND_DOWN
    assert calls == []                                     # a peer owns it → nothing local
    assert dispatch == [("review", "review:github.com/o/r#11@sha")]
    # The click runs locally regardless, and never consults the mesh.
    assert store.dispatch_agent(job, autofix.SOURCE_PANEL) == "spawned"
    assert len(calls) == 1
    assert dispatch == [("review", "review:github.com/o/r#11@sha")]  # no second consult


def test_autofix_poll_does_not_deadlock_on_dispatch(store, monkeypatch):
    """run_autofix_poll_async holds the poll-overlap guard across the whole worker;
    the worker's dispatch_agent takes a SEPARATE lock for the _dispatching_prs guard.
    Sharing ONE non-reentrant lock self-deadlocked the worker the first time a poll
    dispatched a PR-scoped fix (it re-acquired a lock it already held, store.py:667).
    Drive the real wrapper + real dispatch_agent and assert the worker finishes."""
    import threading

    store.pr_autofix_enabled = True
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])

    done = threading.Event()
    result = {}

    def poll_body():
        # What a real poll does on a fixable PR: a PR-scoped dispatch_agent call.
        job = autofix.AgentJob(
            kind="conflicts", audit_action="conflicts", label="Resolve · #42",
            prompt="P", pr_url="https://github.com/o/r/pull/42", pr_number=42,
            counter="conflicts",
        )
        result["verdict"] = store.dispatch_agent(job, autofix.SOURCE_PANEL)
        done.set()

    monkeypatch.setattr(store, "_autofix_poll_once", poll_body)
    store.run_autofix_poll_async()
    assert done.wait(timeout=5.0), \
        "autofix worker deadlocked re-acquiring _autofix_lock (store.py dispatch_agent)"
    assert result["verdict"] == "spawned"


# MARK: - the device's automatic-task cap


def test_running_auto_tasks_combines_the_three_kinds_of_evidence():
    """Live-but-untracked counts (an applet restart loses the book while the agents
    run on); a tracked PANEL agent does not (a click is the operator's own act); a
    tracked AUTO agent counts even before `ps` has caught up with it."""
    run = autofix.running_auto_tasks
    assert run(set(), set(), set()) == 0
    assert run({1, 2}, set(), set()) == 2            # untracked ⇒ counted as automatic
    assert run({1, 2}, set(), {1}) == 1              # …unless it is a known manual one
    assert run(set(), {3}, set()) == 1               # spawned, not yet visible in ps
    assert run({3}, {3}, set()) == 1                 # …and not counted twice once it is
    assert run({1, 2, 3}, {4}, {2}) == 3             # 1, 3 and 4


def test_an_agent_waiting_at_its_prompt_does_not_hold_a_bay():
    """The wedge this subtraction exists for: an agent is spawned into an INTERACTIVE
    session, so finishing its work is not an exit. It sits at its prompt, `ps` keeps
    showing it, and the bay it took is never given back — a machine whose finished
    windows are left open defers automatic work for as long as they stay open (seen
    2026-08-05: two agents idle since the previous evening, both bays of a cap of 2
    held, auto work deferred 12h later).

    Idle is subtracted from the UNION, not from `live_prs`: a tracked auto agent that
    goes quiet is re-added by `| auto_prs` otherwise, and the tracked ones are exactly
    the agents this applet started."""
    run = autofix.running_auto_tasks
    assert run({1, 2}, set(), set(), {1}) == 1        # untracked and idle ⇒ freed
    assert run({1, 2}, set(), set(), {1, 2}) == 0     # both idle ⇒ the cap is empty
    assert run({1}, {1}, set(), {1}) == 0             # tracked and idle ⇒ freed too
    assert run({1, 2}, set(), set(), set()) == 2      # nothing idle ⇒ unchanged
    assert run({1, 2}, set(), set(), {9}) == 2        # an idle PR nobody is running
    assert run({1, 2}, set(), set(), None) == 2       # no evidence ⇒ nothing freed


def test_idle_pr_numbers_reads_the_pane_on_the_agents_own_tty():
    """Which live agents are back at their prompt: the tty joins the `ps` line to the
    tmux pane showing that session, and the CLI's own interrupt hint says whether the
    turn is still running.

    Evidence is required to free a bay, never to hold one — an agent whose pane could
    not be read is absent from the result, so the cap keeps counting it."""
    dump = "\n".join(
        [
            "pts/1    claude Review PR #11 in software-mansion/argent. Use the `gh` CLI.",
            "pts/2    claude Review PR #22 in software-mansion/argent. Use the `gh` CLI.",
            "pts/3    claude Review PR #33 in software-mansion/argent. Use the `gh` CLI.",
            # The spawning shell holds the unexpanded $(cat …), never the prompt.
            "pts/2    /bin/zsh -i -c cd '/x'; claude \"$(cat '/tmp/p.txt')\"",
            "?        grep PR #44 in software-mansion/argent",
        ]
    )
    working = "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
    at_prompt = "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"
    tails = {"pts/1": working, "pts/2": at_prompt}  # pts/3 has no pane at all

    idle = autofix.idle_pr_numbers(dump, tails, "software-mansion", "argent")
    assert idle == {22}, "only the agent whose own pane shows it at the prompt"
    # No panes at all (tmux absent, or the dump failed) frees nothing.
    assert autofix.idle_pr_numbers(dump, {}, "software-mansion", "argent") == set()
    # A pane on a tty whose agent is on some other repo is not this repo's business.
    assert autofix.idle_pr_numbers(dump, tails, "other-org", "other-repo") == set()
    # Only the agents' own panes are worth capturing — the tick that runs this must
    # not pay a `capture-pane` for every pane the developer happens to have open.
    assert autofix.agent_ttys(dump, "software-mansion", "argent") == {"pts/1", "pts/2",
                                                                     "pts/3"}
    # The argv scan reads the very same lines, so one `ps` pass answers both.
    assert autofix.live_pr_numbers(dump, "software-mansion", "argent") == {11, 22, 33}


def test_the_agent_scans_still_read_a_ps_dump_with_no_tty_column():
    """``live_pr_numbers`` predates the tty and is called on macOS through the mesh
    node's own ``ps``; a dump whose first token is the command rather than a tty must
    still yield its PRs, and must not invent a tty that could match a real pane."""
    dump = "claude Review PR #11 in software-mansion/argent. Use the `gh` CLI.\n"
    assert autofix.live_pr_numbers(dump, "software-mansion", "argent") == {11}
    assert autofix.agent_ttys(dump, "software-mansion", "argent") == {"claude"}
    # …and "claude" matches no pane, so nothing is ever read as idle off it.
    assert autofix.idle_pr_numbers(dump, {"pts/1": "at the prompt"},
                                   "software-mansion", "argent") == set()


def test_clamp_auto_task_limit_holds_the_stepper_range():
    assert autofix.DEFAULT_AUTO_TASK_LIMIT == 2
    assert autofix.clamp_auto_task_limit(0) == autofix.MIN_AUTO_TASK_LIMIT == 1
    assert autofix.clamp_auto_task_limit(-4) == 1
    assert autofix.clamp_auto_task_limit(3) == 3
    assert autofix.clamp_auto_task_limit(999) == autofix.MAX_AUTO_TASK_LIMIT == 16


# MARK: - the device's rate-limit budget
#
# PARITY: every number below is asserted again, on the same inputs, by the Swift
# smoke's "the device's rate-limit budget" section — the two front-ends decide
# whether to spend the same account's limit, so a disagreement here is one machine
# gating what the other starts.


def test_budget_defaults_and_confidence_table_parity():
    assert autofix.DEFAULT_BUDGET_CONFIDENCE == 95
    assert autofix.DEFAULT_BUDGET_FLOOR_PCT == 20.0
    # One-sided, NOT the Telemetry screen's two-sided 1.96 on the mean.
    assert autofix.budget_z(95) == 1.6449
    assert autofix.budget_z(95) != 1.96


def test_an_unsupported_confidence_rounds_up_to_the_stricter_neighbour():
    """A table that cannot honour a hand-edited value must hold work back, never
    wave it through on a looser bound than was asked for."""
    assert autofix.clamp_budget_confidence(93) == 95
    assert autofix.clamp_budget_confidence(96) == 99
    assert autofix.clamp_budget_confidence(1) == 50
    assert autofix.clamp_budget_confidence(95) == 95  # a supported level is untouched
    # Above the table it is the strictest level, not a fallback to the default.
    assert autofix.clamp_budget_confidence(100) == 99
    assert autofix.clamp_budget_confidence(999) == 99
    # Every level the clamp can return has a quantile behind it.
    for level in autofix.BUDGET_CONFIDENCE_Z:
        assert autofix.budget_z(level) == autofix.BUDGET_CONFIDENCE_Z[level]


def test_clamp_budget_floor_holds_a_real_share_of_a_window():
    assert autofix.clamp_budget_floor_pct(-1) == 0.0
    assert autofix.clamp_budget_floor_pct(140) == 100.0
    assert autofix.clamp_budget_floor_pct(0) == 0.0  # spend it to the last drop
    assert autofix.clamp_budget_floor_pct(float("nan")) == 20.0


def test_task_cost_bound_is_a_prediction_interval_not_one_on_the_mean():
    """The gate needs what the NEXT task costs, not what the average one costs:
    the distribution is right-skewed, so a bound that converged on the mean would
    wave the expensive tail through every time."""
    z = autofix.budget_z(95)
    # One observation has no spread — the sample sd of n=1 is 0, and reporting a
    # single cheap task as certainty is exactly the trap the minimum exists for.
    assert autofix.task_cost_bound(2.0, 1.0, 1, z=z, min_sample=1) is None
    assert autofix.task_cost_bound(2.0, 1.0, 4, z=z, min_sample=5) is None
    bound = autofix.task_cost_bound(2.0, 1.0, 4, z=z, min_sample=2)
    assert bound == pytest.approx(2.0 + z * math.sqrt(1.25))
    # The √(1 + 1/n) inflation puts it above the plain z·sd, and — unlike the
    # screen's z·sd/√n band — it does NOT collapse onto the mean as n grows.
    assert bound > 2.0 + z
    assert autofix.task_cost_bound(2.0, 1.0, 10_000, z=z, min_sample=2) > 2.0 + z
    assert autofix.task_cost_bound(2.0, float("nan"), 9, z=z, min_sample=2) is None


def _windows(session_left, week_left, session_cost, week_cost):
    """The two rate-limit windows, in the order :func:`autobudget._decide_claude`
    lists them — which is what fixes the tie-break below."""
    return [(autofix.WINDOW_SESSION, session_left, session_cost),
            (autofix.WINDOW_WEEK, week_left, week_cost)]


def test_budget_decide_gates_on_whichever_window_is_tighter():
    def decide(session_left, week_left, session_cost, week_cost, floor=20.0):
        return autofix.budget_decide(
            _windows(session_left, week_left, session_cost, week_cost), floor)

    assert decide(50.0, 50.0, 10.0, 2.0).affordable
    broke = decide(5.0, 50.0, 10.0, 2.0)
    assert not broke.affordable
    assert broke.window == autofix.WINDOW_SESSION
    assert (broke.left, broke.needed, broke.measured) == (5.0, 10.0, True)
    # A full 5-hour window does not buy a spent weekly one.
    week_broke = decide(90.0, 1.0, 10.0, 2.0)
    assert not week_broke.affordable
    assert week_broke.window == autofix.WINDOW_WEEK
    # An exact tie is decided the same way every time, so a log line is stable.
    assert decide(30.0, 30.0, 10.0, 10.0).window == autofix.WINDOW_SESSION


def test_an_unpriced_ledger_falls_back_to_the_floor():
    floored = autofix.budget_decide(_windows(15.0, None, None, None), 20.0)
    assert not floored.affordable
    assert not floored.measured
    assert floored.needed == 20.0
    assert autofix.budget_decide(_windows(25.0, None, None, None), 20.0).affordable
    # Priced against the 5-hour window but not the weekly one: each window answers
    # with what it has, rather than one blanking the other.
    mixed = autofix.budget_decide(_windows(50.0, 10.0, 8.0, None), 20.0)
    assert not mixed.affordable
    assert mixed.window == autofix.WINDOW_WEEK
    assert not mixed.measured


def test_no_quota_reading_is_no_opinion_not_a_refusal():
    """THE fail-open. The usage probe can be switched off (DIPLOMAT_QUOTA_PROBE=0),
    logged out, or offline; a gate that read silence as "no budget" would take the
    machine's automatic work down with the network every time."""
    blind = autofix.budget_decide(_windows(None, None, None, None), 20.0)
    assert blind.affordable
    assert blind.window == ""
    # Even with a floor that nothing could satisfy.
    assert autofix.budget_decide(_windows(None, None, None, None), 100.0).affordable
    # One window readable and the other not still decides on the one that is.
    half = autofix.budget_decide(_windows(None, 5.0, None, None), 20.0)
    assert not half.affordable
    assert half.window == autofix.WINDOW_WEEK
    # An empty list of ceilings is the same silence, not an error.
    assert autofix.budget_decide([], 20.0).affordable


def test_a_window_exactly_at_what_a_task_needs_is_affordable():
    """The boundary is >=, not >: a task that fits precisely is a task that fits,
    and the alternative is a machine that can never spend its last measured slice."""
    assert autofix.budget_decide(_windows(10.0, None, 10.0, None), 20.0).affordable
    assert not autofix.budget_decide(_windows(9.99, None, 10.0, None), 20.0).affordable
    assert autofix.budget_decide(_windows(20.0, None, None, None), 20.0).affordable


def test_the_same_arithmetic_decides_in_dollars():
    """The gate is unit-free: an account billed in money hands it dollars on both
    sides of the comparison and gets the same tightest-ceiling answer. Nothing in
    here may assume a percentage — a $255 balance is not 255% of anything."""
    windows = [(autofix.WINDOW_KEY, 16.85, 0.21),
               (autofix.WINDOW_CREDITS, 17.03, 0.21)]
    rich = autofix.budget_decide(windows, 1.0, autofix.UNIT_USD)
    assert rich.affordable and rich.measured
    assert rich.window == autofix.WINDOW_KEY  # the tighter of the two, and listed first
    assert rich.unit == autofix.UNIT_USD

    # Spent down to less than one task's worth on the key, with credit to spare.
    broke = autofix.budget_decide(
        [(autofix.WINDOW_KEY, 0.10, 0.21), (autofix.WINDOW_CREDITS, 17.03, 0.21)],
        1.0, autofix.UNIT_USD)
    assert not broke.affordable
    assert broke.window == autofix.WINDOW_KEY
    assert (broke.left, broke.needed) == (0.10, 0.21)

    # An uncapped key has no reading of its own; the balance still gates.
    uncapped = autofix.budget_decide(
        [(autofix.WINDOW_KEY, None, 0.21), (autofix.WINDOW_CREDITS, 0.05, 0.21)],
        1.0, autofix.UNIT_USD)
    assert not uncapped.affordable
    assert uncapped.window == autofix.WINDOW_CREDITS


def test_the_dollar_reserve_is_held_to_what_its_knob_can_express():
    """The clamp and the slider share a bound on purpose: a hand-edited value the
    knob could not represent would be silently rewritten the first time it was
    touched."""
    assert autofix.clamp_budget_reserve_usd(-1.0) == 0.0
    assert autofix.clamp_budget_reserve_usd(250.0) == autofix.MAX_BUDGET_RESERVE_USD
    assert autofix.clamp_budget_reserve_usd(float("nan")) == \
        autofix.DEFAULT_BUDGET_RESERVE_USD


def test_auto_task_limit_persists_to_the_shared_config_file(store):
    """It lives in ~/.diplomat/config.json, not QSettings, because the mesh node
    that spawns peer-routed work is a separate Qt-less process reading the same
    cap."""
    from diplomat_runtime import appconfig

    assert store.auto_task_limit == 2  # default, with nothing written
    store.auto_task_limit = 4
    assert appconfig.read()[appconfig.AUTO_TASK_LIMIT] == 4
    assert store.auto_task_limit == 4
    store.auto_task_limit = 0  # clamped on the way in, not just on the way out
    assert appconfig.read()[appconfig.AUTO_TASK_LIMIT] == 1


def test_a_poll_dispatches_only_up_to_the_cap(store, monkeypatch):
    """THE regression: a level-triggered poll sees every unit GitHub currently owes
    and used to dispatch all of them in one pass — five owed reviews, five terminal
    windows, at once. The cap bounds the burst, and the rest are deferred, not
    dropped: they carry no attempt record, so the very next poll offers them again
    as soon as an agent finishes."""
    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)  # the agents stay running
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )

    store._poll_review_requests("o", "r")
    assert len(calls) == store.auto_task_limit == 2

    # A second poll while both are still running adds nothing.
    store._poll_review_requests("o", "r")
    assert len(calls) == 2

    # Both agents finish → the cap frees up → the next poll takes the next two.
    for c in calls:
        with open(c["done"], "w") as fh:
            fh.write("0")
    store._poll_review_requests("o", "r")
    assert len(calls) == 4


def test_a_panel_spawn_does_not_spend_the_automatic_budget(store, monkeypatch):
    """The cap is on AUTOMATIC tasks. A manually-spawned agent is just as visible in
    `ps` as an automatic one, so only the tracked source tells them apart — without
    that subtraction, two clicks would silently stop the monitor."""
    from diplomat_app.store import Store

    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.auto_task_limit = 1

    # A click, then the whole cap of 1 is still there for the monitor — even with
    # the clicked agent live in `ps`, where it is indistinguishable from any other.
    assert store.dispatch_agent(_job(number=1), autofix.SOURCE_PANEL) == "spawned"
    fake_probes(monkeypatch, live_prs={1})
    assert store.dispatch_agent(_job(number=2), autofix.SOURCE_AUTO) == "spawned"
    # …and now the automatic budget is spent.
    assert (
        store.dispatch_agent(_job(number=3), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    # A click is never capped, whatever else is running.
    assert store.dispatch_agent(_job(number=4), autofix.SOURCE_PANEL) == "spawned"
    assert len(calls) == 3


def test_an_untracked_live_agent_counts_against_the_cap(store, monkeypatch):
    """The tracked book dies with the applet while the agents run on, so a restart
    would otherwise hand the monitor a fresh, empty budget on a machine that is
    already saturated. The `ps` floor is what makes the cap survive that."""
    from diplomat_app.store import Store

    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={101, 102})

    assert (
        store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert calls == []
    # One of them exits → room for one more.
    fake_probes(monkeypatch, live_prs={101})
    assert store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO) == "spawned"
    assert len(calls) == 1


def test_a_finished_agent_at_its_prompt_gives_its_bay_back(store, monkeypatch):
    """The wedge, end to end through the store: an agent that finished its work is
    still a live `claude` in `ps` — it was spawned into an interactive session, which
    waits at its prompt instead of exiting — so nothing else here ever retires it. Its
    sentinel does not fire (no exit), and the in-flight TTL cannot help, because the
    `ps` floor re-adds it the moment the record lapses.

    Read as idle, it stops holding a bay while KEEPING its row: the operator has to be
    able to see which window is still open, and a row that vanished while its terminal
    sat there would be the same blindness in the other direction."""
    from diplomat_app.store import Store

    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={101, 102})

    assert store.auto_task_limit == 2
    assert (
        store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert store.free_auto_slots == 0 and calls == []

    # Both windows are left open at the prompt — the state the machine was found in.
    fake_probes(monkeypatch, live_prs={101, 102}, idle_prs={101, 102})
    assert store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO) == "spawned"
    assert len(calls) == 1
    # The bays come back, and the two finished agents are still on the panel saying
    # why their terminals are open — 2 idle rows + 1 working, against a cap of 2.
    rows = {r.pr_number: r.awaiting_input for r in store.running_tasks}
    assert rows == {101: True, 102: True, 9: False}
    assert store.auto_tasks_shown == 1 and store.free_auto_slots == 1


def test_a_finished_agent_still_blocks_a_second_agent_on_its_own_pr(store, monkeypatch):
    """Freeing the bay must not free the PR. That session is still up, still holding
    the whole context of the work, and still one keystroke from continuing it — a
    second agent dispatched onto the same PR would duplicate its review, which is what
    the in-flight dedup exists to stop. The cap asks "is this machine busy", the dedup
    asks "is anyone on this PR", and only the first answer changes when a turn ends."""
    from diplomat_app.store import Store

    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={101}, idle_prs={101})

    assert store.free_auto_slots == 2  # idle ⇒ the machine is free
    assert (
        store.dispatch_agent(_job(number=101), autofix.SOURCE_AUTO)
        == autofix.VERDICT_IN_FLIGHT
    )
    assert calls == []


# MARK: - what ends a record: the agent, not the clock


def test_an_old_record_keeps_its_name_while_its_agent_is_alive(store, monkeypatch):
    """The record is the only place a row's label, kind and age live, so dropping one
    on age alone turns a working agent into an anonymous "untracked" row — while it
    runs on, with hours of work left. Reviews here routinely outlive any TTL, so a
    record is ended by evidence its agent is gone, never by the clock reaching a
    number. There is no TTL any more; this is what replaced it."""
    from diplomat_runtime import agentstate as A

    started = time.time() - 6 * 60 * 60  # six hours in
    register_run(512, pid=4242, tty="pts/3", dispatched_at=started,
                 label="Auto · Review-req · #512", kind="review")
    fake_probes(monkeypatch,
                processes={4242: A.ProcInfo(tty="pts/3", elapsed=6 * 60 * 60,
                                            is_agent=True)})

    store.refresh_auto_task_count()

    (row,) = store.running_tasks
    assert row.tracked and row.pr_number == 512 and row.state == "running"
    assert row.label == "Auto · Review-req · #512" and row.kind == "review"
    assert row.started_at == started  # what the row's age counts from


def test_a_record_ends_when_its_process_is_gone_from_a_table_we_read(store, monkeypatch):
    """The other half. An agent that left without firing its sentinel — a killed
    window, a machine that slept — used to need the TTL to be noticed at all. Its pid
    being absent from a process table we successfully read is positive evidence, and
    needs no clock."""
    register_run(512, pid=4242, tty="pts/3",
                 dispatched_at=time.time() - 6 * 60 * 60, label="Auto · #512")
    fake_probes(monkeypatch, processes={})

    store.refresh_auto_task_count()

    from diplomat_runtime import agentregistry
    assert agentregistry.load() == []
    assert store.running_tasks == [] and store.free_auto_slots == 2


def test_a_record_is_never_ended_by_a_table_we_could_not_read(store, monkeypatch):
    """The distinction the whole resolver is built on, at the store level: an
    unreadable process table is not an empty one. The run holds its bay, keeps its
    name, keeps its PR, and says why."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    register_run(512, pid=4242, tty="pts/3", dispatched_at=time.time() - 600,
                 label="Auto · #512")
    fake_probes(monkeypatch,
                processes=A.Observation.unavailable("could not be read (OSError)"))

    store.refresh_auto_task_count()

    assert [r.run_id for r in agentregistry.load()] != []
    (row,) = store.running_tasks
    assert row.state == "unknown" and "could not be read" in row.reason
    assert store.free_auto_slots == 1  # the bay is held, not handed back
    assert store._in_flight("https://github.com/o/r/pull/512")


def test_a_long_manual_run_never_starts_spending_the_automatic_budget(
    store, monkeypatch
):
    """A click is the operator's own act and spends none of the automatic budget —
    for the whole life of the agent it started, not for its first two hours. The
    exemption lives in the record, which is why the record now outlives a restart:
    lost, its agent reappears as an untracked one, and untracked counts as automatic."""
    from diplomat_runtime import agentstate as A

    register_run(512, source=autofix.SOURCE_PANEL, pid=4242, tty="pts/3",
                 dispatched_at=time.time() - 6 * 60 * 60, label="Review · #512")
    fake_probes(monkeypatch,
                processes={4242: A.ProcInfo(tty="pts/3", elapsed=6 * 60 * 60,
                                            is_agent=True)})

    store.refresh_auto_task_count()

    (row,) = store.running_tasks  # it is on the panel — every run is
    assert row.state == "running" and not row.mesh
    assert store.free_auto_slots == 2  # …but it is not a bay of the automatic cap


def test_at_capacity_is_noted_once_per_episode(store, monkeypatch):
    """One activity line when the machine saturates, not one per deferred PR per
    poll — a 3-minute cadence over a long-running agent would otherwise bury the
    feed under the same sentence."""
    from diplomat_runtime import activity

    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.auto_task_limit = 1
    assert store.dispatch_agent(_job(number=1), autofix.SOURCE_AUTO) == "spawned"
    for n in (2, 3, 4):
        assert (
            store.dispatch_agent(_job(number=n), autofix.SOURCE_AUTO)
            == autofix.VERDICT_AT_CAPACITY
        )
    noted = [e for e in activity.read() if e.action == "at-capacity"]
    assert len(noted) == 1
    assert "cap of 1 automatic task" in noted[0].detail

    # Room again → the next saturation is a new episode and is noted afresh.
    from diplomat_runtime import agentregistry
    agentregistry.save([])
    assert store.dispatch_agent(_job(number=5), autofix.SOURCE_AUTO) == "spawned"
    assert (
        store.dispatch_agent(_job(number=6), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert len([e for e in activity.read() if e.action == "at-capacity"]) == 2


def test_capacity_refusal_never_consults_the_mesh(store, monkeypatch):
    """A claim has gossip side effects and is held by the executor for its agent's
    lifetime. A device that is about to refuse to start the work must not take one
    on the way — every machine scans, so a peer with room finds the same unit."""
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    routed: list = []
    monkeypatch.setattr(
        store, "_route_via_mesh",
        lambda job: (routed.append(job.work_key) or "spawned", False, "brick"),
    )
    store.auto_task_limit = 1

    assert store.dispatch_agent(_job(number=1), autofix.SOURCE_AUTO) == "spawned"
    # A peer ran #1, so it spends the peer's budget, not this machine's. What fills
    # the cap here is an agent nobody booked — one that outlived an applet restart,
    # say — which the legacy prompt scan is still what finds.
    fake_probes(monkeypatch, live_prs={99})
    assert (
        store.dispatch_agent(_job(number=2), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert len(routed) == 1  # no second consult, no second claim


def test_the_cap_spans_both_monitors(store, monkeypatch):
    """It is the DEVICE's cap, not one per monitor. A poll cycle runs the conflict
    reconciler and the review-request monitor back to back; each finds its own queue,
    and between them they must still start no more than the machine allows."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: [_snap(number=n, mergeable="CONFLICTING") for n in (1, 2)],
    )
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=n, requested_at="2026-01-02") for n in (3, 4)],
    )

    store._autofix_poll_once()

    assert len(calls) == store.auto_task_limit == 2
    # Both of them came from the conflict reconciler, which ran first — the review
    # monitor found the machine already full rather than a budget of its own.
    assert all("conflicts" in c["prompt"] for c in calls)


# MARK: - the queue behind the cap


def test_queue_key_names_the_verb_and_the_number():
    """Stable across polls and restarts, which is what lets the operator's drag order
    outlive the list. Not the mesh work key: that one is scoped to a head sha, so a
    push during the wait would read as a different task."""
    assert autofix.queue_key("conflicts", 7) == "conflicts:7"
    # One PR can owe two monitors at once — a conflict AND an unaddressed review.
    assert autofix.queue_key("review-req", 7) != autofix.queue_key("review-reply", 7)
    # And the number is not even always a PR's: issue #7 and PR #7 are different work
    # on different things, so a fix and a review of "7" must be two rows.
    assert autofix.queue_key("issues", 7) != autofix.queue_key("review", 7)


def test_free_slots_never_go_negative():
    """`running` counts agents this device did not necessarily start, and lowering the
    cap leaves running agents running — both would otherwise draw negative bays."""
    assert autofix.free_slots(2, 0) == 2
    assert autofix.free_slots(2, 1) == 1
    assert autofix.free_slots(2, 2) == 0
    assert autofix.free_slots(1, 4) == 0


def test_queue_order_keeps_the_arrangement_and_drops_dead_work():
    order = autofix.queue_order
    assert order(["a", "b", "c"], []) == ["a", "b", "c"]  # never arranged
    assert order(["a", "b", "c"], ["c", "a"]) == ["c", "a", "b"]  # new work lands behind
    # A queue that outlived its evidence would hand "execute now" work GitHub no
    # longer owes.
    assert order(["b"], ["c", "a", "b"]) == ["b"]
    assert order([], ["a"]) == []
    assert order(["a", "a", "b"], ["b", "b"]) == ["b", "a"]  # one task, however offered


def test_a_conflict_fix_sorts_last_whatever_the_arrangement_says():
    """The band outranks the arrangement — "always last" is not a default the operator
    can drag away from, because the next poll would re-band it and take it back."""
    order = autofix.queue_order
    assert order(["conflicts:1", "review-req:2"], []) == ["review-req:2", "conflicts:1"]
    assert (order(["conflicts:1", "review-req:2"], ["conflicts:1", "review-req:2"])
            == ["review-req:2", "conflicts:1"])
    # Within a band the arrangement still decides, and conflict fixes keep their own
    # order relative to each other.
    assert (order(["conflicts:1", "conflicts:2", "review-req:3", "review-req:4"],
                  ["review-req:4", "conflicts:2"])
            == ["review-req:4", "review-req:3", "conflicts:2", "conflicts:1"])
    # A key with no verb (nothing the queue mints, but the saved list is read off
    # disk) bands with the ordinary work rather than sorting somewhere of its own.
    assert autofix.queue_band("a") == 0


def test_requested_work_waits_behind_the_monitors_and_ahead_of_a_conflict_fix():
    """Three bands, in the order the operator would have chosen by hand: what GitHub
    is already owed, then the sweep they asked for when they had time for it, then the
    conflict fix another agent's run may make unnecessary. Sweeping fifty drafts
    otherwise buries every review request behind them for a day.

    Both kinds of ask share the middle band. A Fix-issues sweep is the same promise as
    a whose-PRs one — work the operator started when they had time for it — so a fix
    banded with the conflict fixes would sit behind every one of them for a day."""
    order = autofix.queue_order
    offered = ["conflicts:1", "review:2", "review-req:3", "review-reply:4", "issues:5"]
    assert order(offered, []) == ["review-req:3", "review-reply:4",
                                  "review:2", "issues:5", "conflicts:1"]
    # And the arrangement cannot lift one out of its band, in either direction.
    assert order(offered, ["issues:5", "conflicts:1", "review-req:3"]) == [
        "review-req:3", "review-reply:4", "issues:5", "review:2", "conflicts:1"
    ]
    assert (autofix.queue_band("review:2"), autofix.queue_band("issues:5"),
            autofix.queue_band("conflicts:1")) == (1, 1, 2)
    # Within the requested band the arrangement still decides.
    assert (order(["review:1", "issues:2"], ["issues:2"])
            == ["issues:2", "review:1"])


def test_queue_reorder_can_reach_every_position():
    """Both directions are needed: an insert-before rule alone can never send a task
    to the end, which is the first arrangement anyone reaches for."""
    ro = autofix.queue_reorder
    assert ro(["a", "b", "c", "d"], "a", "c") == ["b", "c", "a", "d"]  # dragged down
    assert ro(["a", "b", "c", "d"], "d", "b") == ["a", "d", "b", "c"]  # dragged up
    assert ro(["a", "b", "c"], "a", "c") == ["b", "c", "a"]  # dropped on the last row
    assert ro(["a", "b", "c"], "b", "b") == ["a", "b", "c"]  # onto itself
    # A drag naming a task that left the queue mid-drag changes nothing.
    assert ro(["a", "b"], "z", "a") == ["a", "b"]
    assert ro(["a", "b"], "a", "z") == ["a", "b"]
    # Nor does one across the band boundary, in either direction: the arrangement it
    # would write down is one `queue_order` undoes on the next poll, so the drag is
    # refused outright instead of landing and springing back.
    both = ["review-req:1", "review-req:2", "conflicts:3"]
    assert ro(both, "conflicts:3", "review-req:1") == both
    assert ro(both, "review-req:1", "conflicts:3") == both
    # Within the conflict band a drag works like any other.
    assert (ro(["conflicts:1", "conflicts:2"], "conflicts:1", "conflicts:2")
            == ["conflicts:2", "conflicts:1"])
    # The requested-review band is a band like the other two: a drag out of it is
    # refused whichever side it is heading for.
    three = ["review-req:1", "review:2", "conflicts:3"]
    assert ro(three, "review:2", "review-req:1") == three
    assert ro(three, "review:2", "conflicts:3") == three


def test_work_over_the_cap_waits_in_the_queue(store, monkeypatch):
    """The cap's refusals are what fill the list: five owed reviews, two started, and
    the other three visible in the panel instead of silently deferred to a later
    poll."""
    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])

    store._autofix_poll_once()

    assert len(calls) == 2
    assert [t.id for t in store.queued_tasks] == [
        autofix.queue_key("review-req", n) for n in (3, 4, 5)
    ]
    # Every slot of the cap is spent, so the panel draws no empty bays beside them.
    assert store.free_auto_slots == 0
    assert store.auto_tasks_shown == 2


def test_a_switched_off_monitor_queues_what_it_finds(store, monkeypatch):
    """The toggles decide who STARTS the work, not whether it is known: a monitor
    switched off keeps polling, lists what it finds, and waits for a click."""
    from diplomat_runtime import activity

    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: [_snap(number=1, mergeable="CONFLICTING")],
    )
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=2, requested_at="2026-01-02")],
    )

    store._autofix_poll_once()

    assert calls == []  # nothing starts by itself
    assert {t.id for t in store.queued_tasks} == {"conflicts:1", "review-req:2"}
    assert all(store.is_paused(t.job.counter) for t in store.queued_tasks)
    assert store.drainable_tasks == []  # the drain may not touch a paused monitor's work

    # …including the next cycle's drain, which is the first one that sees this work
    # in the queue at all. A hold that only lasted until the following poll would be
    # a 3-minute delay dressed as a switch.
    store._autofix_poll_once()
    assert calls == []
    assert {t.id for t in store.queued_tasks} == {"conflicts:1", "review-req:2"}
    # A paused monitor is not a saturated device: the cap's own bays are still open,
    # and the feed says nothing about capacity.
    assert store.free_auto_slots == 2
    assert [e for e in activity.read() if e.action == "at-capacity"] == []


def test_a_conflict_fix_queues_behind_every_review_however_it_was_found(store, monkeypatch):
    """The order the monitors run in is not a priority: the conflict reconciler is
    part of the my-PRs poll, which finishes before the review-request fetch begins, so
    a conflict found this cycle would otherwise sit above every review of it. It is
    the one task another agent's run routinely makes unnecessary, so it goes last."""
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={90, 91})  # cap full
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: [_snap(number=1, mergeable="CONFLICTING"),
                         _snap(number=2, unresolved=3, i_owe=1)],
    )
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=3, requested_at="2026-01-02")],
    )

    store._autofix_poll_once()

    # The reply and the review requested of me first, in the order they were found;
    # the conflict fix last, though its monitor ran before both of them.
    assert [t.id for t in store.queued_tasks] == [
        "review-reply:2", "review-req:3", "conflicts:1",
    ]


def test_the_drain_drops_a_conflict_fix_the_work_ahead_of_it_already_did(store, monkeypatch):
    """The whole reason conflict fixes go last: the agent in front of them is working
    the same branch, and lands its own merge on the way. A queued task carries the
    verdict of the poll that staged it, so by the time a bay frees the conflict it
    names can be gone — and starting it then spends a bay opening a diff that has
    nothing in it, on a branch a second agent is about to touch."""
    calls = _spawn_recorder(monkeypatch, finish=False)  # #7's agent holds the one bay
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )
    store.auto_task_limit = 1
    both = [_snap(number=7, mergeable="CONFLICTING"), _snap(number=8, mergeable="CONFLICTING")]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: both)

    store._autofix_poll_once()
    assert [c["prompt"] for c in calls] == ["PROMPT:conflicts:7"]
    assert [t.id for t in store.queued_tasks] == ["conflicts:8"]
    with open(calls[0]["done"], "w") as fh:
        fh.write("0")  # #7's agent exits → the bay the queue was waiting for

    # #8 came out of conflict while it waited (its author pushed, or the agent on #7
    # merged main into both). The next cycle's fetch says so — the queue is still the
    # old cycle's answer, and the drain reads it first.
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: [_snap(number=7, mergeable="CONFLICTING"), _snap(number=8)],
    )

    store._autofix_poll_once()

    assert [c["prompt"] for c in calls] == ["PROMPT:conflicts:7"]  # nothing new spawned
    assert store.queued_tasks == []  # and the row went with it


def test_the_queue_is_refreshed_even_with_no_room_to_start_anything(store, monkeypatch):
    """The rows of a busy machine are the ones that sit longest — the drain returns on
    its first entry — so the refresh has to run over the whole queue before the
    capacity guard, not over the part a free bay happens to let it reach."""
    store.queued_tasks = [
        autofix.QueuedTask("conflicts:8",
                           _job(number=8, action="conflicts", counter="conflicts"), 1),
        autofix.QueuedTask("conflicts:7",
                           _job(number=7, action="conflicts", counter="conflicts"), 1),
    ]
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 99)  # no bays

    # #8 came out of conflict; #7 has not. (A spawn here would hit the conftest
    # backstop, so "started nothing" is asserted by the test running at all.)
    store._drain_queued_tasks(
        [_snap(number=7, mergeable="CONFLICTING"), _snap(number=8)], closed=set()
    )

    assert [t.id for t in store.queued_tasks] == ["conflicts:7"]


def test_the_drain_drops_every_row_whose_pr_has_left_the_open_state(store, monkeypatch):
    """The check the my-PRs fetch cannot make. It lists what is OPEN, so it answers
    "does #7 still conflict" and never "is #7 still there" — and the verbs it does not
    cover (a review requested of me, a review the operator swept for) would otherwise
    stand for ever on a PR that landed weeks ago. Closed retires all of them, at no
    capacity, over the whole queue rather than the part a free bay lets the drain
    reach."""
    store.queued_tasks = [
        autofix.QueuedTask("review-req:3",
                           _job(number=3, action="review-req",
                                counter="review_requests"), 1),
        autofix.QueuedTask("review-reply:4",
                           _job(number=4, action="review-reply",
                                counter="my_reviews"), 1),
        autofix.QueuedTask("conflicts:7",
                           _job(number=7, action="conflicts", counter="conflicts"), 1),
        autofix.QueuedTask("conflicts:8",
                           _job(number=8, action="conflicts", counter="conflicts"), 1),
    ]
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 99)  # no bays

    # #7 and #8 both still conflict, #4 still owes a reply — and #3, #4 and #7 have
    # left the open state since the poll that queued them.
    store._drain_queued_tasks(
        [_snap(number=4, mergeable="CONFLICTING", i_owe=2),
         _snap(number=7, mergeable="CONFLICTING"),
         _snap(number=8, mergeable="CONFLICTING")],
        closed={3, 4, 7},
    )

    assert [t.id for t in store.queued_tasks] == ["conflicts:8"]


def test_a_pr_missing_from_the_closed_read_is_a_pr_that_is_still_open(store, monkeypatch):
    """The set is the repo's recent closures, capped and newest-first — not an answer
    about the queue. So absence has to read as "open", or one busy afternoon of merges
    would push a waiting PR off the end of the read and empty the panel of it."""
    store.queued_tasks = [
        autofix.QueuedTask("review-req:3",
                           _job(number=3, action="review-req",
                                counter="review_requests"), 1),
    ]
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 99)

    store._drain_queued_tasks([], closed={4, 5, 99})

    assert [t.id for t in store.queued_tasks] == ["review-req:3"]


def test_a_review_request_on_a_landed_pr_is_dropped_before_the_drain_can_spawn_it(
        store, monkeypatch):
    """The drain runs at the TOP of a cycle, before the review-request fetch that would
    have stopped offering a merged PR. So the commit at the end of the cycle is too late
    to be the only thing that retires one: a bay that freed while the row waited would
    already have gone on reviewing a diff nobody will open again."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=n, requested_at="2026-01-02") for n in (3, 4)],
    )
    store.auto_task_limit = 1

    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]
    with open(calls[0]["done"], "w") as fh:
        fh.write("0")  # #3's agent exits → the bay #4 has been waiting for

    # #4 merged in the meantime. GitHub stops requesting a review on a merged PR, so
    # this cycle's request fetch is down to #3 — which the drain has not read yet.
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_closed_prs",
                        lambda *a, **k: {4})
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=3, requested_at="2026-01-02")],
    )
    store._autofix_poll_once()

    assert [c["prompt"] for c in calls] == ["PROMPT:review:3"]  # #4 never opened
    assert store.queued_tasks == []


def test_a_failed_closed_pr_read_holds_the_drain(store, monkeypatch):
    """A read that failed is not an empty answer. Treated as one, every PR in the queue
    reads as open and the drain spends its bays on the strength of the poll that staged
    them — the blindness the re-check exists to end. So it stands the drain down, the
    same way a failed my-PRs fetch does."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])

    def boom(*a, **k):
        raise RuntimeError("gh timed out after 60s")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_closed_prs", boom)
    store.queued_tasks = [
        autofix.QueuedTask("review-req:3",
                           _job(number=3, action="review-req",
                                counter="review_requests"), 1),
    ]

    store._autofix_poll_once()

    assert calls == []  # a free bay, and nothing put in it
    assert [t.id for t in store.queued_tasks] == ["review-req:3"]
    assert "gh timed out" in (store.autofix_poll_error or "")


def test_the_closed_pr_read_asks_for_closed_prs(nothing_closed, monkeypatch):
    """The qualifier IS the feature: `is:open` here, or a search missing `is:closed`
    altogether, hands the drain a set that retires the rows it was built to keep. Every
    other test in the suite has this fetch stubbed out, so this is the only place its
    query and its decoding are seen at all — hence the fixture that stubs it, asked for
    by name to get the real function back."""
    import json

    fetch_closed_prs = nothing_closed
    seen: dict = {}

    def fake_run(args, timeout=60.0):
        seen["args"] = args
        # A search over issues returns non-PR nodes as `{}` — skipped, not counted.
        return json.dumps(
            {"data": {"search": {"nodes": [{"number": 41}, {}, {"number": 42}]}}}
        ).encode()

    monkeypatch.setattr("diplomat_runtime.gh.run", fake_run)

    assert fetch_closed_prs("o", "r") == {41, 42}
    assert "q=repo:o/r is:pr is:closed sort:updated-desc" in seen["args"]


def test_the_drain_still_starts_a_conflict_fix_the_branch_still_needs(store, monkeypatch):
    """The other half: the check is evidence, not a way of never running the work.
    A PR the fetch still calls conflicting is dispatched the moment a bay frees."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )
    store.auto_task_limit = 1
    both = [_snap(number=7, mergeable="CONFLICTING"), _snap(number=8, mergeable="CONFLICTING")]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: both)

    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["conflicts:8"]
    with open(calls[0]["done"], "w") as fh:
        fh.write("0")

    store._autofix_poll_once()

    assert [c["prompt"] for c in calls] == ["PROMPT:conflicts:7", "PROMPT:conflicts:8"]


def test_a_review_request_is_drained_on_evidence_this_fetch_does_not_carry(store, monkeypatch):
    """A review requested of me is owed until I review it — nothing another agent on
    this machine does retires it, and it is not in the my-PRs fetch to check against.
    Unanswerable must read as "still owed": checked against that fetch, every review
    request would look answered and none would ever start."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=n, requested_at="2026-01-02") for n in (3, 4)],
    )
    store.auto_task_limit = 1

    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]
    with open(calls[0]["done"], "w") as fh:
        fh.write("0")

    store._autofix_poll_once()

    assert [c["prompt"] for c in calls] == ["PROMPT:review:3", "PROMPT:review:4"]


def test_one_poll_offering_a_task_twice_queues_the_backoff_aware_one(store, monkeypatch):
    """A PR whose thread count just went up is offered by the edge-trigger (always
    attempt 1) and again by the reconciler that owns its retry ladder. They are one
    task, and the one that should run is the reconciler's — queueing the edge's would
    silently restart the ladder at 5 minutes."""
    import time as _time

    store.review_requests_enabled = False
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={90, 91})  # cap full
    snaps = [_snap(number=5, unresolved=3, i_owe=1)]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: []
    )
    store._save_fingerprints({5: PRFingerprint("MERGEABLE", "", 0)})  # threads went up
    store._save_attempts(
        "myReviewAttempts",
        {"5": ReviewAttempt(autofix.STAMP_UNRESOLVED_REVIEW, _time.time() - 3600, 1)},
    )

    store._autofix_poll_once()

    assert [(t.id, t.attempt) for t in store.queued_tasks] == [("review-reply:5", 2)]


def test_a_switched_off_queue_starts_nothing_at_all(store, monkeypatch):
    """The switch over the queue itself. Neither monitor toggle speaks for a review the
    operator asked for, so without this one nothing stops a fifty-PR sweep emptying into
    agents a bay at a time. Off, every find waits — including one that meets a free bay,
    which would never reach the queue to be held there."""
    from diplomat_runtime import activity

    store.queue_auto_run = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=n, requested_at="2026-01-02") for n in (1, 2)],
    )

    store._autofix_poll_once()

    assert calls == []
    assert [t.id for t in store.queued_tasks] == ["review-req:1", "review-req:2"]
    assert store.drainable_tasks == []
    # A held queue is not a saturated device: both bays are open, and the feed says
    # nothing about capacity — the operator switched this off on purpose.
    assert store.free_auto_slots == 2
    assert [e for e in activity.read() if e.action == "at-capacity"] == []

    # …and it holds across the next cycle too, rather than being a 3-minute delay.
    store._autofix_poll_once()
    assert calls == []

    # "execute now" is the way past it, as it is past every other hold.
    store._execute_queued_task(store.queued_tasks[0])
    assert [c["prompt"] for c in calls] == ["PROMPT:review:1"]

    # Switched back on, the queue starts what it was holding.
    store.queue_auto_run = True
    store._autofix_poll_once()
    assert [c["prompt"] for c in calls] == ["PROMPT:review:1", "PROMPT:review:2"]


def test_a_monitor_switched_back_on_drains_what_it_held(store, monkeypatch):
    """The held work is the same work: turning the toggle back on starts it, without
    waiting for GitHub to offer it again."""
    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests",
        lambda *a, **k: [_req(number=2, requested_at="2026-01-02")],
    )
    store._autofix_poll_once()
    assert calls == [] and len(store.queued_tasks) == 1

    store.review_requests_enabled = True
    store._autofix_poll_once()
    assert len(calls) == 1
    assert store.queued_tasks == []  # started, so no longer waiting


def test_the_drain_runs_the_queue_in_the_operators_order(store, monkeypatch):
    """The whole point of the drag order: the slot that just freed goes to whatever
    the operator put first, not to whichever PR this poll's fetch happens to list
    first. The drain runs at the TOP of the cycle, before the monitors look again."""
    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    assert [c["prompt"] for c in calls] == ["PROMPT:review:1", "PROMPT:review:2"]

    # Send the last of the three waiting tasks to the front, then free one slot.
    store.move_queued_task("review-req:5", "review-req:3")
    assert [t.id for t in store.queued_tasks] == [
        "review-req:5", "review-req:3", "review-req:4",
    ]
    with open(calls[0]["done"], "w") as fh:
        fh.write("0")

    store._autofix_poll_once()
    assert calls[-1]["prompt"] == "PROMPT:review:5"


def test_a_queued_dispatch_records_the_attempt_the_monitor_would_have(store, monkeypatch):
    """The retry ladder hangs off that record. Without it the very next poll after the
    agent exits reads the PR as never attempted, and re-dispatches it every 3 minutes
    with no backoff ever engaging."""
    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    assert len(calls) == 2 and [t.id for t in store.queued_tasks] == ["review-req:3"]

    # Free both slots and let the drain take #3; its agent then finishes without
    # leaving the review, so the PR is owed again on the next poll.
    for c in calls:
        with open(c["done"], "w") as fh:
            fh.write("0")
    store._autofix_poll_once()
    assert calls[-1]["prompt"] == "PROMPT:review:3"
    with open(calls[-1]["done"], "w") as fh:
        fh.write("0")

    record = store._load_attempts("reviewReqAttempts")["3"]
    assert record.requested_at == "2026-01-02"  # the monitor's own stamp, not a second one
    assert record.attempts == 1
    # …so the 5-minute backoff is what holds the retry, rather than nothing at all.
    before = len(calls)
    store._autofix_poll_once()
    assert len(calls) == before


def test_execute_now_starts_a_queued_task_past_the_cap(store, monkeypatch):
    """The operator overrides the cap and nothing else: it stays auto work — same
    label, same counter — and once running it occupies a slot like any other."""
    from diplomat_runtime import activity

    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    handled = store.review_requests_handled
    assert len(calls) == 2 and store.free_auto_slots == 0

    store._execute_queued_task(store.queued_tasks[0])

    assert calls[-1]["prompt"] == "PROMPT:review:3"
    assert store.review_requests_handled == handled + 1  # still auto-handled work
    assert store.error is None
    ran = [e for e in activity.read() if e.action == "queue-run"]
    assert len(ran) == 1 and "ahead of the task cap" in ran[0].detail
    # Three agents up under a cap of two: the override spends a slot like any other
    # automatic agent, so the rest of the queue waits behind it.
    assert store.auto_tasks_shown == 3 and store.free_auto_slots == 0


def test_execute_now_says_why_when_nothing_opens(store, monkeypatch):
    """The row vanished and no terminal opened; a refusal the operator can't see is
    indistinguishable from a silent failure."""
    entry = autofix.QueuedTask(
        id="review:9", job=_job(number=9, counter="my_reviews"), attempt=1
    )
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={9})

    store._execute_queued_task(entry)

    assert store.error is not None and "already on this PR" in store.error
    # …and nothing is left spinning: a refusal that kept its starting row would be a
    # task waiting on a spawn that will never answer.
    assert store.starting_tasks == []


def test_execute_now_answers_the_press_before_the_worker_starts(store, monkeypatch):
    """The dispatch is a worker thread's seconds-long round trip — a `ps` scan, a mesh
    placement, a terminal. If the row only changed when that came back, the press would
    look like it had done nothing (or, worse, deleted the task)."""
    entry = autofix.QueuedTask(
        id="review-req:3", job=_job(number=3, counter="review_requests"), attempt=1
    )
    store.queued_tasks = [entry]
    # The worker is stubbed out entirely, so what is asserted below is the state the
    # CALLING thread — the one that handled the click — left behind.
    monkeypatch.setattr(type(store), "_execute_queued_task", lambda self, e: None)

    store.execute_queued_task_async("review-req:3")

    assert store.queued_tasks == [] and store.starting_tasks == [entry]


def test_a_task_being_started_stays_on_the_panel(store, monkeypatch):
    """Starting one takes seconds, and for all of them it is in neither list. Held in
    neither, "execute now" reads as the click DELETING the row: it goes on the press,
    and an agent appears in its place later, which is exactly what a dropped task
    would look like."""
    store.pr_autofix_enabled = False
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:3", "review-req:4"]

    starting = store.queued_tasks[0]
    store._begin_starting(starting)
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]
    assert [t.id for t in store.starting_tasks] == ["review-req:3"]
    # Nor is it the drain's any more — it is already being started.
    assert [t.id for t in store.drainable_tasks] == ["review-req:4"]

    # The work stays owed until the spawn answers and the attempt is recorded, so the
    # poll re-offers it. Published as queued as well, it would be two rows for one
    # task — the second promising a start that is already under way.
    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]
    assert [t.id for t in store.starting_tasks] == ["review-req:3"]

    # A start that comes to nothing is re-offered, so its key keeps its PLACE in the
    # arrangement rather than coming back at the end of a queue the operator ordered.
    store._end_starting(starting.id)
    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:3", "review-req:4"]


def test_the_drain_skips_a_task_the_operator_started_under_it(store, monkeypatch):
    """The drain waits on a spawn per task, so the list moves while it walks: an
    "execute now" during one of those takes that row off the queue and starts it
    there and then. Reaching it anyway would dispatch one unit of work twice."""
    a = autofix.QueuedTask("review-req:3", _job(number=3, counter="review_requests"), 1)
    b = autofix.QueuedTask("review-req:4", _job(number=4, counter="review_requests"), 1)
    store.queued_tasks = [a, b]
    ran = []

    def fake_run(self, entry, *, forced):
        ran.append(entry.id)
        self._begin_starting(b)  # the click lands while #3 is spawning
        return "spawned"

    monkeypatch.setattr(type(store), "_run_queued_task", fake_run)
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 0)

    store._drain_queued_tasks([], closed=set())  # neither fetch answers a review request

    assert ran == ["review-req:3"]


def test_a_starting_task_holds_the_bay_it_is_about_to_fill(store):
    """Drawn free, the panel would stand a row that is launching next to the empty
    slot it is launching into — one row more than the cap allows."""
    entry = autofix.QueuedTask(
        id="review-req:3", job=_job(number=3, counter="review_requests"), attempt=1
    )
    store.queued_tasks = [entry]
    assert store.auto_task_limit == 2 and store.free_auto_slots == 2

    store._begin_starting(entry)
    assert store.free_auto_slots == 1
    store._end_starting(entry.id)
    assert store.free_auto_slots == 2


def test_a_dispatch_that_raises_leaves_no_row_starting(store, monkeypatch):
    """The band is the one list a poll cannot rebuild — a starting key is left out of
    the published queue on purpose — so the hand-off out of it has to survive a raise.
    The dispatch reads files, scans `ps` and, on a live mesh, talks to a node; one of
    those throwing would otherwise strand a row that never resolves, over work nothing
    re-offers. The same throw before the band existed cost only the poll it was in."""
    entry = autofix.QueuedTask(
        id="review-req:3", job=_job(number=3, counter="review_requests"), attempt=1
    )
    store.queued_tasks = [entry]

    def boom(*_a, **_k):
        raise OSError("ps: cannot fork")

    monkeypatch.setattr(type(store), "dispatch_agent", boom)

    with pytest.raises(OSError):
        store._run_queued_task(entry, forced=False)

    assert store.starting_tasks == []


def test_the_queue_is_rebuilt_from_live_evidence_every_poll(store, monkeypatch):
    """It is a VIEW of what the monitors would re-offer, never a second copy of their
    state — so work that was taken, resolved, or whose author was banned drops out on
    its own."""
    store.pr_autofix_enabled = False
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    owed = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: owed
    )
    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:3", "review-req:4"]

    # #3 gets its review by hand — GitHub stops offering it.
    owed = [r for r in owed if r.number != 3]
    store._autofix_poll_once()
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]


def test_a_failed_cycle_freezes_the_queue_rather_than_emptying_it(store, monkeypatch):
    """"We no longer know what is owed" is not "nothing is owed". A cycle that failed
    part-way also must not drain: while `gh` is down the list can only go stale, and
    a drain firing from it would spawn agents at work answered hours ago."""
    store.pr_autofix_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    queued = list(store.queued_tasks)
    assert [t.id for t in queued] == ["review-req:3"]

    def boom(*a, **k):
        raise RuntimeError("gh: not authenticated")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests", boom)
    store._autofix_poll_once()
    assert store.autofix_poll_error is not None
    assert store.queued_tasks == queued  # a fetch that failed offered nothing, and
    assert len(calls) == 2               # said nothing about what is still owed

    # Both agents finish, so there is room now — but the list is frozen at whatever
    # the last successful cycle saw, and a drain firing from it would spawn an agent
    # at work that may have been answered by hand since.
    for c in calls:
        with open(c["done"], "w") as fh:
            fh.write("0")
    store._autofix_poll_once()
    assert len(calls) == 2
    assert store.queued_tasks == queued


def test_the_arrangement_outlives_the_list_and_the_applet(store, monkeypatch):
    """The keys are persisted, and only the keys: the queue itself is rebuilt from
    GitHub, but the order the operator dragged it into cannot be."""
    from diplomat_app.store import Store

    store.pr_autofix_enabled = False
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4, 5)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    store._autofix_poll_once()
    store.move_queued_task("review-req:5", "review-req:3")

    fresh = Store()  # the applet restarts: no queue, but the arrangement is there
    fresh.me = "alice"
    assert fresh.queued_tasks == []
    assert fresh.queued_task_order[:3] == ["review-req:5", "review-req:3", "review-req:4"]
    fresh.pr_autofix_enabled = False
    fake_probes(monkeypatch, live_prs={1, 2})
    fresh._autofix_poll_once()
    assert [t.id for t in fresh.queued_tasks] == [
        "review-req:5", "review-req:3", "review-req:4",
    ]


def test_a_spawn_failure_stops_the_drain_rather_than_clearing_the_queue(store, monkeypatch):
    """A spawn that fails means terminal automation is broken, not that this one task
    was unlucky — and each entry leaves the list before it is tried, so walking the
    whole queue into the same failure would empty the panel for a reason none of them
    caused."""
    store.pr_autofix_enabled = False
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    reqs = [_req(number=n, requested_at="2026-01-02") for n in (1, 2, 3, 4)]
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs
    )
    calls = _spawn_recorder(monkeypatch, finish=False)
    store._autofix_poll_once()
    assert len(calls) == 2 and len(store.queued_tasks) == 2

    attempted: list = []

    def refuse(prompt, preferred, done_path=None, pid_path=None, prompt_file=None,
               port=None, settings_file=None):
        attempted.append(prompt)
        raise review.SpawnError("no terminal emulator found")

    monkeypatch.setattr(review, "spawn", refuse)
    for c in calls:  # both agents finish → the drain has room for both queued tasks
        with open(c["done"], "w") as fh:
            fh.write("0")
    store._drain_queued_tasks([], closed=set())

    # The first was tried and failed; the second was never touched, so it is still in
    # the panel rather than dropped alongside it.
    assert attempted == ["PROMPT:review:3"]
    assert [t.id for t in store.queued_tasks] == ["review-req:4"]


def test_a_panel_spawn_is_never_queued(store, monkeypatch):
    """A click is one deliberate agent: uncapped, unpaused, and nothing to defer."""
    calls = _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.auto_task_limit = 1
    store.pr_autofix_enabled = False

    assert store.dispatch_agent(_job(number=1), autofix.SOURCE_PANEL) == "spawned"
    assert store.dispatch_agent(_job(number=2), autofix.SOURCE_PANEL) == "spawned"
    store.commit_queue()
    assert store.queued_tasks == []
    assert len(calls) == 2


def test_work_no_monitor_owns_is_not_queued(store, monkeypatch):
    """The queue's contents are what the next poll would re-offer. A job with no
    monitor behind it is re-offered by nothing, so queueing it would make the list the
    only record of it — which is exactly what this list is not."""
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    fake_probes(monkeypatch, live_prs={77, 78})

    assert (
        store.dispatch_agent(_job(number=1), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    store.commit_queue()
    assert store.queued_tasks == []


# MARK: - reading what the agents are doing must not change it


# MARK: - Closing the window of a run that went quiet
#
# The one destructive consequence of a tick, so what must be pinned is mostly what it
# does NOT do: a window is the operator's, and one killed under a live agent takes the
# task's whole context with it.


def _killed(monkeypatch):
    """Record the ttys the reaper closes instead of closing them."""
    from diplomat_runtime import tmuxwatch

    seen: list[str] = []
    monkeypatch.setattr(tmuxwatch, "kill_session_for_tty",
                        lambda tty: seen.append(tty) or True)
    return seen


def _age_the_stillness(seconds):
    """Backdate every run's stillness clock, keeping the digest the last tick actually
    recorded — the screen has not changed, it has merely been that way for longer."""
    import time as _time

    from diplomat_runtime import agentregistry

    agentregistry.save([dataclasses.replace(r, quiet_since=_time.time() - seconds)
                        for r in agentregistry.load()])


def test_a_window_still_at_twenty_minutes_of_stillness_is_closed(store, monkeypatch):
    import time as _time
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    killed = _killed(monkeypatch)
    now = _time.time()
    register_run(700, pid=7000, tty="pts/70", dispatched_at=now - 4000)
    fake_probes(monkeypatch, processes=agent_alive(7000, tty="pts/70", elapsed=4000),
                tails={"pts/70": WORKING})

    # The first tick only records what the screen looks like; stillness is measured
    # from there, so it takes a second one to have lasted any time at all.
    store._settle_agents()
    assert killed == [], "a screen seen once has not been still for anything yet"
    _age_the_stillness(A.QUIET_TIMEOUT + 5)

    store._settle_agents()

    assert killed == ["pts/70"]


def test_the_terminals_own_clock_does_not_keep_a_window_open(store, monkeypatch):
    """The whole reaper, end to end, on a screen shaped the way a real one is. A dump
    carries the multiplexer's status line too, and tmux draws a wall clock in it — so
    on any box whose shells wrap themselves in tmux this pane changed once a minute,
    the stillness clock restarted every time, and no window was ever closed."""
    import time as _time
    from diplomat_runtime import agentstate as A

    wrapped = WORKING + '\n[0] 0:zsh*  "agent" %s 24-sie-26'
    killed = _killed(monkeypatch)
    now = _time.time()
    register_run(703, pid=7003, tty="pts/73", dispatched_at=now - 4000)
    fake_probes(monkeypatch, processes=agent_alive(7003, tty="pts/73", elapsed=4000),
                tails={"pts/73": wrapped % "16:31"})
    store._settle_agents()
    _age_the_stillness(A.QUIET_TIMEOUT + 5)

    # A minute of the clock, and nothing else, has moved.
    fake_probes(monkeypatch, processes=agent_alive(7003, tty="pts/73", elapsed=4060),
                tails={"pts/73": wrapped % "16:32"})
    store._settle_agents()

    assert killed == ["pts/73"]


def test_a_run_that_merely_finished_keeps_its_window(store, monkeypatch):
    """Its agent is alive at its prompt holding the whole task, and the operator may
    still want to read it or type into it. Closing this one is the mistake the
    backstop must not make on the way to closing the wedged one."""
    import time as _time
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    killed = _killed(monkeypatch)
    now = _time.time()
    rec = register_run(701, pid=7001, tty="pts/71", dispatched_at=now - 60)
    fake_probes(monkeypatch, processes=agent_alive(7001, tty="pts/71", elapsed=60),
                tails={"pts/71": AT_PROMPT},
                activity={rec.run_id: ("idle", now - 5)})

    tick = store._agent_tick()

    assert tick.states[rec.run_id].state == A.FINISHED, \
        "the run must actually be finished, or this pins nothing"
    store._settle_agents()
    assert killed == [], "a finished run's window is not the reaper's to close"


def test_a_run_short_of_the_timeout_keeps_its_window(store, monkeypatch):
    import time as _time
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    killed = _killed(monkeypatch)
    now = _time.time()
    register_run(702, pid=7002, tty="pts/72", dispatched_at=now - 4000)
    fake_probes(monkeypatch, processes=agent_alive(7002, tty="pts/72", elapsed=4000),
                tails={"pts/72": WORKING})
    store._settle_agents()
    _age_the_stillness(A.QUIET_TIMEOUT - 120)

    store._settle_agents()

    assert killed == []


# MARK: - …including the window of an agent nobody dispatched
#
# The ordinary end state, not a rare one: a run retired by its runner's turn report
# keeps its window on purpose, its record is forgotten, and its still-live agent is
# re-derived as `untracked:<pr>` on the very next tick. Everything below is about the
# record that re-derivation leaves — which has to be remembered long enough for the
# stillness clock to run, and dropped the moment the agent it describes is gone.


def test_an_untracked_runs_stillness_clock_runs_across_ticks(store, monkeypatch):
    """The backstop measures a screen against the last one seen, so the record has to
    survive between ticks. Thrown away, every tick reads a screen it has no memory of:
    ``quiet_since`` restarts from nothing, ``went_quiet`` is still None two hours in,
    and the row can never reach FINISHED by stillness however long its agent sits
    there."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    fake_probes(monkeypatch, live_prs={337}, idle_prs={337})

    store._settle_agents()

    (first,) = agentregistry.load()
    assert first.run_id == "untracked:337", "the synthesized record must be kept"
    assert first.quiet_since is None, \
        "the tick that makes a record has no screen of its own to compare against yet"

    store._settle_agents()

    (second,) = agentregistry.load()
    assert second.quiet_digest and second.quiet_since is not None, \
        "the second tick is the one that has something to compare, and starts the clock"
    assert A.went_quiet(second, second.quiet_since + A.QUIET_TIMEOUT) is not None


def test_an_untracked_agents_wedged_window_is_closed(store, monkeypatch):
    """The whole point of remembering one: twenty minutes of a screen that has not
    moved, and the session nobody dispatched is closed like any other."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    killed = _killed(monkeypatch)
    fake_probes(monkeypatch, live_prs={337})
    store._settle_agents()
    store._settle_agents()
    _age_the_stillness(A.QUIET_TIMEOUT + 5)

    store._settle_agents()

    assert killed == ["pts/337"]
    assert agentregistry.load() == [], "and the record it was kept for is dropped"


def test_an_untracked_record_does_not_outlive_its_agent(store, monkeypatch):
    """The other end of remembering one. Kept past its agent it would hold that PR
    against a fresh agent, and a bay of the cap, for the life of the applet — and
    nothing would ever drop it, because the scan that made it is all there is."""
    from diplomat_runtime import agentregistry

    url = "https://github.com/o/r/pull/337"
    fake_probes(monkeypatch, live_prs={337})
    store._settle_agents()
    assert [r.run_id for r in agentregistry.load()] == ["untracked:337"]
    assert store._in_flight(url), "a live agent's PR is deduped against"

    fake_probes(monkeypatch, live_prs=set())  # its agent exited
    store._settle_agents()

    assert agentregistry.load() == []
    assert not store._in_flight(url), "and its PR is free again"


def test_a_kept_records_tty_follows_its_prs_sighting_onto_disk(store, monkeypatch):
    """The scan reports one agent per PR, so an operator's second session becomes the
    sighting the moment the first exits. A record left on the gone one has no screen to
    be judged by, so it reads as working and holds its bay for as long as the second
    session lives."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    fake_probes(monkeypatch, live_prs={337})
    store._settle_agents()
    store._settle_agents()
    (before,) = agentregistry.load()
    assert before.tty == "pts/337"
    assert before.quiet_since is not None, "the first window's clock is running"

    fake_probes(monkeypatch, live_prs=A.Observation.present({337: "pts/9"}),
                tails={"pts/9": WORKING})
    store._settle_agents()

    (after,) = agentregistry.load()
    assert after.tty == "pts/9"
    assert after.quiet_since is None, "and the new window starts its own clock"


def test_the_merged_probe_is_never_asked_about_an_untracked_runs_pr(store, monkeypatch):
    """A merged verdict ends a run so it can be priced and its bay handed back —
    neither of which a synthesized run has any use for, and its agent is manifestly
    still in the process table. Asked about, a landed PR whose agent is still sitting in its window
    would retire the record and have the next tick synthesize it straight back, one
    `gh` call and one audit line per tick."""
    from diplomat_app import probes
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    asked = []

    def record_and_answer(prs):
        asked.append(prs)
        return A.Observation.present(set())

    monkeypatch.setattr(probes, "merged_prs", record_and_answer)
    register_run(512, pid=4242, tty="pts/3")
    fake_probes(monkeypatch, processes=agent_alive(4242, tty="pts/3"),
                live_prs={337}, tails={"pts/3": WORKING})
    store._settle_agents()
    assert {r.pr_number for r in agentregistry.load()} == {512, 337}, \
        "both runs must be in the book, or the filter below is asserting nothing"

    store.refresh_merged_statuses()

    assert asked == [{512}]


def test_an_unreadable_scan_does_not_drop_an_untracked_record(store, monkeypatch):
    """The scan is the sole evidence about one, which is exactly why "could not look"
    must not read as "it is gone": every untracked row on the machine would be dropped
    at once, and every one of their PRs handed a second agent."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    fake_probes(monkeypatch, live_prs={337})
    store._settle_agents()

    fake_probes(monkeypatch, live_prs=A.Observation.unavailable("ps failed"))
    store._settle_agents()

    assert [r.run_id for r in agentregistry.load()] == ["untracked:337"]


def test_drawing_the_rows_retires_nothing_and_writes_nothing(store, monkeypatch,
                                                             tmp_path):
    """The panel asks for the rows and the free slots on every repaint. When those
    reads had consequences, a headless render retired records and wrote probe warnings
    into the operator's real activity feed — found by rendering the panel and then
    finding the lines in ~/.diplomat afterwards."""
    from diplomat_runtime import activity, agentregistry

    register_run(512, pid=4242, tty="pts/3", ledger_key="review:512:abc",
                 dispatched_at=time.time() - 600)
    fake_probes(monkeypatch, processes={})  # its process is gone: retirable
    before = len(activity.read(500))

    # Every read the panel performs, several times over.
    for _ in range(5):
        store.running_tasks
        store.free_auto_slots
        store.auto_tasks_shown
        store._in_flight("https://github.com/o/r/pull/512")

    assert agentregistry.load(), "a read retired the run"
    assert len(activity.read(500)) == before, "a read wrote to the activity feed"

    # The settle pass is what is allowed to act on it.
    store.refresh_auto_task_count()
    assert agentregistry.load() == []


def test_a_spawn_during_a_tick_is_not_dropped_by_the_write_back(store, monkeypatch):
    """The tick resolves against the book as it was; writing its own copy back would
    drop a run registered in between — an agent nothing counts, which is a bay the
    machine then spends twice."""
    from diplomat_runtime import agentregistry

    slow = register_run(101, pid=1, tty="pts/1", dispatched_at=time.time())
    fake_probes(monkeypatch, processes={1: __import__(
        "diplomat_runtime.agentstate", fromlist=["x"]).ProcInfo(
            tty="pts/1", elapsed=5.0, is_agent=True)})
    t = store._agent_tick()

    # A second dispatch lands after the tick resolved and before it is written back.
    register_run(202, pid=2, tty="pts/2", dispatched_at=time.time())
    store._persist_run_changes(t)

    assert sorted(r.pr_number for r in agentregistry.load()) == [101, 202]
    assert slow.run_id in {r.run_id for r in agentregistry.load()}


def test_the_poll_settles_the_agents_even_with_the_panel_shut(store, monkeypatch):
    """Diplomat is a TRAY applet: its panel is shut most of the time, and the panel's
    own tick is gated on being visible. If retirement rides only on that tick, a
    finished agent's record is never dropped, its bay never comes back, its PR stays
    deduped and its cost never reaches the ledger — on precisely the machines that
    leave the tray alone. Seen live with three runs and the panel closed.

    So the 3-minute poll settles them, whatever is on screen."""
    from diplomat_runtime import agentregistry

    register_run(512, pid=4242, tty="pts/3", dispatched_at=time.time() - 600)
    fake_probes(monkeypatch, processes={})  # its process is gone
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])

    store._autofix_poll_once()

    assert agentregistry.load() == [], "the poll must retire what has ended"


def test_the_poll_writes_back_what_the_tick_learned(store, monkeypatch):
    """The same gate cost the write-back too: a run's pid and tty are learned by a
    tick, and if only a visible panel ever ticked, they were never persisted — so the
    pane probe kept asking about a tty nobody had recorded."""
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    register_run(512, dispatched_at=time.time())  # no pid, no tty yet
    fake_probes(monkeypatch, live_prs={512})
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])

    store._autofix_poll_once()

    (kept,) = agentregistry.load()
    assert kept.tty == "pts/512", "the tty the scan found must be persisted"
