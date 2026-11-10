"""Thin wrapper around the ``glab`` CLI: the GitLab twin of :mod:`gh` (GLab.swift).

Same contract as :mod:`gh`: the binary runs with args passed literally and its own
per-host auth, queries come from the shared ``assets/graphql`` assets, and every
failure is a :class:`gh.GHError`, so a caller's existing catch covers both hosts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from . import core
from .gh import GHError
from .prref import LocalRepo

_CANDIDATES = ["/opt/homebrew/bin/glab", "/usr/local/bin/glab", "/usr/bin/glab"]
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
    """Run glab with the given argv, returning stdout bytes (raises on failure)."""
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
    """Run a shared ``assets/graphql`` query against ``host``'s GitLab, returning the
    decoded JSON envelope. ``variables`` go through ``-f`` (strings), ``typed_variables``
    through ``-F`` (parsed into the declared type), as in :func:`gh.graphql`.

    A query asking for ``project(fullPath: $path)`` that answers ``null`` raises: a
    project missing or hidden from this login is an unreadable root, never one with
    nothing open. Retries once on failure, like :func:`gh.graphql`."""
    query = core.read_graphql(query_name)
    args = ["api", "--hostname", host, "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        args += ["-f", f"{k}={v}"]
    for k, v in (typed_variables or {}).items():
        args += ["-F", f"{k}={v}"]

    last: Exception | None = None
    for attempt in range(2):
        try:
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
        except Exception as exc:  # noqa: BLE001 — bubble the real failure up
            last = exc
            if attempt == 0:
                time.sleep(0.8)
    raise last  # type: ignore[misc]


def project(query_name: str, root: LocalRepo, variables: dict[str, str] | None = None,
            typed_variables: dict[str, str] | None = None) -> dict:
    """:func:`graphql` of a query about ``root``'s project, which is its ``$path``."""
    return graphql(query_name, root.host, {"path": root.slug, **(variables or {})},
                   typed_variables)
