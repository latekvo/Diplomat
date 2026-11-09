"""Tasks, the long-running task tracker, as the Linux front-end reaches it.

Everything Tasks decides is ``DiplomatCore``, reached through the ``diplomat-core`` CLI
(``tasks-list``, ``tasks-sync``, ``tasks-edit``, ``tasks-group``), which reads and writes
``$DIPLOMAT_TASKS_DIR`` (default ``~/.diplomat/tasks``). What lives here is calling it,
and the tunables of ``assets/tasks.json`` the front-end reads itself: the sync period
(``syncSecs``, ``minSyncSecs``), the ``chunk`` that bounds a sync's run time, and
``grouper.timeoutSecs``, which bounds the grouper's.

Stdlib-only, like the rest of the runtime.
"""

from __future__ import annotations

import json
import math
import os

from . import core, promptcore

#: Raised by :func:`views` and :func:`edit`: the CLI's own reason, or why it could not run.
Failure = (RuntimeError, promptcore.CoreBinaryMissing, ValueError)


def tunables() -> dict:
    try:
        return core.tasks()
    except core.CoreError:
        return {}


#: A day: well inside a ``QTimer`` interval's int32 milliseconds.
MAX_SYNC_SECS = 86400.0


def sync_secs() -> float:
    """``DIPLOMAT_TASKS_SECS``, else ``syncSecs``; held between ``minSyncSecs`` and a
    day."""
    t = tunables()
    default = float(t.get("syncSecs", 300))
    floor = float(t.get("minSyncSecs", 60))
    try:
        secs = float(os.environ.get("DIPLOMAT_TASKS_SECS") or default)
    except ValueError:
        secs = default
    if not math.isfinite(secs):
        secs = default
    return min(MAX_SYNC_SECS, max(floor, secs))


#: ``GH.timeout`` plus the five seconds ``GH.stop`` waits before its SIGKILL; the login
#: shell's ``lookupTimeout`` for finding ``gh``; and a margin for the rest.
_GH_CALL_SECS = 65
_GH_LOOKUP_SECS = 5
_MARGIN_SECS = 30


def sync_timeout(github_links: int) -> float:
    """``tasks-sync``'s own bounds: one ``gh`` call per ``chunk`` of the unarchived
    tasks' ``github_links``, at least one."""
    chunk = max(1, int(tunables().get("chunk", 25)))
    calls = max(1, math.ceil(github_links / chunk))
    return calls * _GH_CALL_SECS + _GH_LOOKUP_SECS + _MARGIN_SECS


def group_timeout() -> float:
    """``tasks-group``'s own bounds: its ``gh`` call, tried twice with a second between,
    then ``claude`` for ``grouper.timeoutSecs`` and the five seconds before its SIGKILL;
    finding each of the two."""
    claude = float((tunables().get("grouper") or {}).get("timeoutSecs", 300))
    return 2 * _GH_CALL_SECS + 1 + claude + 5 + 2 * _GH_LOOKUP_SECS + _MARGIN_SECS


def views(*, archived: bool = False, me: str = "") -> list[dict]:
    """``tasks-list``: ``[TaskView]`` (``{task, status, reasons, history, lastEventAt,
    linkStates, graph, work}``), not done first; archived tasks too with ``archived``.
    ``me`` is the operator's GitHub login: an ``auto`` task's PRs it wrote get
    ``findings`` work."""
    rows = json.loads(promptcore.cli(["tasks-list", *(["--archived"] if archived else []),
                                      *(["--me", me] if me else [])]))
    if not isinstance(rows, list):
        raise ValueError("diplomat-core tasks-list answered something that is not a list")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("task"), dict)]


def sync(github_links: int) -> dict:
    """``tasks-sync``: ``{"ok", "error"?, "events"}``. Never raises: a CLI that could not
    answer is an ``error``."""
    try:
        out = json.loads(promptcore.cli(["tasks-sync"], timeout=sync_timeout(github_links)))
    except Failure as exc:
        return {"ok": False, "error": str(exc), "events": 0}
    if not isinstance(out, dict):
        return {"ok": False, "events": 0,
                "error": "diplomat-core tasks-sync answered something that is not a sync"}
    return out


def group(*, if_due: bool) -> dict:
    """``tasks-group``: ``{"ok", "error"?, "created", "added"}``, or with ``if_due`` before
    it is due ``{"ok": true, "skipped", "ranAt", "lastError"?}``. Never raises: a CLI
    that could not answer is an ``error``."""
    try:
        out = json.loads(promptcore.cli(["tasks-group", *(["--if-due"] if if_due else [])],
                                        timeout=group_timeout()))
    except Failure as exc:
        return {"ok": False, "error": str(exc), "created": 0, "added": 0}
    if not isinstance(out, dict):
        return {"ok": False, "created": 0, "added": 0,
                "error": "diplomat-core tasks-group answered something that is not a run"}
    return out


def edit(op: dict) -> dict:
    """``tasks-edit``: the task as it now is. ``op`` is a ``TaskEdit`` (``{"op":
    "create" | "update" | "link" | "unlink" | "note" | "archive" | "unarchive" | "mode" |
    "dispatched", …}``)."""
    task = json.loads(promptcore.cli(["tasks-edit"], stdin=op))
    if not isinstance(task, dict):
        raise ValueError("diplomat-core tasks-edit answered something that is not a task")
    return task

