"""The local ban list - devices that broke the foreign-accountability contract.

A **ban** is this node marking, for its operator, that a foreign device ACCEPTED
a SzpontRequest (replied ``spawned``) and then failed to deliver its result
within the completion deadline - giving no, or a non-fulfilling, answer to the
readiness reminder (docs/szpontnet/13-foreign-execution.md#accountability-
deadline-reminder-ban). The operator can also ban manually. Like the trusted
allowlist ([trust](trust.py)) it is **machine-local and never gossiped**: a ban
is each operator's own mark, keyed on the device's *verified* Ed25519
fingerprint - or, for a keyless device that has no fingerprint, its node id
(a weaker, best-effort mark; a stranger can re-mint an id, which is one more
reason keyless devices are already foreign everywhere).

Persisted at ``~/.diplomat/mesh/banned.json``; the running node keeps the list in
memory and edits it through the ``ban``/``unban`` control commands, and appends
to it itself when an automatic accountability ban fires.
"""

from __future__ import annotations

import json
import time

from . import identity
from .atomicjson import write_atomic


def banned_path():
    return identity.mesh_dir() / "banned.json"


def load() -> list[dict]:
    """The ban entries: ``[{fingerprint, node, label, reason, bannedAt, jobId}]``
    (``fingerprint`` empty for a keyless device, ``jobId`` empty for a manual
    ban). Missing/corrupt file = nobody banned."""
    try:
        raw = json.loads(banned_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    entries = raw.get("banned") if isinstance(raw, dict) else None
    # A present-but-scalar "banned" (null/int/bool) is NOT covered by .get's default
    # (that only fires on an absent key), so guard the type or `for e in null` raises
    # an uncaught TypeError that aborts node startup — corrupt file = nobody banned.
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        fp, node = str(e.get("fingerprint", "")), str(e.get("node", ""))
        if not fp and not node:
            continue  # an entry that names nobody bans nobody
        try:
            banned_at = float(e.get("bannedAt", 0.0))
        except (TypeError, ValueError, OverflowError):
            banned_at = 0.0
        out.append({
            "fingerprint": fp,
            "node": node,
            "label": str(e.get("label", "")),
            "reason": str(e.get("reason", "")),
            "bannedAt": banned_at,
            "jobId": str(e.get("jobId", "")),
        })
    return out


def save(entries: list[dict]) -> None:
    """Atomic write (tmp + rename); best-effort, never raises."""
    write_atomic(banned_path(), {"banned": entries})


def entry(fingerprint: str, node: str, label: str = "", reason: str = "",
          job_id: str = "") -> dict:
    """A fresh ban record, stamped now."""
    return {
        "fingerprint": fingerprint,
        "node": node,
        "label": label,
        "reason": reason or "manual",
        "bannedAt": time.time(),
        "jobId": job_id,
    }


def add(entries: list[dict], new: dict) -> list[dict]:
    """Entries with ``new`` added, replacing any older ban of the same device
    (the newest mark wins; no duplicate rows for one device)."""
    kept = [e for e in entries if not _same_device(e, new)]
    kept.append(new)
    return kept


def remove(entries: list[dict], fingerprint: str = "", node: str = "") -> tuple[list[dict], bool]:
    """(entries without the named device, whether anything was removed).
    Matches by fingerprint when given, else by node id."""
    probe = {"fingerprint": fingerprint, "node": node}
    kept = [e for e in entries if not _same_device(e, probe)]
    return kept, len(kept) != len(entries)


def is_banned(entries: list[dict], fingerprint: str, node_id: str = "") -> bool:
    """Whether a device is banned: its **verified** fingerprint matches an entry,
    or - only when it never proved a key (empty ``fingerprint``) - its node id
    matches a fingerprint-less entry. An id never overrides a key: a keyed device
    is judged by its key alone, so a spoofed id can't inherit someone's ban."""
    if fingerprint:
        return any(e["fingerprint"] == fingerprint for e in entries)
    return bool(node_id) and any(
        not e["fingerprint"] and e["node"] == node_id for e in entries)


def _same_device(a: dict, b: dict) -> bool:
    fa, fb = a.get("fingerprint", ""), b.get("fingerprint", "")
    if fa or fb:
        return fa == fb
    return bool(a.get("node")) and a.get("node") == b.get("node")
