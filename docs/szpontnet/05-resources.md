# 05 - Resource advertisement

The reason a node exists on the mesh is to **advertise the resources it has
available** and let the mesh put work on the best fit. This chapter specifies the
v1 resource vocabulary, how a node offers and updates its resources, and - because
the user's brief is explicitly *"extensible without breaking changes"* - the rules
for growing the vocabulary and for later attaching **limits on altruism** to an
advertisement.

## What a node advertises (v1)

A node's advertisement is its [NodeInfo](04-messages.md#nodeinfo). Four fields
describe resources; the rest are identity and bookkeeping. Under the v1
[full-altruism model](README.md#the-trust-model-personal-vs-foreign), advertising a
resource is a standing, unconditional offer to use it for the mesh.

### Platform

`platform` (string) - the kind of machine, e.g. `"linux"`, `"macos"`. It is a
resource because some duties must run on a particular platform (a duty's
[spread](06-coordination.md#placement-policy) names platforms). The v1 model enumerates the
platforms it knows (`linux`, `macos`; [appendix B](appendix-b-constants.md)), each
with display metadata (an emoji, a monochrome glyph, a colour) that is presentation
only. A node MAY advertise a platform not in the model; peers treat it as an opaque
string - placement that names a known platform simply won't match it, which is the
correct, safe behavior.

### Tier

`tier` (int, `1`-`5`, default `3`) - the machine's **strength rank**, where **1 is
the strongest**. Tier is the knob that lets the mesh route *on purpose*:

- Keep a powerful workstation free for interactive use by sending grunt work to
  weaker machines (`weakest-first` - and the default `surplus-first` falls back to
  exactly this ordering when no node advertises spare-quota stats).
- Or get the fastest wall-clock by preferring the strongest (`strongest-first`).

**Auto-detection.** Tier is **auto-detected on first run** from the machine's specs
(total RAM, logical CPU count, presence of a discrete/Apple-Silicon GPU), mapped to
the `[min, max]` scale by a small scoring function (a strong box scores low = strong).
A node advertises `strengthAuto` (bool) alongside `tier` to say whether the value is
still auto-derived; an operator edit to `tier` **pins** it (`strengthAuto` → false) so
detection stops overriding the choice, and setting `strengthAuto` back to true
re-detects. Bounds live in the model (`tiers.min`, `tiers.max`, `tiers.default`); the
optional `tiers.labels` map gives UIs human words per level. `tier` is `[min, max]`-
clamped on apply ([04](04-messages.md#set-attr)). An implementation that cannot probe
specs MUST fall back to `tiers.default` rather than fail.

### Tokens

`tokens` (string, `"ok"` | `"low"` | `"out"`, default `"ok"`) - a **coarse budget
availability** signal. In Diplomat Mesh it means "this machine still has API budget
to spawn agents"; in general it is *any* consumable the operator wants placement to
respect. Semantics:

- `"ok"` - full availability; preferred.
- `"low"` - usable, but ranked **behind** `"ok"` peers of the same strategy tier
  (a soft de-prioritization, not an exclusion).
- `"out"` - exhausted; **excluded** from any duty whose placement is
  [token-aware](06-coordination.md#eligibility).

Tokens is the one resource a node is expected to update *frequently* as its budget
changes; a node MAY flip itself to `"out"` when it hits a limit and back to `"ok"`
when the budget resets, and the mesh reacts within a gossip round.

**Auto-derivation.** There is no provider API for remaining quota, so a node derives
this state from its **own measured consumption**: it sums the tokens spent over a
trailing window (`accounts.usageWindowHours`, default 5h - matching the provider's
rolling-limit cadence) from the local agent's usage logs, and compares that to a
heuristic per-plan ceiling `plan.weight × accounts.tokensPerWeight`. The fraction
remaining `1 − used/ceiling` maps to the state: `≥ accounts.lowThreshold` → `"ok"`,
`> 0` → `"low"`, `≤ 0` → `"out"`. A node advertises `tokensAuto` (bool, whether the
state is auto-derived vs pinned) and `tokensPct` (the fraction remaining, `0.0`-`1.0`)
so UIs can show a live "NN%". An operator MAY still **pin** the state (a "pause this
node" escape); a pin sets `tokensAuto` false and wins over the measurement. The ceiling
constants are deliberately rough - real limits are dynamic and account-specific - and
are model-tunable.

### Per-node stats (account-aware load balancing)

`stats` (object, optional) - a **fine-grained** load-balancing view that complements
`tokens`. Where `tokens` is the coarse three-state budget signal, `stats` carries the
continuous number a dispatcher ranks on so it can pick the node with the most spare
capacity. It is additive; a node advertising no `stats` is treated as neutral (see
below). The keys:

- **`plan`** (string) - the node's account type id: `pro`, `max-5x`, or `max-20x`.
- **`surplus`** (number) - the node's **burn-down ratio**: budget left ÷ the fraction
  of the binding rate-limit window's clock still ahead, so `1.0` is exactly on pace,
  above is flush, below is rationing. **This is the field routing ranks on.** It is a
  *relative* measure, capped at `10.0`; the owning node computes it from the real
  reset instants of its 5-hour session and 7-day week windows (the tighter binds) and
  advertises the result. Full definition in
  [11 - surplus](11-trust-and-balancing.md#surplus).
- **`usageAvg`** (number, **display-only**) - a **21-day exponential rolling average**
  of token usage, in plan-relative capacity units per day. It decays with a ~21-day
  time constant, so a burst fades over weeks. Retained for display and for peers on
  older builds; it no longer feeds the ranking.
- **`quotaLeft`** (number, **display-only**) - remaining capacity in the current quota
  window, in the same plan-relative units. Capacity is `plan weight ×
  capacityPerWeight`, so Max 20× has **4× the room** of Max 5× (weights 20 vs 5).
  Absolute token quotas are deliberately **not** modelled - Anthropic's limits are
  dynamic rolling windows - so this is plan-relative, never raw tokens.

Earlier revisions defined surplus as the *absolute* `quotaLeft − usageAvg`; it is now
the relative ratio above, because a raw remaining amount ranks two nodes backwards - a
balance that expires tonight should be drained before a bigger one that must stretch
across a week (see [11](11-trust-and-balancing.md#surplus)).

`stats` is **additive**. A node that advertises no `stats` - or a legacy peer that
advertises only the `quotaLeft`/`usageAvg` pair with no `surplus` - is treated as
`NEUTRAL_SURPLUS` (`1.0`): under [surplus-first ranking](06-coordination.md#ranking)
it ranks exactly as it would under `weakest-first`, so an old node in a mixed mesh is
never penalised (and its absolute figures are never folded into the ratio ordering).
See [06-coordination](06-coordination.md#ranking) for the ranking and
[11-trust-and-balancing](11-trust-and-balancing.md#the-load-balancer) for how stats
drive placement and dispatch.

### Device identity (trust)

`pubkey` (string, optional) - the node's advertised **Ed25519 public key**. It is
**not a resource** and grants no access: advertising it offers nothing and lets no
one place work anywhere. It exists only so a peer can **prove device identity** for
the trust boundary - a peer signs a fresh per-link challenge, and its verified key's
**fingerprint** (`sha256(pubkey)`) is matched against a **local operator allowlist**
to classify it *personal* vs *foreign*. This chapter only names the field;
[11-trust-and-balancing](11-trust-and-balancing.md) is authoritative.

### Duties enabled

`dutiesEnabled` (object<string,bool>) - a **per-duty opt-out**. A duty *absent from
the map* is enabled; a duty mapped to `false` means "I will not run this class of
work." This is how a node scopes what it offers: a machine can join the mesh purely
to run audits and decline reviews, or vice-versa. An empty map (the default) means
"I'll run anything."

## Duties

A **duty** ([01-model](01-model.md#duty)) is a class of work. The duty is a
*resource-consumer* descriptor, not a node field: it lives in the shared model with
an `id`, display metadata, and a default [placement policy](06-coordination.md).
The v1 model defines three ([appendix B](appendix-b-constants.md)):

| Duty | Default placement |
|------|-------------------|
| `review` | `surplus-first` (inherited default), token-aware, no spread |
| `conflicts` | `surplus-first` (inherited default), token-aware, no spread |
| `audit` | `surplus-first` (inherited default), token-aware, spread = **1× linux + 1× macos** |

Duties are **data**. An implementation MUST tolerate a duty it doesn't recognize:
gossip advertisements that reference it, run its placement, and dispatch it, all
without special-casing. Adding a duty is an [additive change](09-extensibility.md#adding-a-duty).

## Updating an advertisement

A node changes its advertisement by applying a [`set-attr`](04-messages.md#set-attr)
to itself - whether the edit originated locally, from a control client, or was
forwarded from a peer's control client (so one operator can retune the whole fleet
from one panel). On any effective change a node **MUST**:

1. update its in-memory attributes and persist them ([08-state](08-state.md#nodejson));
2. bump its `seq` (a new advertisement version);
3. gossip the new [NodeInfo](04-messages.md#node) to every linked peer;
4. recompute assignments ([06-coordination](06-coordination.md)).

Because placement is a pure function of advertisements, a token or tier change
ripples to identical new assignments on every node within one gossip round - no
coordination messages beyond the advertisement itself.

## Extending the resource vocabulary

The v1 vocabulary (platform / tier / tokens / duties) is intentionally minimal.
Richer resource descriptors - CPU cores, RAM, GPU presence, attached devices,
software versions, arbitrary operator-defined capabilities - are anticipated and
accommodated **without breaking changes** by these rules:

1. **Additive fields only.** A new resource is a new optional field on NodeInfo
   (or a nested object, e.g. a `resources` object grouping structured capabilities).
   v1 nodes ignore unknown fields ([09](09-extensibility.md)), so they keep
   interoperating; they simply can't *place* on a resource they don't understand.
2. **Never repurpose an existing field.** `tier`/`tokens`/`platform` keep their v1
   meaning forever. A change in meaning is a breaking change and requires a `v`
   bump ([09](09-extensibility.md#the-compatibility-contract)).
3. **Placement degrades safely.** A duty whose placement references a resource some
   nodes don't advertise treats those nodes as *not matching* that requirement
   (as with an unknown platform in a spread), never as an error.

A recommended (reserved, not yet normative) shape for structured capabilities:

```json
"resources": {
  "cpu": {"cores": 16},
  "gpu": {"present": true, "vram_gb": 24},
  "devices": ["ios-sim", "android-emu"],
  "labels": ["fast-disk", "on-ac-power"]
}
```

A future minor revision MAY define `resources` normatively and extend placement to
match on it; until then, nodes MAY include it and peers MUST ignore keys they don't
understand.

## Attaching limits to altruism (forward-looking)

v1 offers resources *unconditionally*. The **first** such limit has now landed:
per-node quota accounting (the [`stats`](#per-node-stats-account-aware-load-balancing)
object above) plus a node that **declines work it can't serve** - the beginning of
conditional altruism, specified in [11-trust-and-balancing](11-trust-and-balancing.md).
The rest of this section stays the forward-looking roadmap.

The extension path for **limiting** that altruism - the "option to add some limits
in future iterations" from the brief - attaches **terms** to an advertisement and
**policy** to a node, again additively. The full roadmap is in
[09-extensibility](09-extensibility.md#the-altruism-limits-roadmap); in summary, an
advertisement can grow optional fields such as:

- **`limits`** - caps the node offers under: e.g. `{"maxConcurrent": 2,
  "perPeer": {"…": 1}}` (at most N jobs at once; at most M from any one peer).
- **`priority` / `class`** - which peers or job classes this node prefers to serve.
- **`cost` / `accounting`** - a notion of what a job "costs", enabling fair-share
  or reciprocity later.

Correspondingly, [`job-status`](04-messages.md#job-status) can grow a `"rejected"`
status (with a reason like `"over quota"`), and dispatch already fails a slot over
to the next candidate on any non-`spawned` outcome ([07-dispatch](07-dispatch.md)),
so a node that *declines* work for policy reasons slots into the existing failover
path with **no change to v1 dispatchers**. That property - a policy-declining node
being handled by the same code that handles a dead or out-of-tokens node - is why
the limits can be added without a breaking change.

Until such fields exist, a v1 node MUST behave altruistically: accept any job for a
duty it has enabled and is [eligible](06-coordination.md#eligibility) for.
