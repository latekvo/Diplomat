# 06 - Coordination & assignment

This is the heart of SzpontNet: how, with no leader and no messages beyond gossiped
advertisements, every node agrees on which machine runs each duty. The answer is a
**pure deterministic function** - `assign` - that every node evaluates over the
same inputs and so produces the same output everywhere. When the inputs change (a
node joins, dies, or updates its advertisement), every node re-evaluates and
converges on the new answer without negotiating.

Interoperability depends on every implementation computing this function
**identically**. This chapter specifies it exactly; the reference is
[`assign.py`](../../linux/argent_utils/mesh/assign.py) and its tests
[`test_mesh_logic.py`](../../linux/tests/test_mesh_logic.py).

## The live-node set

The input to assignment is the set of **live** nodes: the local node plus every
peer whose [link state](03-transport.md#link-state) is `up` **or** `stale`. A
`down` peer is excluded. Including `stale` peers is deliberate - a momentary Wi-Fi
stall must not bounce ownership; only a full timeout moves work.

Each live node contributes its freshest [NodeInfo](04-messages.md#nodeinfo).

## Placement policy

Each duty has a **placement policy** - the effective one is the
[override](#placement-overrides) if present, else the duty's default from the
model. A policy has three parts:

```json
{"strategy": "weakest-first", "tokenAware": true,
 "spread": [{"platform": "linux", "count": 1}, {"platform": "macos", "count": 1}]}
```

- **`strategy`** ∈ {`weakest-first`, `strongest-first`, `local-first`,
  `surplus-first`} - the ranking (below).
- **`tokenAware`** (bool) - whether `tokens: "out"` excludes a node.
- **`spread`** (array of `{platform, count}`) - platform-coverage requirements;
  empty means "any single node".

## Eligibility

A node is **eligible** for a duty when **both**:

1. it has the duty **enabled** - `dutiesEnabled[duty]` is not `false` (absent =
   enabled); and
2. if the policy is `tokenAware`, its `tokens` is **not** `"out"`.

Ineligible nodes are removed before ranking. (A `"low"`-token node is *eligible*;
it is only de-prioritized in the ranking.)

## Ranking

Eligible nodes are sorted by a **total order** - a tuple whose final element is the
node `id`, so the order is fully deterministic with no ties. Let
`tok_rank(tokens)` = `0` for `"ok"`, `2` for `"out"`, and `1` for `"low"` **or any
other (unknown) value** — an unrecognized token string ranks *with* `low`, never
excluded (matching [09 rule 3](09-extensibility.md#the-compatibility-contract) and
[appendix B](appendix-b-constants.md#tokens); a token-aware duty has already
removed `out` nodes in [eligibility](#eligibility), so `out`'s rank only orders
them when token-awareness is off). Then the sort key per node `n`, given the local
node id `L`, is:

| Strategy | Sort key (ascending) |
|----------|----------------------|
| `weakest-first` (and any **unknown** strategy) | `(tok_rank(n.tokens), −n.tier, n.id)` |
| `strongest-first` | `(tok_rank(n.tokens), n.tier, n.id)` |
| `local-first` | `(tok_rank(n.tokens), n.id != L, −n.tier, n.id)` |
| `surplus-first` | `(−surplus(n), tok_rank(n.tokens), −n.tier, n.id)` |

where `surplus(n)` = `n.stats.quotaLeft − n.stats.usageAvg`, or `0` when the node
advertises no stats.

Reading the keys:

- **Token rank first, always.** `ok` beats `low` beats the rest, under every
  strategy. Budget availability dominates machine preference.
- **weakest-first** then prefers the *largest* tier number (weakest machine).
- **strongest-first** then prefers the *smallest* tier number (strongest machine).
- **local-first** then prefers the local node (the boolean `n.id != L` sorts
  `False`=local first), then falls back to weakest-first ordering for the rest.
- **surplus-first** leads with the *most* spare quota (`−surplus` sorts the largest
  surplus first); ties fall back to weakest-first (token rank, then tier, then id).
  A neutral-stats node (surplus `0`) ranks exactly as it would under weakest-first,
  so it never jumps ahead of a node with measured spare budget.
- **id tie-break** makes the result identical on every node.

> An **unknown** strategy (from a newer peer's override) MUST fall back to
> `weakest-first`, never error. This keeps a mixed-version mesh converging.

## The assignment algorithm

```
function assign_duty(duty, live_nodes, overrides, local_id) -> (assigned[], shortfall[]):
    policy   = effective_placement(duty, overrides)      # override else model default
    eligible = [n for n in live_nodes if is_eligible(n, duty, policy)]
    ranked   = sort(eligible, key = strategy_key(policy.strategy, local_id))

    if policy.spread is empty:
        if ranked is empty:
            return (assigned = [], shortfall = [("any", 1)])
        return (assigned = [ranked[0].id], shortfall = [])   # single best node

    assigned = []
    shortfall = []
    taken = {}                                             # a node fills at most one slot
    for (platform, count) in policy.spread:
        filled = 0
        for n in ranked:
            if filled == count: break
            if n.platform == platform and n.id not in taken:
                taken.add(n.id); assigned.append(n.id); filled += 1
        if filled < count:
            shortfall.append((platform, count - filled))
    return (assigned, shortfall)
```

- **No spread:** the single best-ranked eligible node owns the duty. Empty pool →
  empty assignment with a `("any", 1)` shortfall.
- **Spread:** each `{platform, count}` requirement is filled from that platform's
  ranked candidates; a node fills **at most one** slot (so "1 linux + 1 macos"
  lands on two distinct machines). Requirements that can't be met are reported as
  **shortfall** - the duty still gets whatever coverage exists; it is never
  dropped for being under-covered.

`assign_all` simply runs `assign_duty` for every duty in the model and returns the
map `{duty: {assigned, shortfall}}` that the [snapshot](08-state.md) publishes.

### Worked examples

Fleet: `A` linux tier 4, `B` macos tier 1, `C` macos tier 4, all `tokens: ok`, all
duties enabled.

| Duty / policy | Eligible, ranked | Assigned | Shortfall |
|---------------|------------------|----------|-----------|
| `review` weakest-first, no spread | A(t4), C(t4), B(t1) → `A,C,B` | `[A]` | - |
| `review` strongest-first | B(t1), A(t4)/C(t4) by id | `[B]` | - |
| `audit` weakest-first, spread 1×linux+1×macos | linux: A; macos: C(t4),B(t1) | `[A, C]` | - |
| `audit` but only B,C present (no linux) | linux: -; macos: C,B | `[C]` | `[(linux, 1)]` |
| `review`, but A is `tokens:out` | eligible C,B (A excluded) | `[C]` | - |
| `review`, A `tokens:low`, others ok | ranked B,C ahead of A | `[C]` | - |

These are exactly the cases asserted in `test_mesh_logic.py`.

## Determinism requirements (normative)

For interop, an implementation **MUST**:

- produce assignments that depend only on the live-node advertisements and the
  effective overrides - never on wall-clock, iteration order, or local state;
- use the exact token ranking and strategy keys above, ending every key with the
  node `id` so there are no ties;
- treat an unknown strategy as `weakest-first`;
- fill spread slots in the order the `spread` array lists them, one node per slot.

Two conformant nodes with the same live set and overrides **MUST** compute
byte-identical assignments. The reference test `test_assignment_is_permutation_invariant`
checks that input order cannot change the result - a good property to replicate.

## Placement overrides

The default placement lives in the model, but an operator can retune a duty's
policy at runtime, mesh-wide, from any node (a control client issues
[`set-overrides`](04-messages.md#set-overrides)). Overrides are gossiped
**last-writer-wins**:

```json
{"rev": 3, "updatedBy": "3236…", "duties": {
    "review": {"strategy": "strongest-first", "tokenAware": true, "spread": []}
}}
```

| Field | Type | Meaning |
|-------|------|---------|
| `rev` | int | a monotonically increasing revision counter. |
| `updatedBy` | string | node id of the last editor (the tie-break). |
| `duties` | object<string, placement> | the *full* policy for each overridden duty (not a diff). |

**LWW comparison.** Overrides `X` **wins over** `Y` iff the tuple
`(X.rev, X.updatedBy) > (Y.rev, Y.updatedBy)` (compare `rev` numerically, then
`updatedBy` lexicographically). A node adopts incoming overrides only if they win
over what it holds; on adopting, it re-gossips them and recomputes. An edit bumps
`rev` to `(current rev) + 1` and stamps `updatedBy` with the editor's id.

This gives eventual convergence: concurrent edits on different nodes get the same
`rev`, the `updatedBy` tie-break picks one deterministic winner, and it propagates
to all. `duties` carries the *whole* policy per duty so a merge never has to
combine partial edits - the winning `duties` map replaces the loser's wholesale.

A duty **not** present in `duties` uses its model default. To reset a duty to
default, an implementation MAY omit it from a new (higher-`rev`) `duties` map.

## Placement strategy vs dispatch strategy

Two separate rankings, easily confused, do different jobs:

- A duty's **placement `strategy`** drives the **stable displayed ownership** - what
  the topology panel shows as the duty's owner. It is recomputed by consensus
  (`assign_all` over the shared inputs), so every node shows the same owner and it
  only moves when the inputs move.
- The **dispatch-time target selection** is a **separate, dispatcher-local choice**.
  When a node actually dispatches a request it ranks candidates by
  `dispatchStrategy` (default `surplus-first`) via
  `slot_candidates(…, strategy=…)` - its `strategy` argument overrides only the
  *ranking*, not eligibility or spread. This decision is made **unilaterally** from
  the dispatcher's own gossiped view, with **no consensus**.

That separation is deliberate: fast-moving `stats` load-balance dispatch to whoever
has the most spare quota *without* churning the displayed owner, which stays put on
its stable placement strategy. See [07-dispatch](07-dispatch.md) for how a chosen
target is executed and [11-trust-and-balancing](11-trust-and-balancing.md) for the
accounting the surplus ranking reads.

> **`surplus-first` is a *dispatch* strategy, not a placement one.** Consensus
> assignment MUST be a pure function of *gossiped* advertisements ([determinism
> requirements](#determinism-requirements-normative)); but a node's `stats` **decay
> continuously and locally** and are re-gossiped only on a real change
> ([11](11-trust-and-balancing.md#surplus)), so a node's own live surplus differs
> from the last value its peers hold. Ranking the **consensus** assignment by
> surplus would therefore let two nodes compute different owners — breaking
> determinism. So `surplus-first` is defined for **dispatch** target selection
> only; an implementation **SHOULD NOT** configure it as a duty's placement
> `strategy`, and one that receives it as a placement override MAY treat it as
> `weakest-first` for the *displayed* assignment. (It stays fully valid, and is the
> default, for `dispatchStrategy`.)

## Why leaderless works, briefly

There is no elected owner and no lock. "Ownership" is an *observation*: every node
independently computes the same `assign_all` and therefore the same owner. When the
inputs change identically everywhere (a gossiped advertisement, a timed-out peer,
an adopted override), the computed owner changes identically everywhere. Dispatch
([07](07-dispatch.md)) then *acts* on that shared computation. There is a brief
window between an event and gossip reaching every node during which two nodes may
hold different views - this is why dispatch carries a **failover list** rather than
trusting a single computed owner, and why work is never *enforced* to be exclusive
in v1 (see the [trust model](README.md#the-trust-model-personal-vs-foreign)).
