# 04 - Message reference

Every SzpontNet message is a JSON object with a string **type** field `t` and an
integer **version** field `v` (default `1`), encoded as one newline-terminated
line ([03-transport](03-transport.md#framing)). This chapter is the exhaustive
catalog. Unless stated otherwise, a receiver **MUST** ignore fields it does not
recognize and **MUST NOT** fail on a message whose optional fields are absent.

Transport legend: **UDP** = sent as a discovery datagram; **link** = sent on a
peer TCP link; **ctl** = sent on a control session (client↔node).

| `t` | transport | direction | purpose |
|-----|-----------|-----------|---------|
| [`beacon`](#beacon) | UDP | broadcast | "I exist, dial me here" |
| [`hello`](#hello) | link | both, first message | full advertisement + overrides + trust challenge; opens a peer link |
| [`auth`](#auth) | link | both, reply to a hello | proof of possession: signs the peer's hello `nonce` |
| [`node`](#node) | link | gossip | an updated advertisement |
| [`overrides`](#overrides) | link | gossip | updated placement overrides (LWW) |
| [`heartbeat`](#heartbeat) | link | both | liveness keep-alive |
| [`set-attr`](#set-attr) | link / ctl | to a node | change a node's advertised attributes |
| [`dispatch`](#dispatch) | link / ctl | to a node | run a job here |
| [`job-status`](#job-status) | link | reply | outcome of a dispatch (`spawned` / `declined` / `failed`) |
| [`job-result`](#job-result) | link | executor→originator | the computed artifact a **foreign** request returns; the originator then acts on it ([13](13-foreign-execution.md)) |
| [`job-ack`](#job-ack) | link | originator→executor | acknowledges a `job-result` (reliable delivery) ([13](13-foreign-execution.md)) |
| [`job-reminder`](#job-reminder) | link | originator→executor | "is this ready?" - an accepted foreign SzpontRequest passed its completion deadline ([13](13-foreign-execution.md#accountability-deadline-reminder-ban)) |
| [`job-progress`](#job-progress) | link | executor→originator | reply to a `job-reminder` when the work is still running - the executor's case for an extension ([13](13-foreign-execution.md#accountability-deadline-reminder-ban)) |
| [`work-claim`](#work-claim) | link | gossip | a self-signed origination lease on a unit of work ([12](12-work-claims.md)) |
| [`ctl`](#ctl) | ctl | client→node, first message | opens a control session |
| [`status`](#status) | ctl | client→node | request the state snapshot |
| [`state`](#state) | ctl | node→client | the state snapshot (reply to `status`) |
| [`set-overrides`](#set-overrides) | ctl | client→node | edit a duty's placement policy |
| [`trust`](#trust--untrust) | ctl | client→node | add a fingerprint to the local allowlist |
| [`untrust`](#trust--untrust) | ctl | client→node | remove a fingerprint from the local allowlist |
| [`ban`](#ban--unban) | ctl | client→node | add a device to the local ban list ([13](13-foreign-execution.md#the-ban)) |
| [`unban`](#ban--unban) | ctl | client→node | remove a device from the local ban list |
| [`set-default-trust`](#set-default-trust) | ctl | client→node | set the default trust level for unlisted devices |
| [`claim`](#claim--claim-result) | ctl | client→node | run the origination claim gate for a work key, without dispatching ([12](12-work-claims.md)) |
| [`claim-result`](#claim--claim-result) | ctl | node→client | the claim gate's verdict (reply to `claim`) |
| [`stop`](#stop) | ctl | client→node | ask the node to shut down |
| [`ok` / `error`](#ok--error) | ctl | node→client | generic command results |
| [`dispatch-result`](#dispatch-result) | ctl | node→client | per-slot dispatch outcomes |

Two composite objects recur inside messages and are defined first:
[**NodeInfo**](#nodeinfo) (the resource advertisement) and [**Job**](#job).

---

## Composite objects

### NodeInfo

The resource advertisement for one node. Appears inside `hello` and `node`, and
(decorated with link fields) inside the [`state`](#state) snapshot.

```json
{
  "id": "3236817363144d8dbd842ec2973506c2",
  "name": "softoobox",
  "platform": "linux",
  "tier": 4,
  "tokens": "ok",
  "tcpPort": 40878,
  "epoch": 1784057237.23,
  "seq": 12,
  "sees": ["bd4eaf7671d24b9792bcfd09762ac5b5"],
  "dutiesEnabled": {"audit": false},
  "pubkey": "kQ0f…base64-Ed25519-public-key…=",
  "stats": {"plan": "max-20x", "usageAvg": 3.1, "quotaLeft": 20.0},
  "v": 1
}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string | **yes** | stable, mesh-unique node id. A NodeInfo without a usable `id` is invalid and MUST be dropped. |
| `name` | string | no (`"?"`) | human label; presentation only, never used for identity or placement. |
| `platform` | string | no (`"unknown"`) | machine kind (`"linux"`, `"macos"`, …); a *resource* - see [05](05-resources.md#platform). |
| `tier` | int | no (`3`) | machine strength, 1 = strongest - see [05](05-resources.md#tier). |
| `strengthAuto` | bool | no (`true`) | whether `tier` is auto-detected from specs (vs pinned). Display hint - see [05](05-resources.md#tier). |
| `tokens` | string | no (`"ok"`) | **effective** budget state: `"ok"`/`"low"`/`"out"` - see [05](05-resources.md#tokens). |
| `tokensAuto` | bool | no (`true`) | whether `tokens` is auto-derived from real usage (vs a manual pin). Display hint. |
| `tokensPct` | float | no (`1.0`) | fraction of the heuristic token ceiling remaining (`0.0`-`1.0`), for a live "NN%" readout. |
| `tcpPort` | int | no (`0`) | the node's TCP listen port. |
| `epoch` | float | no (`0`) | incarnation stamp; increases each process (re)start. |
| `seq` | int | no (`0`) | per-incarnation update counter. |
| `sees` | array<string> | no (`[]`) | ids of peers this node currently holds a link to (for topology display + partition awareness). |
| `dutiesEnabled` | object<string,bool> | no (`{}`) | per-duty opt-out; a duty absent from the map is **enabled** by default. |
| `pubkey` | string | no | the node's advertised base64 Ed25519 public key. Advertising it grants **nothing** - a peer must prove possession by signing the [`hello`](#hello)/[`auth`](#auth) challenge before it is believed to hold this key. Trust then keys on its fingerprint `sha256(pubkey)` against a local allowlist. See [11-trust-and-balancing](11-trust-and-balancing.md). |
| `stats` | object | no (`{}`) | load-balancing accounting: `{"plan", "usageAvg", "quotaLeft"}` in plan-relative units. See [05-resources](05-resources.md#per-node-stats-account-aware-load-balancing) and [11](11-trust-and-balancing.md). |
| `sig` | string | no | base64 Ed25519 signature by this node's device key over the advert's canonical bytes, authenticating it end to end across relays. A **keyed** advert (one with a `pubkey`) MUST carry a valid `sig` or be dropped; a keyless advert carries none. See [11 - authenticated gossip](11-trust-and-balancing.md#authenticated-gossip). |
| `v` | int | no (`1`) | protocol version of this advertisement. |

`pubkey`, `stats`, and `sig` are **additive and optional**: all are **omitted from
the wire form when empty**, so a v1 advertisement that sets none is byte-identical
to before. The `stats` sub-keys are `plan` (string, account-type id, e.g.
`max-20x`), `usageAvg` (float, 21-day rolling average of usage per day), and
`quotaLeft` (float, remaining capacity in the current window).

**Freshness.** Two NodeInfos for the same `id` are ordered by the tuple
`(epoch, seq)`: the larger wins. A restart (higher `epoch`) always supersedes the
prior incarnation regardless of `seq`; within one incarnation, the higher `seq`
is newer. Receivers MUST keep only the freshest NodeInfo per id and MUST NOT let
an older one overwrite a newer one. See [08-state](08-state.md#liveness--incarnations).

Validation: if `id` is missing, or a present numeric field fails to parse as its
type (e.g. `tier` is `"abc"`), the whole NodeInfo is invalid and MUST be dropped
(not partially applied).

### Job

A unit of dispatched work. A Job is the payload of what the spec and UI call a
**SzpontRequest** (the user-facing name for a dispatched unit of work); the wire
type stays [`dispatch`](#dispatch) carrying a `job`.

```json
{
  "id": "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6",
  "duty": "audit",
  "prompt": "…the work payload, an opaque string…",
  "requestedBy": "3236817363144d8dbd842ec2973506c2",
  "requestedAt": 1784057240.5
}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string | **yes** | unique job id (dispatcher-assigned). |
| `duty` | string | **yes** | the duty this job belongs to. |
| `prompt` | string | no (`""`) | opaque work payload; SzpontNet does not interpret it. |
| `requestedBy` | string | no (`"?"`) | node id of the dispatcher. |
| `requestedAt` | float | no (now) | dispatcher's timestamp. |
| `workKey` | string | no (`""`) | the origination-dedup key this job executes ([12](12-work-claims.md)). When set, the **executor** claims it on spawn and holds the lease for the agent's lifetime; empty on a server-local / explicit-target / manual dispatch. Additive — a pre-claims node ignores it. |

A Job missing `id` or `duty` is invalid and MUST be dropped.

---

## Discovery message

### `beacon`

UDP presence advert. Small enough for one datagram; sent to the multicast group
and (off loopback) the subnet broadcast - see [02-discovery](02-discovery.md).

```json
{"t": "beacon", "id": "3236…", "name": "softoobox",
 "platform": "linux", "tcpPort": 40878, "epoch": 1784057237.23, "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | sender's node id. |
| `name` | string | sender's name. |
| `platform` | string | sender's platform. |
| `tcpPort` | int | **the port to dial for a link.** Receiver MUST ignore the beacon if missing/≤0. |
| `epoch` | float | sender's incarnation; a higher value than a linked peer's means it restarted. |

The beacon intentionally omits `tier`/`tokens`/`dutiesEnabled` - the authoritative
advertisement travels in the [`hello`](#hello), keeping beacons tiny.

---

## Peer-link messages

### `hello`

First message on a peer link, sent by **both** sides (the dialer sends it on
connect; the accepter sends it in reply). Carries the sender's full advertisement,
its current placement overrides, a fresh per-connection trust challenge (`nonce`),
and - if a [join fence](03-transport.md#the-join-fence) is configured - the shared
secret.

```json
{"t": "hello",
 "node": { …NodeInfo… },
 "overrides": { …PlacementOverrides, see 06… },
 "secret": "optional-shared-secret",
 "nonce": "a1b2c3d4e5f6…",
 "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `node` | NodeInfo | **yes** | the sender's advertisement. |
| `overrides` | object | no (`{}`) | the sender's placement overrides ([06](06-coordination.md#placement-overrides)); merged LWW. |
| `secret` | string | conditional | present iff a join fence is configured; MUST match. |
| `nonce` | string | no | a fresh per-connection trust challenge (hex). The peer must return an [`auth`](#auth) signing it to prove possession of the private key for its advertised `pubkey`. |

On receiving a valid `hello` a node: validates the secret; records/updates the
peer's NodeInfo (by freshness); binds this link's writer to that peer; merges the
`overrides`; answers the `nonce` with an [`auth`](#auth); and recomputes
assignments. See [03-transport](03-transport.md#the-join-fence) for the
authentication ordering an unauthenticated link MUST enforce before accepting
anything other than a hello.

### `auth`

Proof of possession, sent in reply to the `nonce` in a peer's [`hello`](#hello). It
carries a signature proving the sender holds the private key for the `pubkey` it
advertised.

```json
{"t": "auth", "sig": "…base64-Ed25519-signature…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `sig` | string | **yes** | base64 Ed25519 signature over the domain-separated challenge (below). |

**The signed message is domain-separated** — not the bare nonce, but the bytes
`"szpontnet-auth-v1:" || <nonce as UTF-8>` (so the device key can't double as a
generic signing oracle). The exact construction is normative in
[11](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement).

The handshake is **symmetric**: both link directions send a `hello` (each with its
own `nonce`) and answer the other's challenge with an `auth`. A receiver verifies
the `sig` against the domain-separated form of the nonce **it** issued and the
peer's advertised `pubkey`; on success it records the peer's **verified
fingerprint** (`sha256(pubkey)` - what trust keys on), bound to that exact key and
**discarded only if the peer re-advertises a different `pubkey` on its own link**
(never from third-party gossip - see [11](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement)).
A bad or absent signature leaves the peer **unverified**, hence *foreign* under any
configured allowlist. See [11-trust-and-balancing](11-trust-and-balancing.md).

### `node`

A gossiped advertisement update - the sender relaying a (possibly other node's)
fresher NodeInfo across the mesh.

```json
{"t": "node", "node": { …NodeInfo… }, "v": 1}
```

Receiver first **authenticates** the advertisement — a keyed NodeInfo with an
absent/invalid `sig`, or one that tries to change a known id's `pubkey` via gossip,
is **dropped** ([11 - authenticated gossip](11-trust-and-balancing.md#authenticated-gossip)).
It then merges by [freshness](#nodeinfo): adopt only if newer than what is held for
that `id`; if adopted, re-propagate **verbatim** (the exact received `node` dict, so
the originator's signature survives the hop) and recompute assignments. A `node` for
the receiver's own `id` is ignored.

### `overrides`

A gossiped [placement-overrides](06-coordination.md#placement-overrides) update.

```json
{"t": "overrides", "overrides": {"rev": 3, "updatedBy": "3236…", "duties": { … },
                                 "sig": "…base64…"}, "v": 1}
```

The `overrides` object carries an optional `sig`: an Ed25519 signature by the
`updatedBy` node over the override's canonical bytes. A receiver **authenticates** a
non-default (`rev > 0`) override against the editor's pinned key and **drops** it on
a bad/absent signature ([11](11-trust-and-balancing.md#signed-overrides)); it then
adopts it only if it **wins** the last-writer-wins comparison against the overrides
it holds (higher `rev`, ties broken by `updatedBy`); if adopted, re-propagate and
recompute. See [06-coordination](06-coordination.md#placement-overrides).

### `heartbeat`

Liveness keep-alive, sent on every link every `heartbeatIntervalSecs`.

```json
{"t": "heartbeat", "ts": 1784057241.0, "v": 1}
```

`ts` is the sender's timestamp (informational). Receiving *any* message refreshes
a peer's liveness, but heartbeats guarantee traffic on an otherwise idle link so
[link state](03-transport.md#link-state) stays `up`.

### `set-attr`

Ask a node to change its own advertised attributes. Used both peer→peer-forwarded
and from a [control session](#control-messages): a UI can edit *any* node's
attributes, and the request is forwarded over the mesh to the target.

```json
{"t": "set-attr", "target": "bd4eaf…", "attrs": {"tokens": "out", "tier": 2}, "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `target` | string | node id to edit; `""`, `"self"`, or the local id all mean *this* node. |
| `attrs` | object | attributes to apply (see below). |

**Applying `attrs`** (a node applying it to *itself*): each recognized key is
validated and applied; unknown keys and invalid values are ignored (the sender may
be a newer or older peer).

| `attrs` key | Type | Effect |
|-------------|------|--------|
| `name` | string | set the node's name (trimmed; non-empty; reference caps length at 64). |
| `tier` | int | set the tier, **clamped** to the model's `[min, max]`; **pins** strength (`strengthAuto` → false) ([05](05-resources.md#tier)). |
| `strengthAuto` | bool | re-enable (`true`) or disable auto-detection; enabling immediately re-detects the tier from specs. |
| `tokens` | string | set the token **override**; ignored unless one of `"auto"`/`"ok"`/`"low"`/`"out"`. `"auto"` returns the node to real-usage derivation. |
| `dutiesEnabled` | object<string,bool> | merge per-duty enable flags. |
| `plan` | string | switch the accounting plan ([11](11-trust-and-balancing.md#quotaleft---account-type-aware)). |
| `quotaLeft` | number | set remaining quota directly (clamped to plan capacity). |
| `usageAvg` | number | set the rolling usage average directly. |
| `usage` | number | book a usage delta against the quota window. |

The last four are the **accounting** keys (chapter 11); they update the node's
[`stats`](05-resources.md#per-node-stats-account-aware-load-balancing), not its
core NodeInfo fields. Like the others, an unrecognized key or invalid value is
ignored.

If `target` names a **peer** (not self), the receiver **forwards** the `set-attr`
over that peer's link (it does not apply it locally). A node that applies a change
MUST bump its `seq`, persist the new attributes, gossip the new NodeInfo, and
recompute assignments.

> **`set-attr` is a personal-only action.** Because a `set-attr` rewrites
> placement- and balancing-affecting attributes and is forwarded across the mesh,
> a receiver MUST classify the sender of a **peer-link** `set-attr` from its
> verified link and act only for a **personal** device — a `set-attr` from a
> **foreign** device MUST be ignored. A control-session `set-attr` (the local
> operator) is a first-party action and is exempt. See
> [11 — mutating a node](11-trust-and-balancing.md#mutating-a-node-is-a-personal-only-action).
> (Empty allowlist = personal, so an unconfigured mesh is unaffected.)

### `dispatch`

Ask a node to run work now. `dispatch` has **two shapes** depending on the
transport - they are distinct and MUST both be supported by their respective
receivers:

**On a peer link** - a fully-formed [Job](#job) to run *on the receiving node*:

```json
{"t": "dispatch", "job": { …Job… }, "apiKey": "optional-server-key", "v": 1}
```

The receiver runs the job locally ([07-dispatch](07-dispatch.md#execution)) and
replies with a [`job-status`](#job-status). On an unauthenticated link a bare
`dispatch` MUST be rejected per [the fence ordering rule](03-transport.md#the-join-fence).
The optional `apiKey` field carries the credential a
[server](11-trust-and-balancing.md#the-api-key) with an API key configured
requires; it is omitted when unset (so a core v1 node never sends one), and a
server that requires it and doesn't receive a match replies `declined`.

**On a [control session](#control-messages)** - a request to *route* a job through
the mesh, carrying the `duty` and `prompt` as **top-level** fields (the node mints
the Job id and does the [slot routing](07-dispatch.md#routing-a-job) itself):

```json
{"t": "dispatch", "duty": "audit", "prompt": "…the work payload…", "v": 1}
```

The node replies with a [`dispatch-result`](#dispatch-result) (the per-slot
outcomes), not a `job-status`. An unknown `duty` yields an [`error`](#ok--error).

An optional `target` field (a node id) names one node to run the request on
directly - the dispatcher's unilateral pick, with **no failover**. When `target`
is absent the node ranks candidates itself (surplus-first). An optional `apiKey`
field carries the credential to forward to an API-key-gated
[server](11-trust-and-balancing.md#the-api-key) target. An optional `workKey`
field opts the request into [origination dedup](12-work-claims.md#integration-with-dispatch):
the node claims the key first and, if a better peer already owns the work, replies
with a single `"suppressed"` slot instead of dispatching. The peer-link shape is
unchanged (it carries a `job`, never a `target`/`workKey`).

> The two shapes exist because a peer link dispatches *one job to this node*, while
> a control client asks *this node to place a job across the mesh on its behalf*.
> Don't wrap the control-session form's `duty`/`prompt` in a `job` object.

### `job-status`

The outcome of a `dispatch`, sent back to the dispatcher.

```json
{"t": "job-status", "id": "b1c2…", "status": "spawned", "reason": "", "node": "bd4eaf…", "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | the Job id this is answering. |
| `status` | string | `"spawned"` (the node started the work), `"declined"` (refused for policy), or `"failed"`. |
| `reason` | string | human-readable detail when `status` = `"declined"` or `"failed"`; else `""`. |
| `node` | string | the id of the node reporting (the executor). |
| `direct` | bool | optional, additive (v0.4.0; omitted when false): the executor ran this `spawned` job on the [personal path](11-trust-and-balancing.md#the-personal-path-v1), so **no `job-result` will follow**. An originator MUST NOT arm a [completion deadline](13-foreign-execution.md#the-completion-deadline) for it. |

A dispatcher **MUST** correlate a `job-status` to its request by Job `id` **and**
accept it only from the peer it dispatched that job to; a `job-status` for an
unknown id, or one arriving from any other link, MUST be dropped (so a third peer
that learns a live job id cannot resolve someone else's dispatch).

> v1 defines three statuses: `spawned`, `declined`, and `failed`. `spawned` means
> the node *accepted and started* the work, not that the work *completed* -
> SzpontNet tracks placement and hand-off, not job completion. `declined` means the
> receiver **refused for policy** - a foreign requester, a required API key that was
> missing, a locally-disabled duty, or being out of tokens - with the `reason`
> explaining it.
>
> The dispatcher treats any non-`spawned` status (both `failed` and `declined`) as
> "this candidate didn't take it - fail over to the next." The sole exception is an
> explicit [`target`](#dispatch): that outcome is reported as-is, with no failover.
> Additional statuses are a reserved extension ([09](09-extensibility.md)).

### `job-result`

The computed artifact a **foreign** (zero-trust) SzpontRequest returns to its
originator, who then performs any social action itself. Sent by the executor on the
same link the [`dispatch`](#dispatch) arrived on, correlated by Job `id`, and
**re-sent until acknowledged** by a [`job-ack`](#job-ack). Full semantics in
[13-foreign-execution](13-foreign-execution.md).

```json
{"t": "job-result", "id": "b1c2…", "node": "bd4eaf…",
 "result": {"ok": true, "duty": "review", "output": "…the artifact…", "error": ""},
 "sig": "…base64…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `id` | string | **yes** | the Job `id` this result answers. |
| `node` | string | **yes** | the executor's node id. |
| `result` | object | **yes** | `{"ok": bool, "duty": string, "output": string, "error": string}` — the computed payload; `output` is the opaque artifact the originator acts on, `error` is set when `ok` is `false`. |
| `sig` | string | no | Ed25519 signature by the executor over `"szpontnet-jobresult-v1:" ‖ canonical({id,node,result})`. A **keyed** executor MUST sign; a keyless one omits it. |

A receiver **MUST** accept a `job-result` only from the peer it dispatched that Job
`id` to (the responder-link gate, like [`job-status`](#job-status)) and — when that
executor is keyed — only with a valid `sig` against its pinned key; it **drops** an
unknown-id, wrong-link, or badly-signed result. `output` rides one NDJSON line, so
it is bounded by [`MAX_LINE_BYTES`](#encoding-rules-summary); a larger artifact is
truncated by the executor. See [13](13-foreign-execution.md).

### `job-ack`

The originator's acknowledgement of a [`job-result`](#job-result), by Job `id`.
Stops the executor's retry loop — reliable delivery, not fire-and-forget.

```json
{"t": "job-ack", "id": "b1c2…", "node": "3236…", "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | the Job `id` being acknowledged. |
| `node` | string | the acknowledging (originator) node id. |

An executor **MUST** accept a `job-ack` only from the node it owes that result to.
The originator **MUST** ack every recognized result — a duplicate included — and act
on it **at most once**. See [13-foreign-execution](13-foreign-execution.md).

### `job-reminder`

The originator's **"is this ready?"** - sent when a **foreign** executor's
accepted SzpontRequest passes its completion deadline without a result
([13 - accountability](13-foreign-execution.md#accountability-deadline-reminder-ban)).
Correlated by Job `id`, sent on the executor's link.

```json
{"t": "job-reminder", "id": "b1c2…", "node": "3236…", "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | the Job `id` being asked about. |
| `node` | string | the asking (originator) node id. |

An executor **MUST** accept a `job-reminder` only from the requester it received
that Job from (the same responder-link gate as everywhere), and MUST answer a
recognized one truthfully: with the [`job-result`](#job-result) if the compute
finished (re-arming its delivery retries), or with a
[`job-progress`](#job-progress) if the work is still running. A reminder for an
unrecognized Job id is dropped. What the originator does with the answer - or
its absence - is specified in
[13](13-foreign-execution.md#resolution-fulfilled-extended-or-banned).

### `job-progress`

The executor's reply to a [`job-reminder`](#job-reminder) when the work is
**not** ready: a status note that is the executor's case for a deadline
extension. The originator judges it (by its configured
[extension decision](13-foreign-execution.md#the-extension-decision) - an agent's
call, not a rule); an unpersuasive or unjudgeable note leads to a
[ban](13-foreign-execution.md#the-ban).

```json
{"t": "job-progress", "id": "b1c2…", "node": "bd4eaf…",
 "note": "review 70% done, large diff - need ~1h more", "v": 1}
```

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | the Job `id` this reports on. |
| `node` | string | the reporting (executor) node id. |
| `note` | string | human-readable status + justification; truncated by the receiver at 4 KiB. |

An originator **MUST** accept a `job-progress` only from the peer it dispatched
that Job `id` to, and only while a reminder for it is outstanding; anything else
is dropped. Like `job-status` (and unlike `job-result`), it is **unsigned**: it
is gated by the responder link alone, since it influences only the originator's
local extension decision, never a social action.

### `work-claim`

A gossiped, self-signed **origination lease** on a unit of work: how nodes that
independently observe the same external event agree that only one of them
originates it. Full semantics in [12-work-claims](12-work-claims.md).

```json
{"t": "work-claim", "claim": {
   "workKey": "review:github.com/acme/app#123@abc123",
   "node": "3236…", "pubkey": "kQ0f…=",
   "epoch": 1784057237.23, "seq": 0, "state": "active", "sig": "…base64…"}, "v": 1}
```

| `claim` field | Type | Req? | Meaning |
|-------|------|------|---------|
| `workKey` | string | **yes** | the unit of work; a claim without a non-empty `workKey` MUST be dropped. |
| `node` | string | **yes** | the claimant node id; a claim without a non-empty `node` MUST be dropped. |
| `pubkey` | string | no | the claimant's advertised base64 Ed25519 key, carried inline so the claim self-authenticates. Omitted by a keyless claimant. |
| `epoch` | float | no (`0`) | the claimant's incarnation (its node `epoch`), so a restart supersedes its prior leases. |
| `seq` | int | no (`0`) | the claimant's per-`workKey` counter; the freshest same-claimant record wins. |
| `state` | string | no (`"active"`) | `"active"` (holding the work) or `"released"` (given up). An unknown value MUST be treated as **not active**. |
| `sig` | string | no | base64 Ed25519 signature by the claimant over `"szpontnet-workclaim-v1:" ‖ canonical(claim)`. A **keyed** claim MUST carry a valid `sig` or be dropped; a keyless claim carries none. |

A receiver **authenticates** the claim (drop a keyed claim with an absent/invalid
`sig`, or one whose `pubkey` disagrees with the claimant's pinned key), merges it
into its claim book by `(epoch, seq)` freshness per `(workKey, node)`, re-propagates
an adopted claim **verbatim** (so the signature survives the hop), and recomputes the
key's owner. The **owner** is the lowest-id **active** claimant that is **live** and
**`personal`** - a keyless, foreign, or `down` claimant never owns. See
[12-work-claims](12-work-claims.md). A node that does not implement work-claims
simply drops this message and keeps the link ([09 rule 2](09-extensibility.md#the-compatibility-contract)).

---

## Control messages

Control messages flow on a **control session**: a TCP connection a client opens
with a `ctl` first message instead of a `hello`. The node answers each command
with exactly one reply line.

### `ctl`

Opens a control session. If a [join fence](03-transport.md#the-join-fence) is
configured, MUST carry the matching `secret`; if the node is an
[API-key server](11-trust-and-balancing.md#the-api-key), MUST also carry the
matching `apiKey`.

```json
{"t": "ctl", "secret": "optional-shared-secret", "apiKey": "optional-server-key", "v": 1}
```

The node validates the secret (and, if configured, the API key) and then reads
commands until the client disconnects. Both fields are omitted when unset.

### `status`

Request the node's live state snapshot.

```json
{"t": "status", "v": 1}
```

Reply: one [`state`](#state) message.

### `state`

The node's state snapshot, sent in reply to `status`. Its `state` field has the
same shape as the persisted [`state.json`](08-state.md#the-statejson-snapshot) - the whole
topology as this node sees it.

```json
{"t": "state", "state": {
   "updatedAt": "2026-07-16T04:31:02.517Z",
   "pid": 12345,
   "tcpPort": 40878,
   "self": { …NodeInfo…, "fingerprint": "…64-hex…" },
   "peers": [ { …NodeInfo…, "link": "up", "addr": "192.168.1.21", "lastSeenSecsAgo": 1.2,
               "verified": true, "fingerprint": "…64-hex…", "trust": "personal", "surplus": 1.75 } ],
   "trusted": [ {"fingerprint": "…64-hex…", "label": "mbp"} ],
   "banned": [ {"fingerprint": "…64-hex…", "node": "bd4e…", "label": "flaky-box",
                "reason": "accepted SzpontRequest b1c2… and failed to deliver", "bannedAt": 1784057240.5,
                "jobId": "b1c2…"} ],
   "assignments": {"review": {"duty": "review", "assigned": ["…"], "shortfall": []}},
   "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
   "v": 1
}, "v": 1}
```

The `state` reply is the **same object** as the on-disk
[`state.json`](08-state.md#the-statejson-snapshot), including its `updatedAt`/`pid`/`v`
envelope — a client gets the identical snapshot live or from disk. Alongside the
link/addr decoration, the snapshot carries the trust + balancing view: `self` gains
its own `fingerprint`; each peer entry gains `verified` (bool - whether the peer
proved a key on this link), `fingerprint` (its **verified** fingerprint, or the
fingerprint of its advertised `pubkey` when not yet verified), `trust`
(`personal`/`foreign`/`banned` against the local allowlist and ban list), and
`surplus` (float - its spare-quota rank score); a top-level `trusted` array lists
the local allowlist as `{fingerprint, label}` entries; and a top-level `banned`
array mirrors the local [ban list](08-state.md#bannedjson) as
`{fingerprint, node, label, reason, bannedAt, jobId}` entries.

See [08-state](08-state.md#the-statejson-snapshot) for the full snapshot schema.

### `set-overrides`

Edit one duty's [placement policy](06-coordination.md#placement-overrides)
mesh-wide. The node bumps the last-writer-wins `rev`, applies it, and gossips it.

```json
{"t": "set-overrides", "duty": "review",
 "placement": {"strategy": "strongest-first", "tokenAware": true, "spread": []}, "v": 1}
```

Reply: [`ok`](#ok--error), or [`error`](#ok--error) if `duty` is unknown or
`placement` is malformed.

### `trust` / `untrust`

Edit this node's **local** trust allowlist. `trust` adds a fingerprint (with an
optional operator label); `untrust` removes one.

```json
{"t": "trust", "fingerprint": "a1b2…64-hex…", "label": "mbp", "v": 1}
{"t": "untrust", "fingerprint": "a1b2…64-hex…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `fingerprint` | string | **yes** | the device fingerprint (`sha256(pubkey)`, 64 hex) to add or remove. |
| `label` | string | no | human label stored alongside a trusted fingerprint (`trust` only). |

Both reply [`ok`](#ok--error) (or [`error`](#ok--error) when `fingerprint` is
missing). This edits **machine-local** state (`~/.diplomat/mesh/trusted.json`) and is
**never gossiped** - trust is each operator's own call. See
[11-trust-and-balancing](11-trust-and-balancing.md).

### `ban` / `unban`

Edit this node's **local** ban list
([13 - the ban](13-foreign-execution.md#the-ban)). `ban` marks a device banned
(the manual counterpart of the automatic accountability ban); `unban` reverses a
ban - the operator's recovery path.

```json
{"t": "ban", "fingerprint": "a1b2…64-hex…", "node": "bd4e…", "label": "flaky-box",
 "reason": "manual", "v": 1}
{"t": "unban", "fingerprint": "a1b2…64-hex…", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `fingerprint` | string | one of these two | the device fingerprint (`sha256(pubkey)`, 64 hex) to ban/unban. |
| `node` | string | one of these two | node id, for a keyless device that has no fingerprint (best-effort mark). |
| `label` | string | no | human label stored alongside (`ban` only). |
| `reason` | string | no | why (`ban` only; defaults to `"manual"`). |

Both reply [`ok`](#ok--error) (or [`error`](#ok--error) when neither
`fingerprint` nor `node` is given). A manual `ban` of a fingerprint currently on
the trusted allowlist also **removes it from the allowlist** (the two states are
mutually exclusive, and the operator's newest word wins). This edits
**machine-local** state (`~/.diplomat/mesh/banned.json`) and is **never
gossiped** - a ban is each operator's own mark.

### `set-default-trust`

Set this node's **default trust level** - how a device *not* in the allowlist is
classified. `level` is `"personal"` or `"foreign"`.

```json
{"t": "set-default-trust", "level": "personal", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `level` | string | **yes** | `"foreign"` (zero-trust default — a new device is untrusted until promoted) or `"personal"` (full-trust — every unlisted peer trusted). |

Replies [`ok`](#ok--error) (or [`error`](#ok--error) for an unrecognised `level`).
Like `trust`/`untrust` this edits **machine-local** state (persisted as `defaultLevel`
in `~/.diplomat/mesh/trusted.json`) and is **never gossiped**; it takes effect live,
re-classifying every unlisted peer on the next snapshot. See
[11-trust-and-balancing](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement).

### `claim` / `claim-result`

Run the [origination claim gate](12-work-claims.md#origination-and-yield) for one
unit of external work **without dispatching** - for a control client that will run
the work *itself* (the applet's auto-monitor spawns a local, tracked agent when it
originates). The node consults the claim book, and either announces its own active
claim (`owned: true` - proceed) or stands the caller down (`owned: false` - a
better live personal peer holds the lease). Re-claiming a key this node already
owns is idempotent, so a legitimate retry is never suppressed.

```json
{"t": "claim", "workKey": "review:github.com/acme/app#123@abc123", "v": 1}
{"t": "claim-result", "owned": false, "owner": "3236…", "ownerName": "softoobox", "v": 1}
```

| Field | Type | Req? | Meaning |
|-------|------|------|---------|
| `workKey` | string | **yes** | the unit of work ([12 - the work key](12-work-claims.md#the-work-key)). An empty key is an [`error`](#ok--error). |
| `owned` | bool | reply | whether this node now originates the work. |
| `owner` | string | reply | the current owner's node id (`null` when nobody owns it - only possible with `owned: true`). |
| `ownerName` | string | reply | the owner's human name, for the caller's log line. |

A [`dispatch`](#dispatch) with a `workKey` runs this same gate internally and
reports `suppressed`; the stand-alone verb exists so origination dedup does not
require routing the execution through the mesh.

### `stop`

Ask the node to shut down cleanly. Reply: [`ok`](#ok--error).

```json
{"t": "stop", "v": 1}
```

### `ok` / `error`

Generic command results.

```json
{"t": "ok", "v": 1}
{"t": "error", "reason": "unknown duty 'foo'", "v": 1}
```

### `dispatch-result`

Reply to a control-session [`dispatch`](#dispatch): the per-slot outcomes of
routing the job through the mesh.

```json
{"t": "dispatch-result", "duty": "audit", "results": [
   {"slot": "linux", "node": "3236…", "nodeName": "softoobox", "status": "spawned", "reason": ""},
   {"slot": "macos", "node": "bd4e…", "nodeName": "mbp-weak", "status": "spawned", "reason": ""}
], "v": 1}
```

Each entry reports one [slot](07-dispatch.md#slots): which slot it was — a
`platform` for a spread duty, `"any"` for a no-spread duty, `"target"` for an
explicit [target](#dispatch), `"server"` for a
[server node](11-trust-and-balancing.md#the-server-role) running the request
locally, or `"claim"` for a request [suppressed](12-work-claims.md#integration-with-dispatch)
because a peer already owns its `workKey` (status `"suppressed"`, with the owner in
`node`/`nodeName`) — which node took it (`node`/`nodeName`, `null` if none did), and
the `status`/`reason`. See [07-dispatch](07-dispatch.md).

---

## Encoding rules (summary)

- One object per line, compact (no interior newlines), UTF-8, `\n`-terminated.
- Always include `t` (string) and `v` (int, default 1).
- Lines longer than `MAX_LINE_BYTES` (512 KiB) are dropped.
- Unknown `t` → drop the message, keep the link. Unknown fields → ignore them.
- A malformed line is never fatal to the link (except an over-length line, which
  MAY close it). See [09-extensibility](09-extensibility.md) for the full
  compatibility contract.
