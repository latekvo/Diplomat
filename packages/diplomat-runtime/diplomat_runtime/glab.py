"""The ``glab`` CLI: the GitLab twin of :mod:`gh` (``GLab.swift``).

Same contract as :mod:`gh`: literal args, glab's own per-host auth, queries from the
shared ``assets/graphql``, and every failure a :class:`gh.GHError`, so one catch covers
both hosts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import TypeVar

from . import core, gitlab
from .gh import GHError
from .prref import LocalRepo

T = TypeVar("T")
_CANDIDATES = ["/usr/bin/glab", "/usr/local/bin/glab", "/opt/homebrew/bin/glab"]
# Set only on success, so a glab installed after launch is picked up by the next call.
_cached_path: str | None = None


def glab_path() -> str:
    global _cached_path
    if _cached_path:
        return _cached_path
    found = next((c for c in _CANDIDATES if shutil.which(c)), None) or shutil.which("glab")
    if not found:
        raise GHError("`glab` CLI not found. Install GitLab CLI and run `glab auth login`.")
    _cached_path = found
    return found


def _forget(path: str) -> None:
    global _cached_path
    if _cached_path == path:
        _cached_path = None


def run(args: list[str], timeout: float = 60.0) -> bytes:
    path = glab_path()
    try:
        proc = subprocess.run([path, *args], capture_output=True, timeout=timeout,
                              check=False)
    except OSError as exc:
        _forget(path)
        raise GHError(f"could not execute glab: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GHError(f"glab timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise GHError(f"glab exited {proc.returncode}: {stderr or '(no stderr)'}")
    return proc.stdout


def graphql(query_name: str, host: str, variables: dict[str, str] | None = None,
            typed_variables: dict[str, str] | None = None) -> dict:
    """A shared query asked of ``host``'s GitLab, as :func:`gh.graphql` asks GitHub:
    ``variables`` through ``-f`` (strings), ``typed_variables`` through ``-F`` (typed),
    retried once.

    A ``null`` ``project`` raises: a project missing or hidden from this login is an
    unreadable root, never an empty one."""
    query = core.read_graphql(query_name)
    args = ["api", "--hostname", host, "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        args += ["-f", f"{k}={v}"]
    for k, v in (typed_variables or {}).items():
        args += ["-F", f"{k}={v}"]

    def ask() -> dict:
        env = json.loads(run(args))
        errs = env.get("errors")
        if errs:
            msgs = "; ".join(e.get("message", "?") for e in errs)
            raise GHError(f"GraphQL: {msgs}")
        data = env.get("data") or {}
        if "project" in data and data["project"] is None:
            path = (variables or {}).get("path", "?")
            raise GHError(f"GitLab project {path} not found on {host}")
        return env

    return _twice(ask)


def _twice(call: Callable[[], T]) -> T:
    """``call()``, retried once after a pause; the second failure is raised."""
    try:
        return call()
    except Exception:  # noqa: BLE001 — retried, and a second failure bubbles up
        time.sleep(0.8)
        return call()


def project(query_name: str, root: LocalRepo, variables: dict[str, str] | None = None,
            typed_variables: dict[str, str] | None = None) -> dict:
    """:func:`graphql` with ``root``'s project as ``$path``."""
    return graphql(query_name, root.host, {"path": root.slug, **(variables or {})},
                   typed_variables)


def members(root: LocalRepo, ids: list[int]) -> set[int]:
    """Which of user ``ids`` are members of ``root``'s project: one REST read per chunk
    (:func:`gitlab.member_paths`), each retried once, any failure raised."""
    found: set[int] = set()
    for path in gitlab.member_paths(root.slug, ids):
        found |= {m["id"] for m in _twice(lambda: json.loads(
            run(["api", "--hostname", root.host, path])))}
    return found
