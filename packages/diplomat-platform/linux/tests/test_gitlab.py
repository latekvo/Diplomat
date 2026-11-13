"""GitLab repo roots: every read goes through ``glab`` and comes back in the GitHub
shape, so everything past it is the GitHub path.

The adapters are pinned by the fixtures shared with ``GitLab.swift``; the rest pins
what a GitLab root changes: which CLI is asked, with what, as whom, and which link
names the item."""

from __future__ import annotations

import json
import re
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
#: LAB's root key: its items' keys are ``KEY#n``.
KEY = f"{HOST}/group/sub/proj"
HUB = LocalRepo("o", "r", "/src/r")


def _unasked(ids: list[int]) -> set[int]:
    raise AssertionError(f"a member page with no more looked up {ids}")


def _review_requests(env: dict, lookup) -> dict:
    return gitlab.review_requests(env, "carol", lookup)


#: The review-requests fixtures are asked for reviewer ``carol``, and their member pages
#: are complete, so nothing is looked up.
ADAPTERS = {
    "prs": gitlab.prs, "monitor-prs": gitlab.monitor_prs, "closed-prs": gitlab.closed_prs,
    **{name: lambda env: _review_requests(env, _unasked)
       for name in ("review-requests", "review-requests-nofiles", "review-requests-window",
                    "review-requests-left-review", "review-requests-dst",
                    "review-requests-reviewed")},
    "issues": lambda env: gitlab.issues(env, _unasked), "mr": gitlab.mr,
}
#: Fixtures with a full member page: in ``{answer, members}``, ``members`` the
#: ``members/all`` answer to any lookup; out ``{asked, result}``, ``asked`` the id lists
#: looked up.
PAGED = {"issues-paged": gitlab.issues, "review-requests-paged": _review_requests}
QUERIES = ("viewer", "prs", "monitor-prs", "closed-prs", "review-requests", "issues", "mr")
RELATIONS = "relations: [DIRECT, INHERITED, INVITED_GROUPS, SHARED_INTO_ANCESTORS]"


def _fixture(name: str, which: str) -> dict:
    return json.loads((FIXTURES / f"{name}.{which}.json").read_text())


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_each_adapter_turns_the_gitlab_answer_into_its_github_twin(name):
    assert ADAPTERS[name](_fixture(name, "input")) == _fixture(name, "expected")


@pytest.mark.parametrize("name", sorted(PAGED))
def test_an_author_past_a_full_member_page_is_looked_up_by_id(name):
    case = _fixture(name, "input")
    asked: list[list[int]] = []

    def lookup(ids: list[int]) -> set[int]:
        asked.append(ids)
        return {m["id"] for m in case["members"]}

    got = PAGED[name](case["answer"], lookup)
    assert {"asked": asked, "result": got} == _fixture(name, "expected")


def test_a_member_lookup_asks_forty_ids_a_read():
    case = _fixture("member-paths", "input")
    assert gitlab.member_paths(case["slug"], case["ids"]) == _fixture("member-paths",
                                                                      "expected")
    assert gitlab.member_paths("g/p", []) == []


def test_an_author_with_no_readable_id_fails_the_read():
    env = _fixture("issues-paged", "input")["answer"]
    env["data"]["project"]["issues"]["nodes"][0]["author"]["id"] = "gid://gitlab/User/14x"
    with pytest.raises(gh.GHError, match="GitLab user Dave has no readable id"):
        gitlab.issues(env, lambda ids: set())


def test_every_fixture_has_an_adapter():
    names = {p.name.split(".")[0] for p in FIXTURES.glob("*.json")}
    assert names == set(ADAPTERS) | set(PAGED) | {"member-paths", "glab-config", "keys"}


def test_the_queries_ask_what_their_adapters_read():
    """System-note discussions fill the discussions page, a thread's adapted comment is
    its LAST non-system note, a request older than the activity window is decided by my
    review state or falls back on the MR's creation, and the ``prs`` adapter reads no
    members."""
    for name in ("prs", "monitor-prs"):
        text = core.read_graphql(f"gitlab-{name}")
        assert "discussions(first: 100)" in text, name
        assert "notes(last: 20) { nodes { system author" in text, name
    assert "projectMembers" not in core.read_graphql("gitlab-prs")
    requests = core.read_graphql("gitlab-review-requests")
    assert "notes(last: 100, filter: ONLY_ACTIVITY)" in requests
    assert "diffHeadSha createdAt" in requests
    assert "reviewers { nodes { username mergeRequestInteraction { reviewState } } }" \
        in requests
    for name in ("issues", "review-requests"):
        text = core.read_graphql(f"gitlab-{name}")
        assert f"projectMembers(first: 100, {RELATIONS}) {{\n      pageInfo {{ hasNextPage }}" \
            in text, name
        assert "        author { id username }\n" in text, name
    assert "notes(first: 100, filter: ONLY_COMMENTS) { nodes { author { id username } } }" \
        in core.read_graphql("gitlab-issues")


# ---- glab ---------------------------------------------------------------------


class FakeGlab:
    """``glab.run`` answering each ``gitlab-*`` query from ``answers`` (a dict, or an
    exception to raise) and recording ``(query, -f/-F variables, host)``; REST paths
    likewise from ``rest`` into ``rest_asked``, a missing one a 404."""

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
        name = self._names[query]
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
    assert item_key_from_url(prs[0].url) == f"{KEY}#12"
    assert item_key_from_url(issues[0].url) == f"{KEY}#5"


MEMBERS_14_15 = "projects/group%2Fsub%2Fproj/members/all?per_page=100&user_ids[]=14&user_ids[]=15"


def test_a_full_member_page_costs_one_lookup_and_a_failed_one_fails_the_read(monkeypatch):
    paged = _fixture("issues-paged", "input")
    fake = FakeGlab(monkeypatch, issues=paged["answer"])
    fake.rest = {MEMBERS_14_15: paged["members"]}
    issues = API.fetch_open_issues(LAB)
    assert fake.rest_asked == [MEMBERS_14_15]
    assert fake.argv[1] == ["api", "--hostname", HOST, MEMBERS_14_15]
    assert [(i.number, i.author_association, i.member_responded) for i in issues] == [
        (9, "MEMBER", False), (8, "NONE", True), (7, "NONE", False)]

    fake.rest = {MEMBERS_14_15: gh.GHError("glab exited 1: glab: 502 Bad Gateway")}
    fake.rest_asked.clear()
    with pytest.raises(gh.GHError, match="502 Bad Gateway"):
        API.fetch_open_issues(LAB)
    assert fake.rest_asked == [MEMBERS_14_15] * 2, "retried once"


def test_a_review_requests_author_past_a_full_member_page_is_looked_up(monkeypatch):
    from diplomat_app import autofixmonitor

    paged = _fixture("review-requests-paged", "input")
    fake = FakeGlab(monkeypatch, review_requests=paged["answer"])
    fake.rest = {MEMBERS_14_15: paged["members"]}
    reqs = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert fake.rest_asked == [MEMBERS_14_15]
    assert [(r.number, r.author_association) for r in reqs] == [
        (3, "NONE"), (2, "MEMBER"), (1, "MEMBER")]


def test_a_member_lookup_asks_glab_once_per_forty_ids(monkeypatch):
    fake = FakeGlab(monkeypatch)
    paths = gitlab.member_paths(LAB.slug, list(range(1, 82)))
    fake.rest = {paths[0]: [{"id": 7, "username": "a"}], paths[1]: [],
                 paths[2]: [{"id": 81, "username": "b"}]}
    assert glab.members(LAB, list(range(1, 82))) == {7, 81}
    assert fake.rest_asked == paths and len(paths) == 3
    assert glab.members(LAB, []) == set() and len(fake.rest_asked) == 3


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
    """GraphQL never makes GitLab recheck a merge status; the REST read with
    ``with_merge_status_recheck`` does, and only undecided ones are asked."""
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
        for key, n, url in ((f"conflicts:{KEY}#6", 6, LAB.link("pull", 6)),
                            ("conflicts:o/r#8", 8, HUB.link("pull", 8)))]
    monkeypatch.setattr(type(lab_only), "_auto_tasks_running", lambda self: 99)  # no bays
    snaps = [replace(_snap(number=6, mergeable="UNKNOWN"), url=LAB.link("pull", 6)),
             _snap(number=8, mergeable="UNKNOWN")]
    lab_only._drain_queued_tasks(snaps, closed=set(), read=[HUB, LAB])
    assert [t.id for t in lab_only.queued_tasks] == [f"conflicts:{KEY}#6"]


def test_the_commit_keeps_a_conflict_fix_on_an_undecided_merge_request(lab_only, monkeypatch):
    from dataclasses import replace

    from test_autofix import _job, _snap

    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    lab_only.queued_tasks = [
        autofix.QueuedTask(key, replace(_job(number=n, action="conflicts", counter="conflicts"),
                                        pr_url=url), 1)
        for key, n, url in ((f"conflicts:{KEY}#6", 6, LAB.link("pull", 6)),
                            ("conflicts:o/r#8", 8, HUB.link("pull", 8)))]
    snaps = [replace(_snap(number=6, mergeable="UNKNOWN"), url=LAB.link("pull", 6)),
             _snap(number=8, mergeable="UNKNOWN")]
    lab_only._staged_queue = []
    lab_only._reconcile_my_conflicts(snaps, 0.0)
    lab_only.commit_queue()
    assert [t.id for t in lab_only.queued_tasks] == [f"conflicts:{KEY}#6"]


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
    assert f"conflicts:{KEY}#12" in {t.id for t in lab_only.queued_tasks}

    unchecked = copy.deepcopy(conflicting)
    mr = next(m for m in unchecked["data"]["project"]["mergeRequests"]["nodes"]
              if m["iid"] == "12")
    mr.update(conflicts=False, detailedMergeStatus="UNCHECKED")
    fake.answers["monitor-prs"] = unchecked
    fake.rest = {"projects/group%2Fsub%2Fproj/merge_requests/12?with_merge_status_recheck=true":
                 {"has_conflicts": False, "detailed_merge_status": "unchecked"}}
    lab_only._autofix_poll_once()
    assert lab_only.autofix_poll_error is None
    assert f"conflicts:{KEY}#12" in {t.id for t in lab_only.queued_tasks}


def test_a_request_older_than_the_activity_window_is_answered_inside_it(monkeypatch):
    from diplomat_app import autofixmonitor

    FakeGlab(monkeypatch, review_requests=_fixture("review-requests-window", "input"))
    answered, silent = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert answered.requested_at == "2026-11-06T08:00:00Z"
    assert not answered.owe_review
    assert silent.requested_at == "2026-11-02T07:00:00Z" and silent.owe_review


def test_a_long_merge_request_i_reviewed_is_not_owed_again(monkeypatch):
    """My request and response both older than the windows: GitLab's review state says
    whether I answered. Unanswered, the request reads as made at the window's oldest
    note; re-requested inside the window, the note decides."""
    from diplomat_app import autofixmonitor

    FakeGlab(monkeypatch, review_requests=_fixture("review-requests-reviewed", "input"))
    got = {r.number: r for r in autofixmonitor.fetch_review_requests(LAB, "carol")}
    assert sorted(got) == [44, 45, 46, 47]
    assert [n for n, r in sorted(got.items()) if r.owe_review] == [44, 45, 46]
    assert got[44].requested_at == "2026-11-06T08:00:00Z"
    assert got[45].requested_at == "2026-11-09T08:00:00Z"


def test_a_review_left_as_comments_alone_answers_the_request(monkeypatch):
    """GitLab's "left review comments" is my review, after my own comments have left the
    comment window."""
    from diplomat_app import autofixmonitor

    FakeGlab(monkeypatch, review_requests=_fixture("review-requests-left-review", "input"))
    (request,) = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert request.my_last_comment_at is None
    assert request.my_last_review_at == "2026-11-10T09:00:00Z"
    assert not request.owe_review


def test_times_across_a_clock_change_compare_in_utc(monkeypatch):
    """An approval at 02:10+01:00 follows a request at 02:40+02:00, and the window's
    oldest note is the one at 02:50+02:00."""
    from diplomat_app import autofixmonitor

    FakeGlab(monkeypatch, review_requests=_fixture("review-requests-dst", "input"))
    answered, unanswered = autofixmonitor.fetch_review_requests(LAB, "carol")
    assert (answered.requested_at, answered.my_last_review_at) == ("2026-10-25T00:40:00Z",
                                                                   "2026-10-25T01:10:00Z")
    assert not answered.owe_review
    assert unanswered.requested_at == "2026-10-25T00:50:00Z" and unanswered.owe_review


def test_an_unreadable_time_fails_the_read():
    env = _fixture("review-requests-dst", "input")
    env["data"]["project"]["mergeRequests"]["nodes"][0]["comments"]["nodes"][0][
        "createdAt"] = "yesterday"
    with pytest.raises(gh.GHError, match="GitLab time yesterday is unreadable"):
        _review_requests(env, _unasked)


def test_the_panel_reads_a_page_of_merge_requests_as_long_as_githubs_of_pull_requests():
    page = re.compile(r"(?:pullRequests\(states: OPEN|mergeRequests\(state: opened), first: (\d+)")
    assert page.search(core.read_graphql("gitlab-prs"))[1] \
        == page.search(core.read_graphql("prs"))[1]


def test_the_closed_read_keys_merge_requests_by_the_whole_namespace(nothing_closed,
                                                                    monkeypatch):
    FakeGlab(monkeypatch, closed_prs=_fixture("closed-prs", "input"))
    assert nothing_closed(LAB) == {f"{KEY}#28", f"{KEY}#29", f"{KEY}#30"}


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
    obs = probes.merged_prs({f"{KEY}#3", "o/r#3"}, [HUB, LAB])
    assert obs.value == {f"{KEY}#3"}
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


def test_a_rewritten_glab_config_reads_the_gitlab_login_again(store,  # noqa: F811
                                                              monkeypatch):
    """``glab auth login`` as another account rewrites glab's config; while it is
    unchanged the kept login is used without asking glab."""
    import os

    config = localrepos.glab_config_file()
    fake = FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}})
    assert store.me_for(LAB) == "carol"
    assert store.me_for(LAB) == store.known_me(LAB) == "carol"
    assert fake.names() == ["viewer"]
    fake.answers["viewer"] = {"data": {"currentUser": {"username": "dave"}}}
    config.write_text(f"hosts:\n    {HOST}:\n        user: dave\n")
    assert store.known_me(LAB) == ""
    assert store.me_for(LAB) == "dave"
    assert store.me_for(LAB) == "dave"
    assert fake.names() == ["viewer"] * 2
    read = config.stat()
    config.write_text(config.read_text().replace("dave", "erin"))
    os.utime(config, ns=(read.st_atime_ns, read.st_mtime_ns + 1_000_000_000))
    assert store.known_me(LAB) == "", "the same size, a later time"
    assert store.me_for(LAB) == "dave"
    read = config.stat()
    config.write_text(config.read_text().replace("erin", "frank"))
    os.utime(config, ns=(read.st_atime_ns, read.st_mtime_ns))
    assert store.known_me(LAB) == "", "the same time, another size"


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
    assert f"conflicts:{KEY}#12" in ids
    assert f"review-req:{KEY}#22" in ids
    assert fake.names().count("viewer") == 1
    assert {a[1].get("author") or a[1].get("reviewer") for a in fake.asked
            if a[0] in ("monitor-prs", "review-requests")} == {"carol"}


def test_without_a_github_login_a_gitlab_root_is_polled_and_a_github_one_held(lab_only,
                                                                              monkeypatch):
    """No GitHub login stops the whole poll only with no GitLab root; with one, the
    GitHub roots go unread like failed reads and the GitLab ones are read."""
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
    assert f"conflicts:{KEY}#12" in {t.id for t in lab_only.queued_tasks}
    assert lab_only._held_keys == {"o/r"}
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
    """A merge request of a root not on the panel: its author is compared with glab's
    login on that host, read on demand, not with nothing."""
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


@pytest.mark.parametrize("wizard,field,link", [
    ("wizardview.WizardView", "specific_pr", "merge_requests/12"),
    ("issuewizardview.IssueWizardView", "specific_issue", "issues/40"),
    ("conflictwizardview.ConflictWizardView", "specific_pr", "merge_requests/12"),
])
def test_a_wizard_run_on_another_roots_item_is_mine_as_that_roots_login(
        lab_only, monkeypatch, wizard, field, link):
    """The GitHub root is on the panel; the pasted link is a GitLab root's."""
    import importlib

    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(review, "repo_roots", lambda: [HUB, LAB])
    monkeypatch.setattr(lab_only, "start_background", lambda work, name=None: None)
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}})
    assert lab_only.me_for(LAB) == "carol" and lab_only.selected_repo == HUB
    module, name = wizard.split(".")
    w = getattr(importlib.import_module(f"diplomat_app.{module}"), name)(lab_only)
    assert w._config().me == "alice"
    w.target.setCurrentIndex(w.target.findData(type(w._config().target).SPECIFIC))
    getattr(w, field).setText(f"https://{HOST}/group/sub/proj/-/{link}")
    cfg = w._config()
    assert (cfg.target_root, cfg.me) == (LAB, "carol")


def test_refresh_lists_a_gitlab_roots_merge_requests(lab_only, monkeypatch):
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
             prs=_fixture("prs", "input"), issues=_fixture("issues", "input"))
    lab_only.refresh()
    assert lab_only.lookup(12).url == f"https://{HOST}/group/sub/proj/-/merge_requests/12"
    assert lab_only.known_me(LAB) == "carol"


def test_the_lookups_open_button_names_the_forge_and_never_guesses_a_gitlab_link(
        lab_only, monkeypatch):
    from diplomat_app import panel as panel_mod
    from diplomat_app.store import Store
    from PySide6.QtWidgets import QApplication, QToolButton

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    opened: list[str] = []
    monkeypatch.setattr(panel_mod.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()))
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "carol"}}},
             prs=_fixture("prs", "input"), issues=_fixture("issues", "input"))
    lab_only.refresh()
    panel = panel_mod.Panel(lab_only)

    def buttons(n: int) -> list[QToolButton]:
        panel._rebuild_lookup(n)
        return [b for b in panel.lookup_scroll.widget().findChildren(QToolButton)
                if b.text() == "↗"]

    (mr,) = buttons(12)
    assert mr.toolTip() == "Open #12 on GitLab"
    mr.click()
    assert opened == [f"https://{HOST}/group/sub/proj/-/merge_requests/12"]
    assert buttons(999) == [], "a number on no list is a merge request or an issue"

    monkeypatch.setattr(review, "repo_roots", lambda: [HUB])
    (issue,) = buttons(999)
    assert issue.toolTip() == "Open #999 on GitHub"
    issue.click()
    assert opened[-1] == "https://github.com/o/r/issues/999"
    panel.deleteLater()


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


def test_a_bare_bang_number_is_a_merge_request_of_a_gitlab_home_only():
    assert parse_pr_ref(" !5 ", "group/sub", "proj", host=HOST) == prref.PRRef(5, False)
    assert parse_pr_ref("!5", "o", "r").number is None, "GitHub has no ! spelling"
    assert parse_pr_ref("!5", "group/sub", "proj", "issues", host=HOST).number is None
    assert parse_pr_ref("!0", "group/sub", "proj", host=HOST).number is None


def test_a_work_items_link_is_an_issue():
    item = f"https://{HOST}/group/sub/proj/-/work_items/7?foo=1"
    assert parse_pr_ref(item, "group/sub", "proj", "issues", host=HOST).number == 7
    assert not parse_pr_ref(item, "group/sub", "proj", host=HOST).is_valid, "not a PR field's"
    assert item_key_from_url(item) == f"{KEY}#7"


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

    live = A.Observation.present({f"{KEY}#337": "pts/7"})
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
    assert run.item == f"{KEY}#337"
    (hub,) = A.synthesize_untracked([], A.Observation.present({"o/r#5": ""}), 0.0,
                                    (HUB, LAB))
    assert hub.pr_url == "https://github.com/o/r/pull/5"


def test_a_nested_namespace_is_labelled_by_its_last_segment():
    assert prref.item_label(f"{KEY}#5", 2) == "proj#5"


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
        h: h for h in ("gitlab.com", "code.example.org", "other.example.net")}
    assert localrepos.config_hosts("hosts:\n") == {}
    (tmp_path / "glab" / "config.yml").write_text(GLAB_CONFIG)
    hosts = localrepos.gitlab_hosts()
    assert localrepos.is_gitlab_host("other.example.net", hosts)
    assert not localrepos.is_gitlab_host("other.example.net")
    assert localrepos.is_gitlab_host("gitlab.internal.example")
    for bare in ("gitlab", "gitlab.", "gitlab..example.com", "gitlab.example.com."):
        assert not localrepos.is_gitlab_host(bare), bare
    assert not localrepos.is_gitlab_host("github.com", {"github.com": "github.com"})
    assert localrepos.parse_remote("git@code.example.org:a/b/c.git", hosts) == (
        "code.example.org", "a/b", "c")


def test_a_hosts_ssh_host_names_that_gitlab_host():
    """A remote on a configured host's non-empty ``ssh_host`` is on that host; an empty
    one, one nested deeper and one outside ``hosts:`` name nothing, and a configured
    host's own name wins over another's ``ssh_host``."""
    case = _fixture("glab-config", "input")
    expected = _fixture("glab-config", "expected")
    hosts = localrepos.config_hosts(case["config"])
    assert hosts == expected["hosts"]
    assert [list(r) if (r := localrepos.parse_remote(u, hosts)) else None
            for u in case["remotes"]] == expected["remotes"]


def test_a_root_on_an_ssh_host_is_on_its_gitlab_host(tmp_path, monkeypatch):
    import subprocess

    from diplomat_runtime import appconfig

    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    (tmp_path / "glab" / "config.yml").write_text(_fixture("glab-config", "input")["config"])
    clone = tmp_path / "c"
    clone.mkdir()
    for args in (["init", "-q"], ["remote", "add", "origin", "git@ssh.gl.example.com:ns/p.git"]):
        subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True)
    appconfig.set_repo_roots([str(clone)])
    assert review.repo_roots() == [LocalRepo("ns", "p", str(clone), "gl.example.com")]


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
    """A checkout with a GitHub remote resolves to a GitHub repo, the configured one
    when the remote names it; only one with none is a GitLab root, upstream before
    origin."""
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
    repos a pasted link can name; only a root's key takes a listed repo's place, so the
    GitHub repo of a GitLab root's slug stays listed."""
    lab_only._local_checkouts = [
        localrepos.Checkout("/w", tuple(localrepos.entries("/w", MIRRORED))),
        localrepos.Checkout("/x", (LocalRepo("Group/Sub", "Proj", "/x", HOST),
                                   LocalRepo("Group/Sub", "Proj", "/x"),
                                   LocalRepo("e", "f", "/x")))]
    assert lab_only.local_repos == (LAB, LocalRepo("acme", "widget", "/w"),
                                    LocalRepo("acme", "widget", "/w", "gitlab.com"),
                                    LocalRepo("Group/Sub", "Proj", "/x"),
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


# ---- one slug on GitHub and on GitLab ----------------------------------------------

GOLDEN = FIXTURES.parents[2] / "assets/golden-prompts"
TWIN_HUB = LocalRepo("Acme", "Widget", "/src/hub")
TWIN_LAB = LocalRepo("acme", "widget", "/src/lab", HOST)


def test_keys_name_a_gitlab_item_by_its_host():
    """GitHub keys stay ``owner/repo#n``; a GitLab one carries its host, so the two
    twins agree on every key (the fixture ``GitLab.swift``'s smoke reads too)."""
    case, want = _fixture("keys", "input"), _fixture("keys", "expected")
    roots = [LocalRepo(r["owner"], r["repo"], "", r["host"]) for r in case["roots"]]
    assert [{"key": r.key, "item": prref.item_key(r.owner, r.repo, 5, r.host)}
            for r in roots] == want["roots"]
    assert [item_key_from_url(u) for u in case["urls"]] == want["urls"]
    assert [list(s) if (s := prref.split_item_key(k)) else None
            for k in case["split"]] == want["split"]
    texts = [(GOLDEN / p["golden"]).read_text() if "golden" in p else p["text"]
             for p in case["prompts"]]
    assert [list(autofix.prompt_items(t, set(p["roots"])))
            for t, p in zip(texts, case["prompts"])] == want["prompts"]


def test_a_name_with_no_host_is_the_github_roots_first():
    roots = [TWIN_LAB, TWIN_HUB]
    assert prref.root_named("acme/widget", roots) is TWIN_HUB
    assert prref.root_named(f"{HOST.upper()}/acme/widget", roots) is TWIN_LAB
    assert prref.root_named("group/sub/proj", [TWIN_HUB, LAB]) is LAB
    assert prref.root_named("acme/other", roots) is None
    ref = parse_pr_ref("acme/widget#5", "o", "r", also=roots)
    assert (ref.number, ref.repo) == (5, TWIN_HUB)
    ref = parse_pr_ref(f"https://{HOST}/acme/widget/-/merge_requests/5", "o", "r", also=roots)
    assert (ref.number, ref.repo) == (5, TWIN_LAB)
    assert parse_pr_ref("acme/widget#5", "acme", "widget", also=roots, host=HOST).repo is None


def test_one_slug_on_github_and_on_gitlab_is_two_roots_with_two_of_everything(
        lab_only, monkeypatch):
    """#5 of each is its own queued task, ledger task and run record, each resolving to
    its own root, link and checkout; both resolve as roots; and a live agent on either
    is that root's."""
    from diplomat_app import autofixmonitor, probes

    from diplomat_runtime import agentstate as A
    from diplomat_runtime import telemetry

    monkeypatch.setattr(review, "repo_roots", lambda: [TWIN_HUB, TWIN_LAB])
    FakeGlab(monkeypatch, viewer={"data": {"currentUser": {"username": "alice"}}})
    monkeypatch.setattr(autofixmonitor, "fetch_snapshots", lambda root, me: [
        autofix.PRSnapshot(5, "t", root.link("pull", 5), False, "CONFLICTING", "", 0, 0,
                           head_sha="ab" * 20)])
    monkeypatch.setattr(autofixmonitor, "fetch_review_requests", lambda *a, **k: [])
    lab_only._autofix_poll_once()
    lab_only._autofix_poll_once()

    assert lab_only.autofix_poll_error is None
    hub_key, lab_key = "conflicts:acme/widget#5", f"conflicts:{HOST}/acme/widget#5"
    tasks = {t.id: t for t in lab_only.queued_tasks}
    assert set(tasks) == {hub_key, lab_key}
    for key, root in ((hub_key, TWIN_HUB), (lab_key, TWIN_LAB)):
        job = tasks[key].job
        assert (job.pr_url, job.checkout) == (root.link("pull", 5), root.path)
        assert lab_only._root_named(autofix.item_of_key(key)) is root
        record = A.RunRecord(run_id=key, dispatched_at=0.0, pr_number=5, pr_url=job.pr_url,
                             source=A.SOURCE_AUTO, placement=A.PLACEMENT_LOCAL)
        assert record.item == key.partition(":")[2]
    assert {t.key for t in telemetry.load().tasks if t.key.startswith("conflicts:")} == {
        f"conflicts:github.com/Acme/Widget#5@{'ab' * 20}",
        f"conflicts:{HOST}/acme/widget#5@{'ab' * 20}"}

    remotes = {"/hub": "remote.origin.url git@github.com:Acme/Widget.git\n",
               "/lab": f"remote.origin.url git@{HOST}:acme/widget.git\n"}
    assert localrepos.resolve_roots(["/hub", "/lab"], remotes, ("o", "r")) == [
        LocalRepo("Acme", "Widget", "/hub"), LocalRepo("acme", "widget", "/lab", HOST)]
    assert lab_only.local_repos == (TWIN_HUB, TWIN_LAB)

    hub_prompt = (GOLDEN / "conflicts-single.txt").read_text().replace(
        "software-mansion/argent", "acme/widget").replace("#337", "#5")
    lab_prompt = (GOLDEN / "review-single-gitlab.txt").read_text().replace(
        "group/sub/proj", "acme/widget").replace("#337", "#5")
    # ps prints each argv on one line, a newline in it as "?".
    dump = A.Observation.present("".join(
        f"{pid} pts/{pid - 100} 00:05 claude {prompt.replace(chr(10), '?')}\n"
        for pid, prompt in ((101, hub_prompt), (102, lab_prompt))))
    live = probes.live_agents(dump, {r.key for r in (TWIN_HUB, TWIN_LAB)})
    assert live.value == {"acme/widget#5": "pts/1", f"{HOST}/acme/widget#5": "pts/2"}
    booked = A.synthesize_untracked([], live, 0.0, (TWIN_HUB, TWIN_LAB))
    assert {r.pr_url: r.tty for r in booked} == {TWIN_HUB.link("pull", 5): "pts/1",
                                                 TWIN_LAB.link("pull", 5): "pts/2"}


def test_a_gitlab_key_no_root_has_is_never_asked_of_github(monkeypatch):
    """Its first segment is a host, so it names no GitHub repo: with no root to ask
    through it is left unknown, and an untracked agent on it is linked on its host."""
    from diplomat_app import probes

    from diplomat_runtime import agentstate as A

    fake = FakeGlab(monkeypatch)
    asked_gh = []
    monkeypatch.setattr(gh, "run",
                        lambda args, **_: asked_gh.append(args) or b'{"state":"MERGED"}\n')
    assert probes.merged_prs({f"{KEY}#3", "o/r#3"}).value == {"o/r#3"}
    assert len(asked_gh) == 1 and fake.asked == []

    live = A.Observation.present({f"{KEY}#4": "pts/3"})
    [booked] = A.synthesize_untracked([], live, 0.0, ())
    assert (booked.pr_url, booked.item) == (LAB.link("pull", 4), f"{KEY}#4")


def test_a_key_on_a_dotless_gitlab_host_is_that_hosts_project(monkeypatch):
    """A key's kind is its depth, not a dot: ``owner/repo`` is GitHub, and three segments
    or more a GitLab project (which always has a namespace), so a glab host with no dot
    (``localhost``) is never asked of GitHub."""
    from diplomat_app import probes

    from diplomat_runtime import agentstate as A

    assert prref.repo_of_key("localhost/grp/proj") == LocalRepo("grp", "proj", "", "localhost")
    assert prref.repo_of_key("gitlab/a/b/c") == LocalRepo("a/b", "c", "", "gitlab")
    assert prref.repo_of_key("o/r") == LocalRepo("o", "r", "")
    assert prref.repo_of_key("o/web.site") == LocalRepo("o", "web.site", "")

    fake = FakeGlab(monkeypatch)
    asked_gh = []
    monkeypatch.setattr(gh, "run",
                        lambda args, **_: asked_gh.append(args) or b'{"state":"MERGED"}\n')
    assert probes.merged_prs({"localhost/grp/proj#3", "o/r#3"}).value == {"o/r#3"}
    assert len(asked_gh) == 1 and fake.asked == []

    live = A.Observation.present({"localhost/grp/proj#4": "pts/3"})
    [booked] = A.synthesize_untracked([], live, 0.0, ())
    assert booked.pr_url == "https://localhost/grp/proj/-/merge_requests/4"


@needs_core
def test_an_issue_fix_is_its_own_roots_on_two_roots_of_one_slug(lab_only, monkeypatch):
    """A fix asked on either twin records that root's issue link, so its row carries
    that root's item and Comms knows the fix as that root's only, though both rows wear
    one label."""
    from types import SimpleNamespace

    from diplomat_app import issues
    from diplomat_app.store import Store

    from diplomat_runtime import agentstate as A

    monkeypatch.setattr(review, "repo_roots", lambda: [TWIN_HUB, TWIN_LAB])
    records = []
    for root in (TWIN_HUB, TWIN_LAB):
        config = issues.IssueConfig(target=issues.Target.SPECIFIC, specific_issue="9",
                                    home=root).prompt_payload()
        entry = autofix.RequestedWork(number=9, url="", author="", config=config,
                                      repo=root.key)
        job = lab_only._requested_task(entry).job
        assert (job.pr_url, job.pr_number) == (root.link("issues", 9), None)
        records.append((A.RunRecord(run_id=root.key, dispatched_at=0.0, pr_url=job.pr_url,
                                    kind=autofix.QUEUE_ISSUES_ACTION,
                                    label="Issues · widget#9 · deep"),
                        SimpleNamespace(state="running", reason="")))
    monkeypatch.setattr(lab_only, "_agent_tick", lambda: SimpleNamespace(rows=records))
    rows = lab_only.running_tasks
    assert [a.item for a in rows] == ["acme/widget#9", f"{HOST}/acme/widget#9"]
    for row in rows:
        assert Store._comms_row_doing(
            autofix.queue_key(autofix.QUEUE_ISSUES_ACTION, row.item), rows) is row
    assert Store._comms_row_doing(
        autofix.queue_key(autofix.QUEUE_ISSUES_ACTION, "gitlab.other.org/acme/widget#9"),
        rows) is None
