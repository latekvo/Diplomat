"""Parse a single-PR reference from a wizard text field.

Mirrors ``PRRef.swift`` verbatim: accepts a bare number (``337`` / ``#337``), a
full GitHub PR URL (``https://github.com/owner/repo/pull/337`` with any trailing
path/query), or the ``owner/repo#337`` shorthand. When the input names a repo it's
checked against the configured target repo, so a link to the wrong project is
rejected instead of silently reviewing the wrong PR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# PR numbers are ASCII digits only ([0-9], not \d): Python's \d / str.isdigit()
# also match non-ASCII digits like "٣٣٧", which Swift's Int(_:) rejects.
# github.com/OWNER/REPO/pull/N — scheme/www optional, trailing path/query allowed.
_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/([0-9]+)",
    re.IGNORECASE,
)
# OWNER/REPO#N shorthand (whole string).
_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)#([0-9]+)$")
# A bare PR number (after any leading '#' is stripped).
_BARE_NUMBER = re.compile(r"^[0-9]+$")

# Swift's ``Int`` is 64-bit, so ``Int("9223372036854775808")`` (2^63, one past
# Int64.max) overflows to nil and PRRef.swift rejects it. Python's ``int()`` is
# unbounded and would accept it, diverging the two front-ends. Clamp here so an
# over-large PR number is rejected on both (PRRef.swift parity).
_INT64_MAX = 9223372036854775807


def _int64(digits: str) -> int | None:
    """The value of an all-ASCII-digit string as Swift's ``Int(_:)`` would see it:
    the integer, or ``None`` when it overflows the 64-bit range."""
    n = int(digits)  # digits is [0-9]+, so this never raises
    return n if n <= _INT64_MAX else None


@dataclass(frozen=True)
class PRRef:
    number: int | None
    repo_mismatch: bool

    @property
    def is_valid(self) -> bool:
        """A usable reference: a number was found and any named repo matched."""
        return self.number is not None and not self.repo_mismatch

    @property
    def number_string(self) -> str:
        """The bare number for prompt injection ("" when none)."""
        return str(self.number) if self.number is not None else ""


def parse_pr_ref(raw: str, owner: str, repo: str) -> PRRef:
    """Parse ``raw`` against the expected ``owner``/``repo`` (case-insensitive)."""
    s = raw.strip()
    if not s:
        return PRRef(None, False)

    m = _URL.search(s) or _SHORTHAND.match(s)
    if m:
        o, r, n = m.group(1), m.group(2), _int64(m.group(3))
        matches = o.lower() == owner.lower() and r.lower() == repo.lower()
        return PRRef(n if (n is not None and n > 0) else None, not matches)

    bare = s[1:] if s.startswith("#") else s
    if _BARE_NUMBER.match(bare):
        n = _int64(bare)
        if n is not None and n > 0:
            return PRRef(n, False)

    return PRRef(None, False)
