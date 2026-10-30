"""Which GitHub repos the extra checkouts listed in Settings answer for.

A wizard accepts a pasted link to any of them (the ``also`` of
:func:`prref.parse_pr_ref`) and starts its agent in that checkout instead of the repo
root. A checkout's coordinates are read off its own git remotes rather than typed: a
fork clone answers for its ``upstream`` and its ``origin`` alike, so every distinct
GitHub remote is an entry of its own, all pointing at the one path.

Resolved when the list changes and once at start, never per keystroke: each path is a
``git`` call.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .prref import LocalRepo

#: Bound on the one ``git config`` read per path. It is local file I/O, so anything
#: near this is a wedged filesystem, and the caller is the GUI thread.
GIT_TIMEOUT = 5.0

#: A GitHub remote in either spelling git writes: scp-like (``git@github.com:o/r``) or
#: a URL (``https``/``http``/``ssh``/``git``, with any user and port), each with an
#: optional ``.git`` and trailing slash. Same pattern as ``LocalRepo.swift``.
_GITHUB_REMOTE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def parse_remote(url: str) -> tuple[str, str] | None:
    """``(owner, repo)`` of a GitHub remote URL, or ``None`` for any other remote."""
    m = _GITHUB_REMOTE.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def remote_urls(path: str) -> list[tuple[str, str]]:
    """``(remote name, url)`` for every remote of the checkout at ``path``, in config
    order; empty when it is no checkout, has no remotes, or ``git`` failed.

    ``--local`` because outside a repository a bare ``git config`` would answer from
    the user's global config instead of failing."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "config", "--local", "--get-regexp",
             r"^remote\..*\.url$"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    remotes = []
    for line in out.stdout.splitlines():
        key, _, url = line.partition(" ")
        remotes.append((key[len("remote."):-len(".url")], url))
    return remotes


@dataclass(frozen=True)
class Checkout:
    """One path listed in Settings, and the repos it answers for: none when it is no
    checkout or has no GitHub remote. The first is the one Settings names it by."""

    path: str
    repos: tuple[LocalRepo, ...]

    @property
    def slug(self) -> str | None:
        return f"{self.repos[0].owner}/{self.repos[0].repo}" if self.repos else None


def inspect(path: str) -> Checkout:
    """``path`` (``~`` expanded) with its GitHub remotes, ``upstream`` first, then
    ``origin``, then the rest in config order; one entry per distinct owner/repo."""
    where = os.path.expanduser(path)
    remotes = remote_urls(where)
    ranked = sorted(range(len(remotes)),
                    key=lambda i: (remotes[i][0] != "upstream", remotes[i][0] != "origin", i))
    repos: list[LocalRepo] = []
    seen: set[tuple[str, str]] = set()
    for i in ranked:
        coords = parse_remote(remotes[i][1])
        if coords is None or (coords[0].lower(), coords[1].lower()) in seen:
            continue
        seen.add((coords[0].lower(), coords[1].lower()))
        repos.append(LocalRepo(coords[0], coords[1], where))
    return Checkout(path, tuple(repos))


def resolve(paths: list[str]) -> list[Checkout]:
    return [inspect(p) for p in paths]


def local_repos(checkouts: list[Checkout]) -> tuple[LocalRepo, ...]:
    """Every repo the listed checkouts answer for, in the order they are listed: the
    first checkout to answer for a repo is the one its links resolve to."""
    return tuple(r for c in checkouts for r in c.repos)
