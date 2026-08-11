# 15 — Iroh transport (WAN reachability)

v1 is single-LAN ([02](02-discovery.md), [03](03-transport.md)): discovery is
link-local multicast/broadcast, and a link is a direct TCP connection to a peer's
LAN address. That is the whole mesh as long as every machine is on the same
network. The **iroh transport** lifts that restriction: once two nodes have met,
they can keep talking from **anywhere**, with **no public IP and no domain name**.

It is **opt-in** and **atomic**. Complementary, not alternative: multicast discovery
and direct TCP links remain the path between peers that share a network, and this is
what makes several such networks one mesh. Nothing below changes the LAN path — the
iroh transport is added *beside* it.

[14 — Tor](14-tor-transport.md) reaches the same peers through an onion service and
is the default WAN transport; this one costs no daemon, no multi-minute bootstrap and
no rendezvous circuit per dial. A node may run either, both, or neither, and one
running both prefers this one per peer.

Turned on with `SZPONTNET_IROH=1` (`true`/`yes`/`on` are honoured too; every other
value, including a typo, leaves it off), and absent whenever the implementation's
iroh library is unavailable. In either case this transport is gone; the node is the
LAN-only node described in the rest of these docs unless it also runs
[14](14-tor-transport.md), and wire-identical to one when it does not.

> **A node running this transport publishes a stable endpoint.** It serves only the
> peer-link accept path — control sessions are refused
> ([Security notes](#security-notes)) — but on an **open mesh** (no `SZPONTNET_SECRET`,
> the documented home-LAN default) any peer holding the address can link from the
> WAN, which on a LAN-only node would have required being on the LAN. Set a join
> secret, or leave `SZPONTNET_IROH` unset, if that is not what you want.

## The idea in one paragraph

Every iroh-enabled node binds a **QUIC endpoint** whose id is a permanent,
NAT-independent handle: an Ed25519 public key, persisted on disk. A node
**advertises its endpoint id inside its signed [advertisement](04-messages.md#node)**,
so peers learn it on the very first `hello`. When a node holds a peer's endpoint id
but does not currently see it on the LAN, it **dials that id** (with per-peer
exponential backoff) — by identity, naming no address: the transport locates the peer
through discovery, hole-punches a direct path where both networks allow it, and
relays where they do not. A dialed connection runs the **identical**
`hello`/`auth`/trust handshake and message pump a LAN link runs — so once up, an iroh
link is indistinguishable from a LAN link to everything above the socket (dispatch,
gossip, heartbeats, trust).

## What plugs in, and what doesn't

The link layer already consumes a bare `(reader, writer)` stream, so the transport
seam is tiny:

- **Inbound adds no protocol surface.** Each accepted connection's first
  bidirectional stream is adapted to that pair and handed straight to the *same*
  accept path a LAN link uses ([03](03-transport.md#inbound-the-accepter)), running
  the same handshake. The node **tags** the link as arriving over iroh — which it
  uses to keep the link's endpoint out of the LAN redial cache and to refuse operator
  control (`ctl`) sessions over it (see [Security notes](#security-notes)).
- **Outbound is the one new primitive:** a QUIC connection to the peer's endpoint id,
  whose first bidirectional stream is handed to the same link pump a LAN dial uses.

The NDJSON framing and the `MAX_LINE_BYTES` budget of
[03](03-transport.md#framing) apply unchanged: the stream is a byte stream, and the
line budget is enforced on it exactly as on TCP.

**ALPN.** The connection negotiates the application protocol `szpontnet/1`,
versioned with the wire protocol so a future incompatible framing can be introduced
without a v1 node ever accepting it — a mismatch is refused before any bytes are
read.

## Address exchange

The advertisement gains one additive field, `endpoint` (64 lowercase hex characters),
**omitted when empty** so a LAN-only or older node stays wire-identical. Because the
field rides **inside the signed advert**
([11 — authenticated gossip](11-trust-and-balancing.md#authenticated-gossip)), it is
bound to the advertiser's device key end to end: a relay cannot swap a peer's
endpoint to redirect a future dial.

The endpoint key is **not** the device key of [11](11-trust-and-balancing.md).
Both are Ed25519, and collapsing them would be tempting, but the transport signs its
own handshake with the endpoint key while the mesh signs nonce challenges with the
device key; one key serving two protocols buys nothing here. After connecting, the
peer still proves its **device** key with the normal nonce `auth`, so a wrong or
hijacked endpoint simply lands the connection as an unverified, `foreign` peer.

A node persists the WAN addresses it learns in `wan.json` (keyed by node id, with the
device fingerprint they were paired with) — the WAN sibling of the LAN `peers.json`
redial cache ([08](08-state.md)). One entry holds a peer's `endpoint` and, while Tor
is still shipped, its `onion`: they are one fact about one peer, so one eviction
policy and one trust check govern WAN redials however many transports are compiled
in. It is a best-effort accelerator: a missing or stale entry costs at most a fenced
dial.

## Reconnecting: reachability with backoff

A node probes a known-but-unseen peer on a per-peer **exponential backoff** (start
small, double to a ceiling, and drop the schedule entirely the moment a WAN link to
that peer actually **binds** — a valid signed hello, not a bare connection). An
address that answers but never links stays throttled.

**The dial rule is the LAN's**: the smaller node id dials, so exactly one side
originates ([02](02-discovery.md#the-dial-rule-smaller-id-dials)).

**Only `personal` peers are auto-dialed.** A linked `foreign` peer can advertise an
arbitrary, attacker-chosen address; auto-dialing it would make the node a dial
reflector aimed by the attacker and leak a signed hello to a destination it picked.
Foreign peers reach *us* inbound. Promote the device, run full-altruism
(`SZPONTNET_DEFAULT_TRUST=personal`), or reach across with a one-shot manual paste
(below).

**One dial per peer per tick, most-preferred transport first.** A peer reachable both
ways is dialed over iroh; a peer that advertises only an `onion` is still dialed over
Tor for as long as that transport is enabled. A transport that is not yet up (or has
died) is skipped, and the peer falls through to the next one that is.

**No aggressive switching.** A peer that already holds a live link — over *any*
transport — is never probed or re-dialed. A WAN link is not torn down merely because
the peer reappears on the LAN, and vice versa; a link only changes on a genuine peer
restart (a higher-epoch advert once the old link has gone quiet), exactly as on the
LAN today.

## Manual introduction (no prior LAN meeting)

You can reach a peer you were **never** on a LAN with by pasting its endpoint id:

```
python -m szpontnet --iroh-connect <64-hex>
```

This dials **unconditionally** (bypassing the smaller-id rule — it is a deliberate
one-shot). The handshake proceeds normally; from then on the peer is an ordinary mesh
member and its endpoint is cached like any other.

## Lifecycle & degradation

On start, the node binds its endpoint from a **persisted key** under
`<mesh_dir>/iroh/` (so the id is stable across restarts, and so several nodes on one
host never collide). Coming online runs **in the background** — the node is fully
usable on the LAN meanwhile — and the endpoint is advertised (a fresh gossip) once it
is reachable, never before: advertising earlier would hand peers an id that resolves
to nothing. If the library is missing, or the endpoint never comes online, the node
logs it and stays **LAN-only** — the same graceful degradation as the keyless path
when `cryptography` is absent.

Degradation also extends **past** startup: if the endpoint is later closed, the node
stops advertising and dialing it and reports iroh as not-ready — it degrades back to
LAN-only rather than claiming a WAN handle that no longer answers.

## Security notes

- iroh gives the WAN link **transport confidentiality and integrity** for free (QUIC
  with TLS 1.3, so a relay carries only ciphertext), which the plaintext LAN link
  does not have — but the mesh's **trust** decision does not depend on it either way:
  trust still keys only on the **verified device fingerprint**
  ([11](11-trust-and-balancing.md)), so an iroh peer is `foreign` until its
  fingerprint is in your allowlist, exactly like a LAN peer.
- The [join fence](03-transport.md#the-join-fence) (`SZPONTNET_SECRET`) applies
  unchanged: the secret check is transport-agnostic, and because the connection is
  encrypted the token is not exposed in transit (as it would be on the plaintext LAN).
- **Operator control (`ctl`) is never served over a WAN transport.** The accept path
  serves *two* kinds of opener: peer links (`hello`) and the operator's local
  **control** channel (`ctl` — `status`, `dispatch`, `set-attr`, `trust`/`ban`,
  `set-default-trust`, `iroh-connect`, `stop`). Only `hello` is meant to arrive from
  the network; `ctl` is the operator driving their *own* node over loopback. A
  connection that did not arrive on the LAN is therefore refused outright if it opens
  a `ctl` session — otherwise the full node-control surface would be reachable by
  anyone holding the advertised address, and in an **open mesh** (no join secret —
  the documented home-LAN default) with no authentication at all. The gate is "is
  this a LAN link", not a list of WAN transport names, so a transport added later is
  refused by default. Peer linking, dispatch, gossip, and trust are unaffected; only
  the local admin channel is fenced off from the WAN.
- Enabling iroh advertises a stable endpoint to your mesh peers, and publishes its
  reachability to whatever discovery service the implementation is configured with.
  Beyond that endpoint — which serves only peer links to the accept path (control
  sessions refused, per the point above) — it does not expose the node to the open
  internet. Note the corollary of the join fence: on an **open** mesh, a peer can
  still *link* and (subject to [trust](11-trust-and-balancing.md)) exchange gossip and
  dispatch with you from the WAN, exactly as an unauthenticated LAN peer could on the
  LAN. If that is not what you want, set a `SZPONTNET_SECRET`.

## Configuration

| Env | Meaning |
|-----|---------|
| `SZPONTNET_IROH=1` | Enable the iroh transport. Off by default; `true`/`yes`/`on` also enable, and every other value leaves it off. |
| `SZPONTNET_IROH_ONLINE_SECS` | Wait for the endpoint to come online before giving up (default 30). |

## Conformance

A node that never binds an endpoint is still conformant — the transport is optional
to *implement*, and everything above degrades to the LAN-only node in
[02](02-discovery.md)/[03](03-transport.md). What is normative is what goes on the
wire when it is implemented: `endpoint` inside the signed advert
([04](04-messages.md#nodeinfo)), omitted when empty and 64 lowercase hex when
present; the `szpontnet/1` ALPN; and the refusal of `ctl` on a connection that did
not arrive over the LAN.

The reference implementation's own coverage runs at two altitudes — the node's WAN
decisions against an injected dialer, and the whole path against real endpoints. See
`szpontnet-core/tests/test_mesh_iroh.py` and `test_iroh_e2e.py`.
