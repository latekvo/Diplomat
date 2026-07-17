"""Thin wrapper around the ``gh`` CLI (mirrors the macOS GH.swift layer).

We run the binary directly with args passed literally (no shell quoting), and
rely on ``gh``'s own auth/config. GraphQL queries are loaded from the shared
``core/graphql`` assets and passed by value, with ``$owner``/``$name`` supplied
as GraphQL variables so the query text itself stays repo-agnostic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from . import core


class GHError(RuntimeError):
    """A failure from the gh shell-out layer, surfaced verbatim to the UI."""


_CANDIDATES = ["/usr/bin/gh", "/usr/local/bin/gh", "/opt/homebrew/bin/gh"]
_cached_path: str | None = None


def gh_path() -> str:
    global _cached_path
    if _cached_path:
        return _cached_path
    for c in _CANDIDATES:
        if shutil.which(c):
            _cached_path = c
            return c
    found = shutil.which("gh")
    if found:
        _cached_path = found
        return found
    raise GHError("`gh` CLI not found. Install GitHub CLI and run `gh auth login`.")


def run(args: list[str], timeout: float = 60.0) -> bytes:
    """Run gh with the given argv, returning stdout bytes (raises on failure)."""
    path = gh_path()
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GHError(f"could not execute gh: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GHError(f"gh timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise GHError(f"gh exited {proc.returncode}: {stderr or '(no stderr)'}")
    return proc.stdout


def graphql(query_name: str, *, with_repo: bool) -> dict:
    """Run a shared core/graphql query, returning the decoded JSON envelope.

    Retries once on failure — GitHub intermittently times the heavier queries
    out ("Something went wrong…"), so a single retry turns a blip into a
    non-event (mirrors API.graphqlDecoded in Models.swift).
    """
    query = core.read_graphql(query_name)
    args = ["api", "graphql", "-f", f"query={query}"]
    if with_repo:
        cfg = core.config()
        args += ["-f", f"owner={cfg['owner']}", "-f", f"name={cfg['repo']}"]

    last: Exception | None = None
    for attempt in range(2):
        try:
            data = run(args)
            env = json.loads(data)
            errs = env.get("errors")
            if errs:
                msgs = "; ".join(e.get("message", "?") for e in errs)
                raise GHError(f"GraphQL: {msgs}")
            return env
        except Exception as exc:  # noqa: BLE001 — bubble the real failure up
            last = exc
            if attempt == 0:
                time.sleep(0.8)
    raise last  # type: ignore[misc]
