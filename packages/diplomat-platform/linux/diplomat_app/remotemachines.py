"""Bridge to the remote-machines daemon + installer, for the Machines screen.

The applet *views* the daemon's ``state.json`` (every machine it can offload to, best
first) and ``history.json`` (each one's usage over the last day), *drives* the Node
installer (``remote-machines/src/install.js``) exactly as it drives the device
allocator's, asks the daemon's socket for a fresh scan, and writes the operator's
exclusions to the shared config, which the daemon re-reads within a second.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from diplomat_runtime import appconfig
from . import deviceallocator as da


def package_dir() -> str:
    return da.sibling_package("remote-machines", "DIPLOMAT_REMOTE_MACHINES_DIR")


def install_js() -> str:
    return os.path.join(package_dir(), "src", "install.js")


def package_available() -> bool:
    return os.path.exists(install_js())


def base_dir() -> Path:
    """The daemon's directory, relocated by ``RM_BASE_DIR`` as in ``src/paths.js``."""
    env = os.environ.get("RM_BASE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".diplomat" / "remote-machines"


def state_path() -> Path:
    return base_dir() / "state.json"


def history_path() -> Path:
    return base_dir() / "history.json"


def socket_path() -> Path:
    return base_dir() / "daemon.sock"


# MARK: installer

def check() -> dict | None:
    return da.run_installer(install_js(), "--check")


def install() -> dict | None:
    return da.run_installer(install_js(), "--install")


def uninstall() -> dict | None:
    return da.run_installer(install_js(), "--uninstall")


def start_daemon() -> dict | None:
    return da.run_installer(install_js(), "--start-daemon")


def restart_daemon() -> dict | None:
    return da.run_installer(install_js(), "--restart-daemon")


def ensure_deps() -> bool:
    return da.ensure_deps(package_dir())


#: A full scan rediscovers and re-reads every machine; the MCP server waits as long.
SCAN_TIMEOUT_SECS = 120


def scan() -> da.DaemonReply | str:
    """Ask the daemon for a full scan: its reply, or ``da.UNREACHABLE`` /
    ``da.NO_REPLY``. No ``ownerPid``: that would attach this applet as an agent
    session, and the daemon would never go idle."""
    return da.exchange("scan", {"full": True}, timeout_secs=SCAN_TIMEOUT_SECS,
                       sock=socket_path())


def daemon_listening() -> bool:
    """Whether a daemon answers on the socket now. Any status counts: the client only
    POSTs, and the daemon's /health is a GET."""
    return da.exchange("health", timeout_secs=2, sock=socket_path()) != da.UNREACHABLE


# MARK: the snapshot

@dataclass(frozen=True)
class Machine:
    """One machine of the snapshot. Every field the daemon may leave out is None (or
    empty), so a reading this applet cannot place is simply not shown."""

    id: str
    status: str
    target: str | None = None
    aliases: tuple[str, ...] = ()
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    sources: tuple[str, ...] = ()
    error: str | None = None
    machine_name: str | None = None
    cores: float | None = None
    ssh_sessions: float | None = None
    load_1m: float | None = None
    memory_total: float | None = None
    memory_available: float | None = None
    pressure: str | None = None
    disk_free: float | None = None
    gpus: tuple[str, ...] = ()
    os_name: str | None = None
    arch: str | None = None
    cpu_model: str | None = None
    probed_at: float | None = None
    #: Taken out of the pool until this instant (epoch seconds), still read.
    paused_until: float | None = None
    #: Taken out of the pool for good: not contacted at all.
    excluded_for_good: bool = False
    #: The settings entries that take it out, which may name it by an address that is
    #: neither its target nor an alias.
    excluded_by: tuple[str, ...] = ()
    spare_cores: float | None = None
    usage_cpu: float | None = None
    usage_memory: float | None = None
    warnings: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """What the operator's exclusions may name this machine by."""
        return ((self.target,) if self.target else ()) + self.aliases + self.excluded_by


@dataclass(frozen=True)
class Daemon:
    pid: int | None = None
    started_at: float | None = None
    sessions: int | None = None


@dataclass(frozen=True)
class Snapshot:
    updated_at: float | None
    discovered_at: float | None
    probe_interval_ms: float | None
    daemon: Daemon | None
    local: Machine | None
    #: Best first: the load-balanced order, never re-sorted here.
    machines: tuple[Machine, ...]
    #: Paused ones first, then those excluded for good.
    excluded: tuple[Machine, ...]
    unavailable: tuple[Machine, ...]


def _num(v) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _strings(v) -> tuple[str, ...]:
    return tuple(s for s in v if isinstance(s, str)) if isinstance(v, list) else ()


def decode_machine(raw) -> Machine | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("status"), str):
        return None
    target = _str(raw.get("target"))
    memory, os_, cpu, usage = (_obj(raw.get(k)) for k in ("memory", "os", "cpu", "usage"))
    excluded = raw.get("excluded") if isinstance(raw.get("excluded"), dict) else None
    until = da.parse_instant(excluded.get("until")) if excluded is not None else None
    port = _num(raw.get("port"))
    return Machine(
        id=_str(raw.get("id")) or target or "",
        status=raw["status"],
        target=target,
        aliases=_strings(raw.get("aliases")),
        hostname=_str(raw.get("hostname")),
        user=_str(raw.get("user")),
        port=int(port) if port is not None else None,
        sources=_strings(raw.get("sources")),
        error=_str(raw.get("error")),
        machine_name=_str(raw.get("machineName")),
        cores=_num(raw.get("cores")),
        ssh_sessions=_num(raw.get("sshSessions")),
        load_1m=_num(_obj(raw.get("load")).get("last1m")),
        memory_total=_num(memory.get("totalBytes")),
        memory_available=_num(memory.get("availableBytes")),
        pressure=_str(memory.get("pressure")),
        disk_free=_num(_obj(raw.get("disk")).get("freeBytes")),
        gpus=tuple(g["name"] for g in raw.get("gpus") if isinstance(g, dict)
                   and _str(g.get("name"))) if isinstance(raw.get("gpus"), list) else (),
        os_name=_str(os_.get("name")),
        arch=_str(os_.get("arch")),
        cpu_model=_str(cpu.get("model")),
        probed_at=da.parse_instant(raw.get("probedAt")),
        paused_until=until,
        excluded_for_good=excluded is not None and excluded.get("until") is None,
        excluded_by=_strings(excluded.get("by")) if excluded is not None else (),
        spare_cores=_num(raw.get("spareCores")),
        usage_cpu=_num(usage.get("cpu")),
        usage_memory=_num(usage.get("memory")),
        warnings=_strings(raw.get("warnings")),
    )


def _machines(v) -> tuple[Machine, ...]:
    rows = (decode_machine(m) for m in v) if isinstance(v, list) else ()
    return tuple(m for m in rows if m is not None)


def decode_state(raw) -> Snapshot | None:
    """None until the daemon has finished a first scan. ``daemon`` and ``excluded``
    are absent from an older daemon's snapshot."""
    if not isinstance(raw, dict) or not isinstance(raw.get("machines"), list):
        return None
    d = raw.get("daemon")
    daemon = None
    if isinstance(d, dict):
        pid, sessions = _num(d.get("pid")), _num(d.get("sessions"))
        daemon = Daemon(pid=int(pid) if pid is not None else None,
                        started_at=da.parse_instant(d.get("startedAt")),
                        sessions=int(sessions) if sessions is not None else None)
    return Snapshot(
        updated_at=da.parse_instant(raw.get("updatedAt")),
        discovered_at=da.parse_instant(raw.get("discoveredAt")),
        probe_interval_ms=_num(raw.get("probeIntervalMs")),
        daemon=daemon,
        local=decode_machine(raw.get("local")),
        machines=_machines(raw.get("machines")),
        excluded=_machines(raw.get("excluded")),
        unavailable=_machines(raw.get("unavailable")),
    )


def _read_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - absent/partial file is a normal state
        return None


def read_state() -> Snapshot | None:
    return decode_state(_read_json(state_path()))


#: One reading: (epoch seconds, cpu, memory, ssh sessions), any value possibly None.
Reading = tuple[float, float | None, float | None, float | None]


@dataclass(frozen=True)
class History:
    interval_ms: float
    #: Machine id -> its readings, oldest first.
    machines: dict[str, tuple[Reading, ...]]


def decode_history(raw) -> History | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("machines"), dict):
        return None
    machines = {}
    for mid, rows in raw["machines"].items():
        if not isinstance(rows, list):
            continue
        machines[mid] = tuple(
            (r[0] / 1000, *(_num(v) for v in (list(r[1:4]) + [None] * 3)[:3]))
            for r in rows
            if isinstance(r, list) and r and _num(r[0]) is not None)
    return History(interval_ms=_num(raw.get("intervalMs")) or 60_000, machines=machines)


def read_history() -> History | None:
    return decode_history(_read_json(history_path()))


# MARK: the history graph

#: A silence longer than this many probe intervals is a gap: the daemon was not
#: running, so no line is drawn across it.
GAP_INTERVALS = 3
#: The graph never spans less than this, so a fresh daemon's few readings sit at
#: the right edge rather than across the whole width.
MIN_SPAN_SECS = 3600


def chart_start(rows: tuple[Reading, ...], now: float) -> float:
    return min(rows[0][0], now - MIN_SPAN_SECS) if rows else now - MIN_SPAN_SECS


def chart_runs(rows: tuple[Reading, ...], *, start: float, end: float, columns: int,
               interval_ms: float) -> list[list[Reading]]:
    """``rows`` cut at every gap, each run reduced to at most one reading per pixel
    column: the mean of cpu and of memory, the most SSH sessions."""
    gap = GAP_INTERVALS * interval_ms / 1000
    span = max(end - start, 1e-9)
    columns = max(1, columns)
    runs: list[list[Reading]] = []
    # The run being read, as (pixel column, its readings) pairs.
    current: list[tuple[int, list[Reading]]] = []
    for row in rows:
        if row[0] < start:
            continue
        if current and row[0] - current[-1][1][-1][0] > gap:
            runs.append(_reduce(current))
            current = []
        col = min(columns - 1, int((row[0] - start) / span * columns))
        if current and current[-1][0] == col:
            current[-1][1].append(row)
        else:
            current.append((col, [row]))
    if current:
        runs.append(_reduce(current))
    return runs


def _mean(values: list) -> float | None:
    got = [v for v in values if v is not None]
    return sum(got) / len(got) if got else None


def _reduce(buckets: list[tuple[int, list[Reading]]]) -> list[Reading]:
    out = []
    for _col, rows in buckets:
        sessions = [r[3] for r in rows if r[3] is not None]
        out.append((sum(r[0] for r in rows) / len(rows), _mean([r[1] for r in rows]),
                    _mean([r[2] for r in rows]), max(sessions) if sessions else None))
    return out


# MARK: how a machine reads

GB = 2 ** 30
GREEN, AMBER, RED, GREY = "#34C759", "#FF9500", "#FF3B30", "#8E8E93"


def gb_number(b: float) -> str:
    """As ``format.js`` prints bytes: one decimal under 10 GB."""
    return f"{b / GB:.{1 if b < 10 * GB else 0}f}"


def gb(b: float | None) -> str:
    return "?" if b is None else f"{gb_number(b)} GB"


def number(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def half_up(x: float) -> int:
    """JavaScript's ``Math.round``; Python's ``round`` takes 2.5 to 2."""
    return math.floor(x + 0.5)


def ago(seconds: float) -> str:
    s = max(0, half_up(seconds))
    if s < 120:
        return f"{s} s ago"
    if s < 120 * 60:
        return f"{half_up(s / 60)} min ago"
    if s < 48 * 3600:
        return f"{half_up(s / 3600)} h ago"
    return f"{half_up(s / 86400)} d ago"


def percent(fraction: float) -> str:
    return f"{half_up(fraction * 100)}%"


def usage_color(value: float, warn_above: float) -> str:
    """Red where the daemon warns (``warn_above``), amber from 75 %."""
    if value > warn_above:
        return RED
    return AMBER if value >= 0.75 else GREEN


def cpu_text(m: Machine) -> str:
    load = "?" if m.load_1m is None else number(m.load_1m)
    cores = "?" if m.cores is None else number(m.cores)
    spare = "" if m.spare_cores is None else f" · {number(m.spare_cores)} spare"
    return f"load {load} on {cores} cores{spare}"


def memory_text(m: Machine) -> str:
    if m.memory_total is None or m.memory_available is None:
        return "RAM ?"
    used = gb_number(m.memory_total - m.memory_available)
    pressure = (f" · pressure {m.pressure}" if m.pressure in ("warning", "critical")
                else "")
    return f"{used} of {gb(m.memory_total)} used{pressure}"


def gpu_names(gpus: tuple[str, ...]) -> str:
    """``2× NVIDIA RTX 6000 Ada, Apple M2 Ultra``, in the order the daemon lists them."""
    counts: dict[str, int] = {}
    for name in gpus:
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{n}× {name}" if n > 1 else name for name, n in counts.items())


def specs(m: Machine) -> str:
    os_part = " ".join(p for p in (m.os_name, m.arch) if p)
    parts = [os_part, m.cpu_model,
             m.cores is not None and f"{number(m.cores)} cores",
             m.memory_total is not None and f"{gb(m.memory_total)} RAM",
             gpu_names(m.gpus)]
    return " · ".join(p for p in parts if p)


def address(m: Machine) -> str:
    at = f"{m.user}@{m.hostname}" if m.user and m.hostname else (m.hostname or "")
    return f"{at}:{m.port}" if at and m.port not in (None, 22) else at


#: As ``format.js`` words each status.
STATUS = {
    "auth-failed": "SSH refused our keys",
    "unreachable": "not answering",
    "untrusted": "host key never accepted",
    "key-changed": "host key changed since it was accepted",
    "proxied": "behind a ProxyJump or ProxyCommand (not probed)",
    "error": "probe failed",
}
MAX_NAMES = 8


def unavailable_line(rows: tuple[Machine, ...]) -> str | None:
    """Twin of the ``Not usable`` line ``format.js`` gives agents, without the
    excluded machines (each has its own panel here)."""
    groups: dict[str, list[str]] = {}
    stale = strangers = 0
    for u in rows:
        # An address known_hosts alone remembers is usually a lease long since handed on.
        if u.status == "unreachable" and u.sources == ("known-hosts",):
            stale += 1
        # Any peer on the network picks its own mDNS name, so those are counted, never shown.
        elif u.status == "untrusted" and u.sources == ("mdns",):
            strangers += 1
        else:
            groups.setdefault(u.status, []).append(u.target or u.id)
    parts = []
    for status, names in groups.items():
        more = f" and {len(names) - MAX_NAMES} more" if len(names) > MAX_NAMES else ""
        parts.append(f"{STATUS.get(status, status)}: {', '.join(names[:MAX_NAMES])}{more}")
    if stale:
        parts.append(f"{stale} old known_hosts addresses not answering")
    if strangers:
        parts.append(f"{strangers} other machines advertising SSH on the network, "
                     "never vetted")
    return f"Not usable ({len(rows)}) - {'; '.join(parts)}." if parts else None


# MARK: exclusions (the shared config)

def iso_utc(epoch: float) -> str:
    """``2026-11-09T22:36:07.189Z``, as JavaScript's ``toISOString`` writes it."""
    ms = int(round(epoch * 1000))
    stamp = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return f"{stamp:%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


def _until(now: float) -> dict:
    """The pauses, less those run out. A time this applet cannot read is kept: the
    daemon reads it with ``Date.parse``, which takes more forms."""
    out = {}
    for name, at in appconfig.get_dict(appconfig.REMOTE_MACHINE_EXCLUDE_UNTIL).items():
        if not isinstance(at, str):
            continue
        when = da.parse_instant(at)
        if when is None or when > now:
            out[name] = at
    return out


def _without(names: tuple[str, ...], entries):
    """``entries`` (a list, or a dict's keys) minus every one of ``names``, by case."""
    drop = {n.lower() for n in names}
    if isinstance(entries, dict):
        return {k: v for k, v in entries.items() if k.lower() not in drop}
    return [e for e in entries if not (isinstance(e, str) and e.lower() in drop)]


def pause(m: Machine, seconds: float, now: float) -> None:
    until = _until(now)
    until[m.target] = iso_utc(now + seconds)
    appconfig.set_dict(appconfig.REMOTE_MACHINE_EXCLUDE_UNTIL, until)


def exclude(m: Machine, now: float) -> None:
    # The list first: in between, the daemon reads the machine as both, and for good wins.
    listed = appconfig.get_list(appconfig.REMOTE_MACHINE_EXCLUDE)
    if _without((m.target,), listed) == listed:
        appconfig.set_list(appconfig.REMOTE_MACHINE_EXCLUDE, listed + [m.target])
    appconfig.set_dict(appconfig.REMOTE_MACHINE_EXCLUDE_UNTIL, _without(m.names, _until(now)))


def resume(m: Machine, now: float) -> None:
    appconfig.set_dict(appconfig.REMOTE_MACHINE_EXCLUDE_UNTIL, _without(m.names, _until(now)))


def include(m: Machine, now: float) -> None:
    listed = appconfig.get_list(appconfig.REMOTE_MACHINE_EXCLUDE)
    appconfig.set_list(appconfig.REMOTE_MACHINE_EXCLUDE, _without(m.names, listed))
    resume(m, now)
