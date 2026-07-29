# SzpontNet

A small **leaderless LAN protocol** for self-discovery, resource advertisement and
work hand-off. Machines on a local network find each other over UDP, gossip what
they can do, and agree — with no coordinator and no election — on which machine
owns each class of work. When one drops or runs dry, every survivor has already
recomputed and the work has moved.

This directory is the whole module:

| | |
|---|---|
| [`docs/`](docs/README.md) | the **normative specification** (v0.4.0, wire `v: 1`), 15 chapters ordered for bottom-up implementation |
| [`conformance/`](conformance/README.md) | a **black-box interoperability tester**: launches a candidate node as an opaque subprocess, joins the mesh around it over real multicast + TCP, and exits non-zero if any MUST fails |

The reference implementation is **Diplomat Mesh**, the stdlib-only Python node in
[`../linux/diplomat_app/mesh/`](../linux/diplomat_app/mesh); the shared constants
live in [`../core/mesh.json`](../core/mesh.json).

## Checking an implementation

```bash
cd szpontnet/conformance

# The tester's own codec + placement oracle, no node needed:
python -m szpont --selftest

# A candidate node, as an opaque subprocess:
python -m szpont --node-cmd "python szpontnet/conformance/adapters/reference.py"
```

The tester speaks only the wire protocol from `docs/`; nothing in it reads the
reference node's source. That is what makes "independently implementable"
checkable rather than aspirational.
