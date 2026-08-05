"""Tests for the PR auto-fix monitor: the pure decision logic (autofix.py) and the
Store orchestration (poll → diff → dispatch → reconcile, with dedup + backoff)."""

from __future__ import annotations

import pytest

from diplomat_app import autofix, review, telemetry
from diplomat_app.autofix import (
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


@pytest.fixture
def store(monkeypatch):
    from diplomat_app.store import Store

    st = Store()
    st.me = "alice"  # skip the gh viewer-login shell-out
    # Never run the diplomat-core CLI in a unit test: stub the prompt builder.
    monkeypatch.setattr(
        "diplomat_app.promptcore.build_prompt",
        lambda cfg: f"PROMPT:{cfg.get('kind')}:{cfg.get('specificPR')}",
    )
    # The two scans behind the cap would read this MACHINE's real processes and its
    # real tmux panes — neutralize both so tests exercise only the tracked-list dedup
    # (the scan-specific tests override them). Nothing live and nothing idle is the
    # empty machine every other test here assumes.
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: set())
    monkeypatch.setattr(Store, "_idle_pr_agents", lambda self: set())
    return st


def _spawn_recorder(monkeypatch, finish=False):
    """Patch review.spawn to record calls (and optionally create the done sentinel,
    simulating an agent that finished immediately so the in-flight guard clears)."""
    calls = []

    def fake_spawn(prompt, preferred, done_path=None):
        calls.append({"prompt": prompt, "done": done_path})
        if finish and done_path:
            with open(done_path, "w") as fh:
                fh.write("0")
        return "/tmp/prompt.txt"

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

    store._poll_my_prs("o", "r")
    assert len(calls) == 1
    assert "conflicts" in calls[0]["prompt"]  # kind=conflicts
    assert store.autofix_conflicts_handled == 1

    # An immediate second poll must NOT re-dispatch (ReviewReconcile 5-min backoff).
    store._poll_my_prs("o", "r")
    assert len(calls) == 1
    assert store.autofix_conflicts_handled == 1


def test_in_flight_dedup(store, monkeypatch):
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)  # agent still running
    snaps = [_snap(number=9, mergeable="CONFLICTING")]
    store._save_fingerprints({9: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)

    store._poll_my_prs("o", "r")
    store._poll_my_prs("o", "r")  # sentinel still absent → still in flight
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
    monkeypatch.setattr("diplomat_app.promptcore.build_prompt", boom_build)

    store._autofix_poll_once()   # must NOT raise (a raise here would kill the worker thread)
    assert store.autofix_poll_error and "diplomat-core failed" in store.autofix_poll_error

    # Recovery: once build-prompt works again, the pill clears on the next clean poll.
    monkeypatch.setattr("diplomat_app.promptcore.build_prompt",
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
    assert store._dispatch_my_review(snap, 1) is True
    assert store._dispatch_conflict_fix(3, snap.url, 1, "auto", head_sha=snap.head_sha) is True
    assert calls == [
        ("review", "review-reply:github.com/o/r#3@beef"),
        ("conflicts", "conflicts:github.com/o/r#3@beef"),
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


def test_a_mesh_agent_is_counted_until_ps_says_it_is_gone(store, monkeypatch):
    """A mesh-placed agent has no completion sentinel of ours, so ``ps`` is what
    retires it — and ``ps`` cannot see an agent whose terminal is still starting.
    Until it is old enough for that to be a real answer, its own dispatch is the
    evidence; after that, absence means it exited and the slot comes back."""
    _mesh_store(monkeypatch, store, dispatch=_spawned_here())
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    _spawn_recorder(monkeypatch)

    assert store.dispatch_agent(_job(number=1, mesh=True), autofix.SOURCE_AUTO) == "spawned"
    # `ps` is blind here (the store fixture's default) — too young to be judged by it.
    assert store._auto_tasks_running() == 1

    store._autofix_inflight[0]["at"] -= store._MESH_AGENT_GRACE + 1
    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: {1})
    assert store._auto_tasks_running() == 1           # old enough, and still up

    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: set())
    assert store._auto_tasks_running() == 0           # gone from ps → gone
    assert store._autofix_inflight == []
    # ...and what it cost is booked, like any other agent that ran on this machine.
    assert telemetry.load().tasks[0].done_at is not None


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
    on (and the TTL can lapse under a long-running one) — the ps live-agent scan
    must still dedup, or the retry backoff re-spawns onto a working PR."""
    from diplomat_app.store import Store

    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch, finish=False)
    snap = _snap(number=9, mergeable="CONFLICTING")
    object.__setattr__(snap, "url", "https://github.com/o/r/pull/9")
    store._save_fingerprints({9: PRFingerprint("MERGEABLE", "", 0)})
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [snap]
    )
    assert store._autofix_inflight == []  # nothing remembered locally…
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {9})
    store._poll_my_prs("o", "r")
    assert calls == []  # …yet the agent visible in ps suppressed the dispatch
    # And with no live agent either, the dispatch goes through.
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: set())
    store._poll_my_prs("o", "r")
    assert len(calls) == 1


def test_live_pr_agents_fails_open_on_undecodable_ps_output(store, monkeypatch):
    """`ps -eo tty=,args=` renders every process's argv; a single process on the box
    with a non-UTF-8 byte in its arguments makes text=True raise UnicodeDecodeError — a
    ValueError, NOT an OSError/SubprocessError. It must be caught, or it escapes this
    fail-open guard and wedges the autofix poll worker every cycle (the raise precedes
    the cache write, so every subsequent poll re-runs ps and re-raises).

    Both scans over that dump have to survive it, not just the one that predates the
    other: the idle scan runs on the same poll, and an exception from it would wedge
    the worker just as thoroughly."""
    import diplomat_app.store as storemod

    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(storemod.subprocess, "run", boom)
    store._ps_dump_cache = None
    assert store._live_pr_agents() == set()  # fails open to empty, never raises
    store._ps_dump_cache = None
    assert storemod.Store._idle_pr_agents(store) == set()


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
    assert (
        autofix.dispatch_label(autofix.SOURCE_AUTO, "Review · #7", 2)
        == "Auto · Review · #7 · retry 2"
    )
    assert autofix.dispatch_label(autofix.SOURCE_PANEL, "Review · #7") == "Review · #7"
    assert autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 1)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 2)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_PANEL, 1)


def _job(number=9, author=None, counter=None, stamp="", mesh=False):
    """One dispatchable job. ``mesh`` gives it the work + ledger keys a job needs to
    be routed through the mesh at all (both are minted from the PR's head sha)."""
    return autofix.AgentJob(
        kind="review",
        audit_action="review",
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


def test_auto_task_limit_persists_to_the_shared_config_file(store):
    """It lives in ~/.diplomat/config.json, not QSettings, because the mesh node
    that spawns peer-routed work is a separate Qt-less process reading the same
    cap."""
    from diplomat_app import appconfig

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
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {1})
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
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {101, 102})

    assert (
        store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert calls == []
    # One of them exits → room for one more.
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {101})
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
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {101, 102})

    assert store.auto_task_limit == 2
    assert (
        store.dispatch_agent(_job(number=9), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    assert store.free_auto_slots == 0 and calls == []

    # Both windows are left open at the prompt — the state the machine was found in.
    monkeypatch.setattr(Store, "_idle_pr_agents", lambda self: {101, 102})
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
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: {101})
    monkeypatch.setattr(Store, "_idle_pr_agents", lambda self: {101})

    assert store.free_auto_slots == 2  # idle ⇒ the machine is free
    assert (
        store.dispatch_agent(_job(number=101), autofix.SOURCE_AUTO)
        == autofix.VERDICT_IN_FLIGHT
    )
    assert calls == []


def test_at_capacity_is_noted_once_per_episode(store, monkeypatch):
    """One activity line when the machine saturates, not one per deferred PR per
    poll — a 3-minute cadence over a long-running agent would otherwise bury the
    feed under the same sentence."""
    from diplomat_app import activity

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
    store._autofix_inflight.clear()
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
        lambda job: (routed.append(job.work_key) or "spawned", False),
    )
    store.auto_task_limit = 1

    assert store.dispatch_agent(_job(number=1), autofix.SOURCE_AUTO) == "spawned"
    # A peer ran #1, so nothing is tracked here — but the ps floor sees an agent on
    # this machine that nobody booked (one that outlived an applet restart, say),
    # and that is what fills the cap.
    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: {1})
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


def test_queue_key_names_the_monitor_and_the_pr():
    """Stable across polls and restarts, which is what lets the operator's drag order
    outlive the list. Not the mesh work key: that one is scoped to a head sha, so a
    push during the wait would read as a different task."""
    assert autofix.queue_key("conflicts", 7) == "conflicts:7"
    # One PR can owe two monitors at once — a conflict AND an unaddressed review.
    assert autofix.queue_key("review-req", 7) != autofix.queue_key("review-reply", 7)


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
    from diplomat_app import activity

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


def test_one_poll_offering_a_task_twice_queues_the_backoff_aware_one(store, monkeypatch):
    """A PR whose thread count just went up is offered by the edge-trigger (always
    attempt 1) and again by the reconciler that owns its retry ladder. They are one
    task, and the one that should run is the reconciler's — queueing the edge's would
    silently restart the ladder at 5 minutes."""
    import time as _time

    store.review_requests_enabled = False
    _spawn_recorder(monkeypatch, finish=False)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: {90, 91})  # cap full
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
    from diplomat_app import activity

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
    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: {9})

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

    def fake_run(self, entry):
        ran.append(entry.id)
        self._begin_starting(b)  # the click lands while #3 is spawning
        return "spawned"

    monkeypatch.setattr(type(store), "_run_queued_task", fake_run)
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 0)

    store._drain_queued_tasks()

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
        store._run_queued_task(entry)

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
    monkeypatch.setattr(type(fresh), "_live_pr_agents", lambda self: {1, 2})
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

    def refuse(prompt, preferred, done_path=None):
        attempted.append(prompt)
        raise review.SpawnError("no terminal emulator found")

    monkeypatch.setattr(review, "spawn", refuse)
    for c in calls:  # both agents finish → the drain has room for both queued tasks
        with open(c["done"], "w") as fh:
            fh.write("0")
    store._drain_queued_tasks()

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
    monkeypatch.setattr(type(store), "_live_pr_agents", lambda self: {77, 78})

    assert (
        store.dispatch_agent(_job(number=1), autofix.SOURCE_AUTO)
        == autofix.VERDICT_AT_CAPACITY
    )
    store.commit_queue()
    assert store.queued_tasks == []
