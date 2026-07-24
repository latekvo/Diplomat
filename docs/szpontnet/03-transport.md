# 03 — Transport & security

Once discovery has located a peer, all real work — exchanging advertisements,
heartbeats, gossip, and dispatches — happens over a **TCP link**. Control clients
(UIs, CLIs) use the same TCP port.

## Binding

A node **MUST** listen on TCP for links and control sessions. It binds the first
free port in the shared range: from `tcpPortBase` (default `40878`) through
`tcpPortBase + tcpPortSpan - 1` (default span `10`, i.e. `40878`–`40887`). The
first port it successfully binds becomes its listen port, and it **MUST** advertise
exactly that port in its [beacons](02-discovery.md#beacons). Binding the first
free port (rather than a fixed one) is what lets several nodes share a host.

Off loopback, bind the wildcard address (`0.0.0.0`); in loopback-only mode bind
`127.0.0.1`.

## Framing

The link protocol is **NDJSON**: each message is one JSON object, serialized
without interior newlines, terminated by a single `\n` (`0x0A`). A reader
accumulates bytes until a newline, parses the line as one message, and continues.

- A message **MUST** be valid UTF-8 JSON encoding an object with a string `t`
  field (the message type). Anything else — non-JSON, a non-object, a missing/
  non-string `t` — **MUST** be dropped (skip the line; do not close the link).
- Implementations **MUST** enforce a maximum line length of `MAX_LINE_BYTES`
  (**512 KiB**, `524288`). A line exceeding it is dropped and the link **MAY** be
  closed. The limit is measured on the encoded line **including** its terminating
  `\n`, so the largest accepted JSON object payload is `MAX_LINE_BYTES - 1`
  (`524287`) bytes. 512 KiB is generous headroom — a dispatch prompt is the only
  large payload and is typically a few KiB — not a semantic limit.
- Every message carries a protocol version `v` (integer). Senders set it (default
  `1`); receivers **MUST** tolerate messages with a `v` they don't recognize by
  applying the [compatibility rules](09-extensibility.md) (ignore unknown fields;
  drop unknown types) rather than failing.

See [04-messages](04-messages.md) for the full type catalog.

## Link lifecycle

A link is created by discovery's [dial rule](02-discovery.md#the-dial-rule-smaller-id-dials):
the smaller-id node connects; the larger-id node accepts.

### Outbound (the dialer)

1. Open the TCP connection to the peer's advertised `address:tcpPort`.
2. Immediately send a [`hello`](04-messages.md#hello) carrying this node's full
   resource advertisement and its current placement overrides (and the shared
   secret, if configured).
3. Enter the message pump as an **unauthenticated** link (see
   [the join fence](#the-join-fence)).

### Inbound (the accepter)

1. Accept the connection and read the first line.
2. The first message determines the connection kind:
   - `t: "hello"` → a **peer link**. Validate the secret (below), reply with our
     own `hello`, process the peer's hello, then enter the message pump as an
     **authenticated** link.
   - `t: "ctl"` → a **control session** (validate the secret, then handle
     [control messages](04-messages.md#control-messages)).
   - anything else → close the connection.

### Message pump

While the link is open, each side reads messages and dispatches on `t`
([04-messages](04-messages.md) enumerates the handlers). Every message received
refreshes the peer's liveness timestamp. The link ends on EOF, a socket error, an
over-length line, or a protocol violation (below). On teardown the node **MUST**
only tear down the *peer* if the writer being closed is still that peer's current
link — a reconnect may already have replaced it.

## The join fence

SzpontNet supports an optional **pre-shared secret** that fences off who may join
the mesh and receive work. It is configured out of band (the reference
implementation reads `DIPLOMAT_MESH_SECRET`; every node and every control client
must carry the same value). When a secret is configured:

- A `hello` or `ctl` message **MUST** carry a `secret` field equal to the
  configured value. The accepter **MUST** drop the connection immediately if the
  opening `hello`/`ctl` does not present the matching secret.
- **Authentication ordering (critical).** An **outbound-dialed** link is,
  before the peer's hello arrives, talking to *whoever answered a beacon* — and a
  beacon can be spoofed by anything on the LAN. Therefore an unauthenticated link
  (one that has not yet seen a valid hello) **MUST NOT** process any message other
  than a `hello`. The first message on such a link **MUST** be a `hello` carrying
  the matching secret; any other first message (a bare `dispatch`, `set-attr`,
  `overrides`, …) **MUST** cause the link to be torn down. Only after a valid,
  correctly-secreted hello is the link authenticated and other message types
  accepted.

  > This rule is load-bearing. Without it, an attacker who spoofs a beacon to make
  > a victim *dial* them could send a `dispatch` on the resulting link and have the
  > victim run arbitrary work — bypassing the fence entirely, because the fence's
  > other check only fires on the *accepter's* first line, not on the dialer's
  > link. The reference implementation and its regression test
  > (`test_outbound_dial_fence_rejects_naked_dispatch`) exist specifically to lock
  > this down.

- The `secret` value rides in plaintext on the LAN. It is a **fence, not
  cryptography**: it keeps a stray machine, or a colleague's separate mesh on the
  same office network, from joining yours and receiving jobs. It does **not**
  defend against an attacker who can read LAN traffic. For a home LAN the default
  (no secret, open mesh) is fine; on a shared network, set one.

When **no** secret is configured (the open-mesh default), the same ordering rule
still applies (first message on any link MUST be a hello), with the secret check
reduced to "the empty secret matches the empty secret" — i.e. any node may join,
but a link still can't skip the hello handshake.

## Link state

Each peer link has a **state** derived from how recently a message was received on
it, using two thresholds from the shared model:

| State | Condition | Meaning for placement |
|-------|-----------|-----------------------|
| `up` | last message within `peerStaleSecs` (default **5 s**) | fully live |
| `stale` | last message older than `peerStaleSecs` but within `peerTimeoutSecs` (default **10 s**) | still counts — a stale peer **retains its duties** |
| `down` | no live link, or last message older than `peerTimeoutSecs` | excluded — duties reassign |

A **stale** peer is deliberately still considered part of the mesh for placement:
a brief Wi-Fi hiccup should not bounce duty ownership. Only a full **timeout**
(the peer goes `down`) removes it from the assignment input and moves its work.
See [06-coordination](06-coordination.md#the-live-node-set) and
[08-state](08-state.md#liveness--incarnations).

To keep links alive and detect death, each node **MUST** send a
[`heartbeat`](04-messages.md#heartbeat) on every link every
`heartbeatIntervalSecs` (default **2 s**), and **MUST** mark a peer `down` (and
recompute assignments) once no message has been seen from it for
`peerTimeoutSecs`. A node **SHOULD** keep a downed peer visible in its
[state snapshot](08-state.md) for a retention window (reference: **300 s**) marked
`down`, so observers see *what* died rather than a silently shrinking list, then
drop it.

## Gossip fan-out

The TCP links form the gossip fabric. When a node's own advertisement changes (an
attribute edit, or its set of live links changes), or when it adopts a newer
placement override, it **MUST** propagate the change to every currently-linked
peer (a `node`/`overrides` message). Peers merge by freshness
([04-messages](04-messages.md#node), [06](06-coordination.md#placement-overrides))
and re-propagate genuinely newer information, so a change reaches the whole mesh in
O(diameter) hops. A node **MUST NOT** re-propagate information that is not newer
than what it already holds, or gossip will not converge.

Because a relay is just another peer, a gossiped advertisement/override is
**self-signed by its originator** and **verified before it is adopted or
re-propagated**, and a relay forwards it **verbatim** so the signature survives the
hop — a relay can neither forge nor tamper with another node's gossip. This is
specified in [11 - authenticated gossip](11-trust-and-balancing.md#authenticated-gossip).
