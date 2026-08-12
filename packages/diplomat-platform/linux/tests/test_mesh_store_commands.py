"""The Store's mesh control commands: argument passing, error surfacing, refresh.

Every mesh edit the panel offers (set an attribute, trust/untrust a device, lift
a ban, re-place a duty, pick the preferred WAN transport, link to a pasted id) is
the same routine around one ``ctl`` call: run it off the UI thread, put any
:class:`ctl.CtlError` in ``mesh_error``, then re-read the topology so the edit
shows. Every step is invisible when it goes missing - drop the refresh and the
screen keeps rendering pre-edit state, drop the error assignment and a rejected
edit looks like it worked - so the commands share one routine,
``Store._mesh_command``.

These pin all three of its steps plus the arguments each command forwards, and
the one command that is not a ``ctl`` call at all: starting the node process, whose
whole configuration is the environment it is handed.
"""

from __future__ import annotations

import os
import threading

import pytest

from diplomat_app.store import Store


_COMMANDS = ("set_attr", "trust_device", "untrust_device",
             "unban_device", "set_overrides", "set_wan", "connect")


class Recorder:
    """Stands in for the whole ``ctl`` surface: records calls, can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_with: str | None = None


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(app, monkeypatch):
    s = Store()
    refreshes: list[int] = []
    monkeypatch.setattr(s, "refresh_mesh_state", lambda: refreshes.append(1))
    s._test_refreshes = refreshes
    return s


@pytest.fixture
def ctl(monkeypatch):
    """Patch the real ``ctl`` module's functions.

    ``Store._mesh_command`` imports ``.mesh.ctl`` itself, so there is nothing to
    inject a fake into - the module's own attributes are the seam.
    """
    from szpontnet import ctl as real_ctl

    rec = Recorder()

    def record(name):
        def fn(*args, **kwargs):
            rec.calls.append((name, args, kwargs))
            if rec.fail_with is not None:
                raise real_ctl.CtlError(rec.fail_with)
        return fn

    for name in _COMMANDS:
        monkeypatch.setattr(real_ctl, name, record(name))
    return rec


def _settle():
    """Join the daemon thread the command started."""
    for t in threading.enumerate():
        if t.name.startswith("mesh-"):
            t.join(timeout=5)
            assert not t.is_alive(), f"{t.name} did not finish"


@pytest.mark.parametrize("method,expected", [
    ("mesh_set_attr", "set_attr"),
    ("mesh_trust", "trust_device"),
    ("mesh_untrust", "untrust_device"),
    ("mesh_unban", "unban_device"),
    ("mesh_set_overrides", "set_overrides"),
    ("mesh_set_wan", "set_wan"),
    ("mesh_connect", "connect"),
])
def test_the_fake_covers_the_call_each_command_makes(method, expected):
    """Anti-vacuity: the ``ctl`` fake only intercepts the functions listed in
    ``_COMMANDS``. If a command is repointed at some other ``ctl`` function the
    tests below would silently start driving the real control socket, so pin the
    mapping here instead."""
    import inspect

    assert expected in _COMMANDS
    assert f"ctl.{expected}(" in inspect.getsource(getattr(Store, method))


# ---- each command forwards what it was given -----------------------------


def test_set_attr_forwards_node_and_attrs(store, ctl):
    store.mesh_set_attr("node-1", {"tier": 3})
    _settle()
    assert ctl.calls == [("set_attr", ("node-1", {"tier": 3}), {})]


def test_trust_forwards_fingerprint_and_label(store, ctl):
    store.mesh_trust("ff11", "my-laptop")
    _settle()
    assert ctl.calls == [("trust_device", ("ff11", "my-laptop"), {})]


def test_untrust_forwards_the_fingerprint(store, ctl):
    store.mesh_untrust("ff11")
    _settle()
    assert ctl.calls == [("untrust_device", ("ff11",), {})]


def test_unban_forwards_fingerprint_and_node(store, ctl):
    store.mesh_unban("ee22", "flaky-box")
    _settle()
    assert ctl.calls == [("unban_device", ("ee22", "flaky-box"), {})]


def test_set_overrides_forwards_duty_and_placement(store, ctl):
    store.mesh_set_overrides("review", {"strategy": "weakest-first"})
    _settle()
    assert ctl.calls == [("set_overrides", ("review", {"strategy": "weakest-first"}), {})]


def test_set_wan_forwards_the_transport(store, ctl):
    store.mesh_set_wan("tor")
    _settle()
    assert ctl.calls == [("set_wan", ("tor",), {})]


def test_connect_forwards_the_pasted_id_verbatim(store, ctl):
    """Verbatim: the node owns parsing — it is what decides which transport an id
    names — so the Store reshapes nothing on the way there."""
    pasted = "  IROH://" + "A" * 64 + "  "
    store.mesh_connect(pasted)
    _settle()
    assert ctl.calls == [("connect", (pasted,), {})]


# ---- the steps every command shares --------------------------------------


def test_a_successful_edit_clears_any_previous_error(store, ctl):
    store.mesh_error = "something earlier went wrong"
    store.mesh_trust("ff11")
    _settle()
    assert store.mesh_error is None


def test_a_successful_edit_refreshes_the_topology(store, ctl):
    """Without this the screen keeps rendering pre-edit state until the next poll."""
    store.mesh_trust("ff11")
    _settle()
    assert store._test_refreshes == [1]


def test_a_rejected_edit_surfaces_the_reason(store, ctl):
    """A CtlError has to reach ``mesh_error`` - the mesh screen renders it. Swallowing
    it makes a refused edit indistinguishable from an applied one."""
    ctl.fail_with = "node not running"
    store.mesh_trust("ff11")
    _settle()
    assert store.mesh_error == "node not running"


def test_a_rejected_edit_still_refreshes(store, ctl):
    """The refresh is unconditional: a rejected edit must still re-read the real
    state, or the screen is left showing the change the user attempted."""
    ctl.fail_with = "node not running"
    store.mesh_trust("ff11")
    _settle()
    assert store._test_refreshes == [1]


@pytest.mark.parametrize("command", [
    pytest.param(lambda s: s.mesh_set_attr("n", {}), id="set_attr"),
    pytest.param(lambda s: s.mesh_trust("ff11"), id="trust"),
    pytest.param(lambda s: s.mesh_untrust("ff11"), id="untrust"),
    pytest.param(lambda s: s.mesh_unban("ff11"), id="unban"),
    pytest.param(lambda s: s.mesh_set_overrides("review", {}), id="set_overrides"),
    pytest.param(lambda s: s.mesh_set_wan("iroh"), id="set_wan"),
    pytest.param(lambda s: s.mesh_connect("a" * 64), id="connect"),
])
def test_every_command_surfaces_errors_and_refreshes(store, ctl, command):
    """The property has to hold for every one, not just the one spot-checked above -
    that is the whole reason they share a routine."""
    ctl.fail_with = "boom"
    command(store)
    _settle()
    assert store.mesh_error == "boom"
    assert store._test_refreshes == [1]


def test_commands_do_not_block_the_caller(store, monkeypatch):
    """The panel calls these from a click handler on the UI thread; a synchronous
    control round-trip would freeze the popover for the socket timeout."""
    from szpontnet import ctl as real_ctl

    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=5)
        returned.set()

    monkeypatch.setattr(real_ctl, "trust_device", slow)
    store.mesh_trust("ff11")
    assert entered.wait(timeout=5), "the command never ran"
    # We are back on the calling thread while the round-trip is still in flight.
    assert not returned.is_set(), "mesh_trust ran the control call synchronously"
    release.set()
    _settle()
    assert returned.is_set()


#: A developer's own ``PYTHONPATH``, so the entries the applet adds can be told apart
#: from the ones it merely forwards.
AMBIENT = "/opt/ambient-python-path"

PACKAGES = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


@pytest.fixture
def spawned(store, monkeypatch):
    """Run ``ensure_mesh_running_async`` against a stubbed ``Popen`` and return the
    call it made.

    Nothing else reaches this code: the node it starts is a real background process,
    so every other mesh test rebuilds that environment by hand rather than running the
    method that builds it.
    """
    import subprocess

    from szpontnet import statefile

    monkeypatch.setenv("PYTHONPATH", AMBIENT)
    monkeypatch.setattr(statefile, "node_running", lambda: False)
    monkeypatch.setattr(store, "_mesh_enabled_override", True)
    calls: list[dict] = []
    done = threading.Event()

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        done.set()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    store.ensure_mesh_running_async()
    assert done.wait(timeout=5), "no node was started"
    return calls[0]


def _path(spawned) -> list[str]:
    return spawned["env"]["PYTHONPATH"].split(os.pathsep)


def test_the_node_is_handed_the_shared_runtime_and_nothing_of_the_applet(spawned):
    """The node is a separate stdlib-only process that imports its host by name off
    ``PYTHONPATH``. Hand it this package instead and every duty it runs reaches for
    PySide6 on a machine that may have none."""
    added = [p for p in _path(spawned) if p != AMBIENT]
    assert os.path.join(PACKAGES, "diplomat-runtime") in added
    assert os.path.join(PACKAGES, "diplomat-platform", "linux") not in added


def test_the_host_module_it_names_is_importable_from_that_path(spawned):
    """The name and the path are one answer in two halves: name a host the node cannot
    import and it comes up on the library's own defaults - its own state directory,
    none of our duties, no activity feed - which looks like a healthy mesh right up
    until a peer routes work here."""
    host = spawned["env"]["SZPONTNET_HOST"]
    module = os.path.join(*host.split(".")) + ".py"
    assert any(os.path.isfile(os.path.join(p, module)) for p in _path(spawned)), host


def test_a_developers_own_import_path_is_kept(spawned):
    """Prepended, not replaced: the applet is often run out of a virtualenv or an
    editable install that lives on that path."""
    assert AMBIENT in _path(spawned)


def test_the_node_is_started_detached_from_the_applet(spawned):
    """It outlives the applet on purpose - a peer's work is placed on this machine's
    node, not on this window."""
    assert spawned["argv"][1:] == ["-m", "szpontnet", "--daemon"]
    assert spawned["start_new_session"] is True
