"""Parse a PR or issue reference from a wizard's single-item text field.

Mirrors ``PRRef.swift`` verbatim: accepts a bare number (``337`` / ``#337``, or a
merge request's ``!337`` on a GitLab home repo), a
full GitHub URL (``https://github.com/owner/repo/pull/337`` with any trailing
path/query), a GitLab one (``https://<host>/group/sub/proj/-/merge_requests/337``), or
the ``owner/repo#337`` shorthand (owner may be a nested namespace; ``group/proj!337``
is a merge request). When the input names a repo it's
checked against the home repo (the root the wizard works on, else the configured repo)
and then the local checkouts listed in Settings (:mod:`localrepos`), so a link to an
unknown project is rejected instead of silently reviewing the wrong PR, and a link to a
listed one says which checkout the agent works in. A bare number always means the home
repo. ``kind`` picks the URL path segment a pasted link must carry — a PR link and an
issue link differ by that segment alone, and accepting either everywhere would let an
issue URL through the Review wizard as a PR number.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

GITHUB = "github.com"

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
# HOST/NAMESPACE.../PROJECT/-/<segment>/N on any host; GitLab serves issues at
# work_items too.
_GITLAB_URL = {
    kind: re.compile(
        rf"(?:https?://)?([A-Za-z0-9.-]+)(?::[0-9]+)?/([\w.-]+(?:/[\w.-]+)*)/([\w.-]+)"
        rf"/-/{segment}/([0-9]+)",
        re.IGNORECASE,
    )
    for kind, segment in (("pull", "merge_requests"), ("issues", "(?:issues|work_items)"))
}
# OWNER/REPO#N shorthand (whole string); OWNER may be nested, and a merge request may
# be OWNER/REPO!N.
_SHORTHAND = {
    "pull": re.compile(r"^([\w.-]+(?:/[\w.-]+)*)/([\w.-]+)[#!]([0-9]+)$"),
    "issues": re.compile(r"^([\w.-]+(?:/[\w.-]+)*)/([\w.-]+)#([0-9]+)$"),
}
# A bare PR number (after any leading '#', or a GitLab merge request's '!', is stripped).
_BARE_NUMBER = re.compile(r"^[0-9]+$")
_ITEM_KEY = re.compile(r"([\w.-]+(?:/[\w.-]+)+)#([0-9]+)")
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
    """A local checkout a link may target besides the home repo: one remote of it
    (``owner``/``repo`` on ``host``, lowercased) and where it is cloned (``path``). On
    GitLab ``owner`` is the whole namespace, ``/`` and all."""

    owner: str
    repo: str
    path: str
    host: str = GITHUB

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", self.host.lower())

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_gitlab(self) -> bool:
        return self.host != GITHUB

    @property
    def key(self) -> str:
        """Its identity among repo roots, lowercased: the slug on GitHub, ``host/slug``
        on GitLab, so one slug on both is two roots."""
        return (f"{self.host}/{self.slug}" if self.is_gitlab else self.slug).lower()

    def link(self, kind: str, number: int) -> str:
        """The web URL of PR (``kind`` ``"pull"``) or issue (``"issues"``) ``number``."""
        if not self.is_gitlab:
            return f"https://{GITHUB}/{self.slug}/{kind}/{number}"
        segment = "merge_requests" if kind == "pull" else "issues"
        return f"https://{self.host}/{self.slug}/-/{segment}/{number}"


def item_key(owner: str, repo: str, number: int, host: str = GITHUB) -> str:
    """One PR or issue across every repo root, its repo's :attr:`LocalRepo.key` and
    number: ``owner/repo#n`` on GitHub, ``host/owner/repo#n`` on GitLab. A host has a
    dot and a GitHub owner never does, so the two cannot clash. GitHub numbers a repo's
    PRs and issues in one space, so the key carries no kind."""
    return f"{LocalRepo(owner, repo, '', host).key}#{number}"


def root_named(name: str, roots: Sequence[LocalRepo]) -> LocalRepo | None:
    """The one of ``roots`` whose key is ``name``, else whose slug is (a name with no
    host, a GitHub root's first), any case; ``None`` for neither."""
    want = name.lower()
    return (next((r for r in roots if r.key == want), None)
            or next((r for r in roots if r.slug.lower() == want), None))


def repo_of_key(key: str) -> LocalRepo:
    """The repo a :attr:`LocalRepo.key` names, with no checkout: a GitLab project when
    its first segment has a dot (a host), else a GitHub repo."""
    host, _, rest = key.partition("/")
    if "." not in host:
        host, rest = GITHUB, key
    owner, _, repo = rest.rpartition("/")
    return LocalRepo(owner, repo, "", host)


def root_label(root: LocalRepo, roots: Sequence[LocalRepo]) -> str:
    """How ``root`` is named among ``roots`` on screen: its slug, or ``host/slug`` for
    a GitLab root whose slug another of ``roots`` has too."""
    shared = any(r.key != root.key and r.slug.lower() == root.slug.lower() for r in roots)
    return f"{root.host}/{root.slug}" if root.is_gitlab and shared else root.slug


def _link(url: str, kind: str) -> tuple[str, str, str, str] | None:
    """``(host, owner, repo, digits)`` of a ``kind`` link: github.com's spelling, else
    GitLab's on any host."""
    m = _URL[kind].search(url)
    if m:
        return GITHUB, m.group(1), m.group(2), m.group(3)
    m = _GITLAB_URL[kind].search(url)
    return (m.group(1).lower(), m.group(2), m.group(3), m.group(4)) if m else None


def item_key_from_url(url: str) -> str | None:
    """:func:`item_key` of a GitHub or GitLab PR (merge request) or issue link, or
    ``None`` for any other."""
    for kind in ("pull", "issues"):
        found = _link(url, kind)
        if found:
            n = _int64(found[3])
            return item_key(found[1], found[2], n, found[0]) if n else None
    return None


def split_item_key(key: str) -> tuple[str, int] | None:
    """``(root key, number)`` of an :func:`item_key`, or ``None`` when ``key`` is not
    one."""
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
    """``#N`` while one repo is loaded, else ``repo#N`` by the repo's name alone, no
    host or namespace."""
    split = split_item_key(item)
    if split is None:
        return item
    slug, n = split
    return f"#{n}" if roots <= 1 else f"{slug.rpartition('/')[2]}#{n}"


@dataclass(frozen=True)
class PRRef:
    number: int | None
    repo_mismatch: bool
    #: The listed checkout the input named, or ``None`` for the home repo (which a bare
    #: number always means).
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
                 also: Sequence[LocalRepo] = (), host: str = GITHUB) -> PRRef:
    """Parse ``raw`` against the expected ``owner``/``repo`` on ``host``, then ``also``
    in order (all case-insensitive): the expected repo wins over a listed one with the
    same coordinates, and the first listed match over a later one. A link names its
    host and matches a repo there only; the shorthand names none and matches any, a
    GitHub one before a GitLab one.
    ``kind`` is ``"pull"`` for a PR field or ``"issues"`` for an issue one."""
    s = raw.strip()
    if not s:
        return PRRef(None, False)

    found: tuple[str | None, str, str, str] | None = _link(s, kind)
    if found is None and (m := _SHORTHAND[kind].match(s)):
        found = (None, m.group(1), m.group(2), m.group(3))
    if found:
        at, o, r = found[0], found[1].lower(), found[2].lower()
        n = _int64(found[3])
        n = n if (n is not None and n > 0) else None

        def names(h: str, x_owner: str, x_repo: str) -> bool:
            return at in (None, h.lower()) and x_owner.lower() == o and x_repo.lower() == r

        if names(host, owner, repo):
            return PRRef(n, False)
        listed = [x for x in also if names(x.host, x.owner, x.repo)]
        local = next((x for x in listed if not x.is_gitlab), listed[0] if listed else None)
        return PRRef(n, local is None, local)

    merge_request = kind == "pull" and host.lower() != GITHUB
    bare = s[1:] if s[0] == "#" or (s[0] == "!" and merge_request) else s
    if _BARE_NUMBER.match(bare):
        n = _int64(bare)
        if n is not None and n > 0:
            return PRRef(n, False)

    return PRRef(None, False)
