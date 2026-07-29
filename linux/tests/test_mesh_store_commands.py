"""The Store's mesh control commands: argument passing, error surfacing, refresh.

Every mesh edit the panel offers (set an attribute, trust/untrust a device, lift
a ban, re-place a duty) is the same routine around one ``ctl`` call: run it off
the UI thread, put any :class:`ctl.CtlError` in ``mesh_error``, then re-read the
topology so the edit shows. Every step is invisible when it goes missing - drop
the refresh and the screen keeps rendering pre-edit state, drop the error
assignment and a rejected edit looks like it worked - so the five commands share
one routine, ``Store._mesh_command``.

These pin all three of its steps plus the arguments each command forwards.
"""

from __future__ import annotations

import threading

import pytest

from diplomat_app.store import Store


_COMMANDS = ("set_attr", "trust_device", "untrust_device",
             "unban_device", "set_overrides")


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
    from diplomat_app.mesh import ctl as real_ctl

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
])
def test_the_fake_covers_the_call_each_command_makes(method, expected):
    """Anti-vacuity: the ``ctl`` fake only intercepts the five functions listed in
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
])
def test_every_command_surfaces_errors_and_refreshes(store, ctl, command):
    """The property has to hold for all five, not just the one spot-checked above -
    that is the whole reason they share a routine."""
    ctl.fail_with = "boom"
    command(store)
    _settle()
    assert store.mesh_error == "boom"
    assert store._test_refreshes == [1]


def test_commands_do_not_block_the_caller(store, monkeypatch):
    """The panel calls these from a click handler on the UI thread; a synchronous
    control round-trip would freeze the popover for the socket timeout."""
    from diplomat_app.mesh import ctl as real_ctl

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
