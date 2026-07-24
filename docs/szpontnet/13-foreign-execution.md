# 13 - Foreign zero-trust execution (confined compute, response-back)

The [trust model](11-trust-and-balancing.md#two-trust-levels) has two levels. A
**personal** SzpontRequest runs *directly* on the receiver, with full trust, and
may take social actions under your identity ([the personal
path](11-trust-and-balancing.md#the-personal-path-v1)). A **foreign** request is
the opposite - zero trust - and in the base protocol it is simply
[**declined**](07-dispatch.md#refusal-policy). This chapter specifies the third
option: a receiver that *does* want to help a stranger's machine can run the
**compute** for a foreign request under confinement and hand the **result** back
for the requester to act on - without ever letting the stranger's work act as the
receiver, or the receiver act as the requester.

This is the concrete realization of the normative [foreign execution security
contract](11-trust-and-balancing.md#the-foreign-execution-security-contract-normative)
(sandboxed compute, no host-identity action, request-in / response-out). That
contract says *what* a foreign executor must guarantee; this chapter specifies the
**wire mechanism** - two messages and a reliable-delivery rule - that lets it do so
and return the answer.

Foreign execution is an **optional** layer, like [work-claims](12-work-claims.md).
A node that only ever runs personal work, and declines every foreigner, is fully
conformant and interoperates unchanged (the new messages ride additive types, [09
rule 2](09-extensibility.md#the-compatibility-contract)). What this layer adds is
the ability to **share spare compute with machines you have not personally
trusted** - a stronger box reviewing a PR for a colleague whose device isn't on
your allowlist - while keeping every identity-bearing action on the requester's own
machine.

## The shape: request in, response out

The permitted shape of a foreign SzpontRequest is exactly that of a pure function:
**receive a request, compute, return a result.** The worked example, with Alice
foreign to Bob:

1. **Alice dispatches** a review SzpontRequest to Bob (an ordinary
   [`dispatch`](04-messages.md#dispatch) carrying a [Job](04-messages.md#job); it is
   fine for Alice to just send a prompt).
2. **Bob classifies Alice foreign** from the [verified
   link](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement) -
   never from the job's `requestedBy`.
3. **Bob accepts, confined.** Instead of declining, Bob runs the compute inside a
   [sandbox](#confinement-the-executors-responsibility) and replies
   [`job-status: spawned`](04-messages.md#job-status) - the hand-off ack, exactly as
   for a personal job. `spawned` means *accepted and started*, not *finished*.
4. **Bob's agent is programmatically barred** from acting under Bob's identity: it
   cannot use `gh`, push commits, comment on a PR, or reach Bob's credentials
   (they are not in its environment), but it *may* use Bob's own confined resources
   - launch an emulator/simulator, run code in a container, spawn a build.
5. **Bob computes the review** and writes it to the result file the sandbox was
   given.
6. **Bob returns the result** to Alice as a [`job-result`](#the-messages) on the
   same link the request arrived on, correlated by Job `id`, **signed** with Bob's
   device key.
7. **Alice acknowledges** it with a [`job-ack`](#the-messages) (reliable delivery -
   not fire-and-forget), and **performs the social action herself**: she submits the
   review via `gh`, under *her* identity, on *her* machine.

The invariant across the whole exchange: **the identity-bearing action always runs
on the originator's own node, under the originator's own credentials.** Bob never
holds or uses Alice's credentials, and never acts as Alice; Alice never runs Bob's
untrusted compute on her host. Each machine only ever acts as itself.

> **Why the originator, specifically.** The security contract says a social action
> must route "back to a **personal** node of the requester." The requester is
> trivially personal to itself, so returning the result to the originator satisfies
> the contract directly: Alice submitting the review she asked for is her own
> first-party action, not a stranger acting for her.

## Confinement: the executor's responsibility

*How* a node isolates foreign work is the **implementation's** choice - the protocol
does not mandate a specific sandbox - but the boundaries are normative (they are the
[security contract](11-trust-and-balancing.md#the-foreign-execution-security-contract-normative)).
An implementation that runs (rather than declines) a foreign request **MUST**:

1. **Confine the compute.** The untrusted `prompt` MUST run inside a sandbox (a
   container, VM, jailed process, or equivalent), never as a command the host
   executes with its own privileges. A *personal* request MAY run directly on the
   host; a foreign one MUST NOT.
2. **Withhold host identity.** The confined runner MUST NOT be able to take any
   social or identity-bearing action or reach the operator's credentials/secrets.
   In particular it MUST NOT inherit host credentials into its environment **or via
   the filesystem** — the sandbox MUST isolate the interior's environment *and* its
   view of `HOME`/dotfiles (`~/.ssh`, `~/.netrc`, `~/.aws`, `~/.config/gh`, …). A
   node MAY scrub obvious app-secret variables before launching the runner as a
   backstop, but that is defence in depth, **not** the boundary: a container/VM that
   starts the interior with a fresh env and filesystem is what actually enforces
   this. Do not treat a scrubbed launcher environment as sufficient isolation.
3. **Return, don't act.** The runner's only sanctioned output is the **result** it
   returns. Declared side effects confined to the executor's *own* resources
   (spawning a build, launching an emulator, allocating a device) are permitted;
   escaping the sandbox or acting under the requester's identity is not.

> **The reference confinement.** The reference gates foreign execution on an
> operator-supplied sandbox command, `DIPLOMAT_MESH_FOREIGN_SPAWN` (with
> `{prompt_file}`/`{result_file}` placeholders - e.g. a `docker run` wrapper).
> **Its absence means no foreign execution**: a foreign request is declined, exactly
> as the base protocol does. So a node only ever runs a stranger's compute when the
> operator has explicitly supplied the jail to run it in - isolation is opt-in, and
> the opt-in *is* the sandbox. As defence in depth the reference also scrubs the
> child's environment of every credential-bearing variable (anything whose name
> carries `TOKEN`/`SECRET`/`KEY`/`PASSWORD`/… , including the mesh join secret and
> API key) and prepends a response-only preamble to the prompt. The protocol does
> not require these specific mechanisms - only that the three boundaries above hold.

## The messages

Two additive message types carry the response and its acknowledgement. Both ride on
the peer link, correlated to the request by Job `id`.

### `job-result`

Sent by the **executor** to the **originator** when the confined compute produces
its artifact.

```json
{"t": "job-result", "id": "b1c2…", "node": "bd4eaf…",
 "result": {"ok": true, "duty": "review", "output": "…the computed artifact…", "error": ""},
 "sig": "…base64-Ed25519-signature…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string | **yes** | the Job `id` this result answers. |
| `node` | string | **yes** | the executor's node id (who computed it). |
| `result` | object | **yes** | the computed payload (below). |
| `sig` | string | no | base64 Ed25519 signature by the executor over the result's canonical bytes ([authenticity](#correlation-and-authenticity)). A **keyed** executor MUST sign; a keyless one omits it. |

The `result` object:

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `ok` | bool | **yes** | whether the compute succeeded and `output` is meaningful. |
| `duty` | string | no | the duty this answers (informational; the originator already knows it from correlation). |
| `output` | string | no (`""`) | the opaque artifact the originator acts on (e.g. the review body). SzpontNet does not interpret it. |
| `error` | string | no (`""`) | human-readable failure detail when `ok` is `false`. |

`output` travels inside one NDJSON line, so it is bounded by
[`MAX_LINE_BYTES`](04-messages.md#encoding-rules-summary) (512 KiB) with headroom
for the envelope; a larger artifact is truncated by the executor. A `job-result`
carries no free-form numeric field (its payload is strings and a bool), so the
general rule that a **non-finite numeric field is dropped**
([04](04-messages.md#encoding-rules-summary)) leaves it untouched - the same holds
for `job-status` and `job-ack`, whose only number is the integer `v`.

### `job-ack`

Sent by the **originator** to the **executor** to acknowledge a `job-result`. It
stops the executor's [retries](#reliable-delivery).

```json
{"t": "job-ack", "id": "b1c2…", "node": "3236…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string | **yes** | the Job `id` being acknowledged. |
| `node` | string | **yes** | the acknowledging (originator) node id. |

## Correlation and authenticity

A `job-result` changes what the originator *does* (it triggers a social action), so
it is authenticated two ways - the same posture as a
[`job-status`](07-dispatch.md#placing-on-a-node) plus a signature:

- **Responder-link gate.** The originator MUST accept a `job-result` **only from the
  peer it dispatched that Job to**, and only for a Job `id` it actually dispatched
  there. A result for an unknown id, or one arriving on any other link, MUST be
  dropped - so a third peer that learns a live Job id cannot inject a result for
  someone else's dispatch. (Symmetrically, the executor MUST accept a `job-ack` only
  from the node it owes the result to.)
- **Signature bind.** The signed bytes are

  ```
  "szpontnet-jobresult-v1:" || canonical({"id", "node", "result"})
  ```

  where `canonical(x)` is the JSON of `x` with its `sig` removed, keys sorted, and
  compact separators - byte-identical to the construction used for
  [adverts/overrides/claims](11-trust-and-balancing.md#authenticated-gossip) under
  this message's **own** domain tag. A **keyed** executor (one that advertises a
  `pubkey`) MUST sign, and the originator MUST verify the signature against the
  executor's [pinned key](11-trust-and-balancing.md#authenticated-gossip) and **drop
  the result on an absent or invalid signature**. A **keyless** executor carries no
  signature; its result is accepted on the responder-link gate alone (the same safe
  degradation as a keyless advertisement). The signature binds the artifact to the
  executor's key so neither a relay nor another peer on the link can forge or tamper
  with it.

The originator classifies and correlates from the **link**, never from the
`result`'s self-reported `node` field, exactly as trust everywhere keys on the
verified link rather than an advertised value.

## Reliable delivery

Unlike a fire-and-forget `job-status`, a `job-result` carries the *product* of the
work and MUST be delivered reliably - the originator has to act on it exactly once.

- **Executor: retry until acked.** After computing, the executor sends the
  `job-result` and **re-sends** it every `foreignResultRetryIntervalSecs`
  ([appendix B](appendix-b-constants.md)) until it receives a matching `job-ack`,
  giving up after `foreignResultMaxSecs` (the originator is then presumed gone). A
  re-send uses the originator's *current* link, so a flapped-and-healed link resumes
  delivery.
- **Originator: ack every recognized result, act at most once.** The originator MUST
  `job-ack` **every** `job-result` it recognizes - including a duplicate that arrives
  because its earlier ack was lost - so the executor's retries stop. It MUST perform
  the social action **at most once** per Job `id`: a duplicate is re-acked but never
  re-acted. (The reference records acted-on Job ids and de-duplicates against them.)
- **Compute bound.** The executor bounds the confined compute at
  `foreignJobTimeoutSecs`; on timeout it returns a `job-result` with `ok:false` and
  an `error`, so the originator is never left waiting indefinitely.

Both ends bound their bookkeeping (in-flight results owed, dispatches awaiting a
result, and acted-on ids) against a flood, and expire entries by time, so a
never-acking originator or a never-returning executor cannot grow memory without
bound.

## Accountability: deadline, reminder, ban

Reliable delivery bounds the *transport* of a result, but nothing above bounds
its *production*: a foreign executor that replies `spawned` and then simply never
delivers costs the originator the work it deferred, silently. Because a foreign
device is by definition one the operator has **not** vouched for, an acceptance
is the only promise it ever makes - and v0.4.0 makes that promise **binding**.
This layer is the originator's: it needs no cooperation beyond the two messages
below, and a device that breaks the promise is **banned** - marked, machine-local,
so the operator can see exactly who burned them.

### The completion deadline

When an originator dispatches a SzpontRequest to a peer that **it classifies
foreign** ([11](11-trust-and-balancing.md#two-trust-levels), from its own
allowlist) and receives a [`job-status: spawned`](04-messages.md#job-status) that
does **not** carry `direct: true` (below), the executor has **accepted** the work
and now owes a [`job-result`](#the-messages). The originator arms a **completion
deadline** of `foreignCompletionDeadlineSecs`
([appendix B](appendix-b-constants.md) - default **21600 s / 6 hours**) from the
acceptance.

The deadline is a **floor, not a cap**: an originator MUST NOT hold the executor
to account before it elapses (the executor is entitled to the full window), and
the window can be **extended** - but only by the originator's own
[extension decision](#the-extension-decision), never unilaterally by the
executor. A valid result that arrives before the deadline resolves the entry
normally - including an honest `ok: false` failure, which is a *timely answer*,
not a broken promise.

**The `direct` escape hatch.** Trust is asymmetric: an executor that classifies
the *requester* personal runs the job on the [personal
path](11-trust-and-balancing.md#the-personal-path-v1) - directly, fire-and-forget,
with **no** `job-result` ever following. So that an accountability-tracking
originator does not ban an executor for a result that was never owed, v0.4.0 adds
an **additive** bool field `direct` to `job-status`: an executor that ran a
`spawned` job on the personal path SHOULD set `direct: true`, and an originator
MUST NOT arm a completion deadline for a `spawned` that carries it. Absent (every
older node) or `false` means a result will follow. See
[Limitations](#limitations-v1) for what this deliberately does not defend.

### The reminder

When the deadline passes unresolved, the originator sends a
[`job-reminder`](04-messages.md#job-reminder) - "is this ready?" - on the
executor's link, correlated by Job `id`, and gives the executor
`foreignReminderGraceSecs` ([appendix B](appendix-b-constants.md) - default
**900 s**) to answer. Delivery of the reminder is best-effort and links flap, so
the originator SHOULD **re-send it across the grace window** (the reference
re-asks on the `foreignResultRetryIntervalSecs` cadence); the grace clock runs
from the **first** ask either way - being unreachable does not stretch the
window, but a link that happens to be down at the deadline instant doesn't turn
a held result into a silence ban. An executor that receives a valid reminder (correlation
gate: only for a job it received, only from that job's requester link) MUST
answer truthfully with one of:

- the **[`job-result`](#the-messages)** itself, if the compute finished - a
  reminder also **revives a given-up delivery**: an executor SHOULD retain a
  computed result (the artifact, not necessarily the in-memory message) for at
  least `foreignCompletionDeadlineSecs + foreignReminderGraceSecs` after computing it,
  and answer a reminder by re-sending it with the normal
  [retry loop](#reliable-delivery) re-armed. This is what saves an honest
  executor whose originator was unreachable past `foreignResultMaxSecs`;
- a **[`job-progress`](04-messages.md#job-progress)** - the work is genuinely
  still running (or queued), with a human-readable `note` saying where it stands.
  The note is the executor's *case for an extension*; it is judged, not trusted.

A reminder for a job the executor does not recognize (e.g. it restarted and lost
it) is **dropped** - under this layer, losing accepted work *is* failing to
deliver it.

### Resolution: fulfilled, extended, or banned

Within the grace window, exactly one of four things happens:

1. **A valid ready result arrives** (passes the [correlation and
   authenticity](#correlation-and-authenticity) gates, `ok: true`) → the promise
   is **fulfilled**; the entry resolves; the originator acts on the result as
   normal.
2. **A `job-progress` arrives** → the originator runs the
   [extension decision](#the-extension-decision). Approved → the deadline
   **re-arms for another full `foreignCompletionDeadlineSecs` window** (the cycle
   can repeat, each round re-judged). Declined → **ban**.
3. **A non-fulfilling reply arrives** - a `job-result` with `ok: false` → after
   six-plus hours and a direct ask, an answer that does not fulfill the task is
   a broken promise → **ban**. (A malformed or unauthentic reply is simply
   **dropped** by the [correlation gates](#correlation-and-authenticity), so the
   grace window runs out and resolution 4 bans for silence - same verdict,
   reached as a timeout.)
4. **Nothing arrives** by the end of the grace window → **ban**.

### The extension decision

Whether a late executor's "still working" plea is credible is a **judgement
call, so an agent makes it** - not a fixed rule. The originator hands the case -
the Job (duty, prompt), who the executor is, when it accepted, how many
extensions it has already received, and the `job-progress` note - to a
locally-configured **decider**, and the decider's verdict is final: extend
(re-arm the full window) or ban.

The reference exposes this as `DIPLOMAT_MESH_EXTEND_DECIDER`, a command template
run with `{job_file}` substituted by a JSON file carrying exactly that case; exit
status `0` extends, anything else bans. The operator typically points it at an
agent (the same pattern as `DIPLOMAT_MESH_FOREIGN_SPAWN` /
`DIPLOMAT_MESH_ON_RESULT`). **Unset, no extension is ever granted**: a progress
plea then cannot save the executor and resolution 2 collapses into a ban - the
zero-trust default, consistent with every other opt-in in this chapter. A decider
that crashes, times out (the reference bounds it by the grace window), or answers
malformed grants **no** extension.

### The ban

A ban is the originator **marking, for its operator**, that this device accepted
work and failed to deliver it. Like the allowlist it is **machine-local and never
gossiped** - each operator's own ledger of who burned them
([`banned.json`](08-state.md#bannedjson), mirrored in the
[state snapshot](08-state.md#the-statejson-snapshot) so UIs can show it). A ban
records the device's **verified fingerprint** (the identity that proved itself on
the link the acceptance arrived on) - or, for a device that advertised a key but
never proved it, the fingerprint of that **advertised** key: signed
advertisements mean the device actually holds it, and a deny-side mark on a
claimed identity only ever costs the claimant. A keyless executor is recorded by
node id alone - a weaker, best-effort mark. Alongside the identity the record
keeps the node id, its last known name, the Job id it broke its promise on, the
reason, and when.

From the moment of the ban, the banning node treats the device as trust level
**`banned`** ([11](11-trust-and-balancing.md#two-trust-levels)):

- **every** SzpontRequest from it is **declined** (reason `"banned device"`) -
  even with a confinement runner configured; the confined path is a favor, and
  favors end here;
- it is **never selected** as a dispatch target (excluded from candidate
  ranking; an explicit [`target`](04-messages.md#dispatch) naming it is refused
  locally);
- every other privilege gate that requires `personal` (mutation via `set-attr`,
  [work-claim](12-work-claims.md) ownership) already excludes it, since banned is
  never personal;
- outstanding accountability entries for the device are dropped (one broken
  promise resolves them all);
- the **peer link itself stays**: a ban withdraws privileges, not mesh
  membership - the [join fence](03-transport.md#the-join-fence) governs who may
  be on the mesh, and gossip/liveness continue so the operator can still see the
  device (marked banned) in the topology.

Two hard rules bound the automation. An automatic ban MUST only ever apply to a
device the originator classifies **foreign at the moment of the ban** - if the
operator promoted it to personal mid-flight, the pending accountability entry is
disarmed instead (trusting a device and auto-banning it are contradictory, and
the operator's explicit promotion wins). And a ban is **the operator's to
reverse**: the [`unban`](04-messages.md#ban--unban) control command (and its
`ban` counterpart for a manual, preemptive mark) edits the same machine-local
store, exactly as `trust`/`untrust` edit the allowlist.

### Reference bookkeeping

The reference originator holds its accountability ledger **in memory** alongside
the awaiting-result entries it already keeps (same cap, but an armed entry lives
until it resolves - fulfilled, extended, or banned - rather than expiring on the
short result TTL). A node restart therefore forgets in-flight accountability
(the promise dies with the incarnation; no ban fires) - only the **bans
themselves** persist, in [`banned.json`](08-state.md#bannedjson). The deadline
check rides the heartbeat tick, next to the result-retry and reaping passes.

## Admission: when a foreign request is confined vs declined

The receiver's [admission check](07-dispatch.md#refusal-policy) chooses among three
outcomes from the [verified](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement)
requester classification and local policy:

- **personal** → run **directly** on the host ([the personal
  path](11-trust-and-balancing.md#the-personal-path-v1)); reply `spawned` (with
  [`direct: true`](#the-completion-deadline), since no result will follow).
- **foreign**, and a confinement runner is configured → run **confined,
  response-only** (this chapter); reply `spawned`; the result follows as a
  `job-result`.
- **foreign**, and **no** confinement runner is configured → **decline** (the safe
  default: reason `"foreign device (no confinement runner configured)"`).
- **banned** ([accountability](#the-ban)) → **decline** (reason `"banned
  device"`), runner or not.

A **disabled duty** or being **out of tokens** declines regardless of trust - a node
that cannot serve the work refuses it outright rather than sandboxing it. A confined
run also requires a **requester link to return the result on** - an established link
with a known node id to address the [`job-result`](#the-messages) to. This is *not* a
proven Ed25519 fingerprint: a foreign requester may be **keyless** yet still
legitimately receive its confined result (correlation rides the
[responder link](#correlation-and-authenticity), not the requester's key). What the
run cannot tolerate is *no* link at all - absent a requester id to route the result
back to, the receiver reports the slot `failed` rather than running a stranger's code
for nobody.

The dispatcher needs no new logic: a `spawned` from a confined executor looks
exactly like any other hand-off, and a decline fails the slot over as always. The
*result* arrives later, out of band, on the persistent link.

## Conformance

Foreign execution defines an **optional role**, the **Confined-Executor** (and its
counterpart, the **Result-Originator**). A node need not implement it; if it
*executes* a foreign SzpontRequest rather than declining it, it MUST:

1. **Confine.** Enforce the [three boundaries](#confinement-the-executors-responsibility):
   sandboxed compute, no host-identity action or credential access, output confined
   to the returned result.
2. **Classify from the link.** Decide *foreign* from the verified link fingerprint
   against the local allowlist, never from `requestedBy`.
3. **Return a signed result.** Send the artifact as a [`job-result`](#the-messages)
   correlated by Job `id`; a keyed executor MUST sign it over
   `"szpontnet-jobresult-v1:" || canonical({id,node,result})`.
4. **Deliver reliably.** Re-send until `job-ack`ed (bounded by `foreignResultMaxSecs`);
   bound the confined compute by `foreignJobTimeoutSecs`, returning `ok:false` on
   timeout.

A **Result-Originator** (a node that dispatches to a possibly-foreign executor and
acts on the returned result) MUST:

5. **Correlate and authenticate.** Accept a `job-result` only from the peer it
   dispatched that Job to; verify a keyed executor's signature against its pinned key
   and **drop** on a bad/absent one.
6. **Ack and act once.** `job-ack` every recognized result (duplicates included) and
   perform the social action **at most once** per Job `id`, under its **own**
   identity.

[Accountability](#accountability-deadline-reminder-ban) (v0.4.0) adds obligations
to both roles. A **Confined-Executor** MUST answer a valid
[`job-reminder`](04-messages.md#job-reminder) truthfully (the result if computed,
a [`job-progress`](04-messages.md#job-progress) if still running), SHOULD retain a
computed result for the accountability window so a reminder can revive its
delivery, and SHOULD mark a personal-path `spawned` with `direct: true`. A
Result-Originator that implements accountability MUST NOT remind before
`foreignCompletionDeadlineSecs` has elapsed since the acceptance, MUST NOT arm a
deadline for a `spawned` carrying `direct: true`, MUST grant the full
`foreignReminderGraceSecs` before banning, MUST grant an extension **only**
through its configured [extension decision](#the-extension-decision) (never
because the executor asked), and MUST auto-ban only a device it classifies
**foreign** at that moment. A node that implements no accountability simply drops
the two messages ([09 rule 2](09-extensibility.md#the-compatibility-contract)) -
but on a mesh where originators do implement it, an executor that stays silent
after accepting work will be banned by them; answering reminders is how a
conformant executor keeps its standing.

A node that implements **neither** role MUST still, per the [compatibility
contract](09-extensibility.md#the-compatibility-contract), **drop the unknown
`job-result`/`job-ack` message and keep the link** (rule 2). A foreign requester it
receives is simply declined ([07](07-dispatch.md#refusal-policy)) - so a
confinement-aware node and a base node share a mesh without trouble.

## Security properties

- **No stranger acts as you.** A foreign request never runs on the host and never
  reaches host credentials; the only thing that leaves the sandbox is the returned
  artifact. The identity-bearing action runs on the **originator's** node, under the
  originator's identity - proven by construction, not by the foreign node's good
  behaviour.
- **No forged results.** A `job-result` is honored only from the exact executor the
  Job went to (responder-link gate) and, when that executor is keyed, only with a
  valid signature over its canonical `{id,node,result}` - so a relay or a third peer
  on the link can neither invent nor tamper with a result. A keyless executor is
  gated by the link alone, the same degradation as keyless gossip.
- **Exactly-once *action*, at-least-once *delivery*.** Reliable retry guarantees the
  result is delivered; the originator's per-Job-id de-duplication guarantees the
  social action fires at most once even under duplicate delivery or a lost ack.
- **No amplification.** Verifying a result is one signature check; both ends'
  bookkeeping is size-bounded and time-expired; a duplicate result is a cheap re-ack.
  A flood of results for unknown Jobs is dropped by the correlation gate.

## Limitations (v1)

- **Sandbox strength is the operator's.** The protocol guarantees the *routing* (no
  host-identity action here, the social action on the originator) and mandates *that*
  a sandbox confine the compute, but the isolation quality of the chosen sandbox is
  the node's own responsibility. A weak jail weakens confinement; it never weakens
  the routing invariant, because the executor holds no credentials to leak in the
  first place.
- **Trust is one-directional.** Bob confining Alice's request protects **Bob**. It
  does not vouch for the *content* of Bob's result to Alice: Alice runs the returned
  artifact through her own social action, so she SHOULD treat the `output` as she
  would any proposed change - review before it becomes a PR - rather than as trusted
  because a peer produced it. A malicious executor can return a bad review, but not
  *submit* one as Alice.
- **Result confidentiality.** The `job-result` is authenticated but, like the rest of
  the wire, **not encrypted** - transport confidentiality remains [future
  work](09-extensibility.md#non-goals-for-v1-explicitly-deferred). Don't route a
  secret-bearing artifact across an untrusted LAN until an encrypted transport lands.
- **Not exactly-once *compute*.** The reliability here is on result *delivery* and
  the originator's *action*, not on the executor running the Job exactly once; the
  Job itself is still a [fire-once dispatch](07-dispatch.md#idempotency--duplicates)
  (origination dedup lives in [work-claims](12-work-claims.md)).
- **Accountability can be dodged, not gamed.** An executor can escape the
  [completion deadline](#the-completion-deadline) by replying `spawned` with
  `direct: true` and never delivering - but that buys it nothing a pre-v0.4.0
  executor didn't already have (unaccountable acceptance was the *only* mode
  before), and an originator remains free to simply not dispatch to devices it
  finds unreliable. What the layer guarantees is the converse: an executor that
  *does* owe a result can no longer be silently late forever - it delivers,
  makes a case an agent accepts, or is banned. Bans are also only as strong as
  the identity they mark: a **keyless** executor is banned by node id, which a
  determined stranger can re-mint - one more reason keyless devices are already
  foreign everywhere.
- **A restart clears the reference's in-flight ledger.** The reference keeps
  accountability entries in memory ([bookkeeping](#reference-bookkeeping)), so an
  originator that restarts mid-window forgets the promise and no ban fires;
  persisted, restart-surviving accountability is future work. Bans themselves
  persist.
