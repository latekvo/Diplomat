"""Loader for the language-neutral files in ``packages/diplomat-core/assets``.

This is the single source of truth shared with the macOS app. Nothing here is
Linux- or Qt-specific — it just resolves the ``assets/`` directory and decodes
the JSON / GraphQL files into plain Python structures.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path


class CoreError(RuntimeError):
    """Raised when the shared assets can't be located or parsed."""


def _candidate_dirs() -> list[Path]:
    cands: list[Path] = []
    env = os.environ.get("DIPLOMAT_CORE")
    if env:
        cands.append(Path(env))
    # Monorepo layout: packages/diplomat-runtime/diplomat_runtime/core.py, so
    # parents[2] is packages/ and the assets are in the diplomat-core package.
    cands.append(Path(__file__).resolve().parents[2] / "diplomat-core" / "assets")
    # Fallbacks for a copy of this package living outside a checkout: the working
    # directory as the package root, then as a checkout root. Twin of the two cwd
    # candidates in ``CoreAssets.candidateDirs``.
    cands.append(Path.cwd() / "assets")
    cands.append(Path.cwd() / "packages" / "diplomat-core" / "assets")
    return cands


@functools.lru_cache(maxsize=1)
def assets_dir() -> Path:
    for d in _candidate_dirs():
        if (d / "catalog.json").is_file():
            return d
    tried = ", ".join(str(d) for d in _candidate_dirs())
    raise CoreError(f"could not locate the shared assets/ directory (tried: {tried})")


def _read_json(name: str) -> dict:
    path = assets_dir() / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoreError(f"failed to read {path}: {exc}") from exc


def read_graphql(name: str) -> str:
    """Return the contents of an assets/graphql/<name>.graphql query."""
    path = assets_dir() / "graphql" / f"{name}.graphql"
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


def issues() -> dict:
    """The Fix-issues prompt model (depth ladder, scope templates, enumeration and
    action blocks). Mirrors ``CoreAssets.Issues``; see assets/issues.json."""
    return _read_json("issues.json")


@functools.lru_cache(maxsize=1)
def mesh() -> dict:
    """The shared mesh model (protocol constants, duty catalog, strategies).

    See assets/mesh.json; consumed by the LAN P2P mesh node
    (:mod:`szpontnet`) and the topology panel.
    """
    return _read_json("mesh.json")


@functools.lru_cache(maxsize=1)
def telemetry() -> dict:
    """The Telemetry screen's model — lookback ranges, chart resolutions, the
    confidence level, and the copy for each figure.

    Mirrors ``CoreAssets.TelemetryModel``; see assets/telemetry.json. The
    arithmetic over the ledger lives in :mod:`telemetry`, not here.
    """
    return _read_json("telemetry.json")


@functools.lru_cache(maxsize=1)
def audit_categories() -> dict:
    """The shared audit/activity taxonomy (categories + action→category map).

    Mirrors diplomat-core/Sources/DiplomatCore/AuditCategory.swift; see
    assets/audit-categories.json.
    """
    return _read_json("audit-categories.json")
