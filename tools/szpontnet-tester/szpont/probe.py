"""A multi-identity probe mesh: the tester's fleet of well-behaved fake nodes.

To exercise a candidate's placement, gossip, dispatch and fence behavior
black-box, the tester needs to *be* the rest of the mesh around it. ``ProbeMesh``
runs one or more ``ProbePeer`` identities — each a spec-correct SzpontNet node
(beacon, hello handshake, heartbeats, gossip, dispatch executor) — over real
sockets, so from the candidate's point of view it is talking to genuine peers.

Each peer is also a chapter-11 **trust peer**: it carries an Ed25519 keypair,
advertises its ``pubkey``, **self-signs every advertisement** it sends (an advert
``sig`` over ``"szpontnet-nodeinfo-v1:" ‖ canonical(NodeInfo)``, re-signed on
every ``gossip_self``/``set_info`` change), issues a challenge nonce in its own
hello, and answers the candidate's challenge with an ``auth`` signature over the
domain-separated bytes (``"szpontnet-auth-v1:" ‖ nonce``) — so the candidate
accepts, verifies and pins its adverts, and a trust suite can assert the
proof-of-possession round-trip. It can also sign an ``overrides`` edit it emits
on its own behalf (:meth:`signed_override`). A peer built with
``trust_peer=False`` (or on a host without ``cryptography``) stays keyless, so
the foreign / unverifiable paths are testable too. A peer may also advertise
``stats`` to drive ``surplus-first`` dispatch.

Each peer also *records* everything it receives and exposes hooks
(``send``, ``gossip_self``, ``raw_accept_handler``) so a test can drive precise
scenarios: retune an advertisement, inject an override, or play the adversary in
the outbound-dial fence test.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import socket
import threading
import time
from dataclasses import replace

from . import codec, net
from .codec import Job, NodeInfo

try:  # the one third-party dep; a probe degrades to keyless if it's missing
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _CRYPTO = True
except Exception:  # pragma: no cover - only where cryptography is absent
    _CRYPTO = False


class ProbeKey:
    """A probe peer's Ed25519 trust identity: advertises a base64 public key and
    signs the domain-separated challenge to PROVE possession (11). A keyless
    probe (no ``cryptography``) advertises nothing and can never be verified —
    exactly the reference's keyless degradation."""

    def __init__(self) -> None:
        self._priv = Ed25519PrivateKey.generate()
        raw = self._priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_b64 = base64.b64encode(raw).decode("ascii")
        self.fingerprint = hashlib.sha256(raw).hexdigest()

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._priv.sign(data)).decode("ascii")


def fingerprint_of(pubkey_b64: str) -> str:
    """sha256 of the raw 32-byte public key, hex — what the trust allowlist
    matches on (11). Empty for an empty/malformed key."""
    if not pubkey_b64:
        return ""
    try:
        return hashlib.sha256(base64.b64decode(pubkey_b64, validate=True)).hexdigest()
    except (ValueError, TypeError):
        return ""


def wait_until(predicate, timeout: float, interval: float = 0.1):
    """Poll ``predicate`` until truthy; return its value or None on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last if last else None


class ProbePeer:
    """One fake node identity the tester presents to the candidate."""

    def __init__(
        self, mesh: "ProbeMesh", id: str, name: str, platform: str, tier: int,
        tokens: str = "ok", duties_enabled: dict | None = None,
        dispatch_reply: str = "spawned", dial_mode: str = "auto",
        raw_accept_handler=None, trust_peer: bool = True,
        stats: dict | None = None,
    ) -> None:
        self.mesh = mesh
        self.dispatch_reply = dispatch_reply  # "spawned" | "failed" | "silent"
        self.dial_mode = dial_mode            # "auto" (id rule) | "always" | "never"
        self.raw_accept_handler = raw_accept_handler  # callable(conn, peer) for adversary tests

        # This probe's Ed25519 trust identity (11). ``trust_peer=True`` (the
        # default) advertises a pubkey and answers the candidate's auth challenge
        # so it can be verified; ``trust_peer=False`` (or a missing crypto lib)
        # leaves the probe keyless — foreign under any non-empty allowlist.
        self.key: ProbeKey | None = ProbeKey() if (trust_peer and _CRYPTO) else None
        self.pubkey = self.key.public_b64 if self.key else ""
        self.fingerprint = self.key.fingerprint if self.key else ""
        # Trust bookkeeping so a suite can assert the exchange happened.
        self.auth_sent = 0        # auth signatures WE sent (proving our key)
        self.auth_received = 0    # auth signatures the candidate sent us
        self.candidate_verified_ok = False  # candidate's auth verified against its pubkey
        self.candidate_pubkey = ""          # pubkey the candidate advertised in its hello

        # Listen socket the candidate dials when candidate.id < peer.id.
        host = "127.0.0.1" if mesh.loopback else "0.0.0.0"
        self.listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listen.bind((host, 0))
        self.listen.listen(8)
        self.listen.settimeout(0.3)
        self.tcp_port = self.listen.getsockname()[1]

        self.info = NodeInfo(
            id=id, name=name, platform=platform, tier=tier, tokens=tokens,
            tcp_port=self.tcp_port, duties_enabled=duties_enabled or {},
            epoch=mesh.epoch, seq=0, pubkey=self.pubkey, stats=dict(stats or {}),
        )
        # A keyed probe SELF-SIGNS its advertisement (11 authenticated gossip): the
        # reference now DROPS a keyed advert (one carrying a ``pubkey``) whose
        # ``sig`` is missing or doesn't verify, so an unsigned keyed advert would
        # never link/verify. A keyless probe (no key) leaves ``sig`` empty and stays
        # unauthenticated, exactly as before.
        self.info = self._signed(self.info)

        self._lock = threading.Lock()          # guards socket sends + info mutation
        self._conn: socket.socket | None = None
        self.linked = False
        self.accept_count = 0                  # inbound dials the candidate made to us
        self.received: list[dict] = []         # parsed messages seen on the link
        self.raw_received: list[bytes] = []    # raw frames, for framing checks
        self.jobs: list[Job] = []              # dispatch jobs the candidate sent us
        self.overrides: dict = {"rev": 0, "updatedBy": "", "duties": {}}
        self._nonce = ""       # the challenge THIS probe issues in its own hello
        self._stop = False
        self._dialing = False
        self._frozen = False   # simulate a silent death (stop sending heartbeats)

    # MARK: - beacon payload

    def beacon_bytes(self) -> bytes:
        with self._lock:
            return codec.encode(codec.beacon(self.info))

    # MARK: - link setup (both directions)

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.listen.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accept_count += 1
            if self.raw_accept_handler is not None:
                threading.Thread(target=self._raw_serve, args=(conn,), daemon=True).start()
                continue
            threading.Thread(target=self._serve_accepted, args=(conn,), daemon=True).start()

    def _raw_serve(self, conn: socket.socket) -> None:
        try:
            self.raw_accept_handler(conn, self)
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _hello_bytes(self) -> bytes:
        """Our hello, carrying a fresh per-connection trust-challenge nonce (11)
        so the candidate proves possession of its advertised key back to us."""
        import secrets
        with self._lock:
            self._nonce = secrets.token_hex(16)
            info = self.info
            nonce = self._nonce
        return codec.encode(codec.hello(info, self.overrides, self.mesh.secret, nonce))

    def _serve_accepted(self, conn: socket.socket) -> None:
        """Candidate dialed us: it sends its hello first; we reply with ours."""
        reader = net.LineReader(conn)
        first = reader.read_line(timeout=10.0)
        if not first:
            conn.close()
            return
        msg = codec.decode(first)
        if not msg or msg.get("t") != "hello":
            conn.close()
            return
        self._record(first, msg)
        node = msg.get("node") or {}
        if isinstance(node, dict) and isinstance(node.get("pubkey"), str):
            with self._lock:
                self.candidate_pubkey = node["pubkey"]
        self._send_raw(conn, self._hello_bytes())
        # The candidate's opening hello may already carry its challenge nonce;
        # answer it (prove OUR key) before pumping the link.
        self._maybe_answer_challenge(conn, msg)
        self._run_link(conn, reader)

    def _dial_candidate(self) -> None:
        with self._lock:
            if self.linked or self._dialing:
                return
            self._dialing = True
        try:
            cand = self.mesh.candidate
            if not cand or not cand.get("addr") or not cand.get("tcp_port"):
                return
            try:
                conn = net.connect_tcp(cand["addr"], cand["tcp_port"], timeout=5.0)
            except OSError:
                return
            self._send_raw(conn, self._hello_bytes())        # dialer sends hello first
            self._run_link(conn, net.LineReader(conn))
        finally:
            with self._lock:
                self._dialing = False

    def _run_link(self, conn: socket.socket, reader: net.LineReader) -> None:
        with self._lock:
            self._conn = conn
            self.linked = True
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        try:
            while not self._stop:
                line = reader.read_line(timeout=1.0)
                if line is None:
                    if reader.closed:
                        break
                    continue
                msg = codec.decode(line)
                self._record(line, msg)
                if msg is not None:
                    self._handle(conn, msg)
        finally:
            with self._lock:
                self.linked = False
                self._conn = None
            with contextlib.suppress(OSError):
                conn.close()

    def freeze(self) -> None:
        """Stop sending heartbeats without closing the socket — simulates a
        silent death so the candidate must reap us via the heartbeat timeout
        (03-transport link-state), not a clean EOF."""
        self._frozen = True

    def _heartbeat_loop(self) -> None:
        interval = self.mesh.proto["heartbeatIntervalSecs"]
        while not self._stop:
            time.sleep(interval)
            if self._frozen:
                continue
            conn = self._conn
            if conn is None:
                return
            try:
                self._send_raw(conn, codec.encode(codec.heartbeat()))
            except OSError:
                return

    # MARK: - message handling (executor half + gossip sink)

    def _handle(self, conn: socket.socket, msg: dict) -> None:
        t = msg.get("t")
        if t == "dispatch":
            job = Job.from_dict(msg.get("job") or {})
            if job is None:
                return
            self.jobs.append(job)
            if self.dispatch_reply == "silent":
                return
            reason = "" if self.dispatch_reply == "spawned" else "probe declined (test)"
            self._send_raw(conn, codec.encode(codec.job_status(
                job.id, self.dispatch_reply, reason, self.info.id)))
        elif t == "hello":
            # A candidate that dials us sends its hello on the pumped link; it may
            # carry a challenge nonce we must answer (prove OUR key). Also learn
            # the candidate's advertised pubkey so we can verify its own auth.
            node = msg.get("node") or {}
            if isinstance(node, dict) and isinstance(node.get("pubkey"), str):
                with self._lock:
                    self.candidate_pubkey = node["pubkey"]
            self._maybe_answer_challenge(conn, msg)
        elif t == "auth":
            self._verify_candidate_auth(msg)
        elif t == "overrides":
            raw = msg.get("overrides")
            if isinstance(raw, dict):
                self.overrides = raw

    def _maybe_answer_challenge(self, conn: socket.socket, hello_msg: dict) -> None:
        """The candidate's hello carried a nonce → sign the domain-separated
        challenge with our private key to prove possession (11). A keyless probe
        stays silent (never verified → foreign under any allowlist)."""
        nonce = hello_msg.get("nonce")
        if not (isinstance(nonce, str) and nonce and self.key is not None):
            return
        sig = self.key.sign(codec.auth_challenge(nonce))
        with contextlib.suppress(OSError):
            self._send_raw(conn, codec.encode(codec.auth(sig)))
            with self._lock:
                self.auth_sent += 1

    def _verify_candidate_auth(self, msg: dict) -> None:
        """The candidate answered OUR challenge with an auth. Verify its signature
        over ``AUTH_CONTEXT || our-nonce`` against the pubkey it advertised, so a
        suite can assert the candidate proved possession of its key."""
        with self._lock:
            self.auth_received += 1
            nonce, pub = self._nonce, self.candidate_pubkey
        sig = msg.get("sig")
        if not (isinstance(sig, str) and sig and nonce and pub and _CRYPTO):
            return
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pub, validate=True)).verify(
                base64.b64decode(sig, validate=True), codec.auth_challenge(nonce))
            with self._lock:
                self.candidate_verified_ok = True
        except Exception:
            pass

    # MARK: - test-driver hooks

    def send(self, msg: dict) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            self._send_raw(conn, codec.encode(msg))
            return True
        except OSError:
            return False

    def _signed(self, info: NodeInfo) -> NodeInfo:
        """Attach our Ed25519 signature over this advert's canonical form so a
        keyed advert verifies at the receiver (11). Byte-for-byte identical to the
        reference: sign ``ADVERT_CONTEXT ‖ canonical(to_dict() without sig)``. A
        keyless probe returns the advert unsigned (never verifiable)."""
        if self.key is None:
            return info
        sig = self.key.sign(codec.advert_signing_bytes(info.to_dict()))
        return replace(info, sig=sig)

    def gossip_self(self, **changes) -> bool:
        """Bump our advertisement (seq+1, applying ``changes``), RE-SIGN it (the
        signed bytes changed), and gossip it."""
        with self._lock:
            self.info = self._signed(self.info.bumped(**changes))
            info = self.info
        return self.send(codec.node_update(info))

    def set_info(self, **changes) -> None:
        with self._lock:
            self.info = self._signed(replace(self.info, **changes))

    def sign_node_dict(self, node_dict: dict) -> dict:
        """Self-sign an arbitrary raw NodeInfo dict with this probe's key, over the
        FINAL dict (so any extra/unknown fields it carries are covered, exactly as a
        real originator that added them would sign — the reference's canonical form
        covers every field but ``sig``). Stamps our ``pubkey`` if absent so the sig
        verifies against it. A keyless probe returns the dict unsigned. Use for
        adverts hand-built for id/epoch/seq control (freshness, tolerance tests)."""
        d = dict(node_dict)
        if self.key is None:
            return d
        d.setdefault("pubkey", self.pubkey)
        d.pop("sig", None)
        d["sig"] = self.key.sign(codec.advert_signing_bytes(d))
        return d

    def signed_override(self, overrides: dict) -> dict:
        """Sign a placement-overrides edit as if THIS probe were the editor: set
        ``updatedBy`` to our id and attach a ``sig`` over the canonical override
        bytes (11). The reference requires a valid signature for a non-default
        (rev>0) override from a KNOWN editor, so a probe sending an override on its
        own behalf must sign it or the edit is dropped. A keyless probe returns the
        override unsigned (the reference then can't verify it and accepts it as
        legacy)."""
        ov = dict(overrides)
        ov["updatedBy"] = self.info.id
        ov.pop("sig", None)
        if self.key is not None:
            ov["sig"] = self.key.sign(codec.overrides_signing_bytes(ov))
        return ov

    def _send_raw(self, conn: socket.socket, data: bytes) -> None:
        with self._lock:
            conn.sendall(data)

    def _record(self, raw: bytes, msg: dict | None) -> None:
        with self._lock:
            self.raw_received.append(raw)
            if msg is not None:
                self.received.append(msg)

    def messages(self, t: str | None = None) -> list[dict]:
        with self._lock:
            msgs = list(self.received)
        return [m for m in msgs if m.get("t") == t] if t else msgs

    def stop(self) -> None:
        self._stop = True
        with contextlib.suppress(OSError):
            self.listen.close()
        conn = self._conn
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()


class ProbeMesh:
    """Owns the shared discovery sockets and a set of ProbePeer identities."""

    def __init__(self, model, proto: dict, candidate_id: str, loopback: bool,
                 secret: str = "") -> None:
        self.model = model
        self.proto = proto
        self.candidate_id = candidate_id
        self.loopback = loopback
        self.secret = secret
        self.epoch = time.time()
        self.group = proto["multicastGroup"]
        self.mport = proto["multicastPort"]
        self.peers: list[ProbePeer] = []
        self.candidate: dict = {}              # learned from the candidate's beacons
        self.candidate_beacons: list[dict] = []
        self.candidate_beacon_raw: list[bytes] = []
        self._rx = net.make_beacon_rx(self.group, self.mport, loopback)
        self._tx = net.make_beacon_tx(loopback)
        self._stop = False

    def add_peer(self, **kwargs) -> ProbePeer:
        peer = ProbePeer(self, **kwargs)
        self.peers.append(peer)
        return peer

    def start(self) -> None:
        for peer in self.peers:
            threading.Thread(target=peer._accept_loop, daemon=True).start()
        threading.Thread(target=self._beacon_loop, daemon=True).start()
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._dial_loop, daemon=True).start()

    def _beacon_loop(self) -> None:
        interval = self.proto["beaconIntervalSecs"]
        while not self._stop:
            for peer in self.peers:
                net.send_beacon(self._tx, peer.beacon_bytes(), self.group, self.mport, self.loopback)
            time.sleep(interval)

    def _rx_loop(self) -> None:
        while not self._stop:
            try:
                self._rx.settimeout(0.3)
                data, (host, _) = self._rx.recvfrom(4096)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                return
            msg = codec.decode(data)
            if not msg or msg.get("t") != "beacon":
                continue
            if str(msg.get("id")) == self.candidate_id:
                raw = data if data.endswith(b"\n") else data + b"\n"
                self.candidate_beacons.append(msg)
                self.candidate_beacon_raw.append(raw)
                addr = "127.0.0.1" if self.loopback else host
                self.candidate = {
                    "id": self.candidate_id, "addr": addr,
                    "tcp_port": msg.get("tcpPort"), "epoch": msg.get("epoch"),
                    "name": msg.get("name"), "platform": msg.get("platform"),
                }

    def _dial_loop(self) -> None:
        """Once the candidate is known, each peer whose id sorts below it dials
        (smaller-id-dials); the rest wait to be dialed on their listen sockets."""
        while not self._stop:
            if self.candidate.get("tcp_port"):
                for peer in self.peers:
                    if peer.linked or peer._dialing:
                        continue
                    should_dial = (
                        peer.dial_mode == "always"
                        or (peer.dial_mode == "auto" and peer.info.id < self.candidate_id)
                    )
                    if should_dial:
                        threading.Thread(target=peer._dial_candidate, daemon=True).start()
            time.sleep(0.25)

    def raw_beacon(self, payload: dict) -> None:
        """Send an arbitrary beacon (adversarial / spoof tests)."""
        net.send_beacon(self._tx, codec.encode(payload), self.group, self.mport, self.loopback)

    def stop(self) -> None:
        self._stop = True
        for peer in self.peers:
            peer.stop()
        with contextlib.suppress(OSError):
            self._rx.close()
        with contextlib.suppress(OSError):
            self._tx.close()
