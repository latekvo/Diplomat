"""Resolve-conflicts config + prompt builder.

The prompt text (scope templates, action blocks) all comes from the shared
``core/conflicts.json``; only the *assembly* order/conditions live here as a thin
glue layer, identical to ConflictConfig's ``buildPrompt`` in DiplomatCore. The
terminal spawner is shared with :mod:`review` (``review.spawn`` / ``review.resolved``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .configbase import PRSweepConfig
from .prtarget import PRTarget

# Whose PRs we sweep — the same axis the Review wizard uses. Kept as ``Target`` here
# (and re-exported) so existing call sites stay unchanged.
Target = PRTarget


@dataclass
class ConflictConfig(PRSweepConfig):
    target: Target = Target.MINE
    username: str = ""
    me: str = ""  # authenticated viewer login, used as the @handle for "mine"
    specific_pr: str = ""

    # author_handle / is_single_pr / target_repo / pr_ref are inherited verbatim
    # from PRSweepConfig (shared with ReviewConfig).

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.pr_ref.is_valid
        return bool(self.author_handle)

    def build_prompt(self) -> str:
        # Single-sourced in Swift (DiplomatCore) via the diplomat-core CLI.
        from . import promptcore

        return promptcore.build_prompt({
            "kind": "conflicts",
            "target": self.target.name.lower(),
            "username": self.username,
            "me": self.me,
            "specificPR": self.specific_pr,
        })
