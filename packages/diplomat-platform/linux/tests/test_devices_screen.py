"""The Devices screen and what it stands on: the daemon's snapshot read lossily, the
daemon's replies to the actions and the remote-pool calls, the provider setting, and
the screen in each state that builds a different tree.

The decoding and reply cases are the twins of the macOS ``DIPLOMAT_ALLOCATOR_TEST``
(AllocatorSetupTest.swift), so the two applets read one daemon the same way.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from diplomat_runtime import activity, appconfig
from diplomat_app import deviceallocator as da

pytest.importorskip("PySide6")

ROW = {"key": "ios:A", "platform": "ios", "name": "iPhone", "status": "free"}
NO_POOL = "this allocator has no remote device pool — update it, then restart its daemon"


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# MARK: the snapshot

def test_a_snapshot_without_a_remote_pool():
    st = da.decode_state({"updatedAt": "x", "daemonPid": 812, "devices": [ROW]})
    assert st == {"devices": [ROW], "daemonPid": 812, "remote": None}
    assert da.is_remote(st["devices"][0]) is False


def test_a_remote_status_is_read_and_its_unknown_keys_kept_in_key_order():
    st = da.decode_state({"devices": [ROW, {
        "key": "p:7", "platform": "android", "source": "remote",
        "handle": "127.0.0.1:41207", "status": "booting",
        "owner": {"agentName": "e2e", "ownerPid": 42}}],
        "remote": {"provider": "lab-pool", "state": "signing-in",
                   "loginUrl": "https://h/login", "zone": "b", "count": 3,
                   "retryAt": "2026-10-27T18:05:00.000Z"}})
    assert st["remote"] == da.RemoteStatus(
        state="signing-in", provider="lab-pool", login_url="https://h/login",
        retry_at=1_793_124_300.0, extras=(("count", "3"), ("zone", "b")))
    assert [da.is_remote(d) for d in st["devices"]] == [False, True]
    assert da.status_badge(st["devices"][1]) == ("attaching", "#FF9500")


def test_an_unreadable_row_costs_only_itself_and_garbage_elsewhere_costs_nothing():
    st = da.decode_state({"daemonPid": "x", "remote": "broken", "devices": [
        ROW,
        {"key": "b", "platform": "ios", "status": 7},
        {"key": "c", "platform": "ios", "status": "ready", "owner": "pid 4"},
        {"key": "d", "platform": "ios", "status": "ready", "owner": {"ownerPid": 4.5}},
        {"key": "e", "platform": "ios", "status": "ready", "owner": {"ownerPid": True}},
        {"key": "f", "platform": "ios", "status": "ready", "idleMs": "long"},
        "junk",
        {**ROW, "key": "g", "owner": {"agentName": "a", "ownerPid": 9.0}},
    ]})
    assert [d["key"] for d in st["devices"]] == ["ios:A", "g"]
    assert st["daemonPid"] is None
    # A value that is not an object is a status that cannot be read, not an absent pool.
    assert st["remote"] == da.RemoteStatus(state="unknown", extras=(("remote", "broken"),))


@pytest.mark.parametrize("snapshot", [{"devices": []}, {"devices": [], "remote": None}])
def test_an_absent_or_null_remote_is_no_pool(snapshot):
    assert da.decode_state(snapshot)["remote"] is None


def test_odd_remote_fields_are_read_leniently():
    r = da.decode_state({"devices": [], "remote": {
        "state": 5, "provider": 7, "retryAt": "soon", "reason": None}})["remote"]
    assert r == da.RemoteStatus(state="unknown", extras=(("retryAt", "soon"),))


def test_retry_at_in_epoch_milliseconds_and_without_milliseconds():
    for v in (1_793_124_300_000, "2026-10-27T18:05:00Z", "2026-10-27T19:05:00+01:00"):
        st = da.decode_state({"devices": [], "remote": {"state": "x", "retryAt": v}})
        assert st["remote"].retry_at == 1_793_124_300.0, v
    # A date alone is not an instant, and is shown as it says.
    st = da.decode_state({"devices": [], "remote": {"state": "x", "retryAt": "2026-10-27"}})
    assert st["remote"].retry_at is None
    assert st["remote"].extras == (("retryAt", "2026-10-27"),)


@pytest.mark.parametrize("raw", [None, [], {"devices": "x"}, {"remote": {"state": "on"}}])
def test_without_a_device_list_there_is_no_snapshot(raw):
    assert da.decode_state(raw) is None


def test_the_state_file_is_read_from_da_base_dir(tmp_path):
    base = tmp_path / "device-allocator"
    base.mkdir()
    (base / "state.json").write_text(json.dumps(
        {"updatedAt": "t", "devices": [ROW], "remote": {"state": "on", "provider": "p"}}))
    assert da.read_state() == {"devices": [ROW], "daemonPid": None,
                               "remote": da.RemoteStatus(state="on", provider="p")}
    assert da.socket_path() == base / "daemon.sock"


def test_unknown_values_are_shown_as_one_line():
    assert [da.json_text(v) for v in ("s", 3, 3.0, 1.5, True, None, [1, "a"],
                                      {"b": 1, "a": [2]})] == \
        ["s", "3", "3", "1.5", "true", "—", "1, a", "a: 2, b: 1"]


# MARK: the daemon's replies

def reply(body, status):
    return da.DaemonReply(status, body)


def test_a_daemon_without_the_route_says_it_has_no_remote_pool():
    r = reply({"error": "unknown route /remote/devices"}, 404)
    assert da.devices_reply(r) == da.RemoteReply(reason=NO_POOL)
    assert da.sign_in_reply(r) == da.RemoteReply(reason=NO_POOL)


def test_another_404_is_its_own_error_not_the_missing_pool():
    assert da.sign_in_reply(reply({"error": "no such device"}, 404)) == \
        da.RemoteReply(reason="no such device")
    assert da.sign_in_reply(reply(None, 502)) == \
        da.RemoteReply(reason="the allocator answered HTTP 502")


@pytest.mark.parametrize("r,want", [
    (None, da.RemoteReply(reason="the allocator daemon did not answer")),
    (reply({"ok": True}, 200), da.RemoteReply(ok=True)),
    (reply({"ok": False, "reason": "timed out"}, 200), da.RemoteReply(reason="timed out")),
    (reply({"ok": 1}, 200), da.RemoteReply(reason="the sign-in did not complete")),
    (reply({"error": "boom"}, 500), da.RemoteReply(reason="boom")),
])
def test_sign_in_replies(r, want):
    assert da.sign_in_reply(r) == want


def test_a_pool_listing_keeps_object_rows_and_stringifies_numbers():
    r = reply({"devices": [{"key": "k", "name": "S24", "version": 14, "state": "ready"},
                           "junk", {"name": "no key"}]}, 200)
    assert da.devices_reply(r) == da.RemoteReply(devices=(
        da.RemotePoolDevice(id="k", name="S24", version="14", state="ready"),
        da.RemotePoolDevice(id="#2", name="no key"),
    ))


def test_a_pool_that_cannot_list_says_why():
    assert da.devices_reply(reply({"reason": "not signed in"}, 200)) == \
        da.RemoteReply(reason="not signed in")
    assert da.devices_reply(reply({"devices": "x"}, 200)) == \
        da.RemoteReply(reason="the pool sent no device list")


# MARK: the socket

class _FakeDaemon:
    """An HTTP server on a unix socket in a short /tmp dir (the path limit), answering
    each route from ``routes`` and recording what it was sent."""

    def __init__(self, routes):
        self.dir = tempfile.mkdtemp(prefix="dae.", dir="/tmp")
        self.path = os.path.join(self.dir, "daemon.sock")
        self.got: list[tuple[str, object]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.got.append((self.path, body))
                status, answer = routes.get(self.path, (404, {"error": f"unknown route {self.path}"}))
                if status is None:
                    time.sleep(answer)
                    return
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(self.path, Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def daemon(monkeypatch):
    made = []

    def make(routes):
        d = _FakeDaemon(routes)
        monkeypatch.setenv("DA_BASE_DIR", d.dir)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def test_release_asks_on_the_holders_behalf_and_reads_what_it_freed(daemon):
    d = daemon({"/release": (200, {"released": 1})})
    assert da.release_device("ios:A", 4242) is True
    assert d.got == [("/release", {"ownerPid": 4242, "deviceId": "ios:A"})]


def test_a_release_that_freed_nothing_is_not_a_success(daemon):
    daemon({"/release": (200, {"released": 0})})
    assert da.release_device("ios:A", 4242) is False


def test_kill_and_repaired_post_the_key_and_a_refusal_is_not_a_success(daemon):
    d = daemon({"/kill": (200, {"killed": "k"}), "/repaired": (404, {"error": "no"})})
    assert da.kill_device("k") is True
    assert da.mark_repaired("k") is False
    assert d.got == [("/kill", {"key": "k"}), ("/repaired", {"key": "k"})]


def test_the_remote_calls_reach_their_routes(daemon):
    d = daemon({"/remote/sign-in": (200, {"ok": True}),
                "/remote/devices": (200, {"devices": [{"key": "k"}]})})
    assert da.remote_sign_in() == da.RemoteReply(ok=True)
    assert da.remote_devices() == da.RemoteReply(devices=(da.RemotePoolDevice(id="k"),))
    assert [p for p, _ in d.got] == ["/remote/sign-in", "/remote/devices"]


def test_a_daemon_predating_the_pool_is_named_through_the_socket(daemon):
    daemon({})
    assert da.remote_sign_in() == da.RemoteReply(reason=NO_POOL)


def test_the_sign_in_waits_long_enough_for_a_browser_sign_in(monkeypatch):
    seen = {}
    monkeypatch.setattr(da, "call", lambda ep, body=None, *, timeout_secs:
                        seen.setdefault(ep, timeout_secs) and None)
    da.remote_sign_in()
    da.remote_devices()
    assert seen == {"remote/sign-in": 690, "remote/devices": 90}


def test_no_socket_or_a_silent_daemon_is_no_answer(daemon, tmp_path, monkeypatch):
    monkeypatch.setenv("DA_BASE_DIR", str(tmp_path / "nothing"))
    assert da.call("kill", {"key": "k"}, timeout_secs=1) is None
    daemon({"/kill": (None, 2.0)})
    started = time.monotonic()
    assert da.call("kill", {"key": "k"}, timeout_secs=0.3) is None
    assert time.monotonic() - started < 1.5


# MARK: the store

def test_the_sign_in_runs_off_the_gui_thread_and_one_at_a_time(app, monkeypatch):
    from diplomat_app.store import Store

    gate = threading.Event()
    calls = []

    def blocking():
        calls.append(threading.current_thread() is threading.main_thread())
        gate.wait(10)
        return da.RemoteReply(ok=True)

    monkeypatch.setattr(da, "remote_sign_in", blocking)
    store = Store()
    signals = []
    store.remote_changed.connect(lambda: signals.append(store.remote_sign_in))
    started = time.monotonic()
    store.remote_sign_in_async()
    assert time.monotonic() - started < 0.5
    assert store.remote_sign_in.running
    store.remote_sign_in_async()  # a second click while it waits
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [False]
    gate.set()
    assert store.wait_for_background(5) == []
    app.processEvents()
    assert store.remote_sign_in.reply == da.RemoteReply(ok=True)
    assert store.remote_sign_in.at is not None
    assert [c.running for c in signals] == [True, False]


def test_a_free_is_logged_with_what_really_happened(app, monkeypatch):
    from diplomat_app.store import Store

    outcomes = iter([True, False])
    monkeypatch.setattr(da, "release_device", lambda key, pid: next(outcomes))
    store = Store()
    dev = {"key": "ios:A", "name": "iPhone", "owner": {"agentName": "e2e", "ownerPid": 7}}
    for _ in range(2):
        store.release_device_async(dev)
        assert store.wait_for_background(5) == []
    assert [e.detail for e in activity.read()][:2] == ["Free FAILED for iPhone",
                                                     "Freed iPhone from e2e"]


def test_the_provider_is_written_trimmed_and_blank_removes_it(monkeypatch):
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    store = Store()
    store.device_remote_provider = "  ~/pool/provider.mjs \n"
    assert appconfig.read()["deviceRemoteProvider"] == "~/pool/provider.mjs"
    assert store.device_remote_provider == "~/pool/provider.mjs"
    store.device_remote_provider = "   "
    assert "deviceRemoteProvider" not in appconfig.read()
    assert store.device_remote_provider == ""


def test_a_headless_run_never_writes_the_provider(monkeypatch):
    from diplomat_app.store import Store

    monkeypatch.setenv("DIPLOMAT_RENDER", "devices-screen-remote")
    store = Store()
    store.device_remote_provider = "~/pool/provider.mjs"
    assert store.device_remote_provider == "~/pool/provider.mjs"
    assert not appconfig.path().exists()


#: What ``install.js --start-daemon`` prints: no ``installed`` key.
START_REPLY = {"action": "start-daemon", "mcpRegistered": True, "skillInstalled": True,
               "ruleInstalled": True, "claudeMdInjected": True, "daemonRunning": True,
               "version": "0.1.0", "drift": [], "outdated": False}
INSTALLED = {**START_REPLY, "action": "check", "daemonRunning": False, "installed": True}


@pytest.mark.parametrize("started", [True, False])
def test_starting_the_daemon_keeps_the_install_status(app, monkeypatch, started):
    from diplomat_app.store import Store

    monkeypatch.setattr(da, "start_daemon", lambda: {**START_REPLY, "daemonRunning": started})
    store = Store()
    store.allocator_install = INSTALLED
    store.start_allocator_daemon_async()
    assert store.wait_for_background(5) == []
    assert store.allocator_install["installed"] is True
    assert store.allocator_install["daemonRunning"] is started
    assert activity.read()[0].detail == \
        f"Started the device-allocator daemon (ok: {'true' if started else 'false'})"


def test_a_reply_that_reports_the_install_status_is_taken_as_it_says():
    assert da.with_install_status({**START_REPLY, "installed": False}, INSTALLED)["installed"] \
        is False
    assert da.with_install_status(START_REPLY, None) == START_REPLY
    assert da.with_install_status(None, INSTALLED) is None


def test_a_restart_shells_the_installer_and_reports_what_happened(app, monkeypatch):
    from diplomat_app.store import Store

    shelled = []
    replies = iter([
        {**START_REPLY, "action": "restart-daemon", "installed": True},
        {**START_REPLY, "action": "restart-daemon", "installed": True, "daemonRunning": False,
         "error": "the running daemon (pid 9) did not exit within 60 s"},
    ])
    monkeypatch.setattr(da, "_run_installer", lambda arg: shelled.append(arg) or next(replies))
    store = Store()
    for _ in range(2):
        store.restart_allocator_daemon_async()
        assert store.wait_for_background(5) == []
    assert shelled == ["--restart-daemon"] * 2
    assert [e.detail for e in activity.read()][:2] == [
        "Restarted the device-allocator daemon (ok: false): the running daemon (pid 9) did "
        "not exit within 60 s",
        "Restarted the device-allocator daemon (ok: true)"]


def test_a_failed_repaired_shows_on_the_screens_recent_card(app, monkeypatch):
    from diplomat_app.devicesview import allocator_events
    from diplomat_app.store import Store

    monkeypatch.setattr(da, "mark_repaired", lambda key: False)
    store = Store()
    store.mark_device_repaired_async("appletv:ATV1")
    assert store.wait_for_background(5) == []
    assert [e.detail for e in allocator_events(activity.read())] == \
        ["Repaired FAILED for device appletv:ATV1"]


@pytest.mark.parametrize("outcome, line", [
    ({"installed": True}, "Installed device allocator (ok: true)"),
    ({"installed": False}, "Installed device allocator (ok: false)"),
    (None, "Installed device allocator (ok: false)"),
])
def test_an_install_is_logged_with_what_really_happened(app, monkeypatch, outcome, line):
    from diplomat_app.devicesview import allocator_events
    from diplomat_app.store import Store

    monkeypatch.setattr(da, "ensure_deps", lambda: True)
    monkeypatch.setattr(da, "install", lambda: outcome)
    store = Store()
    store.install_allocator_async()
    assert store.wait_for_background(5) == []
    assert [e.detail for e in allocator_events(activity.read())] == [line]


@pytest.mark.parametrize("outcome, line", [
    ({"installed": False}, "Uninstalled device allocator (ok: true)"),
    ({"installed": True}, "Uninstalled device allocator (ok: false)"),
    (None, "Uninstalled device allocator (ok: false)"),
])
def test_an_uninstall_is_logged_with_what_really_happened(app, monkeypatch, outcome, line):
    from diplomat_app.devicesview import allocator_events
    from diplomat_app.store import Store

    monkeypatch.setattr(da, "uninstall", lambda: outcome)
    store = Store()
    store.uninstall_allocator_async()
    assert store.wait_for_background(5) == []
    assert [e.detail for e in allocator_events(activity.read())] == [line]


# MARK: the screen

def _texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    labels = widget.findChildren(QLabel)
    return [w.text() for w in labels] + [w._full for w in labels if hasattr(w, "_full")]


def _buttons(widget) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in widget.findChildren(QPushButton) if b.isVisibleTo(widget)]


@pytest.fixture
def screen(app, monkeypatch):
    """The screen as a render seeds it, for one of the four render modes."""
    from diplomat_app import render
    from diplomat_app.devicesview import DevicesView
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    built = []

    def build(mode: str) -> DevicesView:
        monkeypatch.setenv("DIPLOMAT_RENDER", mode)
        store = Store()
        render._devices_screen_fixture(store, mode)
        view = DevicesView(store)
        view.resize(1060, 1400)
        built.append(view)
        return view

    yield build
    for view in built:
        view.deleteLater()


def test_the_screen_against_a_daemon_without_a_remote_pool(screen):
    view = screen("devices-screen")
    texts = _texts(view)
    assert "No remote pool configured." in texts
    assert view._remote_pill.text() == "not available"
    assert any(t.startswith("pid 51734 · ") for t in texts)
    assert not view.sign_in.isEnabled() and not view.list_pool.isEnabled()
    assert "Start daemon" not in _buttons(view)
    # The agent reaper's kill-device line is not a device event.
    assert "Freed iPad Pro from checkout flow" in texts
    assert "closed #431's window — finished" not in texts


def test_the_screen_with_a_pool_signing_in(screen):
    view = screen("devices-screen-remote")
    texts = _texts(view)
    assert view._remote_pill.text() == "signing in…"
    assert "⟳ Waiting for the browser sign-in (up to 10 min)…" in texts
    assert any(t.startswith("Pool: 3 devices · ") for t in texts)
    assert "LEASED HERE · 2" in texts
    assert texts.count("remote · lab-pool") == 2
    assert not view.sign_in.isEnabled()  # one sign-in at a time
    assert view.list_pool.isEnabled()
    assert _buttons(view).count("Return") == 2
    assert "Open the sign-in page" in " ".join(texts)


def test_the_screen_with_a_sign_in_nobody_answered(screen):
    view = screen("devices-screen-remote-away")
    texts = _texts(view)
    assert view._remote_pill.text() == "☾ away"
    assert any(t.startswith("☾ Sign-in went unanswered: treated as away, no prompt until ")
               for t in texts)
    assert {"queue", "3", "region", "eu-west"} <= set(texts)
    assert any(t.startswith("Sign-in: nobody finished the sign-in in the browser · ")
               for t in texts)
    assert view.sign_in.isEnabled()


def test_the_screen_with_the_daemon_off_and_no_snapshot(screen):
    view = screen("devices-screen-off")
    texts = _texts(view)
    assert "No snapshot yet: the allocator daemon has not run on this machine." in texts
    assert "No snapshot from the allocator yet, so no remote status either." in texts
    assert view._remote_pill.text() == "unknown"
    assert view._light.text() == "daemon not running"
    assert "Start daemon" in _buttons(view)
    assert "Restart daemon" not in _buttons(view)


def test_a_running_daemon_can_be_restarted_from_the_allocator_card(screen):
    view = screen("devices-screen")
    controls = view.allocator_controls
    assert controls.restart.isVisibleTo(view) and not controls.start.isVisibleTo(view)
    assert "leases" in controls.restart.toolTip()


def test_a_daemon_predating_the_pool_is_offered_a_restart_where_the_screen_says_so(
        screen, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from diplomat_app import devicesview
    from diplomat_app.store import Store

    restarts = []
    monkeypatch.setattr(Store, "restart_allocator_daemon_async",
                        lambda self: restarts.append(self))
    view = screen("devices-screen")
    view.store.device_remote_provider = "~/pool/provider.mjs"
    view.rebuild()
    assert devicesview.PREDATES_POOL_NOTE in _texts(view)
    assert "reinstall" not in devicesview.PREDATES_POOL_NOTE
    in_note = [b for b in view.findChildren(QPushButton)
               if b.text() == "Restart daemon" and b is not view.allocator_controls.restart]
    assert len(in_note) == 1
    in_note[0].click()
    assert restarts == [view.store]


def test_text_from_the_daemon_and_the_provider_is_never_read_as_html(screen):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    from diplomat_app.store import RemoteCall

    view = screen("devices-screen-remote-away")
    store = view.store
    state = store.device_state
    tag = "<table>"
    state["devices"][0] = {**state["devices"][0], "name": f"iPhone {tag}", "owner": {
        "agentName": f"fix the {tag} layout", "ownerPid": 7}}
    state["remote"] = da.RemoteStatus(
        state=f"weird{tag}", provider=f"pool{tag}", reason=f"HTTP 502: <html>{tag}</html>",
        url="https://h/a?b=<i>", extras=((f"k{tag}", f"v{tag}"),))
    store.remote_listing = RemoteCall(reply=da.RemoteReply(devices=(da.RemotePoolDevice(
        id="x", name=f"S24 {tag}", state=f"leased{tag}"),)), at=0.0)
    store.audit_entries = [activity.AuditEntry("2026-07-03T09:14:00Z", "panel",
                                               "release-device", f"Freed it from x {tag}")]
    view.rebuild()
    labels = [lb for lb in view.findChildren(QLabel)
              if any(t in lb.text() for t in (tag, "<i>", "&lt;i&gt;"))]
    shown = {lb.text() for lb in labels if lb.textFormat() == Qt.TextFormat.PlainText}
    assert {f"● fix the {tag} layout", f"⚠ HTTP 502: <html>{tag}</html>", f"k{tag}",
            f"v{tag}", f"pool{tag}", f"S24 {tag}", f"leased{tag}", f"iPhone {tag}",
            f"Freed it from x {tag}", f"weird{tag}"} <= shown, shown
    # The one label that is rich text on purpose, the link, carries the text escaped.
    rich = [lb for lb in labels if lb.textFormat() != Qt.TextFormat.PlainText]
    assert [lb.text() for lb in rich] == \
        ['<a href="https://h/a?b=&lt;i&gt;">https://h/a?b=&lt;i&gt;</a>']
    assert view._remote_pill.textFormat() == Qt.TextFormat.PlainText


def test_an_agents_name_in_the_free_tooltip_is_never_read_as_html(app):
    from PySide6.QtGui import QTextDocument
    from diplomat_app.devicesview import DeviceScreenRow
    from diplomat_app.store import Store

    row = DeviceScreenRow({"key": "k", "platform": "ios", "status": "ready",
                           "owner": {"agentName": "fix the <table> layout", "ownerPid": 3}},
                          provider=None, idle_limit=30, store=Store())
    shown = QTextDocument()
    shown.setHtml(row.actions["Free"].toolTip())
    assert shown.toPlainText().startswith("Take it back from fix the <table> layout. ")


@pytest.mark.parametrize("mode", ["devices-screen", "devices-screen-off",
                                  "devices-screen-remote", "devices-screen-remote-away"])
def test_no_render_shows_the_operators_own_provider(app, monkeypatch, mode):
    from diplomat_app import render
    from diplomat_app.store import Store

    appconfig.set_value(appconfig.DEVICE_REMOTE_PROVIDER, "~/operator/own-pool.mjs")
    monkeypatch.setenv("DIPLOMAT_RENDER", mode)
    store = Store()
    render._devices_screen_fixture(store, mode)
    assert "operator" not in store.device_remote_provider


def test_with_no_daemon_running_the_remote_card_offers_a_start_not_a_restart(
        screen, monkeypatch):
    from diplomat_app import devicesview
    from diplomat_app.store import Store

    starts = []
    monkeypatch.setattr(Store, "start_allocator_daemon_async", lambda self: starts.append(self))
    view = screen("devices-screen")
    store = view.store
    store.device_remote_provider = "~/pool/provider.mjs"
    store.allocator_install = {**store.allocator_install, "daemonRunning": False}
    store.allocator_changed.emit()
    texts = _texts(view)
    assert "Restart daemon" not in _buttons(view)
    assert not any("running allocator daemon" in t for t in texts)
    assert devicesview.STOPPED_PREDATES_POOL_NOTE in texts
    in_note = [b for b in view.findChildren(devicesview.QPushButton)
               if b.text() == "Start daemon" and b is not view.allocator_controls.start]
    assert len(in_note) == 1
    in_note[0].click()
    assert starts == [store]

    store.allocator_install = None
    store.allocator_changed.emit()
    assert devicesview.SNAPSHOT_PREDATES_POOL_NOTE in _texts(view)
    assert not {"Start daemon", "Restart daemon"} & set(_buttons(view))


@pytest.mark.parametrize("running", [False, True])
def test_an_allocator_that_is_not_installed_is_offered_no_start_or_restart_by_the_remote_card(
        screen, running):
    """As the ALLOCATOR card offers neither: a start after an uninstall would run a
    daemon for an allocator the operator removed."""
    from diplomat_app import devicesview

    view = screen("devices-screen")
    store = view.store
    store.device_remote_provider = "~/pool/provider.mjs"
    store.allocator_install = {**store.allocator_install, "installed": False,
                               "daemonRunning": running}
    store.allocator_changed.emit()
    assert not {"Start daemon", "Restart daemon"} & set(_buttons(view))
    assert devicesview.UNINSTALLED_PREDATES_POOL_NOTE in _texts(view)


def _installer(monkeypatch, gates: dict):
    """Stand in for ``install.js``: each argument's reply, held until its gate (if
    any) is set, and the arguments in the order they were asked."""
    asked = []
    replies = {
        "--restart-daemon": {**START_REPLY, "action": "restart-daemon", "installed": True},
        "--check": {**INSTALLED, "daemonRunning": False},
        "--start-daemon": START_REPLY,
    }

    def run(arg):
        asked.append(arg)
        gate = gates.get(arg)
        if gate is not None:
            assert gate.wait(5)
        return replies[arg]
    monkeypatch.setattr(da, "_run_installer", run)
    return asked


def test_a_restart_in_flight_holds_the_poll_and_every_other_allocator_action(
        app, monkeypatch):
    import threading
    from diplomat_app.devicesview import AllocatorControls
    from diplomat_app.store import Store

    release = threading.Event()
    asked = _installer(monkeypatch, {"--restart-daemon": release})
    store = Store()
    store.allocator_install = {**INSTALLED, "daemonRunning": True}
    controls = AllocatorControls(store, offers_daemon=True)
    store.restart_allocator_daemon_async()
    assert store.allocator_busy
    controls.refresh(store.allocator_install)
    assert not any(b.isEnabled() for b in (controls.install, controls.uninstall,
                                           controls.restart, controls.recheck))
    store.refresh_allocator_install_async()
    store.start_allocator_daemon_async()
    store.install_allocator_async()
    store.uninstall_allocator_async()
    release.set()
    assert store.wait_for_background(5) == []
    assert asked == ["--restart-daemon"]
    assert not store.allocator_busy
    assert store.allocator_install["daemonRunning"] is True


def test_a_check_that_a_restart_overlapped_is_dropped(app, monkeypatch):
    import threading
    from diplomat_app.store import Store

    release = threading.Event()
    asked = _installer(monkeypatch, {"--check": release})
    store = Store()
    store.refresh_allocator_install_async()
    for _ in range(50):
        if asked:
            break
        time.sleep(0.02)
    store.restart_allocator_daemon_async()
    for _ in range(100):
        if not store.allocator_busy:
            break
        time.sleep(0.02)
    release.set()
    assert store.wait_for_background(5) == []
    assert asked == ["--check", "--restart-daemon"]
    assert store.allocator_install["daemonRunning"] is True


def test_a_provider_set_by_hand_shows_on_the_screens_next_poll(app, monkeypatch):
    from diplomat_app.devicesview import DevicesView
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    view = DevicesView(Store(), live=False)
    assert view.provider_field.text() == ""
    appconfig.set_value(appconfig.DEVICE_REMOTE_PROVIDER, "~/by/hand.mjs")
    view._install_timer.timeout.emit()
    assert view.provider_field.text() == "~/by/hand.mjs"


def test_idle_limits_set_by_hand_show_on_the_screens_next_poll_and_in_settings(
        app, monkeypatch):
    from diplomat_app.devicesview import DevicesView
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    store = Store()
    panel = Panel(store)
    view = DevicesView(store, live=False)
    settings = panel.settings_view

    def sliders():
        return ([s.value() for s, _row in view._idle],
                [settings._idle_free.value(), settings._idle_leased.value()])

    assert sliders() == ([5, 30], [5, 30])
    appconfig.set_int(appconfig.DEVICE_IDLE_FREE_MINUTES, 12)
    appconfig.set_int(appconfig.DEVICE_IDLE_LEASED_MINUTES, 90)
    view._install_timer.timeout.emit()
    assert sliders() == ([12, 90], [12, 90])
    assert view._drawn_limit == 90

    appconfig.set_int(appconfig.DEVICE_IDLE_LEASED_MINUTES, 120)
    panel._set_screen("settings")
    assert sliders()[1] == [12, 120]
    panel.deleteLater()
    view.deleteLater()


def test_idle_limits_set_by_hand_show_as_the_screen_opens(app, monkeypatch):
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    panel = Panel(Store())
    view = panel.devices_view
    assert [s.value() for s, _row in view._idle] == [5, 30]
    appconfig.set_int(appconfig.DEVICE_IDLE_LEASED_MINUTES, 90)
    panel._set_screen("devices")
    assert [s.value() for s, _row in view._idle] == [5, 90]
    view.closed()
    panel.deleteLater()


def test_a_rows_actions_are_only_what_the_daemon_does_for_it(app):
    from diplomat_app.devicesview import DeviceScreenRow
    from diplomat_app.store import Store

    store = Store()

    def actions(dev):
        return list(DeviceScreenRow(dev, provider=None, idle_limit=30, store=store).actions)

    held = {"key": "k", "platform": "ios", "status": "ready", "owner": {"ownerPid": 3}}
    assert actions(held) == ["Free", "Kill"]
    assert actions({**held, "source": "remote"}) == ["Return"]
    assert actions({"key": "k", "platform": "ios", "status": "repairing",
                    "owner": {"ownerPid": None}}) == ["Repaired", "Kill"]
    assert actions({"key": "k", "platform": "ios", "status": "free"}) == []


def test_free_reaches_the_daemon_with_the_rows_holder(app, monkeypatch):
    from diplomat_app.devicesview import DeviceScreenRow
    from diplomat_app.store import Store

    got = []
    monkeypatch.setattr(da, "release_device", lambda key, pid: got.append((key, pid)) or True)
    store = Store()
    row = DeviceScreenRow({"key": "ios:A", "platform": "ios", "status": "ready",
                           "owner": {"agentName": "e2e", "ownerPid": 42}},
                          provider=None, idle_limit=30, store=store)
    row.actions["Free"].click()
    assert store.wait_for_background(5) == []
    assert got == [("ios:A", 42)]


def test_idle_time_is_coloured_toward_the_configured_leased_limit(app):
    assert da.idle_color(29, 30) == "#FF3B30"
    assert da.idle_color(20, 30) == "#FF9500"
    assert da.idle_color(19, 30) is None
    assert da.idle_color(500, 0) is None
    # The thresholds scale with the limit: 14 minutes is quiet under a long one.
    assert da.idle_color(14, 240) is None
    assert da.idle_minutes({"idleMs": 59_999}) is None
    assert da.idle_minutes({"idleMs": 60_000}) == 1


def test_the_provider_field_commits_on_apply_and_clear_removes_it(app, monkeypatch):
    from diplomat_app.devicesview import DevicesView
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    view = DevicesView(Store(), live=False)
    assert not view.provider_apply.isEnabled()
    view.provider_field.setText("~/p.mjs")
    view.provider_field.textEdited.emit("~/p.mjs")
    assert view.provider_apply.isEnabled()
    assert "deviceRemoteProvider" not in appconfig.read()  # not per keystroke
    view.provider_apply.click()
    assert appconfig.read()["deviceRemoteProvider"] == "~/p.mjs"
    assert not view.provider_apply.isEnabled()
    assert "⚠ No file at " in view._provider_row.summary()
    view.provider_clear.click()
    assert "deviceRemoteProvider" not in appconfig.read()
    assert view.provider_field.text() == ""


def test_the_panel_opens_the_screen_from_its_header_and_the_device_section(app, monkeypatch):
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    store = Store()
    panel = Panel(store)
    store.device_state = {"devices": [
        {"key": "a", "platform": "ios", "status": "ready", "owner": {"ownerPid": 1}},
        {"key": "b", "platform": "ios", "status": "free"}], "daemonPid": 1, "remote": None}
    store.devices_changed.emit()
    assert panel.devices_btn.badge_text() == "1"
    panel.devices_btn.click()
    assert panel.body.currentIndex() == panel._screens["devices"]
    panel.devices_btn.click()
    assert panel.body.currentIndex() == panel._screens["main"]
    panel.devices_col.itemAt(0).widget().clicked.emit()
    assert panel.body.currentIndex() == panel._screens["devices"]
    panel.deleteLater()


@pytest.mark.parametrize("mode", ["devices-screen", "devices-screen-remote",
                                  "devices-screen-remote-away", "devices-screen-off"])
def test_each_mode_renders_through_the_real_entry(app, monkeypatch, tmp_path, mode):
    from diplomat_app import render

    monkeypatch.setenv("DIPLOMAT_RENDER", mode)
    monkeypatch.setattr(render, "_agents_scratch_dir", None)
    out = tmp_path / f"{mode}.png"
    assert render.run(mode, str(out)) == 0
    assert out.stat().st_size > 10_000
    assert not appconfig.path().exists()
