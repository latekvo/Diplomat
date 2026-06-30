"""Loader for the shared, language-neutral ``core/`` assets.

This is the single source of truth shared with the macOS app. Nothing here is
Linux- or Qt-specific — it just resolves the ``core/`` directory and decodes the
JSON / GraphQL files into plain Python structures.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path


class CoreError(RuntimeError):
    """Raised when the shared core/ assets can't be located or parsed."""


def _candidate_dirs() -> list[Path]:
    cands: list[Path] = []
    env = os.environ.get("ARGENT_UTILS_CORE")
    if env:
        cands.append(Path(env))
    # Repo layout: <repo>/linux/argent_utils/core.py -> <repo>/core
    cands.append(Path(__file__).resolve().parents[2] / "core")
    # Fallback: a core/ next to the current working directory (e.g. `swift run` cwd).
    cands.append(Path.cwd() / "core")
    return cands


@functools.lru_cache(maxsize=1)
def core_dir() -> Path:
    for d in _candidate_dirs():
        if (d / "catalog.json").is_file():
            return d
    tried = ", ".join(str(d) for d in _candidate_dirs())
    raise CoreError(f"could not locate shared core/ assets (tried: {tried})")


def _read_json(name: str) -> dict:
    path = core_dir() / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoreError(f"failed to read {path}: {exc}") from exc


def read_graphql(name: str) -> str:
    """Return the contents of a core/graphql/<name>.graphql query."""
    path = core_dir() / "graphql" / f"{name}.graphql"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoreError(f"failed to read {path}: {exc}") from exc


@functools.lru_cache(maxsize=1)
def config() -> dict:
    return _read_json("config.json")


@functools.lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return _read_json("catalog.json")["tools"]


@functools.lru_cache(maxsize=1)
def filters() -> dict:
    return _read_json("filters.json")


@functools.lru_cache(maxsize=1)
def review() -> dict:
    return _read_json("review.json")


@functools.lru_cache(maxsize=1)
def conflicts() -> dict:
    return _read_json("conflicts.json")


@functools.lru_cache(maxsize=1)
def audit() -> dict:
    return _read_json("audit.json")
