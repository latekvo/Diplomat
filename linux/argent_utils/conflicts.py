"""Resolve-conflicts config + prompt builder.

The prompt text (scope templates, action blocks) all comes from the shared
``core/conflicts.json``; only the *assembly* order/conditions live here as a thin
glue layer, identical to ConflictConfig's ``buildPrompt`` in ArgentUtilsCore. The
terminal spawner is shared with :mod:`review` (``review.spawn`` / ``review.resolved``).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import core
from .prref import PRRef, parse_pr_ref
from .prtarget import PRTarget

# Whose PRs we sweep — the same axis the Review wizard uses. Kept as ``Target`` here
# (and re-exported) so existing call sites stay unchanged.
Target = PRTarget


@dataclass
class ConflictConfig:
    target: Target = Target.MINE
    username: str = ""
    me: str = ""  # authenticated viewer login, used as the @handle for "mine"
    specific_pr: str = ""

    # The @handle whose PRs we sweep (empty in single-PR mode).
    @property
    def author_handle(self) -> str:
        if self.target == Target.MINE:
            return self.me or "me"
        if self.target == Target.SOMEONE:
            return self.username.strip()
        return ""

    @property
    def is_single_pr(self) -> bool:
        return self.target == Target.SPECIFIC

    @property
    def target_repo(self) -> tuple[str, str]:
        """The configured target repo (owner, repo), from the shared core config."""
        cfg = core.config()
        return cfg["owner"], cfg["repo"]

    @property
    def pr_ref(self) -> PRRef:
        """The single-PR field parsed as a number / URL / ``owner/repo#n`` shorthand,
        checked against the target repo."""
        owner, repo = self.target_repo
        return parse_pr_ref(self.specific_pr, owner, repo)

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.pr_ref.is_valid
        return bool(self.author_handle)

    def build_prompt(self) -> str:
        cfg = core.config()
        owner, repo = cfg["owner"], cfg["repo"]
        c = core.conflicts()
        s = c["scope"]
        blocks_src = c["blocks"]
        blocks: list[str] = []

        if self.is_single_pr:
            blocks.append(s["single"].format(pr=self.pr_ref.number_string, owner=owner, repo=repo))
        else:
            tmpl = s["scopeMine"] if self.target == Target.MINE else s["scopeOther"]
            scope = tmpl.format(handle=self.author_handle)
            blocks.append(s["multi"].format(scope=scope, owner=owner, repo=repo))

        lead = "Merge" if self.is_single_pr else "For each, merge"
        blocks.append(blocks_src["merge"].format(lead=lead))
        blocks.append(blocks_src["bar"])
        blocks.append(blocks_src["summary"])
        blocks.append(blocks_src["trailer"])
        return "\n\n".join(blocks)
