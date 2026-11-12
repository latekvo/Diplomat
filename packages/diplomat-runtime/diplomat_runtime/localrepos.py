"""Which GitHub repos and GitLab projects the extra checkouts listed in Settings answer
for.

A wizard accepts a pasted link to any of them (the ``also`` of
:func:`prref.parse_pr_ref`) and starts its agent in that checkout instead of the repo
root. A checkout's coordinates are read off its own git remotes rather than typed: a
fork clone answers for its ``upstream`` and its ``origin`` alike, so every distinct
GitHub or GitLab remote is an entry of its own, all pointing at the one path.

Resolved when the list changes and once at start, never per keystroke: each path is a
``git`` call.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .prref import GITHUB, LocalRepo

#: Bound on the one ``git config`` read per path. It is local file I/O, so anything
#: near this is a wedged filesystem, and the caller is the GUI thread.
GIT_TIMEOUT = 5.0

#: The GitLab hosts known without asking glab. Any host whose first DNS label is
#: ``gitlab`` counts as well (:func:`is_gitlab_host`).
GITLAB_HOSTS = frozenset({"gitlab.com"})

#: A GitHub remote in either spelling git writes: scp-like (``git@github.com:o/r``) or
#: a URL (``https``/``http``/``ssh``/``git``, with any user and port), each with an
#: optional ``.git`` and trailing slash. Same pattern as ``LocalRepo.swift``.
_GITHUB_REMOTE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?github\.com(?::[0-9]+)?/|[^@/:]+@github\.com:)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
#: The same spellings on any host, with a namespace of one or more segments: a GitLab
#: remote once its host is one (:func:`is_gitlab_host`).
_HOSTED_REMOTE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?([A-Za-z0-9.-]+)(?::[0-9]+)?/"
    r"|[^@/:]+@([A-Za-z0-9.-]+):)"
    r"([\w.-]+(?:/[\w.-]+)*)/([\w.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
#: A host key of glab's ``hosts:`` map (any port is dropped: a remote names none).
_GLAB_HOST = re.compile(r"^\s+([A-Za-z0-9.-]+)(?::[0-9]+)?:\s*$")


def glab_config_file() -> Path | None:
    """glab's config, the first that exists of ``$GLAB_CONFIG_DIR/config.yml``,
    ``$XDG_CONFIG_HOME/glab-cli/config.yml`` (``~/.config`` by default) and, on macOS,
    ``~/Library/Application Support/glab-cli/config.yml``."""
    candidates = []
    if os.environ.get("GLAB_CONFIG_DIR"):
        candidates.append(Path(os.environ["GLAB_CONFIG_DIR"]) / "config.yml")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates.append(Path(xdg) / "glab-cli" / "config.yml")
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "glab-cli"
                          / "config.yml")
    return next((c for c in candidates if c.is_file()), None)


def config_hosts(text: str) -> set[str]:
    """The keys of the column-0 ``hosts:`` map of a glab config, lowercased. Read
    without a YAML parser: every line at the indent of the map's first entry that is a
    bare ``host:`` key, up to the next column-0 key."""
    hosts: set[str] = set()
    inside = False
    indent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            if inside:
                break
            inside = line.rstrip() == "hosts:"
            continue
        if not inside:
            continue
        lead = len(line) - len(line.lstrip())
        indent = lead if indent is None else indent
        if lead == indent and (m := _GLAB_HOST.match(line)):
            hosts.add(m.group(1).lower())
    return hosts


def gitlab_hosts() -> frozenset[str]:
    """:data:`GITLAB_HOSTS` and every host glab is configured for
    (:func:`glab_config_file`); a missing or unreadable config adds none."""
    path = glab_config_file()
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except (OSError, UnicodeDecodeError):
        text = ""
    return GITLAB_HOSTS | config_hosts(text)


def is_gitlab_host(host: str, hosts: frozenset[str] = GITLAB_HOSTS) -> bool:
    """Whether ``host`` is a GitLab: one of ``hosts``, or any of two or more non-empty
    DNS labels whose first is ``gitlab``."""
    host = host.lower()
    labels = host.split(".")
    return host != GITHUB and (host in hosts or len(labels) >= 2 and labels[0] == "gitlab"
                               and all(labels))


def parse_remote(url: str, hosts: frozenset[str] = GITLAB_HOSTS
                 ) -> tuple[str, str, str] | None:
    """``(host, owner, repo)`` of a GitHub remote URL, or of a GitLab one on a host
    :func:`is_gitlab_host` takes (``owner`` its whole namespace), else ``None``. The
    host is lowercased."""
    url = url.strip()
    m = _GITHUB_REMOTE.match(url)
    if m:
        return GITHUB, m.group(1), m.group(2)
    m = _HOSTED_REMOTE.match(url)
    if m is None:
        return None
    host = (m.group(1) or m.group(2)).lower()
    return (host, m.group(3), m.group(4)) if is_gitlab_host(host, hosts) else None


def remote_config(path: str) -> str | None:
    """``git config --local --get-regexp '^remote\\..*\\.url$'`` of the checkout at
    ``path``, or ``None`` when it is no checkout or ``git`` failed. A checkout with no
    remotes answers ``None`` too: ``git config --get-regexp`` exits 1 on no match.

    ``--local`` because outside a repository a bare ``git config`` would answer from
    the user's global config instead of failing; and no ``.git`` here is no checkout,
    though ``git -C`` would answer for a repository the folder sits inside."""
    if not os.path.exists(os.path.join(os.path.expanduser(path), ".git")):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", path, "config", "--local", "--get-regexp",
             r"^remote\..*\.url$"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def entries(path: str, remotes: str, only: tuple[str, ...] = (),
            hosts: frozenset[str] = GITLAB_HOSTS) -> list[LocalRepo]:
    """Every GitHub or GitLab remote in ``remotes`` (:func:`remote_config` output), or
    every one named in ``only``, as a repo cloned at ``path``, each once: ``upstream``
    first, then ``origin``, then the rest in config order. Twin of
    ``LocalRepo.entries(path:remotes:only:)``."""
    named: list[tuple[str, LocalRepo]] = []
    for line in remotes.split("\n"):
        key, sep, url = line.partition(" ")
        coords = parse_remote(url, hosts) if sep else None
        if coords is None or not key.startswith("remote.") or not key.endswith(".url"):
            continue
        if only and key[len("remote."):-len(".url")] not in only:
            continue
        named.append((key[len("remote."):-len(".url")],
                      LocalRepo(coords[1], coords[2], path, coords[0])))
    ranked = sorted(range(len(named)),
                    key=lambda i: (named[i][0] != "upstream", named[i][0] != "origin", i))
    out: list[LocalRepo] = []
    seen: set[str] = set()
    for i in ranked:
        repo = named[i][1]
        if _identity(repo) not in seen:
            seen.add(_identity(repo))
            out.append(repo)
    return out


def _identity(repo: LocalRepo) -> str:
    return f"{repo.host}/{repo.slug}".lower()


@dataclass(frozen=True)
class Checkout:
    """One path listed in Settings, and the repos it answers for: none when it is no
    checkout or has no GitHub or GitLab remote."""

    path: str
    repos: tuple[LocalRepo, ...]

    @property
    def slug(self) -> str | None:
        """What Settings names it by: its first GitHub repo, as :func:`resolve_roots`
        ranks a GitHub remote over a GitLab one, else its first."""
        pick = next((r for r in self.repos if not r.is_gitlab), None) or next(
            iter(self.repos), None)
        return pick.slug if pick else None


def inspect(path: str, hosts: frozenset[str] = GITLAB_HOSTS) -> Checkout:
    """``path`` (``~`` expanded) with its GitHub and GitLab remotes (:func:`entries`)."""
    where = os.path.expanduser(path)
    return Checkout(path, tuple(entries(where, remote_config(where) or "", hosts=hosts)))


def resolve(paths: list[str]) -> list[Checkout]:
    hosts = gitlab_hosts()
    return [inspect(p, hosts) for p in paths]


def local_repos(checkouts: list[Checkout]) -> tuple[LocalRepo, ...]:
    """Every repo the listed checkouts answer for, in the order they are listed: the
    first checkout to answer for a repo is the one its links resolve to."""
    return tuple(r for c in checkouts for r in c.repos)


def resolve_roots(paths: list[str], remotes: dict[str, str],
                  configured: tuple[str, str],
                  hosts: frozenset[str] = GITLAB_HOSTS) -> list[LocalRepo]:
    """The repo roots ``paths`` name, primary first. Twin of ``RepoRoots.resolve``.

    ``remotes`` maps an expanded path to its :func:`remote_config` output; a path
    missing from it is no checkout. A path with a GitHub remote answers for a GitHub
    repo: the ``configured`` one when its GitHub ``upstream`` or ``origin`` (any GitHub
    remote, with neither) is that repo, or a repo of that name under another owner (a
    fork's ``origin``, or a URL from before the repo moved); else its first GitHub
    remote (:func:`entries`). Another remote naming it is a sibling fetched from, not
    what the checkout is. Only a path with no GitHub remote answers for a GitLab project:
    its first GitLab remote, which is only ever itself. A path with no GitHub or GitLab
    remote is skipped, and of two answering for one repo the first wins. When nothing
    resolves, the first path stands for the configured repo, so a missing checkout reads
    as the single root it always did (:func:`guessed` tells the case of several)."""
    want = f"{configured[0]}/{configured[1]}".lower()
    roots: list[LocalRepo] = []
    seen: set[str] = set()
    for raw in paths:
        where = os.path.expanduser(raw)
        if where not in remotes:
            continue
        found = entries(where, remotes[where], hosts=hosts)
        hub = [r for r in found if not r.is_gitlab]
        own = [r for r in entries(where, remotes[where], ("upstream", "origin"), hosts)
               if not r.is_gitlab] or hub
        pick = next((r for r in own if r.slug.lower() == want), None)
        if pick is None and any(r.repo.lower() == configured[1].lower() for r in own):
            pick = LocalRepo(configured[0], configured[1], where)
        if pick is None and found:
            pick = (hub or found)[0]
        if pick is None or _identity(pick) in seen:
            continue
        seen.add(_identity(pick))
        roots.append(pick)
    if not roots and paths:
        return [LocalRepo(configured[0], configured[1], os.path.expanduser(paths[0]))]
    return roots


def all_resolve(paths: list[str], remotes: dict[str, str],
                hosts: frozenset[str] = GITLAB_HOSTS) -> bool:
    """Whether every one of ``paths`` has a GitHub or GitLab remote in ``remotes``, as
    :func:`resolve_roots` reads them: False while one is missing, ``git`` failed on it,
    or it has neither. Twin of ``RepoRoots.allResolve``."""
    return all(entries(w, remotes.get(w, ""), hosts=hosts)
               for w in map(os.path.expanduser, paths))


def primary_resolves(paths: list[str], remotes: dict[str, str],
                     hosts: frozenset[str] = GITLAB_HOSTS) -> bool:
    """Whether :func:`resolve_roots`' first root is the first path's own repo: always
    with one path (the fallback is the single root it always was), else only while the
    first path has a GitHub or GitLab remote. Without one the next path's repo stands
    first, so "the primary" names another repo. Twin of ``RepoRoots.primaryResolves``."""
    if len(paths) < 2:
        return True
    first = os.path.expanduser(paths[0])
    return bool(entries(first, remotes.get(first, ""), hosts=hosts))


def guessed(paths: list[str], remotes: dict[str, str],
            hosts: frozenset[str] = GITLAB_HOSTS) -> bool:
    """Whether :func:`resolve_roots` fell back on several paths none of which resolved:
    its one root is then a guess that the first path checks out the configured repo,
    possibly a checkout of another. Twin of ``RepoRoots.guessed``."""
    return len(paths) > 1 and not any(entries(w, remotes.get(w, ""), hosts=hosts)
                                      for w in map(os.path.expanduser, paths))
