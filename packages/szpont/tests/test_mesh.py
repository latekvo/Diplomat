"""Talking to the node.

The interesting half here is the exception split. The library raises one class for
every control-session failure and puts the difference in the message, so the thing
worth pinning is that this package tells the two apart *structurally* - by whether
a node was reachable at all - and never by reading the text of an error, which
would go quietly wrong the day a message is reworded.
"""

from __future__ import annotations

import json
import os

import pytest
from szpontnet import ctl, identity

from szpont import CommandRejected, Mesh, NodeUnavailable
from szpont.errors import SzpontError


@pytest.fixture
def live_node(snapshot_dict, monkeypatch):
    """A state.json naming a live pid - this test process, so the liveness check
    is the library's real one rather than a stub of it."""
    snapshot_dict["pid"] = os.getpid()
    state_dir = identity.mesh_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(snapshot_dict), encoding="utf-8")
    return snapshot_dict


@pytest.fixture
def calls(monkeypatch):
    """Record what reaches the library, so each wrapper method can be pinned to
    the call it is supposed to make."""
    recorded = []

    def recorder(name, result=None):
        def fn(*args, **kwargs):
            recorded.append((name, args, kwargs))
            return result() if callable(result) else result
        return fn

    monkeypatch.setattr(ctl, "status", recorder("status", lambda: {"self": {"name": "live"}}))
    monkeypatch.setattr(ctl, "dispatch", recorder("dispatch", lambda: [
        {"slot": "linux", "node": "b", "nodeName": "tower", "status": "spawned"}]))
    monkeypatch.setattr(ctl, "claim_work", recorder("claim_work", lambda: {
        "owned": False, "owner": "bbbb", "ownerName": "tower"}))
    monkeypatch.setattr(ctl, "set_attr", recorder("set_attr"))
    monkeypatch.setattr(ctl, "set_overrides", recorder("set_overrides"))
    monkeypatch.setattr(ctl, "trust_device", recorder("trust_device"))
    monkeypatch.setattr(ctl, "untrust_device", recorder("untrust_device"))
    monkeypatch.setattr(ctl, "ban_device", recorder("ban_device"))
    monkeypatch.setattr(ctl, "unban_device", recorder("unban_device"))
    monkeypatch.setattr(ctl, "set_default_trust", recorder("set_default_trust"))
    monkeypatch.setattr(ctl, "set_wan", recorder("set_wan"))
    monkeypatch.setattr(ctl, "connect", recorder("connect", ("tor", "xyz.onion")))
    monkeypatch.setattr(ctl, "tor_connect", recorder("tor_connect", "xyz.onion"))
    monkeypatch.setattr(ctl, "stop", recorder("stop"))
    return recorded


# ---- reading without a node ----------------------------------------------


def test_a_machine_where_no_node_ever_ran_reads_as_nothing():
    mesh = Mesh()

    assert mesh.running is False
    assert mesh.snapshot() is None


def test_a_corrupt_snapshot_reads_as_no_node_rather_than_raising():
    """The library's contract for an unreadable state.json, inherited: a bad file
    degrades to "no node" instead of crashing whoever polls it."""
    state_dir = identity.mesh_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text("{ not json", encoding="utf-8")

    assert Mesh().snapshot() is None
    assert Mesh().running is False


def test_a_snapshot_naming_a_dead_pid_still_reads_but_is_not_running(snapshot_dict):
    """A node that died leaves its last snapshot behind. It is worth reading -
    it is what the mesh last looked like - and it is not a live node."""
    snapshot_dict["pid"] = 0x7FFFFFFF  # no such process
    state_dir = identity.mesh_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(snapshot_dict), encoding="utf-8")

    mesh = Mesh()
    assert mesh.running is False
    assert mesh.snapshot().peer("tower").name == "tower"


def test_reading_the_snapshot_opens_no_control_session(live_node, monkeypatch):
    """It is a file read. A UI polls it every couple of seconds and must not be
    opening a socket to do it."""
    def fail(*_a, **_k):
        raise AssertionError("snapshot() must not talk to the node")

    monkeypatch.setattr(ctl, "request", fail)
    monkeypatch.setattr(ctl, "status", fail)

    assert Mesh().snapshot().node.name == "mbp"


# ---- the exception split --------------------------------------------------


def test_no_node_is_node_unavailable_and_nothing_is_attempted(monkeypatch):
    attempted = []
    monkeypatch.setattr(ctl, "dispatch", lambda *a, **k: attempted.append(a))

    with pytest.raises(NodeUnavailable):
        Mesh().dispatch("review", "prompt")

    assert attempted == []


def test_a_snapshot_with_no_control_port_is_node_unavailable(snapshot_dict):
    """A live pid is not enough - without a port there is nowhere to send the
    command, which is the same "nothing happened" as no node at all."""
    snapshot_dict["pid"] = os.getpid()
    del snapshot_dict["tcpPort"]
    state_dir = identity.mesh_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(snapshot_dict), encoding="utf-8")

    with pytest.raises(NodeUnavailable):
        Mesh().status()


def test_a_socket_failure_is_node_unavailable(live_node, monkeypatch):
    """The node died between the liveness check and the connect. Nothing was
    carried out, so this is the retryable kind."""
    def unreachable(*_a, **_k):
        raise ctl.CtlError("mesh node unreachable on :40878") from OSError("refused")

    monkeypatch.setattr(ctl, "status", unreachable)

    with pytest.raises(NodeUnavailable):
        Mesh().status()


def test_a_node_that_answers_with_an_error_is_command_rejected(live_node, monkeypatch):
    monkeypatch.setattr(ctl, "dispatch",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            ctl.CtlError("unknown duty 'nope'")))

    with pytest.raises(CommandRejected, match="unknown duty"):
        Mesh().dispatch("nope", "prompt")


def test_the_split_reads_the_cause_and_not_the_message(live_node, monkeypatch):
    """Both errors carry the same text. Only the chained OSError tells them
    apart, which is what keeps the classification from depending on wording."""
    same_text = "mesh node is having a bad day"

    def with_cause(*_a, **_k):
        raise ctl.CtlError(same_text) from OSError("refused")

    def without_cause(*_a, **_k):
        raise ctl.CtlError(same_text)

    monkeypatch.setattr(ctl, "status", with_cause)
    with pytest.raises(NodeUnavailable):
        Mesh().status()

    monkeypatch.setattr(ctl, "status", without_cause)
    with pytest.raises(CommandRejected):
        Mesh().status()


def test_both_errors_stay_catchable_as_the_librarys_own(live_node, monkeypatch):
    """The split is additive. Code already written against szpontnet keeps
    working after it starts getting these."""
    monkeypatch.setattr(ctl, "stop",
                        lambda *_a, **_k: (_ for _ in ()).throw(ctl.CtlError("no")))

    with pytest.raises(ctl.CtlError):
        Mesh().stop()
    with pytest.raises(SzpontError):
        Mesh().stop()


def test_the_unavailable_half_is_catchable_as_the_librarys_own_too():
    """No ``live_node``, so this is the preflight's own refusal rather than a
    translated one - the path that never touches :mod:`szpontnet.ctl` at all and
    could most easily fall outside its exception hierarchy."""
    with pytest.raises(ctl.CtlError):
        Mesh().status()
    with pytest.raises(SzpontError):
        Mesh().status()


def test_the_underlying_error_is_kept_as_the_cause(live_node, monkeypatch):
    monkeypatch.setattr(ctl, "stop",
                        lambda *_a, **_k: (_ for _ in ()).throw(ctl.CtlError("no")))

    with pytest.raises(CommandRejected) as caught:
        Mesh().stop()

    assert isinstance(caught.value.__cause__, ctl.CtlError)


# ---- what each call sends -------------------------------------------------


def test_status_returns_the_live_view(live_node, calls):
    assert Mesh().status().node.name == "live"
    assert [name for name, _, _ in calls] == ["status"]


def test_dispatch_forwards_every_routing_argument(live_node, calls):
    result = Mesh().dispatch("review", "the prompt", target="bbbb",
                             api_key="sk-1", work_key="review:o/r#1", timeout=12.0)

    name, args, kwargs = calls[0]
    assert name == "dispatch"
    assert args == ("review", "the prompt", "bbbb", "sk-1", "review:o/r#1")
    assert kwargs == {"timeout": 12.0}
    assert result[0].node_name == "tower"


def test_dispatch_defaults_to_the_mesh_choosing(live_node, calls):
    """No target, no key: the leaderless path, which is the whole point."""
    Mesh().dispatch("review", "p")

    _, args, kwargs = calls[0]
    assert args == ("review", "p", None, "", "")
    assert kwargs == {"timeout": 60.0}


def test_dispatch_gets_a_longer_default_timeout_than_a_plain_command(live_node, calls):
    """Routing may walk several failover candidates before it can answer, so the
    5-second default that suits an attribute edit would time out a healthy
    dispatch."""
    mesh = Mesh()
    mesh.dispatch("review", "p")
    mesh.set_attrs({"tier": "2"})

    assert calls[0][2]["timeout"] == 60.0
    assert calls[1][2]["timeout"] == 5.0


def test_a_declined_dispatch_is_a_return_value_not_an_exception(live_node, monkeypatch):
    """The exceptions are about reaching the local node. What the mesh decided is
    an outcome, and a caller has to be able to read the reason off it."""
    monkeypatch.setattr(ctl, "dispatch", lambda *_a, **_k: [
        {"slot": "linux", "node": "c", "nodeName": "stranger",
         "status": "declined", "reason": "foreign"}])

    result = Mesh().dispatch("review", "p")
    assert result.ok is False
    assert result[0].reason == "foreign"


def test_claim_returns_the_gates_verdict(live_node, calls):
    claim = Mesh().claim("review:o/r#1@sha")

    assert calls[0][0] == "claim_work"
    assert calls[0][1] == ("review:o/r#1@sha",)
    assert bool(claim) is False
    assert claim.owner_name == "tower"


def test_attributes_edit_this_machine_unless_a_target_is_named(live_node, calls):
    mesh = Mesh()
    mesh.set_attrs({"tier": "1"})
    mesh.set_attrs({"tokens": "out"}, target="bbbb")

    assert calls[0][1] == ("self", {"tier": "1"})
    assert calls[1][1] == ("bbbb", {"tokens": "out"})


def test_placement_edits_go_out_as_overrides(live_node, calls):
    Mesh().set_placement("review", {"tokenAware": False, "spread": []})

    assert calls[0][0] == "set_overrides"
    assert calls[0][1] == ("review", {"tokenAware": False, "spread": []})


def test_a_keyed_device_is_banned_by_fingerprint_and_a_keyless_one_by_id(live_node, calls):
    mesh = Mesh()
    mesh.ban(fingerprint="f" * 64, label="laptop", reason="went silent")
    mesh.ban(node="cccc")

    assert calls[0][1] == ("f" * 64, "")
    assert calls[0][2] == {"label": "laptop", "reason": "went silent", "timeout": 5.0}
    assert calls[1][1] == ("", "cccc")


def test_trust_and_its_reversal_reach_the_right_calls(live_node, calls):
    mesh = Mesh()
    mesh.trust("f" * 64, "tower")
    mesh.untrust("f" * 64)
    mesh.unban(node="cccc")
    mesh.set_default_trust("personal")

    assert [name for name, _, _ in calls] == [
        "trust_device", "untrust_device", "unban_device", "set_default_trust"]
    assert calls[0][1] == ("f" * 64, "tower")
    assert calls[3][1] == ("personal",)


def test_tor_connect_returns_the_address_being_dialed(live_node, calls):
    assert Mesh().tor_connect("ABC.onion") == "xyz.onion"
    assert calls[0][1] == ("ABC.onion",)


def test_connect_returns_the_transport_the_address_shape_picked(live_node, calls):
    assert Mesh().connect("ABC.onion") == ("tor", "xyz.onion")
    assert calls[0][1] == ("ABC.onion",)


def test_the_preferred_wan_transport_is_forwarded_verbatim(live_node, calls):
    """The bindings reshape nothing: an unknown name is the node's to refuse, so
    the caller gets its reason rather than a second, divergent vocabulary here."""
    Mesh().set_wan("iroh")
    assert (calls[0][0], calls[0][1]) == ("set_wan", ("iroh",))


def test_stop_asks_the_node_to_shut_down(live_node, calls):
    Mesh().stop()
    assert calls[0][0] == "stop"


def test_the_timeout_given_to_the_mesh_is_the_one_used(live_node, calls):
    Mesh(timeout=1.5).trust("f" * 64)
    assert calls[0][2]["timeout"] == 1.5


def test_peers_is_the_live_list(live_node, monkeypatch, snapshot_dict):
    monkeypatch.setattr(ctl, "status", lambda **_k: snapshot_dict)
    assert [p.name for p in Mesh().peers()] == ["tower", "stranger"]
