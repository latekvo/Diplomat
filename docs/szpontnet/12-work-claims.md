# 12 - Work claims (leaderless origination leases)

Coordination ([06](06-coordination.md)) decides *which machine should run a duty*,
and dispatch ([07](07-dispatch.md)) routes one job to exactly one executor. Neither
answers a different question: **when two nodes independently notice the same
external event, which of them originates the work?**

That gap is real. SzpontNet does not watch GitHub, a queue, or a filesystem - a
node *originates* a [SzpontRequest](07-dispatch.md#jobs) because something outside
the mesh told it to. If two of your machines both poll the same PR and both see the
same review request, each is an independent origin: v1 dispatch will happily route
**two** jobs for one piece of work ([07 - idempotency](07-dispatch.md#idempotency--duplicates)).
Placement can't prevent it, because the duplication happens *before* dispatch.

A **work-claim** closes that gap. It is a gossiped, self-signed **lease on a unit
of work**: before originating, a node claims the work; a deterministic rule elects
a single owner from the competing claims, and the losers stand down. There is **no
leader and no negotiation round** - the owner is a pure function of the gossiped
claims and the live set, exactly like [assignment](06-coordination.md), and a
simultaneous double-claim is reconciled by the loser the instant it hears the
winner. The claim is a **liveness-scoped lease**: it counts only while its claimant
is alive, so an owner that dies frees the work for a survivor with no timer of its
own.

Work-claims are an **optional** layer. A node that never originates externally
triggered work - or is happy to occasionally double-run it - need not implement
them, and interoperates unchanged (they ride an additive message type, [09
rule 2](09-extensibility.md#the-compatibility-contract)). Where they matter is a
group of **personal** machines that each watch the same source: claims turn "both
run it" into "exactly one runs it, and if that one dies another takes over."

## The work key

A **work key** is a string that identifies one unit of external work. Two nodes
that observe the *same* work MUST derive the *same* work key, and two *different*
units of work MUST derive *different* keys - that agreement is the entire basis for
deduplication. SzpontNet treats the key as **opaque** (like a job's `prompt`); how
it is derived is a **client responsibility**.

The derivation SHOULD be a pure function of stable identifiers of the work, and
SHOULD include anything that makes the work genuinely *new*. The reference
convention for a PR review is:

```
review:<host>/<owner>/<repo>#<number>@<head-sha>
```

The `<head-sha>` is load-bearing: a new push is legitimately new work and MUST
produce a new key (so it is claimed and run afresh), while a re-observation of the
*same* commit reuses the key (so it is suppressed). A key that omitted the sha
would wrongly suppress review of new commits; one that included a timestamp would
wrongly fail to suppress a re-poll of the same commit.

> **Skew is safe, not silent.** If two nodes disagree on the derivation (say one
> includes the sha and one doesn't), they simply fail to deduplicate for that work -
> the pre-work-claims behavior. They never corrupt each other. As with the shared
> [model](09-extensibility.md#vocabulary-skew), operators SHOULD deploy one
> derivation everywhere for consistent dedup.

## The claim record

A claim is a small, self-signed record. It appears on the wire inside a
[`work-claim`](04-messages.md#work-claim) message and is stored in the receiver's
**claim book**.

```json
{
  "workKey": "review:github.com/acme/app#123@abc123",
  "node": "3236817363144d8dbd842ec2973506c2",
  "pubkey": "kQ0f…base64-Ed25519-public-key…=",
  "epoch": 1784057237.23,
  "seq": 0,
  "state": "active",
  "sig": "…base64-Ed25519-signature…"
}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `workKey` | string | **yes** | the unit of work this lease is on. A claim without a non-empty `workKey` is invalid and MUST be dropped. |
| `node` | string | **yes** | the **claimant** node id. A claim without a non-empty `node` is invalid and MUST be dropped. |
| `pubkey` | string | no | the claimant's advertised base64 Ed25519 key, carried **inline** so the record is self-authenticating (a receiver can verify `sig` without having seen the claimant's advertisement first). Omitted by a keyless claimant. |
| `epoch` | float | no (`0`) | the claimant's incarnation stamp (its node `epoch`), so a restart supersedes the prior incarnation's leases. |
| `seq` | int | no (`0`) | the claimant's per-`workKey` update counter. |
| `state` | string | no (`"active"`) | `"active"` (the claimant holds the work) or `"released"` (it has given the work up). An unknown value MUST be treated as **not active** (so a future state never counts as ownership). |
| `sig` | string | no | base64 Ed25519 signature by the claimant over the record's canonical bytes ([signing](#authentication)). A **keyed** claim (one with a `pubkey`) MUST carry a valid `sig` or be dropped; a keyless claim carries none. |

`pubkey` and `sig` are **omitted when empty**, so a keyless claim is compact and a
signed one round-trips byte-stable (the signature covers the sig-less form).

**Freshness (same claimant).** Two records for the same `(workKey, node)` are
ordered by the tuple `(epoch, seq)`: the larger wins, exactly as
[NodeInfo](04-messages.md#nodeinfo) freshness. A receiver MUST keep only the
freshest record per `(workKey, node)` and MUST NOT let an older one overwrite a
newer one. This is what lets a claimant **re-assert** (bump `seq`) or **withdraw**
(`state:"released"`, higher `seq`) and have every node converge.

## Authentication

A claim is authenticated exactly like the other [gossiped, self-signed
payloads](11-trust-and-balancing.md#authenticated-gossip), under its **own** domain
tag so a signature can never be lifted from a claim onto an advert/override or vice
versa. The signed bytes are:

```
"szpontnet-workclaim-v1:" || canonical(claim)
```

where `canonical(x)` is the JSON of `x` **with its `sig` field removed, keys
sorted, and compact separators** (`,` / `:`) - byte-identical to the construction
in [11](11-trust-and-balancing.md#authenticated-gossip) and
[appendix B](appendix-b-constants.md#authenticated-gossip-construction). A receiver
MUST:

1. **Verify a keyed claim.** If the claim carries a `pubkey`, it MUST carry a `sig`
   that verifies against that `pubkey` over the signed bytes above; otherwise the
   claim is a forgery or was tampered with in relay and MUST be **dropped**. A
   **keyless** claim (no `pubkey`) has nothing to verify - it is accepted and
   relayed, but it can never be [authoritative](#ownership): ownership requires a
   claim actually **signed by the node it names** (next rule + the
   [binding](#ownership)), and a keyless claim proves nothing about who minted it.
   This is the same safe degradation as a keyless advertisement.

2. **Pin the claimant's key.** If the receiver already knows a `pubkey` for `node`
   (from that node's advertisement), a claim whose `pubkey` **differs from** that
   pin MUST be dropped - **including a keyless claim** (an *absent* key differs from
   the pin). Otherwise a third party could mint `{node: P}` with a *different* key
   (a re-key attack) **or with no key at all** and have it believed as trusted peer
   P's lease - suppressing work P never claimed. The guard is therefore
   unconditional once a key is pinned: the claim MUST carry P's exact key. This
   mirrors, and is as strict as, the advertisement
   [id→key pin](11-trust-and-balancing.md#authenticated-gossip).

Because the claimant's `pubkey` travels **inside** the claim, a keyed claim is
self-authenticating and can be verified and relayed even by a node that has not yet
seen the claimant's advertisement - but see the [ownership binding](#ownership) for
why verification alone does not grant a claim any power: it must also be signed by a
key that the claimant is trusted to hold.

## The claim book

Each node keeps a **claim book**: `workKey → { claimant node id → freshest claim
record }`. On receiving a [`work-claim`](04-messages.md#work-claim) a node MUST:

1. **Authenticate** it ([above](#authentication)); drop on failure.
2. **Merge by freshness.** Adopt the record only if it is newer (by `(epoch, seq)`)
   than the one held for that exact `(workKey, node)`; otherwise ignore it.
3. **Relay it verbatim** if adopted - re-broadcast the **exact received claim dict**
   (not a re-serialization), so the claimant's signature survives the hop. The
   freshness gate on every receiver stops the relay from looping. This is the same
   verbatim-relay rule as a gossiped advertisement.
4. **Reconcile** its own claim ([yield](#origination-and-yield)) if the adoption
   changed who owns the key.

A node MUST bound the claim book against a gossip flood of spoofed `workKey`s, and
it MUST do so **without letting the bound itself starve a genuine claim**. The
reference caps the total record count; a fresher update to an existing lease is
always merged (freshness never counts against the cap). At the cap, a **new**
`(workKey, node)` record is admitted by the **authoritative-eviction** rule:

- An [**authoritative**](#ownership) incoming claim - one that can actually win
  ownership here: a live, `personal`, key-bound claimant (self included) - **evicts
  one expendable stored record** to make room, then is inserted. An **expendable**
  record is a `released` tombstone **or** any record that is *not* authoritative
  here (a foreign, `down`, keyless, wrong-key, or prior-incarnation claimant that
  can never win ownership). If no expendable record exists - the book is full of
  live authoritative claims - the new claim is refused, but that is a real
  saturation of genuine work, not a spoofing artifact.
- A **non-authoritative** incoming claim past the cap is **refused outright**,
  exactly as before: it never displaces a stored record.

So a foreign or keyless flood can only ever occupy the evictable slots and can never
displace or starve a live, key-bound `personal` claim, while the book stays bounded
at the cap (evict-one-then-insert). A node's **own** claim is authoritative locally
and is never subject to being out-freshed - or evicted - by a relayed copy of
itself.

## Ownership

The **owner** of a work key is a pure, deterministic function of the claim book and
the live set - every node computes the same owner, leaderlessly:

```
function claim_owner(work_key, book, self_id, live, trust, pinned_key):
    active = [ node for (node, rec) in book[work_key]
               if rec.state == "active"
               and is_authoritative(rec, self_id, live, trust, pinned_key) ]
    return min(active) if active else None      # lowest node id wins

function is_authoritative(rec, self_id, live, trust, pinned_key):
    node = rec.node
    if node == self_id:            return true          # self always counts
    if node not in live:           return false         # dead lease lapses
    if trust(node) != "personal":  return false         # foreign never counts
    # The BINDING: the claim must be signed by the node it names — its key must be
    # non-empty and equal the key that node is pinned to (its sig was already
    # verified under that key). This is what stops a third party minting a claim
    # under a trusted peer's id (keyless, or with a key it controls).
    return rec.pubkey != "" and rec.pubkey == pinned_key(node)
```

Four rules, each load-bearing:

- **Lowest id wins.** The tie-break among competing active claims is the smallest
  `node` id. It is deterministic, needs no clock (wall-clock is
  [forbidden](06-coordination.md#determinism-requirements-normative) in a
  consensus-like decision), and is computed identically everywhere. *Which* node
  wins the race barely matters - the winner only does the cheap origination step
  (derive key, call dispatch) and then [dispatch](07-dispatch.md) load-balances the
  actual work to the best executor by surplus - so a simple, stable rule beats a
  clever one.

- **Only live claimants count.** A claim is a **lease** tied to its claimant's
  liveness (next section). A dead claimant's lease lapses, so ownership never gets
  stuck on a node that has gone away.

- **Only `personal` claimants count.** A claim suppresses work only if its claimant
  is a [trusted-`personal`](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement)
  device (or self). A **foreign** node's claim - even a perfectly valid signed one -
  is stored and relayed but **never owns** anything. This is the anti-starvation
  guard: a stranger cannot claim your work keys and then never run them to deny you
  the work. With an **empty allowlist** every verified peer is `personal`
  ([the full-trust default](11-trust-and-balancing.md)), so a home mesh dedupes
  across all its nodes with no configuration.

- **The claim must be signed by the claimant.** Trusting the *name* on a claim is
  not enough - a third party could put a trusted peer's id in the `node` field. So
  ownership additionally requires the record to carry that peer's **pinned key**
  (its signature having been verified under it): the claim is authoritative only if
  it was provably minted by the node it names. A **keyless** claim (no key to bind
  to) is therefore never authoritative under *any* trust configuration - not even
  the empty-allowlist full-trust default - and a keyless node participates without
  the power to suppress, exactly like a keyless advertiser is never `personal`.

## The liveness lease

A claim is **not** a permanent reservation and it is **not** released by the work
finishing (v1 does not track [completion](07-dispatch.md#idempotency--duplicates)).
It is a lease scoped to the **claimant's liveness**:

- A claim by a **live** node (its link is `up` or `stale`) is authoritative.
- A claim by a **`down`** node is **not** - the moment a claimant times out, its
  leases stop counting and the work is ownable again.

This reuses the mesh's existing liveness machinery, and the `stale`-vs-`down`
distinction it already draws for [assignment](06-coordination.md#the-live-node-set):
a momentary Wi-Fi stall (`stale`) does **not** free a lease (so the work doesn't
bounce between machines on a blip), but a full timeout (`down`) does. The payoff is
**failover for originated work**, which plain v1 lacks: if the machine that claimed
and started a review dies mid-flight, its lease lapses on every survivor and another
personal node can pick the work up - with no lease timer, no renewal traffic, and no
coordinator, because node liveness *is* the lease.

A node MUST drop a claimant's records from its book when that claimant is **reaped**
(fully removed from the peer table after the retention window) - the lease can no
longer be authoritative and the records only waste memory. A `down`-but-retained
peer's records MAY be kept (they are already non-authoritative) so a brief flap
doesn't discard state that a reconnect would restore.

> **Why not a fixed TTL?** A TTL would need periodic re-gossip (renewal traffic) and
> a second liveness notion layered on the one the mesh already has. Deriving the
> lease from node liveness costs **zero** extra messages and cannot disagree with
> the rest of the mesh about whether a node is alive. The cost is that a claim
> covers "the claiming node is alive and responsible," not "the work is still
> running" - acceptable because completion tracking is a
> [reserved extension](09-extensibility.md#adding-a-job-status), and a lease that
> outlives a *finished* job merely suppresses a redundant re-run of the same
> `workKey`, which is the desired behavior anyway. An executor that *does* observe
> its agent's completion (the applet watches a per-agent sentinel) MAY
> [release](#origination-and-yield) the lease early — turning the crash of an agent
> into a prompt retry — without changing the primitive: a release is just a normal
> withdrawal, and a node death still frees the lease regardless.

## Origination and yield

A node that wants to originate externally triggered work runs the **claim gate**
first:

```
function should_originate(work_key):
    owner = claim_owner(work_key, ...)
    if owner is not null and owner != self_id and owner < self_id:
        return false                      # a better (lower-id) live+personal peer owns it
    announce_active_claim(work_key)       # mint, store, and gossip our own active claim
    return true
```

- If **nobody** owns the key, or **we** already own it, or we **out-rank** the
  current (higher-id) owner, we announce our active claim and proceed. Re-claiming a
  key we already own is **idempotent** - a legitimate retry by the owner is never
  suppressed.
- If a **lower-id** live, personal peer already owns it, we **stand down** and do
  not originate.

The **race** - two nodes both see the work, both find no owner, both announce - is
resolved without any extra round by the **yield** rule. When a node adopts a peer's
claim (step 4 of the [book](#the-claim-book) merge), if that adoption means a
**better (lower-id)** peer now owns a key the node was itself originating, the node
MUST:

1. **withdraw** its own claim (`state:"released"`, bumped `seq`), and
2. invoke its **loss hook** so the caller can abort the work it started.

So on a simultaneous double-claim, the higher-id node hears the lower-id claim,
yields, and withdraws; the lower-id node hears the higher-id claim, stays owner, and
continues. The mesh converges on the single lowest-id owner in one gossip round,
with no bidding, no lock, and no leader.

> **The abort window.** Yielding cleanly aborts only work not yet side-effecting. If
> a node has *already* launched the job before it learns it lost the race, a brief
> double-run can still occur. An implementation SHOULD announce the claim **before**
> the side-effecting spawn, so the common simultaneous-detection case is decided
> while both nodes are still only *intending* to run. Work-claims turn "**always**
> double on simultaneous detection" into "**rarely** double"; they are a strong
> deduplication, not an exactly-once guarantee (which remains
> [out of scope](09-extensibility.md#non-goals-for-v1-explicitly-deferred)).

## Integration with dispatch

The claim gate attaches to the [control-session dispatch](07-dispatch.md#dispatching-via-a-control-session):
the [`dispatch`](04-messages.md#dispatch) control message MAY carry an optional
`workKey`. When present, the **dispatcher** first *reads* the current owner: if a
live [authoritative](#ownership) node already holds the key, an agent is on the work,
so it returns a [`dispatch-result`](04-messages.md#dispatch-result) whose single slot
is `"claim"` with status **`"suppressed"`** (the owner in `node`/`nodeName`)
**instead of** routing anything. Otherwise it places the run on the best node exactly
as [chapter 07](07-dispatch.md) describes, and the **executor** — the node that
spawns the agent — mints the active claim, holds it for that agent's lifetime, and
[releases](#origination-and-yield) it when the agent finishes.

Claiming on the **executor**, not the dispatcher, is what makes the lease track the
*work* rather than merely the act of dispatching. From the one
liveness-plus-completion lease: a re-observation while the agent runs is suppressed
(the executor owns the key), an agent that crashes frees the key so the work is
*retried*, and the executor's death frees it for *failover*. A simultaneous race —
two dispatchers both read "unowned" and place on different executors — still
converges by the [yield rule](#origination-and-yield): the higher-id executor hears
the lower-id claim, withdraws, and aborts.

The gate applies **only** to the leaderless surplus-first origination path, because
that is the only path where two nodes can race to the same external event:

- a [**server**](11-trust-and-balancing.md#the-server-role) node runs the request
  locally and never originates to peers - nothing to deduplicate;
- an [**explicit target**](07-dispatch.md#explicit-target) is the client
  deliberately overriding placement - the dedup would fight that intent.

Both bypass the claim gate. A `workKey` on either is simply ignored.

## Integration without dispatch: the `claim` control verb

A client that will run the work *itself* — not route the execution through the mesh
— can still use the dedup directly. The stand-alone
[`claim`](04-messages.md#claim--claim-result) control verb runs
`should_originate(workKey)` and reports the verdict, so the caller originates only
when it wins.

The applet's **PR auto-monitors** (reference work-key kinds: `review:` for a review
requested of the operator, `review-reply:` for replies owed on the operator's own
PR, `conflicts:` for a merge-conflict fix — all
`<kind>:<host>/<owner>/<repo>#<number>@<head-sha>`) instead integrate through
[claim-gated **dispatch**](#integration-with-dispatch): **every machine scans**, and
each find is routed *with* its `workKey`, so the mesh runs it **once, on the
best-surplus node**, with the executor holding the lease for the agent's lifetime.

There is deliberately **no** duty-*assignment* stand-down. Assignment
([06](06-coordination.md)) answers "where should a dispatched job run", not "who is
scanning"; deferring a scan to the assignee dropped the work whenever that node was
not itself watching (a `tokens:"out"` machine, a duty toggled off) — the very bug
this integration replaces. The **claim**, not the assignment, is the dedup: it is
minted only where an agent actually runs, and the losers stand down against *it*.

Fail-open is deliberate: when the local node is unreachable the monitors revert to
pre-claims behavior (spawn locally), preferring a rare duplicate over silently
dropping the operator's work.

## Conformance

Work-claims define an **optional role**, the **Originator-with-dedup**. A node need
not implement them; if it does, it MUST:

1. **Derive keys deterministically.** Two conformant nodes observing the same work
   MUST produce the same `workKey` (per the deployed derivation), and distinct work
   MUST produce distinct keys.
2. **Authenticate.** Sign every claim it originates under
   `"szpontnet-workclaim-v1:" || canonical(claim)`; **drop** a keyed claim with an
   absent/invalid `sig`; **drop** a claim whose `pubkey` disagrees with the
   claimant's pinned key; treat a keyless claim as non-authoritative.
3. **Merge and relay.** Keep the freshest record per `(workKey, node)` by
   `(epoch, seq)`, never overwrite newer with older, and re-propagate an adopted
   claim **verbatim**. Bound the book against a spoofed-`workKey` flood **by
   [authoritative eviction](#the-claim-book)** - at the cap, admit an authoritative
   claim by evicting an expendable record, and refuse a non-authoritative one - so
   the bound never starves a live key-bound `personal` claim.
4. **Elect deterministically.** Compute `claim_owner` as the lowest-id **active**
   claimant that is **live**, **`personal`**, **and bound** — its claim carries the
   claimant's pinned key (self always; a keyless, wrong-key, foreign, or dead
   claimant never, in *any* trust configuration). Two conformant nodes with the same
   book and live set MUST compute the same owner.
5. **Lease to liveness.** Treat a claim as authoritative only while its claimant is
   `up`/`stale`; free it on `down`; drop a reaped claimant's records.
6. **Gate and yield.** Stand down when a lower-id owner holds the key; announce
   before proceeding; on adopting a better peer's claim for a key it is originating,
   withdraw and fire the loss hook.
7. **Scope the gate.** Apply it to the surplus-first origination path only, never to
   server-local or explicit-target dispatch.

A node that does **not** implement the role MUST still, per the
[compatibility contract](09-extensibility.md#the-compatibility-contract), **drop the
unknown `work-claim` message and keep the link** (rule 2) - so a dedup-aware node and
a plain node share a mesh without trouble; they simply don't deduplicate against each
other.

## Security properties

- **No forgery or tampering.** Ownership requires a claim **signed by the node it
  names** ([the binding](#ownership)): a relay can neither invent a claim under
  someone else's identity — not with a key it controls (dropped by the [id→key
  pin](#authentication)) and not by omitting the key (a keyless claim is never
  authoritative) — nor alter one in flight (the signature). Trusting the *name* on a
  claim is never sufficient; the *key* must check out.
- **No starvation *by an untrusted party*.** Only a claimant that is **both**
  `personal` **and** proven to hold the key it claims under can suppress work, so a
  foreign or keyless node can never deny you work by claiming keys it won't run. This
  holds **even under a book-filling flood**: the [authoritative-eviction
  rule](#the-claim-book) admits a live, key-bound `personal` claim at the cap by
  evicting an expendable (foreign / `down` / keyless / wrong-key / `released`)
  record, so a stranger who spams 4096 spoofed `workKey`s occupies only evictable
  slots and can never crowd out - and thereby silently defeat the dedup for - a
  genuine personal claim. The residual is a *trusted* peer griefing: with a
  **configured allowlist** the suppression set is exactly the devices you chose to
  trust; with the **empty-allowlist full-trust default** you have declared every
  joined, **keyed** device trusted, so fence the mesh (a [join
  secret](03-transport.md#the-join-fence) or an allowlist) if you don't trust
  everyone who can reach the LAN. A keyless intruder is powerless regardless.
- **No amplification.** Verifying a claim is one signature check; the book is
  size-bounded (and a full book admits an authoritative claim only by
  [evicting](#the-claim-book) an expendable one, never by growing); a stale or
  duplicate claim is dropped by the freshness gate, so a claim flood cannot be turned
  into unbounded work or memory - nor into starvation of a real claim.
- **Bounded replay.** A claim is not nonce-bound (unlike the
  [link auth](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement)),
  so a captured claim can be replayed - but a replay only re-asserts the *same*
  claimant's ownership of the *same* key, which is a no-op against the freshness
  gate and the liveness lease, and gains an attacker nothing.

## Limitations (v1)

- **Not exactly-once.** See [the abort window](#origination-and-yield): work-claims
  strongly reduce, but do not eliminate, a double-run under exactly-simultaneous
  detection. True exactly-once/completion tracking stays
  [reserved](09-extensibility.md#non-goals-for-v1-explicitly-deferred).
- **Multi-hop ownership needs a direct trust view.** A claim only *owns* on a node
  that can classify the claimant as `personal` from its own verified link. A
  claimant that is live only *transitively* (linked via a third node, not directly)
  has no verified fingerprint locally, so its claim does not suppress there -
  degrading safely to "may double-originate," never to starvation. On the full-mesh
  LAN this layer targets (personal machines that all link directly), this does not
  arise; closing it in a sparse topology is future work alongside a transitive-trust
  or PKI story ([11](11-trust-and-balancing.md)).
- **Cold-join ordering.** If a claim outraces its claimant's first advertisement to
  a third node, the id→key pin has nothing to pin against yet, so a claim naming a
  not-yet-known node is stored unpinned; it is never authoritative (the claimant is
  not a known live peer, and — being unbound — fails the ownership binding). A
  receiver **MUST**, on learning a node's key (its advertisement), **purge any
  stored claim under that id whose key doesn't match** — otherwise a forged
  keyless/wrong-key claim planted in this window, with a spoofed-high `(epoch, seq)`,
  would out-fresh the claimant's real signed claim indefinitely and defeat dedup for
  that key (it can never *suppress*, being unbound, only cause a redundant
  double-run). With that purge (the reference does it in `_learn_node`), the window
  closes the instant the real advertisement arrives, leaving only a brief transient
  before the claimant is known.
