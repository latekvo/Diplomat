"""What a node needs from whoever is running it.

SzpontNet routes work between machines. It has no opinion about *what* the work
is, how a machine actually runs it, or where a node's events should be recorded —
and it must not, or it stops being a library and becomes half of one application.
So the five questions it cannot answer alone are asked of a **host**:

* :meth:`Host.model` — the network model this deployment runs (its duty catalog,
  any retuned protocol constant). Merged over ``netmodel.json``, the canonical v1
  model from ``szpontnet/docs/appendix-b-constants.md``.
* :meth:`Host.state_dir` — where this node's identity and snapshot live. The
  device keypair there is what every peer's trust allowlist is keyed to, so a host
  that already owns a node's state keeps naming that directory.
* :meth:`Host.log` — where a node event goes. A node narrates a lot (peer up,
  dispatch, ban, spawn); a library that picks the sink for that is picking the
  application's logging.
* :meth:`Host.run_job` — run a *personal* job here. This is the whole point of a
  duty: the mesh decides **which machine**, the host decides **what running it
  means**. Only reached when the operator configured no ``SZPONTNET_SPAWN``
  template, which is the deployment-independent way to say it.
* :meth:`Host.work_already_running` — whether the work behind a key is already
  under way on this machine by some means the mesh never saw. Only the
  application knows how to look.

Every one has a working default, so the package imports and a node runs with no
host at all — it just advertises the v1 duties, discards its log, declines work
it has no runner for, and dedups solely against its own book.

Two ways to register:

* in-process, :func:`set_host` — for an application driving the node's modules
  directly (its own UI reading placements, say);
* out-of-process, ``SZPONTNET_HOST=<module>`` — the daemon imports that module and
  calls its ``host()`` factory. This is how an application that *spawns* a node
  puts itself behind it, since the node is a separate process.
"""

from __future__ import annotations

import functools
import importlib
import json
from pathlib import Path

from . import env
from .launch import JobSpawnError

_NETMODEL = Path(__file__).resolve().parent / "netmodel.json"


class Host:
    """The default host: a node with no application behind it.

    Subclass and override only what you answer differently — each default is the
    honest answer for "nobody is home", never a placeholder that pretends.
    """

    def model(self) -> dict:
        """This deployment's network model, merged one level deep over the
        library's own. Empty = run the canonical v1 model unchanged."""
        return {}

    def state_dir(self) -> Path:
        """Where this node keeps its identity, trust allowlist and topology
        snapshot. ``~/.szpontnet`` by default.

        A host answers differently when the node's state belongs to *it* — an
        application that has been writing a node identity somewhere of its own
        keeps answering with that directory, because the device keypair in it is
        what every peer's trust allowlist is keyed to. Move it and the machine
        becomes a stranger to its own fleet.
        """
        return Path.home() / ".szpontnet"

    def log(self, action: str, detail: str) -> None:
        """Record one node event. Discarded by default — a node is not entitled
        to pick a log file on a machine it knows nothing about."""

    def run_job(self, prompt: str, done_path: str | None) -> str:
        """Run a personal (trusted-peer) job on this machine and return a handle
        for it — conventionally the path the prompt was staged at.

        ``done_path``, when given, is the completion sentinel the executor watches
        to free its work-claim; whatever runs the job must create it on exit.

        Raising :class:`NoRunner` is a legitimate answer and the default one: this
        machine cannot take the job, so the dispatcher fails over to the next
        candidate. It is *not* an error condition to have no runner.
        """
        raise NoRunner("no host runner (set SZPONTNET_SPAWN, or register a host)")

    def work_already_running(self, work_key: str) -> bool:
        """Whether this machine is already working on ``work_key`` by some route
        the mesh never saw — a locally-started agent, a leftover from before a
        node restart.

        The executor's ground-truth floor against a double-spawn, and unknowable
        from inside the protocol: the mesh sees claims, not processes. Default
        ``False`` — no host, no second opinion, so the claim book decides alone.
        Answer conservatively: a false ``True`` silently drops work.
        """
        return False


class NoRunner(JobSpawnError):
    """This machine has no way to run the job it was handed.

    A :class:`~.launch.JobSpawnError`, so the dispatcher's fail-over path handles
    "nobody configured a runner here" as the ordinary decline it is.
    """


_host: Host | None = None


def set_host(host: Host) -> None:
    """Put ``host`` behind this node, in this process. Overrides ``SZPONTNET_HOST``.

    Drops the resolved-model cache, or a registration that lands after anything
    has read a placement leaves the node running on the library's duty catalog
    while the host believes its own is in force.
    """
    global _host
    _host = host
    model.cache_clear()


def reset_host() -> None:
    """Drop the registered host and the resolved-model cache — back to the
    library's own defaults. For tests, and for a host that reconfigures."""
    global _host
    _host = None
    model.cache_clear()


def host() -> Host:
    """The host behind this node.

    Resolution order: an explicit :func:`set_host`, then ``SZPONTNET_HOST`` (a
    module exporting a ``host()`` factory), then the null :class:`Host`. A named
    module that won't import, or that has no factory, resolves to the null host:
    a node that runs and declines is a better failure than a node that won't
    start, and the caller finds out the moment it is handed work.
    """
    if _host is not None:
        return _host
    name = (env.get("HOST") or "").strip()
    if not name:
        return _NULL_HOST
    try:
        factory = importlib.import_module(name).host
    except (ImportError, AttributeError):
        return _NULL_HOST
    try:
        resolved = factory()
    except Exception:  # noqa: BLE001 - a broken host must not stop the node
        return _NULL_HOST
    return resolved if isinstance(resolved, Host) else _NULL_HOST


_NULL_HOST = Host()


def log(action: str, detail: str) -> None:
    """Record one node event through the current host — the spelling the node
    itself uses, so a narration line never has to resolve the host by hand."""
    host().log(action, detail)


def work_already_running(work_key: str) -> bool:
    """Ask the current host whether this machine is already on ``work_key``.

    Fails **open** (``False``) if the host raises: a second opinion that errors
    must not drop a dispatch on the floor — a duplicate agent is recoverable,
    silently declined work is not.
    """
    try:
        return bool(host().work_already_running(work_key))
    except Exception:  # noqa: BLE001 - a broken host must not cost us the job
        return False


@functools.lru_cache(maxsize=1)
def netmodel() -> dict:
    """The canonical v1 network model shipped with the library."""
    return json.loads(_NETMODEL.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def model() -> dict:
    """The effective network model: the host's overlay over the library's own.

    Merged one level deep, so an overlay that restates ``tiers.labels`` keeps the
    library's ``tiers.min``/``max``, while a list value (the duty catalog) is
    replaced wholesale — a deployment that names its duties means *those* duties,
    not those plus ours.

    Cached: this is read on every placement resolution and every advert. A host
    that registers later (or changes its answer) must call :func:`reset_host`.
    """
    merged = dict(netmodel())
    for key, value in (host().model() or {}).items():
        base = merged.get(key)
        if isinstance(base, dict) and isinstance(value, dict):
            merged[key] = {**base, **value}
        else:
            merged[key] = value
    return merged
