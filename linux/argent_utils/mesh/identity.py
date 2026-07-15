"""Node identity + node-local attributes, persisted in ``~/.argent/mesh/node.json``.

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

from . import config

TOKEN_STATES = ("ok", "low", "out")


def mesh_dir() -> Path:
    """State directory — override with ARGENT_MESH_DIR (tests give every
    fake node its own)."""
    env = os.environ.get("ARGENT_MESH_DIR")
    return Path(env) if env else Path.home() / ".argent" / "mesh"


def node_path() -> Path:
    return mesh_dir() / "node.json"


def detect_platform() -> str:
    env = os.environ.get("ARGENT_MESH_PLATFORM")  # tests fake mixed-OS meshes
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
    tokens: str  # "ok" | "low" | "out"
    duties_enabled: dict  # duty id -> bool (absent = enabled)
    # Trust domain: a stable id for whoever owns this node (the human/fleet).
    # Nodes sharing an owner are "personal" to each other; a different owner is
    # "foreign". Empty (the default) means unset — every peer is personal, so a
    # mesh with no owners set behaves exactly like v1 full-trust.
    owner: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "tokens": self.tokens,
            "dutiesEnabled": self.duties_enabled,
        }
        if self.owner:
            d["owner"] = self.owner
        return d

    def duty_enabled(self, duty_id: str) -> bool:
        return bool(self.duties_enabled.get(duty_id, True))


def trust_of(peer_owner: str, my_owner: str) -> str:
    """Classify a peer from its advertised ``owner`` against ours.

    ``"personal"`` — one of my own devices (same owner), or trust can't be
    decided because either side hasn't set an owner (the v1-compatible default:
    unknown owner ⇒ trusted). A personal peer's SzpontRequests run directly.

    ``"foreign"`` — a device owned by someone else (both owners set, and
    different). Its requests are declined in v1; the zero-trust path (run foreign
    compute, route social actions back through a personal node) is future work.
    """
    if not my_owner or not peer_owner:
        return config.trust_default()
    return "personal" if peer_owner == my_owner else "foreign"


def _clamped_tier(raw: object) -> int:
    lo, hi, default = config.tier_bounds()
    try:
        return min(hi, max(lo, int(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def load() -> LocalNode:
    """Load (or mint) this machine's identity. Malformed fields fall back to
    defaults; a missing file is first-run and gets persisted immediately."""
    _, _, default_tier = config.tier_bounds()
    raw: dict = {}
    try:
        raw = json.loads(node_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    node = LocalNode(
        id=str(raw.get("id") or uuid.uuid4().hex),
        name=str(raw.get("name") or default_name()),
        tier=_clamped_tier(raw.get("tier", default_tier)),
        tokens=raw.get("tokens") if raw.get("tokens") in TOKEN_STATES else "ok",
        duties_enabled=dict(raw.get("dutiesEnabled", {})),
        # ARGENT_MESH_OWNER lets a fleet stamp the same owner on every machine
        # without editing each node.json; the file value wins if both are set.
        owner=str(raw.get("owner") or os.environ.get("ARGENT_MESH_OWNER", "")),
    )
    if raw.get("id") != node.id:  # first run (or a corrupt file): persist the minted id
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
        out = replace(out, tier=_clamped_tier(attrs["tier"]))
    if attrs.get("tokens") in TOKEN_STATES:
        out = replace(out, tokens=attrs["tokens"])
    if isinstance(attrs.get("owner"), str):
        out = replace(out, owner=attrs["owner"].strip()[:64])
    if isinstance(attrs.get("dutiesEnabled"), dict):
        merged = dict(out.duties_enabled)
        for k, v in attrs["dutiesEnabled"].items():
            merged[str(k)] = bool(v)
        out = replace(out, duties_enabled=merged)
    return out
