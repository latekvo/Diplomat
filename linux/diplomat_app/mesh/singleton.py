"""Newest-wins singleton for the mesh NODE daemon.

The tray has its own newest-wins singleton (:mod:`diplomat_app.singleton`), but
it DELIBERATELY excludes the mesh node: its ``_cmdline_is_applet_gui`` matches the
applet module *exactly*, so ``diplomat_app.mesh`` never counts (the comment there
spells it out — "the mesh node is a separate long-lived process that must never be
terminated here"). The node is also spawned detached (``start_new_session=True``,
see ``store.ensure_mesh_running_async`` / ``mesh.__main__._daemonize``), so it
never dies with the tray that launched it.

That left the node owned by nobody. Its only guard was the launcher's
``statefile.node_running()`` reuse check — and that is keyed to the
*per-incarnation* state dir (``~/.diplomat/mesh`` vs the pre-rename
``~/.argent/mesh``). A rename (``argent_utils`` -> ``diplomat_app``) split that
dir, the reuse check went blind to the old node, and the detached pre-rename node
ran forever as a ghost: still scanning GitHub and still spawning duplicate fix
terminals long after its own source tree was deleted.

This module gives the node the same guarantee the tray has, applied to itself: on
startup a fresh node terminates *every other live mesh node of this uid* — under
any module name it has ever launched as (``argent_utils.mesh``,
``diplomat_app.mesh``) — found by scanning ``/proc``, never by a state file. So
there is never more than one node per machine, across renames and independent of
which state dir each incarnation writes.

Linux-only: it reads ``/proc``, so on a host without it the scan finds nobody and
the node simply starts without reaping (best-effort, exactly as before). The node
also runs on macOS (spawned by the Swift ``MeshBridge``), where the equivalent
would be a ``ps``-based sweep — a follow-up, not this change.
"""

from __future__ import annotations

from .. import procscan

# Every ``python -m <module>`` a mesh node has launched under. Mirrors
# ``diplomat_app.singleton._APPLET_MODULES`` (the tray's list) with the ``.mesh``
# submodule appended — a rename adds a name and keeps the old ones, so a fresh
# node still reaps a pre-rename ghost across the boundary.
_MESH_MODULES = frozenset({"diplomat_app.mesh", "argent_utils.mesh"})


def _cmdline_is_mesh_node(tokens: list[str]) -> bool:
    """Whether an argv is a long-lived mesh NODE: ``python -m <mesh-module>`` with
    no trailing option.

    A node is launched with *no* flags at all — ``mesh.__main__.main`` only reaches
    ``_run_node`` when every one-shot branch (the launcher's own ``--daemon``,
    ``--status``, ``--stop``, ``--dispatch``, ``--set``, …) is absent. So any option
    after the module (anything starting with ``-``) marks a short-lived CLI
    invocation, which must never be reaped as — nor reap — the node. The module
    match is exact, so neither a look-alike top-level (``diplomat_app.meshery``) nor
    a deeper submodule (``diplomat_app.mesh.foo``) can masquerade as the node.
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

    The reap escalation itself is :func:`diplomat_app.procscan.terminate`, shared
    with the tray's singleton: ~2s of grace for a clean asyncio ``stop()`` before
    a survivor is forced down, so the guarantee holds even against a wedged node
    rather than degrading to two.

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
