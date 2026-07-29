# 01 — Model & terminology

This chapter defines the nouns the rest of the spec uses. Everything else is
mechanism; these are the concepts.

## Node

A **node** is one running participant in the mesh — normally one physical or
virtual machine, though nothing forbids several nodes on one host (the reference
implementation runs whole test meshes on a single machine; see
[02-discovery](02-discovery.md#several-nodes-on-one-host)).

A node has a stable **node id** (`id`): an opaque string, unique within the mesh,
that persists across restarts. The reference implementation uses a 32-character
lowercase hex UUID minted on first run and stored in `node.json`
([08-state](08-state.md)). An implementation MAY use any scheme as long as the id
is (a) stable across restarts of the same logical node and (b) unique across the
mesh. Two nodes sharing an id is a misconfiguration and degrades the mesh (see
[08-state](08-state.md#cloned-identity)).

A node also has an **incarnation**, identified by an `epoch` (a number that
increases every time the node process (re)starts) and a per-incarnation update
counter `seq`. Together `(epoch, seq)` totally order the versions of a node's
advertisement so peers can tell newer from older — see
[08-state](08-state.md#liveness--incarnations).

## Resource advertisement

The **resource advertisement** is what a node publishes about itself: the payload
peers use to decide what work it should do. In v1 it is exactly the **NodeInfo**
object ([04-messages](04-messages.md#nodeinfo)), whose resource-bearing fields are:

- **`platform`** — the kind of machine (`"linux"`, `"macos"`, …). Some work must
  run on a specific platform.
- **`tier`** — a small integer *strength* rank, `1` = strongest. Lets placement
  prefer weak or strong machines on purpose.
- **`tokens`** — a coarse *budget availability* signal: `"ok"`, `"low"`, or
  `"out"`. A machine that is `"out"` is skipped for budget-aware work.
- **`dutiesEnabled`** — which duties this node is willing to run (a per-duty
  on/off switch).

These four are the v1 **resource vocabulary**. [05-resources](05-resources.md)
describes each in depth and specifies how the vocabulary is extended (richer
resource descriptors, and later the altruism-limit fields) without breaking
compatibility.

> **Why "advertisement" and not "capabilities"?** A node does not merely *have*
> resources, it *offers* them. Under the v1 [full-altruism model](README.md#the-trust-model-personal-vs-foreign)
> advertising a resource is a standing offer to use it for the mesh. When altruism
> limits arrive ([09](09-extensibility.md)), an advertisement gains the ability to
> also say *under what terms* — but it stays an offer, not a passive inventory.

## Duty

A **duty** is a named class of work the mesh routes — the unit that placement
assigns and dispatch delivers. A duty has an `id` and a **placement policy**
(below). The reference vocabulary defines three duties
([appendix B](appendix-b-constants.md)): `review`, `conflicts`, `audit`. The duty
set is data, not protocol: implementations load it from the shared model and MUST
tolerate duties they don't recognize (treat an unknown duty as opaque — gossip
it, place it, dispatch it — never crash on it). See
[05-resources](05-resources.md#duties) and
[09-extensibility](09-extensibility.md#adding-a-duty).

## Placement policy

Each duty carries a **placement policy** describing *how* to choose which node(s)
run it:

- a **strategy** — how to rank eligible nodes (`surplus-first` (the default),
  `weakest-first`, `strongest-first`, `local-first`);
- **token-awareness** — whether a node that is `tokens: "out"` is excluded;
- a **spread** — an optional list of `{platform, count}` requirements, so a duty
  can demand coverage across platforms (e.g. *one Linux and one macOS node*)
  rather than landing on a single machine.

Placement policies have a default (from the shared model) and can be overridden
mesh-wide at runtime; the override is gossiped last-writer-wins. See
[06-coordination](06-coordination.md).

## Assignment

An **assignment** is the *output* of running a duty's placement policy over the
current live nodes: the ordered set of node ids that should run that duty right
now, plus any **shortfall** (spread requirements that could not be met). Every
node computes assignments independently and — given the same inputs — identically.
Assignments are advisory: they are what the mesh *believes* should happen and what
dispatch *will* do, but nothing enforces exclusivity in v1.

## Dispatch and job

A **dispatch** is a request to actually run a duty now, carrying a **job** — a
unit of work with an id, the duty, and an opaque **prompt** (the work payload;
Diplomat Mesh uses it as the text handed to an agent, but SzpontNet treats it as an
uninterpreted string). Dispatch routes the job to the node(s) the placement picks,
one **slot** at a time, failing over within a slot if a candidate can't take it.
See [07-dispatch](07-dispatch.md).

## Work key and work claim

A **work key** is a deterministic, client-derived string naming a unit of
*external* work (e.g. a specific PR at a specific commit). A **work claim** is a
gossiped, self-signed lease on a work key: nodes that independently notice the same
work each claim it, a leaderless rule elects one owner, and the rest stand down — so
the work is originated once, not once per observer. A claim lasts only while its
claimant is alive, so a dead owner's work is freed for a survivor. This is an
optional layer on top of dispatch; see [12-work-claims](12-work-claims.md).

## Foreign execution and job result

A **foreign** dispatch (from a device you have not trusted) is either declined or —
if the receiver has a confinement runner — run **confined**: its compute happens in a
sandbox, and the receiver returns a **job result** rather than acting on it. The
**originator** then performs any social action (opening a PR, commenting) itself,
under its own identity. The result is delivered reliably (acknowledged, retried until
acked). This is an optional zero-trust layer; see
[13-foreign-execution](13-foreign-execution.md).

## Link and peer

A **peer** is another node this node knows about. A **link** is the single TCP
connection held between a pair of peers, over which they exchange hellos,
heartbeats, gossip, and dispatches. A link has a **state** — `up`, `stale`, or
`down` — derived from how recently traffic was seen
([03-transport](03-transport.md#link-state)).

## Control session

A **control session** is a TCP connection to a node's port opened by a *client*
(a UI, a CLI) rather than a peer, used to read the node's state and to drive it
(edit attributes, edit placement overrides, dispatch a job). It is distinguished
from a peer link by its opening message. See
[04-messages](04-messages.md#control-messages) and
[07-dispatch](07-dispatch.md#dispatching-via-a-control-session).

## The shared model

Constants that every node must agree on to interoperate — the discovery
group/ports, the timing values, the resource vocabulary (platforms, tiers,
tokens), the duty catalog and the strategies — form the **shared model**. In this
repository it is [`core/mesh.json`](../../core/mesh.json), and the canonical
values are tabulated in [appendix B](appendix-b-constants.md). An implementation
MAY read these from its own copy, but two nodes with different discovery
groups/ports or timing far enough apart will not form a healthy mesh, and two
nodes with different *vocabularies* will still interoperate at the wire level but
may place work differently — see
[09-extensibility](09-extensibility.md#vocabulary-skew).
