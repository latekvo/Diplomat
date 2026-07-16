# 02 — Discovery

Discovery is how a node learns that other nodes exist and where to reach them. It
is UDP-only, connectionless, and best-effort: a lost beacon costs nothing because
the next one follows in a couple of seconds.

## Beacons

A node **MUST** periodically emit a **beacon** — a UDP datagram carrying one
[`beacon` message](04-messages.md#beacon) — announcing its id, name, platform,
the TCP port it listens on, and its current incarnation `epoch`.

- The beacon interval is `beaconIntervalSecs` (default **2.0 s**;
  [appendix B](appendix-b-constants.md)).
- A node **SHOULD** also emit a beacon promptly on startup (don't wait a full
  interval) so a fresh node is discovered quickly.
- A beacon **MUST** fit in a single datagram. It is tiny (well under 512 bytes);
  receivers read with a buffer of at least 2048 bytes and **MUST** silently drop
  anything larger or unparseable.

### Why the beacon carries the TCP port

Discovery (UDP) and links (TCP) are separate. The beacon is the only place a peer
learns which TCP port to dial, which is essential because a host may run several
nodes that each bind a *different* port from the shared range
([03-transport](03-transport.md#binding)). A receiver **MUST** ignore a beacon
whose `tcpPort` is missing or not a positive integer.

## Transport: multicast plus broadcast

A node **MUST** send each beacon to the multicast group and, when not restricted
to loopback, **SHOULD** also send it to the subnet broadcast address:

- **Multicast**: group `multicastGroup` (default `239.83.77.7`), port
  `multicastPort` (default `40877`), with `IP_MULTICAST_TTL` = 1 (link-local; the
  mesh is a single LAN) and multicast loopback enabled (so co-located nodes hear
  each other).
- **Broadcast**: `255.255.255.255` on the same port, with `SO_BROADCAST` set.

Sending *both* is deliberate: consumer Wi-Fi access points frequently drop
multicast between wireless clients, or (less often) drop directed broadcast.
Emitting both and having receivers **dedupe** maximizes the chance a beacon is
heard. A receiver **MUST** treat multicast and broadcast copies of the same
beacon as one (dedupe by `id`); receiving the same beacon twice **MUST NOT**
create two peers or two links.

> **Interop note.** If your network blocks *all* client-to-client traffic (some
> "client isolation" Wi-Fi configurations do), neither discovery nor the TCP links
> can work — SzpontNet needs a LAN that permits client-to-client traffic. A wired
> switch or an AP with client isolation disabled is the fix. If only the
> *beacon channel* is blocked (multicast/broadcast filtered while unicast still
> flows — also seen when an OS privacy gate such as macOS 15's Local Network
> permission fails every LAN `sendto` with `EHOSTUNREACH`), first-contact
> discovery still cannot work, but peers that have met before recover via
> [redial from memory](#redial-from-memory). A node **SHOULD** detect that every
> beacon send is failing and surface it to the operator
> ([08-state](08-state.md#the-statejson-snapshot) `beaconBlocked`) rather than
> fail silently — the node is undiscoverable while it lasts.

## Receiving

A node **MUST** listen on the multicast port for beacons:

- Bind UDP to the wildcard address on `multicastPort` with `SO_REUSEADDR` (and
  `SO_REUSEPORT` where available — required so several nodes on one host can each
  join the group; see below).
- Join the multicast group (`IP_ADD_MEMBERSHIP` for `multicastGroup`).
- Directed broadcast to the same port arrives on the same socket.

For each received datagram the node parses it as a [message](04-messages.md); if
it is not a well-formed `beacon`, drop it. Otherwise:

1. Ignore a beacon whose `id` equals the local node id (that is our own beacon
   looped back — but see [08-state](08-state.md#cloned-identity) for the case
   where a *different host* advertises our id).
2. Record/refresh the peer's last-known address (the datagram's source IP) and
   TCP port.
3. Decide whether to **dial** (below).

## The dial rule: smaller id dials

To guarantee **exactly one TCP link per pair of nodes** with no races, SzpontNet
uses a deterministic tie-break: **the node whose id sorts lexicographically
*smaller* initiates the TCP connection.** Concretely, on hearing a beacon from a
peer:

- If `local_id < peer_id` **and** there is no existing (or in-progress) link to
  that peer, the local node **MUST** attempt to dial the peer's advertised
  `address:tcpPort`.
- If `local_id > peer_id`, the local node **MUST NOT** dial; it waits for the peer
  to dial *it*. (Its own beacons will prompt the peer to dial.)

Because ids are unique, exactly one side of every pair satisfies the `<`
condition, so exactly one dial happens. An implementation **MUST** guard against
dialing the same peer twice concurrently (beacons repeat faster than a handshake
completes): hold a per-peer "dialing" marker for the whole life of a dial attempt
*and* the link it produces, and skip a new dial while it is held.

A node **SHOULD** re-dial when a beacon indicates the peer **restarted** — i.e.
the beacon's `epoch` is greater than the epoch of the currently-linked
incarnation — by dropping the stale link and dialing the new incarnation. See
[08-state](08-state.md#liveness--incarnations).

## Redial from memory

Beacons are the *normal* (re)dial trigger, but they ride multicast/broadcast — a
channel that can silently die under a live mesh while unicast keeps working:
consumer APs filter multicast between wireless clients, and OS privacy gates can
start failing every LAN send on one node (macOS 15's Local Network permission
denies them with `EHOSTUNREACH`). A mesh that has already formed then loses a
dropped link *forever*: nothing ever re-triggers the dial.

To heal this, a node **SHOULD** remember the last address each peer was actually
reached at — the source IP of an authenticated `hello` received on the peer's own
link plus the TCP port that hello advertised, **never** a beacon's contents (a
beacon is unauthenticated and would poison the cache) — persist it across
restarts (the reference implementation keeps `peers.json` next to `node.json`),
and periodically attempt a direct dial of every remembered peer that is currently
unlinked, every `redialIntervalSecs` (default **10.0 s**;
[appendix B](appendix-b-constants.md)).

Redial obeys the same rules as a beacon-triggered dial: only the smaller id
dials, the in-progress dial guard applies, and the [hello
fence](03-transport.md#outbound-the-dialer) authenticates whoever answers. A
stale or wrong cache entry therefore costs one failed (or fenced) dial per
interval and nothing else; a peer whose address changed is re-learned from its
next authenticated hello (or its beacons, where those still flow).

## Several nodes on one host

Running multiple nodes on one machine (useful for testing, and not forbidden in
production) requires:

- `SO_REUSEADDR` + `SO_REUSEPORT` on the receive socket so each node can bind the
  shared multicast port and independently join the group;
- multicast loopback enabled on the send socket so co-located nodes hear each
  other;
- each node binding a *distinct* TCP port from the range
  ([03-transport](03-transport.md#binding)) and advertising it in its beacon.

The reference implementation's integration tests run three-node meshes entirely on
`127.0.0.1` this way.

## Loopback-only mode

An implementation MAY offer a "loopback-only" mode (the reference one keys it off
`ARGENT_MESH_LOOPBACK=1`) that pins every socket to `127.0.0.1`, skips the
subnet-broadcast copy, and sets the multicast interface to loopback. This is for
running a self-contained mesh on one machine without touching the LAN; it is not
part of the wire protocol and needs no agreement between nodes.

## What discovery does *not* do

Discovery only *locates* peers and triggers link setup. It carries no resource
advertisement beyond the identity needed to dial (id, name, platform, port,
epoch). The full resource advertisement travels on the TCP link in the
[`hello`](04-messages.md#hello), not in the beacon — keeping beacons tiny and the
authoritative advertisement on the authenticated, reliable channel.
