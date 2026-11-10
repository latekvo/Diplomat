"""The extra checkouts listed in Settings, and the links that resolve into them.

Three layers, each pinned where it is decided: :mod:`localrepos` reads a checkout's
coordinates off its real git remotes, :func:`prref.parse_pr_ref` resolves a pasted link
against them, and the configs turn that into the repo the prompt names, the directory
the agent starts in, and a run the dispatch gate must not confuse with the configured
repo's PR of the same number.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from diplomat_runtime import appconfig, autofix, core, localrepos, prref, review
from diplomat_runtime.prref import LocalRepo, PRRef, parse_pr_ref
from diplomat_runtime.prtarget import PRTarget
from diplomat_app import conflicts, issues

OWNER, REPO = "software-mansion", "argent"
RADON = LocalRepo("software-mansion", "radon", "/src/radon")
FORK = LocalRepo("me", "radon", "/src/radon")


def _git(path, *args) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _checkout(tmp_path, name: str, **remotes: str):
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q")
    for remote, url in remotes.items():
        _git(path, "remote", "add", remote, url)
    return path


# ---- remote URLs -----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "git@github.com:software-mansion/radon.git",
    "git@github.com:software-mansion/radon",
    "https://github.com/software-mansion/radon.git",
    "https://github.com/software-mansion/radon",
    "https://github.com/software-mansion/radon/",
    "ssh://git@github.com/software-mansion/radon.git",
    "ssh://git@github.com/software-mansion/radon",
    "https://user@github.com/software-mansion/radon.git",
    "ssh://git@github.com:22/software-mansion/radon.git",
])
def test_every_github_remote_spelling_parses(url):
    assert localrepos.parse_remote(url) == ("software-mansion", "radon")


def test_a_repo_name_with_dots_keeps_them():
    assert localrepos.parse_remote("git@github.com:o/o.github.io.git") == ("o", "o.github.io")


@pytest.mark.parametrize("url", [
    "git@gitlab.com:software-mansion/radon.git",
    "https://example.com/software-mansion/radon",
    "/srv/git/radon.git",
    "https://github.com/software-mansion",
    "https://github.com.evil.io/software-mansion/radon",
    "",
])
def test_anything_else_is_no_github_remote(url):
    assert localrepos.parse_remote(url) is None


# ---- a checkout's repos ----------------------------------------------------


def test_upstream_names_the_checkout_and_every_remote_resolves_to_it(tmp_path):
    path = _checkout(tmp_path, "radon",
                     origin="git@github.com:me/radon.git",
                     backup="https://github.com/backup/radon",
                     upstream="https://github.com/software-mansion/radon.git")
    c = localrepos.inspect(str(path))
    assert c.slug == "software-mansion/radon"
    assert c.repos == (LocalRepo("software-mansion", "radon", str(path)),
                       LocalRepo("me", "radon", str(path)),
                       LocalRepo("backup", "radon", str(path)))


def test_origin_names_it_when_there_is_no_upstream(tmp_path):
    path = _checkout(tmp_path, "radon",
                     backup="https://github.com/backup/radon",
                     origin="git@github.com:me/radon.git")
    assert localrepos.inspect(str(path)).slug == "me/radon"


def test_the_first_remote_names_it_when_neither_exists(tmp_path):
    path = _checkout(tmp_path, "radon",
                     zeta="https://github.com/zeta/radon",
                     alpha="https://github.com/alpha/radon")
    # Config order, which is the order the remotes were added - not alphabetical.
    assert localrepos.inspect(str(path)).slug == "zeta/radon"


def test_two_remotes_on_one_repo_are_one_entry(tmp_path):
    path = _checkout(tmp_path, "radon",
                     origin="git@github.com:software-mansion/radon.git",
                     https="https://github.com/Software-Mansion/Radon")
    assert len(localrepos.inspect(str(path)).repos) == 1


def test_a_checkout_without_a_github_remote_answers_for_nothing(tmp_path):
    path = _checkout(tmp_path, "mirror", origin="git@gitlab.com:x/y.git")
    c = localrepos.inspect(str(path))
    assert c.repos == () and c.slug is None


def test_a_plain_folder_answers_for_nothing(tmp_path):
    (tmp_path / "plain").mkdir()
    assert localrepos.inspect(str(tmp_path / "plain")).repos == ()
    assert localrepos.inspect(str(tmp_path / "missing")).repos == ()


def test_a_tilde_path_is_expanded_for_the_spawn_and_kept_for_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _checkout(tmp_path, "radon", origin="git@github.com:software-mansion/radon.git")
    c = localrepos.inspect("~/radon")
    assert c.path == "~/radon"
    assert c.repos[0].path == str(path)


def test_listed_order_decides_which_checkout_a_shared_repo_resolves_to(tmp_path):
    a = _checkout(tmp_path, "a", origin="git@github.com:software-mansion/radon.git")
    b = _checkout(tmp_path, "b", origin="git@github.com:software-mansion/radon.git")
    also = localrepos.local_repos(localrepos.resolve([str(b), str(a)]))
    ref = parse_pr_ref("software-mansion/radon#1", OWNER, REPO, also=also)
    assert ref.repo.path == str(b)


# ---- repo roots --------------------------------------------------------------


def _remotes(**urls: str) -> str:
    """``remote_config`` output for these remotes, in this order."""
    return "".join(f"remote.{name}.url {url}\n" for name, url in urls.items())


def test_entries_rank_upstream_then_origin_then_config_order_and_skip_the_rest():
    out = localrepos.entries("/p", _remotes(
        zeta="https://github.com/zeta/radon", origin="git@github.com:me/radon.git",
        lab="git@gitlab.com:x/y.git", upstream="https://github.com/Software-Mansion/Radon",
        again="https://github.com/software-mansion/radon") + "garbage\n")
    assert out == [LocalRepo("Software-Mansion", "Radon", "/p"), LocalRepo("me", "radon", "/p"),
                   LocalRepo("zeta", "radon", "/p")]


def test_a_root_answers_for_the_configured_repo_when_it_has_that_remote():
    """A fork checkout of the configured repo is that repo's root, whichever remote
    git ranks first: it is the repo every automatic job of a single-root setup ran on."""
    remotes = {"/a": _remotes(origin="git@github.com:me/argent.git",
                              sm="https://github.com/Software-Mansion/Argent")}
    assert localrepos.resolve_roots(["/a"], remotes, (OWNER, REPO)) == [
        LocalRepo(OWNER, REPO, "/a")]
    assert localrepos.resolve_roots(["/a"], remotes, ("x", "y")) == [
        LocalRepo("me", "argent", "/a")]


def test_a_remote_of_the_configured_repos_name_under_another_owner_answers_for_it():
    """A fork-only clone (origin someone/argent, no upstream) and a URL from before the
    repo moved are the configured repo's root, ahead of an upstream; so another path
    naming that repo is skipped. Twin of the ``repo roots`` smoke section."""
    remotes = {"/forkonly": _remotes(origin="git@github.com:someone/ARGENT.git"),
               "/moved": _remotes(origin="https://github.com/old-owner/argent",
                                  upstream="https://github.com/o/radon")}
    assert localrepos.resolve_roots(["/forkonly", "/moved"], remotes, (OWNER, REPO)) == [
        LocalRepo(OWNER, REPO, "/forkonly")]
    assert localrepos.resolve_roots(["/moved"], remotes, (OWNER, REPO)) == [
        LocalRepo(OWNER, REPO, "/moved")]
    assert localrepos.resolve_roots(["/forkonly"], remotes, ("x", "y")) == [
        LocalRepo("someone", "ARGENT", "/forkonly")]


def test_a_sibling_remote_naming_the_configured_repo_does_not_take_a_checkout():
    """A remote beside upstream and origin is somewhere the checkout fetches from, not
    what it is. With neither, any remote may name it. Twin of the ``repo roots`` smoke
    section."""
    sibling = (OWNER, REPO)
    remotes = {"/radon": "remote.origin.url https://github.com/o/radon\n"
                         f"remote.argent.url https://github.com/{OWNER}/{REPO}\n"
                         "remote.fork.url https://github.com/me/argent\n",
               "/argent": _remotes(origin=f"https://github.com/{OWNER}/{REPO}"),
               "/odd": f"remote.gh.url https://github.com/me/{REPO}\n"}
    assert localrepos.resolve_roots(["/radon", "/argent"], remotes, sibling) == [
        LocalRepo("o", "radon", "/radon"), LocalRepo(OWNER, REPO, "/argent")]
    assert localrepos.resolve_roots(["/odd"], remotes, sibling) == [
        LocalRepo(OWNER, REPO, "/odd")]
    # However assets/config.json cases it, the remote naming it is the root.
    assert localrepos.resolve_roots(["/argent"], remotes, (OWNER.upper(), REPO.upper())) == [
        LocalRepo(OWNER, REPO, "/argent")]


def test_every_path_resolves_only_while_each_has_a_github_remote():
    remotes = {"/a": _remotes(origin="git@github.com:o/a.git"),
               os.path.expanduser("~/b"): _remotes(origin="git@github.com:o/b.git"),
               "/plain": "", "/lab": _remotes(origin="git@gitlab.com:x/y.git")}
    assert localrepos.all_resolve(["/a", "~/b"], remotes)
    assert not localrepos.all_resolve(["/a", "/missing"], remotes)
    assert not localrepos.all_resolve(["/a", "/plain"], remotes)
    assert not localrepos.all_resolve(["/lab"], remotes)


def test_roots_skip_a_path_with_no_github_remote_and_keep_the_first_of_a_repo():
    remotes = {"/lab": _remotes(origin="git@gitlab.com:x/y.git"),
               "/b": _remotes(origin="git@github.com:software-mansion/radon.git"),
               "/a": _remotes(origin="https://github.com/Software-Mansion/radon"),
               "/c": _remotes(origin="https://github.com/o/other")}
    assert localrepos.resolve_roots(["/lab", "/missing", "/b", "/a", "/c"], remotes,
                                    (OWNER, REPO)) == [
        LocalRepo("software-mansion", "radon", "/b"), LocalRepo("o", "other", "/c")]


def test_roots_that_resolve_to_nothing_are_the_configured_repo_at_the_first_path():
    """A checkout that is missing or has no remote yet: the single root it always was."""
    assert localrepos.resolve_roots(["~/x", "/y"], {"/y": ""}, (OWNER, REPO)) == [
        LocalRepo(OWNER, REPO, os.path.expanduser("~/x"))]
    assert localrepos.resolve_roots([], {}, (OWNER, REPO)) == []


def test_repo_roots_are_kept_for_a_while_then_read_again(tmp_path, monkeypatch):
    """A root that failed to resolve (here a checkout not there yet) or whose remote
    changed is read again once :data:`review.ROOTS_TTL` passes, whatever its ``.git``
    is. A changed list of paths is read at once."""
    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    clock = [1000.0]
    monkeypatch.setattr(review, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    a = _checkout(tmp_path, "a", origin="git@github.com:software-mansion/radon.git")
    b = tmp_path / "b"
    asked: list[str] = []
    real = localrepos.remote_config
    monkeypatch.setattr(localrepos, "remote_config",
                        lambda path: asked.append(path) or real(path))
    appconfig.set_repo_roots([str(a), str(b)])

    radon = LocalRepo("software-mansion", "radon", str(a))
    assert review.repo_roots() == [radon] and review.repo_path() == str(a)
    assert not review.roots_complete()
    assert len(asked) == 2

    _checkout(tmp_path, "b", origin="git@github.com:o/other.git")
    clock[0] += review.ROOTS_TTL - 1
    assert review.repo_roots() == [radon] and len(asked) == 2, "kept for ROOTS_TTL"

    clock[0] += 1
    assert review.repo_roots() == [radon, LocalRepo("o", "other", str(b))]
    assert review.roots_complete() and len(asked) == 4

    _git(b, "remote", "set-url", "origin", "https://github.com/o/renamed")
    clock[0] += review.ROOTS_TTL
    assert review.repo_roots()[1] == LocalRepo("o", "renamed", str(b))

    appconfig.set_repo_roots([str(b)])
    assert review.repo_roots() == [LocalRepo("o", "renamed", str(b))]


def test_the_env_override_is_the_one_root(tmp_path, monkeypatch):
    appconfig.set_repo_roots([str(tmp_path / "a"), str(tmp_path / "b")])
    monkeypatch.setenv("DIPLOMAT_REPO", str(tmp_path / "env"))
    assert review.repo_root_paths() == [str(tmp_path / "env")]
    assert review.repo_roots() == [LocalRepo(OWNER, REPO, str(tmp_path / "env"))]


# ---- item keys --------------------------------------------------------------


def test_an_item_key_is_its_repo_lowercased_and_its_number():
    assert prref.item_key("Software-Mansion", "Argent", 12) == "software-mansion/argent#12"


@pytest.mark.parametrize("url, want", [
    ("https://github.com/O/R.js/pull/12", "o/r.js#12"),
    ("https://github.com/o/r/issues/12", "o/r#12"),
    ("https://github.com/o/r/pull/12/files?w=1", "o/r#12"),
    ("https://ghe.example.com/o/r/pull/12", None),
    ("https://github.com/o/r/pull/0", None),
    ("https://github.com/o/r", None),
    ("", None),
])
def test_an_item_key_from_a_link(url, want):
    assert prref.item_key_from_url(url) == want


@pytest.mark.parametrize("key, want", [
    ("o/r.js#12", ("o/r.js", 12)),
    ("o/r", None), ("#12", None), ("o/r#", None), ("o/r#x", None), ("o/r#12x", None),
    ("a/b/c#1", None), ("12", None),
])
def test_splitting_an_item_key(key, want):
    assert prref.split_item_key(key) == want


def test_a_bare_number_older_builds_stored_becomes_the_primarys_item():
    assert prref.migrate_item_key("12", "Software-Mansion/Argent") == \
        "software-mansion/argent#12"
    for unchanged in ("o/r#12", "x12", ""):
        assert prref.migrate_item_key(unchanged, "p/q") == unchanged


def test_a_queue_key_older_builds_stored_becomes_the_primarys_and_a_ts_does_not():
    assert prref.migrate_queue_key("review-req:12", "P/Q") == "review-req:p/q#12"
    for unchanged in ("investigate:1508284197000015", "review:o/r#12", "review:", "nonsense"):
        assert prref.migrate_queue_key(unchanged, "p/q") == unchanged


def test_an_item_label_names_the_repo_only_beside_another_root():
    assert prref.item_label("o/argent#12", 1) == "#12"
    assert prref.item_label("o/argent#12", 2) == "argent#12"
    assert prref.item_label("not an item", 2) == "not an item"


# ---- parsing a link against the listed repos -------------------------------


def test_a_link_to_a_listed_repo_resolves_to_its_checkout():
    ref = parse_pr_ref("https://github.com/software-mansion/radon/pull/291", OWNER, REPO,
                       also=(RADON,))
    assert ref == PRRef(291, False, RADON)
    assert ref.is_valid


def test_listed_repos_match_case_insensitively():
    assert parse_pr_ref("Software-Mansion/RADON#291", OWNER, REPO,
                        also=(RADON,)).repo == RADON


def test_the_first_listed_match_wins():
    other = LocalRepo("software-mansion", "radon", "/elsewhere")
    assert parse_pr_ref("software-mansion/radon#1", OWNER, REPO,
                        also=(RADON, other)).repo == RADON


def test_the_configured_repo_wins_over_a_listed_one_with_its_coordinates():
    shadow = LocalRepo(OWNER, REPO, "/elsewhere")
    assert parse_pr_ref(f"{OWNER}/{REPO}#5", OWNER, REPO, also=(shadow,)) == PRRef(5, False, None)


def test_a_repo_neither_configured_nor_listed_is_still_a_mismatch():
    ref = parse_pr_ref("https://github.com/other/proj/pull/5", OWNER, REPO, also=(RADON,))
    assert ref == PRRef(5, True, None) and not ref.is_valid


def test_a_bare_number_never_reaches_a_listed_repo():
    assert parse_pr_ref("#291", OWNER, REPO, also=(RADON,)) == PRRef(291, False, None)


def test_an_issue_link_resolves_the_same_way():
    ref = parse_pr_ref("https://github.com/software-mansion/radon/issues/7", OWNER, REPO,
                       kind="issues", also=(RADON,))
    assert ref == PRRef(7, False, RADON)


# ---- what the configs make of it -------------------------------------------


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(core, "config", lambda: {"owner": OWNER, "repo": REPO})


def _review(text, target=PRTarget.SPECIFIC, also=(RADON,)):
    return review.ReviewConfig(target=target, specific_pr=text, me="latekvo",
                               local_repos=also)


def test_a_listed_pr_targets_its_own_repo_and_checkout():
    cfg = _review("https://github.com/software-mansion/radon/pull/291")
    assert cfg.is_valid
    assert cfg.target_repo == ("software-mansion", "radon")
    assert cfg.checkout == "/src/radon"
    assert cfg.listed_scope == "software-mansion/radon#291"


def test_a_listed_pr_is_booked_by_url_and_never_by_number():
    """The dedup and merged-PR retirement key on bare numbers of the configured repo:
    radon#291 handed over as 291 would read as argent#291."""
    cfg = _review("https://github.com/software-mansion/radon/pull/291")
    assert cfg.single_pr_number is None
    assert cfg.single_pr_url == "https://github.com/software-mansion/radon/pull/291"


def test_the_configured_repo_keeps_its_number_and_its_root():
    cfg = _review("291")
    assert cfg.single_pr_number == 291
    assert cfg.single_pr_url == f"https://github.com/{OWNER}/{REPO}/pull/291"
    assert cfg.target_repo == (OWNER, REPO)
    assert cfg.checkout is None and cfg.listed_scope is None


def test_a_sweep_ignores_a_listed_link_left_in_the_field():
    cfg = _review("https://github.com/software-mansion/radon/pull/291", target=PRTarget.MINE)
    assert cfg.target_repo == (OWNER, REPO)
    assert cfg.checkout is None
    assert cfg.single_pr_url is None


def test_without_listed_repos_a_foreign_link_is_rejected():
    cfg = _review("https://github.com/software-mansion/radon/pull/291", also=())
    assert not cfg.is_valid and cfg.pr_ref.repo_mismatch


def test_the_payload_hands_the_core_the_listed_repos():
    payload = _review("https://github.com/software-mansion/radon/pull/291").prompt_payload()
    assert payload["localRepos"] == [{"owner": "software-mansion", "repo": "radon",
                                      "path": "/src/radon"}]
    assert "localRepos" not in _review("291", also=()).prompt_payload()


HOME_ROOT = LocalRepo("o", "other", "/src/other")


def test_a_bare_number_targets_the_home_root_and_runs_in_its_checkout():
    cfg = review.ReviewConfig(target=PRTarget.SPECIFIC, specific_pr="12", me="latekvo",
                              home=HOME_ROOT)
    assert cfg.target_repo == ("o", "other") and cfg.checkout == "/src/other"
    assert cfg.single_pr_url == "https://github.com/o/other/pull/12"
    assert cfg.for_pr(13).home == HOME_ROOT


def test_the_payload_names_the_home_root_only_when_there_is_one():
    """Absent, the core targets the configured repo, which keeps every golden prompt
    byte-identical."""
    assert "home" not in _review("291", also=()).prompt_payload()
    payload = review.ReviewConfig(target=PRTarget.SPECIFIC, specific_pr="12", me="latekvo",
                                  home=HOME_ROOT).prompt_payload()
    assert payload["home"] == {"owner": "o", "repo": "other", "path": "/src/other"}
    issue = issues.IssueConfig(target=issues.Target.SPECIFIC, specific_issue="7",
                               home=HOME_ROOT)
    assert issue.prompt_payload()["home"] == payload["home"]
    assert issue.target_repo == ("o", "other")


def test_one_review_of_a_sweep_carries_no_listed_repos():
    """A queued review names its PR by bare number, which never reaches a listed repo,
    so the list would only be stored with it."""
    one = _review("", target=PRTarget.MINE).for_pr(12)
    assert one.local_repos == () and "localRepos" not in one.prompt_payload()


def test_a_listed_conflict_targets_its_repo():
    cfg = conflicts.ConflictConfig(target=PRTarget.SPECIFIC, specific_pr="software-mansion/radon#9",
                                   local_repos=(RADON,))
    assert cfg.is_valid and cfg.target_repo == ("software-mansion", "radon")
    assert cfg.checkout == "/src/radon" and cfg.single_pr_number is None
    assert cfg.prompt_payload()["localRepos"][0]["repo"] == "radon"


def test_a_listed_issue_targets_its_repo():
    cfg = issues.IssueConfig(target=issues.Target.SPECIFIC,
                             specific_issue="https://github.com/software-mansion/radon/issues/7",
                             local_repos=(RADON,))
    assert cfg.is_valid and cfg.target_repo == ("software-mansion", "radon")
    assert cfg.checkout == "/src/radon"
    assert cfg.listed_scope == "software-mansion/radon#7"
    assert cfg.prompt_payload()["localRepos"][0]["path"] == "/src/radon"


def test_an_issue_sweep_ignores_a_listed_link_left_in_the_field():
    cfg = issues.IssueConfig(target=issues.Target.ALL,
                             specific_issue="https://github.com/software-mansion/radon/issues/7",
                             local_repos=(RADON,))
    assert cfg.target_repo == (OWNER, REPO) and cfg.checkout is None
    assert cfg.for_issue(7).local_repos == ()


# ---- the spawn -------------------------------------------------------------


def test_the_spawn_cds_into_the_checkout_it_is_given(monkeypatch):
    monkeypatch.setattr(review, "repo_path", lambda: "/root/argent")
    assert review.shell_command("/tmp/p.txt").startswith("cd /root/argent ")
    assert review.shell_command("/tmp/p.txt", checkout="/src/my radon").startswith(
        "cd '/src/my radon' ")


def test_a_listed_run_is_not_deduped_against_the_configured_repos_pr(monkeypatch):
    """radon#291 must spawn while an agent is on argent#291: the gate keys on the PR
    number, and a listed run has none."""
    from diplomat_app.store import Store

    s = Store()
    asked: list[str] = []
    monkeypatch.setattr(s, "_in_flight", lambda url: asked.append(url) or True)
    spawned: list[dict] = []
    monkeypatch.setattr(s, "_spawn_tracked",
                        lambda prompt, url, number, source, ledger_key="", **kw: spawned.append(
                            {"url": url, "number": number, **kw}))
    job = autofix.AgentJob(kind="review", audit_action="review", label="L", prompt="p",
                           pr_url="https://github.com/software-mansion/radon/pull/291",
                           checkout="/src/radon")
    assert s.dispatch_agent(job, autofix.SOURCE_PANEL) == "spawned"
    assert asked == []
    assert spawned[0]["number"] is None and spawned[0]["checkout"] == "/src/radon"
    assert spawned[0]["url"] == "https://github.com/software-mansion/radon/pull/291"

    # The configured repo's PR of the same number is still deduped.
    same = autofix.AgentJob(kind="review", audit_action="review", label="L", prompt="p",
                            pr_url=f"https://github.com/{OWNER}/{REPO}/pull/291",
                            pr_number=291)
    assert s.dispatch_agent(same, autofix.SOURCE_PANEL) == autofix.VERDICT_IN_FLIGHT


def test_a_plain_folder_inside_a_checkout_has_no_remotes(tmp_path):
    """``git -C`` would answer for the repository around it."""
    outer = _checkout(tmp_path, "outer", origin="git@github.com:o/dotfiles.git")
    (outer / "plain").mkdir()
    assert localrepos.remote_config(str(outer)) is not None
    assert localrepos.remote_config(str(outer / "plain")) is None
