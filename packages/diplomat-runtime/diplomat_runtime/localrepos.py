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


def remote_config(path: str) -> str | None:
    """``git config --local --get-regexp '^remote\\..*\\.url$'`` of the checkout at
    ``path``, or ``None`` when it is no checkout or ``git`` failed. A checkout with no
    remotes answers ``None`` too: ``git config --get-regexp`` exits 1 on no match.

    ``--local`` because outside a repository a bare ``git config`` would answer from
    the user's global config instead of failing."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "config", "--local", "--get-regexp",
             r"^remote\..*\.url$"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def entries(path: str, remotes: str, only: tuple[str, ...] = ()) -> list[LocalRepo]:
    """Every GitHub remote in ``remotes`` (:func:`remote_config` output), or every one
    named in ``only``, as a repo cloned at ``path``, each once: ``upstream`` first, then
    ``origin``, then the rest in config order. Twin of
    ``LocalRepo.entries(path:remotes:only:)``."""
    named: list[tuple[str, LocalRepo]] = []
    for line in remotes.split("\n"):
        key, sep, url = line.partition(" ")
        coords = parse_remote(url) if sep else None
        if coords is None or not key.startswith("remote.") or not key.endswith(".url"):
            continue
        if only and key[len("remote."):-len(".url")] not in only:
            continue
        named.append((key[len("remote."):-len(".url")],
                      LocalRepo(coords[0], coords[1], path)))
    ranked = sorted(range(len(named)),
                    key=lambda i: (named[i][0] != "upstream", named[i][0] != "origin", i))
    out: list[LocalRepo] = []
    seen: set[str] = set()
    for i in ranked:
        repo = named[i][1]
        if repo.slug.lower() not in seen:
            seen.add(repo.slug.lower())
            out.append(repo)
    return out


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
    """``path`` (``~`` expanded) with its GitHub remotes (:func:`entries`)."""
    where = os.path.expanduser(path)
    return Checkout(path, tuple(entries(where, remote_config(where) or "")))


def resolve(paths: list[str]) -> list[Checkout]:
    return [inspect(p) for p in paths]


def local_repos(checkouts: list[Checkout]) -> tuple[LocalRepo, ...]:
    """Every repo the listed checkouts answer for, in the order they are listed: the
    first checkout to answer for a repo is the one its links resolve to."""
    return tuple(r for c in checkouts for r in c.repos)


def resolve_roots(paths: list[str], remotes: dict[str, str],
                  configured: tuple[str, str]) -> list[LocalRepo]:
    """The repo roots ``paths`` name, primary first. Twin of ``RepoRoots.resolve``.

    ``remotes`` maps an expanded path to its :func:`remote_config` output; a path
    missing from it is no checkout. Each path answers for the ``configured`` repo when
    its ``upstream`` or ``origin`` (any remote, with neither) is that repo, or a repo of
    that name under another owner (a fork's ``origin``, or a URL from before the repo
    moved); else for its first (:func:`entries`). Another remote naming it is a sibling
    fetched from, not what the checkout is. A path with no GitHub remote is skipped, and
    of two answering for one repo the first wins. When nothing resolves, the first path
    stands for the configured repo, so a missing checkout reads as the single root it
    always did (:func:`guessed` tells the case of several)."""
    want = f"{configured[0]}/{configured[1]}".lower()
    roots: list[LocalRepo] = []
    seen: set[str] = set()
    for raw in paths:
        where = os.path.expanduser(raw)
        if where not in remotes:
            continue
        found = entries(where, remotes[where])
        own = entries(where, remotes[where], ("upstream", "origin")) or found
        pick = next((r for r in own if r.slug.lower() == want), None)
        if pick is None and any(r.repo.lower() == configured[1].lower() for r in own):
            pick = LocalRepo(configured[0], configured[1], where)
        if pick is None and found:
            pick = found[0]
        if pick is None or pick.slug.lower() in seen:
            continue
        seen.add(pick.slug.lower())
        roots.append(pick)
    if not roots and paths:
        return [LocalRepo(configured[0], configured[1], os.path.expanduser(paths[0]))]
    return roots


def all_resolve(paths: list[str], remotes: dict[str, str]) -> bool:
    """Whether every one of ``paths`` has a GitHub remote in ``remotes``, as
    :func:`resolve_roots` reads them: False while one is missing, ``git`` failed on it,
    or it has no GitHub remote. Twin of ``RepoRoots.allResolve``."""
    return all(entries(w, remotes.get(w, "")) for w in map(os.path.expanduser, paths))


def primary_resolves(paths: list[str], remotes: dict[str, str]) -> bool:
    """Whether :func:`resolve_roots`' first root is the first path's own repo: always
    with one path (the fallback is the single root it always was), else only while the
    first path has a GitHub remote. Without one the next path's repo stands first, so
    "the primary" names another repo. Twin of ``RepoRoots.primaryResolves``."""
    if len(paths) < 2:
        return True
    first = os.path.expanduser(paths[0])
    return bool(entries(first, remotes.get(first, "")))


def guessed(paths: list[str], remotes: dict[str, str]) -> bool:
    """Whether :func:`resolve_roots` fell back on several paths none of which resolved:
    its one root is then a guess that the first path checks out the configured repo,
    possibly a checkout of another. Twin of ``RepoRoots.guessed``."""
    return len(paths) > 1 and not any(entries(w, remotes.get(w, ""))
                                      for w in map(os.path.expanduser, paths))
