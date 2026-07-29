# 08 - State & persistence

A node keeps five machine-local files - `node.json`, `device.key`, `trusted.json`,
`banned.json`, and `stats.json` - plus the `state.json` topology snapshot it
publishes. None of the files are part of the wire protocol - two implementations
interoperate purely over [messages](04-messages.md) - but they are specified here
because the reference implementation's UIs and CLI read them, and a compatible
implementation that wants to drive those tools should match the shapes.

All five are written **atomically** (write a temp file, then rename over the
target) so a concurrent reader never sees a torn file, and all are best-effort
(an unwritable home directory is non-fatal - the node keeps running with in-memory
state).

The reference paths live under `~/.diplomat/mesh/` (overridable via
`DIPLOMAT_MESH_DIR`).

## `node.json`

The node's **persisted identity and advertised attributes** - what it restores on
restart.

```json
{
  "id": "3236817363144d8dbd842ec2973506c2",
  "name": "softoobox",
  "tier": 4,
  "strengthAuto": true,
  "tokens": "auto",
  "dutiesEnabled": {"audit": false}
}
```

| Field | Notes |
|-------|-------|
| `id` | minted once (reference: 32-hex UUID) on first run; **stable forever** after. |
| `name` | defaults to the hostname's first label. |
| `tier` | clamped to the model's `[min, max]` on load; **auto-detected from specs** when `strengthAuto` is set ([05](05-resources.md#tier)). |
| `strengthAuto` | bool; whether `tier` is still auto-detected (true) or pinned by an edit (false). Absent + an explicit `tier` is treated as a pin (back-compat). |
| `tokens` | the manual **override**: `"auto"` (default - derive the state from real usage) or a pinned `"ok"`/`"low"`/`"out"` ([05](05-resources.md#tokens)); anything else resets to `"auto"`. |
| `dutiesEnabled` | per-duty opt-out map. |

Trust is **not** persisted here: a node's identity is its Ed25519 key
([`device.key`](#devicekey)) and who it trusts is a separate local allowlist
([`trusted.json`](#trustedjson)); neither is a `node.json` field and neither is
gossiped. See [11-trust-and-balancing](11-trust-and-balancing.md).

On first run (no file, or a corrupt one), a node mints a fresh `id`, fills defaults,
and **persists immediately** so the id is stable across the very next restart.
Malformed individual fields fall back to their defaults rather than failing the
whole load.

### Cloned identity

If two machines start from a *copied* `node.json` they share an `id`. This is a
misconfiguration: each ignores the other's beacon (a beacon whose `id` equals the
local id is treated as self), so they never link, and a third node keyed by `id`
flip-flops between them. A node **SHOULD** detect a beacon carrying its own `id`
arriving from a **different machine** and warn the operator, exactly once, rather
than failing silently. Give each machine its own `node.json`.

> Detecting "a different machine" correctly requires care: a node's own
> multicast/broadcast beacon **loops back**, and off the real interface its source
> address is the machine's own **LAN IP**, not `127.0.0.1`. A node MUST therefore
> compare the beacon's source against the set of *its own* addresses (loopback
> **and** its real interface addresses) - not merely against loopback - or a lone
> node on a real LAN will falsely warn about itself.

## `device.key`

This node's **Ed25519 private key** - its cryptographic trust identity
([11](11-trust-and-balancing.md)). Stored as **raw hex** on a single line, written
`0600`, **machine-local**, and **never gossiped** - the private key never leaves the
box.

```
3f8a…c1  (32 raw key bytes, hex-encoded)
```

Like the node `id`, it is **minted once on first run and stable forever** after: on
start the node reads this file, and only if it is missing or malformed does it
generate a fresh keypair and persist it (atomically, `0600` from creation). The
derived **public** key is what the node advertises as
[`NodeInfo.pubkey`](04-messages.md#nodeinfo); its **fingerprint** = `sha256(public
key)` is what the trust allowlist matches on. Writing is best-effort: if the file is
unwritable the node still runs with the freshly-minted key **in memory** (so it can
prove its identity for the current process, but takes a new identity on restart).

A node MAY run **keyless** (the reference degrades this way when its crypto library
is unavailable): it advertises no `pubkey`, can never be verified, and is therefore
treated as `foreign` by any peer that has configured an allowlist.

## `trusted.json`

The operator's **local trusted-device allowlist** - the set of Ed25519 fingerprints
this machine considers its own. **Machine-local** and **never gossiped**; trust is
set by the operator, never derived from anything a peer advertises.

```json
{
  "defaultLevel": "foreign",
  "trusted": [
    {"fingerprint": "9c1f…a7", "label": "mbp"}
  ]
}
```

Each `trusted` entry is a `{fingerprint, label}` pair (`label` is a human note,
optional) naming a device the operator has explicitly promoted to `personal`.
`defaultLevel` (`"personal"`/`"foreign"`) is the operator's persisted **default trust
level** for any device *not* in the allowlist; it ships **`foreign`** — a new device
is zero-trust until promoted ([11](11-trust-and-balancing.md)). An **absent**
`defaultLevel` falls back to the node baseline (`DIPLOMAT_MESH_DEFAULT_TRUST` /
`core/mesh.json`'s `trust.default`, itself `foreign`). The running node keeps both the
set and the level in memory and edits them live through the
[`trust`/`untrust`/`set-default-trust`](04-messages.md#ctl) control commands, so a
change takes effect without a restart.

## `banned.json`

The operator's **local ban list** - devices this node has marked as having broken
the [foreign-accountability
contract](13-foreign-execution.md#accountability-deadline-reminder-ban) (accepted
a SzpontRequest, then failed to deliver its result and gave no - or a
non-fulfilling - answer to the readiness reminder), plus any the operator banned
manually. Like the allowlist it is **machine-local and never gossiped**: a ban is
each operator's own mark, written by the node when an automatic ban fires and
edited live through the [`ban`/`unban`](04-messages.md#ban--unban) control
commands.

```json
{
  "banned": [
    {"fingerprint": "5e2b…c9", "node": "bd4eaf…", "label": "flaky-box",
     "reason": "accepted SzpontRequest b1c2… (review) and failed to deliver: no response to readiness reminder",
     "bannedAt": 1784057240.5, "jobId": "b1c2…"}
  ]
}
```

| Field | Notes |
|-------|-------|
| `fingerprint` | the device's **verified** fingerprint - the strong identity a ban keys on. Empty for a keyless device (then `node` is the best-effort key). |
| `node` | the device's node id at ban time (display, and the match key when `fingerprint` is empty). |
| `label` | the device's last known name (human note). |
| `reason` | why - which promise it broke, or `"manual"`. |
| `bannedAt` | wall-clock time of the ban. |
| `jobId` | the SzpontRequest the automatic ban fired on (absent for a manual ban). |

An empty or absent file means nobody is banned. A device is **banned** if its
verified fingerprint matches an entry, or - only when it never proved a key - its
node id matches a fingerprint-less entry. Enforcement is specified in
[13 - the ban](13-foreign-execution.md#the-ban): every request declined, never a
dispatch target, surfaced to the operator via the
[snapshot](#the-statejson-snapshot).

## `stats.json`

A **machine-local** file: this node's load-balancing accounting
([11](11-trust-and-balancing.md)). Unlike the other two it is **never gossiped** -
only its derived `advertise()` view (`plan`, `usageAvg`, `quotaLeft`, and the
`surplus` burn-down ratio routing ranks on) rides on
[NodeInfo.stats](04-messages.md#nodeinfo). It is written atomically like the others,
best-effort, and rebuilt fresh (defaults) if missing or corrupt.

```json
{
  "plan": "max-5x",
  "acc": 12.5,
  "quotaUsed": 3.0,
  "windowStart": 1752553862.5,
  "updatedAt": 1752554100.0
}
```

| Field | Notes |
|-------|-------|
| `plan` | the account plan whose weight sets capacity. |
| `acc` | the decaying usage reservoir (units); the advertised `usageAvg` derives from it. |
| `quotaUsed` | units consumed in the current quota window. |
| `windowStart` | wall-clock start of the current window; rolls forward when the window elapses, resetting `quotaUsed`. |
| `updatedAt` | wall-clock of the last decay/record, the origin for the next decay step. |

## The `state.json` snapshot

The node's **public topology snapshot**, rewritten every `stateWriteIntervalSecs`
(default **2 s**) and on every topology change. UIs poll this file (cheap read, no
socket needed) the way they poll any status file; the same object is returned
verbatim inside a [`state`](04-messages.md#state) reply on a control session, so a
client can get it live or from disk.

```json
{
  "updatedAt": "2026-07-15T04:31:02.517Z",
  "pid": 12345,
  "tcpPort": 40878,
  "linking": 0,
  "beaconBlocked": false,
  "self": { …NodeInfo…, "fingerprint": "3d2a…f1", "uptimeSecs": 934.0 },
  "peers": [
    { …NodeInfo…, "link": "up", "addr": "192.168.1.21", "lastSeenSecsAgo": 1.2,
      "uptimeSecs": 187.0, "verified": true, "fingerprint": "9c1f…a7",
      "trust": "personal", "surplus": 1.75 }
  ],
  "trusted": [{"fingerprint": "9c1f…a7", "label": "mbp"}],
  "banned": [{"fingerprint": "5e2b…c9", "node": "bd4eaf…", "label": "flaky-box",
              "reason": "accepted SzpontRequest b1c2… (review) and failed to deliver: no response to readiness reminder",
              "bannedAt": 1784057240.5, "jobId": "b1c2…"}],
  "assignments": {
    "review":    {"duty": "review",    "assigned": ["3236…"], "shortfall": []},
    "conflicts": {"duty": "conflicts", "assigned": ["3236…"], "shortfall": []},
    "audit":     {"duty": "audit",     "assigned": ["3236…"], "shortfall": [{"platform": "macos", "missing": 1}]}
  },
  "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
  "claims": {},
  "foreign": {"pendingResults": 0, "awaiting": 0},
  "v": 1
}
```

Each NodeInfo also carries the display-hint fields from [05](05-resources.md):
`strengthAuto`, `tokensAuto`, and `tokensPct` (fraction of the token ceiling
remaining). `tokens` in the snapshot is the **effective** state (the override when
pinned, else the usage-derived state), not the raw override.

| Field | Type | Meaning |
|-------|------|---------|
| `updatedAt` | string | ISO-8601 UTC write time. Advances every write; readers detecting "meaningful change" SHOULD ignore it (and `pid`, `linking`, and the per-node ticking numbers `lastSeenSecsAgo`/`uptimeSecs`/`tokensPct`) so an idle mesh doesn't churn the UI. |
| `pid` | int | the node process id - a liveness check (is a local node actually running?). |
| `tcpPort` | int | the node's control/link port - how a local client finds the control endpoint. |
| `linking` | int | peers currently mid-handshake - lets a UI show a "linking to N…" / "scanning" affordance while the mesh forms. |
| `beaconBlocked` | bool | true while *every* beacon send fails (the node is undiscoverable - e.g. an OS privacy gate denying LAN sends; [02](02-discovery.md#redial-from-memory)) - lets a UI say so instead of showing an inexplicably empty mesh. |
| `self` | NodeInfo | this node's own advertisement, plus its own `fingerprint` (`sha256` of its advertised `pubkey`, 64 hex — *not* the pubkey itself) and `uptimeSecs` (seconds this node has been running). |
| `peers` | array | each known peer's NodeInfo plus link decoration: `link` (`up`/`stale`/`down`), `addr` (last-seen source IP), `lastSeenSecsAgo` (float), `uptimeSecs` (float, seconds the current link has been up - `null` while down), plus **this node's local view** of the peer: `verified` (bool - whether the peer *proved possession* of its key on the link), `fingerprint` (the fingerprint it proved, or merely claims if unverified), `trust` (`personal`/`foreign`/`banned`, [11](11-trust-and-balancing.md)) and `surplus` (float - its advertised burn-down ratio, the value the load balancer ranks on). |
| `trusted` | array | this node's local allowlist as `[{fingerprint, label}]` - a read-only mirror of [`trusted.json`](#trustedjson). Like the per-peer trust fields it is this node's own view; `trusted.json` and `device.key` are themselves **never gossiped**. |
| `banned` | array | this node's local ban list as `[{fingerprint, node, label, reason, bannedAt, jobId}]` - a read-only mirror of [`banned.json`](#bannedjson) (also never gossiped), so a UI can show the operator **who was marked banned and why**. `[]` when nobody is. |
| `defaultTrust` | string | this node's **default trust level** for an unknown (unlisted/unverified) device: `foreign` (zero-trust default — a new device is untrusted until promoted) or `personal` (full-trust). Mirrors [`trusted.json`](#trustedjson)'s `defaultLevel`; lets a UI render the default-trust toggle. |
| `assignments` | object | `{duty: {duty, assigned:[node_id], shortfall:[{platform, missing}]}}` - the computed placement ([06](06-coordination.md)). |
| `overrides` | object | the effective [placement overrides](06-coordination.md#placement-overrides). |
| `claims` | object | `{workKey: ownerNodeId}` for every currently-owned [work-claim](12-work-claims.md) this node observes (unowned keys omitted); lets a UI show what work is already spoken for. `{}` on a node that implements no work-claims. |
| `foreign` | object | `{pendingResults, awaiting}` — counts of in-flight [foreign-execution](13-foreign-execution.md) exchanges: `job-result`s this node computed and owes back to a foreign requester (unacked), and remote dispatches it is still willing to receive a result for. Both `0` on a node that runs no foreign work. |
| `v` | int | snapshot/protocol version. |

**Liveness of the snapshot itself.** A reader can tell a live node from a dead one
by checking that `pid` names a running process. A suspended laptop resumes with a
stale `updatedAt` but a live `pid`; freshness beyond "is the process alive" is the
reader's judgement.

## Liveness & incarnations

Two clocks matter, and they are deliberately different:

- **Link liveness** uses a **monotonic** clock: "seconds since I last heard from
  this peer" for the `up`/`stale`/`down` thresholds ([03](03-transport.md#link-state)).
  Monotonic so that a wall-clock jump (NTP correction, VM resume) can't spuriously
  age or rejuvenate a link.
- **Incarnation** uses `epoch` (a wall-clock-ish stamp taken at process start) plus
  the per-incarnation `seq` counter. `(epoch, seq)` orders advertisement versions
  ([04](04-messages.md#nodeinfo)). A restart takes a new, higher `epoch`, so its
  advertisements supersede the dead incarnation's, and peers holding a stale link
  see the higher `epoch` in the new beacon and re-dial
  ([02](02-discovery.md#the-dial-rule-smaller-id-dials)).

  > Edge case: if a node restarts *and* its wall clock has jumped backward across
  > the restart, the new `epoch` could be lower than the dead incarnation's, and
  > peers won't immediately treat the beacon as a restart - they fall back to the
  > heartbeat timeout to reap the dead link (recoverable, just slower). An
  > implementation MAY use a persisted, monotonically-increasing incarnation
  > counter instead of a wall-clock epoch to avoid this; v1 uses the wall clock for
  > simplicity.

## Down-peer retention

When a peer goes `down`, a node SHOULD keep it in the snapshot marked `"link":
"down"` for a retention window (reference: **300 s**) before dropping it entirely,
so observers see *what* went away rather than a list that silently shrinks. After
the window, the peer is removed from `peers` (and from the assignment input, which
already excluded it the moment it went `down`).

The same retention applies to a **gossip-only phantom** — a node learned purely via
multi-hop [`node` relay](03-transport.md#gossip-fan-out) that this node never linked
to directly. Such a peer never transitions through `down` (it had no link to lose),
so a node SHOULD reap it once its **last gossip** is older than the retention
window; otherwise a phantom that stops being gossiped lingers forever as a zombie.
Relatedly, the peer-table bound ([10](10-conformance.md#should--may)) MUST cover
this gossip-learned growth, not only the beacon path, so a peer relaying a flood of
spoofed ids cannot balloon the table.
