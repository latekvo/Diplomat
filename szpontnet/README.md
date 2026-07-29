# SzpontNet

A small **leaderless LAN protocol** for self-discovery, resource advertisement and
work hand-off. Machines on a local network find each other over UDP, gossip what
they can do, and agree — with no coordinator and no election — on which machine
owns each class of work. When one drops or runs dry, every survivor has already
recomputed and the work has moved.

This directory is the whole module:

| | |
|---|---|
| [`szpontnet/`](szpontnet/__init__.py) | the **reference implementation**: a standard-library-only Python node, `python -m szpontnet` |
| [`docs/`](docs/README.md) | the **normative specification** (v0.4.0, wire `v: 1`), 15 chapters ordered for bottom-up implementation |
| [`conformance/`](conformance/README.md) | a **black-box interoperability tester**: launches a candidate node as an opaque subprocess, joins the mesh around it over real multicast + TCP, and exits non-zero if any MUST fails |

## Standing on its own

The library depends on nothing — not on the application that happens to ship it in
this repository, and not on any package outside the standard library (Ed25519
device identity is an optional extra; without it a node runs keyless).

The canonical v1 constants and duty catalog from
[appendix B](docs/appendix-b-constants.md) ship as
[`szpontnet/netmodel.json`](szpontnet/netmodel.json), so a bare node is conformant
out of the box. The five things it *cannot* answer for itself — which duties this
deployment routes, where a node's state lives, where its events go, what "running a
job" means on this machine, and whether that work is already under way here — it
asks of a **host**
([`szpontnet/host.py`](szpontnet/host.py)). Every one has a working default, so a
node with no host runs the canonical model, keeps its state in `~/.szpontnet`,
discards its log and declines work it has no runner for.

Diplomat is one such host
([`../linux/diplomat_app/szponthost.py`](../linux/diplomat_app/szponthost.py)) and
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

## Checking an implementation

```bash
cd szpontnet/conformance

# The tester's own codec + placement oracle, no node needed:
python -m szpont --selftest

# A candidate node, as an opaque subprocess (the command runs from HERE):
python -m szpont --node-cmd "python adapters/reference.py"
```

The tester speaks only the wire protocol from `docs/`; nothing in it reads the
reference node's source. That is what makes "independently implementable"
checkable rather than aspirational.
