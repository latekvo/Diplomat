"""Per-device cryptographic identity for SzpontNet trust.

A node's **trust identity** is an Ed25519 keypair generated once per machine and
persisted locally (``~/.diplomat/mesh/device.key``, ``0600``). The private key
never leaves the machine and is never gossiped; the public key is advertised, but
**advertising it grants nothing**. A peer is only believed to hold a given key
once it *signs a fresh, per-connection challenge* with the matching private key
(proof of possession, in :mod:`diplomat_app.mesh.node`). Trust decisions then key
on the resulting **fingerprint** against a local operator-managed allowlist
(:mod:`diplomat_app.mesh.trust`).

This is the design consequence of "assume advertisements are spoofed": the node
`id`, `name`, and every other self-reported field are display-only and confer no
privilege. The only thing that promotes a peer to *personal* - and so lets its
requests run social actions under your identity - is a signature a stranger
cannot forge without your private key.

Ed25519 is provided by the ``cryptography`` package. If it is unavailable the
node still runs, keyless: it advertises no public key, can never be verified, and
so is treated as *foreign* by any peer that has configured a trust allowlist (and
as *personal* only under the empty-allowlist full-trust fallback).
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from . import identity

try:  # the one third-party dependency; a keyless node degrades, never crashes
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    AVAILABLE = True
except Exception:  # pragma: no cover - exercised only where the lib is absent
    AVAILABLE = False


def key_path() -> Path:
    return identity.mesh_dir() / "device.key"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def fingerprint_of(pubkey_b64: str) -> str:
    """Stable 64-hex fingerprint of a public key - what the trust allowlist
    matches on and what the UI/CLI shows (short-prefixed). Empty for an empty
    or malformed key, which can therefore never match an allowlist entry."""
    if not pubkey_b64:
        return ""
    try:
        return hashlib.sha256(_unb64(pubkey_b64)).hexdigest()
    except (ValueError, TypeError):
        return ""


class DeviceKey:
    """This machine's Ed25519 identity."""

    def __init__(self, private: "Ed25519PrivateKey") -> None:
        self._priv = private
        raw_pub = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_b64 = _b64(raw_pub)
        self.fingerprint = hashlib.sha256(raw_pub).hexdigest()

    def sign(self, data: bytes) -> str:
        """Sign a challenge; returns base64. Bound to a fresh per-connection
        nonce by the caller, so the signature can't be replayed on another link."""
        return _b64(self._priv.sign(data))

    @property
    def short(self) -> str:
        return self.fingerprint[:16]


def load_or_create() -> "DeviceKey | None":
    """Load this machine's keypair, minting + persisting one on first run.
    Returns None when ``cryptography`` is unavailable (the node runs keyless)."""
    if not AVAILABLE:
        return None
    path = key_path()
    try:
        raw = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        return DeviceKey(priv)
    except (OSError, ValueError):
        pass
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".key.tmp")
        # Write private-key material 0600 from the start (never world-readable).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw.hex() + "\n")
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort: an unwritable HOME still yields an in-memory key
    return DeviceKey(priv)


def verify(pubkey_b64: str, data: bytes, sig_b64: str) -> bool:
    """True iff ``sig_b64`` is a valid signature of ``data`` under ``pubkey_b64``.
    False on any malformed input or a bad signature - never raises, so a hostile
    peer can't wedge the link with garbage."""
    if not AVAILABLE or not pubkey_b64 or not sig_b64:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(_unb64(pubkey_b64))
        pub.verify(_unb64(sig_b64), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
