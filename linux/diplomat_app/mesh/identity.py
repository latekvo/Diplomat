"""Node identity + node-local attributes, persisted in ``~/.diplomat/mesh/node.json``.

The id is a stable UUID minted on first run; name/tier/tokens are the
user-editable attributes the node gossips (and that peers may edit remotely
through a ``set-attr`` message — the topology panel configures the whole mesh
from one machine that way).
"""

from __future__ import annotations

import json
import os
import platform as _platform
import socket
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from . import config, hardware

# The manual token-override values. "auto" (the default) means "derive my ok/low/out
# state from real local usage" (see usage.py); the other three pin it, as a
# "pause this node" / force-available escape.
TOKEN_STATES = ("auto", "ok", "low", "out")


def mesh_dir() -> Path:
    """State directory — override with DIPLOMAT_MESH_DIR (tests give every
    fake node its own)."""
    env = os.environ.get("DIPLOMAT_MESH_DIR")
    return Path(env) if env else Path.home() / ".diplomat" / "mesh"


def node_path() -> Path:
    return mesh_dir() / "node.json"


def detect_platform() -> str:
    env = os.environ.get("DIPLOMAT_MESH_PLATFORM")  # tests fake mixed-OS meshes
    if env:
        return env
    sys = _platform.system()
    if sys == "Darwin":
        return "macos"
    if sys == "Linux":
        return "linux"
    return sys.lower() or "unknown"


def default_name() -> str:
    return socket.gethostname().split(".")[0] or "unnamed"


@dataclass(frozen=True)
class LocalNode:
    """The persisted identity + attributes of *this* node."""

    id: str
    name: str
    tier: int
    tokens: str  # manual token override: "auto" | "ok" | "low" | "out"
    duties_enabled: dict  # duty id -> bool (absent = enabled)
    # Whether ``tier`` was auto-detected from hardware (True) or pinned by an
    # explicit edit (False). A manual tier edit flips this off so detection stops
    # overriding the operator's choice.
    strength_auto: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "tokens": self.tokens,
            "strengthAuto": self.strength_auto,
            "dutiesEnabled": self.duties_enabled,
        }

    def duty_enabled(self, duty_id: str) -> bool:
        return bool(self.duties_enabled.get(duty_id, True))


def _clamped_tier(raw: object) -> int:
    lo, hi, default = config.tier_bounds()
    try:
        return min(hi, max(lo, int(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def load() -> LocalNode:
    """Load (or mint) this machine's identity. Malformed fields fall back to
    defaults; a missing file is first-run and gets persisted immediately.

    Strength auto-detection: a fresh node (or one that never pinned its tier) has
    ``strengthAuto`` on, so its tier is (re)computed from the machine's specs on
    every load and persisted. An explicit ``tier`` in the file with no
    ``strengthAuto`` flag is treated as a pin (back-compat: that's how a
    hand-written node.json expresses a chosen tier)."""
    _, _, default_tier = config.tier_bounds()
    raw: dict = {}
    try:
        raw = json.loads(node_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass

    # Default: auto unless the file explicitly pins a tier (older files) or says so.
    if "strengthAuto" in raw:
        auto = bool(raw.get("strengthAuto"))
    else:
        auto = "tier" not in raw
    tier = hardware.detect_tier() if auto else _clamped_tier(raw.get("tier", default_tier))

    node = LocalNode(
        id=str(raw.get("id") or uuid.uuid4().hex),
        name=str(raw.get("name") or default_name()),
        tier=tier,
        tokens=raw.get("tokens") if raw.get("tokens") in TOKEN_STATES else "auto",
        duties_enabled=dict(raw.get("dutiesEnabled", {})),
        strength_auto=auto,
    )
    # First run, a corrupt file, or a refreshed auto tier: persist the current view.
    if raw.get("id") != node.id or node.to_dict() != raw:
        save(node)
    return node


def save(node: LocalNode) -> None:
    """Atomic write (tmp + rename) so a concurrent reader never sees a torn file."""
    path = node_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(node.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # best-effort: an unwritable HOME still gets an in-memory identity


def apply_attrs(node: LocalNode, attrs: dict) -> LocalNode:
    """Apply a (possibly remote) attribute edit. Unknown keys and invalid
    values are ignored — the message may come from a newer/older peer."""
    out = node
    if isinstance(attrs.get("name"), str) and attrs["name"].strip():
        out = replace(out, name=attrs["name"].strip()[:64])
    if "tier" in attrs:
        # An explicit tier edit pins the value: turn auto-detection off so it
        # doesn't get clobbered on the next load.
        out = replace(out, tier=_clamped_tier(attrs["tier"]), strength_auto=False)
    if "strengthAuto" in attrs:
        auto = bool(attrs["strengthAuto"])
        # Re-enabling auto immediately re-detects, so the panel shows the effect.
        out = replace(out, strength_auto=auto,
                      tier=hardware.detect_tier() if auto else out.tier)
    if attrs.get("tokens") in TOKEN_STATES:
        out = replace(out, tokens=attrs["tokens"])
    if isinstance(attrs.get("dutiesEnabled"), dict):
        merged = dict(out.duties_enabled)
        for k, v in attrs["dutiesEnabled"].items():
            merged[str(k)] = bool(v)
        out = replace(out, duties_enabled=merged)
    return out
