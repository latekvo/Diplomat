# SzpontNet

A small **leaderless LAN protocol** for self-discovery, resource advertisement and
work hand-off. Machines on a local network find each other over UDP, gossip what
they can do, and agree — with no coordinator and no election — on which machine
owns each class of work. When one drops or runs dry, every survivor has already
recomputed and the work has moved.

This package is the **reference implementation**:
[`szpontnet/`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/szpontnet/__init__.py), a standard-library-only Python node run as
`python -m szpontnet`. The protocol it implements is specified next door, in
[`szpontnet-spec`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-spec/README.md) — the normative chapters and the
black-box conformance tester that can judge any implementation, this one included.
The split is the point: the spec is a document about a wire format, not
documentation *of this code*, and it is versioned and read as such.

## Standing on its own

The library depends on nothing — not on the application that happens to ship it in
this repository, and not on any package outside the standard library (Ed25519
device identity is an optional extra; without it a node runs keyless).

Every knob it reads from the environment is `SZPONTNET_<NAME>`, through a single
accessor ([`szpontnet/env.py`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/szpontnet/env.py)) so that is a property of the code
rather than a convention. These names are part of the spec: the conformance tester
configures a candidate through them and has no other channel, so a node that reads
its settings under some other spelling is one the tester cannot drive. The
pre-rename `DIPLOMAT_MESH_<NAME>` spellings are still honoured when the new one is
unset — see the module for when that can be dropped.

The canonical v1 constants and duty catalog from
[appendix B](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-spec/docs/appendix-b-constants.md) ship as
[`szpontnet/netmodel.json`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/szpontnet/netmodel.json), so a bare node is conformant
out of the box. The five things it *cannot* answer for itself — which duties this
deployment routes, where a node's state lives, where its events go, what "running a
job" means on this machine, and whether that work is already under way here — it
asks of a **host**
([`szpontnet/host.py`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/szpontnet/host.py)). Every one has a working default, so a
node with no host runs the canonical model, keeps its state in `~/.szpontnet`,
discards its log and declines work it has no runner for.

Diplomat is one such host
([`diplomat-platform/linux/diplomat_app/szponthost.py`](https://github.com/latekvo/Diplomat/blob/main/packages/diplomat-platform/linux/diplomat_app/szponthost.py)) and
registers itself two ways: in-process, and by putting
`SZPONTNET_HOST=diplomat_app.szponthost` in the environment of the node it spawns.

```python
import szpontnet.host

class MyHost(szpontnet.host.Host):
    def model(self):
        return {"duties": [{"id": "render", "placement": {"tokenAware": False, "spread": []}}]}

    def run_job(self, prompt, done_path):
        ...  # your machine, your rules

szpontnet.host.set_host(MyHost())
```

## Its own tests

```bash
pip install -e './packages/szpontnet-core[trust]' pytest
pytest packages/szpontnet-core/tests -q
```

Nothing here imports the application that ships the library, and CI runs this job
with no Qt, no `diplomat-core` and no Diplomat on the import path — so a dependency
creeping back in fails a build rather than going unnoticed. One of the tests
enforces that directly: it scans every module for a mention of a host application,
and another walks the package AST to catch an environment read that skips
[`env.py`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/szpontnet/env.py).

Beyond the unit and host-seam tests, the integration ones live here too: real nodes
over loopback for the control-edit state flush, the one-node-per-state-dir startup
lock, and the Tor transport at two altitudes — the node's Tor *decisions* against an
injected dialer (deterministic, no daemon in the way), and the whole onion path
against a real tor daemon.

The second is `test_tor_e2e.py`, and it runs against either of two backends. By
default a **simulated onion network**: `simtor.py`, a stand-in daemon speaking the
exact contract `tor.py` depends on — it parses the torrc it is handed, logs a
bootstrap to stdout, writes a hostname derived from a persisted key, and answers real
SOCKS5 over a real socket, resolving onions through a descriptor directory on disk
instead of the Tor network. Every line of the transport runs; only the network is
simulated. With `SZPONTNET_TEST_TOR=real` the same tests run against the actual `tor`
binary and the live Tor network (slower, and skipped when no `tor` is installed).

Nodes there are whole processes on distinct multicast ports, so they cannot discover
each other on the LAN at all — a link between them came over an onion, which is the
claim the transport exists to make.

### Whole meshes, on a network the test controls

Most of what a mesh has to get right only happens when the network misbehaves, and
loopback sockets have no way to drop a beacon or cut a link. So
[`tests/simnet.py`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-core/tests/simnet.py)
virtualizes the two transports — an in-memory switch behind `asyncio.open_connection`
and a multicast bus behind the beacon sockets — and leaves everything above them the
real node. A test then runs several nodes in one process and steers what reaches
them:

```python
def test_a_partition_heals(simnet):
    async def scenario():
        a, b = await simnet.node("a"), await simnet.node("b")
        await simnet.linked(a, b)
        simnet.cut(a, b)                      # nothing is closed; delivery stops
        await simnet.until(lambda: a.link_state(b) == "down", 4.0, "still up")
        simnet.heal_all()
        await simnet.linked(a, b)
    simnet.run(scenario())
```

`cut` / `isolate` / `partition` for split brains, `drop_kind` for losing one message
type on one path, `freeze` for a peer that dies without closing its socket,
`stall_writes_from` for one that stops reading, plus per-node quotas, trust levels
and protocol constants. On top of it: discovery and dial races, gossip convergence
and the forgeries it has to refuse, dispatch and failover, simultaneous work-claims,
foreign zero-trust execution with its accountability clock, and recovery.

The suite is checked by mutation rather than by coverage — break a rule in the node,
and it has to be a test that says so.

## Checking this node against the spec

```bash
cd packages/szpontnet-spec/conformance
python -m szpont --node-cmd "python adapters/reference.py"
```

The tester speaks only the wire protocol from the spec; nothing in it reads this
node's source. That is what makes "independently implementable" checkable rather
than aspirational — see [`szpontnet-spec`](https://github.com/latekvo/Diplomat/blob/main/packages/szpontnet-spec/README.md).
