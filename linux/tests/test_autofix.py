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
    # The ps live-agent fallback would see this MACHINE's real processes —
    # neutralize it so tests exercise only the tracked-list dedup (the
    # fallback-specific tests override this).
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: set())
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
                "review:github.com/acme/app#nope@sha", "review:acme/app#1@s"]:
        assert autofix.parse_work_key(bad) is None


# MARK: - Store routing (the monitor routes auto work through the mesh)


def _mesh_store(monkeypatch, store, dispatch=None):
    """Enable the mesh for `store` against a fake node snapshot. `dispatch` is the
    fake ``ctl.dispatch`` outcome — a list of slot-result dicts, an Exception to
    raise, or None to fail the test if the mesh is consulted at all. Returns the
    recorded ``(duty, work_key)`` dispatch calls."""
    from diplomat_app.mesh import ctl, statefile

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
    from diplomat_app.mesh import ctl

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


# MARK: - unified dispatch pipeline (buttons and monitors are triggers, not paths)


def test_dispatch_gate_matrix_parity():
    """The behavior matrix of the ONE pipeline both interfaces ride - PARITY with
    the Swift smoke's AgentDispatchGate assertions: any new source asymmetry must
    be added there AND here first, or it's a bug."""
    for src in (autofix.SOURCE_PANEL, autofix.SOURCE_AUTO):
        assert autofix.dispatch_decide(src, True, True, True) == autofix.VERDICT_BANNED
        assert autofix.dispatch_decide(src, False, True, True) == autofix.VERDICT_IN_FLIGHT
        assert autofix.dispatch_decide(src, False, False, False) == autofix.VERDICT_PROCEED
    # The documented trigger asymmetries - and ONLY these:
    assert (
        autofix.dispatch_decide(autofix.SOURCE_AUTO, False, False, True)
        == autofix.VERDICT_STAND_DOWN
    )
    assert (
        autofix.dispatch_decide(autofix.SOURCE_PANEL, False, False, True)
        == autofix.VERDICT_PROCEED
    )  # a human's click already decided placement
    assert (
        autofix.dispatch_label(autofix.SOURCE_AUTO, "Review · #7", 2)
        == "Auto · Review · #7 · retry 2"
    )
    assert autofix.dispatch_label(autofix.SOURCE_PANEL, "Review · #7") == "Review · #7"
    assert autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 1)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_AUTO, 2)
    assert not autofix.dispatch_bumps_counter(autofix.SOURCE_PANEL, 1)


def _job(number=9, author=None):
    return autofix.AgentJob(
        kind="review",
        audit_action="review",
        label=f"Review · #{number}",
        prompt="PROMPT",
        pr_url=f"https://github.com/o/r/pull/{number}",
        pr_number=number,
        author_login=author,
        duty="review",
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
