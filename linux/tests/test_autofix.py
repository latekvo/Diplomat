"""Tests for the PR auto-fix monitor: the pure decision logic (autofix.py) and the
Store orchestration (poll → diff → dispatch → reconcile, with dedup + backoff)."""

from __future__ import annotations

import pytest

from diplomat_app import autofix, review
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
         files=None, number=7):
    return ReviewRequest(
        number=number, title="t", url=f"https://x/pr/{number}", author="bob",
        author_association=author_association, files=files or [],
        requested_at=requested_at, my_last_review_at=my_last_review_at,
    )


def test_owe_review_rules():
    assert _req(requested_at=None).owe_review is True  # requested, no detail → owed
    assert _req("2026-01-02", None).owe_review is True  # never reviewed
    assert _req("2026-01-02", "2026-01-01").owe_review is True  # request newer
    assert _req("2026-01-01", "2026-01-02").owe_review is False  # already reviewed since


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


def test_poll_noop_when_both_disabled(store, monkeypatch):
    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    calls = _spawn_recorder(monkeypatch)
    called = []
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_snapshots",
        lambda *a, **k: called.append(1) or [],
    )
    store.run_autofix_poll_async()
    # Synchronous drain: the guard prevents overlap, so acquiring it means the
    # worker finished. (run_autofix_poll_async returned before spawning since both
    # toggles are off.)
    assert called == [] and calls == []


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

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", boom)
    store._autofix_poll_once()
    assert store.autofix_poll_error and "gh exploded" in store.autofix_poll_error

    # Recovery clears it.
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    store._autofix_poll_once()
    assert store.autofix_poll_error is None


# MARK: - Mesh coordination (work keys + assignment gate + monitor gating)
#
# The work-key / stand-down fixtures are PARITY fixtures: the Swift twin
# (AutofixMesh, asserted in DiplomatCoreSmoke) must produce byte-identical
# strings for the same inputs — the whole point of the key is that two nodes
# observing the same work agree on it (docs/szpontnet/12).


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


def test_mesh_stand_down():
    assigned_other = {"review": {"assigned": ["bbbb"]}}
    assert autofix.mesh_stand_down(assigned_other, "aaaa", "review") == ["bbbb"]
    # Assigned to us (alone or with others) → originate.
    assert autofix.mesh_stand_down({"review": {"assigned": ["aaaa"]}}, "aaaa", "review") is None
    assert (
        autofix.mesh_stand_down({"review": {"assigned": ["aaaa", "bbbb"]}}, "aaaa", "review")
        is None
    )
    # Nobody assigned / unknown duty → originate (better handled than dropped).
    assert autofix.mesh_stand_down({"review": {"assigned": []}}, "aaaa", "review") is None
    assert autofix.mesh_stand_down({}, "aaaa", "review") is None
    # Empty ids are noise, not assignees.
    assert autofix.mesh_stand_down({"review": {"assigned": [""]}}, "aaaa", "review") is None


# MARK: - Store gating (the monitor consults the mesh before every auto spawn)


def _mesh_store(monkeypatch, store, assignments, claim=None):
    """Enable the mesh for `store` against a fake node snapshot; `claim` is the
    fake ctl claim_work outcome (True/False), or an exception to raise, or None
    to fail the test if the claim gate is consulted. Returns the recorded claim
    keys."""
    from diplomat_app.mesh import ctl, statefile

    store._mesh_enabled_override = True
    state = {
        "pid": 1,
        "tcpPort": 1,
        "self": {"id": "me-node", "name": "mac"},
        "peers": [{"id": "peer-node", "name": "softoobox"}],
        "assignments": assignments,
    }
    monkeypatch.setattr(statefile, "read_state", lambda: state)
    monkeypatch.setattr(statefile, "node_running", lambda s=None: True)
    keys: list[str] = []

    def fake_claim(work_key, timeout=5.0):
        keys.append(work_key)
        if claim is None:
            raise AssertionError("claim gate must not be consulted")
        if isinstance(claim, Exception):
            raise claim
        return {"owned": claim, "owner": None if claim else "peer-node",
                "ownerName": None if claim else "softoobox"}

    monkeypatch.setattr(ctl, "claim_work", fake_claim)
    return keys


def _mesh_req(number=7, sha="abc123"):
    return ReviewRequest(
        number=number, title="t", url=f"https://github.com/o/r/pull/{number}",
        author="bob", author_association="MEMBER", files=[],
        requested_at="2026-01-02", my_last_review_at=None, head_sha=sha,
    )


def test_review_request_stands_down_when_duty_assigned_elsewhere(store, monkeypatch):
    _mesh_store(monkeypatch, store, {"review": {"assigned": ["peer-node"]}})
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: [_mesh_req()]
    )
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    assert calls == []  # softoobox's own monitor originates there — not us
    assert store._mesh_duty_stood_down["review"] is True
    # No attempt recorded: if the assignee dies, the next poll takes over fresh.
    assert store._load_attempts("reviewReqAttempts") == {}


def test_review_request_claims_then_originates_here(store, monkeypatch):
    keys = _mesh_store(
        monkeypatch, store, {"review": {"assigned": ["me-node"]}}, claim=True
    )
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: [_mesh_req()]
    )
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    assert len(calls) == 1  # assigned here → claimed → spawned locally
    assert keys == ["review:github.com/o/r#7@abc123"]


def test_review_request_suppressed_by_peer_claim(store, monkeypatch):
    _mesh_store(monkeypatch, store, {"review": {"assigned": []}}, claim=False)
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: [_mesh_req()]
    )
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    assert calls == []  # a live personal peer owns the lease
    assert store._load_attempts("reviewReqAttempts") == {}  # keeps watching


def test_review_request_fails_open_when_claim_unreachable(store, monkeypatch):
    from diplomat_app.mesh import ctl

    _mesh_store(
        monkeypatch, store, {"review": {"assigned": []}}, claim=ctl.CtlError("down")
    )
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(
        "diplomat_app.autofixmonitor.fetch_review_requests", lambda *a, **k: [_mesh_req()]
    )
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    assert len(calls) == 1  # mesh unavailability must never leave PRs unhandled


def test_my_review_and_conflicts_use_their_own_duties_and_kinds(store, monkeypatch):
    keys = _mesh_store(
        monkeypatch, store,
        {"review": {"assigned": ["me-node"]}, "conflicts": {"assigned": ["me-node"]}},
        claim=True,
    )
    calls = _spawn_recorder(monkeypatch)
    snap = PRSnapshot(
        number=3, title="t", url="https://github.com/o/r/pull/3", is_draft=False,
        mergeable="CONFLICTING", review_decision="", threads_unresolved=1,
        threads_i_owe=1, head_sha="beef",
    )
    assert store._dispatch_my_review(snap, 1) is True
    assert store._dispatch_conflict_fix(3, snap.url, 1, "auto", head_sha=snap.head_sha) is False  # in-flight now
    # Clear in-flight (the spawned fake finished nothing) — drive conflicts alone.
    store._autofix_inflight.clear()
    assert store._dispatch_conflict_fix(3, snap.url, 1, "auto", head_sha=snap.head_sha) is True
    assert keys == [
        "review-reply:github.com/o/r#3@beef",
        "conflicts:github.com/o/r#3@beef",
    ]
    assert len(calls) == 2


def test_conflict_stand_down_only_gates_auto_source(store, monkeypatch):
    _mesh_store(monkeypatch, store, {"conflicts": {"assigned": ["peer-node"]}})
    calls = _spawn_recorder(monkeypatch)
    # Auto: stands down. Panel: a deliberate user action — never mesh-gated.
    assert store._dispatch_conflict_fix(4, "https://github.com/o/r/pull/4", 1, "auto",
                                        head_sha="beef") is False
    assert calls == []
    assert store._dispatch_conflict_fix(4, "https://github.com/o/r/pull/4", 1, "panel") is True
    assert len(calls) == 1
