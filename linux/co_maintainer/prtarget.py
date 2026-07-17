"""Whose PRs a wizard acts on (mirrors ``PRTarget.swift``).

My own, another user's, or one specific PR by number/URL. Shared by the Review and
Resolve-conflicts wizards.
"""

from __future__ import annotations

from enum import IntEnum


class PRTarget(IntEnum):
    MINE = 0
    SOMEONE = 1
    SPECIFIC = 2

    @property
    def title(self) -> str:
        return {
            PRTarget.MINE: "Mine",
            PRTarget.SOMEONE: "Someone else's",
            PRTarget.SPECIFIC: "Specific PR",
        }[self]
