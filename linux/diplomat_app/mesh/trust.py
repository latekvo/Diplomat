"""The local trusted-device allowlist - who counts as *personal*.

Trust is set **manually by the operator** and lives **only on this machine**; it
is never gossiped and never derived from anything a peer advertises. The store
is a set of Ed25519 **fingerprints** ([crypto](crypto.py)) the operator has
explicitly marked as their own devices. A peer is *personal* only if the
fingerprint it **proved possession of** on the link (its verified key, not a
claimed one) is in this set; everything else falls to the **default trust level**.

**Zero-trust by default.** A device the operator has not explicitly marked
personal is *foreign*: a new machine that joins the mesh is untrusted until you
promote it. The allowlist is thus the set of *exceptions* (promotions) to a
foreign baseline. The default level is configurable per node
(``config.default_trust`` - env / ``core/mesh.json``, persisted operator choice in
this same file's ``defaultLevel``): flipping it to *personal* restores the
pre-trust **full-altruism** mode where every unlisted peer is trusted, exactly as
a fresh mesh behaved before the default became configurable.

Persisted at ``~/.diplomat/mesh/trusted.json`` as
``{"defaultLevel": "...", "trusted": [{"fingerprint","label"}, ...]}``; the running
node keeps the set + level in memory and edits them through control commands so
``--trust`` / the panel's default-trust toggle take effect live.
"""

from __future__ import annotations

import json

from . import identity
from .atomicjson import write_atomic


def trusted_path():
    return identity.mesh_dir() / "trusted.json"


def _read() -> dict:
    """The parsed store dict, or ``{}`` for a missing/corrupt file."""
    try:
        raw = json.loads(trusted_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load() -> dict[str, str]:
    """Return the allowlist ``{fingerprint: label}`` (the operator's explicitly
    *personal* devices). Missing/corrupt file = empty allowlist."""
    out: dict[str, str] = {}
    for e in _read().get("trusted", []):
        if isinstance(e, dict) and isinstance(e.get("fingerprint"), str) and e["fingerprint"]:
            out[e["fingerprint"]] = str(e.get("label", ""))
    return out


def load_default_level() -> str:
    """The operator's persisted default-trust choice (``"personal"``/``"foreign"``),
    or ``""`` when the file predates the toggle / doesn't set one - the caller then
    falls back to :func:`config.default_trust` (env / shipped baseline)."""
    lvl = str(_read().get("defaultLevel", "")).strip().lower()
    return lvl if lvl in ("personal", "foreign") else ""


def save(entries: dict[str, str], default_level: str = "") -> None:
    """Atomic write (tmp + rename) of the allowlist and, when set, the persisted
    ``defaultLevel``. Best-effort, never raises."""
    payload: dict = {}
    if default_level in ("personal", "foreign"):
        payload["defaultLevel"] = default_level
    payload["trusted"] = [{"fingerprint": fp, "label": lbl}
                          for fp, lbl in sorted(entries.items())]
    write_atomic(trusted_path(), payload)


def classify(fingerprint: str, entries: dict[str, str], default_level: str = "foreign") -> str:
    """``personal`` vs ``foreign`` for a *verified* fingerprint.

    - fingerprint present in the allowlist -> ``personal`` (an explicit promotion
      always wins);
    - otherwise (unlisted, or empty ``fingerprint`` because the peer was never
      verified) -> ``default_level`` - which ships ``foreign`` (a new device is
      zero-trust until the operator promotes it), but an operator can set to
      ``personal`` for a full-trust mesh.
    """
    if fingerprint and fingerprint in entries:
        return "personal"
    return "personal" if default_level == "personal" else "foreign"
