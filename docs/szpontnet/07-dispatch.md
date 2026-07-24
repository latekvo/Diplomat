# 07 - Dispatch

Assignment ([06](06-coordination.md)) decides *who should* run a duty. **Dispatch**
is the act of actually running one now: staging a job, routing it to the chosen
node(s), and failing over if a chosen node can't take it. Dispatch is optional for
a node that only wants to *offer* resources - but any node that wants to *originate*
work implements it, and any node that accepts work implements the receiving half
([execution](#execution)).

> A dispatched unit of work is what the UI and this spec call a **SzpontRequest**.
> On the wire it stays a [`dispatch`](04-messages.md#dispatch) message carrying a
> [`job`](04-messages.md#job); "SzpontRequest" is just the name for that one
> request-to-run.

## Jobs

A dispatch carries a [Job](04-messages.md#job): an `id`, the `duty`, an opaque
`prompt` (the work payload), and provenance (`requestedBy`, `requestedAt`). The
dispatcher assigns a fresh unique `id` per job.

## Slots

A duty's placement may require **spread** across platforms ([06](06-coordination.md#placement-policy)) -
e.g. the `audit` duty runs on *one Linux and one macOS node*. Dispatch therefore
runs **one job per slot**:

- A **no-spread** duty has a single slot labeled `"any"`.
- A **spread** duty has one slot per unit of `{platform, count}` (a `count: 2`
  contributes two slots for that platform).

Each slot has its own **ranked candidate list**, computed by `slot_candidates`:

```
function slot_candidates(duty, live_nodes, overrides, local_id) -> [(slot_label, [node_id])]:
    policy = effective_placement(duty, overrides)
    ranked = sort(eligible(live_nodes, duty, policy), strategy_key(policy.strategy, local_id))
    if policy.spread is empty:
        return [("any", [n.id for n in ranked])]              # one slot, all eligible nodes
    slots = []
    for (platform, count) in policy.spread:
        of_platform = [n.id for n in ranked if n.platform == platform]
        emit = count                                          # or, per the note below:
        # emit = min(count, max(1, len(of_platform)))         # cap slots at eligible nodes
        repeat emit times: slots.append((platform, of_platform))
    return slots
```

> **Slot count MAY be capped at the eligible-node count.** A `(platform, count)`
> requirement asks for `count` slots, but no two slots ever land on one machine
> ([routing](#routing-a-job)), so any slots beyond the number of eligible nodes of
> that platform are structurally unfillable. An implementation **MAY** therefore emit
> only `min(count, max(1, len(of_platform)))` slots for the requirement (at least one,
> so an empty platform still yields a `failed` slot that reports the shortfall) and
> report the unfillable remainder as an [unfilled shortfall](#routing-a-job) rather
> than as extra `failed` slot entries. The reference does exactly this, as an OOM
> guard against a hostile or mistyped `count` on an open mesh. The **effective
> placement is identical** either way - the same jobs land on the same nodes; only
> the number of `dispatch-result` slot entries for an over-asked requirement differs.

The candidate list for a slot is the assigned node **first**, then every other
eligible node of that platform by rank - so a slot survives its top pick dropping
out between gossip rounds.

## Routing a job

**Target selection is the dispatcher's own load-balancing call - no consensus.**
The dispatcher ranks the eligible candidates itself and forwards; peers do not vote
on where work lands. By default candidates are ranked **surplus-first** (config
`dispatchStrategy`), so work flows to the node with the most spare quota
([11](11-trust-and-balancing.md)).

> A node in [server mode](11-trust-and-balancing.md#the-server-role) is the
> exception to origination: it **never** routes to peers. A request it is asked to
> dispatch runs on itself (a single `"server"` slot), and a request explicitly
> targeted at another node is refused — the server accepts work but is never a
> source of it.

To dispatch a duty, a node:

1. computes `slot_candidates` over the current live set;
2. for each slot in order, walks its candidate list and tries to place the job on
   the first candidate that accepts, **skipping any node already used by an
   earlier slot** (so two slots never land on one machine);
3. records one result per slot: `{slot, node, nodeName, status, reason}`.

```
used = {}
results = []
for (slot_label, candidates) in slot_candidates(...):
    outcome = {slot: slot_label, node: null, status: "failed", reason: "no eligible node"}
    for node_id in candidates:
        if node_id in used: continue
        (status, reason) = place_on(node_id, duty, prompt)     # local or remote
        if status == "spawned":
            used.add(node_id)
            outcome = {slot: slot_label, node: node_id, status: "spawned"}
            break
        outcome = {slot: slot_label, node: node_id, status: "failed", reason: reason}
    results.append(outcome)
return results
```

A slot whose every candidate declines (or that has no candidates) ends `failed` -
the dispatch as a whole is "partial" but the other slots still ran. This is the same
shape whether a candidate declines because it is **dead**, **out of tokens**, or -
under a [future altruism limit](09-extensibility.md#the-altruism-limits-roadmap) -
**over quota**: any non-`spawned` outcome simply advances to the next candidate.

### Explicit target

The client may name a single node to run the SzpontRequest, bypassing surplus-first
selection entirely. An explicit **target** produces one slot with that node as its
*only* candidate and therefore **no failover**: the request goes there, and if that
node **declines** the decline is reported as-is (the slot ends non-`spawned`; it is
not retried elsewhere). This is the "Alice may forward everything to Bob, and Bob may
refuse" case - the dispatcher's load-balancing default is overridden, but the
receiver's [refusal policy](#execution) still applies.

## Placing on a node

- **Local** (`node_id` is this node): run the job here directly
  ([execution](#execution)) and use its `spawned`/`failed` result.
- **Remote**: send a [`dispatch`](04-messages.md#dispatch) on that peer's link and
  wait for the [`job-status`](04-messages.md#job-status) reply, up to
  `dispatchAckTimeoutSecs` (default **8 s**). Map the reply's `status`/`reason` to
  the slot outcome; a timeout or link error is a `failed` outcome
  (`reason: "peer did not answer"`) and the slot fails over.

  A dispatcher correlates the reply to the request by Job `id` **and by
  responder**: it MUST tolerate (drop) a `job-status` for an unknown id, and MUST
  accept a reply only from the peer it dispatched that job to — a `job-status`
  arriving on any other link is dropped, so a third peer that learns a live job id
  can't resolve someone else's dispatch.

## Execution

The **receiving half** of dispatch. A node that receives a
[`dispatch`](04-messages.md#dispatch) - on an
[authenticated](03-transport.md#the-join-fence) link, or from a control client -
**runs the job locally** and replies with a [`job-status`](04-messages.md#job-status):

- On success (the work was started): `status: "spawned"`.
- On failure (the node could not start it - e.g. no way to launch it here):
  `status: "failed"` with a human `reason`; the dispatcher fails the slot over to
  the next candidate.

### Refusal policy

Before running, the receiver applies its own **admission** check - its own call, no
consensus needed. It replies [`declined`](04-messages.md#job-status) (a `job-status`
status) rather than running when any of:

- the requester is a **foreign** device - one whose proven key isn't in this node's
  local [trust allowlist](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement)
  (classified from the *verified* link, never from the job's `requestedBy`) - **and**
  this node has no [confinement runner](13-foreign-execution.md#confinement-the-executors-responsibility)
  configured. A node *with* one instead runs the foreign request **confined and
  response-only** - the [zero-trust path](13-foreign-execution.md): compute in a
  sandbox, return a [`job-result`](04-messages.md#job-result) for the requester to act
  on - replying `spawned` (the hand-off) rather than declining. Without a runner,
  declining is the safe default. Either way the receiver never takes a social action
  on a stranger's behalf, and any node that *does* execute foreign work MUST honor the
  [foreign execution security
  contract](11-trust-and-balancing.md#the-foreign-execution-security-contract-normative)
  (sandboxed, no host-identity action, response-only);
- the requester is **banned** - it is on this node's local
  [ban list](13-foreign-execution.md#the-ban) (it accepted a SzpontRequest of this
  node's and failed to deliver it, or the operator banned it manually). Declined
  outright (reason `"banned device"`), confinement runner or not - the confined
  path is a favor, and a ban ends it;
- the request lacks a required **API key** - a
  [server](11-trust-and-balancing.md#the-api-key) configured with one refuses a
  request that doesn't present a matching `apiKey`;
- the duty is **disabled** locally (the node opted out of that class of work);
- the node is **out of tokens** (this is Bob refusing the job Alice sent anyway,
  which the protocol expressly allows).

The dispatcher needs **no new logic** for this: its failover treats `declined`
exactly like `failed` - any non-`spawned` outcome fails the slot over (or, for an
[explicit target](#explicit-target), is reported as-is). See
[11 - Trust & balancing](11-trust-and-balancing.md) for the trust model behind the
foreign check.

What "run locally" *means* is implementation-defined and outside the wire protocol.
Diplomat Mesh stages the `prompt` to a file and opens a terminal running an agent on
it, exactly like a local spawn; a headless deployment substitutes its own runner
(the reference honors an `DIPLOMAT_MESH_SPAWN` command template for exactly this).
SzpontNet only requires that the node truthfully report `spawned` vs `failed`.

> **v1 reports hand-off, not completion.** `spawned` means the node accepted and
> started the work; for a *personal* job SzpontNet does not track it to completion or
> return its result. Completion tracking is a [reserved
> extension](09-extensibility.md). The one place a result *is* returned is a
> **foreign, confined** job: it too replies `spawned` at hand-off, then returns its
> computed artifact out of band as a [`job-result`](04-messages.md#job-result) for the
> originator to act on ([13](13-foreign-execution.md)) - a response to a zero-trust
> request, not general completion tracking. As of v0.4.0 that owed result is also
> **held to a completion deadline**: a foreign acceptance the originator never sees
> fulfilled ends in a reminder and, unanswered, a [ban](13-foreign-execution.md#accountability-deadline-reminder-ban).

## Dispatching via a control session

A UI or CLI dispatches by opening a [control session](04-messages.md#control-messages)
to its **local** node and sending a [`dispatch`](04-messages.md#dispatch); the node
performs the routing above and replies with a
[`dispatch-result`](04-messages.md#dispatch-result) carrying the per-slot outcomes.
This is how the topology panel's "run on mesh" and the CLI's `--dispatch` work: the
client talks only to its local node, which does the mesh routing on its behalf.

The control `dispatch` command takes an optional `target` (a node id): when present,
the local node routes to that node with a single-candidate slot and no failover (the
[explicit target](#explicit-target) case) instead of surplus-first selection.

## Idempotency & duplicates

SzpontNet does not deduplicate jobs: a `dispatch` is a fire-once request, and job
`id`s are unique per dispatch. If the same logical work is dispatched twice (two
operators, or a retried dispatch), two jobs run. A dispatcher SHOULD avoid
re-dispatching the same work; a receiver treats every `dispatch` it accepts as new.
Exactly-once semantics are out of scope for v1.

> **Origination dedup exists one level up.** The common cause of a double-run is not
> a retried `dispatch` but two nodes *independently observing the same external
> event* and each originating. [Work-claims](12-work-claims.md) deduplicate exactly
> that: a node claims a `workKey` before originating and stands down if a peer
> already owns it (a `"suppressed"` [dispatch result](04-messages.md#dispatch-result)).
> That is an origination-time gate, not job-level exactly-once — which remains out of
> scope — but it removes the case where it actually bites.
