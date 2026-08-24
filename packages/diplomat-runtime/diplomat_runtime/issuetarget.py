"""Which of the repo's open issues a Fix-issues run works on (mirrors ``IssueTarget.swift``).

Wider than the whose-PRs axis the Review and Resolve-conflicts wizards share
(:mod:`prtarget`), because an issue's AUTHOR ASSOCIATION is a scope in its own
right: "everything the community filed" and "everything the org filed" are the two
cuts a triage sweep actually wants, and neither of them is one @handle.
"""

from __future__ import annotations

from enum import IntEnum


class IssueTarget(IntEnum):
    ALL = 0
    MINE = 1
    SOMEONE = 2
    CONTRIBUTORS = 3
    MEMBERS = 4
    SPECIFIC = 5

    @property
    def title(self) -> str:
        return {
            IssueTarget.ALL: "All open issues",
            IssueTarget.MINE: "Mine",
            IssueTarget.SOMEONE: "Someone else's",
            IssueTarget.CONTRIBUTORS: "Contributors",
            IssueTarget.MEMBERS: "Org members",
            IssueTarget.SPECIFIC: "Specific issue",
        }[self]

    @property
    def wire_name(self) -> str:
        """The spelling the ``build-prompt`` CLI reads, shared with the Swift twin's
        ``wireName`` so one vocabulary covers both front-ends."""
        return self.name.lower()

    @property
    def needs_handle(self) -> bool:
        """Whether this scope names one person, and so needs a @handle to be usable."""
        return self in (IssueTarget.MINE, IssueTarget.SOMEONE)
