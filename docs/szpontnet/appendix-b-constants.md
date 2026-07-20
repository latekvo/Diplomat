# Appendix B - Constants

Every default value SzpontNet v1 nodes must agree on, in one place. The canonical
source is [`core/mesh.json`](../../core/mesh.json); these are its v1 values. Two
nodes that disagree on the discovery group/ports, or whose timing values differ far
enough, will not form a healthy mesh; nodes that disagree on the *vocabulary*
(platforms/tiers/tokens/duties/strategies) still interoperate at the wire level but
may place work differently ([09](09-extensibility.md#vocabulary-skew)).

## Protocol

| Constant | Value | Used in |
|----------|-------|---------|
| protocol `version` / message `v` | `1` | every message |
| `multicastGroup` | `239.83.77.7` | [discovery](02-discovery.md) |
| `multicastPort` | `40877` | [discovery](02-discovery.md) |
| `tcpPortBase` | `40878` | [transport binding](03-transport.md#binding) |
| `tcpPortSpan` | `10` (ports `40878`-`40887`) | [transport binding](03-transport.md#binding) |
| `beaconIntervalSecs` | `2.0` | [beacons](02-discovery.md#beacons) |
| `redialIntervalSecs` | `10.0` | [redial from memory](02-discovery.md#redial-from-memory) |
| `heartbeatIntervalSecs` | `2.0` | [heartbeats](03-transport.md#link-state) |
| `peerStaleSecs` | `5.0` | [link state → `stale`](03-transport.md#link-state) |
| `peerTimeoutSecs` | `10.0` | [link state → `down`](03-transport.md#link-state) |
| `dispatchAckTimeoutSecs` | `8.0` | [remote dispatch wait](07-dispatch.md#placing-on-a-node) |
| `stateWriteIntervalSecs` | `2.0` | [snapshot write cadence](08-state.md#the-statejson-snapshot) |
| `foreignResultRetryIntervalSecs` | `5.0` | [job-result retry cadence](13-foreign-execution.md#reliable-delivery) |
| `foreignResultMaxSecs` | `120.0` | [job-result give-up window](13-foreign-execution.md#reliable-delivery) |
| `foreignJobTimeoutSecs` | `900.0` | [confined compute budget](13-foreign-execution.md#reliable-delivery) |
| `foreignCompletionDeadlineSecs` | `21600.0` (6 h) | [completion deadline on a foreign-accepted SzpontRequest](13-foreign-execution.md#the-completion-deadline) - a floor; extensions re-arm it |
| `foreignReminderGraceSecs` | `900.0` | [answer window after a `job-reminder`](13-foreign-execution.md#the-reminder) before the ban |
| `MAX_LINE_BYTES` | `524288` (512 KiB) | [framing](03-transport.md#framing) |
| UDP receive buffer | ≥ `2048` bytes | [discovery receive](02-discovery.md#receiving) |
| multicast TTL | `1` (link-local) | [discovery send](02-discovery.md#transport-multicast-plus-broadcast) |
| down-peer retention | `300` s (reference) | [snapshot retention](08-state.md#down-peer-retention) |

> Timing values are the reference defaults. An implementation MAY expose overrides
> for testing (the reference reads `DIPLOMAT_MESH_*` env vars to run fast-timed
> meshes on loopback), but nodes on the *same* mesh must use compatible values -
> in particular `peerTimeoutSecs` must exceed `heartbeatIntervalSecs` with margin,
> and `peerStaleSecs` must sit between them.

## Tiers

| Constant | Value |
|----------|-------|
| `tiers.min` | `1` (strongest) |
| `tiers.max` | `5` (weakest) |
| `tiers.default` | `3` |
| `tiers.labels` | `1`→"Very strong" … `5`→"Very light" (UI words, optional) |

Tier is clamped to `[min, max]` on apply ([04](04-messages.md#set-attr)) and
**auto-detected from specs** on first run ([05](05-resources.md#tier)).

## Tokens

| Value | Rank | Placement effect |
|-------|------|------------------|
| `ok` | `0` | preferred |
| `low` | `1` | eligible, de-prioritized behind `ok` |
| `out` | `2` | excluded from token-aware duties |
| (any other) | `1` | treated like `low` - never excluded ([09 rule 3](09-extensibility.md#the-compatibility-contract)) |

The state is **auto-derived from real usage** by default (the `tokens` node.json
override is `"auto"`); these constants set the heuristic ceiling it's measured against:

| Constant | Value | Meaning |
|----------|-------|---------|
| `accounts.tokensPerWeight` | `2000000` | tokens per unit of plan weight over the window - the per-plan ceiling is `plan.weight × this`. |
| `accounts.usageWindowHours` | `5.0` | trailing window over which local token consumption is summed. |
| `accounts.lowThreshold` | `0.34` | remaining-fraction boundary below which the state drops to `low` (`≤ 0` → `out`). |

## Trust (v1 vocabulary)

| id | notes |
|----|-------|
| `personal` | the peer's **verified** fingerprint is in my local allowlist; a received SzpontRequest runs directly. |
| `foreign` | any other device - unlisted, or it proved no key; its requests are declined by default, or run [confined and response-only](13-foreign-execution.md) when a confinement runner is configured. |
| `banned` | a device on my local [ban list](13-foreign-execution.md#the-ban) (`~/.diplomat/mesh/banned.json`) - it broke the [foreign-accountability contract](13-foreign-execution.md#accountability-deadline-reminder-ban) or was banned manually; every request declined, never a dispatch target. |

`trust.default` = `foreign` - the classification of a device **not** on the local
allowlist (zero-trust: a new device is untrusted until promoted). Configurable to
`personal` (full-trust mesh) via `set-default-trust` / `defaultLevel` in
`trusted.json`. See [11-trust-and-balancing](11-trust-and-balancing.md).

**Trust identity / files.** Each device holds an Ed25519 keypair persisted at
`~/.diplomat/mesh/device.key` (`0600`, machine-local, never gossiped); a fingerprint
is `sha256(public key)` as 64 hex chars. The trusted allowlist lives at
`~/.diplomat/mesh/trusted.json` (operator-managed, machine-local, never gossiped).

**Auth proof-of-possession construction.** The `auth` signature is over the
domain-separated bytes `"szpontnet-auth-v1:" || <nonce as UTF-8>` (ASCII tag
`szpontnet-auth-v1:` + the hello nonce), verified against the peer's advertised
`pubkey`. A verified fingerprint is bound to that exact key and discarded only if
the peer re-advertises a different `pubkey` on its own link (never from third-party
gossip). See
[11](11-trust-and-balancing.md#trust-is-never-derived-from-an-advertisement).

**Authenticated-gossip construction.** A gossiped advertisement / override carries a
`sig` field: an Ed25519 signature over `<tag> || canonical(payload)`, where
`canonical(x)` = JSON of `x` **with its `sig` removed, keys sorted, compact
separators** (`,`/`:`), and the tags are `szpontnet-nodeinfo-v1:` (advertisements),
`szpontnet-overrides-v1:` (overrides), and `szpontnet-workclaim-v1:`
([work-claims](12-work-claims.md#authentication)). The same canonical construction,
under the tag `szpontnet-jobresult-v1:`, signs a
[`job-result`](13-foreign-execution.md#correlation-and-authenticity) — a unicast link
reply rather than gossip, but signed identically so the originator can bind the
returned artifact to the executor's key. An advertisement is signed by its own device
key and verified against its own `pubkey`; an override is signed by its `updatedBy`
node and verified against that node's pinned key; a work-claim is signed by its `node`
claimant and verified against the `pubkey` carried inline; a job-result is signed by
its executor and verified against that executor's pinned key. `sig` is omitted when
empty (keyless). See [11 - authenticated gossip](11-trust-and-balancing.md#authenticated-gossip).

## Server & API key (v1 vocabulary)

| Constant | Value | Meaning |
|----------|-------|---------|
| server mode via | `DIPLOMAT_MESH_SERVER=1` | node accepts work but never dispatches to peers ([11](11-trust-and-balancing.md#the-server-role)). |
| API key via | `DIPLOMAT_MESH_API_KEY` | required `apiKey` on inbound `ctl`/`dispatch` ([11](11-trust-and-balancing.md#the-api-key)); empty = no gate. |

The `apiKey` field is optional and additive (omitted when empty), and is
orthogonal to the join `secret` and to device trust.

## Accounts (v1 vocabulary)

| Constant | Value | Meaning |
|----------|-------|---------|
| plan `pro` | weight `1` | Claude Pro - the reference weight. |
| plan `max-5x` | weight `5` | Claude Max 5× - 5× Pro's quota capacity. |
| plan `max-20x` | weight `20` | Claude Max 20× - 20× Pro's quota capacity. |
| `defaultPlan` | `max-5x` | plan assumed when a node advertises none. |
| `capacityPerWeight` | `1.0` | capacity units per unit of plan weight. |
| `jobCostUnits` | `1.0` | usage a single spawned job books against quota. |
| `quotaWindowDays` | `7.0` | length of the rolling quota window. |
| `usageTimeConstantDays` | `21.0` | time constant of the `usageAvg` rolling average. |

## Platforms (v1 vocabulary)

| id | notes |
|----|-------|
| `linux` | |
| `macos` | |

Platforms carry display metadata (`emoji`, `linuxGlyph`, `colorHex`) that is
presentation only. Unknown platforms are opaque strings.

## Strategies

| id | ranking (after token rank) |
|----|----------------------------|
| `weakest-first` (default, and the unknown-strategy fallback) | prefer larger `tier` (weaker machine) |
| `strongest-first` | prefer smaller `tier` (stronger machine) |
| `local-first` | prefer the dispatching node, then weakest-first |
| `surplus-first` | prefer the most spare quota (`quotaLeft − usageAvg`), account-aware; ties fall back to weakest-first |

`defaultStrategy` = `weakest-first`. `dispatchStrategy` = `surplus-first` - the
ranking a dispatcher uses to pick a target for a SzpontRequest (unilateral, from
its own gossiped view; separate from a duty's placement `strategy`). Full sort keys
in [06-coordination](06-coordination.md#ranking).

## Duties (v1 vocabulary)

| id | default placement |
|----|-------------------|
| `review` | `{strategy: weakest-first, tokenAware: true, spread: []}` |
| `conflicts` | `{strategy: weakest-first, tokenAware: true, spread: []}` |
| `audit` | `{strategy: weakest-first, tokenAware: true, spread: [{linux,1},{macos,1}]}` |

## Work claims (v0.2.0 vocabulary)

| Constant | Value | Meaning |
|----------|-------|---------|
| claim signing tag | `szpontnet-workclaim-v1:` | domain tag for a claim `sig` ([12](12-work-claims.md#authentication)). |
| claim `state` | `active` \| `released` | active = holding the work; any unknown value counts as **not** active. |
| owner rule | lowest node id | among **active**, **live**, **`personal`** claimants of a `workKey` ([12](12-work-claims.md#ownership)). |
| lease scope | claimant liveness | authoritative only while the claimant is `up`/`stale`; freed on `down`. |
| claim-book cap | `4096` (reference) | max stored `(workKey, claimant)` records; bounds a spoofed-`workKey` flood. |
| suppressed slot | `"claim"` / `"suppressed"` | the `dispatch-result` slot returned when a peer already owns the `workKey`. |

Work-claims are an **optional role** ([12 conformance](12-work-claims.md#conformance));
a node that omits them drops the `work-claim` message and keeps the link.

## Foreign execution (v0.3.0 vocabulary)

| Constant | Value | Meaning |
|----------|-------|---------|
| job-result signing tag | `szpontnet-jobresult-v1:` | domain tag for a `job-result` `sig` ([13](13-foreign-execution.md#correlation-and-authenticity)). |
| confinement runner via | `DIPLOMAT_MESH_FOREIGN_SPAWN` | operator's sandbox command (`{prompt_file}`/`{result_file}`); its presence enables confined foreign execution, absence = decline ([13](13-foreign-execution.md#confinement-the-executors-responsibility)). |
| result handler via | `DIPLOMAT_MESH_ON_RESULT` | originator's own-identity action on a returned result (`{result_file}`); where e.g. `gh` runs. |
| `foreignResultRetryIntervalSecs` | `5.0` | executor re-sends an unacked `job-result` this often. |
| `foreignResultMaxSecs` | `120.0` | executor gives up delivering after this (originator presumed gone). |
| `foreignJobTimeoutSecs` | `900.0` | confined compute budget before the executor returns an `ok:false` result. |

Foreign execution is an **optional role** ([13 conformance](13-foreign-execution.md#conformance));
a node that omits it drops the `job-result`/`job-ack` messages and keeps the link,
and declines foreign requests.

## Foreign accountability (v0.4.0 vocabulary)

| Constant | Value | Meaning |
|----------|-------|---------|
| `foreignCompletionDeadlineSecs` | `21600.0` (6 h) | how long a **foreign** executor that replied `spawned` (without `direct: true`) has to deliver its `job-result` before the originator sends a [`job-reminder`](04-messages.md#job-reminder). A **floor** - the originator must not remind earlier; an approved extension re-arms the full window. |
| `foreignReminderGraceSecs` | `900.0` | how long the executor has to answer the reminder (result / [`job-progress`](04-messages.md#job-progress)) before the originator **bans** it. |
| extension decider via | `DIPLOMAT_MESH_EXTEND_DECIDER` | the originator's command template (`{job_file}`) that judges a late executor's `job-progress` plea - exit `0` extends, anything else bans. **Unset = no extensions** ([13](13-foreign-execution.md#the-extension-decision)). |
| `job-status.direct` | `true` \| absent | additive flag on a `spawned` [`job-status`](04-messages.md#job-status): the executor ran the job on the personal path, no result will follow, no deadline is armed. |
| `job-progress.note` cap | `4096` bytes | receiver-side truncation of the progress note. |
| ban list | `~/.diplomat/mesh/banned.json` | machine-local, never gossiped ([08](08-state.md#bannedjson)); edited by the automatic ban and the [`ban`/`unban`](04-messages.md#ban--unban) control commands. |

Accountability is an **optional originator-side layer**
([13 conformance](13-foreign-execution.md#conformance)); a node that omits it
drops the `job-reminder`/`job-progress` messages and keeps the link - but an
executor on a mesh with accountability-tracking originators keeps its standing by
answering reminders.

## Message types

`beacon`, `hello`, `auth`, `node`, `overrides`, `heartbeat`, `set-attr`,
`dispatch`, `job-status`, `job-result`, `job-ack`, `job-reminder`, `job-progress`,
`work-claim`, `ctl`, `status`, `state`, `set-overrides`, `trust`, `untrust`,
`ban`, `unban`, `stop`, `ok`, `error`, `dispatch-result`. Full reference:
[04-messages](04-messages.md).

## Job statuses

| Value | Meaning |
|-------|---------|
| `spawned` | node accepted and started the work |
| `declined` | node refused for policy - foreign requester, missing API key, disabled duty, or out of tokens (dispatcher fails the slot over) |
| `failed` | node could not start it (dispatcher fails the slot over) |

(`completed`, … are [reserved extensions](09-extensibility.md#adding-a-job-status).)

## Reference file locations

| Path | Contents |
|------|----------|
| `~/.diplomat/mesh/node.json` | persisted identity + attributes ([08](08-state.md#nodejson)) |
| `~/.diplomat/mesh/device.key` | this device's Ed25519 private key (`0600`, never gossiped, [08](08-state.md#devicekey)) |
| `~/.diplomat/mesh/trusted.json` | local trusted-device allowlist (never gossiped, [08](08-state.md#trustedjson)) |
| `~/.diplomat/mesh/banned.json` | local ban list (never gossiped, [08](08-state.md#bannedjson)) |
| `~/.diplomat/mesh/stats.json` | local load-balancing accounting (never gossiped, [08](08-state.md#statsjson)) |
| `~/.diplomat/mesh/state.json` | public topology snapshot ([08](08-state.md#the-statejson-snapshot)) |
| overridable via | `DIPLOMAT_MESH_DIR` |
| join secret via | `DIPLOMAT_MESH_SECRET` ([03](03-transport.md#the-join-fence)) |
| server mode / API key via | `DIPLOMAT_MESH_SERVER` / `DIPLOMAT_MESH_API_KEY` ([11](11-trust-and-balancing.md#server-nodes--api-key-authentication)) |
