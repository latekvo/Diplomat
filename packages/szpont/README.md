# szpont

Typed Python bindings for **[SzpontNet](../szpontnet-core/README.md)**, the
leaderless LAN protocol for self-discovery, resource advertisement and work
hand-off.

SzpontNet's reference node is the [`szpontnet`](../szpontnet-core/README.md)
package, and its public surface is the **wire format**: JSON snapshots,
string-keyed dictionaries, `lastSeenSecsAgo`, and one exception class for every
way a control session can fail. That is the right shape for a protocol and an
awkward one to program against.

`szpont` is that surface bound to Python types. It adds nothing to the protocol
and reimplements none of it - every call here is the corresponding `szpontnet`
call with its answer parsed and its failure classified.

```python
import szpont

mesh = szpont.Mesh()

for peer in mesh.status().peers:
    print(peer.name, peer.link, peer.quota.surplus, peer.trust)

result = mesh.dispatch("review", prompt, work_key="review:owner/repo#41")
if result.suppressed:
    print("another machine already has this one")
```

## What it gives you over the dictionaries

**Types.** `Snapshot`, `Node`, `Peer`, `Assignment`, `Dispatch`, `Slot`, `Claim`,
`Quota`, `Device`, `Tor` - with the questions you actually ask as properties:
`peer.up`, `peer.personal`, `assignment.satisfied`, `result.ok`, `slot.suppressed`.

Parsing never raises. A snapshot can be truncated mid-write, written by a newer
protocol version, or hand-edited by someone hostile; the library's own contract is
that such a file degrades to "no node" rather than crashing whoever renders it,
and these types inherit that. Every object also keeps the dict it was read from as
`.raw`, because the protocol grows by adding fields - `onion`, `stats` and `sig`
all arrived that way - and bindings that exposed only what they knew about would
hide the next one.

**Failures you can branch on.** The library raises one `CtlError` for every
control-session failure and puts the difference in the message. This package
splits it along the line that decides what a caller should do:

| | meaning | what to do |
|---|---|---|
| `NodeUnavailable` | there was no node to talk to, so nothing was attempted | start one, or retry |
| `CommandRejected` | a node was there and the command did not take effect | retrying sends the same thing |

The split is structural, not textual - it turns on whether a live node with a
usable control port was there, and on whether the library chained a socket error -
so it does not quietly go wrong the day a message is reworded. Both remain
`szpontnet.ctl.CtlError`, so code already written against the library keeps working.

**Hosting without a subclass.** A node asks its *host* the five things it cannot
answer alone. The library's way to answer is to subclass `szpontnet.host.Host`;
most hosts answer one or two of them, and a class for that is ceremony:

```python
szpont.register_host(
    duties=["render"],
    run_job=lambda prompt, done_path: my_queue.submit(prompt, done_path),
)
```

Anything you leave out keeps the library's default, which is a real answer rather
than a placeholder: no runner means this machine declines work and the dispatcher
fails over to the next candidate.

This registers **in-process**. A node your application *spawns* is a separate
process and cannot see it - point that one at a module with `SZPONTNET_HOST`.

## Reading is two calls, on purpose

```python
mesh.snapshot()   # state.json: a file read, no socket, no node needed
mesh.status()     # the node itself: fresher, needs a live node
```

They cost different things. `snapshot()` is what a UI polls every couple of
seconds; it can lag the node slightly and it survives the node's death, so check
`mesh.running` before treating what it says as current. `status()` is what you
call before acting on what you read.

## Install

```bash
pip install szpont          # the bindings and the node
pip install 'szpont[trust]' # ... with Ed25519 device identity
```

Without the `trust` extra a node still runs, keyless: it advertises no public key,
can never be verified, and so is foreign to any peer with a trust allowlist.

## The command of the same name

The distribution also installs `szpont`, which fetches, builds and starts
**[Diplomat](../../README.md#install)** - the applet this protocol was written for.
`szpont --plan` prints what it would do without doing any of it.

```bash
pip install szpont && szpont      # the same thing `npx szpont` does
```

It is `szpont_launcher.py`, a top-level module rather than part of this package,
and it imports nothing outside the standard library: starting an applet has no use
for the protocol library, and `import szpont` has no use for a launcher. Its twin
is [`packages/szpont-npm`](../szpont-npm/README.md), which publishes the same
command to npm; the two are held to the same plan by a parity test.

## What it does not do

Wrap what already has a good shape. The node itself, its CLI (`python -m
szpontnet`), the placement function and the protocol codec are all reached
through `szpontnet` directly and are not duplicated here.

## Its own tests

```bash
pip install -e ./packages/szpontnet-core -e ./packages/szpont pytest
pytest packages/szpont/tests -q
```

The suite refuses to open a socket. That is not tidiness: a test's state directory
is isolated but the port inside a snapshot is not, and a fixture naming the
protocol's default 40878 is naming exactly the port a developer's own node is
listening on. A test that reached the transport would drive that live node - and
pass while doing it - so the transport is removed and reaching it is the failure.

## Note on the name

`szpont` names three things, and they share the namespace rather than compete for
it: this import package (`import szpont`), the launcher installed as the `szpont`
command, and the [conformance tester](../szpontnet-spec/conformance/README.md), run
as `python -m szpont` from its own directory.

Only the last is precarious. The tester is never installed, and the working
directory precedes site-packages on `sys.path`, so it wins from where it is run -
but publishing *it* under this name is what the three could not share. The console
script cannot collide with either: an entry point is a file in `bin/`, not a name
on the import path.
