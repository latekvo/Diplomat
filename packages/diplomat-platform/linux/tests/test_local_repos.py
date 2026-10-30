"""The extra checkouts listed in Settings, and the links that resolve into them.

Three layers, each pinned where it is decided: :mod:`localrepos` reads a checkout's
coordinates off its real git remotes, :func:`prref.parse_pr_ref` resolves a pasted link
against them, and the configs turn that into the repo the prompt names, the directory
the agent starts in, and a run the dispatch gate must not confuse with the configured
repo's PR of the same number.
"""

from __future__ import annotations

import subprocess

import pytest

from diplomat_runtime import autofix, core, localrepos, review
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
