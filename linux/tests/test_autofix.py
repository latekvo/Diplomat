"""Tests for the PR auto-fix monitor: the pure decision logic (autofix.py) and the
Store orchestration (poll → diff → dispatch → reconcile, with dedup + backoff)."""

from __future__ import annotations

import pytest

from co_maintainer import autofix, review
from co_maintainer.autofix import (
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
    from co_maintainer.store import Store

    st = Store()
    st.me = "alice"  # skip the gh viewer-login shell-out
    # Never run the co-maintainer-core CLI in a unit test: stub the prompt builder.
    monkeypatch.setattr(
        "co_maintainer.promptcore.build_prompt",
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
        "co_maintainer.autofixmonitor.fetch_snapshots",
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
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)

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
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_snapshots", lambda *a, **k: snaps)

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
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs)
    store._poll_review_requests("o", "r")
    assert len(calls) == 1 and "review" in calls[0]["prompt"]
    assert store.review_requests_handled == 1

    # A different SKILL PR → verdict withheld: still dispatched (comments-only).
    calls.clear()
    skill_reqs = [_req(number=8, requested_at="2026-02-02", author_association="MEMBER",
                       files=["foo.skill.md"])]
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_review_requests",
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
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_review_requests", lambda *a, **k: reqs)
    monkeypatch.setattr("co_maintainer.bans.read", lambda: [])
    store._poll_review_requests("o", "r")
    # Dispatched + finished → no longer in-flight → counts as still unaddressed until
    # the reviewer resolves it (the reconciler will retry on the next poll).
    assert store.unaddressed_reviews == 1


def test_poll_error_surfaced_and_recovers(store, monkeypatch):
    store.review_requests_enabled = False

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_snapshots", boom)
    store._autofix_poll_once()
    assert store.autofix_poll_error and "gh exploded" in store.autofix_poll_error

    # Recovery clears it.
    monkeypatch.setattr("co_maintainer.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    store._autofix_poll_once()
    assert store.autofix_poll_error is None
