"""Shared config-class behaviour for the four spawn wizards.

The Full-E2E-test (:mod:`audit`), Fix-issues (:mod:`diplomat_app.issues`),
Resolve-conflicts (:mod:`conflicts`) and Review (:mod:`review`) wizards each carry
a small ``*Config`` dataclass. A few properties were byte-identical across them:

* ``target_repo`` — all four target the shared core repo, and
* ``author_handle`` / ``is_single_pr`` / ``pr_ref`` — both the conflicts and
  review configs sweep a *whose-PRs* axis with an optional single-PR override.

Those live here once as mix-in classes the dataclasses inherit. The per-wizard
``is_valid`` and ``build_prompt`` stay in their own modules — those genuinely
differ (audit is always valid, review additionally needs a PR-state box ticked,
Fix-issues sweeps a wider axis of its own, each builds a different prompt
payload).
"""

from __future__ import annotations

from . import core
from .prref import PRRef, parse_pr_ref
from .prtarget import PRTarget


class RepoConfig:
    """A config that targets the shared core repository — by default the whole of
    it, not one pull request."""

    @property
    def target_repo(self) -> tuple[str, str]:
        """The configured target repo (owner, repo), from the shared core config."""
        cfg = core.config()
        return cfg["owner"], cfg["repo"]

    @property
    def single_pr_number(self) -> int | None:
        """The one PR this run is scoped to — the dispatch pipeline's dedup key, so
        two agents can't land on the same PR. ``None`` when the run isn't scoped to
        one (a whose-PRs sweep, a whole-repo audit); the pipeline then dedups on
        nothing, which is right, because there is nothing to collide over."""
        return None

    @property
    def single_pr_url(self) -> str | None:
        """The canonical URL of :attr:`single_pr_number`, for the sessions list's
        open-in-browser fallback. ``None`` in lockstep with it."""
        number = self.single_pr_number
        if number is None:
            return None
        owner, repo = self.target_repo
        return f"https://github.com/{owner}/{repo}/pull/{number}"


class PRSweepConfig(RepoConfig):
    """A config that sweeps one person's PRs, with an optional single-PR override.

    The inheriting dataclass supplies the ``target`` (MINE/SOMEONE/SPECIFIC),
    ``me`` (authenticated viewer login), ``username`` (the "someone" handle) and
    ``specific_pr`` fields these properties read.
    """

    # Declared for readers / type-checkers only — the concrete dataclass owns the
    # actual fields (a non-dataclass mix-in's annotations never become fields).
    target: PRTarget
    me: str
    username: str
    specific_pr: str

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
        checked against the target repo."""
        owner, repo = self.target_repo
        return parse_pr_ref(self.specific_pr, owner, repo)

    @property
    def single_pr_number(self) -> int | None:
        """The PR this run is scoped to, in single-PR mode only. A sweep across
        someone's PRs stays unscoped even when the PR field holds a leftover value."""
        return self.pr_ref.number if self.is_single_pr else None
