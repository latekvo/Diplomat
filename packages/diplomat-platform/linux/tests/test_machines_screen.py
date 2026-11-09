"""The Machines screen and what it stands on: the remote-machines snapshot and history
as the daemon writes them, the exclusions the screen writes to the shared config, and
the one daemon route it calls.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from diplomat_runtime import activity, appconfig
from diplomat_app import deviceallocator as da
from diplomat_app import remotemachines as rm

pytest.importorskip("PySide6")

NOW = 1_793_000_000.0
GB = 2 ** 30


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def raw_machine(target="radoniusz", **extra) -> dict:
    m = {
        "id": f"id-{target}", "target": target, "aliases": [f"{target}.local"],
        "hostname": f"{target}.local", "user": "ci", "port": 22,
        "sources": ["ssh-config", "mdns"], "status": "ok", "machineName": target,
        "cores": 10, "sshSessions": 2,
        "load": {"last1m": 2.5, "last5m": 2.0, "last15m": 1.0, "sinceLastScan": None},
        "memory": {"totalBytes": 16 * GB, "availableBytes": 4 * GB, "pressure": "warning"},
        "disk": {"totalBytes": 500 * GB, "freeBytes": 120 * GB},
        "gpus": [{"name": "Apple M3 Pro (14-core)"}, {"vramBytes": 3}],
        "os": {"name": "macOS 27.0", "kernel": "25.0", "arch": "arm64"},
        "cpu": {"model": "Apple M3 Pro", "perfCores": 5},
        "probedAt": "2026-11-09T12:00:00.000Z", "capabilitiesAt": None,
        "excluded": None, "spareCores": 7.5, "usage": {"cpu": 0.25, "memory": 0.75},
        "warnings": ["memory pressure warning"],
    }
    m.update(extra)
    return m


def snapshot(**extra) -> dict:
    s = {"updatedAt": "2026-11-09T12:00:05.000Z", "discoveredAt": "2026-11-09T11:58:00.000Z",
         "probeIntervalMs": 60_000,
         "daemon": {"pid": 812, "startedAt": "2026-11-09T10:00:00.000Z", "sessions": 2},
         "local": {"id": "me", "status": "ok", "machineName": "mbp", "cores": 8,
                   "usage": {"cpu": 0.5, "memory": 0.5}, "warnings": []},
         "machines": [raw_machine()], "excluded": [], "unavailable": []}
    s.update(extra)
    return s


# MARK: the snapshot

def test_a_machine_is_read_with_every_field_the_panel_shows():
    m = rm.decode_machine(raw_machine())
    assert m == rm.Machine(
        id="id-radoniusz", status="ok", target="radoniusz", aliases=("radoniusz.local",),
        hostname="radoniusz.local", user="ci", port=22, sources=("ssh-config", "mdns"),
        machine_name="radoniusz", cores=10, ssh_sessions=2, load_1m=2.5,
        memory_total=16 * GB, memory_available=4 * GB, pressure="warning",
        disk_free=120 * GB, gpus=("Apple M3 Pro (14-core)",), os_name="macOS 27.0",
        arch="arm64", cpu_model="Apple M3 Pro", probed_at=1_794_225_600.0,
        spare_cores=7.5, usage_cpu=0.25, usage_memory=0.75,
        warnings=("memory pressure warning",))
    assert m.names == ("radoniusz", "radoniusz.local")


def test_an_exclusion_is_for_good_without_a_time_and_a_pause_with_one():
    good = rm.decode_machine(raw_machine(status="excluded", excluded={"until": None}))
    paused = rm.decode_machine(raw_machine(excluded={"until": "2026-11-09T13:00:00.000Z"}))
    pool = rm.decode_machine(raw_machine())
    assert (good.excluded_for_good, good.paused_until) == (True, None)
    assert (paused.excluded_for_good, paused.paused_until) == (False, 1_794_229_200.0)
    assert (pool.excluded_for_good, pool.paused_until) == (False, None)


def test_an_exclusion_is_named_by_the_entries_it_came_from_too():
    m = rm.decode_machine(raw_machine(status="excluded",
                                      excluded={"until": None, "by": ["10.0.0.21", 7]}))
    assert m.excluded_by == ("10.0.0.21",)
    assert m.names == ("radoniusz", "radoniusz.local", "10.0.0.21")
    assert rm.decode_machine(raw_machine(excluded={"until": None})).excluded_by == ()


def test_an_older_daemons_snapshot_has_no_daemon_and_nothing_excluded():
    raw = snapshot()
    del raw["daemon"], raw["excluded"]
    s = rm.decode_state(raw)
    assert s.daemon is None and s.excluded == ()
    assert [m.target for m in s.machines] == ["radoniusz"]
    assert rm.decode_state(snapshot()).daemon == rm.Daemon(
        pid=812, started_at=1_794_218_400.0, sessions=2)


@pytest.mark.parametrize("raw", [None, [], {"machines": "x"}, {"local": {"status": "ok"}}])
def test_without_a_machine_list_there_is_no_snapshot(raw):
    assert rm.decode_state(raw) is None


def test_an_unreadable_row_costs_only_itself():
    s = rm.decode_state(snapshot(machines=[
        "junk", {"target": "no-status"}, raw_machine("ok-one", gpus="x", cores="8",
                                                     usage={"cpu": True})]))
    assert [m.target for m in s.machines] == ["ok-one"]
    m = s.machines[0]
    assert (m.gpus, m.cores, m.usage_cpu) == ((), None, None)


def test_the_files_and_the_socket_are_under_rm_base_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RM_BASE_DIR", str(tmp_path))
    (tmp_path / "state.json").write_text(json.dumps(snapshot()))
    (tmp_path / "history.json").write_text(json.dumps(
        {"version": 1, "intervalMs": 30_000,
         "machines": {"a": [[1000, 0.5, None, 2], [2000, 0.25], ["x", 1, 1, 1]], "b": "x"}}))
    assert [m.target for m in rm.read_state().machines] == ["radoniusz"]
    assert rm.read_history() == rm.History(
        interval_ms=30_000, machines={"a": ((1.0, 0.5, None, 2.0), (2.0, 0.25, None, None))})
    assert rm.socket_path() == tmp_path / "daemon.sock"


# MARK: the history graph

def rows(*spec) -> tuple:
    return tuple((at, cpu, mem, ssh) for at, cpu, mem, ssh in spec)


def test_a_silence_longer_than_three_intervals_breaks_the_lines():
    history = rows((0, 0.1, 0.1, 0), (60, 0.2, 0.2, 0), (240, 0.3, 0.3, 1),
                   (421, 0.4, 0.4, 1))
    runs = rm.chart_runs(history, start=0, end=421, columns=1000, interval_ms=60_000)
    # 180 s is three intervals and is drawn through; 181 s is a gap.
    assert [[r[0] for r in run] for run in runs] == [[0, 60, 240], [421]]


def test_readings_in_one_pixel_column_are_one_point_mean_and_max():
    history = rows((0, 0.2, 0.5, 1), (10, 0.4, None, 3), (20, None, 0.7, None),
                   (100, 0.9, 0.9, 0))
    runs = rm.chart_runs(history, start=0, end=100, columns=2, interval_ms=60_000)
    assert runs == [[(10.0, pytest.approx(0.3), pytest.approx(0.6), 3), (100.0, 0.9, 0.9, 0)]]


def test_the_graph_spans_at_least_an_hour():
    assert rm.chart_start(rows((NOW - 600, 0, 0, 0)), NOW) == NOW - 3600
    assert rm.chart_start(rows((NOW - 7200, 0, 0, 0)), NOW) == NOW - 7200
    assert rm.chart_start((), NOW) == NOW - 3600


# MARK: how a machine reads

@pytest.mark.parametrize("value, warn_above, color", [
    (1.01, 1.0, rm.RED), (1.0, 1.0, rm.AMBER), (0.75, 1.0, rm.AMBER), (0.74, 1.0, rm.GREEN),
    (0.91, 0.9, rm.RED), (0.9, 0.9, rm.AMBER),
])
def test_a_bar_is_red_where_the_daemon_warns_and_amber_from_three_quarters(
        value, warn_above, color):
    assert rm.usage_color(value, warn_above) == color


def test_the_bar_texts():
    m = rm.decode_machine(raw_machine())
    assert rm.cpu_text(m) == "load 2.5 on 10 cores · 7.5 spare"
    assert rm.memory_text(m) == "12 of 16 GB used · pressure warning"
    normal = rm.decode_machine(raw_machine(memory={
        "totalBytes": 64 * GB, "availableBytes": 60.5 * GB, "pressure": "normal"}))
    assert rm.memory_text(normal) == "3.5 of 64 GB used"
    assert rm.specs(m) == ("macOS 27.0 arm64 · Apple M3 Pro · 10 cores · 16 GB RAM · "
                           "Apple M3 Pro (14-core)")
    assert rm.address(m) == "ci@radoniusz.local"
    assert rm.address(rm.decode_machine(raw_machine(port=2222))) == "ci@radoniusz.local:2222"


def test_the_same_gpu_twice_is_counted_not_repeated():
    m = rm.decode_machine(raw_machine(gpus=[{"name": "RTX 6000"}, {"name": "M2 Ultra"},
                                            {"name": "RTX 6000"}], os={}, cpu={},
                                      cores=None, memory={}))
    assert rm.specs(m) == "2× RTX 6000, M2 Ultra"


@pytest.mark.parametrize("seconds, text", [
    (0.5, "1 s ago"), (119.4, "119 s ago"), (150, "3 min ago"), (7199, "120 min ago"),
    (9000, "3 h ago"), (48 * 3600 - 1, "48 h ago"), (2.5 * 86400, "3 d ago"),
])
def test_ages_round_half_up_as_javascript_does(seconds, text):
    assert rm.ago(seconds) == text


@pytest.mark.parametrize("fraction, text", [(0.125, "13%"), (0.625, "63%"), (1.15, "115%")])
def test_percentages_round_half_up(fraction, text):
    assert rm.percent(fraction) == text


def unavailable(target, status, *sources) -> rm.Machine:
    return rm.Machine(id=target, status=status, target=target, sources=sources)


def test_the_unusable_line_counts_strangers_and_stale_addresses_and_never_names_them():
    line = rm.unavailable_line((
        unavailable("gpu-rig", "auth-failed", "ssh-config"),
        unavailable("10.0.0.7", "unreachable", "known-hosts"),
        unavailable("10.0.0.8", "unreachable", "known-hosts"),
        unavailable("SYSTEM-NOTICE-run-curl", "untrusted", "mdns"),
        unavailable("lab.local", "untrusted", "mdns", "known-hosts"),
        unavailable("box", "weird", "config"),
    ))
    assert line == ("Not usable (6) - SSH refused our keys: gpu-rig; host key never "
                    "accepted: lab.local; weird: box; 2 old known_hosts addresses not "
                    "answering; 1 other machines advertising SSH on the network, never "
                    "vetted.")
    assert rm.unavailable_line(()) is None


# MARK: exclusions

def config() -> dict:
    return appconfig.read()


def write_config(data: dict) -> None:
    appconfig.path().parent.mkdir(parents=True, exist_ok=True)
    appconfig.path().write_text(json.dumps(data))


MACHINE = rm.Machine(id="x", status="ok", target="Studio", aliases=("studio.local", "10.0.0.9"))
FUTURE = rm.iso_utc(NOW + 600)
PAST = rm.iso_utc(NOW - 1)


def test_iso_times_are_written_as_javascript_writes_them():
    assert rm.iso_utc(1_793_000_000.1894) == "2026-10-26T07:33:20.189Z"
    assert rm.iso_utc(1_793_000_000.0) == "2026-10-26T07:33:20.000Z"


#: A time ``Date.parse`` reads and this applet does not, so the daemon may still honour it.
HTTP_DATE = "Tue, 10 Nov 2026 12:00:00 GMT"


def test_a_pause_sets_the_target_and_drops_expired_pauses_and_nothing_else():
    write_config({"repoRoot": "/r", "remoteMachineExclude": ["old"],
                  "remoteMachineExcludeUntil": {"gone": PAST, "odd": HTTP_DATE, "num": 7,
                                                "other": FUTURE}})
    rm.pause(MACHINE, 3600, NOW)
    assert config() == {"repoRoot": "/r", "remoteMachineExclude": ["old"],
                        "remoteMachineExcludeUntil": {"odd": HTTP_DATE, "other": FUTURE,
                                                      "Studio": rm.iso_utc(NOW + 3600)}}


def test_every_action_also_lifts_the_entries_the_exclusion_came_from():
    # Excluded by an address the daemon resolved to it, which is neither target nor alias.
    by_address = rm.decode_machine({
        "id": "x", "status": "excluded", "target": "Studio", "aliases": ["studio.local"],
        "excluded": {"until": None, "by": ["10.0.0.21"]}})
    write_config({"remoteMachineExclude": ["10.0.0.21", "keep"],
                  "remoteMachineExcludeUntil": {"10.0.0.21": FUTURE, "keep": FUTURE}})
    rm.resume(by_address, NOW)
    assert config()["remoteMachineExcludeUntil"] == {"keep": FUTURE}
    write_config({"remoteMachineExcludeUntil": {"10.0.0.21": FUTURE, "keep": FUTURE}})
    rm.exclude(by_address, NOW)
    assert config() == {"remoteMachineExclude": ["Studio"],
                        "remoteMachineExcludeUntil": {"keep": FUTURE}}
    write_config({"remoteMachineExclude": ["10.0.0.21", "keep"],
                  "remoteMachineExcludeUntil": {"10.0.0.21": FUTURE, "keep": FUTURE}})
    rm.include(by_address, NOW)
    assert config() == {"remoteMachineExclude": ["keep"],
                        "remoteMachineExcludeUntil": {"keep": FUTURE}}


def test_an_include_writes_the_list_before_the_pauses(monkeypatch):
    written = []
    monkeypatch.setattr(appconfig, "set_list", lambda key, v: written.append(key))
    monkeypatch.setattr(appconfig, "set_dict", lambda key, v: written.append(key))
    rm.include(MACHINE, NOW)
    assert written == [appconfig.REMOTE_MACHINE_EXCLUDE, appconfig.REMOTE_MACHINE_EXCLUDE_UNTIL]


def test_an_exclude_lists_the_target_once_and_ends_its_pause_by_any_name():
    write_config({"repoRoot": "/r", "remoteMachineExclude": ["old"],
                  "remoteMachineExcludeUntil": {"STUDIO.local": FUTURE, "studio": FUTURE,
                                                "other": FUTURE, "gone": PAST}})
    rm.exclude(MACHINE, NOW)
    assert config() == {"repoRoot": "/r", "remoteMachineExclude": ["old", "Studio"],
                        "remoteMachineExcludeUntil": {"other": FUTURE}}
    write_config({"remoteMachineExclude": ["studio"],
                  "remoteMachineExcludeUntil": {"10.0.0.9": FUTURE}})
    rm.exclude(MACHINE, NOW)
    assert config() == {"remoteMachineExclude": ["studio"]}


def test_a_resume_ends_the_pause_by_every_name_of_the_machine():
    write_config({"repoRoot": "/r", "remoteMachineExclude": ["studio"],
                  "remoteMachineExcludeUntil": {"Studio.Local": FUTURE, "other": FUTURE}})
    rm.resume(MACHINE, NOW)
    assert config() == {"repoRoot": "/r", "remoteMachineExclude": ["studio"],
                        "remoteMachineExcludeUntil": {"other": FUTURE}}


def test_an_include_takes_every_name_out_of_both_and_drops_keys_left_empty():
    write_config({"repoRoot": "/r", "remoteMachineExclude": ["STUDIO", "10.0.0.9", "keep", 7],
                  "remoteMachineExcludeUntil": {"studio.local": FUTURE, "gone": PAST}})
    rm.include(MACHINE, NOW)
    assert config() == {"repoRoot": "/r", "remoteMachineExclude": ["keep", 7]}
    write_config({"repoRoot": "/r", "remoteMachineExclude": ["Keep"]})
    rm.include(rm.Machine(id="k", status="excluded", target="keep"), NOW)
    assert config() == {"repoRoot": "/r"}


def test_the_store_writes_off_the_gui_thread_and_logs_what_happened(app, monkeypatch):
    from diplomat_app.machinesview import machine_events
    from diplomat_app.store import Store

    threads = []
    real = rm.pause

    def pause(m, seconds, now):
        threads.append(threading.current_thread() is threading.main_thread())
        if len(threads) == 2:
            raise PermissionError(13, "Permission denied")
        real(m, seconds, now)

    monkeypatch.setattr(rm, "pause", pause)
    store = Store()
    for _ in range(2):
        store.pause_machine_async(MACHINE, 3600)
        assert store.wait_for_background(5) == []
    assert threads == [False, False]
    assert "Studio" in config()["remoteMachineExcludeUntil"]
    details = [e.detail for e in machine_events(activity.read())]
    assert details[0].startswith("Paused Studio until ")
    assert details[0].endswith(" FAILED: config.json was not written")
    assert details[1].startswith("Paused Studio until ")
    assert "FAILED" not in details[1]


@pytest.mark.parametrize("method, line", [
    ("exclude_machine_async", "Excluded Studio from the pool"),
    ("resume_machine_async", "Resumed Studio"),
    ("include_machine_async", "Put Studio back in the pool"),
])
def test_each_exclusion_action_is_logged(app, method, line):
    from diplomat_app.store import Store

    store = Store()
    getattr(store, method)(MACHINE)
    assert store.wait_for_background(5) == []
    assert activity.read()[0].detail == line


@pytest.mark.parametrize("install, starts", [
    ({"installed": True, "daemonRunning": False}, True),
    ({"installed": True, "daemonRunning": True}, False),
    ({"installed": False, "daemonRunning": False}, False),
    (None, False),
])
def test_an_exclusion_starts_an_installed_daemon_that_is_not_running(
        app, monkeypatch, install, starts):
    from diplomat_app.store import Store

    shelled = []
    monkeypatch.setattr(da, "run_installer", lambda script, arg: shelled.append(arg) or {
        "daemonRunning": True})
    store = Store()
    store.machines_install = install
    store.exclude_machine_async(MACHINE)
    # The write's worker, then the start it hands on.
    for _ in range(2):
        assert store.wait_for_background(5) == []
    assert shelled == (["--start-daemon"] if starts else [])
    assert "Studio" in config()["remoteMachineExclude"]


# MARK: the daemon's socket and installer

class _FakeDaemon:
    """An HTTP server on a unix socket in a short /tmp dir (the path limit), answering
    each route from ``routes`` and recording what it was sent."""

    def __init__(self, routes):
        self.dir = tempfile.mkdtemp(prefix="rm.", dir="/tmp")
        self.path = os.path.join(self.dir, "daemon.sock")
        self.got: list[tuple[str, object]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.got.append((self.path, body))
                status, answer = routes.get(self.path, (404, {"error": "unknown route"}))
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
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def daemon(monkeypatch):
    made = []

    def make(routes):
        d = _FakeDaemon(routes)
        monkeypatch.setenv("RM_BASE_DIR", d.dir)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


RUNNING = {"installed": True, "daemonRunning": True, "version": "0.1.0"}


@pytest.mark.parametrize("install", [RUNNING, {**RUNNING, "daemonRunning": False}])
@pytest.mark.parametrize("status, answer, line", [
    (200, {}, "Rescanned the remote machines"),
    (500, {"error": "scan crashed"}, "Rescanned the remote machines FAILED: scan crashed"),
    (500, {}, "Rescanned the remote machines FAILED: HTTP 500"),
])
def test_refresh_asks_the_daemon_for_a_full_scan_whatever_the_last_check_said(
        app, daemon, monkeypatch, install, status, answer, line):
    """``daemonRunning`` may be 15 s old: a daemon that answers is asked, never started."""
    from diplomat_app.store import Store

    shelled = []
    monkeypatch.setattr(da, "run_installer", lambda script, arg: shelled.append(arg))
    d = daemon({"/scan": (status, answer)})
    store = Store()
    store.machines_install = install
    store.refresh_machines_async()
    assert store.wait_for_background(10) == []
    assert d.got == [("/scan", {"full": True})]
    assert shelled == []
    assert not store.machines_scanning and not store.machines_busy
    assert activity.read()[0].action == "machines-scan"
    assert activity.read()[0].detail == line


def test_a_scan_that_gets_no_reply_says_so(app, monkeypatch):
    from diplomat_app.store import Store

    asked = []
    monkeypatch.setattr(da, "exchange", lambda *a, **kw: asked.append(kw) or da.NO_REPLY)
    store = Store()
    store.refresh_machines_async()
    assert store.wait_for_background(5) == []
    assert asked == [{"timeout_secs": 120, "sock": rm.socket_path()}]
    assert activity.read()[0].detail == ("Rescanned the remote machines FAILED: the daemon "
                                         "did not answer")


def test_a_scan_holds_the_installer_gate(app, monkeypatch):
    from diplomat_app.store import Store

    release = threading.Event()
    during = []

    def exchange(*a, **kw):
        during.append((store.machines_busy, store.machines_scanning))
        # An install asked for meanwhile is refused, not run beside the scan.
        store.install_machines_async()
        release.wait(5)
        return da.DaemonReply(200, {})

    shelled = []
    monkeypatch.setattr(da, "run_installer", lambda script, arg: shelled.append(arg))
    monkeypatch.setattr(da, "exchange", exchange)
    store = Store()
    store.refresh_machines_async()
    assert store.machines_busy
    store.refresh_machines_async()
    release.set()
    assert store.wait_for_background(5) == []
    assert during == [(True, True)]
    assert shelled == []


@pytest.mark.parametrize("install", [RUNNING, {**RUNNING, "daemonRunning": False}])
def test_refresh_starts_a_daemon_that_does_not_answer(app, monkeypatch, install):
    from diplomat_app.store import Store

    shelled = []
    # As ``--start-daemon`` answers: no ``installed``.
    monkeypatch.setattr(da, "run_installer", lambda script, arg: shelled.append(
        (os.path.relpath(script, rm.package_dir()), arg)) or {"daemonRunning": True})
    store = Store()
    store.machines_install = install
    store.refresh_machines_async()
    assert store.wait_for_background(5) == []
    assert shelled == [("src/install.js", "--start-daemon")]
    assert store.machines_install == {"daemonRunning": True, "installed": True}
    assert activity.read()[0].detail == "Started the remote-machines daemon (ok: true)"


def test_the_install_controls_drive_the_remote_machines_installer(app, monkeypatch):
    from diplomat_app.store import Store

    shelled = []
    monkeypatch.setattr(da, "ensure_deps", lambda package=None: shelled.append(package))
    monkeypatch.setattr(da, "run_installer",
                        lambda script, arg: shelled.append((script, arg)) or {"installed": True})
    store = Store()
    store.install_machines_async()
    assert store.wait_for_background(5) == []
    assert shelled == [rm.package_dir(), (rm.install_js(), "--install")]
    assert rm.package_dir().endswith(os.path.join("packages", "remote-machines"))
    assert activity.read()[0].detail == "Installed remote machines (ok: true)"


def test_an_install_check_is_published_only_when_it_says_something_new(app, monkeypatch):
    from diplomat_app.store import Store

    replies = iter([RUNNING, dict(RUNNING), {**RUNNING, "daemonRunning": False}])
    monkeypatch.setattr(rm, "check", lambda: next(replies))
    store = Store()
    published = []
    store.machines_install_changed.connect(lambda: published.append(store.machines_install))
    for _ in range(3):
        store.refresh_machines_install_async()
        assert store.wait_for_background(5) == []
        app.processEvents()
    assert published == [RUNNING, {**RUNNING, "daemonRunning": False}]


def test_the_snapshot_is_published_on_change_and_the_history_reread_with_a_new_scan(
        app, tmp_path, monkeypatch):
    from diplomat_app.store import Store

    monkeypatch.setenv("RM_BASE_DIR", str(tmp_path))
    state, history = tmp_path / "state.json", tmp_path / "history.json"
    state.write_text(json.dumps(snapshot()))
    history.write_text(json.dumps({"intervalMs": 60_000, "machines": {"a": [[1000, 1, 1, 1]]}}))
    store = Store()
    published = []
    store.machines_changed.connect(lambda: published.append(store.machines_history))
    store.refresh_machines_state()
    store.refresh_machines_state()
    assert len(published) == 1
    # Read again only with the snapshot of a new scan.
    history.write_text(json.dumps({"intervalMs": 60_000, "machines": {"a": [[2000, 1, 1, 1]]}}))
    store.refresh_machines_state()
    assert len(published) == 1
    state.write_text(json.dumps(snapshot(updatedAt="2026-11-09T12:01:05.000Z")))
    store.refresh_machines_state()
    assert len(published) == 2
    assert store.machines_history.machines == {"a": ((2.0, 1.0, 1.0, 1.0),)}


# MARK: the screen

def _texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in widget.findChildren(QLabel)]


def _panels(view):
    from diplomat_app.machinesview import MachinePanel

    found = view.findChildren(MachinePanel)
    return sorted(found, key=lambda p: p.mapTo(view, p.rect().topLeft()).y())


@pytest.fixture
def screen(app, monkeypatch):
    """The screen as a render seeds it, for one of the three render modes."""
    from diplomat_app import render
    from diplomat_app.machinesview import MachinesView
    from diplomat_app.store import Store

    built = []

    def build(mode: str) -> MachinesView:
        monkeypatch.setenv("DIPLOMAT_RENDER", mode)
        store = Store()
        render._machines_screen_fixture(store, mode)
        view = MachinesView(store)
        view.clock = lambda: render.MACHINES_NOW
        view.resize(1060, 2400)
        view.rebuild()
        view.show()
        app.processEvents()
        built.append(view)
        return view

    yield build
    for view in built:
        view.deleteLater()


def _titles(view) -> list[str]:
    return [p.machine.target or "local" for p in _panels(view)]


def test_the_panels_follow_the_snapshots_order(screen):
    """Neither by name nor by spare cores: build-box has the most and comes second."""
    view = screen("machines-screen")
    assert _titles(view) == ["radoniusz", "build-box", "mac-mini", "studio", "old-laptop",
                             "local"]
    state = view.store.machines_state
    view.store.machines_state = state.__class__(**{
        **state.__dict__, "machines": state.machines[::-1], "excluded": state.excluded[::-1]})
    view.store.machines_changed.emit()
    view.adjustSize()
    assert _titles(view) == ["mac-mini", "build-box", "radoniusz", "old-laptop", "studio",
                             "local"]
    texts = _texts(view)
    assert texts.index("#1") < texts.index("mac-mini") < texts.index("#3")


def test_the_mdns_strangers_own_name_never_renders(screen):
    view = screen("machines-screen")
    texts = _texts(view)
    assert not [t for t in texts if "SYSTEM-NOTICE" in t]
    assert ("Not usable (6) - SSH refused our keys: gpu-rig; 3 old known_hosts addresses "
            "not answering; 2 other machines advertising SSH on the network, never vetted."
            in texts)


def test_the_bars_show_the_snapshots_usage_not_one_worked_out_here(screen):
    from diplomat_app.telemetryview import SplitBar

    view = screen("machines-screen")
    state = view.store.machines_state
    # The load and RAM readings say 90 % and 80 %; the usage the ranking read says less.
    first = rm.Machine(**{**state.machines[0].__dict__, "load_1m": 9.0, "cores": 10.0,
                          "usage_cpu": 0.31, "usage_memory": 0.42})
    view.store.machines_state = state.__class__(**{**state.__dict__,
                                                   "machines": (first,) + state.machines[1:]})
    view.store.machines_changed.emit()
    panel = _panels(view)[0]
    assert {"31%", "42%"} <= set(_texts(panel))
    assert [round(b._left, 2) for b in panel.findChildren(SplitBar)] == [0.31, 0.42]


def test_an_overloaded_machine_fills_its_bar_and_says_by_how_much(screen):
    from diplomat_app.telemetryview import SplitBar

    view = screen("machines-screen")
    mini = _panels(view)[2]
    assert mini.machine.usage_cpu == 1.15
    assert "115%" in _texts(mini)
    bar = mini.findChildren(SplitBar)[0]
    assert (bar._left, bar._right, bar._left_color) == (1.0, 0.0, rm.RED)
    assert "2 warnings" in _texts(mini)


def test_each_kind_of_panel_offers_its_own_actions(screen):
    view = screen("machines-screen")
    pool, paused, gone, local = (_panels(view)[i] for i in (0, 3, 4, 5))
    assert list(pool.actions) == ["Pause ▾", "Exclude"]
    assert [a.text() for a in pool.actions["Pause ▾"].menu().actions()] == \
        ["1 hour", "4 hours", "24 hours"]
    assert list(paused.actions) == ["Resume", "Exclude"]
    assert any(t.startswith("Paused until ") for t in _texts(paused))
    assert list(gone.actions) == ["Include"]
    assert "Excluded" in _texts(gone)
    assert _texts(gone).count("not read while excluded") == 2
    assert local.actions == {} and "This machine" in _texts(local)
    assert not [t for t in _texts(local) if t.startswith("#")]


def test_the_panel_actions_reach_the_store_with_their_machine(screen, monkeypatch):
    from diplomat_app.store import Store

    asked = []
    for name in ("pause_machine_async", "exclude_machine_async", "resume_machine_async",
                 "include_machine_async"):
        monkeypatch.setattr(Store, name, lambda self, m, *a, name=name: asked.append(
            (name, m.target, *a)))
    view = screen("machines-screen")
    pool, paused, gone = (_panels(view)[i] for i in (1, 3, 4))
    pool.actions["Pause ▾"].menu().actions()[1].trigger()
    pool.actions["Exclude"].click()
    paused.actions["Resume"].click()
    gone.actions["Include"].click()
    assert asked == [("pause_machine_async", "build-box", 4 * 3600),
                     ("exclude_machine_async", "build-box"),
                     ("resume_machine_async", "studio"),
                     ("include_machine_async", "old-laptop")]


def test_an_idle_daemons_snapshot_is_shown_with_its_age(screen):
    view = screen("machines-screen-idle")
    texts = _texts(view)
    assert "The daemon is idle, so this is its last snapshot, as of 47 min ago." in texts
    assert view._light.text() == "daemon idle"
    assert view.refresh.isEnabled() is (rm.package_available() and
                                        da.resolve_node() is not None)
    assert view._facts["Agent sessions"].text() == "—"


def test_a_daemon_going_idle_marks_the_snapshot_it_left(screen):
    view = screen("machines-screen")
    view.store.machines_install = {**view.store.machines_install, "daemonRunning": False}
    view.store.machines_install_changed.emit()
    assert "The daemon is idle, so this is its last snapshot, as of 12 s ago." in _texts(view)


def test_a_running_daemon_reports_its_sessions_and_ages(screen):
    view = screen("machines-screen")
    texts = _texts(view)
    assert view._facts["Agent sessions"].text() == "3"
    assert view._facts["Last scan"].text() == "12 s ago"
    assert any(t.startswith("pid 48213 · started 2 h ago") for t in texts)
    assert not any("as of" in t for t in texts)
    view.clock = lambda: view.store.machines_state.updated_at + 300
    view._refresh_ages()
    assert view._facts["Last scan"].text() == "5 min ago"


def test_not_installed_and_no_snapshot(screen):
    view = screen("machines-screen-off")
    texts = _texts(view)
    assert "No snapshot yet: the remote-machines daemon has not run on this machine." in texts
    assert _panels(view) == []
    assert not view.refresh.isEnabled()
    assert view._light.text() == "not installed"


def test_refresh_is_offered_while_a_daemon_runs_even_when_not_installed(screen, monkeypatch):
    from diplomat_app import machinesview

    monkeypatch.setattr(machinesview, "machines_ready", lambda: True)
    view = screen("machines-screen-off")
    store = view.store
    for install, enabled in (({"installed": False, "daemonRunning": True}, True),
                             ({"installed": True, "daemonRunning": False}, True),
                             ({"installed": False, "daemonRunning": False}, False),
                             (None, False)):
        store.machines_install = install
        store.machines_install_changed.emit()
        assert view.refresh.isEnabled() is enabled, install


def test_the_header_counts_the_pool_the_paused_and_the_excluded(screen):
    view = screen("machines-screen")
    assert view._counts.text() == "3 in pool · 1 paused · 1 excluded"
    state = view.store.machines_state
    view.store.machines_state = state.__class__(**{**state.__dict__, "excluded": ()})
    view.store.machines_changed.emit()
    assert view._counts.text() == "3 in pool"
    view.store.machines_state = state.__class__(**{
        **state.__dict__, "machines": (), "excluded": state.excluded[1:]})
    view.store.machines_changed.emit()
    assert view._counts.text() == "0 in pool · 1 excluded"


def test_an_older_daemons_snapshot_says_so_in_the_status_card(screen):
    view = screen("machines-screen")
    assert view._old_daemon.isHidden()
    state = view.store.machines_state
    view.store.machines_state = state.__class__(**{**state.__dict__, "daemon": None})
    view.store.machines_changed.emit()
    assert not view._old_daemon.isHidden()
    assert view._old_daemon.text() == ("The daemon predates this screen; restart it to read "
                                       "usage and apply exclusions.")
    view.store.machines_state = None
    view.store.machines_changed.emit()
    assert view._old_daemon.isHidden()


def test_a_panel_counts_a_gpu_listed_twice(screen):
    view = screen("machines-screen")
    assert any(t.endswith("· 2× NVIDIA GeForce RTX 4090") for t in _texts(_panels(view)[1]))


def test_text_from_the_daemon_is_never_read_as_html(screen):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    view = screen("machines-screen")
    tag = "<b>x</b>"
    state = view.store.machines_state
    first = rm.Machine(**{**state.machines[0].__dict__, "target": f"t{tag}",
                          "aliases": (f"a{tag}",), "hostname": f"h{tag}",
                          "warnings": (f"w{tag}",), "os_name": f"o{tag}"})
    local = rm.Machine(**{**state.local.__dict__, "status": "error", "error": f"e{tag}",
                          "machine_name": f"m{tag}"})
    view.store.machines_state = state.__class__(**{
        **state.__dict__, "machines": (first,), "local": local,
        "unavailable": (unavailable(f"u{tag}", "auth-failed", "config"),)})
    view.store.machines_changed.emit()
    labels = [lb for lb in view.findChildren(QLabel) if tag in lb.text()]
    assert len(labels) >= 8
    assert all(lb.textFormat() == Qt.TextFormat.PlainText for lb in labels)


def test_the_header_button_opens_the_screen_and_its_polls_run_only_while_it_is_open(
        app, monkeypatch):
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    checks = []
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_machines_install_async",
                        lambda self: checks.append(self))
    panel = Panel(Store())
    view = panel.machines_view
    assert view.live
    panel.machines_btn.click()
    assert panel.body.currentIndex() == panel._screens["machines"]
    assert view._state_timer.isActive() and view._install_timer.isActive()
    assert len(checks) == 1
    panel.machines_btn.click()
    assert panel.body.currentIndex() == panel._screens["main"]
    assert not view._state_timer.isActive() and not view._install_timer.isActive()
    panel.deleteLater()


def test_hiding_the_window_stops_the_machines_polls_and_showing_it_restarts_them(
        app, monkeypatch):
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_machines_install_async", lambda self: None)
    panel = Panel(Store())
    view = panel.machines_view
    panel.show()
    panel.machines_btn.click()
    assert view._state_timer.isActive() and view._install_timer.isActive()
    panel.hide()
    app.processEvents()
    assert not view._state_timer.isActive() and not view._install_timer.isActive()
    panel.show()
    app.processEvents()
    assert view._state_timer.isActive() and view._install_timer.isActive()
    panel.hide()
    panel.deleteLater()


@pytest.mark.parametrize("mode", ["machines-screen", "machines-screen-idle",
                                  "machines-screen-off"])
def test_each_mode_renders_through_the_real_entry_and_touches_no_real_state(
        app, monkeypatch, tmp_path, mode):
    from diplomat_app import render

    read = []
    monkeypatch.setattr(rm, "read_state", lambda: read.append("state"))
    monkeypatch.setattr(rm, "read_history", lambda: read.append("history"))
    monkeypatch.setenv("DIPLOMAT_RENDER", mode)
    monkeypatch.setattr(render, "_agents_scratch_dir", None)
    out = tmp_path / f"{mode}.png"
    assert render.run(mode, str(out)) == 0
    assert out.stat().st_size > 10_000
    assert read == []
    assert not appconfig.path().exists()
