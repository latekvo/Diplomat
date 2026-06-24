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

# github.com/OWNER/REPO/pull/N — scheme/www optional, trailing path/query allowed.
_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
# OWNER/REPO#N shorthand (whole string).
_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


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
        o, r, n = m.group(1), m.group(2), int(m.group(3))
        matches = o.lower() == owner.lower() and r.lower() == repo.lower()
        return PRRef(n if n > 0 else None, not matches)

    bare = s[1:] if s.startswith("#") else s
    if bare.isdigit() and int(bare) > 0:
        return PRRef(int(bare), False)

    return PRRef(None, False)
