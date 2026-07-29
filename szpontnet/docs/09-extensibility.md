# 09 - Extensibility & future work

SzpontNet v1 is deliberately small, but it is designed to grow. This chapter is the
normative contract for evolving it **without breaking changes**, and then applies
that contract to the one extension the brief calls out explicitly: **adding limits
to the v1 full-altruism model**.

## The compatibility contract

Every conformant implementation **MUST** obey these rules. Together they guarantee
that a node speaking a newer minor revision and a node speaking an older one keep
interoperating.

1. **Ignore unknown fields.** A receiver MUST ignore object fields it does not
   recognize, at every level (message, NodeInfo, Job, placement, overrides). It
   MUST NOT reject a message for carrying extra fields. This is what makes new
   optional fields safe to add.

2. **Drop unknown message types, don't die.** A message whose `t` the receiver
   doesn't handle MUST be dropped silently; the link stays open. New message types
   are therefore additive - old nodes simply don't act on them.

3. **Tolerate unknown enum values.** Unknown `platform`, `strategy`, `tokens`, or
   `duty` values MUST degrade safely, never error:
   - unknown `platform` → an opaque string that known-platform placement won't
     match;
   - unknown `strategy` → treated as `weakest-first`
     ([06](06-coordination.md#ranking));
   - unknown `tokens` → treated as neither `ok` nor `out` (ranks after `low` via
     the token-rank fallback), never excluded;
   - unknown `duty` → gossiped, placed, and dispatched as opaque.

4. **Never repurpose an existing field.** The meaning of a v1 field is fixed
   forever. Changing what `tier` or `tokens` *means* is a breaking change; adding a
   *new* field alongside it is not.

5. **Defaults preserve behavior.** Any new optional field MUST have a default that
   reproduces exactly the v1 behavior when the field is absent. A v1 node (which
   omits the field) and a new node (which defaults it) MUST behave identically.

6. **Version signaling is additive-first.** Every message carries `v`. A minor,
   backward-compatible extension **keeps `v: 1`** and relies on rules 1-5; a
   receiver MAY read `v` to *offer* an enhanced behavior but MUST NOT *require* a
   higher `v` to keep basic interop. A genuinely breaking change (one that violates
   rules 1-5) **MUST** bump `v` and SHOULD be gated behind capability negotiation
   (below) so mixed meshes still function.

### Vocabulary skew

Because the resource vocabulary (platforms, tiers, tokens, duties, strategies) is
*data* loaded from a shared model, two nodes with **different** models still
interoperate at the wire level (rules 1-3), but may **place work differently** if,
say, one knows a duty the other doesn't. This is acceptable and safe - the node
that doesn't know a duty just treats it opaquely - but for consistent *placement*
across the mesh, operators SHOULD deploy the same model to every node. A future
revision MAY gossip a model digest so nodes can warn on skew.

### Capability negotiation (reserved)

For extensions that genuinely need both ends to agree (not just "ignore if
unknown"), the reserved mechanism is a `caps` array in the [`hello`](04-messages.md#hello):
each side advertises the capability tokens it supports, and a feature is used on a
link only if both ends list it. `caps` is not defined normatively in v1; until it
is, an implementation MAY include it and peers MUST ignore it (rule 1). This lets a
capability like `job-completion-tracking` or `quota-negotiation` be rolled out
incrementally without a `v` bump.

## Extension recipes

### Adding a duty

Add an entry to the model's duty catalog with an `id` and a default
[placement](06-coordination.md). No wire change: advertisements already carry
`dutiesEnabled` for arbitrary duty ids, and placement/dispatch are generic over the
duty set. Old nodes handle the new duty opaquely (rule 3). Deploy the model update
to every node for consistent placement.

### Adding a resource

Add an optional field (or a nested `resources` object,
[05](05-resources.md#extending-the-resource-vocabulary)) to NodeInfo. Old nodes
ignore it (rule 1) and can't place on it; new nodes may extend placement to match
on it. No `v` bump.

### Adding a placement strategy

Add a `strategy` id to the model and implement its ranking key. Old nodes that
receive an override naming it fall back to `weakest-first` (rule 3) - so the mesh
still converges, just with the fallback ranking on old nodes, until they're
updated.

### Adding a job status

Extend [`job-status`](04-messages.md#job-status) with a new `status` value (e.g.
`"rejected"`, `"completed"`). Dispatchers already treat any non-`"spawned"` outcome
as "this candidate didn't take it, fail over" ([07](07-dispatch.md#routing-a-job)),
so a new negative status needs no dispatcher change; a new *positive* status (like
`"completed"`) is only acted on by nodes that understand it.

## The altruism-limits roadmap

v1 is **full-altruism**: a node accepts any job for a duty it has enabled and is
[eligible](06-coordination.md#eligibility) for, with no accounting, quota, or
admission policy ([README](README.md#the-trust-model-personal-vs-foreign)). The brief
asks that limits on this altruism be addable *later, without breaking changes*.
They are - via the recipes above. Here is the concrete design, all additive:

### 1. Offered limits (advertisement side)

A node advertises the terms under which it offers resources, as an optional
`limits` object on its NodeInfo:

```json
"limits": {
  "maxConcurrent": 2,            // at most 2 jobs running here at once
  "perPeer": {"default": 1},     // at most 1 concurrent job from any single peer
  "duties": {"audit": {"maxConcurrent": 1}}  // per-duty caps
}
```

Old nodes ignore `limits` (rule 1) and keep treating the node as unconditionally
available - which is safe, because the *enforcement* lives on the offering node, not
the dispatcher (next point).

### 2. Enforcement (execution side) reuses the failover path

> **Partially landed.** `job-status` has gained a `declined` status, and nodes now
> decline via this exact failover path - refusing a **foreign** requester, a
> **disabled** duty, or being **out of tokens** ([11](11-trust-and-balancing.md),
> [07 refusal policy](07-dispatch.md#refusal-policy)). The quota-limit enforcement
> below is the remaining piece; it slots into the same mechanism.

When a dispatched job would exceed a node's advertised limits, the node **declines**
it: it replies with `job-status` `status: "declined"` (or the earlier `"failed"`,
or, once defined, the clearer `"rejected"`) and a `reason` like `"over quota"`. The
dispatcher's existing logic already **fails that slot over to the next candidate** on
any non-`spawned` outcome ([07](07-dispatch.md#routing-a-job)). So:

> A policy-declining node is handled by the *exact same code* that already handles a
> dead node or an out-of-tokens node. This is the crux of why altruism limits are a
> non-breaking addition: the dispatcher needs no new logic, only the node needs the
> new policy.

### 3. Placement awareness (optional, later)

For the mesh to *avoid* over-limit nodes proactively (rather than discovering the
limit at dispatch time), placement can grow to read `limits` and a node's
advertised current load, ranking a near-limit node lower. This is a
[resource-matching extension](05-resources.md#extending-the-resource-vocabulary):
additive, safe to roll out node-by-node, and purely an optimization - the failover
in point 2 remains the correctness backstop.

### 4. Accounting & reciprocity (further out)

A `cost`/`accounting` model (what a job "costs", per-peer balances, fair-share or
tit-for-tat reciprocity) attaches as further optional advertisement fields plus an
optional [`caps`](#capability-negotiation-reserved)-gated exchange for peers that
both support it. None of it changes the v1 wire format; nodes that don't participate
simply keep behaving altruistically.

### Migration property

At every step above, a mesh containing both a limits-aware node and a pure v1 node
functions: the v1 node offers unconditionally and is dispatched to normally; the
limits-aware node offers conditionally and declines-with-failover when over its
caps. No flag day, no `v` bump required for the common cases - exactly the
"extensible without breaking changes" the brief requires.

## Non-goals for v1 (explicitly deferred)

- **Cross-subnet / WAN operation.** The LAN path is single-subnet (link-local
  multicast, subnet broadcast). WAN reachability **has landed** as the opt-in
  [Tor transport](14-tor-transport.md): each node runs a permanent v3 onion service,
  advertises its `.onion` inside its signed advert, and redials known-but-unseen
  peers over Tor with exponential backoff (or a manual `--tor-connect` paste) — no
  public IP or domain, and a Tor link runs the identical handshake/trust as a LAN
  link. It is additive and off by default (LAN-only nodes are wire-unchanged).
  Native cross-subnet federation *without* Tor remains future work.
- **Foreign zero-trust execution.** The trust model (`personal`/`foreign`, keyed on
  a peer's **verified device fingerprint** against a local allowlist
  ([11](11-trust-and-balancing.md)), plus the v0.4.0 `banned`
  [accountability mark](13-foreign-execution.md#the-ban) layered on top) **has
  landed**, and so now has foreign **execution**: a node with a [confinement runner](13-foreign-execution.md)
  configured runs a foreign request **confined and response-only** — sandboxed
  compute, no host-identity action, the result returned as a
  [`job-result`](04-messages.md#job-result) for the requester to act on — per the
  normative [foreign execution security
  contract](11-trust-and-balancing.md#the-foreign-execution-security-contract-normative)
  and [13-foreign-execution](13-foreign-execution.md). Without a runner a foreign
  request is still [declined](07-dispatch.md#refusal-policy) (the safe default); the
  model forbids unsandboxed foreign code either way. What remains future work is
  *transport confidentiality* for the returned artifact (below) and a
  transitive/PKI trust story so a foreign result can route through a personal
  *relay*, not only back to the origin.
- **Transport encryption / confidentiality.** **Authentication has landed, both at
  the link and across gossip.** Each node holds a per-device
  [Ed25519 key](08-state.md#devicekey), proves possession on every link (a signature
  over the peer's fresh hello nonce), and **signs every gossiped advertisement and
  override** so a relay can neither forge nor tamper with another node's gossip
  ([11 - authenticated gossip](11-trust-and-balancing.md#authenticated-gossip)) - so
  **gossip** forgery/tamper and **passive** link impersonation are covered. What remains
  future work is an **encrypted, mutually-authenticated transport** (mutual TLS or an
  equivalent) - needed both for *confidentiality* of the bytes on the wire AND to close
  the [active link-auth reflection](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement):
  a bare-nonce proof of possession has no channel binding, so an *active* LAN adversary
  can reflect a personal peer's signature off it and be verified as that peer (a known
  v1 limitation). The [join fence](03-transport.md#the-join-fence) is still a **plaintext
  gate** (a shared-secret admission check, not a confidential channel), and a signed
  advertisement is authenticated but not secret.
- **Exactly-once dispatch / completion tracking.** v1 tracks hand-off, not
  completion, and does not deduplicate *jobs*
  ([07](07-dispatch.md#idempotency--duplicates)). **Origination** dedup — stopping
  two nodes that see the same external event from both starting the work — **has
  landed** as [work-claims](12-work-claims.md); job-level exactly-once and completion
  tracking remain future work.
- **IPv6.** v1 discovery and links are IPv4. IPv6 is additive future work.
