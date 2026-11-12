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
ADAPTERS = {
    "prs": gitlab.prs, "monitor-prs": gitlab.monitor_prs, "closed-prs": gitlab.closed_prs,
    "review-requests": gitlab.review_requests,
    "review-requests-nofiles": gitlab.review_requests, "issues": gitlab.issues,
    "mr": gitlab.mr,
}
QUERIES = ("viewer", "prs", "monitor-prs", "closed-prs", "review-requests", "issues", "mr")


def _fixture(name: str, which: str) -> dict:
    return json.loads((FIXTURES / f"{name}.{which}.json").read_text())


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_each_adapter_turns_the_gitlab_answer_into_its_github_twin(name):
    assert ADAPTERS[name](_fixture(name, "input")) == _fixture(name, "expected")


def test_every_fixture_has_an_adapter():
    names = {p.name.split(".")[0] for p in FIXTURES.glob("*.json")}
    assert names == set(ADAPTERS)


# ---- glab ---------------------------------------------------------------------


class FakeGlab:
    """``glab.run`` answering each shared ``gitlab-*`` query from ``answers`` (a dict,
    or an exception to raise), recording ``(query, -f/-F variables, host)``."""

    def __init__(self, monkeypatch, **answers):
        self.answers = {k.replace("_", "-"): v for k, v in answers.items()}
        self.asked: list[tuple[str, dict, str]] = []
        self.argv: list[list[str]] = []
        self._names = {core.read_graphql(f"gitlab-{q}"): q for q in QUERIES}
        monkeypatch.setattr(glab, "run", self)
        monkeypatch.setattr(glab.time, "sleep", lambda s: None)

    def __call__(self, args, timeout=60.0):
        self.argv.append(args)
        pairs = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a in ("-f", "-F"))
        name = self._names[pairs.pop("query")]
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
        LocalRepo("group/sub", "radon", "/mixed", "gitlab.com")]


def test_a_gitlab_root_is_never_the_meshs_checkout(monkeypatch):
    monkeypatch.setattr(review, "roots_guessed", lambda: False)
    monkeypatch.setattr(core, "config", lambda: {"owner": "o", "repo": "r"})
    monkeypatch.setattr(review, "repo_roots", lambda: [
        LocalRepo("o", "r", "/src/lab", "gitlab.com"), HUB])
    assert review.mesh_checkout() == "/src/r"
    monkeypatch.setattr(review, "repo_roots", lambda: [
        LocalRepo("o", "r", "/src/lab", "gitlab.com")])
    assert review.mesh_checkout() is None
