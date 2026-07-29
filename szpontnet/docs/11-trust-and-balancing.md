# 11 - Trust levels & surplus load balancing

Chapters 01-10 specify the SzpontNet **core**: discovery, links, gossip,
leaderless assignment, dispatch. This chapter specifies the layer built on top of
it - **who a node trusts** and **how a dispatcher chooses where work goes**. Both
are **additive** (a node that advertises no `pubkey` and no `stats`, and configures
no allowlist, behaves exactly as the core describes), so a v1 core node and a node
implementing this chapter interoperate on the same mesh.

A dispatched unit of work is called a **SzpontRequest** throughout this chapter;
on the wire it is still a [`dispatch`](04-messages.md#dispatch) message carrying a
[Job](04-messages.md#job). The name is the user-facing one - "run this for me,
whoever's best placed."

## Two trust levels

Trust exists because a *personal* SzpontRequest runs **directly** on the receiver -
staging a prompt and spawning work that can take **social actions under your
identity** (submitting a PR, commenting on GitHub via your CLI). Granting that to
the wrong peer is a privilege-escalation bug. From any node's point of view a peer
is one of two levels:

| Level | Meaning | What happens to its SzpontRequests |
|-------|---------|-----------------------------------|
| **personal** | a device *you have explicitly trusted* | run **directly**, as if you had triggered the work from your own panel |
| **foreign** | any other device | **declined** by default; or, with a [confinement runner](13-foreign-execution.md) configured, run **confined and response-only** (see [the foreign path](#the-foreign-path-zero-trust)) |
| **banned** | a foreign device that **accepted a SzpontRequest of yours and failed to deliver it** ([13 - accountability](13-foreign-execution.md#accountability-deadline-reminder-ban)), or one you banned manually | **declined** outright (`"banned device"`), runner or not; and the device is never picked as a dispatch target |

The first two are the trust *levels* proper; **banned** is a machine-local mark
layered on top (its own store, [`banned.json`](08-state.md#bannedjson), like the
allowlist never gossiped) that overrides the foreign classification with a harder
refusal. A banned device is never personal by definition - an explicit promotion
([`trust`](04-messages.md#trust--untrust)) is contradictory with a ban, so the
newest operator action wins ([13 - the ban](13-foreign-execution.md#the-ban)).

### Trust is never derived from an advertisement

**Assume every advertised field is spoofed.** A node's `id`, `name`, and any other
self-reported value are display-only and **grant zero privilege** - a stranger can
beacon any of them. Trust therefore rests on two things a stranger cannot forge:

1. **A proven device key.** Each node has an Ed25519 keypair
   ([08](08-state.md#devicekey)); its **fingerprint** is `sha256(public key)`. The
   public key is advertised (as [`pubkey`](04-messages.md#nodeinfo)), but
   *advertising it grants nothing*. On every link the peer must **prove possession**
   of the matching private key: our [`hello`](04-messages.md#hello) carries a fresh
   random `nonce`, and the peer must return an [`auth`](04-messages.md#auth)
   message signing it. Only a peer holding the private key can produce a valid
   signature, and the nonce is per-connection so a captured signature can't be
   replayed, and a peer that copies someone else's advertised `pubkey` holds no
   matching private key, so it cannot *itself* produce a signature over our challenge —
   **passive** copy-and-replay never yields a verification.

   > **Known limitation — active reflection (v1).** Proof of possession over a bare
   > per-connection nonce is **reflectable** by an *active* on-mesh adversary, because
   > the plaintext transport gives the signature no **channel binding**. An attacker A
   > holding no key can still be verified as a personal peer P by using the online P as
   > a one-shot signing oracle: A reads our fresh nonce `N` (we put it in our own hello),
   > opens a link to P and challenges it with `N`; P — which answers any hello's
   > challenge — signs `"szpontnet-auth-v1:" || N` with P's key; A replays that exact
   > `auth` back to us and is recorded as P (a foreign→personal escalation to host
   > execution, needing no packet capture). The domain tag stops the key being an oracle
   > over *non-auth* bytes, but not this *within-protocol* reflection, and no variant
   > binding only **public** values (the peers' identities or nonces, a dial/role tag)
   > closes it — an active relay forwards or supplies each one, and can flip the dial
   > direction by choosing its own id. The robust fix is an **authenticated key
   > exchange** (mutual TLS / an encrypted transport whose signatures bind the session's
   > ephemeral keys) — the [deferred transport-encryption
   > work](09-extensibility.md#non-goals-for-v1-explicitly-deferred). Until then,
   > `personal` trust on an **open** mesh is sound against *passive* adversaries only;
   > where an *active* LAN attacker is a concern, fence the mesh with a [join
   > secret](03-transport.md#the-join-fence) or a trusted network.

   **The signed message (normative).** To keep the device key from doubling as a
   generic signing oracle over attacker-chosen bytes, the signature is **not** over
   the bare nonce but over a domain-separated construction: the peer signs the
   bytes

   ```
   "szpontnet-auth-v1:" || <nonce as UTF-8>
   ```

   (the ASCII tag `szpontnet-auth-v1:` immediately followed by the nonce string),
   and the verifier checks the signature against exactly those bytes and the
   peer's advertised `pubkey`. A signature over any other construction (including
   the bare nonce) MUST NOT verify.

   **A verified fingerprint is bound to the key that was proven.** The recorded
   fingerprint names the *specific* `pubkey` the peer signed for. If the peer
   re-advertises a **different** `pubkey` **on its own link** (a fresh
   [`hello`](04-messages.md#hello)), the node **MUST** discard the verification and
   require re-proof of the new key (the accompanying `auth` re-establishes it). A
   pubkey change seen only via a **third-party gossip relay** (a `node` message that
   did not arrive on that peer's own link) **MUST NOT** clear the verification:
   otherwise any member could relay a spoofed advertisement for a personal peer P
   (a bogus `pubkey` with an inflated `seq`) to force P *personal→foreign*, and the
   inflated `seq` would outrank P's honest gossip and block recovery until P
   restarts — a persistent trust-DoS. This is safe because **trust keys on the
   *proven* fingerprint**, so an advertised-but-unproven pubkey drift never changes
   a trust decision; the classification stays on the key P actually proved.
2. **A local allowlist.** Trust is **set manually by the operator and stored only
   on this machine** ([`trusted.json`](08-state.md#trustedjson), never gossiped): a
   set of fingerprints marked as "my devices."

The executor classifies the requester from the **verified fingerprint of the link
the request arrived on** - never from the job's self-reported `requestedBy`:

```
function classify(verified_fingerprint, allowlist, default_level) -> "personal" | "foreign":
    if verified_fingerprint in allowlist:
        return "personal"                           # an explicit promotion always wins
    return default_level                            # unlisted, or never verified
```

**Zero-trust by default.** `default_level` ships **`foreign`**: a device you have
not explicitly marked personal is untrusted - a new machine that joins the mesh
cannot run your requests, [mutate your node](#mutating-a-node-is-a-personal-only-action),
or [own work](12-work-claims.md) until you promote it. The allowlist is thus the set
of **exceptions** (promotions) to a foreign baseline; you add to it deliberately with
`--trust <fingerprint>` (get a peer's fingerprint from its `--fingerprint`, shown in
`--status`, or its `state.json`).

The default level is **operator-configurable** per node - the panel's default-trust
toggle, `--default-trust <level>`, or `DIPLOMAT_MESH_DEFAULT_TRUST`; the choice persists
in [`trusted.json`](08-state.md#trustedjson) alongside the allowlist. Setting it to
**`personal`** restores the pre-trust **full-altruism** mode - every unlisted peer is
trusted, exactly as a fresh mesh behaved before the default became configurable - the
right mode for a fleet of machines you all own. Interop is unchanged either way: trust
is a purely local decision (nothing about it is on the wire), so a node running the
foreign default and a core-only node still link, gossip, and dispatch; the foreign node
simply declines requests it hasn't been told to trust.

> Because verification is symmetric and per-link, an unverified peer (an old core
> node with no key, or a lib-less keyless node) has **no** verified fingerprint, so it
> can never match the allowlist and always falls to `default_level` - `foreign` under
> the shipped default. That is the correct, conservative outcome: you never grant
> personal access to something you couldn't authenticate.

### The personal path (v1)

When a personal peer sends a SzpontRequest, the receiving node **runs it
directly** - exactly the [execution](07-dispatch.md#execution) the core describes:
stage the prompt, spawn the work, reply `spawned`. There is no extra hop. A
review SzpontRequest from your laptop runs on your desktop just as if you had
pressed the review button there yourself - full-trust altruism, scoped to the
devices you have explicitly trusted.

### The foreign path (zero-trust)

A node that receives a SzpontRequest from a foreign device has two safe options,
chosen by whether it has a [confinement runner](13-foreign-execution.md#confinement-the-executors-responsibility)
configured:

- **No runner (the default):** it **declines** (a [`declined`](#refusals-are-first-class)
  `job-status`, reason `"foreign device (no confinement runner configured)"`). The
  dispatcher's [failover](07-dispatch.md#routing-a-job) handles a declined candidate
  like any other, so a foreign node simply falls out of consideration — it costs
  nothing.
- **With a runner:** it runs the *compute* half **confined**, and returns the result
  for the requester to act on — the design realized in
  [13-foreign-execution](13-foreign-execution.md). Any **social action** —
  submitting a pull request, commenting on GitHub, anything that acts under an
  identity — is **never** executed on the foreign node; the computed artifact is sent
  **back to a personal node of the requester** (the requester itself) to perform
  there. That keeps a stranger's machine from ever acting as you, and keeps you from
  ever running its untrusted work on your host.

The **security contract below is normative** either way: an implementation that runs
foreign work (rather than declining it) MUST satisfy it, and the reference does.

#### The foreign execution security contract (normative)

A **personal** SzpontRequest runs with full trust - directly, as if you had
triggered it locally (the [personal path](#the-personal-path-v1)). A **foreign**
SzpontRequest is the opposite: **zero trust**. *How* a node sandboxes foreign work
is the implementation's choice, but the boundaries it MUST enforce are not. An
implementation that executes a foreign SzpontRequest (rather than declining it, as
v1 does) **MUST** guarantee all of:

1. **No arbitrary on-device code execution.** A foreign request MUST NOT be able to
   run code of its choosing on the host outside a sandbox. Its `prompt` is
   untrusted input to a **confined** runner (a container, VM, jailed process, or
   equivalent), never a command the host executes with its own privileges. A
   *personal* request, by contrast, MAY run directly on the host.
2. **No action under the host's identity.** A foreign request MUST NOT take any
   **social or identity-bearing action** - opening or commenting on a PR, pushing a
   commit, calling an authenticated API, touching the operator's credentials or
   secrets. Any such action MUST be **routed back to a personal node of the
   requester** to perform there. The foreign node's own reach is confined to
   producing a **response**.
3. **Request in, response out.** The permitted shape of a foreign SzpontRequest is
   exactly that: receive a request, compute, return a result. **Declared side
   effects that are inherent to the duty and confined to the executor's own
   resources are allowed** and are the client's call - e.g. launching an emulator
   or simulator, spawning a build, allocating a device - because they act only on
   the foreign machine's own hardware, not under the requester's identity. What is
   forbidden is *un*confined effect: escaping the sandbox or acting *as* the
   requester.

These are the boundaries that make "zero trust for foreign, absolute trust for
personal" real rather than advisory. A node with no confinement runner satisfies the
contract trivially by **declining** every foreign request
([refusals](#refusals-are-first-class)); a node that runs foreign compute MUST
satisfy points 1-3 while doing so. The wire mechanism that carries the result back
(and the reference implementation of the confined path) is
[13-foreign-execution](13-foreign-execution.md).

### Mutating a node is a personal-only action

Trust gates more than *executing* a job - it gates **changing a node**. A
[`set-attr`](04-messages.md#set-attr) rewrites a node's advertised
tier/tokens/duties/accounting, which reshapes placement and load balancing across
the whole mesh, and it is even *forwarded* to a named peer. A receiver **MUST**
therefore classify the sender of a **peer-link** `set-attr` from the verified link
(exactly as for a dispatch) and apply (or forward) it only for a **personal**
device; a `set-attr` from a **foreign** device MUST be ignored. A control-session
`set-attr` (the local operator, already fenced by the [join
secret](03-transport.md#the-join-fence)) is a first-party action and is not
subject to this check. As everywhere, an unlisted sender is classified by the node's
**default trust level** - `foreign` by default, so an unconfigured mesh **ignores** a
peer's `set-attr` until that peer is promoted (or the default is switched to
`personal` for a fully-owned fleet).

## Authenticated gossip

The proof-of-possession handshake authenticates a **direct link**, but an
advertisement travels further than one hop: a node's [`node`](04-messages.md#node)
update and its [`overrides`](04-messages.md#overrides) are **relayed** across the
mesh ([gossip fan-out](03-transport.md#gossip-fan-out)), and a relay is just
another peer. Without protection, a relay could **forge** an advertisement for a
node it isn't (spoof peer P's id with attacker-chosen tier/tokens/stats and an
inflated `seq`) or **tamper** with one in flight — poisoning placement and load
balancing mesh-wide, unrecoverable until the victim restarts. So every gossiped
payload is **self-signed by its originator**, and receivers verify it before
adopting or relaying.

### Signed advertisements

A node **signs its own advertisement** with its device key. The
[NodeInfo](04-messages.md#nodeinfo) carries a `sig` field: an Ed25519 signature over
the **canonical bytes** of the advertisement:

```
sig = Ed25519_sign( device_privkey, "szpontnet-nodeinfo-v1:" || canonical(nodeinfo_without_sig) )
```

where `canonical(x)` is the JSON encoding of `x` with **its top-level `sig` field
removed**, **sorted keys**, and **compact separators** (`,`/`:`), UTF-8 encoded — so
every implementation signs and verifies byte-identical input, and the signature covers
*every* field (including `pubkey`, so the key can't be swapped without breaking it).
Canonical bytes are taken over the **raw received dict**, never a re-parse, so a field a
newer signer included is still covered on an older verifier — and this is why a
relay MUST forward the advertisement verbatim (below).

> **Canonical construction (normative - the #1 interop hazard).** For the signed bytes
> to match across implementations, `canonical(x)` MUST be reproduced exactly:
>
> - **only the top-level `sig` is removed** before signing. A **nested** `sig` - e.g.
>   one inside a job-result's `result` sub-object - is **not** stripped and **is** part
>   of the signed bytes, covered by the signature.
> - **strings are ASCII-escaped** (`ensure_ascii`): every non-ASCII code point is emitted
>   as a lowercase `\uXXXX` escape. So a node `name` with non-ASCII characters signs
>   byte-identically everywhere, whatever the implementation's native string encoding.
> - **numbers:** an **integer** carries no decimal point, and any **float-typed** signed
>   field - notably `epoch` and the `stats` floats - MUST be formatted with the
>   **shortest round-trip decimal**, the algorithm that Python `repr`, ECMA-262
>   Number-to-String, Go, and Swift all use by default (the classic JSON-signing pitfall
>   is serializing numbers differently). The reference uses Python's `json`, which
>   formats floats via `repr`; a second implementation must reproduce exactly that.
>   Implementations SHOULD avoid introducing further float-typed fields into signed
>   payloads. Within one implementation (and between the reference and its conformance
>   tester, both Python) this is exact.

On receiving a `hello`/`node` advertisement, a node **MUST**:

- if it carries a `pubkey`, **verify** `sig` against that `pubkey` and **drop the
  advertisement if `sig` is absent or invalid** — a forged or tampered keyed advert
  is never adopted or relayed;
- if it carries **no `pubkey`** (a keyless/legacy node), accept it *unauthenticated*
  — there is nothing to verify, so it can never be *verified* and stays **foreign**
  under any allowlist (the same degradation as before);
- **pin id → key**: once an id is known with a `pubkey`, a **gossiped** advert
  claiming a *different* `pubkey` (even one self-signed by that other key) is a
  third party trying to hijack the id and **MUST be rejected**. Only the node's
  **own link** (a fresh [`hello`](04-messages.md#hello)) may re-key it (which then
  re-runs proof of possession). This is what stops a relay from replacing a known
  node's key — and thus its identity and trust — or downgrading it to keyless.

A relay **MUST forward an advertisement verbatim** (the exact bytes/among fields it
received), so the originator's signature survives the hop; re-serializing from a
partial parse would drop unknown fields and break the signature downstream.

### Signed overrides

A mesh-wide [placement override](06-coordination.md#placement-overrides) is signed
the same way, by its **`updatedBy`** editor:

```
sig = Ed25519_sign( editor_privkey, "szpontnet-overrides-v1:" || canonical(overrides_without_sig) )
```

A receiver **MUST** verify a non-default (`rev > 0`) override's `sig` against the
**editor's pinned key** and drop it on mismatch — so a relay can neither forge an
edit nor tamper with a real one to win the last-writer-wins race. An edit whose
`updatedBy` editor the receiver has **no key for** is **rejected** as
unauthenticatable — otherwise a forged edit under an *unknown* id with an
astronomically high `rev` could permanently mask every real edit. Such an edit
re-propagates and is adopted once the receiver learns that editor's signed
advertisement (in a full mesh it already holds every node's key from the direct
hellos; in a large partial mesh a placement edit may take an extra gossip round or
a reconnect to reach a node that hadn't yet learned the editor — a rare, self-
healing delay for a rarely-used operator action). The default (`rev 0`) override
needs no signature. A node **without a crypto library** can verify nothing and
stays in the legacy accept-everything mode — it is itself keyless, hence foreign to
everyone.

### What this closes, and what it doesn't

Authenticated gossip binds every relayed advertisement and override to the key of
the node it claims to describe, so **no relay can forge or mutate another node's
gossip**. Combined with proof of possession (direct links) and the personal-only
`set-attr` rule, **every message that changes mesh state is authenticated in some
way** - by the verified link it arrives on, or by a signature over its content.
What remains, by construction, is that a **keyless** node's gossip is
unauthenticated (it has no key to sign with) - which is exactly why a keyless node
is never trusted (foreign under any allowlist). Encrypting the gossip bytes for
*confidentiality* is still separate [future work](09-extensibility.md#non-goals-for-v1-explicitly-deferred).

## Server nodes & API-key authentication

The core is peer-to-peer and symmetric: every node both offers and dispatches
work. Two deployments want an **asymmetric** node instead, and SzpontNet supports
both **additively** (a plain v1 node needs no change to interoperate with them):

- an **altruistic pool of professionals** smoothing each other's quota spikes -
  everyone volunteers spare capacity, no one is obliged (the
  [full-altruism model](README.md#the-trust-model-personal-vs-foreign));
- a **dedicated server**: one strong machine (a build box, a device farm) that
  **accepts** work from others but **never dispatches** work of its own.

### The server role

A node MAY run in **server mode** (the reference keys it off
`DIPLOMAT_MESH_SERVER=1`). A server:

- **never originates a dispatch to a peer.** A request it is asked to route (via a
  control client or its CLI) runs on **itself**; a request explicitly
  [targeted](07-dispatch.md#explicit-target) at another node is refused. The server
  is a **sink** for work, never a source. It still beacons, links, gossips, and is
  a normal placement/dispatch *target* for other nodes - only its own origination
  is disabled.
- is otherwise an ordinary [Executor](10-conformance.md#roles) + Controllable node.

### The API key

Independently, a node MAY require an **API key** on inbound requests (the reference
reads `DIPLOMAT_MESH_API_KEY`). This is the *"accepts requests authenticated with an
optional API key"* credential, and it is **orthogonal to both the join secret and
device trust**:

- the [join `secret`](03-transport.md#the-join-fence) fences *who may join the
  mesh*;
- **device trust** (personal/foreign) decides *whose requests run with full
  privilege*;
- the **API key** authenticates *who may submit work to this node*, without
  granting mesh membership or personal trust.

When an API key is configured, the node **MUST** require a matching **`apiKey`**
field on an opening [`ctl`](04-messages.md#ctl) session and on every inbound
[`dispatch`](04-messages.md#dispatch); a control session that lacks it is closed,
and a dispatch that lacks it is **declined** (reason `"invalid or missing API
key"`) - which the dispatcher's [failover](07-dispatch.md#routing-a-job) handles
like any other decline. A dispatcher presents the key by carrying `apiKey` on the
`dispatch` it forwards (the reference lets a client set it per request, e.g.
`--api-key`). The `apiKey` field is **optional and additive**: omitted when unset,
so a node with no key is byte-compatible with a core v1 node, which simply never
sends one. This is a **plaintext credential** on the LAN, with the same threat
model as the join secret ([03](03-transport.md#the-join-fence)): a fence and an
authenticator, not a confidential channel.

## Stats

The core ranks nodes by tier and the coarse `tokens` signal. This chapter adds a
finer, **budget-aware** ranking so a dispatcher can send work to whoever actually
has spare capacity. Each node tracks its accounting locally and advertises a derived
view in the additive [`stats`](04-messages.md#nodeinfo) object
(`{"plan", "usageAvg", "quotaLeft", "surplus"}`). The number the load balancer ranks
on is **`surplus`** ([below](#surplus)) - a relative burn-down ratio. `usageAvg` and
`quotaLeft` are retained for display and for peers on older builds; they are **not**
what routing compares, and a peer reads a node's `surplus` straight from the
advertised field rather than deriving it from them.

### usageAvg - a 21-day rolling average

`usageAvg` is an **exponentially-weighted rolling average of token usage**, in
capacity units per day, with a ~21-day time constant. It is a decaying reservoir:
each unit of usage adds to `acc`; `acc` decays as `acc *= exp(-Δdays / τ)` with
`τ = usageTimeConstantDays` (21); the advertised average is `acc / τ`. A node that
consumes at a steady rate `r` settles at `usageAvg = r`; a node that goes idle sees
its average decay by `1/e` each time constant. This is the node's *typical burn* -
a **display-only** figure now. Surplus is no longer `quotaLeft − usageAvg`; it is a
burn-down ratio ([below](#surplus)), so `usageAvg` no longer feeds the ranking.

### quotaLeft - account-type aware

`quotaLeft` is the **remaining capacity in the current quota window**. Capacity is
`plan.weight × capacityPerWeight`, where the plan weight encodes the subscription
tier **relative to Pro**:

| Plan | `weight` | Relative capacity |
|------|----------|-------------------|
| `pro` | 1 | 1x |
| `max-5x` | 5 | 5x |
| `max-20x` | 20 | 20x |

So a Max 20x node has 4x the room of a Max 5x node. The window rolls every
`quotaWindowDays` (7), resetting what's been used. **Absolute token quotas are
deliberately not modelled** - Anthropic's real limits are dynamic rolling windows,
so hard-coding token counts would be brittle and wrong. `quotaLeft` is a
**display-only** figure: routing does not rank on it directly - a raw remaining
amount is not comparable across nodes (see [Surplus](#surplus)) - it feeds the live
"quota NN%" readout and peers on older builds.

> **Where the numbers come from.** The reference node books `jobCostUnits` of
> usage each time it spawns a SzpontRequest, and exposes `set-attr` keys
> (`plan`, `quotaLeft`, `usageAvg`, `usage`) to inject or correct the accounting.
> When the node's **real quota probe** is live (the OAuth usage endpoint behind
> the `tokens` auto-state), the advertised `quotaLeft` is additionally **capped
> by the binding rate-limit window**:
> `quotaLeft ≤ capacity × min(session_left, week_left)`, so the displayed figure
> never overstates the room the account actually has. Heuristic fallback estimates
> do not cap (they can read 0 for heavy users and would wrongly zero a fresh node's
> displayed quota). The "tightest window binds" reasoning that used to protect the
> ranking now lives in [surplus](#surplus) itself, which paces the *tighter* of the
> session and week windows. The *mechanism* - track, advertise, rank, decline - is
> what this chapter specifies, and it degrades safely when the inputs are neutral.

### Surplus

A node's **surplus** is the single number the load balancer ranks on - a **relative
burn-down ratio**, not an absolute amount:

```
surplus(node)      = budget_left / time_left_fraction   # for the binding window
time_left_fraction = clamp((seconds until the window resets) / (window length), 0, 1)
```

`1.0` is **exactly on the burn-down line** - the budget left is proportional to the
time left. **Above `1.0`** the account is *flush*: ahead of pace, sitting on spare
capacity that will otherwise expire unused at the reset, so spend it here. **Below
`1.0`** it is *rationing*: behind pace, so the budget it has left has to be stretched
to reach the reset.

Measuring it relatively is the entire point, because the raw remaining amount ranks
two nodes backwards:

- **60% of budget left with 2 of 7 days to the reset paces at ≈ 2.1** → **drain it**:
  that budget expires soon, so the work belongs here.
- **70% left with 6 of 7 days to the reset paces at ≈ 0.82** → **genuinely low**,
  despite the bigger number, because it has to stretch across most of a week.

Ranking on the raw remaining amount gets **both** cases backwards - it hoards the
account that is about to reset and hammers the one that must stretch.

**The binding window is the tighter of the two.** An account has a 5-hour session
window and a 7-day week window, each with its own pace; a node's surplus is the
**minimum** of the two. Both gate the next job, so a node is only as flush as its
most-rationed window - an account 3× ahead on the week but behind on the session
cannot absorb work right now, and must not out-rank a peer that can.

Surplus is **capped at `10.0`** (`PACE_CAP`): past ~10× the line a node is simply
"use it or lose it" and finer distinctions there are noise. A window whose reset is
already due (or overdue) paces at the cap - its whole balance is free; an
**exhausted** window (no budget left) paces at `0`, however close its reset.

Each node computes its **own** surplus, because only it holds the real reset instants
the [OAuth usage endpoint](05-resources.md#tokens) reports for the two windows -
pacing a peer's numbers here would compare wall-clocks across machines whose clocks
disagree. When the probe is unavailable it paces its local bookkeeping window
instead, so an offline node still advertises a comparable figure. The value rides in
`stats.surplus`, and peers rank on it directly.

A node that advertises **no `stats`**, or a **legacy peer** on an older build that
advertises only the absolute `quotaLeft`/`usageAvg` pair with **no `surplus` field**,
ranks at **`NEUTRAL_SURPLUS` (`1.0`)** - on the line, ordered between the peers ahead
of pace and those behind. Those absolute figures are a **different scale**
(plan-relative capacity units, commonly `> 1`) and are deliberately **not** converted:
folding them into the ratio ordering would let a legacy advert out-rank every paced
node. A malformed or absent `surplus` degrades to `NEUTRAL_SURPLUS`, never an error.

The advertised `surplus` drifts continuously (the time-left denominator shrinks every
second), but a node re-gossips only on a real change, and rankings compare surplus in
**buckets** of `SURPLUS_RANK_BUCKET` (`0.05`, via `round(surplus / 0.05)`) - so idle
pace drift neither churns the mesh nor reshuffles rankings on noise. See
[the load balancer](#the-load-balancer).

## The load balancer

**`surplus-first` is the default everywhere.** It drives both the **displayed** duty
ownership (the consensus [assignment](06-coordination.md), `defaultStrategy`) and a
**dispatcher's** unilateral target pick (`dispatchStrategy`) - the two are equal now.
The distinction is only *when* the ranking runs, not *which* ranking:

- The core's [assignment](06-coordination.md) is a *consensus* computation - every
  node computes the same duty owner from the **same gossiped adverts**, and that
  drives the **displayed** ownership in the panel. It reads the advertised
  `stats.surplus` verbatim (never a locally-recomputed live value), so every node
  ranks on identical numbers and the result stays
  [deterministic](06-coordination.md#determinism-requirements-normative). What makes
  this safe for a *continuously drifting* metric is the bucketing: displayed
  ownership only moves when a node's surplus crosses a `SURPLUS_RANK_BUCKET`
  boundary, not as the pace ticks down.
- **Dispatch target selection is separate and unilateral.** When a node dispatches a
  SzpontRequest it ranks candidates by `dispatchStrategy` over *its own* gossiped
  view and picks - with no agreement from anyone. It may also name an explicit
  [`target`](07-dispatch.md#routing-a-job) and send the request there directly, with
  no failover - *"Alice may forward everything to Bob, even if Bob is low."* The
  receiver is free to refuse.

`surplus-first` ranks by **descending bucketed surplus** - the sort key is
`(−surplus_bucket(surplus(n)), tok_rank, −tier, id)` - and tie-breaks with the same
`(tokens, tier, id)` order as weakest-first. So a meaningfully more flush node leads,
and when surpluses are **neutral or tied** (in particular when no node advertises
`stats`, all `NEUTRAL_SURPLUS`) the ranking degrades **exactly** to weakest-first and
the core behavior is preserved. See the [ranking table](06-coordination.md#ranking)
and [placement vs dispatch strategy](06-coordination.md#placement-strategy-vs-dispatch-strategy).

## Refusals are first-class

Because a dispatcher chooses unilaterally, the **receiver must be able to say no.**
A node replies with a [`declined`](04-messages.md#job-status) `job-status` (distinct
from `failed`) when it refuses a SzpontRequest for policy. The v1 reference declines
when:

- the requester is **foreign** (the zero-trust path above);
- the requester is **banned** ([13 - the ban](13-foreign-execution.md#the-ban) -
  reason `"banned device"`, confinement runner or not);
- the request lacks a required **API key** (a [server](#the-api-key) with a key
  configured);
- the duty is **disabled** locally (`dutiesEnabled[duty] == false` - the node opted
  out of that class of work);
- the node is **out of tokens** (`tokens == "out"` - it cannot serve; *this is Bob
  refusing the job Alice sent him anyway*).

A `declined` outcome is handled by the *exact same failover* that handles a dead or
out-of-budget candidate: any non-`spawned` status advances the slot to the next
candidate ([07](07-dispatch.md#routing-a-job)). An explicit `target` is the one
exception - it has a single candidate, so a decline there is reported as-is, no
failover (the dispatcher chose that node on purpose).

## Conformance

An implementation of this chapter:

- **MUST NOT** derive trust from any advertised field. Trust rests only on a
  verified key fingerprint against a local allowlist.
- **MUST** classify an **unknown** device - one whose verified fingerprint is not in
  the allowlist, or which has **not** proved a key (so it has no fingerprint) - by the
  node's **default trust level**, and a **listed** fingerprint always as `personal`.
  The default level **MUST** ship **`foreign`** (a new device is zero-trust until the
  operator promotes it) and **MUST** be operator-configurable; setting it to
  `personal` **MUST** restore full trust (every unlisted peer `personal`), so a
  fully-owned fleet interoperates with core-only nodes exactly as the pre-trust core
  did. Because trust is a purely local decision (never on the wire), either default
  interoperates with any other node.
- **MUST** verify proof of possession before treating a peer as `personal`: the
  peer's [`auth`](04-messages.md#auth) signature over the **domain-separated
  challenge** (`"szpontnet-auth-v1:" || nonce`, [above](#trust-is-never-derived-from-an-advertisement))
  for *our* fresh per-connection `nonce` must validate against the `pubkey` it
  advertised. It **MUST** classify the requester from that verified link identity,
  never from `requestedBy`, and **MUST** discard a verification only when the peer
  re-advertises a different `pubkey` **on its own link** — never from a third-party
  gossip relay (which would enable a trust-DoS).
- **MUST** ignore a **peer-link** `set-attr` from a **foreign** device (mutation is
  a personal-only action); a control-session `set-attr` is a first-party action.
- **MUST** [authenticate gossip](#authenticated-gossip): sign its own advertisement
  (and any override it edits) over the domain-separated canonical bytes; **verify**
  a keyed advertisement's/override's signature and **drop** it on absent/invalid
  signature; **pin** id→key so a gossiped key-swap is rejected (only the node's own
  link may re-key); and **relay advertisements verbatim** so signatures survive. A
  keyless advert is accepted unauthenticated (hence foreign).
- **MUST**, if it *executes* (rather than declines) a **foreign** SzpontRequest,
  enforce the [foreign execution security
  contract](#the-foreign-execution-security-contract-normative): sandboxed compute,
  no host-identity/social action, response-only with confined declared side effects —
  returning the result as a [`job-result`](04-messages.md#job-result) for the
  requester to act on, per [13-foreign-execution](13-foreign-execution.md).
- **MUST**, if it keeps a [ban list](13-foreign-execution.md#the-ban), decline
  every SzpontRequest from a banned device (runner or not), exclude banned devices
  from dispatch-candidate ranking, and auto-ban only a device it classifies
  **foreign** at that moment - never a personal one.
- **MUST**, if configured as an API-key [server](#the-api-key), require a matching
  `apiKey` on inbound `ctl` and `dispatch` and refuse those without it; and, if in
  [server mode](#the-server-role), never originate a dispatch to a peer.
- **MUST** omit `pubkey` and `stats` from an advertisement when they are empty, so
  a node that uses neither is byte-compatible with a core v1 advertisement.
- **MUST** treat a `declined` `job-status` as a non-`spawned` outcome (fail the
  slot over), exactly like `failed`, whether or not it understands the reason.
- **SHOULD** decline a foreign device's SzpontRequest unless it runs it via the
  confined [zero-trust foreign path](13-foreign-execution.md) — it MUST NOT run
  foreign work directly on the host.
- **SHOULD** rank dispatch targets `surplus-first` and MUST fall back to
  weakest-first ordering when surpluses are neutral or tied (including the case
  where no node advertises `stats`, so all rank at `NEUTRAL_SURPLUS`).
- **MUST**, when it ranks `surplus-first`, read a node's surplus from the advertised
  `stats.surplus` field and **MUST NOT** re-derive it from `quotaLeft`/`usageAvg`; a
  peer with no `stats`, or a legacy peer advertising only the absolute
  `quotaLeft`/`usageAvg` pair with no `surplus`, **MUST** be treated as
  `NEUTRAL_SURPLUS` (`1.0`) - never converted from those figures, never an error.
- **MAY** advertise `stats`; a node that doesn't ranks at `NEUTRAL_SURPLUS`, i.e. by
  the core weakest-first order, never as an error.

Everything here rides `v: 1` and the [compatibility contract](09-extensibility.md#the-compatibility-contract):
new optional fields (`pubkey`, `stats`, `apiKey`), new message types (`auth`), a
new enum value (`declined`), a new strategy id (`surplus-first`), all safe to add
without a version bump.
