# Diplomat Mesh (experimental)

LAN P2P duty coordination between several machines running Diplomat.

> Diplomat Mesh is the reference implementation of **SzpontNet**, a small leaderless
> LAN protocol for self-discovery, resource advertisement, and work hand-off. The
> full, independently-implementable specification (currently **v0.9.0**, wire `v: 1`)
> is in [`szpontnet-spec/docs/`](../packages/szpontnet-spec/docs/README.md), and
> [`szpontnet-spec/conformance/`](../packages/szpontnet-spec/conformance/README.md) is the black-box
> conformance suite that makes "independently implementable" checkable: it launches
> a candidate node as an opaque subprocess, joins it over real multicast + TCP, and
> exits non-zero if any MUST fails.

With several machines on one desk (say a Linux box and two MacBooks), the
wrench's grunt work shouldn't all land on the laptop you're typing on. **Diplomat
Mesh** makes the machines coordinate: every node self-discovers its peers over
UDP (multicast + subnet broadcast), holds heartbeat TCP links, and gossips its
status - platform, a machine *tier* (1 = strongest, auto-detected from the
hardware CPU-first; editing it pins the value), and token availability
(ok / low / out, tracked from real usage unless you pin that too).

On top of that shared view, every node runs the same **deterministic duty
assignment** - no leader, no election, no split-brain: identical inputs give
identical answers everywhere, so the moment a machine dies (heartbeat timeout)
or runs out of tokens, every survivor has *already* agreed where each duty
moved. Duties are the four spawn actions, each with a configurable placement:

- **Review PRs / Fix issues / Resolve conflicts** - default *surplus-first*: route to the node
  with the most spare quota relative to its reset (with no quota signal this falls
  back to *weakest-first*, keeping the strong machines free for interactive work).
- **Full E2E test** - same surplus-first default, plus a platform **spread**:
  one Linux node **and** one macOS node run the bundle E2E, each slot failing
  over within its platform.

(Strategy and spread are separate placement fields. The strategies are
`weakest-first`, `strongest-first`, `local-first` and `surplus-first` (the
default).)

Dispatching routes a staged prompt to the chosen node over the mesh; the
receiving machine opens its own terminal running its own agent runner exactly like a local
SPAWN AGENT (dispatches are the `📤/📥 mesh` rows in the activity feed). If the
first target declines - gone, or out of tokens - the dispatch fails over to the
next candidate by rank. While the mesh is live, the four wizards grow a
**⬡ Run on mesh** row (checked by default, with a preview of where the duty
currently routes): SPAWN AGENT then hands the job to the node instead of always
opening a local terminal - on both front-ends. The two wizards with a scope offer the
row for one named item only: a sweep opens no session to place, it queues one review
per PR / one fix per issue for this machine's own cap to start.

Both front-ends grow a **Mesh screen** (the ⬡ button in the panel header, beside
[Telemetry](telemetry.md) and Settings): the live node graph (link states), per-node tier/token editors (editing
a *remote* node forwards over the mesh, so one panel configures the whole fleet),
per-duty strategy + token-awareness controls (gossiped last-writer-wins), the whole
**trust surface** - the `New devices: Personal / Foreign` default, a one-time
callout when an unknown device shows up, a per-peer trust toggle, and the banned
chip with its un-ban - and **reaching machines off this network**: the mesh's
preferred WAN transport, this machine's id on each transport it runs (copyable), a
paste box that links to another machine's id, and per peer the one transport that
edge agreed on. It shouts `DEVICE IS NOT DISCOVERABLE` if every beacon send fails.

The mesh node itself is stdlib-only Python that runs on any OS - both the macOS app
and the Linux applet drive that same node (a Swift node is future work), so enabling
the mesh on macOS needs the source checkout on disk (the app finds the checkout its
bundle was built in, `szpont`'s `~/.diplomat/checkout` included; `DIPLOMAT_SELF_REPO`
names any other, else `~/dev/diplomat`):

```bash
cd packages/diplomat-runtime
# Put Diplomat behind the node: without this it runs SzpontNet's own defaults -
# the canonical v1 duties, state in ~/.szpontnet, no activity feed - and so joins
# a different mesh than the one your app is on.
# Absolute, because `--daemon` re-execs the node from the library's own directory:
# a relative PYTHONPATH entry would resolve against THAT cwd, the host module would
# not import, and the node would come up on the null host without saying so.
export SZPONTNET_HOST=diplomat_runtime.szponthost
export PYTHONPATH="$PWD:$PWD/../szpontnet-core"

python3 -m szpontnet --daemon      # join the mesh (any OS, no Qt needed)
python3 -m szpontnet --status      # live topology + duty assignments
python3 -m szpontnet --stop        # stop the running node
python3 -m szpontnet --set tokens=out          # also: tier=N name=X duty.<id>=on|off
python3 -m szpontnet --set tier=1 --node <ID>  # edit a REMOTE node over the mesh
python3 -m szpontnet --dispatch review --prompt "…"   # route a job (--prompt-file, --target)
python3 -m szpontnet --claim <KEY>             # origination-dedup lease (spec ch 12)
```

Bare `python3 -m szpontnet` runs a node in the foreground. `--help` lists
the rest (trust/ban management, `--api-key`, `--work-key`).

The canonical model lives in [`szpontnet-core/szpontnet/netmodel.json`](../packages/szpontnet-core/szpontnet/netmodel.json)
and Diplomat's overlay - the duty catalog both panels render - in
[`assets/mesh.json`](../packages/diplomat-core/assets/mesh.json); node state in
`~/.diplomat/mesh/` (`node.json` identity, `state.json` topology snapshot,
`device.key` + `trusted.json` + `banned.json` for trust, `peers.json` to redial
known peers - the device-allocator pattern; `SZPONTNET_DIR` relocates it).

Every knob the node itself reads is `SZPONTNET_*` - the library owns that
namespace, and the conformance tester configures a candidate through no other
channel. They were `DIPLOMAT_MESH_*` while the node lived inside this app, and the
old spelling is still honoured when the new one is unset, so a shell profile that
sets `DIPLOMAT_MESH_SECRET` keeps its mesh fenced rather than silently opening it.
Diplomat's *own* mesh knobs keep their names (`DIPLOMAT_MESH_POLL_SECS`,
`DIPLOMAT_MESH_CMD_TEST`, `DIPLOMAT_MESH_E2E`): they configure the applet, not the
node.

**Beyond one LAN.** Discovery is link-local, so on its own the mesh stops at the
subnet. A **WAN transport** is what joins several of them: once two nodes have met,
each redials known-but-unseen *personal* peers **by identity, not by address** - so a
desk at home and a desk at the office are one mesh, with no public IP, no domain and
no port forwarding. Either transport is complementary to the LAN rather than a
replacement for it: peers that share a network still find and reach each other by
multicast and direct TCP.

There are two, and a node may run either, both, or neither:

- **Tor** ([spec ch 14](../packages/szpontnet-spec/docs/14-tor-transport.md)), **on by
  default**: a permanent v3 onion service, advertised inside the signed advert. A
  node with no `tor` binary installed simply does not run it, and `SZPONTNET_TOR=0`
  turns it off. Paste an address with
  `python3 -m szpontnet --tor-connect <hash>.onion`.
- **iroh** ([spec ch 15](../packages/szpontnet-spec/docs/15-iroh-transport.md)), **on by
  default** given the optional package (`pip install 'szpontnet[wan]'`): a permanent
  QUIC endpoint whose id is an Ed25519 public key. A node without that package simply
  does not run it, and `SZPONTNET_IROH=0` turns it off. It reaches the same peers with
  no daemon, no multi-minute bootstrap and no rendezvous circuit per dial, connecting
  in well under a second. Paste an address with
  `python3 -m szpontnet --iroh-connect <64-hex>`.

Which of the two an edge settles on is a **mesh-wide pick** ([spec ch
6](../packages/szpontnet-spec/docs/06-coordination.md#the-preferred-wan-transport)),
gossiped last-writer-wins beside the duty placements and set from the Mesh screen or
with `python3 -m szpontnet --wan iroh`. It **orders, it never excludes**: every node
keeps running every transport it can, so the pick decides an edge only where both
ends speak both, and a peer that advertises only an onion is still reached over Tor.
An edge that shares neither has no way off the LAN at all, and the Mesh screen flags
it. `--connect <id>` takes either address and reads the transport off its shape.

**Trust model.** The mesh is designed around a LAN you control (IPv4; discovery is
multicast + subnet broadcast), and - since both WAN transports are on by default - a
node also publishes an onion, and a QUIC endpoint wherever `szpontnet[wan]` is
installed, that peers can reach it on from anywhere. A WAN address
carries peer links only; the operator's control channel (`stop`, `trust`,
`dispatch`, `set-attr`) is refused on any connection that did not arrive on the LAN.
Note what that leaves: on an **open** mesh (no join secret) a peer holding your onion
or endpoint can link and, subject to trust, exchange gossip and dispatch from the WAN,
where before it would have had to be on your LAN. The two fences below apply
identically over every transport, and turning both transports off (`SZPONTNET_TOR=0`,
`SZPONTNET_IROH=0`) removes the WAN surface entirely.

Two independent fences:

- **Join fence** - set the same `SZPONTNET_SECRET=<token>` on every machine
  (and in the applet's environment): a node with a secret refuses peers, control
  sessions, and dispatches that don't present the matching token. The token rides
  plaintext on the LAN, so it keeps a stray machine or a colleague's mesh from
  joining yours; it does not defend against a hostile network.
- **Authenticated device keys** - every node mints an Ed25519 keypair on first
  run (`~/.diplomat/mesh/device.key`, requires the `cryptography` package; without
  it the node runs *keyless* and can never be verified). A peer must prove
  possession of its key on each link (fresh-nonce signature) before its identity
  counts; advertised names/ids grant nothing. Trust is then a **local allowlist**
  of proven key fingerprints (`~/.diplomat/mesh/trusted.json`, never gossiped),
  and it is **zero-trust by default**: a device you have not explicitly promoted
  is `foreign` no matter how empty the allowlist is. Promote from the Mesh screen
  or the CLI:
  `python3 -m szpontnet --fingerprint` (print this machine's),
  `--trust <FP> [--label <name>]`, `--untrust <FP>`. The baseline itself is a
  per-node knob (`--default-trust personal|foreign`, `SZPONTNET_DEFAULT_TRUST`,
  or `trust.default` in `assets/mesh.json`) - set it to `personal` for the old
  full-altruism behaviour where every unlisted peer is trusted.

There are three trust levels, not two:

| Level | What a request from it does |
|-------|------------------------------|
| `personal` | runs directly, exactly as if you'd triggered it locally |
| `foreign` | **declined** - unless a confinement runner is configured (`SZPONTNET_FOREIGN_SPAWN`), in which case it runs sandboxed and *response-only*: the compute happens here, the result is routed back, and this node never takes a social action on it ([spec ch 13](../packages/szpontnet-spec/docs/13-foreign-execution.md)) |
| `banned` | declined outright, even with a confinement runner, and never picked as a dispatch target |

**Foreign accountability.** A foreign device that *accepts* a job takes on a
contract: deliver a result before the completion deadline (6 h by default). Miss it
and the node sends a readiness reminder; an unhelpful or absent answer - judged by
an agent you can point at with `SZPONTNET_EXTEND_DECIDER`, which may grant an
extension instead - earns a **ban**, recorded machine-local in
`~/.diplomat/mesh/banned.json` and never gossiped. Manage bans with
`--ban <FP|ID> [--ban-reason …]` / `--unban`; the macOS Mesh screen surfaces them
as a banned chip with the reason and an inline un-ban.

Nodes also gossip **per-node quota accounting** (plan, a surplus **burn-down ratio**
- budget left ÷ clock left until the quota resets - plus display-only usage-average
and quota-left figures; see `accounts` in `packages/szpontnet-core/szpontnet/netmodel.json`): the default
`surplus-first` ranking sends work to the machine that is most flush *relative to its
reset*, and each executed job books usage on the executor.
