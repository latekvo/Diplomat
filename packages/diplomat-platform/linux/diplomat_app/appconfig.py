"""Cross-process app settings — ``~/.diplomat/config.json``.

Nearly every setting belongs to one front-end and lives in that front-end's own
store (``QSettings`` here, ``UserDefaults`` on macOS). A few can't: the repo root
every spawn ``cd``s into, the cap on how many automatic agents may run here at
once, the three knobs of the rate-limit budget those agents are started against,
and which agent CLI a spawn runs (with the model, when that CLI is OpenCode). Each
is consumed by whichever process picks the work up, and one of those is a **mesh
node** — a separate process that is stdlib-only by design (the root README
advertises joining a mesh with "no Qt needed") and that outlives the applet, so it
can neither read a Qt/UserDefaults store nor be handed the value in its
environment at spawn time.

So those knobs live in the shared ``~/.diplomat`` tree, the way the ban list,
the activity feed and the mesh snapshot already cross process *and* front-end
boundaries. Readers re-read on use, so a change reaches a running node on its next
spawn. ``DIPLOMAT_CONFIG`` relocates the file (tests, self-checks) exactly as
``SZPONTNET_DIR`` relocates the mesh state.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

# Both stdlib-only, so the node keeps its Qt-free import graph: the gate's own
# default + range for the task cap, and the mesh's shared atomic writer rather than
# a seventh copy of tmp-file+rename (see the dedup that introduced it).
from . import autofix
from .atomicjson import read_object, write_atomic

# Keys. Kept in sync with Swift's `AppConfig` (diplomat-platform/macos/Sources/Diplomat/AppConfig.swift).
REPO_ROOT = "repoRoot"
AUTO_TASK_LIMIT = "autoTaskLimit"
AUTO_BUDGET_GATE = "autoBudgetGate"
AUTO_BUDGET_CONFIDENCE = "autoBudgetConfidence"
AUTO_BUDGET_FLOOR_PCT = "autoBudgetFloorPct"
#: Which agent CLI a spawn runs — see :mod:`runner`, which owns the values.
AGENT_RUNNER = "agentRunner"
#: The ``provider/model`` the OpenCode runner is pinned to; "" lets OpenCode pick.
#: A model id, never a credential: those stay in OpenCode's own provider store.
OPENCODE_MODEL = "opencodeModel"


def path() -> Path:
    env = os.environ.get("DIPLOMAT_CONFIG")
    if not env:
        return Path.home() / ".diplomat" / "config.json"
    try:
        return Path(env).expanduser()
    except RuntimeError:  # e.g. "~nosuchuser/..." — no home to expand; use it verbatim
        return Path(env)


def read() -> dict:
    """The whole file, or ``{}`` when it's absent, unreadable or not a JSON object —
    a truncated or hand-edited file must degrade to defaults, never break a spawn."""
    return read_object(path()) or {}


def get(key: str, default: str = "") -> str:
    value = read().get(key, default)
    return value if isinstance(value, str) else default


def get_int(key: str, default: int) -> int:
    """One integer key, or ``default`` when it's absent or isn't one.

    ``bool`` is excluded explicitly: it is a subclass of ``int`` in Python, so a
    hand-edited ``true`` would otherwise read back as the number 1 and quietly
    become a real cap of one agent."""
    value = read().get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def set_value(key: str, value: str) -> None:
    """Read-modify-write one key (empty value removes it), atomically, so a node
    reading concurrently never sees a torn file. Keys the file already holds survive a
    normal write; a file that failed to parse (see :func:`read`) is rewritten from
    defaults, so a *corrupt* file loses the other keys — each then falls back to its
    own default, which is the same degradation an absent file gets."""
    data = read()
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    write_atomic(path(), data)


def get_bool(key: str, default: bool) -> bool:
    """One boolean key, or ``default`` when it's absent or isn't one. A number is
    NOT accepted: ``1`` in a hand-edited file is as likely to be a stray count as an
    intended "on", and every consumer here has a safe default to fall back to."""
    value = read().get(key)
    return value if isinstance(value, bool) else default


def set_int(key: str, value: int) -> None:
    """Read-modify-write one integer key, atomically — :func:`set_value` for a
    number, which has no "empty means remove" spelling of its own."""
    data = read()
    data[key] = int(value)
    write_atomic(path(), data)


def set_bool(key: str, value: bool) -> None:
    """:func:`set_int` for a flag."""
    data = read()
    data[key] = bool(value)
    write_atomic(path(), data)


def set_float(key: str, value: float) -> None:
    """:func:`set_int` for a fractional number (the budget floor is a percentage
    the UI offers in half-steps)."""
    data = read()
    data[key] = float(value)
    write_atomic(path(), data)


def get_float(key: str, default: float) -> float:
    """One numeric key as a float, or ``default`` when it's absent, isn't a number,
    or is one no arithmetic can use. ``bool`` is excluded for the reason
    :func:`get_int` excludes it; an int is accepted, since 20 and 20.0 are the same
    percentage and only one of them survives a hand edit."""
    value = read().get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(value) else default


def auto_task_limit() -> int:
    """How many automatic agents this device will run at once, clamped to the range
    the Settings stepper offers.

    Resolved here rather than at each caller because there are two, in different
    processes: the applet's own dispatch gate and — for work a mesh peer routes in
    — the node's, through ``szponthost.DiplomatHost.at_job_capacity``. A cap the
    two disagree on is not a cap."""
    return autofix.clamp_auto_task_limit(
        get_int(AUTO_TASK_LIMIT, autofix.DEFAULT_AUTO_TASK_LIMIT)
    )


def auto_budget_gate() -> bool:
    """Whether automatic work is held back when the rate-limit windows are too low
    to afford it (Settings → PR AUTO-FIX). Lives here, not in a front-end's own
    store, for the reason the task cap does: a mesh node spends this machine's
    limit on work the applet never sees, and the two must not disagree about
    whether that limit is being watched."""
    return get_bool(AUTO_BUDGET_GATE, True)


def auto_budget_confidence() -> int:
    """How sure the gate must be that a task fits before it starts one, as a
    percentage, snapped to a level with a quantile behind it."""
    return autofix.clamp_budget_confidence(
        get_int(AUTO_BUDGET_CONFIDENCE, autofix.DEFAULT_BUDGET_CONFIDENCE)
    )


def auto_budget_floor_pct() -> float:
    """The share of each rate-limit window to keep in hand while the ledger is too
    thin to price a task."""
    return autofix.clamp_budget_floor_pct(
        get_float(AUTO_BUDGET_FLOOR_PCT, autofix.DEFAULT_BUDGET_FLOOR_PCT)
    )
