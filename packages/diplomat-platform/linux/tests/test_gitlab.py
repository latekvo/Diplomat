"""GitLab repo roots: every read of one goes through ``glab`` and comes back in the
GitHub shape, so the parsers, keys and dispatch past it are the GitHub path's.

The adapters are pinned by the fixtures shared with ``GitLab.swift``; the rest pins
what a GitLab root changes on the way there: which CLI is asked, with what, as whom,
and which link names the item."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_autofix import fake_probes, store  # noqa: F401 — fixture
from test_comms_store import needs_core

from diplomat_runtime import (
    autofix, configbase, core, gh, gitlab, glab, localrepos, prref, review,
)
from diplomat_runtime.models import API
from diplomat_runtime.prref import LocalRepo, item_key_from_url, parse_pr_ref

pytest.importorskip("PySide6")

FIXTURES = Path(__file__).resolve().parents[3] / "diplomat-core/Tests/fixtures/gitlab"
HOST = "gitlab.example.com"
LAB = LocalRepo("group/sub", "proj", "/src/proj", HOST)
HUB = LocalRepo("o", "r", "/src/r")


def _unasked(usernames: list[str]) -> set[str]:
    raise AssertionError(f"a member page with no more searched for {usernames}")


def _review_requests(env: dict, search) -> dict:
    return gitlab.review_requests(env, "carol", search)


#: The review-requests fixtures are answers to the query asked for reviewer ``carol``.
#: Their member pages hold every member, so no adapter searches.
ADAPTERS = {
    "prs": gitlab.prs, "monitor-prs": gitlab.monitor_prs, "closed-prs": gitlab.closed_prs,
    **{name: lambda env: _review_requests(env, _unasked)
       for name in ("review-requests", "review-requests-nofiles", "review-requests-window")},
    "issues": lambda env: gitlab.issues(env, _unasked), "mr": gitlab.mr,
}
#: Fixtures whose member page has more: ``{answer, search}`` in, where ``search`` is the
#: ``gitlab-member-search`` answer, and ``{asked, result}`` or ``{asked, error}`` out,
#: ``asked`` the username lists the adapter searched for.
PAGED = {"issues-paged": gitlab.issues, "issues-paged-unsure": gitlab.issues,
         "review-requests-paged": _review_requests}
QUERIES = ("viewer", "prs", "monitor-prs", "closed-prs", "review-requests", "issues", "mr")
RELATIONS = "relations: [DIRECT, INHERITED, INVITED_GROUPS, SHARED_INTO_ANCESTORS]"


def _fixture(name: str, which: str) -> dict:
    return json.loads((FIXTURES / f"{name}.{which}.json").read_text())


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_each_adapter_turns_the_gitlab_answer_into_its_github_twin(name):
    assert ADAPTERS[name](_fixture(name, "input")) == _fixture(name, "expected")


@pytest.mark.parametrize("name", sorted(PAGED))
def test_an_author_past_a_full_member_page_is_searched_for(name):
    case = _fixture(name, "input")
    asked: list[list[str]] = []

    def search(usernames: list[str]) -> set[str]:
        asked.append(usernames)
        found: set[str] = set()
        for _, variables in gitlab.member_searches(usernames):
            found |= gitlab.searched_members(case["search"], variables)
        return found

    try:
        got = {"result": PAGED[name](case["answer"], search)}
    except gh.GHError as exc:
        got = {"error": str(exc)}
    assert {"asked": asked, **got} == _fixture(name, "expected")


def test_a_member_search_asks_nine_usernames_a_query():
    usernames = _fixture("member-search", "input")["usernames"]
    assert [{"query": q, "variables": v} for q, v in gitlab.member_searches(usernames)] \
        == _fixture("member-search", "expected")
    assert gitlab.member_searches([]) == []


def test_a_member_search_answer_missing_a_username_is_an_error():
    with pytest.raises(gh.GHError, match="GitLab member search answered nothing for bob"):
        gitlab.searched_members({"data": {"project": {}}}, {"u0": "bob"})


def test_every_fixture_has_an_adapter():
    names = {p.name.split(".")[0] for p in FIXTURES.glob("*.json")}
    assert names == set(ADAPTERS) | set(PAGED) | {"member-search"}


def test_the_queries_ask_what_their_adapters_read():
    """System-note discussions fill the discussions page, a thread's adapted comment is
    its LAST note that is no system note, a request older than the activity window falls
    back on the MR's creation, and no adapter of ``prs`` reads members."""
    for name in ("prs", "monitor-prs"):
        text = core.read_graphql(f"gitlab-{name}")
        assert "discussions(first: 100)" in text, name
        assert "notes(last: 20) { nodes { system author" in text, name
    assert "projectMembers" not in core.read_graphql("gitlab-prs")
    requests = core.read_graphql("gitlab-review-requests")
    assert "notes(last: 100, filter: ONLY_ACTIVITY)" in requests
    assert "diffHeadSha createdAt" in requests
    for name in ("issues", "review-requests"):
        text = core.read_graphql(f"gitlab-{name}")
        assert f"projectMembers(first: 100, {RELATIONS}) {{\n      pageInfo {{ hasNextPage }}" \
            in text, name
    search = core.read_graphql("gitlab-member-search")
    assert f"projectMembers(search: $uN, {RELATIONS}, first: 100)" in search
    assert not any("uN" in line for line in search.splitlines() if line.startswith("#"))


# ---- glab ---------------------------------------------------------------------


class FakeGlab:
    """``glab.run`` answering each shared ``gitlab-*`` query from ``answers`` (a dict,
    or an exception to raise), recording ``(query, -f/-F variables, host)``; a REST
    path from ``rest`` the same way, recorded in ``rest_asked``, and one missing
    there is a 404."""

    def __init__(self, monkeypatch, **answers):
        self.answers = {k.replace("_", "-"): v for k, v in answers.items()}
        self.asked: list[tuple[str, dict, str]] = []
        self.argv: list[list[str]] = []
        self.rest: dict[str, dict | Exception] = {}
        self.rest_asked: list[str] = []
        self._names = {core.read_graphql(f"gitlab-{q}"): q for q in QUERIES}
        monkeypatch.setattr(glab, "run", self)
        monkeypatch.setattr(glab.time, "sleep", lambda s: None)

    def __call__(self, args, timeout=60.0):
        self.argv.append(args)
        if "graphql" not in args:
            self.rest_asked.append(args[-1])
            answer = self.rest.get(args[-1], gh.GHError("glab exited 1: 404 Not found"))
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer).encode()
        pairs = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a in ("-f", "-F"))
        query = pairs.pop("query")
        name = "member-search" if "projectMembers(search:" in query else self._names[query]
        self.asked.append((name, pairs, args[args.index("--hostname") + 1]))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer).encode()

    def names(self) -> list[str]:
        return [a[0] for a in self.asked]


def test_a_query_asks_the_roots_host_for_its_project_path(monkeypatch):
    fake = FakeGlab(monkeypatch, review_requests=_fixture("review-requests", "input"))
    glab.project("gitlab-review-requests", LAB, {"reviewer": "carol"}, {"withFiles": "true"})
    args = fake.argv[0]
    assert args[:4] == ["api", "--hostname", HOST, "graphql"]
    assert fake.asked == [("review-requests", {"path": "group/sub/proj", "reviewer": "carol",
                                               "withFiles": "true"}, HOST)]
    assert args[args.index("withFiles=true") - 1] == "-F"
    assert args[args.index("reviewer=carol") - 1] == "-f"


def test_a_missing_project_is_an_unreadable_root_not_an_empty_one(monkeypatch):
    fake = FakeGlab(monkeypatch, prs={"data": {"project": None}})
    with pytest.raises(gh.GHError, match=f"GitLab project group/sub/proj not found on {HOST}"):
        API.fetch_open_prs(LAB)
    assert fake.names() == ["prs", "prs"], "retried once, like gh.graphql"


def test_a_graphql_error_raises_with_its_message(monkeypatch):
    FakeGlab(monkeypatch, issues={"errors": [{"message": "Field 'x' doesn't exist"}]})
    with pytest.raises(gh.GHError, match="GraphQL: Field 'x' doesn't exist"):
        API.fetch_open_issues(LAB)


def test_a_blip_costs_an_attempt_not_the_read(monkeypatch):
    answers = iter([gh.GHError("glab exited 1: glab: 502 Bad Gateway"),
                    _fixture("issues", "input")])
    FakeGlab(monkeypatch, issues=None)
    real = glab.run

    def flaky(args, timeout=60.0):
        real.answers["issues"] = next(answers)
        return real(args, timeout)

    monkeypatch.setattr(glab, "run", flaky)
    assert [i.number for i in API.fetch_open_issues(LAB)] == [5, 4, 3]


def test_no_signed_in_user_and_no_merge_request_raise():
    with pytest.raises(gh.GHError, match=f"not signed in to {HOST}"):
        gitlab.viewer({"data": {"currentUser": None}}, HOST)
    with pytest.raises(gh.GHError, match="merge request not found"):
        gitlab.mr({"data": {"project": {"mergeRequest": None}}})


# ---- the reads a GitLab root makes ---------------------------------------------


def test_open_merge_requests_and_issues_are_read_through_glab(monkeypatch):
    fake = FakeGlab(monkeypatch, prs=_fixture("prs", "input"),
                    issues=_fixture("issues", "input"))
    prs = API.fetch_open_prs(LAB)
    issues = API.fetch_open_issues(LAB)
    assert fake.names() == ["prs", "issues"]
    assert prs[0].number == 12
    assert prs[0].url == f"https://{HOST}/group/sub/proj/-/merge_requests/12"
    assert item_key_from_url(prs[0].url) == "group/sub/proj#12"
    assert item_key_from_url(issues[0].url) == "group/sub/proj#5"


def test_a_full_member_page_costs_one_search_and_a_failed_one_fails_the_read(monkeypatch):
    paged = _fixture("issues-paged", "input")
    fake = FakeGlab(monkeypatch, issues=paged["answer"], member_search=paged["search"])
    issues = API.fetch_open_issues(LAB)
    assert fake.asked[1] == ("member-search", {"path": "group/sub/proj", "u0": "dave",
                                               "u1": "erin"}, HOST)
    assert fake.names() == ["issues", "member-search"]
    assert [(i.number, i.author_association, i.member_responded) for i in issues] == [
        (9, "MEMBER", False), (8, "NONE", True), (7, "NONE", False)]

    fake.answers["member-search"] = gh.GHError("glab exited 1: glab: 502 Bad Gateway")
    with pytest.raises(gh.GHError, match="502 Bad Gateway"):
        API.fetch_open_issues(LAB)


def test_a_review_requests_author_past_a_full_member_page_is_searched_for(monkeypatch):
    from diplomat_app import autofixmonitor

    paged = _fixture("review-requests-paged", "input")
    fake = FakeGlab(monkeypatch, review_requests=paged["answer"], member_search=paged["search"])
    reqs = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert fake.names() == ["review-requests", "member-search"]
    assert [(r.number, r.author_association) for r in reqs] == [
        (3, "MEMBER"), (2, "NONE"), (1, "MEMBER")]


def test_a_member_search_asks_glab_once_per_nine_usernames(monkeypatch):
    """GitLab refuses a query of over 200 complexity read without a token, and each
    search costs 22."""
    nobody = {"data": {"project": {f"u{i}": {"pageInfo": {"hasNextPage": False}, "nodes": []}
                                   for i in range(9)}}}
    fake = FakeGlab(monkeypatch, member_search=nobody)
    assert glab.members(LAB, [f"user{i}" for i in range(19)]) == set()
    assert [len(a[1]) - 1 for a in fake.asked] == [9, 9, 1]
    assert glab.members(LAB, []) == set() and len(fake.asked) == 3


def test_the_monitor_reads_ask_for_me_by_username(monkeypatch):
    from diplomat_app import autofixmonitor

    fake = FakeGlab(monkeypatch, monitor_prs=_fixture("monitor-prs", "input"),
                    review_requests=_fixture("review-requests-nofiles", "input"),
                    closed_prs=_fixture("closed-prs", "input"))
    snaps = autofixmonitor.fetch_snapshots(LAB, "carol")
    reqs = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert fake.asked[0][1]["author"] == "carol"
    assert fake.asked[1][1] == {"path": "group/sub/proj", "reviewer": "carol",
                                "withFiles": "false"}
    assert [s.number for s in snaps][:2] == [12, 8] and snaps[0].mergeable == "CONFLICTING"
    assert [r.number for r in reqs] == [22, 19]


def test_an_undecided_merge_status_takes_the_answer_of_a_recheck(monkeypatch):
    """GraphQL never makes GitLab recheck a merge request it left unchecked; the REST
    read with ``with_merge_status_recheck`` does, and only undecided ones are asked."""
    from diplomat_app import autofixmonitor

    fake = FakeGlab(monkeypatch, monitor_prs=_fixture("monitor-prs", "input"))
    path = "projects/group%2Fsub%2Fproj/merge_requests/{}?with_merge_status_recheck=true"
    fake.rest = {path.format(8): {"has_conflicts": True, "detailed_merge_status": "conflict"},
                 path.format(6): {"has_conflicts": False, "detailed_merge_status": "mergeable"}}
    snaps = autofixmonitor.fetch_snapshots(LAB, "carol")
    assert fake.rest_asked == [path.format(8), path.format(6)]
    assert {s.number: s.mergeable for s in snaps} == {
        12: "CONFLICTING", 8: "CONFLICTING", 7: "MERGEABLE", 6: "MERGEABLE"}

    fake.rest = {path.format(8): {"has_conflicts": False, "detailed_merge_status": "checking"}}
    snaps = autofixmonitor.fetch_snapshots(LAB, "carol")  # #6's recheck fails
    assert {s.number: s.mergeable for s in snaps}[8] == "UNKNOWN"
    assert {s.number: s.mergeable for s in snaps}[6] == "UNKNOWN"


def test_the_drain_keeps_a_conflict_fix_on_an_undecided_merge_request(lab_only, monkeypatch):
    """An undecided GitHub PR has answered its conflict; an undecided GitLab one may
    just not have been rechecked since its target moved."""
    from dataclasses import replace

    from test_autofix import _job, _snap

    lab_only.queued_tasks = [
        autofix.QueuedTask(key, replace(_job(number=n, action="conflicts", counter="conflicts"),
                                        pr_url=url), 1)
        for key, n, url in (("conflicts:group/sub/proj#6", 6, LAB.link("pull", 6)),
                            ("conflicts:o/r#8", 8, HUB.link("pull", 8)))]
    monkeypatch.setattr(type(lab_only), "_auto_tasks_running", lambda self: 99)  # no bays
    snaps = [replace(_snap(number=6, mergeable="UNKNOWN"), url=LAB.link("pull", 6)),
             _snap(number=8, mergeable="UNKNOWN")]
    lab_only._drain_queued_tasks(snaps, closed=set(), read=[HUB, LAB])
    assert [t.id for t in lab_only.queued_tasks] == ["conflicts:group/sub/proj#6"]


def test_the_commit_keeps_a_conflict_fix_on_an_undecided_merge_request(lab_only, monkeypatch):
    from dataclasses import replace

    from test_autofix import _job, _snap

    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    lab_only.queued_tasks = [
        autofix.QueuedTask(key, replace(_job(number=n, action="conflicts", counter="conflicts"),
                                        pr_url=url), 1)
        for key, n, url in (("conflicts:group/sub/proj#6", 6, LAB.link("pull", 6)),
                            ("conflicts:o/r#8", 8, HUB.link("pull", 8)))]
    snaps = [replace(_snap(number=6, mergeable="UNKNOWN"), url=LAB.link("pull", 6)),
             _snap(number=8, mergeable="UNKNOWN")]
    lab_only._staged_queue = []
    lab_only._reconcile_my_conflicts(snaps, 0.0)
    lab_only.commit_queue()
    assert [t.id for t in lab_only.queued_tasks] == ["conflicts:group/sub/proj#6"]


def test_a_queued_conflict_fix_outlives_a_poll_that_reads_its_merge_request_unchecked(
        lab_only, monkeypatch):
    import copy

    conflicting = _fixture("monitor-prs", "input")
    fake = FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
                    monitor_prs=conflicting,
                    review_requests=_fixture("review-requests-nofiles", "input"),
                    issues={"data": {"project": {"issues": {"nodes": []},
                                                 "projectMembers": {"nodes": []}}}})
    lab_only._autofix_poll_once()
    lab_only._autofix_poll_once()
    assert "conflicts:group/sub/proj#12" in {t.id for t in lab_only.queued_tasks}

    unchecked = copy.deepcopy(conflicting)
    mr = next(m for m in unchecked["data"]["project"]["mergeRequests"]["nodes"]
              if m["iid"] == "12")
    mr.update(conflicts=False, detailedMergeStatus="UNCHECKED")
    fake.answers["monitor-prs"] = unchecked
    fake.rest = {"projects/group%2Fsub%2Fproj/merge_requests/12?with_merge_status_recheck=true":
                 {"has_conflicts": False, "detailed_merge_status": "unchecked"}}
    lab_only._autofix_poll_once()
    assert lab_only.autofix_poll_error is None
    assert "conflicts:group/sub/proj#12" in {t.id for t in lab_only.queued_tasks}


def test_a_request_older_than_the_activity_window_is_answered_inside_it(monkeypatch):
    from diplomat_app import autofixmonitor

    FakeGlab(monkeypatch, review_requests=_fixture("review-requests-window", "input"))
    answered, silent = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert answered.requested_at == "2026-11-06T09:00:00+01:00"
    assert not answered.owe_review
    assert silent.requested_at == "2026-11-02T08:00:00+01:00" and silent.owe_review


def test_the_closed_read_keys_merge_requests_by_the_whole_namespace(nothing_closed,
                                                                    monkeypatch):
    FakeGlab(monkeypatch, closed_prs=_fixture("closed-prs", "input"))
    assert nothing_closed(LAB) == {"group/sub/proj#28", "group/sub/proj#29",
                                   "group/sub/proj#30"}


def test_a_merge_requests_state_and_author_come_from_its_iid(monkeypatch):
    fake = FakeGlab(monkeypatch, mr=_fixture("mr", "input"))
    assert API.fetch_pr_state(7, LAB) == "OPEN"
    assert review.fetch_specific_author(LAB, 7) == "alice"
    assert fake.asked[0][1] == {"path": "group/sub/proj", "iid": "7"}


def test_the_merged_probe_asks_a_gitlab_item_of_its_own_root(monkeypatch):
    from diplomat_app import probes

    merged = {"data": {"project": {"mergeRequest": {"state": "merged", "author": None}}}}
    fake = FakeGlab(monkeypatch, mr=merged)
    monkeypatch.setattr(gh, "run", lambda args, **_: b'{"state":"OPEN"}\n')
    obs = probes.merged_prs({"group/sub/proj#3", "o/r#3"}, [HUB, LAB])
    assert obs.value == {"group/sub/proj#3"}
    assert [(a[1]["path"], a[1]["iid"]) for a in fake.asked] == [("group/sub/proj", "3")]


# ---- who "me" is ------------------------------------------------------------------


def test_me_on_gitlab_is_the_glab_login_read_once_and_only_once_it_is_read(store,  # noqa: F811
                                                                           monkeypatch):
    fake = FakeGlab(monkeypatch, viewer=gh.GHError("glab exited 1: glab: 401 Unauthorized"))
    assert store.me_for(HUB) == store.known_me(HUB) == "alice"
    with pytest.raises(gh.GHError):
        store.me_for(LAB)
    assert store.known_me(LAB) == "", "a failed read is not kept"
    fake.answers["viewer"] = {"data": {"currentUser": {"username": "carol"}}}
    assert store.me_for(LAB) == "carol"
    assert store.me_for(LAB) == store.known_me(LAB) == "carol"
    assert fake.names() == ["viewer"] * 3, "one failed read and its retry, one kept"
    assert {a[2] for a in fake.asked} == {HOST}


@pytest.fixture
def lab_only(store, monkeypatch):  # noqa: F811 — the shared Store fixture
    """One GitLab root, the monitors off so a poll queues what it finds."""
    monkeypatch.setattr(review, "repo_roots", lambda: [LAB])
    monkeypatch.setattr(review, "roots_complete", lambda: True)
    monkeypatch.setattr(review, "mesh_checkout", lambda: None)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    store.community_issues_enabled = False
    store.auto_task_limit = 4
    return store


def test_a_poll_of_a_gitlab_root_queues_its_work_as_the_glab_login(lab_only, monkeypatch):
    fake = FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
                    monitor_prs=_fixture("monitor-prs", "input"),
                    review_requests=_fixture("review-requests-nofiles", "input"),
                    issues={"data": {"project": {"issues": {"nodes": []},
                                                 "projectMembers": {"nodes": []}}}})

    lab_only._autofix_poll_once()
    lab_only._autofix_poll_once()

    ids = sorted(t.id for t in lab_only.queued_tasks)
    assert "conflicts:group/sub/proj#12" in ids
    assert "review-req:group/sub/proj#22" in ids
    assert fake.names().count("viewer") == 1
    assert {a[1].get("author") or a[1].get("reviewer") for a in fake.asked
            if a[0] in ("monitor-prs", "review-requests")} == {"carol"}


def test_without_a_github_login_a_gitlab_root_is_polled_and_a_github_one_held(lab_only,
                                                                              monkeypatch):
    """No GitHub login stands the poll down only with no GitLab root: the GitHub roots
    are then unread like a root whose read failed, and the GitLab ones are read."""
    from diplomat_app import store as store_mod

    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    lab_only.me = ""
    asked_gh: list = []

    def signed_out(args, **_):
        asked_gh.append(args)
        raise gh.GHError("gh exited 1: not logged in")

    monkeypatch.setattr(gh, "run", signed_out)
    monkeypatch.setattr(gh.time, "sleep", lambda s: None)
    fake = FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
                    monitor_prs=_fixture("monitor-prs", "input"),
                    closed_prs=_fixture("closed-prs", "input"),
                    review_requests=_fixture("review-requests-nofiles", "input"),
                    issues={"data": {"project": {"issues": {"nodes": []},
                                                 "projectMembers": {"nodes": []}}}})
    logged: list[str] = []
    monkeypatch.setattr(store_mod.activity, "log", lambda *a: logged.append(" ".join(a)))

    lab_only._autofix_poll_once()

    assert lab_only.autofix_poll_error is None
    assert "conflicts:group/sub/proj#12" in {t.id for t in lab_only.queued_tasks}
    assert lab_only._held_slugs == {"o/r"}
    assert "monitor-prs" in fake.names()
    assert not any("o/r" in " ".join(a) for a in asked_gh), "no read of the GitHub root"
    assert any(store_mod.GITHUB_LOGIN_UNKNOWN in m and "o/r" in m for m in logged)

    monkeypatch.setattr(review, "repo_roots", lambda: [HUB])
    lab_only._autofix_poll_once()
    assert lab_only.autofix_poll_error == store_mod.GITHUB_LOGIN_UNKNOWN


def test_the_goal_pass_waits_for_the_github_login(lab_only, monkeypatch):
    """Without it a review of my own PR would be asked for as someone else's."""
    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    asked: list = []
    monkeypatch.setattr(lab_only, "_request", asked.extend)
    monkeypatch.setattr(lab_only, "_edit_tracked_task", lambda op: None)
    work = [("t1", {"kind": "review", "number": 5, "url": "https://github.com/o/r/pull/5",
                    "author": "alice", "key": "k5"})]
    lab_only.me = ""
    lab_only._request_goal_work(work)
    assert asked == []
    lab_only.me = "alice"
    lab_only._request_goal_work(work)
    assert [w.author for w in asked] == [""], "my own PR, once the login is known"


def test_the_review_wizard_asks_who_i_am_in_a_gitlab_checkout_it_never_read(lab_only,
                                                                           monkeypatch):
    """A merge request of another root than the one on the panel: its author is
    compared with glab's login on that host, read for the poll, not with nothing."""
    from diplomat_app.wizardview import WizardView
    from diplomat_runtime.prtarget import PRTarget
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
             mr={"data": {"project": {"mergeRequest": {"state": "opened",
                                                       "author": {"username": "Carol"}}}}})
    monkeypatch.setattr(lab_only, "start_background", lambda work, name=None: work())
    monkeypatch.setattr("time.sleep", lambda s: None)
    w = WizardView(lab_only)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText(f"https://{HOST}/group/sub/proj/-/merge_requests/12")
    assert lab_only.known_me(LAB) == "carol"
    assert w._specific_author == review.SpecificAuthor.MINE


def test_refresh_lists_a_gitlab_roots_merge_requests(lab_only, monkeypatch):
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
             prs=_fixture("prs", "input"), issues=_fixture("issues", "input"))
    lab_only.refresh()
    assert lab_only.lookup(12).url == f"https://{HOST}/group/sub/proj/-/merge_requests/12"
    assert lab_only.known_me(LAB) == "carol"


def test_a_lookup_off_the_lists_asks_gitlabs_rest_api(lab_only, monkeypatch):
    asked: list[list[str]] = []
    answer: dict = {"iid": 40, "web_url": f"https://{HOST}/group/sub/proj/-/issues/40",
                    "state": "opened", "author": {"username": "zed"}}

    def run(args, timeout=60.0):
        asked.append(args)
        if isinstance(answer.get("raise"), Exception):
            raise answer["raise"]
        return json.dumps(answer).encode()

    monkeypatch.setattr(glab, "run", run)
    assert lab_only._comms_lookup("fix", 40, LAB) == {
        "number": 40, "url": f"https://{HOST}/group/sub/proj/-/issues/40",
        "author": "zed", "state": "OPEN"}
    assert asked[-1] == ["api", "--hostname", HOST, "projects/group%2Fsub%2Fproj/issues/40"]
    lab_only._comms_lookup("review", 40, LAB)
    assert asked[-1][-1] == "projects/group%2Fsub%2Fproj/merge_requests/40"

    answer["raise"] = gh.GHError("glab exited 1: glab: 404 Not found (HTTP 404)")
    assert lab_only._comms_lookup("fix", 41, LAB) is None
    answer["raise"] = gh.GHError("glab exited 1: glab: 404 Project Not Found (HTTP 404)")
    assert "Project Not Found" in lab_only._comms_lookup("fix", 41, LAB)["error"]


# ---- links, roots and keys ------------------------------------------------------------


def test_a_gitlab_link_names_its_host_and_the_shorthand_names_none():
    mr = f"https://{HOST}/group/sub/proj/-/merge_requests/5/diffs"
    assert parse_pr_ref(mr, "group/sub", "proj", host=HOST).is_valid
    assert parse_pr_ref(mr, "group/sub", "proj").repo_mismatch, "github.com's repo"
    assert parse_pr_ref(mr, "o", "r", also=[LAB]).repo == LAB
    assert parse_pr_ref("Group/Sub/Proj!5", "o", "r", also=[LAB]).repo == LAB
    assert parse_pr_ref("group/sub/proj#5", "group/sub", "proj", host=HOST).number == 5
    assert parse_pr_ref("group/sub/proj!5", "o", "r", "issues", also=[LAB]).number is None
    issue = f"https://{HOST}/group/sub/proj/-/issues/5"
    assert not parse_pr_ref(issue, "group/sub", "proj", host=HOST).is_valid, "an issue link"
    assert parse_pr_ref(issue, "group/sub", "proj", "issues", host=HOST).number == 5


def test_a_work_items_link_is_an_issue():
    item = f"https://{HOST}/group/sub/proj/-/work_items/7?foo=1"
    assert parse_pr_ref(item, "group/sub", "proj", "issues", host=HOST).number == 7
    assert not parse_pr_ref(item, "group/sub", "proj", host=HOST).is_valid, "not a PR field's"
    assert item_key_from_url(item) == "group/sub/proj#7"


def test_comms_fixes_a_gitlab_issue_served_as_a_work_item(lab_only, monkeypatch):
    monkeypatch.setattr("diplomat_app.store.Store._comms_banned", lambda self, a: False)
    url = f"https://{HOST}/group/sub/proj/-/work_items/5"
    what, ask = lab_only._comms_fix(5, "group/sub/proj", LAB,
                                    {"number": 5, "url": url, "author": "bob",
                                     "state": "OPEN"}, "carol")
    assert ask is not None, what
    assert (ask.number, ask.url, ask.author) == (5, url, "bob")


@needs_core
def test_comms_asks_gitlab_who_i_am_and_names_it_when_it_cannot_ask(lab_only, monkeypatch):
    """A Comms review of my own merge request is mine though no read has asked glab
    who I am yet, and a lookup glab could not answer says GitLab could not be asked."""
    from test_comms_store import _ask, _events, _tick

    lab_only.has_loaded = True
    lab_only.comms_enabled = True
    fake = FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}})
    mrs = "projects/group%2Fsub%2Fproj/merge_requests/{}".format
    fake.rest = {mrs(12): {"iid": 12, "web_url": LAB.link("pull", 12), "state": "opened",
                           "author": {"username": "carol"}},
                 mrs(13): gh.GHError("glab exited 1: glab: 502 Bad Gateway")}
    assert lab_only.known_me(LAB) == ""
    _tick(lab_only, monkeypatch, _ask(1, "review", 12), _ask(2, "review", 13))
    ask = lab_only.requested_work[0]
    assert (ask.number, ask.author, ask.config["specificAuthor"]) == (12, "", "mine")
    assert [e["outcome"] for e in _events() if e["kind"] == "action"][-1] == (
        "couldn't ask GitLab about PR #13 (glab exited 1: glab: 502 Bad Gateway)")


def test_a_roots_links_are_its_hosts():
    assert LAB.link("pull", 5) == f"https://{HOST}/group/sub/proj/-/merge_requests/5"
    assert LAB.link("issues", 5) == f"https://{HOST}/group/sub/proj/-/issues/5"
    assert HUB.link("pull", 5) == "https://github.com/o/r/pull/5"
    assert LocalRepo("a", "b", "", "GitLab.com").host == "gitlab.com"


def test_a_listed_gitlab_checkout_carries_its_host_into_the_prompt():
    assert configbase.listed_payload([HUB, LAB], LAB) == {
        "localRepos": [{"owner": "o", "repo": "r", "path": "/src/r"},
                       {"owner": "group/sub", "repo": "proj", "path": "/src/proj",
                        "host": HOST}],
        "home": {"owner": "group/sub", "repo": "proj", "path": "/src/proj", "host": HOST}}


def test_a_live_agent_on_a_nested_namespace_is_seen_by_its_prompt():
    text = "claude Review PR #337 in group/sub/proj. Use the `gh` CLI to fetch it."
    assert list(autofix.prompt_items(text, {"Group/Sub/Proj"})) == ["group/sub/proj#337"]
    assert list(autofix.prompt_items(text, {"group/sub", "group/sub/proj"})) == [
        "group/sub/proj#337"], "the longest slug the path starts with"
    assert list(autofix.prompt_items(text, {"group/sub"})) == ["group/sub#337"]
    assert list(autofix.prompt_items(text, {"sub/proj"})) == []


def test_an_untracked_agent_on_a_merge_request_is_booked_once_under_its_link():
    """Its record's link is read back into the scan's item key, or the next tick would
    find the agent unbooked again and add a second record."""
    from diplomat_runtime import agentstate as A

    live = A.Observation.present({"group/sub/proj#337": "pts/7"})
    evidence = A.Evidence(processes=A.Observation.present({}),
                          sentinels=A.Observation.present(set()),
                          tails=A.Observation.present({}),
                          claims=A.Observation.present(set()),
                          merged_prs=A.Observation.present(set()), live_agents=live,
                          activity=A.Observation.present({}),
                          sessions=A.Observation.present({}))
    first = A.tick([], evidence, 1000.0, 4, roots=(HUB, LAB))
    second = A.tick(first.records, evidence, 1008.0, 4, roots=(HUB, LAB))
    (run,) = second.records
    assert run.pr_url == f"https://{HOST}/group/sub/proj/-/merge_requests/337"
    assert run.item == "group/sub/proj#337"
    (hub,) = A.synthesize_untracked([], A.Observation.present({"o/r#5": ""}), 0.0,
                                    (HUB, LAB))
    assert hub.pr_url == "https://github.com/o/r/pull/5"


def test_a_nested_namespace_is_labelled_by_its_last_segment():
    assert prref.item_label("group/sub/proj#5", 2) == "proj#5"


def test_a_merge_requests_work_key_keeps_the_whole_namespace():
    url = f"https://{HOST}/group/sub/proj/-/merge_requests/12"
    assert autofix.work_key("conflicts", url, "abc") == f"conflicts:{HOST}/group/sub/proj#12@abc"
    assert autofix.ledger_key("conflicts", url, "") == f"conflicts:{HOST}/group/sub/proj#12"
    assert autofix.work_key("conflicts", f"https://{HOST}/proj/-/merge_requests/1", "a") == ""
    assert autofix.work_key("conflicts", f"https://{HOST}/g/p/-/issues/1", "a") == ""


GLAB_CONFIG = """\
# What protocol to use when performing git operations.
git_protocol: ssh
hosts:
    gitlab.com:
        # What protocol to use to access the api endpoint.
        api_protocol: https
        token: x
    Code.Example.org:8443:
        api_host: code.example.org
        user: alice
    other.example.net:
        user: bob
        aliases:
            co: mr checkout
editor:
"""


def test_the_hosts_glab_is_configured_for_count_as_gitlab(tmp_path, monkeypatch):
    assert localrepos.config_hosts(GLAB_CONFIG) == {
        "gitlab.com", "code.example.org", "other.example.net"}
    assert localrepos.config_hosts("hosts:\n") == set()
    (tmp_path / "glab" / "config.yml").write_text(GLAB_CONFIG)
    hosts = localrepos.gitlab_hosts()
    assert localrepos.is_gitlab_host("other.example.net", hosts)
    assert not localrepos.is_gitlab_host("other.example.net")
    assert localrepos.is_gitlab_host("gitlab.internal.example")
    for bare in ("gitlab", "gitlab.", "gitlab..example.com", "gitlab.example.com."):
        assert not localrepos.is_gitlab_host(bare), bare
    assert not localrepos.is_gitlab_host("github.com", frozenset({"github.com"}))
    assert localrepos.parse_remote("git@code.example.org:a/b/c.git", hosts) == (
        "code.example.org", "a/b", "c")


def test_the_roots_read_the_hosts_glab_is_configured_for(tmp_path, monkeypatch):
    import subprocess

    from diplomat_runtime import appconfig

    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    (tmp_path / "glab" / "config.yml").write_text(GLAB_CONFIG)
    clone = tmp_path / "c"
    clone.mkdir()
    for args in (["init", "-q"], ["remote", "add", "origin", "git@code.example.org:a/b/c.git"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True)
    appconfig.set_repo_roots([str(clone)])
    assert review.repo_roots() == [LocalRepo("a/b", "c", str(clone), "code.example.org")]
    assert review.roots_complete()


def test_a_gitlab_checkout_is_its_own_root_and_never_the_configured_repo():
    """Only GitHub remotes stand for the configured repo: a GitLab project of its name is
    another repo, and the same slug on two hosts is two roots."""
    remotes = {
        "/lab": "remote.origin.url git@gitlab.com:software-mansion/argent.git\n",
        "/labfork": "remote.origin.url git@gitlab.com:someone/argent.git\n",
        "/hub": "remote.origin.url git@github.com:software-mansion/argent.git\n",
        "/mixed": "remote.upstream.url https://gitlab.com/group/sub/radon.git\n"
                  "remote.origin.url git@github.com:me/argent.git\n",
    }
    configured = ("Software-Mansion", "Argent")
    assert localrepos.resolve_roots(["/lab", "/hub"], remotes, configured) == [
        LocalRepo("software-mansion", "argent", "/lab", "gitlab.com"),
        LocalRepo("software-mansion", "argent", "/hub")]
    assert localrepos.resolve_roots(["/lab"], remotes, configured) == [
        LocalRepo("software-mansion", "argent", "/lab", "gitlab.com")]
    assert localrepos.resolve_roots(["/labfork"], remotes, configured) == [
        LocalRepo("someone", "argent", "/labfork", "gitlab.com")], "not a fork of it"
    assert localrepos.resolve_roots(["/mixed"], remotes, configured) == [
        LocalRepo("Software-Mansion", "Argent", "/mixed")]
    assert localrepos.resolve_roots(["/mixed"], remotes, ("x", "y")) == [
        LocalRepo("me", "argent", "/mixed")]


def test_a_github_remote_outranks_a_gitlab_origin():
    """A GitLab mirror checked out beside a GitHub remote resolves to the GitHub repo:
    the configured one when the remote names it, else that GitHub sibling. Only a
    checkout with no GitHub remote is a GitLab root, upstream before origin."""
    remotes = {
        "/mirror": "remote.origin.url git@gitlab.com:acme/widget.git\n"
                   "remote.github.url git@github.com:acme/widget.git\n",
        "/sibling": "remote.origin.url git@gitlab.com:acme/widget.git\n"
                    "remote.gh.url https://github.com/someone/else\n",
        "/lab": "remote.mirror.url git@gitlab.com:other/one.git\n"
                "remote.origin.url git@gitlab.com:acme/widget.git\n",
    }
    assert localrepos.resolve_roots(["/mirror"], remotes, ("acme", "widget")) == [
        LocalRepo("acme", "widget", "/mirror")]
    assert localrepos.resolve_roots(["/sibling"], remotes, ("acme", "widget")) == [
        LocalRepo("someone", "else", "/sibling")]
    assert localrepos.resolve_roots(["/lab"], remotes, ("acme", "widget")) == [
        LocalRepo("acme", "widget", "/lab", "gitlab.com")]
    label = localrepos.Checkout("/sibling", tuple(localrepos.entries(
        "/sibling", remotes["/sibling"]))).slug
    assert label == "someone/else"


MIRRORED = ("remote.upstream.url git@gitlab.com:acme/widget.git\n"
            "remote.origin.url git@github.com:acme/widget.git\n")


def test_a_checkouts_github_remote_is_its_first_repo():
    """Within a checkout GitHub ranks before GitLab, so a link that names no host, and
    the label Settings shows, are the GitHub repo's even under a GitLab upstream."""
    found = localrepos.entries("/w", MIRRORED)
    assert found == [LocalRepo("acme", "widget", "/w"),
                     LocalRepo("acme", "widget", "/w", "gitlab.com")]
    for ref in ("acme/widget#5", "https://github.com/acme/widget/pull/5"):
        assert parse_pr_ref(ref, "o", "r", also=found).repo == found[0], ref
    assert parse_pr_ref("https://gitlab.com/acme/widget/-/merge_requests/5", "o", "r",
                        also=found).repo == found[1]
    assert localrepos.Checkout("/w", tuple(found)).slug == "acme/widget"
    assert localrepos.entries("/w", "remote.upstream.url git@gitlab.com:a/b.git\n"
                                    "remote.origin.url git@gitlab.com:c/d.git\n"
                                    "remote.zz.url git@github.com:e/f.git\n") == [
        LocalRepo("e", "f", "/w"), LocalRepo("a", "b", "/w", "gitlab.com"),
        LocalRepo("c", "d", "/w", "gitlab.com")]


def test_the_wizards_keep_a_listed_checkouts_repo_on_each_host(lab_only):
    """A listed checkout's GitHub repo and its GitLab mirror of the same slug are two
    repos a pasted link can name; only a root's slug takes a listed repo's place."""
    lab_only._local_checkouts = [
        localrepos.Checkout("/w", tuple(localrepos.entries("/w", MIRRORED))),
        localrepos.Checkout("/x", (LocalRepo("Group/Sub", "Proj", "/x"),
                                   LocalRepo("e", "f", "/x")))]
    assert lab_only.local_repos == (LAB, LocalRepo("acme", "widget", "/w"),
                                    LocalRepo("acme", "widget", "/w", "gitlab.com"),
                                    LocalRepo("e", "f", "/x"))


def test_a_gitlab_root_is_never_the_meshs_checkout(monkeypatch):
    monkeypatch.setattr(review, "roots_guessed", lambda: False)
    monkeypatch.setattr(core, "config", lambda: {"owner": "o", "repo": "r"})
    monkeypatch.setattr(review, "repo_roots", lambda: [
        LocalRepo("o", "r", "/src/lab", "gitlab.com"), HUB])
    assert review.mesh_checkout() == "/src/r"
    monkeypatch.setattr(review, "repo_roots", lambda: [
        LocalRepo("o", "r", "/src/lab", "gitlab.com")])
    assert review.mesh_checkout() is None
