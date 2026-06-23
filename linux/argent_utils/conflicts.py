"""Resolve-conflicts config + prompt builder.

The prompt text (scope templates, action blocks) all comes from the shared
``core/conflicts.json``; only the *assembly* order/conditions live here as a thin
glue layer, identical to ConflictConfig's ``buildPrompt`` in ArgentUtilsCore. The
terminal spawner is shared with :mod:`review` (``review.spawn`` / ``review.resolved``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from . import core


class Target(IntEnum):
    """Whose PRs we sweep for merge conflicts (mirrors ConflictConfig.Target)."""

    MINE = 0
    SOMEONE = 1
    SPECIFIC = 2

    @property
    def title(self) -> str:
        return {
            Target.MINE: "Mine",
            Target.SOMEONE: "Someone else's",
            Target.SPECIFIC: "Specific PR",
        }[self]


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
    def trimmed_pr(self) -> str:
        return self.specific_pr.strip()

    @property
    def is_single_pr(self) -> bool:
        return self.target == Target.SPECIFIC

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.trimmed_pr.isdigit()
        return bool(self.author_handle)

    def build_prompt(self) -> str:
        cfg = core.config()
        owner, repo = cfg["owner"], cfg["repo"]
        c = core.conflicts()
        s = c["scope"]
        blocks_src = c["blocks"]
        blocks: list[str] = []

        if self.is_single_pr:
            blocks.append(s["single"].format(pr=self.trimmed_pr, owner=owner, repo=repo))
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
