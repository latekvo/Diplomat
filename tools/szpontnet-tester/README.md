# SzpontNet conformance tester

A **black-box interoperability tester** for [SzpontNet](../../docs/szpontnet/),
the LAN peer-to-peer resource-sharing protocol. It launches *your* node as an
opaque subprocess, joins the mesh around it over **real UDP multicast + TCP
sockets**, and checks — requirement by requirement — that it behaves exactly as
the specification mandates. The goal is a single, language-neutral gate: if a
second implementation (Go, Rust, Swift, JS, …) passes this, it will interoperate
byte-for-byte with any other implementation that also passes it.

It speaks only the wire protocol from `docs/szpontnet/`; nothing here depends on
the reference node's source. Every check names the MUST/SHOULD requirement and
the spec section it enforces. Coverage spans the core protocol (chapters 01–10)
**and chapter 11** — the trust / load-balancing layer and the server / API-key
role: Ed25519 proof-of-possession, `surplus-first` dispatch, per-node `stats`,
the `declined` job-status, server mode, and API-key gating.

## Requirements

- Python 3.9+. The tester itself is standard-library only; the `cryptography`
  package is an **optional** extra used by the chapter-11 trust probes to sign the
  proof-of-possession challenge. Without it those probes degrade to *keyless*
  (they can never be verified — which is itself a valid state to test), so the
  trust suite still runs but exercises only the keyless/foreign paths.
- A host where **loopback multicast** works (Linux, macOS; most CI runners).
  The tester runs a self-contained mesh on `127.0.0.1`.

## Quick start

```bash
cd tools/szpontnet-tester

# 1. Prove the tester's own codec + placement oracle are correct (no node needed):
python -m szpont --selftest

# 2. Run the full conformance suite against the reference node:
python -m szpont --node-cmd "python adapters/reference.py"

# A subset, verbosely, no color:
python -m szpont --node-cmd "python adapters/reference.py" --only A,C,E --verbose --no-color

# List every category and case:
python -m szpont --list
```

Exit code is **0** iff no MUST check failed, so you can gate CI on it. SHOULD
failures print as warnings and do not fail the run.

## How it works

For each scenario the tester:

1. allocates a fresh multicast port + TCP band + working directory (scenarios
   never bleed into each other);
2. launches your node via `--node-cmd`, configured entirely through the
   `SZPONTNET_*` environment (the **candidate contract**, below), with fast
   sub-second timings;
3. presents a **probe mesh** — one or more spec-correct fake peers it fully
   controls (beacon, hello handshake, heartbeats, gossip, dispatch executor, and
   an adversary for the fence tests);
4. observes your node over the wire and through its snapshot (a control-session
   `status` reply, or the on-disk `state.json`), and records per-requirement
   checks.

The tester also runs its **own** pure self-tests first (codec round-trips,
freshness ordering, the placement vectors) — a broken oracle would invalidate
every verdict, so it is proven correct before judging anything.

## The candidate contract

`--node-cmd` is any command that starts **one** node configured from these
environment variables. Adapt your implementation by reading them directly, or by
wrapping it in a tiny launcher like [`adapters/reference.py`](adapters/reference.py)
(which translates them for the reference node).

| Variable | Meaning |
|----------|---------|
| `SZPONTNET_LOOPBACK` | `1` → pin every socket to `127.0.0.1`, skip subnet broadcast (02/03). |
| `SZPONTNET_MCAST_GROUP` / `SZPONTNET_MCAST_PORT` | discovery multicast group + port. |
| `SZPONTNET_TCP_BASE` / `SZPONTNET_TCP_SPAN` | TCP listen port range (bind the first free one; advertise it). |
| `SZPONTNET_BEACON_SECS`, `SZPONTNET_HEARTBEAT_SECS`, `SZPONTNET_STALE_SECS`, `SZPONTNET_TIMEOUT_SECS`, `SZPONTNET_ACK_SECS`, `SZPONTNET_STATE_SECS` | protocol timings (appendix B). |
| `SZPONTNET_DIR` | working directory: put `state.json` here; persist identity here. |
| `SZPONTNET_SECRET` | join-fence secret (empty = open mesh). |
| `SZPONTNET_PLATFORM` | this node's platform (`linux` / `macos` / …). |
| `SZPONTNET_NODE_ID` | the id this node must use (so the tester controls the fleet). |
| `SZPONTNET_NODE_NAME`, `SZPONTNET_TIER`, `SZPONTNET_TOKENS`, `SZPONTNET_DUTIES` | advertised attributes (`SZPONTNET_DUTIES` is a JSON `{duty: bool}` map). |
| `SZPONTNET_SPAWN` | command template a dispatch executes, with `{prompt_file}` substituted — how the tester observes that a job actually ran. |
| `SZPONTNET_SERVER` | `1` → the accept-only [server role](../../docs/szpontnet/11-trust-and-balancing.md#the-server-role): the node runs work but never originates a dispatch to a peer (ch 11). |
| `SZPONTNET_API_KEY` | per-node [API key](../../docs/szpontnet/11-trust-and-balancing.md#the-api-key): when set, inbound `ctl` and `dispatch` MUST present a matching `apiKey` (ch 11). |
| `SZPONTNET_STATS` | JSON `{plan, quotaLeft, usageAvg}` seed for the node's advertised load-balancing stats, so `surplus-first` picks are meaningful (ch 11). |

The three chapter-11 variables are optional and default off, so a node that
implements only chapters 01–10 sees the exact same contract as before.

**Snapshot readout.** To check placement (a Participant MUST), the tester must
read your node's computed assignments. It tries, in order: a control session
(`ctl` → `status` → `state`), then `SZPONTNET_DIR/state.json`. Expose at least
one, or the placement/snapshot cases skip.

**Roles.** Cases scale to the [roles](../../docs/szpontnet/10-conformance.md#roles)
your node claims. Dispatch cases (D) that need a control session skip a pure
Participant; the executor half (D1) is tested over a peer link. So a
resource-offering-only node is judged only on what it promises.

## What is checked

| Cat | Vector | Coverage |
|-----|--------|----------|
| **A** | discovery | beacon shape/cadence/port, smaller-id-dials, larger-id-waits, exactly one link per pair, no double-dial. |
| **B** | link | hello handshake + `sees` gossip, malformed input is non-fatal, unknown fields ignored, heartbeat-timeout → `down` + reassign, down-peer retention, `(epoch,seq)` freshness. |
| **C** | V1 placement | the full spec vector table (weakest-first, strongest-first override, token exclusion/de-prioritization, spread + shortfall) checked against the candidate's snapshot and an independent oracle. |
| **D** | V4 dispatch | executor runs a job and reports `spawned`, control-session routing per slot, slot failover on a decline, unknown-duty error. |
| **E** | V5 fence | wrong-secret peer never links, wrong-secret control client refused, and the **critical outbound-dial fence**: a naked `dispatch` on a dialed link must never execute (tested with and without a secret). |
| **F** | V2 codec | every message the candidate emits is one compact UTF-8 NDJSON line with a valid schema. |
| **G** | state | the `state.json` / `state` snapshot matches the chapter-08 schema. |
| **H** | V3 LWW | placement overrides converge last-writer-wins (higher rev adopted + re-gossiped, lower rev ignored). |
| **I** | ch 11 trust + LB | empty-allowlist full trust, Ed25519 **proof of possession** over the domain-separated challenge (`"szpontnet-auth-v1:" ‖ nonce`), a keyless peer is foreign, requester classified from the **verified link** not `requestedBy`, `declined`-failover, `surplus-first` picks the most-surplus node, and `pubkey`/`stats` omit-when-empty byte-compat. |
| **J** | ch 11 server / key | server mode never dispatches to a peer (and refuses an explicit peer target), the **API key** gates inbound dispatch (declined without, spawned with) and the control session. |

## Writing an adapter for your implementation

If your node already reads the `SZPONTNET_*` variables, point `--node-cmd` at it
directly. Otherwise write a launcher that maps them onto your node's own
configuration — `adapters/reference.py` is the worked example (≈50 lines): it
writes the reference node's `node.json`, translates `SZPONTNET_*` →
`ARGENT_MESH_*`, and execs the node.

## Layout

```
szpont/
  model.py      shared constants + duty catalog (from core/mesh.json, or built-in v1 defaults)
  codec.py      clean-room NDJSON codec + strict message validators
  assign.py     independent placement oracle (06-coordination)
  net.py        multicast + TCP socket helpers
  probe.py      the probe mesh: multi-identity trust-capable fake peers + an adversary hook
  candidate.py  launch + observe the candidate (ctl session / state.json)
  harness.py    per-scenario isolation (ports, timings, work dir)
  suites.py     the conformance cases, grouped A–J (I/J = ch 11 trust + server/API-key)
  selftest.py   pure oracle/codec self-tests (V1–V3 + ch-11 codec/oracle, without a node)
  report.py     per-check reporting, MUST/SHOULD verdict, exit code
adapters/
  reference.py  candidate adapter for linux/argent_utils/mesh
```
