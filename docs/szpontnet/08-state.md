# 08 — State & persistence

A node keeps two on-disk files and one in-memory topology it publishes. None of the
files are part of the wire protocol — two implementations interoperate purely over
[messages](04-messages.md) — but they are specified here because the reference
implementation's UIs and CLI read them, and a compatible implementation that wants
to drive those tools should match the shapes.

Both files are written **atomically** (write a temp file, then rename over the
target) so a concurrent reader never sees a torn file, and both are best-effort
(an unwritable home directory is non-fatal — the node keeps running with in-memory
state).

The reference paths live under `~/.argent/mesh/` (overridable via
`ARGENT_MESH_DIR`).

## `node.json`

The node's **persisted identity and advertised attributes** — what it restores on
restart.

```json
{
  "id": "3236817363144d8dbd842ec2973506c2",
  "name": "softoobox",
  "tier": 4,
  "tokens": "ok",
  "dutiesEnabled": {"audit": false}
}
```

| Field | Notes |
|-------|-------|
| `id` | minted once (reference: 32-hex UUID) on first run; **stable forever** after. |
| `name` | defaults to the hostname's first label. |
| `tier` | clamped to the model's `[min, max]` on load. |
| `tokens` | one of `"ok"`/`"low"`/`"out"`; anything else resets to `"ok"`. |
| `dutiesEnabled` | per-duty opt-out map. |

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
> **and** its real interface addresses) — not merely against loopback — or a lone
> node on a real LAN will falsely warn about itself.

## `state.json` — the snapshot

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
  "self": { …NodeInfo… },
  "peers": [
    { …NodeInfo…, "link": "up", "addr": "192.168.1.21", "lastSeenSecsAgo": 1.2 }
  ],
  "assignments": {
    "review":    {"duty": "review",    "assigned": ["3236…"], "shortfall": []},
    "conflicts": {"duty": "conflicts", "assigned": ["3236…"], "shortfall": []},
    "audit":     {"duty": "audit",     "assigned": ["3236…"], "shortfall": [{"platform": "macos", "missing": 1}]}
  },
  "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
  "v": 1
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `updatedAt` | string | ISO-8601 UTC write time. Advances every write; readers detecting "meaningful change" SHOULD ignore it (and `pid`, and per-peer `lastSeenSecsAgo`) so an idle mesh doesn't churn the UI. |
| `pid` | int | the node process id — a liveness check (is a local node actually running?). |
| `tcpPort` | int | the node's control/link port — how a local client finds the control endpoint. |
| `self` | NodeInfo | this node's own advertisement. |
| `peers` | array | each known peer's NodeInfo plus link decoration: `link` (`up`/`stale`/`down`), `addr` (last-seen source IP), `lastSeenSecsAgo` (float). |
| `assignments` | object | `{duty: {duty, assigned:[node_id], shortfall:[{platform, missing}]}}` — the computed placement ([06](06-coordination.md)). |
| `overrides` | object | the effective [placement overrides](06-coordination.md#placement-overrides). |
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
  > peers won't immediately treat the beacon as a restart — they fall back to the
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
