# 14 — Tor transport (WAN reachability)

> [15 — the iroh transport](15-iroh-transport.md) reaches the same peers without a
> `tor` daemon, a multi-minute bootstrap or a rendezvous circuit per dial, and is the
> other WAN transport a node can run. Both are on by default. Either alone is a
> complete WAN path, and where an edge could use both, the mesh's gossiped
> [preferred transport](06-coordination.md#the-preferred-wan-transport) settles it.

v1 is single-LAN ([02](02-discovery.md), [03](03-transport.md)): discovery is
link-local multicast/broadcast, and a link is a direct TCP connection to a peer's
LAN address. That is the whole mesh as long as every machine is on the same
network. The **Tor transport** lifts that restriction: once two nodes have met,
they can keep talking from **anywhere**, with **no public IP and no domain name**.

It is **on by default** and **atomic**. Complementary, not alternative: multicast
discovery and direct TCP links remain the path between peers that share a network,
and this is what makes several such networks one mesh. Nothing below changes the LAN
path — the Tor transport is added *beside* it.

Turned off with `SZPONTNET_TOR=0` (`false`/`no`/`off`/empty are honoured too), and
absent whenever no `tor` binary is installed. In either case this transport is gone;
the node is the LAN-only node described in the rest of these docs unless it also runs
[15](15-iroh-transport.md), and wire-identical to one when it does not.

> **A node running this transport publishes a stable onion service by default.** It
> forwards only to the peer-link accept path — control sessions are refused
> ([Security notes](#security-notes)) — but on an **open mesh** (no `SZPONTNET_SECRET`,
> the documented home-LAN default) any peer holding the address can link from the
> WAN, which on a LAN-only node would have required being on the LAN. Set a join
> secret, or `SZPONTNET_TOR=0`, if that is not what you want.

## The idea in one paragraph

Every Tor-enabled node runs a **v3 onion service** whose address is a permanent,
NAT-independent handle (a hash of an Ed25519 key, persisted on disk). A node
**advertises its onion inside its signed [advertisement](04-messages.md#node)**, so
peers learn it on the very first `hello`. When a node holds a peer's onion but does
not currently see it on the LAN, it **dials that onion over Tor** (with per-peer
exponential backoff). A Tor-dialed connection runs the **identical**
`hello`/`auth`/trust handshake and message pump a LAN link runs — so once up, a Tor
link is indistinguishable from a LAN link to everything above the socket
(dispatch, gossip, heartbeats, trust).

## What plugs in, and what doesn't

The link layer already consumes a bare `(reader, writer)` stream, so the transport
seam is tiny:

- **Inbound adds no protocol surface.** The onion service forwards its virtual port
  (`ONION_VIRTPORT`) to a small **dedicated loopback listener** the transport owns
  (`HiddenServicePort <ONION_VIRTPORT> 127.0.0.1:<forward-port>`), which hands the
  stream straight to the *same* accept path a LAN link uses
  ([03](03-transport.md#inbound-the-accepter)) and runs the same handshake. The
  dedicated listener (rather than reusing the node's shared TCP port) is what lets an
  inbound Tor connection be **tagged** as arriving over Tor — which the node uses to
  keep a Tor link's endpoint out of the LAN redial cache and to refuse operator
  control (`ctl`) sessions over the onion (see [Security notes](#security-notes)).
- **Outbound is the one new primitive:** a minimal SOCKS5 CONNECT through the local
  `tor` process's SOCKS port to `<peer-onion>:<ONION_VIRTPORT>`. The tunneled stream
  is handed to the same link pump a LAN dial uses.

## Address exchange

The advertisement gains one additive field, `onion` (a `<56-base32>.onion`
hostname), **omitted when empty** so a LAN-only or older node stays wire-identical.
Because the field rides **inside the signed advert**
([11 — authenticated gossip](11-trust-and-balancing.md#authenticated-gossip)), it is
bound to the advertiser's device key end to end: a relay cannot swap a peer's onion
to redirect a future Tor dial. (The onion is self-authenticating as well — it *is* a
public key — but the transport does not lean on that: after connecting, the peer
still proves its **device** key with the normal nonce `auth`, so a wrong or hijacked
onion simply lands the connection as an unverified, `foreign` peer.)

A node persists the onions it learns in `wan.json`, the shared WAN address cache
described in [15](15-iroh-transport.md#address-exchange) (keyed by node id, with the
device fingerprint it was paired with) — the WAN sibling of the LAN `peers.json`
redial cache ([08](08-state.md)). A node upgrading from a Tor-only release reads its
former `onions.json` once, so it does not forget peers it already knew. It is a best-effort accelerator: a
missing or stale entry costs at most a fenced dial.

## Reconnecting: reachability with backoff

A node auto-dials each known-but-unseen peer **it trusts as `personal`** by
**attempting the Tor dial and running the handshake** — it never *originates* a Tor
dial to a `foreign` peer. Auto-dialing a foreign, peer-advertised onion would let a
linked foreign peer aim your node at an arbitrary (third-party) onion it chose — a
dial reflector that also leaks your signed `hello` to that destination — so foreign
peers reach you **inbound** only (or by a deliberate manual paste, below). The same
[dial rule](02-discovery.md#the-dial-rule-smaller-id-dials) as the LAN applies
(only the smaller-id side auto-dials, so exactly one link forms per pair), and the
schedule is **per-peer exponential backoff**: each probe pre-schedules the next one
further out (doubling up to a ceiling), and a probe that **establishes a link resets
the schedule**, so a reachable peer that flaps reconnects promptly while an
unreachable one is probed ever more rarely. The reset keys on a *link actually
binding*, not on a bare TCP answer — an onion that answers but never completes the
handshake (a rotated join secret, a reassigned/squatted address) stays throttled
rather than being re-dialed every tick.

**What this means for the shipped `foreign` default.** Because auto-dial is
personal-only, two nodes that met on the LAN but have *not* promoted each other to
`personal` (the zero-trust default, [11](11-trust-and-balancing.md)) will **not**
auto-reconnect over Tor after they part — each sees the other as `foreign` and
neither originates the dial. This is by design and rarely bites in practice: a mesh
that actually *shares work* has already promoted the collaborating devices to
`personal` (a `foreign` peer's requests are declined-or-confined and it can never own
work — [13](13-foreign-execution.md)), and those personal peers **do** auto-reconnect
over Tor. To Tor-reconnect a pair you deliberately keep `foreign`, either promote one
side (`--trust` / the panel), run the fleet in full-altruism
(`SZPONTNET_DEFAULT_TRUST=personal`), or reach across with a one-shot manual paste
(below). A `foreign` server that takes work is reached the same way: its clients
originate the dial, so a client promotes the server it chose to use.

**No aggressive switching.** A peer that already holds a live link — over *either*
transport — is never probed or re-dialed. The LAN↔Tor quality gap is small, so a
Tor link is not torn down merely because the peer reappears on the LAN, and vice
versa; a link only changes on a genuine peer restart (a higher-epoch advert once the
old link has gone quiet), exactly as on the LAN today.

## Manual introduction (no prior LAN meeting)

You can reach a peer you were **never** on a LAN with by pasting its onion:

```
python -m szpontnet --tor-connect <hash>.onion
python -m szpontnet --connect <hash>.onion       # same dial, transport read off the shape
```

This dials the onion **unconditionally** (bypassing the smaller-id rule — it is a
deliberate one-shot). The handshake proceeds normally; from then on the peer is an
ordinary mesh member and its onion is cached like any other.

## Lifecycle & degradation

On start, with a `tor` binary present, the node spawns a
private `tor` (its own `SocksPort`, `DataDirectory`, and `HiddenServiceDir`, all
under `<mesh_dir>/tor/`, so several nodes on one host never collide). Bootstrap runs
**in the background** — the node is fully usable on the LAN meanwhile — and the
onion is advertised (a fresh gossip) once it is live. If the binary is missing,
bootstrap times out, or the onion never comes up, the node logs it and stays
**LAN-only** — the same graceful degradation as the keyless path when
`cryptography` is absent. The onion **key is persisted**, so the `.onion` address is
stable across restarts.

Degradation also extends **past** bootstrap: if the `tor` child later dies (crash,
OOM-kill), the node stops advertising and dialing the now-dead onion and reports
Tor as not-ready — it degrades back to LAN-only rather than claiming a WAN handle
that no longer answers. Conversely, `tor`'s lifetime is **tied to the node's**: it is
launched so the kernel terminates it if the node dies without a graceful shutdown
(SIGKILL / OOM), so an orphaned `tor` can't keep the `DataDirectory` lock and block
the next node's Tor bring-up.

## Security notes

- Tor gives the WAN link **transport confidentiality and integrity** for free (the
  onion circuit is end-to-end encrypted), which the plaintext LAN link does not
  have — but the mesh's **trust** decision does not depend on it either way: trust
  still keys only on the **verified device fingerprint**
  ([11](11-trust-and-balancing.md)), so a Tor peer is `foreign` until its
  fingerprint is in your allowlist, exactly like a LAN peer.
- The [join fence](03-transport.md#the-join-fence) (`SZPONTNET_SECRET`) applies
  unchanged over Tor: the secret check is transport-agnostic, and because a Tor
  circuit is encrypted the token is not exposed in transit (as it would be on the
  plaintext LAN).
- **Operator control (`ctl`) is never served over the onion.** The onion forwards
  only to the accept path, and that path serves *two* kinds of opener: peer links
  (`hello`) and the operator's local **control** channel (`ctl` — `status`,
  `dispatch`, `set-attr`, `trust`/`ban`, `set-default-trust`, `tor-connect`, `stop`).
  Only `hello` is meant to arrive from the network; `ctl` is the operator driving
  their *own* node over loopback. A connection arriving over Tor is therefore refused
  outright if it opens a `ctl` session — otherwise the full node-control surface would
  be reachable by anyone holding the advertised onion, and in an **open mesh** (no
  join secret — the documented home-LAN default) with no authentication at all. Peer
  linking, dispatch, gossip, and trust over Tor are unaffected; only the local admin
  channel is fenced off from the WAN.
- Enabling Tor advertises a stable onion to your mesh peers. Beyond that onion
  service — which forwards only peer links to the loopback accept path (control
  sessions refused, per the point above) — it does not expose the node to the open
  internet. Note the corollary of the join fence: on an **open** mesh, a Tor peer can
  still *link* and (subject to [trust](11-trust-and-balancing.md)) exchange gossip and
  dispatch with you from the WAN, exactly as an unauthenticated LAN peer could on the
  LAN. If that is not what you want, set a `SZPONTNET_SECRET`.

## Configuration

| Env | Meaning |
|-----|---------|
| `SZPONTNET_TOR=0` | Disable the Tor transport. On by default; `false`/`no`/`off`/empty also disable. Any other value leaves it on. |
| `SZPONTNET_TOR_BINARY` | Path to a non-PATH `tor` executable. |
| `SZPONTNET_TOR_BOOTSTRAP_SECS` | Bootstrap wait before giving up (default 90). |

## Conformance

A node that never runs an onion service is still conformant — the transport is
optional to *implement*, and everything above degrades to the LAN-only node in
[02](02-discovery.md)/[03](03-transport.md). What is normative is what goes on the
wire when it is implemented: `onion` inside the signed advert
([04](04-messages.md#nodeinfo)), omitted when empty; `ONION_VIRTPORT`; and the
refusal of `ctl` on a connection that arrived over the onion.

The reference implementation's own coverage runs at two altitudes — the node's Tor
decisions against an injected dialer, and the whole onion path against a real tor
daemon (a simulated onion network by default, the live Tor network on request). See
`szpontnet-core/tests/test_mesh_tor.py` and `test_tor_e2e.py`.
