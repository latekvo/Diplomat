# SzpontNet — the specification

The normative definition of **SzpontNet** (v0.8.0, wire `v: 1`), the leaderless
LAN protocol for self-discovery, resource advertisement and work hand-off, plus
the tester that decides whether an implementation actually obeys it.

| | |
|---|---|
| [`docs/`](docs/README.md) | the **normative specification**: 16 chapters ordered for bottom-up implementation, from the model and discovery through dispatch, trust, work-claims and the WAN transports |
| [`conformance/`](conformance/README.md) | a **black-box interoperability tester**: it launches a candidate node as an opaque subprocess, joins the mesh around it over real multicast + TCP, and exits non-zero if any MUST fails |

Nothing here reads any implementation's source. That separation is what the
package is for: the spec is a document about a wire format, not documentation of
the Python node that happens to have been written first, and any second
implementation (Go, Rust, Swift, JS, …) that passes the tester interoperates
byte-for-byte with any other that does.

```bash
cd packages/szpontnet-spec/conformance

# The tester's own codec + placement oracle, no node needed:
python -m szpont --selftest

# A candidate node, as an opaque subprocess (the command runs from HERE):
python -m szpont --node-cmd "python adapters/reference.py"
```

The reference node lives in [`szpontnet-core`](../szpontnet-core/README.md);
[`conformance/adapters/reference.py`](conformance/adapters/reference.py) is the
worked example of the candidate contract that every other implementation copies.
