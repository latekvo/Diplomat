# Appendix A — Annotated session trace

A complete two-node session, message by message, to ground the abstract chapters.
Two machines join, link, agree on placement, retune a resource, and dispatch a job.

Wire messages are shown pretty-printed; on the wire each is one compact
newline-terminated line ([03-transport](03-transport.md#framing)). Node ids are
shortened to `aaaa…`/`bbbb…` for readability (real ids are 32 hex chars).

**Cast**

- **N-A** — `id: aaaa…`, name `softoobox`, `platform: linux`, `tier: 4`,
  `tokens: ok`, listening on TCP `40878`, `epoch: 1000`.
- **N-B** — `id: bbbb…`, name `mbp`, `platform: macos`, `tier: 1`,
  `tokens: ok`, listening on TCP `40878` (different host), `epoch: 1000`.

Because `aaaa… < bbbb…`, **N-A will be the dialer** ([02](02-discovery.md#the-dial-rule-smaller-id-dials)).

---

## 1. Discovery

Both nodes beacon every 2 s to `239.83.77.7:40877` (and subnet broadcast).

**N-A → group (UDP):**
```json
{"t":"beacon","id":"aaaa…","name":"softoobox","platform":"linux","tcpPort":40878,"epoch":1000,"v":1}
```

**N-B → group (UDP):**
```json
{"t":"beacon","id":"bbbb…","name":"mbp","platform":"macos","tcpPort":40878,"epoch":1000,"v":1}
```

N-A hears N-B's beacon. It records `bbbb… @ 192.168.1.21:40878`. Since
`aaaa… < bbbb…`, N-A **dials** N-B. (N-B hears N-A's beacon but `bbbb… > aaaa…`,
so N-B does **not** dial — it waits.)

---

## 2. Link handshake (hello exchange)

N-A opens TCP to `192.168.1.21:40878` and immediately sends its hello (its full
advertisement — including its advertised `pubkey` — its current overrides, and a
fresh per-connection challenge `nonce`; no secret configured here):

**N-A → N-B (TCP):**
```json
{"t":"hello",
 "node":{"id":"aaaa…","name":"softoobox","platform":"linux","tier":4,"tokens":"ok",
         "tcpPort":40878,"epoch":1000,"seq":0,"sees":[],"dutiesEnabled":{},
         "pubkey":"kA0f…A-base64-key…=","v":1},
 "overrides":{"rev":0,"updatedBy":"","duties":{}},"nonce":"9f3c…A-hex-nonce…","v":1}
```

N-B accepts, sees the first message is a `hello` (a peer link), replies with its own
hello (its own `pubkey` and its own fresh `nonce`), then processes N-A's:

**N-B → N-A (TCP):**
```json
{"t":"hello",
 "node":{"id":"bbbb…","name":"mbp","platform":"macos","tier":1,"tokens":"ok",
         "tcpPort":40878,"epoch":1000,"seq":0,"sees":[],"dutiesEnabled":{},
         "pubkey":"3Zx1…B-base64-key…=","v":1},
 "overrides":{"rev":0,"updatedBy":"","duties":{}},"nonce":"b71e…B-hex-nonce…","v":1}
```

**Proof of possession.** Each side now answers the *other's* challenge with an
`auth` — a signature over the domain-separated bytes `"szpontnet-auth-v1:" ||
nonce` for the nonce it received, proving it holds the private key for the `pubkey`
it advertised:

**N-A → N-B (TCP):** `{"t":"auth","sig":"…A signs B's nonce…","v":1}`
**N-B → N-A (TCP):** `{"t":"auth","sig":"…B signs A's nonce…","v":1}`

Each verifies the signature against the nonce **it** issued and the peer's
advertised `pubkey`, records the peer's **verified fingerprint**
(`sha256(pubkey)`), and classifies it. Neither has configured a trust allowlist
here, so both peers are **personal** (the empty-allowlist full-trust default —
[11](11-trust-and-balancing.md)); once either operator `--trust`s a first
fingerprint, an unlisted peer would become foreign.

The link is now **authenticated** on both sides. Each learns the other's NodeInfo,
binds this connection as that peer's link, and recomputes assignments. Each node's
`sees` now includes the other; each bumps `seq` and gossips the updated NodeInfo:

**N-A → N-B (TCP):**
```json
{"t":"node","node":{"id":"aaaa…",…,"seq":1,"sees":["bbbb…"],…},"v":1}
```
**N-B → N-A (TCP):** symmetric, `seq:1`, `sees:["aaaa…"]`.

---

## 3. Agreement on placement

Both nodes now hold the same live set `{A(linux,t4,ok), B(macos,t1,ok)}` and the
default policies, so both compute the **same** `assign_all`
([06](06-coordination.md#the-assignment-algorithm)):

| Duty | Policy | Eligible, ranked | Assigned |
|------|--------|------------------|----------|
| `review` | default `surplus-first` | A(t4), B(t1) → `A,B` | `[A]` |
| `conflicts` | default `surplus-first` | `A,B` | `[A]` |
| `audit` | spread 1×linux+1×macos | linux:`A`, macos:`B` | `[A, B]` |

Neither node advertises `stats`, so both rank at `NEUTRAL_SURPLUS` and the default
`surplus-first` orders **exactly as weakest-first** (higher tier first) - hence
`A,B`. No messages are needed to *agree* — agreement is a consequence of both nodes
running the same function on the same input. Each writes it to its
[`state.json`](08-state.md#the-statejson-snapshot).

---

## 4. Heartbeats

Every 2 s, each side sends a heartbeat so the link stays `up`:

```json
{"t":"heartbeat","ts":1784057241.0,"v":1}
```

If N-B stopped sending, N-A would mark the link `stale` after 5 s (N-B keeps its
duties) and `down` after 10 s (N-A recomputes without N-B; `audit`'s macOS slot then
has a shortfall, `review`/`conflicts` stay on A).

---

## 5. Retuning a resource (N-B goes out of tokens)

N-B's operator (or a control client) flips N-B to `tokens: out` — say its API budget
ran out. N-B applies the change to itself, bumps `seq`, persists it, gossips, and
recomputes.

**N-B → N-A (TCP):**
```json
{"t":"node","node":{"id":"bbbb…",…,"tokens":"out","seq":2,…},"v":1}
```

N-A adopts the fresher NodeInfo (`seq:2 > seq:1`) and recomputes. Now `audit`'s macOS
slot has no eligible macOS node (B is `out`, excluded from the token-aware duty):

| Duty | Assigned | Shortfall |
|------|----------|-----------|
| `audit` | `[A]` | `[{macos, 1}]` |

Both nodes reach this identical new view within one gossip hop. Had there been a
*second* macOS node with `tokens: ok`, the slot would have moved to it instead — no
shortfall.

---

## 6. Dispatching a job

A control client on N-A asks N-A to dispatch an `audit`. (For this trace assume N-B
is back to `tokens: ok` so both slots can fill.)

**client → N-A (control session):**
```json
{"t":"ctl","v":1}
{"t":"dispatch","duty":"audit","prompt":"run the bundle E2E","v":1}
```

N-A computes `slot_candidates("audit")` = `[("linux",[aaaa…]), ("macos",[bbbb…])]`
([07](07-dispatch.md#slots)) — ranked `surplus-first`, but neither node advertises
`stats`, so all surpluses are `NEUTRAL_SURPLUS` (`1.0`) and the ranking degrades
exactly to weakest-first. It places one job per slot:

- **linux slot → N-A itself** (local): N-A runs the job and gets `spawned`.
- **macos slot → N-B** (remote): N-A sends a dispatch on the link and waits (≤ 8 s):

  **N-A → N-B (TCP):**
  ```json
  {"t":"dispatch","job":{"id":"job1","duty":"audit","prompt":"run the bundle E2E",
                         "requestedBy":"aaaa…","requestedAt":1784057250.0},"v":1}
  ```
  N-B runs it and answers:

  **N-B → N-A (TCP):**
  ```json
  {"t":"job-status","id":"job1","status":"spawned","reason":"","node":"bbbb…","direct":true,"v":1}
  ```

  (`direct: true` because N-B classified N-A **personal** and ran the job on the
  [personal path](11-trust-and-balancing.md#the-personal-path-v1) - fire-and-forget,
  no `job-result` will follow, so an accountability-tracking dispatcher arms no
  [completion deadline](13-foreign-execution.md#the-completion-deadline). The field
  is additive; a pre-v0.4.0 node simply omits it.)

N-A assembles the per-slot results and replies to the control client:

**N-A → client (control session):**
```json
{"t":"dispatch-result","duty":"audit","results":[
  {"slot":"linux","node":"aaaa…","nodeName":"softoobox","status":"spawned","reason":""},
  {"slot":"macos","node":"bbbb…","nodeName":"mbp","status":"spawned","reason":""}
],"v":1}
```

The bundle E2E is now running on one Linux and one macOS machine — the whole point of
the `audit` spread — coordinated with no server and no configuration beyond both
machines being on the same LAN.

---

## 7. Failover (illustration)

Had N-B failed to start the job (`status:"failed"`) or not answered within 8 s, N-A
would advance the macos slot to the next macos candidate in its ranked list; with
none available, the slot ends `failed` while the linux slot's `spawned` still stands.
The same path handles a node that, under a
[future altruism limit](09-extensibility.md#the-altruism-limits-roadmap), *declines*
the job — failover is indifferent to *why* a candidate didn't take it.
