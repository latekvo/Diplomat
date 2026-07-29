"""Newest-wins singleton for the NODE daemon.

A node is spawned detached (``start_new_session=True``, see
:func:`.__main__._daemonize`), so it never dies with whatever launched it — and
the application that launched it deliberately excludes the node from its *own*
singleton, since a node must not be terminated when a UI restarts. That leaves the
node owned by nobody.

Its only other guard is the launcher's :func:`.statefile.node_running` reuse
check, and that is keyed to the *per-incarnation* state directory. Renaming the
package (or an application relocating its state dir) splits that directory, the
reuse check goes blind to the old node, and the detached previous node runs
forever as a ghost — still gossiping, still taking work, long after its own source
tree is gone. That is not hypothetical: it is what an ``argent_utils`` →
``diplomat_app`` rename did here.

So on startup a fresh node terminates *every other live node of this uid* — under
any module name a node has ever launched as — found by scanning ``/proc``, never
by a state file. There is never more than one node per machine, across renames and
independent of which state dir each incarnation writes.

Linux-only: it reads ``/proc``, so on a host without it the scan finds nobody and
the node simply starts without reaping (best-effort). A node also runs on macOS,
where the equivalent would be a ``ps``-based sweep.
"""

from __future__ import annotations

from . import procscan

# Every ``python -m <module>`` a node has launched under. A rename ADDS a name and
# keeps the old ones: the whole point is that a fresh node reaps a ghost left by a
# previous incarnation, which by definition ran under the previous name. Nothing is
# ever removed from this set while a machine somewhere might still be running it.
_MESH_MODULES = frozenset({"szpontnet", "diplomat_app.mesh", "argent_utils.mesh"})


def _cmdline_is_mesh_node(tokens: list[str]) -> bool:
    """Whether an argv is a long-lived NODE: ``python -m <node-module>`` with no
    trailing option.

    A node is launched with *no* flags at all — :func:`.__main__.main` only reaches
    ``_run_node`` when every one-shot branch (the launcher's own ``--daemon``,
    ``--status``, ``--stop``, ``--dispatch``, ``--set``, …) is absent. So any option
    after the module (anything starting with ``-``) marks a short-lived CLI
    invocation, which must never be reaped as — nor reap — the node. The module
    match is exact, so neither a look-alike top-level (``szpontnetty``) nor a deeper
    submodule (``szpontnet.ctl``) can masquerade as the node.
    """
    if procscan.module_arg(tokens) not in _MESH_MODULES:
        return False
    i = tokens.index("-m")
    return not any(t.startswith("-") for t in tokens[i + 2:])


def _is_mesh_node(pid: int) -> bool:
    """Whether a live pid is a mesh node daemon (under any module name)."""
    return _cmdline_is_mesh_node(procscan.cmdline_tokens(pid))


def _other_nodes() -> set[int]:
    """PIDs of every *other* live mesh node of this uid, by any name.

    Best-effort — on a host without ``/proc`` (or a scan failure) it returns
    nothing and the node simply starts without reaping.
    """
    return procscan.scan_own_pids(_is_mesh_node)


def terminate_other_nodes() -> set[int]:
    """SIGTERM — then SIGKILL any survivor — every OTHER live mesh node of this
    uid, so a freshly starting node is the only one left. Returns the pids it
    targeted (for the caller's log line and the tests).

    The reap escalation itself is :func:`.procscan.terminate`: ~2s of grace for a
    clean asyncio ``stop()`` before a survivor is forced down, so the guarantee
    holds even against a wedged node rather than degrading to two.

    Stands down entirely in loopback-only mode (``DIPLOMAT_MESH_LOOPBACK=1``): the
    singleton's whole premise is "one physical machine = one node", but loopback is
    a *single-host multi-node simulation* (the test fleet, a dev mesh) where many
    isolated nodes — each with its own ``DIPLOMAT_MESH_DIR`` — legitimately share
    one uid, and reaping by argv would make them murder each other. A real
    deployment is never loopback-only (it would never reach another machine), and
    the ghost this guards against was a genuine LAN node, so it is still reaped.
    """
    from . import config

    if config.loopback_only():
        return set()
    victims = _other_nodes()
    procscan.terminate(victims)
    return victims
