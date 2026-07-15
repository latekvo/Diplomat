"""The local trusted-device allowlist - who counts as *personal*.

Trust is set **manually by the operator** and lives **only on this machine**; it
is never gossiped and never derived from anything a peer advertises. The store
is a set of Ed25519 **fingerprints** ([crypto](crypto.py)) the operator has
explicitly marked as their own devices. A peer is *personal* only if the
fingerprint it **proved possession of** on the link (its verified key, not a
claimed one) is in this set; everything else is *foreign*.

Backward-compatible opt-in: an **empty** allowlist means the trust boundary is
not configured, so every peer is *personal* - identical to the pre-trust
full-altruism mesh. The moment the operator trusts even one device, the boundary
switches on and unlisted (or unverified) peers become *foreign* and have their
requests declined. So enabling zero-trust is a deliberate act, and a fresh mesh
keeps working exactly as before.

Persisted at ``~/.argent/mesh/trusted.json``; the running node keeps the set in
memory and edits it through a control command so ``--trust`` takes effect live.
"""

from __future__ import annotations

import json
import os

from . import identity


def trusted_path():
    return identity.mesh_dir() / "trusted.json"


def load() -> dict[str, str]:
    """Return ``{fingerprint: label}``. Missing/corrupt file = empty allowlist
    (full-trust fallback)."""
    try:
        raw = json.loads(trusted_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for e in raw.get("trusted", []) if isinstance(raw, dict) else []:
        if isinstance(e, dict) and isinstance(e.get("fingerprint"), str) and e["fingerprint"]:
            out[e["fingerprint"]] = str(e.get("label", ""))
    return out


def save(entries: dict[str, str]) -> None:
    """Atomic write (tmp + rename); best-effort, never raises."""
    path = trusted_path()
    payload = {"trusted": [{"fingerprint": fp, "label": lbl}
                           for fp, lbl in sorted(entries.items())]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def classify(fingerprint: str, entries: dict[str, str]) -> str:
    """``personal`` vs ``foreign`` for a *verified* fingerprint.

    - empty allowlist -> ``personal`` (boundary not configured; full trust);
    - fingerprint present in the allowlist -> ``personal``;
    - otherwise (unlisted, or empty because the peer was never verified) ->
      ``foreign``.
    """
    if not entries:
        return "personal"
    return "personal" if fingerprint and fingerprint in entries else "foreign"
