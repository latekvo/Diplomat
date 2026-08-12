"""The Mesh screen's WAN card: the transport pick, this machine's id, the paste box.

Reaching a machine off this network is the one part of the mesh with no automatic
path — the two ends have to be introduced, and until they are the screen is all a
user has. So the three things it promises are pinned here: that the preferred
transport reads and writes the mesh-wide pick, that the id shown is the one a peer
can actually dial (and only when it is live), and that a pasted id reaches the node.

Plus the two states an edge can be in that a healthy-looking link hides: which
transport it runs over now, and whether the pair can re-form once they part.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from diplomat_app.meshview import MeshView  # noqa: E402
from diplomat_app.store import Store  # noqa: E402

_SELF = "n-self"
_PEER = "n-peer"
_ENDPOINT = "3f" * 32
_ONION = "b" * 56 + ".onion"


def _snapshot(*, peer: dict | None = None, preferred: str = "iroh",
              **transports) -> dict:
    """A minimal live topology: this node plus one peer, with the WAN block under
    test. Defaults to the shipped state — both transports asked for, iroh up."""
    state = {
        "updatedAt": "now",
        "pid": os.getpid(),  # node_running() must see a live pid, or nothing renders
        "tcpPort": 40878,
        "self": {"id": _SELF, "name": "here", "platform": "linux", "tier": 3,
                 "tokens": "ok", "sees": [_PEER], "v": 1},
        "peers": [peer if peer is not None else {
            "id": _PEER, "name": "there", "platform": "macos", "tier": 3,
            "tokens": "ok", "sees": [_SELF], "v": 1, "link": "up",
            "addr": "192.168.1.9", "lastSeenSecsAgo": 1.0,
            "transport": "lan", "wan": "iroh",
        }],
        "assignments": {},
        "overrides": {"rev": 0, "updatedBy": "", "duties": {}},
        "wan": {"preferred": preferred, "transports": {
            "iroh": {"enabled": True, "ready": True, "address": _ENDPOINT},
            "tor": {"enabled": True, "ready": False, "address": None},
        }},
        "v": 1,
    }
    state["wan"]["transports"].update(transports)
    return state


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_view(app, monkeypatch):
    """A MeshView over a synthetic snapshot, with every control command recorded
    instead of opening a control socket to the operator's own node."""
    calls: list[tuple] = []
    monkeypatch.setattr(Store, "mesh_set_wan",
                        lambda self, t: calls.append(("set_wan", t)))
    monkeypatch.setattr(Store, "mesh_connect",
                        lambda self, a: calls.append(("connect", a)))
    views = []

    def build(state: dict) -> MeshView:
        store = Store()
        store._mesh_enabled_override = True  # render-only: never touches QSettings
        store.mesh_state = state
        v = MeshView(store)
        views.append(v)
        return v

    yield build, calls
    for v in views:
        v.deleteLater()


# ---- preferred transport -------------------------------------------------


def test_the_pick_shows_which_transport_the_mesh_prefers(make_view):
    build, _calls = make_view
    view = build(_snapshot(preferred="tor"))
    assert view.pref_buttons["tor"].isChecked()
    assert not view.pref_buttons["iroh"].isChecked()


def test_picking_a_transport_edits_the_mesh_wide_preference(make_view):
    build, calls = make_view
    view = build(_snapshot(preferred="iroh"))
    view.pref_buttons["tor"].click()
    assert calls == [("set_wan", "tor")]


# ---- my id ---------------------------------------------------------------


def test_my_id_is_the_address_a_peer_would_dial(make_view):
    build, _calls = make_view
    view = build(_snapshot())
    _row, value, copy = view.id_rows["iroh"]
    assert value._full == _ENDPOINT
    assert copy.isEnabled()


def test_copy_puts_this_machines_id_on_the_clipboard(app, make_view):
    """The point of the row: the operator hands this string to the other machine."""
    from PySide6.QtGui import QGuiApplication

    build, _calls = make_view
    view = build(_snapshot())
    QGuiApplication.clipboard().setText("something else")
    view._copy_my_id("iroh")
    assert QGuiApplication.clipboard().text() == _ENDPOINT


def test_a_transport_still_coming_up_offers_no_id_to_copy(make_view):
    """Tor's bootstrap runs into minutes. Copying then would hand a peer an empty
    string — worse than saying nothing, because it looks like an id."""
    build, _calls = make_view
    view = build(_snapshot())
    row, value, copy = view.id_rows["tor"]
    assert not row.isHidden()       # the operator asked for it…
    assert not copy.isEnabled()     # …and there is nothing to hand over yet
    assert value._full == "coming up…"


def test_a_transport_this_machine_never_asked_for_has_no_row(make_view):
    build, _calls = make_view
    view = build(_snapshot(tor={"enabled": False, "ready": False, "address": None}))
    row, _value, _copy = view.id_rows["tor"]
    assert row.isHidden()


def test_copying_an_absent_id_leaves_the_clipboard_alone(app, make_view):
    """A guard, not a nicety: overwriting the clipboard with "" silently destroys
    whatever the operator had copied to paste into the box below."""
    from PySide6.QtGui import QGuiApplication

    build, _calls = make_view
    view = build(_snapshot())
    QGuiApplication.clipboard().setText("keep me")
    view._copy_my_id("tor")  # enabled but not ready
    assert QGuiApplication.clipboard().text() == "keep me"


# ---- connect to id -------------------------------------------------------


def test_a_pasted_id_is_handed_to_the_node(make_view):
    build, calls = make_view
    view = build(_snapshot())
    view.connect_field.setText(f"  {_ONION} ")
    view.connect_btn.click()
    assert calls == [("connect", _ONION)]


def test_the_box_empties_on_submit(make_view):
    """The dial is a background one-shot; a leftover id would be re-sent by the
    next Return."""
    build, _calls = make_view
    view = build(_snapshot())
    view.connect_field.setText(_ENDPOINT)
    view.connect_btn.click()
    assert view.connect_field.text() == ""


def test_an_empty_box_dials_nobody(make_view):
    build, calls = make_view
    view = build(_snapshot())
    view.connect_btn.click()
    assert calls == []


def test_with_no_transport_of_our_own_there_is_nothing_to_dial_from(make_view):
    """A dial leaves over one of OUR transports, so with none up the box would
    only ever produce an error from the node."""
    build, _calls = make_view
    view = build(_snapshot(
        iroh={"enabled": False, "ready": False, "address": None},
        tor={"enabled": False, "ready": False, "address": None}))
    assert not view.connect_btn.isEnabled()
    assert not view.connect_field.isEnabled()
    assert not view.wan_note.isHidden()


def test_a_poll_does_not_eat_the_id_being_pasted(make_view):
    """The snapshot rebuild that repaints the rest of the screen must leave this
    field alone — the 2s poll would otherwise clear a half-pasted id."""
    build, _calls = make_view
    view = build(_snapshot())
    view.connect_field.setText(_ENDPOINT[:20])
    view.store.mesh_state = _snapshot(preferred="tor")
    view._rebuild()
    assert view.connect_field.text() == _ENDPOINT[:20]
    assert view.pref_buttons["tor"].isChecked()  # …while the rest did update


# ---- one edge, one transport ---------------------------------------------


def _edge_texts(view) -> list[str]:
    from PySide6.QtWidgets import QLabel

    peer = (view.store.mesh_state["peers"] or [{}])[0]
    row = view._edge_row(peer)
    return [row.itemAt(i).widget().text() for i in range(row.count())
            if isinstance(row.itemAt(i).widget(), QLabel)]


def test_a_link_over_the_wan_names_the_transport_carrying_it(make_view):
    build, _calls = make_view
    view = build(_snapshot(peer={
        "id": _PEER, "name": "there", "platform": "macos", "link": "up",
        "addr": _ONION, "transport": "tor", "wan": "tor"}))
    assert [t for t in _edge_texts(view) if "Tor" in t]
    assert not [t for t in _edge_texts(view) if "LAN" in t]


def test_a_link_on_this_network_still_shows_how_it_would_re_form(make_view):
    """The pairing that matters: the link is fine now, and the reason it will
    still be fine after the two machines part is the WAN transport they share."""
    build, _calls = make_view
    view = build(_snapshot())
    texts = _edge_texts(view)
    assert [t for t in texts if "LAN" in t]
    assert [t for t in texts if "Iroh" in t and "off-LAN" in t]


def test_a_pair_that_shares_no_transport_is_flagged(make_view):
    """The 'not none' case: both ends are up, the link works, and it can never
    re-form once they leave this network. Nothing else on the screen says so."""
    build, _calls = make_view
    view = build(_snapshot(peer={
        "id": _PEER, "name": "there", "platform": "macos", "link": "up",
        "addr": "192.168.1.9", "transport": "lan", "wan": ""}))
    assert [t for t in _edge_texts(view) if t.startswith("⚠")]


def test_a_lan_only_machine_is_not_told_off_once_per_peer(make_view):
    """With no WAN transport here, no peer can share one — that is this machine's
    own state, said once in the card above, not a fault of every peer."""
    build, _calls = make_view
    view = build(_snapshot(
        peer={"id": _PEER, "name": "there", "platform": "macos", "link": "up",
              "addr": "192.168.1.9", "transport": "lan", "wan": ""},
        iroh={"enabled": False, "ready": False, "address": None},
        tor={"enabled": False, "ready": False, "address": None}))
    assert not [t for t in _edge_texts(view) if t.startswith("⚠")]
    assert [t for t in _edge_texts(view) if "this network only" in t]
