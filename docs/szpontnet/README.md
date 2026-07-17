# SzpontNet - a LAN peer-to-peer resource-sharing protocol

**Specification v0.3.0** (wire `v: 1`). This directory is the normative specification for
**SzpontNet**: a small, leaderless protocol that lets the machines on a local
network find each other, **advertise the resources they have available**, and
hand work to whichever machine is the best fit - with no central coordinator and
automatic take-over when a machine drops.

SzpontNet is the *protocol*. **Co-Maintainer Mesh** (in this repository under
[`linux/co_maintainer/mesh/`](../../linux/co_maintainer/mesh)) is its reference
implementation; the shared constants live in
[`core/mesh.json`](../../core/mesh.json). This spec is written so that a second,
independent implementation - in any language - can join the same mesh and
interoperate byte-for-byte with the reference one.

> The name is deliberately informal (Polish *szpont/spont* - "on a whim",
> impromptu): you power a machine on, it spontaneously joins, offers what it has,
> and takes on work. No registration, no server, no config beyond an optional
> shared secret.

---

## What it does, in one paragraph

Every participating machine runs a **node**. A node **beacons** its presence over
UDP (multicast + broadcast). Nodes that hear each other open a single **TCP link**
per pair and exchange a **hello** carrying that node's *resource advertisement* -
what platform it is, how strong a machine it is (its *tier*), how much budget it
has left (its *token* state), and which classes of work (*duties*) it is willing
to run. Nodes gossip these advertisements so every node holds the same view of the
mesh. Because every node runs the **same deterministic placement function** over
that shared view, they all agree - with no election - on which machine owns each
duty; when a machine dies or runs dry, every survivor has *already* recomputed and
the work has moved. Any node can then **dispatch** a job, and the mesh routes it to
the chosen machine(s), failing over if the first pick can't take it.

---

## Design goals

1. **Zero configuration.** A node self-discovers peers and self-assigns work.
   The only optional knob is a shared secret to fence off who may join.
2. **Leaderless and self-healing.** No coordinator to elect or lose. Placement is
   a pure function of the gossiped state, so all nodes converge on the same answer
   and failover is automatic.
3. **Resource-advertisement first.** A node's whole purpose on the wire is to say
   *"here is what I can do"*; the protocol is the machinery that turns those
   advertisements into placement decisions.
4. **Trust scoped to devices you have explicitly trusted.** A node runs a request
   from a **personal** peer (a device whose key you've added to a local allowlist)
   directly, as if you'd triggered it locally; a request from any other
   (**foreign**) device is declined, or run confined via the [zero-trust foreign
   path](13-foreign-execution.md). Trust is proven by a device keypair, never
   inferred from a spoofable advertised field.
   With no allowlist configured every peer is personal, so this reduces to full
   altruism. See [11-trust-and-balancing](11-trust-and-balancing.md).
5. **Extensible without breaking changes.** The trust model, the resource
   vocabulary, the duty catalog and the placement strategies are all designed to
   grow - in particular so that **limits on altruism** (quotas, caps, priorities,
   accounting) can be added later without any v1 node needing to change. The rules
   that make this safe are normative; see [09-extensibility](09-extensibility.md).
6. **Tolerant.** Unknown fields are ignored, unknown message types are dropped,
   malformed input is never fatal. A newer node must never wedge an older one.

---

## The trust model: personal vs foreign

A **personal** SzpontRequest runs *directly* on the receiver, spawning work that
can take social actions under your identity (open a PR, comment on GitHub). So
trust must be unforgeable. SzpontNet never derives it from an advertised field -
**assume every advertisement is spoofed** - but from a proven device key plus a
local allowlist ([11-trust-and-balancing](11-trust-and-balancing.md)):

- Each node has an **Ed25519 device keypair**; its public key is advertised, but
  advertising it grants nothing. On each link the peer must **sign a fresh
  challenge**, proving it holds the private key for the fingerprint it claims.
- You **manually trust** a device by adding its fingerprint to a **local
  allowlist** (never gossiped). A peer whose *verified* fingerprint is on your
  list is **personal**; anyone else is **foreign**.

A personal peer's requests run directly, as if you'd pressed the button on that
machine yourself. A **foreign** peer's requests are **declined** by default; a node
that opts in with a [confinement runner](13-foreign-execution.md) instead runs the
*compute* sandboxed and routes any social action back to a personal node of the
requester before it happens - zero trust for the stranger, full trust preserved for
you.

The boundary is opt-in and safe by default: **an empty allowlist means full
trust** (every peer personal), so a fresh mesh behaves precisely as the trusting
core did - until you trust a first device. The join fence
([03-transport](03-transport.md#the-join-fence)) still gates *who may join*; trust
is the finer question of *whose requests a node will act on*, and unlike the shared
join secret it distinguishes individual devices.

**Load balancing and refusals.** Beyond eligibility, a dispatcher picks a target
by **surplus** - the node with the most spare quota, account-type aware (Max 5x vs
20x) - computed from a 21-day usage average and remaining quota that each node
advertises. The choice is the dispatcher's alone (no consensus): it may even
forward everything to one peer, and that peer may **refuse** (a first-class
`declined` outcome). The remaining knobs on altruism - per-peer caps, priority
classes, cost accounting - stay reserved as additive extension points in
[09-extensibility](09-extensibility.md#the-altruism-limits-roadmap).

**Who this is for, and the two shapes it takes.** SzpontNet's point is to let you
**defer your routine duties to a peer** when local execution isn't the best fit -
because a peer has a stronger machine, more quota left, or a platform you're missing
for a test. Participation is **100% voluntary and altruistic**: among a group of
work colleagues, everyone offers spare capacity and no one is obliged, which
**smooths out each person's quota spikes** across the group. Two deployments are
supported: the default **peer-to-peer** mesh where every node both offers and
dispatches, and a **dedicated server** ([11](11-trust-and-balancing.md#server-nodes--api-key-authentication)) -
one machine that *accepts* work but *never dispatches*, optionally gated by an
**API key** on inbound requests. Both interoperate on the same mesh with plain
peers.

---

## Versioning

The **specification** is versioned with [zerover](https://0ver.org/) (`0.MINOR.PATCH`)
while the protocol stabilizes: every substantive change **bumps the minor**
(`v0.1.0` → `v0.2.0` → …), and the patch digit is reserved for editorial fixes that
don't change behavior. The major stays `0` until the protocol is declared stable.

| Spec version | Adds |
|--------------|------|
| **v0.1.0** | discovery, links, gossip, deterministic placement, dispatch/failover, the personal/foreign trust model, server + API key, and authenticated gossip (signed advertisements + overrides). |
| **v0.2.0** | [work-claims](12-work-claims.md) — leaderless origination-dedup leases. |
| **v0.3.0** *(this revision)* | [foreign zero-trust execution](13-foreign-execution.md) — confined compute for a foreign request, with the result returned (`job-result`/`job-ack`) for the originator to act on. |

This is **separate from the wire `v` field**, which every message carries
([04](04-messages.md)). That field is the **wire-compatibility version**, a single
integer used by the [compatibility contract](09-extensibility.md#the-compatibility-contract)
to gate breaking changes; it is still **`1`** and does not move for a
backward-compatible, additive spec revision (work-claims are additive, so `v` stays
`1`). Where the prose below says "v1," it means that wire generation — the behavior a
`v: 1` node implements — not the spec's own version number.

## How to read this spec

The chapters are ordered so you can implement bottom-up:

| # | Chapter | What you implement from it |
|---|---------|----------------------------|
| - | [README](README.md) (this file) | the mental model, goals, trust model |
| 01 | [Model & terminology](01-model.md) | the nouns: node, advertisement, duty, placement, dispatch |
| 02 | [Discovery](02-discovery.md) | UDP beacons, the multicast/broadcast pair, the "smaller id dials" rule |
| 03 | [Transport & security](03-transport.md) | TCP links, NDJSON framing, the link state machine, the join fence |
| 04 | [Message reference](04-messages.md) | every message type, field by field, with JSON schemas |
| 05 | [Resource advertisement](05-resources.md) | what a node advertises and how the vocabulary extends |
| 06 | [Coordination & assignment](06-coordination.md) | the deterministic placement algorithm (with pseudocode) |
| 07 | [Dispatch](07-dispatch.md) | routing a job, slot fan-out, failover, results |
| 08 | [State & persistence](08-state.md) | `node.json`, `state.json`, liveness, incarnations |
| 09 | [Extensibility & future work](09-extensibility.md) | the compatibility rules + the altruism-limits roadmap |
| 10 | [Conformance](10-conformance.md) | MUST/SHOULD/MAY, a minimal-node checklist, interop vectors |
| 11 | [Trust & load balancing](11-trust-and-balancing.md) | personal/foreign trust, per-node quota stats, surplus-first dispatch, refusals |
| 12 | [Work claims](12-work-claims.md) | leaderless origination dedup: who runs externally-triggered work, and failover when the owner dies |
| 13 | [Foreign execution](13-foreign-execution.md) | zero-trust: run a foreign request's compute confined, return the result for the originator to act on |
| A | [Appendix A - annotated trace](appendix-a-trace.md) | a full two-node session, message by message |
| B | [Appendix B - constants](appendix-b-constants.md) | every default value in one table |

### Notation

- The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, **MAY** are
  used as in [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119): they mark
  interoperability requirements, not implementation advice.
- Wire examples are JSON. On the wire each message is a single line of compact
  JSON (no interior newlines) terminated by `\n` - see
  [03-transport](03-transport.md#framing). Examples here are shown pretty-printed
  for readability; the newline-free encoding is what actually travels.
- `int`, `float`, `string`, `bool`, `object`, `array` refer to JSON types.
- Field names are given exactly as they appear on the wire (they are
  case-sensitive).

### The fastest path to a working node

A minimal conformant node needs, in order: a UDP beacon sender + listener
([02](02-discovery.md)), a TCP listener + dialer speaking NDJSON
([03](03-transport.md)), the `beacon`/`hello`/`node`/`heartbeat` messages
([04](04-messages.md)), the resource advertisement it puts in its hello
([05](05-resources.md)), and the placement function ([06](06-coordination.md)).
Dispatch ([07](07-dispatch.md)) and the control endpoint are optional for a node
that only wants to *offer* resources. The exact minimal set is enumerated in
[10-conformance](10-conformance.md#minimal-node).

---

## Relationship to the reference implementation

Everything in this spec is implemented and exercised by Co-Maintainer Mesh:

- Wire protocol & node: [`linux/co_maintainer/mesh/`](../../linux/co_maintainer/mesh)
  (`protocol.py`, `node.py`, `assign.py`, `identity.py`, `statefile.py`, `ctl.py`).
- Shared constants & vocabulary: [`core/mesh.json`](../../core/mesh.json).
- Interop-relevant behavior is covered by
  [`linux/tests/test_mesh_logic.py`](../../linux/tests/test_mesh_logic.py) (the
  placement function) and
  [`linux/tests/test_mesh_node.py`](../../linux/tests/test_mesh_node.py) (real
  multi-node sockets: discovery, gossip, failover, the join fence).

Where this spec and the reference implementation disagree, that is a bug in one
of them; please report it. The spec is the interoperability contract.
