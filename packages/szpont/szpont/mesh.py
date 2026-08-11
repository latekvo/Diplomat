"""The local node, as one object.

Everything a client can ask of a node goes through a control session: a TCP
connection to the port its snapshot advertises, one command, one reply
(:mod:`szpontnet.ctl`). :class:`Mesh` is that surface gathered onto a single
object, returning the types in :mod:`szpont.models` and raising the two
exceptions in :mod:`szpont.errors`.

There is no connection to hold: each call opens its own session, exactly as the
library does, so a :class:`Mesh` is cheap to make, safe to keep on an object for
the life of a program, and safe to share between threads.

Reading is deliberately two calls, because they cost different things:

* :meth:`Mesh.snapshot` reads ``state.json`` - a file read, no socket, no node
  needed. What a UI polls.
* :meth:`Mesh.status` opens a control session and asks the node itself - fresher,
  and it requires a live node. What you call before acting on what you read.
"""

from __future__ import annotations

from szpontnet import ctl, statefile

from .errors import CommandRejected, NodeUnavailable
from .models import Claim, Dispatch, Peer, Snapshot

# The library's own default for a plain command. Dispatch keeps its own, much
# longer one: routing may walk several failover candidates before it answers.
DEFAULT_TIMEOUT = 5.0
DEFAULT_DISPATCH_TIMEOUT = 60.0


class Mesh:
    """A client for the node running against this machine's state directory.

    Which directory that is comes from the library (``SZPONTNET_DIR``, or a
    registered host's answer), so a process that has pointed itself at a state
    directory gets a :class:`Mesh` for the node there without repeating it.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    # ---- reading -------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a node appears to be alive here - the snapshot names a live
        pid. False for a machine where no node has ever run.

        Freshness beyond a live pid is the caller's call: a laptop resuming from
        sleep has a stale ``updated_at`` and a perfectly good node.
        """
        state = statefile.read_state()
        return state is not None and statefile.node_running(state)

    def snapshot(self) -> Snapshot | None:
        """The published snapshot, read from disk. ``None`` when no node has ever
        run here, or the file is unreadable, corrupt, or not a JSON object.

        Cheap and node-free, so this is what a UI polls. It can lag the node by a
        couple of seconds, and it survives the node's death - check
        :attr:`running` before treating what it says as current.
        """
        state = statefile.read_state()
        return None if state is None else Snapshot.from_dict(state)

    def status(self) -> Snapshot:
        """The node's own live view, over a control session.

        Raises :class:`~szpont.errors.NodeUnavailable` when no node is there.
        """
        self._require_node()
        with _translated():
            return Snapshot.from_dict(ctl.status(timeout=self.timeout))

    def peers(self) -> tuple[Peer, ...]:
        """This node's peers, live."""
        return self.status().peers

    # ---- work ----------------------------------------------------------

    def dispatch(self, duty: str, prompt: str, *, target: str | None = None,
                 api_key: str = "", work_key: str = "",
                 timeout: float = DEFAULT_DISPATCH_TIMEOUT) -> Dispatch:
        """Route a request through the mesh and return every slot's outcome.

        The mesh picks the machines: one per slot the duty's placement asks for,
        failing over within a slot when a candidate declines. ``target`` names one
        node instead, which is the caller's unilateral pick and gets no failover.

        ``work_key`` opts into origination dedup (12-work-claims): the node checks
        the key first and, if a live personal peer already owns that work, routes
        nothing and answers with a single suppressed slot. Pass it whenever two
        machines could see the same external event.

        ``api_key`` is the credential forwarded to an API-key-gated target.

        A slot that declines or fails is a normal outcome, not an exception -
        check :attr:`Dispatch.ok`. The exceptions are about reaching the local
        node, not about what the mesh decided.
        """
        self._require_node()
        with _translated():
            return Dispatch.from_results(ctl.dispatch(
                duty, prompt, target, api_key, work_key, timeout=timeout))

    def claim(self, work_key: str) -> Claim:
        """Run the origination claim gate for one unit of work without
        dispatching anything (12-work-claims).

        For a caller that will run the work *itself* - a monitor about to spawn a
        local process - and only wants the mesh's answer to "am I the one who
        should". A false :attr:`Claim.owned` means a better live personal peer
        holds the lease and this machine must not originate.
        """
        self._require_node()
        with _translated():
            return Claim.from_dict(ctl.claim_work(work_key, timeout=self.timeout))

    # ---- editing -------------------------------------------------------

    def set_attrs(self, attrs: dict, *, target: str = "self") -> None:
        """Edit a node's local attributes - ``tier``, ``tokens``, ``name``, the
        accounting figures, per-duty toggles.

        ``target`` is a node id to edit a *remote* node, which the local node
        forwards over that peer's link.
        """
        self._require_node()
        with _translated():
            ctl.set_attr(target, attrs, timeout=self.timeout)

    def set_placement(self, duty: str, placement: dict) -> None:
        """Edit one duty's mesh-wide placement. Gossiped last-writer-wins, so
        every node converges on it."""
        self._require_node()
        with _translated():
            ctl.set_overrides(duty, placement, timeout=self.timeout)

    # ---- trust ---------------------------------------------------------

    def trust(self, fingerprint: str, label: str = "") -> None:
        """Promote a device to personal on this machine's allowlist.

        Local and never gossiped: trusting a device here says nothing about it
        anywhere else in the mesh.
        """
        self._require_node()
        with _translated():
            ctl.trust_device(fingerprint, label, timeout=self.timeout)

    def untrust(self, fingerprint: str) -> None:
        """Take a device off the allowlist, back to the default trust level."""
        self._require_node()
        with _translated():
            ctl.untrust_device(fingerprint, timeout=self.timeout)

    def ban(self, *, fingerprint: str = "", node: str = "", label: str = "",
            reason: str = "") -> None:
        """Refuse a device outright - decline everything it asks and never
        dispatch to it.

        The manual counterpart of the automatic ban a foreign executor earns by
        accepting work and going silent. ``fingerprint`` for a keyed device,
        ``node`` (an id) for a keyless one that has no fingerprint to name.
        """
        self._require_node()
        with _translated():
            ctl.ban_device(fingerprint, node, label=label, reason=reason,
                           timeout=self.timeout)

    def unban(self, *, fingerprint: str = "", node: str = "") -> None:
        """Lift a ban - the operator's recovery path."""
        self._require_node()
        with _translated():
            ctl.unban_device(fingerprint, node, timeout=self.timeout)

    def set_default_trust(self, level: str) -> None:
        """Set what an *unknown* device is worth here: ``"foreign"`` (the
        shipped default - a new device is untrusted until promoted) or
        ``"personal"`` for a mesh where every machine is yours."""
        self._require_node()
        with _translated():
            ctl.set_default_trust(level, timeout=self.timeout)

    # ---- transport and lifecycle ---------------------------------------

    def iroh_connect(self, endpoint: str, timeout: float = 10.0) -> str:
        """Reach a peer at its iroh endpoint id, whether or not you ever met it on
        the LAN. Returns the normalized address the node is dialing. Needs a node
        started with ``SZPONTNET_IROH=1``.

        The dial happens in the background: the peer shows up in a later
        :meth:`status`, not in this return value.
        """
        self._require_node()
        with _translated():
            return ctl.iroh_connect(endpoint, timeout=timeout)

    def tor_connect(self, onion: str, timeout: float = 10.0) -> str:
        """The :meth:`iroh_connect` twin for the Tor transport. Needs a node whose
        ``SZPONTNET_TOR`` is unset or on.
        """
        self._require_node()
        with _translated():
            return ctl.tor_connect(onion, timeout=timeout)

    def stop(self) -> None:
        """Ask the local node to shut down."""
        self._require_node()
        with _translated():
            ctl.stop(timeout=self.timeout)

    # ---- internals -----------------------------------------------------

    def _require_node(self) -> None:
        """Fail as :class:`NodeUnavailable` before a command that has nowhere to go.

        This is what makes the two exceptions distinguishable. The library raises
        one class for "nothing is listening" and for "the node said no", and the
        difference is only in the message; checking the two unavailable cases up
        front here means every remaining failure either carries an ``OSError``
        cause (the socket) or came from the node itself.
        """
        state = statefile.read_state()
        if state is None or not statefile.node_running(state):
            raise NodeUnavailable(
                "no local szpontnet node is running (no live state.json here); "
                "start one with `python -m szpontnet --daemon`")
        port = state.get("tcpPort")
        if not isinstance(port, int) or port <= 0:
            raise NodeUnavailable(
                "the local node's snapshot advertises no control port")


class _translated:
    """Re-raise the library's one control error as the one this package means.

    Wraps the call to :mod:`szpontnet.ctl` and nothing else - the preflight runs
    before the block, so everything arriving here came from the library.
    """

    def __enter__(self) -> "_translated":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or not isinstance(exc, ctl.CtlError):
            return False
        # ctl chains the socket failure it wrapped, and only that one, so the
        # cause is what separates "could not reach it" from "it answered no".
        if isinstance(exc.__cause__, OSError):
            raise NodeUnavailable(str(exc)) from exc
        raise CommandRejected(str(exc)) from exc
