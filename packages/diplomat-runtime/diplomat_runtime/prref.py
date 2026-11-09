"""Parse a PR or issue reference from a wizard's single-item text field.

Mirrors ``PRRef.swift`` verbatim: accepts a bare number (``337`` / ``#337``), a
full GitHub URL (``https://github.com/owner/repo/pull/337`` with any trailing
path/query), or the ``owner/repo#337`` shorthand. When the input names a repo it's
checked against the configured target repo and then the local checkouts listed in
Settings (:mod:`localrepos`), so a link to an unknown project is rejected instead of
silently reviewing the wrong PR, and a link to a listed one says which checkout the
agent works in. A bare number always means the configured repo. ``kind`` picks the URL path
segment a pasted link must carry — a PR link and an issue link differ by that
segment alone, and accepting either everywhere would let an issue URL through the
Review wizard as a PR number.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# PR numbers are ASCII digits only ([0-9], not \d): Python's \d / str.isdigit()
# also match non-ASCII digits like "٣٣٧", which Swift's Int(_:) rejects.
# github.com/OWNER/REPO/<kind>/N — scheme/www optional, trailing path/query allowed.
_URL = {
    kind: re.compile(
        rf"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/{kind}/([0-9]+)",
        re.IGNORECASE,
    )
    for kind in ("pull", "issues")
}
# OWNER/REPO#N shorthand (whole string).
_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)#([0-9]+)$")
# A bare PR number (after any leading '#' is stripped).
_BARE_NUMBER = re.compile(r"^[0-9]+$")
_ITEM_KEY = re.compile(r"([\w.-]+/[\w.-]+)#([0-9]+)")
_DIGITS = re.compile(r"[0-9]+")

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
class LocalRepo:
    """A local checkout a link may target besides the configured repo: one GitHub
    remote of it (``owner``/``repo``) and where it is cloned (``path``)."""

    owner: str
    repo: str
    path: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def item_key(owner: str, repo: str, number: int) -> str:
    """``owner/repo#n``, lowercased: one PR or issue across every repo root. GitHub
    numbers a repo's PRs and issues in one space, so the key carries no kind."""
    return f"{owner.lower()}/{repo.lower()}#{number}"


def item_key_from_url(url: str) -> str | None:
    """:func:`item_key` of a github.com PR or issue link, or ``None`` for any other."""
    for kind in ("pull", "issues"):
        m = _URL[kind].search(url)
        if m:
            n = _int64(m.group(3))
            return item_key(m.group(1), m.group(2), n) if n else None
    return None


def split_item_key(key: str) -> tuple[str, int] | None:
    """``(slug, number)`` of an :func:`item_key`, or ``None`` when ``key`` is not one."""
    m = _ITEM_KEY.fullmatch(key)
    n = _int64(m.group(2)) if m else None
    return (m.group(1), n) if m and n else None


def migrate_item_key(key: str, primary: str) -> str:
    """A bare ``"N"`` that single-repo builds persisted, as the primary root's
    ``"<primary>#N"``; any other key unchanged."""
    return f"{primary.lower()}#{key}" if _DIGITS.fullmatch(key) else key


def migrate_queue_key(key: str, primary: str) -> str:
    """``"verb:N"`` as ``"verb:<primary>#N"``; any other key unchanged. An
    investigation is numbered by a Slack ts, not by a repo, so it is never migrated."""
    verb, sep, rest = key.partition(":")
    if not sep or verb == "investigate":
        return key
    return f"{verb}:{migrate_item_key(rest, primary)}"


def item_label(item: str, roots: int) -> str:
    """``#N`` while one repo is loaded, else ``repo#N`` by the repo's name alone."""
    split = split_item_key(item)
    if split is None:
        return item
    slug, n = split
    return f"#{n}" if roots <= 1 else f"{slug.split('/', 1)[1]}#{n}"


@dataclass(frozen=True)
class PRRef:
    number: int | None
    repo_mismatch: bool
    #: The listed checkout the input named, or ``None`` for the configured repo (which
    #: a bare number always means).
    repo: LocalRepo | None = None

    @property
    def is_valid(self) -> bool:
        """A usable reference: a number was found and any named repo matched."""
        return self.number is not None and not self.repo_mismatch

    @property
    def number_string(self) -> str:
        """The bare number for prompt injection ("" when none)."""
        return str(self.number) if self.number is not None else ""


def parse_pr_ref(raw: str, owner: str, repo: str, kind: str = "pull",
                 also: Sequence[LocalRepo] = ()) -> PRRef:
    """Parse ``raw`` against the expected ``owner``/``repo``, then ``also`` in order
    (all case-insensitive): the configured repo wins over a listed one with the same
    coordinates, and the first listed match over a later one. ``kind`` is ``"pull"``
    for a PR field or ``"issues"`` for an issue one."""
    s = raw.strip()
    if not s:
        return PRRef(None, False)

    m = _URL[kind].search(s) or _SHORTHAND.match(s)
    if m:
        o, r, n = m.group(1).lower(), m.group(2).lower(), _int64(m.group(3))
        n = n if (n is not None and n > 0) else None
        if o == owner.lower() and r == repo.lower():
            return PRRef(n, False)
        local = next((x for x in also if x.owner.lower() == o and x.repo.lower() == r), None)
        return PRRef(n, local is None, local)

    bare = s[1:] if s.startswith("#") else s
    if _BARE_NUMBER.match(bare):
        n = _int64(bare)
        if n is not None and n > 0:
            return PRRef(n, False)

    return PRRef(None, False)
