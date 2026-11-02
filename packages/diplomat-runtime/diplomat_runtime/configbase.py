"""Shared config-class behaviour for the four spawn wizards.

The Full-E2E-test (:mod:`audit`), Fix-issues (:mod:`diplomat_app.issues`),
Resolve-conflicts (:mod:`conflicts`) and Review (:mod:`review`) wizards each carry
a small ``*Config`` dataclass. A few properties were byte-identical across them:

* ``target_repo`` / ``checkout`` — all four target the shared core repo, except a
  single-item run whose pasted link named one of the checkouts listed in Settings
  (:mod:`localrepos`), which targets that repo and works in that checkout, and
* ``author_handle`` / ``is_single_pr`` / ``pr_ref`` — both the conflicts and
  review configs sweep a *whose-PRs* axis with an optional single-PR override.

Those live here once as mix-in classes the dataclasses inherit. The per-wizard
``is_valid`` and ``build_prompt`` stay in their own modules — those genuinely
differ (audit is always valid, review additionally needs a PR-state box ticked,
Fix-issues sweeps a wider axis of its own, each builds a different prompt
payload).
"""

from __future__ import annotations

from collections.abc import Sequence

from . import core
from .prref import LocalRepo, PRRef, parse_pr_ref
from .prtarget import PRTarget


class RepoConfig:
    """A config that targets the shared core repository — by default the whole of
    it, not one pull request."""

    @property
    def configured_repo(self) -> tuple[str, str]:
        """The configured target repo (owner, repo), from the shared core config."""
        cfg = core.config()
        return cfg["owner"], cfg["repo"]

    @property
    def single_ref(self) -> PRRef | None:
        """The single-item field parsed, while the run is scoped to that one item;
        ``None`` for a sweep or a whole-repo run, whose field is never read."""
        return None

    @property
    def listed_repo(self) -> LocalRepo | None:
        """The listed checkout a single-item link resolved to, or ``None``."""
        ref = self.single_ref
        return ref.repo if ref is not None else None

    @property
    def target_repo(self) -> tuple[str, str]:
        """The repo this run works on (owner, repo): :attr:`listed_repo`'s, else the
        configured one. What the prompt names, ``gh`` asks about, and links point at."""
        local = self.listed_repo
        return (local.owner, local.repo) if local else self.configured_repo

    @property
    def checkout(self) -> str | None:
        """Where the agent works when that is not the repo root
        (:func:`review.repo_path`): :attr:`listed_repo`'s path, or ``None``."""
        local = self.listed_repo
        return local.path if local else None

    @property
    def listed_scope(self) -> str | None:
        """``owner/repo#N`` for a run in a listed checkout, which a bare ``#N`` would
        misname as the configured repo's; ``None`` otherwise."""
        ref = self.single_ref
        if ref is None or ref.repo is None:
            return None
        return f"{ref.repo.owner}/{ref.repo.repo}#{ref.number_string}"

    @property
    def single_pr_number(self) -> int | None:
        """The one PR this run is scoped to — the dispatch pipeline's dedup key, so
        two agents can't land on the same PR. ``None`` when the run isn't scoped to
        one (a whose-PRs sweep, a whole-repo audit); the pipeline then dedups on
        nothing, which is right, because there is nothing to collide over."""
        return None

    @property
    def single_pr_url(self) -> str | None:
        """The canonical URL of the one PR this run is scoped to, for the sessions
        list's open-in-browser fallback; ``None`` when it isn't scoped to one."""
        return None


def listed_payload(local_repos: Sequence[LocalRepo]) -> dict:
    """The listed checkouts as the ``localRepos`` of a prompt payload, or nothing when
    there are none, so a payload stored before the list existed reads the same."""
    if not local_repos:
        return {}
    return {"localRepos": [{"owner": r.owner, "repo": r.repo, "path": r.path}
                           for r in local_repos]}


class PRSweepConfig(RepoConfig):
    """A config that sweeps one person's PRs, with an optional single-PR override.

    The inheriting dataclass supplies the ``target`` (MINE/SOMEONE/SPECIFIC),
    ``me`` (authenticated viewer login), ``username`` (the "someone" handle),
    ``specific_pr`` and ``local_repos`` fields these properties read.
    """

    # Declared for readers / type-checkers only — the concrete dataclass owns the
    # actual fields (a non-dataclass mix-in's annotations never become fields).
    target: PRTarget
    me: str
    username: str
    specific_pr: str
    local_repos: tuple[LocalRepo, ...]

    @property
    def author_handle(self) -> str:
        """The @handle whose PRs we sweep (empty in single-PR mode)."""
        if self.target == PRTarget.MINE:
            return self.me or "me"
        if self.target == PRTarget.SOMEONE:
            return self.username.strip()
        return ""

    @property
    def is_single_pr(self) -> bool:
        return self.target == PRTarget.SPECIFIC

    @property
    def pr_ref(self) -> PRRef:
        """The single-PR field parsed as a number / URL / ``owner/repo#n`` shorthand,
        checked against the configured repo, then ``local_repos``."""
        owner, repo = self.configured_repo
        return parse_pr_ref(self.specific_pr, owner, repo, also=self.local_repos)

    @property
    def single_ref(self) -> PRRef | None:
        return self.pr_ref if self.is_single_pr else None

    @property
    def single_pr_number(self) -> int | None:
        """The PR this run is scoped to, in single-PR mode only. A sweep across
        someone's PRs stays unscoped even when the PR field holds a leftover value.

        ``None`` for a PR in a listed checkout too: the dedup and the merged-PR
        retirement key on bare numbers of the configured repo, where ``radon#291``
        would read as ``argent#291``. Its :attr:`single_pr_url` still names it."""
        ref = self.single_ref
        return ref.number if ref is not None and ref.repo is None else None

    @property
    def single_pr_url(self) -> str | None:
        ref = self.single_ref
        if ref is None or ref.number is None:
            return None
        owner, repo = self.target_repo
        return f"https://github.com/{owner}/{repo}/pull/{ref.number}"
