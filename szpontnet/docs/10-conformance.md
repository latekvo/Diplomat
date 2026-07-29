# 10 - Conformance

This chapter defines what it means to *be a SzpontNet v1 node*: the mandatory
behavior, the optional roles, and interop test vectors you can check an
implementation against.

## Roles

A node fills one or more roles. Requirements scale with the roles claimed.

- **Participant** (mandatory floor): discoverable, linkable, gossips its
  advertisement, computes assignments. This is the minimum to *offer resources* to
  the mesh.
- **Executor** (to accept work): additionally handles inbound `dispatch` and runs
  jobs.
- **Dispatcher** (to originate work): additionally routes jobs with slot failover.
- **Controllable** (to be driven by a UI/CLI): additionally serves a control
  session.
- **Server** (an accept-only executor): an Executor + Controllable that **never**
  originates a dispatch to a peer (a request it is asked to route runs locally),
  and MAY require an [API key](11-trust-and-balancing.md#the-api-key) on inbound
  control and dispatch. A dedicated shared box that takes work but never pushes it.
- **Originator-with-dedup** (optional): a Dispatcher that also implements
  [work-claims](12-work-claims.md#conformance) — it claims a `workKey` before
  originating externally-triggered work and stands down when a peer already owns it,
  so two nodes watching the same source don't both run the work. A node that omits
  the role MUST still drop the `work-claim` message and keep the link ([09
  rule 2](09-extensibility.md#the-compatibility-contract)); the two interoperate,
  they just don't deduplicate against each other.
- **Confined-Executor** / **Result-Originator** (optional): an Executor that runs a
  **foreign** request's compute [confined and response-only](13-foreign-execution.md#conformance)
  and returns a `job-result`, and/or a Dispatcher that acts on a returned result
  under its own identity (`job-ack`ing it, acting at most once). As of v0.4.0 both
  halves also carry the [accountability](13-foreign-execution.md#accountability-deadline-reminder-ban)
  obligations: the executor answers a `job-reminder` truthfully (the result, or a
  `job-progress`) and marks a personal-path spawn `direct`; an originator that
  implements accountability arms the completion deadline, reminds only past it,
  judges a plea through its own extension decision, and bans only a device it
  classifies foreign. A node that omits the
  role declines foreign requests and MUST still drop the
  `job-result`/`job-ack`/`job-reminder`/`job-progress` messages and keep the link.

## Minimal node

A **Participant** - the smallest conformant node - **MUST**:

1. **Identity.** Have a stable, mesh-unique `id` that survives restart
   ([08](08-state.md#nodejson)); take a higher `epoch` each process start.
2. **Beacon.** Emit a valid [`beacon`](04-messages.md#beacon) to the multicast group
   (and, off loopback, subnet broadcast) every `beaconIntervalSecs`, advertising its
   real TCP port ([02](02-discovery.md)).
3. **Receive beacons.** Join the multicast group, parse beacons, dedupe multicast vs
   broadcast, ignore its own, and apply the [dial rule](02-discovery.md#the-dial-rule-smaller-id-dials)
   (smaller id dials; exactly one link per pair; guard against double-dials).
4. **Link.** Listen on TCP (first free port in the range), speak
   [NDJSON framing](03-transport.md#framing) with the 512 KiB line cap, and complete
   the [hello handshake](03-transport.md#link-lifecycle) in both directions.
5. **Join fence.** If a secret is configured, enforce it on the opening
   `hello`/`ctl` **and** enforce the [authentication-ordering rule](03-transport.md#the-join-fence)
   (an unauthenticated dialed link accepts nothing but a valid hello first).
6. **Advertise.** Put a well-formed [NodeInfo](04-messages.md#nodeinfo) in its hello
   and keep it current.
7. **Heartbeat & liveness.** Send [`heartbeat`](04-messages.md#heartbeat) every
   `heartbeatIntervalSecs`; derive `up`/`stale`/`down` from `peerStaleSecs`/
   `peerTimeoutSecs` using a **monotonic** clock; mark a peer `down` on timeout.
8. **Gossip.** Merge incoming [`node`](04-messages.md#node) and
   [`overrides`](04-messages.md#overrides) by freshness/LWW, re-propagate only
   genuinely newer information, and never re-propagate stale info.
9. **Assign.** Compute [`assign_all`](06-coordination.md#the-assignment-algorithm)
   deterministically over the live set - identical output to any other conformant
   node with the same inputs - and recompute on every relevant change.
10. **Tolerate.** Ignore unknown fields, drop unknown message types, never crash on
    malformed input ([09](09-extensibility.md#the-compatibility-contract)).

An **Executor** additionally **MUST** handle inbound [`dispatch`](04-messages.md#dispatch)
on authenticated links and reply with a truthful
[`job-status`](04-messages.md#job-status) (`spawned`/`failed`, and `declined` if it
implements the [trust/refusal layer](11-trust-and-balancing.md)). A Dispatcher
**MUST** treat any non-`spawned` status - including a `declined` it doesn't
understand - as a failover trigger, never an error.

An Executor that implements the trust layer additionally **MUST**: verify a peer's
**proof of possession** - an [`auth`](04-messages.md#auth) signature over the
**domain-separated** form of the executor's own fresh hello nonce, validated against
the peer's advertised `pubkey` - before treating that peer as `personal`; **classify
the requester from that verified link identity**, never from the job's `requestedBy`
(which is spoofable); **ignore a peer-link `set-attr` from a foreign device**
(mutation is a personal-only action); and treat an **empty allowlist as full trust**
(every verified peer `personal`). A **Server** with an API key **MUST** require a
matching `apiKey` on inbound `ctl`/`dispatch`, and in server mode **MUST NOT**
originate a dispatch to a peer. See
[11-trust-and-balancing](11-trust-and-balancing.md) for the full conformance list.

A **Dispatcher** additionally **MUST** route a job via
[`slot_candidates`](07-dispatch.md#slots) with per-slot failover, one node per slot,
honoring the `dispatchAckTimeoutSecs` wait for remote replies.

A **Controllable** node additionally **MUST** accept a [`ctl`](04-messages.md#ctl)
session (fenced by the secret) and handle `status`, `set-attr`, `set-overrides`,
`dispatch`, and `stop`.

## SHOULD / MAY

- A node **SHOULD** emit a startup beacon immediately (don't wait a full interval).
- A node **SHOULD** re-dial a peer that beacons a higher `epoch` (restart), subject
  to the live-link guard below.
- A node **SHOULD** retain a `down` peer in its snapshot for the retention window.
- A node **SHOULD** warn (once) on a [cloned identity](08-state.md#cloned-identity).
- A node **SHOULD** bound its peer table against growth from **both** a beacon
  flood and a gossip flood — the cap must apply to peers created by relayed `node`
  gossip, not only the beacon/dial path (the reference caps both and stops learning
  new ids past the cap).
- A node **SHOULD** not let an unauthenticated beacon evict a live, verified link:
  honor a higher-`epoch` restart hint only once the link has actually gone quiet,
  and never let a beacon overwrite a live peer's address (a spoofed beacon must not
  hijack a healthy link).
- A node **MAY** persist a `state.json` snapshot and expose a control endpoint even
  if it isn't otherwise Controllable (useful for local tooling).
- A node **MAY** offer a loopback-only mode and timing overrides for testing.

## Interop test vectors

These are behaviors a second implementation can check against the reference. The
reference asserts all of them in
[`test_mesh_logic.py`](../../linux/tests/test_mesh_logic.py) (pure) and
[`test_mesh_node.py`](../../linux/tests/test_mesh_node.py) (real sockets).

### V1 - placement (pure, no sockets)

Fleet `A`=linux/tier4/ok, `B`=macos/tier1/ok, `C`=macos/tier4/ok, all duties
enabled, default policies. No node advertises `stats`, so the default `surplus-first`
ranks all at `NEUTRAL_SURPLUS` and orders exactly as `weakest-first`:

| Input | Expected `assigned` |
|-------|---------------------|
| `review` (default `surplus-first`, no stats → weakest-first) | `[A]` |
| `conflicts` | `[A]` |
| `audit` (spread 1×linux+1×macos) | `[A, C]` |
| `review`, override `strongest-first` | `[B]` |
| `audit` with only `{B,C}` | `[C]`, shortfall `[{linux,1}]` |
| `review`, `A` set `tokens:out` | `[C]` |
| `review`, `A` set `tokens:low` (others ok) | `[C]` |
| any duty, empty fleet | `[]`, unsatisfied |

**Permutation invariance:** shuffling the input node order MUST NOT change any
assignment.

### V2 - codec round-trips

- `encode`→`decode` of every message type yields an equal object (modulo the
  defaulted `v`).
- `decode` returns "drop" for: empty input; non-JSON; a JSON array (non-object); an
  object with no string `t`; invalid UTF-8; a line longer than 512 KiB.
- A NodeInfo with no `id` is invalid; one with a non-numeric `tier` is invalid; one
  with only `id` fills all other fields with defaults.

### V3 - freshness & LWW

- For one `id`, `(epoch=200, seq=1)` supersedes `(epoch=100, seq=50)` (epoch wins
  over seq); within an epoch, higher `seq` wins.
- Overrides LWW: higher `rev` wins; equal `rev` breaks on `updatedBy`; the winner is
  the same on every node.

### V4 - live multi-node (real sockets)

Booting three real nodes on loopback:

- all three link to both peers (discovery converges);
- all three publish **identical** assignments (deterministic agreement);
- an `audit` dispatch spawns on exactly the two assigned machines;
- setting the weak macOS node `tokens:out` moves the audit's macOS slot to the
  strong macOS node, and a re-dispatch lands there (dispatch-time failover);
- an override set on one node converges on all nodes;
- killing a node moves its duties on every survivor and marks it `down`;
- restarting a node re-links as a new incarnation.

### V5 - the join fence

- With a secret set, a wrong-secret node never links; a wrong-secret control client
  can't drive the node.
- **Critical:** a spoofed beacon that induces a dial, followed by a naked
  `dispatch` (no hello, no secret), MUST NOT spawn anything - the
  [authentication-ordering rule](03-transport.md#the-join-fence) rejects it. (The
  reference test `test_outbound_dial_fence_rejects_naked_dispatch` drives exactly
  this and fails if the gate is removed.)

### V6 - work-claims (real sockets, optional role)

For the [Originator-with-dedup](#roles) role, over two live nodes:

- the lower-id node dispatches a `workKey`, claims it, and runs the work; the
  higher-id node dispatching the **same** `workKey` gets a `"suppressed"` result and
  runs **nothing** (origination dedup);
- a **forged** claim (bad `sig`) is dropped, so it does not suppress a later
  dispatch of that key;
- a **keyless** or **foreign** claimant never suppresses (anti-starvation);
- when the **owner is killed**, its lease lapses within `peerTimeoutSecs` and the
  survivor's next dispatch of that key is no longer suppressed - it takes the work
  over. (The reference asserts the live path in
  `test_work_claim_dedupes_origination_and_frees_on_owner_death` and the logic in
  `test_mesh_logic.py`'s work-claims section.)

### V7 - foreign zero-trust execution (real sockets, optional role)

For the [Confined-Executor / Result-Originator](#roles) roles, over two live nodes
where the requester is **foreign** to the executor (the executor trusts only itself)
and the executor has a confinement runner configured:

- the executor **accepts** (`spawned`) rather than declining, and runs the compute in
  the confinement runner (**not** the host spawn path) on the response-only prompt;
- the executor returns the computed artifact as a **signed** `job-result` on the
  requester's link; the requester **`job-ack`s** it and performs the social action
  itself, under its own identity, **exactly once**;
- a `job-result` from the **wrong link**, or a keyed executor's result with a
  **bad/absent signature**, is **dropped** (no ack, no action);
- the executor **re-sends** the unacked result until the ack lands, then stops. (The
  reference asserts the live path in
  `test_foreign_request_runs_confined_and_routes_result_back` and the logic in
  `test_mesh_logic.py`'s foreign-execution section.)

## Interop checklist (quick)

- [ ] Two independent nodes discover each other and form one link.
- [ ] They exchange hellos and each shows the other in its snapshot.
- [ ] Both compute identical assignments for the shared duty set.
- [ ] Killing one moves duties and marks it down on the other within
      `peerTimeoutSecs`.
- [ ] A dispatch routes to the assigned node and reports `spawned`.
- [ ] With a secret set, a node without it cannot join or dispatch.
