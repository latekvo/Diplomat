"""Bridge to the local device-allocator daemon + installer (Linux parity).

Mirrors ``diplomat-platform/macos/Sources/Diplomat/DeviceAllocator.swift``: the applet is a *viewer*
of the daemon's public ``~/.diplomat/device-allocator/state.json`` (the device pool
+ who holds what), a *client* of its unix socket for the actions the Devices screen
takes on a device or on the remote device pool, and a *driver* of the Node installer
(``device-allocator/src/install.js``) for the install controls. It never allocates
devices itself.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def _home() -> Path:
    return Path.home()


def package_dir() -> str:
    """Where the Node package lives; overridable for non-standard checkouts."""
    env = os.environ.get("DIPLOMAT_DEVICE_ALLOCATOR_DIR")
    if env:
        return env
    # A sibling package in this same checkout — resolved from our own path so it
    # works wherever (and however cased) the repo is cloned. This file is
    # packages/diplomat-platform/linux/diplomat_app/deviceallocator.py, so
    # parents[3] = packages/.
    sibling = Path(__file__).resolve().parents[3] / "device-allocator"
    if sibling.exists():
        return str(sibling)
    return str(_home() / "dev" / "diplomat" / "packages" / "device-allocator")


def install_js() -> str:
    return os.path.join(package_dir(), "src", "install.js")


def node_modules_dir() -> str:
    return os.path.join(package_dir(), "node_modules")


def deps_installed() -> bool:
    """True once the MCP server's one runtime dependency is present. The daemon
    needs no deps, but ``mcp.js`` imports ``@modelcontextprotocol/sdk``, so the
    server Claude Code spawns is dead without it."""
    return os.path.isdir(
        os.path.join(node_modules_dir(), "@modelcontextprotocol", "sdk")
    )


def base_dir() -> Path:
    """The daemon's directory, relocated by ``DA_BASE_DIR`` as in ``src/paths.js``."""
    env = os.environ.get("DA_BASE_DIR")
    if env:
        return Path(env).expanduser()
    return _home() / ".diplomat" / "device-allocator"


def state_path() -> Path:
    return base_dir() / "state.json"


def socket_path() -> Path:
    return base_dir() / "daemon.sock"


def package_available() -> bool:
    return os.path.exists(install_js())


def _version_key(name: str) -> list[int]:
    """Numeric sort key for an nvm dir like 'v20.14.1' (so v20 > v9, v18.20 > v18.9)."""
    out: list[int] = []
    for part in name.lstrip("v").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(-1)
    return out


def resolve_node() -> str | None:
    """Find a usable node without depending on a minimal tray PATH."""
    env = os.environ.get("DIPLOMAT_NODE")
    if env and os.path.exists(env):
        return env
    found = shutil.which("node")
    if found:
        return found
    nvm = _home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        for v in sorted(nvm.iterdir(), key=lambda p: _version_key(p.name), reverse=True):
            cand = v / "bin" / "node"
            if cand.exists():
                return str(cand)
    for p in ("/usr/local/bin/node", "/usr/bin/node"):
        if os.path.exists(p):
            return p
    return None


def resolve_npm() -> str | None:
    """Find npm the same way we find node; npm normally sits beside it."""
    env = os.environ.get("DIPLOMAT_NPM")
    if env and os.path.exists(env):
        return env
    node = resolve_node()
    if node:
        cand = os.path.join(os.path.dirname(node), "npm")
        if os.path.exists(cand):
            return cand
    return shutil.which("npm")


def ensure_deps() -> bool:
    """Ensure the package's node_modules exist (``npm install``) so the MCP
    server can start. No-op when already present. Returns True on success."""
    if not package_available():
        return False
    if deps_installed():
        return True
    npm = resolve_npm()
    node = resolve_node()
    if not npm:
        return False
    # npm's own shebang is `env node`; a minimal tray PATH may lack node, so
    # prepend node's directory before shelling out.
    child_env = dict(os.environ)
    if node:
        child_env["PATH"] = os.path.dirname(node) + os.pathsep + child_env.get("PATH", "")
    try:
        subprocess.run(
            [npm, "install", "--omit=dev", "--no-audit", "--no-fund"],
            cwd=package_dir(), env=child_env,
            capture_output=True, text=True, timeout=300,
        )
    except Exception:  # noqa: BLE001
        return False
    return deps_installed()


def read_state() -> dict | None:
    try:
        with open(state_path(), "r") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001 — absent/partial file is a normal state
        return None
    return decode_state(raw)


# MARK: snapshot decoding (mirrors DeviceState / RemoteStatus in Swift)

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    if isinstance(v, float):
        return math.isfinite(v) and v == int(v)
    return _is_number(v)


_ROW_STRINGS = ("key", "platform", "status")
_ROW_OPTIONAL_STRINGS = ("name", "version", "apiVersion", "handle", "brokenReason",
                         "repairLog", "format", "source")


def _row_readable(row) -> bool:
    if not isinstance(row, dict):
        return False
    if not all(isinstance(row.get(k), str) for k in _ROW_STRINGS):
        return False
    for k in _ROW_OPTIONAL_STRINGS:
        if row.get(k) is not None and not isinstance(row[k], str):
            return False
    for k in ("allocatedAt", "idleMs"):
        if row.get(k) is not None and not _is_number(row[k]):
            return False
    owner = row.get("owner")
    if owner is None:
        return True
    if not isinstance(owner, dict):
        return False
    if owner.get("agentName") is not None and not isinstance(owner["agentName"], str):
        return False
    return owner.get("ownerPid") is None or _is_int(owner["ownerPid"])


def decode_state(raw) -> dict | None:
    """``updatedAt`` is left out: it changes on every publish and would defeat the
    Store's publish-on-change guard."""
    if not isinstance(raw, dict) or not isinstance(raw.get("devices"), list):
        return None
    pid = raw.get("daemonPid")
    return {
        "devices": [row for row in raw["devices"] if _row_readable(row)],
        "daemonPid": int(pid) if _is_number(pid) and math.isfinite(pid) else None,
        "remote": RemoteStatus.from_json(raw.get("remote")),
    }


def json_text(v) -> str:
    """Any JSON value as one line: how a key this applet does not know is shown."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if _is_number(v):
        if math.isfinite(v) and v == round(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(float(v))
    if v is None:
        return "—"
    if isinstance(v, list):
        return ", ".join(json_text(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {json_text(v[k])}" for k in sorted(v))
    return str(v)


def _string(v) -> str | None:
    return v if isinstance(v, str) and v else None


_ISO_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")


def parse_instant(v) -> float | None:
    """Epoch seconds from an ISO-8601 instant or from epoch milliseconds."""
    if isinstance(v, str):
        if not _ISO_INSTANT.fullmatch(v):
            return None
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if _is_number(v) and math.isfinite(v) and v > 0:
        return v / 1000
    return None


@dataclass(frozen=True)
class RemoteStatus:
    """The snapshot's ``remote``. Every key it does not name is kept, sorted, as a
    ``(key, text)`` pair shown as it stands."""

    #: ``off``, ``loading``, ``error``, ``on``, ``signed-out``, ``signing-in``,
    #: ``sign-in-unanswered``, or ``unknown`` when the snapshot names none.
    state: str
    provider: str | None = None
    reason: str | None = None
    url: str | None = None
    login_url: str | None = None
    #: With ``sign-in-unanswered``: when sign-in prompts resume, in epoch seconds.
    retry_at: float | None = None
    extras: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_json(cls, v) -> RemoteStatus | None:
        """A non-object is an unreadable status, never an absent pool."""
        if v is None:
            return None
        if not isinstance(v, dict):
            return cls(state="unknown", extras=(("remote", json_text(v)),))
        known = {"provider", "state", "reason", "url", "loginUrl"}
        retry_at = parse_instant(v["retryAt"]) if "retryAt" in v else None
        # An unparseable retryAt stays an extra, shown as it stands.
        if retry_at is not None:
            known.add("retryAt")
        reason = v.get("reason")
        return cls(
            state=_string(v.get("state")) or "unknown",
            provider=_string(v.get("provider")),
            reason=None if reason is None else json_text(reason),
            url=_string(v.get("url")),
            login_url=_string(v.get("loginUrl")),
            retry_at=retry_at,
            extras=tuple((k, json_text(v[k])) for k in sorted(v) if k not in known),
        )


# MARK: daemon IPC (its unix socket)

@dataclass(frozen=True)
class DaemonReply:
    status: int
    json: object = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def get(self, key: str):
        return self.json.get(key) if isinstance(self.json, dict) else None


@dataclass(frozen=True)
class RemotePoolDevice:
    """One device the remote pool lists (``POST /remote/devices``)."""

    id: str
    platform: str | None = None
    name: str | None = None
    version: str | None = None
    api_version: str | None = None
    format: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class RemoteReply:
    """Signed in (``ok``), a listing (``devices``), or why neither (``reason``)."""

    ok: bool = False
    devices: tuple[RemotePoolDevice, ...] | None = None
    reason: str | None = None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


#: :func:`exchange` reached no daemon: no socket file, or nothing accepting on it.
UNREACHABLE = "unreachable"
#: :func:`exchange` met a daemon that did not answer in time, so it may yet act on the
#: request.
NO_REPLY = "no reply"


def exchange(endpoint: str, body: dict | None = None, *,
             timeout_secs: float) -> DaemonReply | str:
    """Any HTTP status is an answer; otherwise :data:`UNREACHABLE` or :data:`NO_REPLY`.
    Blocking, so off the GUI thread. Each call is its own connection, so a long one (a
    sign-in) holds up no other. Raises on a body JSON cannot encode."""
    path = socket_path()
    if not path.exists():
        return UNREACHABLE
    payload = json.dumps(body or {})
    conn = _UnixHTTPConnection(str(path), timeout_secs)
    try:
        try:
            conn.connect()
        except TimeoutError:
            return NO_REPLY
        except OSError:
            return UNREACHABLE
        try:
            conn.request("POST", f"/{endpoint}", body=payload,
                         headers={"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (OSError, http.client.HTTPException):
            return NO_REPLY
    finally:
        conn.close()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return DaemonReply(status, parsed)


def call(endpoint: str, body: dict | None = None, *,
         timeout_secs: float) -> DaemonReply | None:
    """:func:`exchange`, with None for no answer of either kind."""
    try:
        reply = exchange(endpoint, body, timeout_secs=timeout_secs)
    except (TypeError, ValueError):
        return None
    return reply if isinstance(reply, DaemonReply) else None


def post(endpoint: str, body: dict, *, timeout_secs: float) -> bool:
    """A 4xx is never a success: a device gone since the snapshot answers 404, and the
    activity feed must not log an action that never happened."""
    reply = call(endpoint, body, timeout_secs=timeout_secs)
    return reply is not None and reply.ok


def kill_device(key: str) -> bool:
    """Free a device and shut it down, or return a remote lease. The daemon shuts a
    simulator/emulator down inline, hence the timeout."""
    return post("kill", {"key": key}, timeout_secs=25)


def release_device(key: str, owner_pid: int) -> bool:
    """Take a device back from the agent holding it, releasing on its behalf."""
    reply = call("release", {"ownerPid": owner_pid, "deviceId": key}, timeout_secs=25)
    if reply is None or not reply.ok:
        return False
    # 200 with nothing released: the agent had already let it go.
    released = reply.get("released")
    return _is_number(released) and released >= 1


def mark_repaired(key: str) -> bool:
    """End a device's quarantine."""
    return post("repaired", {"key": key}, timeout_secs=10)


#: The daemon answers a sign-in once the browser sign-in ends: up to ten minutes.
SIGN_IN_TIMEOUT_SECS = 11 * 60 + 30
#: A listing is a network call on the daemon's side.
LISTING_TIMEOUT_SECS = 90


def remote_sign_in() -> RemoteReply:
    return sign_in_reply(call("remote/sign-in", timeout_secs=SIGN_IN_TIMEOUT_SECS))


def remote_devices() -> RemoteReply:
    """Never prompts a sign-in."""
    return devices_reply(call("remote/devices", timeout_secs=LISTING_TIMEOUT_SECS))


def _refusal(reply: DaemonReply | None) -> str | None:
    """The reason a reply carries when it is not the answer asked for, or None."""
    if reply is None:
        return "the allocator daemon did not answer"
    error = _string(reply.get("error"))
    if reply.status == 404 and error and error.startswith("unknown route"):
        return "this allocator has no remote device pool — update it, then restart its daemon"
    if not reply.ok:
        return (error or _string(reply.get("reason"))
                or f"the allocator answered HTTP {reply.status}")
    return None


def sign_in_reply(reply: DaemonReply | None) -> RemoteReply:
    why = _refusal(reply)
    if why:
        return RemoteReply(reason=why)
    if reply.get("ok") is True:
        return RemoteReply(ok=True)
    return RemoteReply(reason=_string(reply.get("reason")) or "the sign-in did not complete")


def devices_reply(reply: DaemonReply | None) -> RemoteReply:
    why = _refusal(reply)
    if why:
        return RemoteReply(reason=why)
    rows = reply.get("devices")
    if not isinstance(rows, list):
        return RemoteReply(reason=_string(reply.get("reason")) or "the pool sent no device list")

    def text(row: dict, k: str) -> str | None:
        v = row.get(k)
        return None if v is None else (json_text(v) or None)

    return RemoteReply(devices=tuple(
        RemotePoolDevice(id=text(row, "key") or f"#{i}", platform=text(row, "platform"),
                         name=text(row, "name"), version=text(row, "version"),
                         api_version=text(row, "apiVersion"), format=text(row, "format"),
                         state=text(row, "state"))
        for i, row in enumerate(rows) if isinstance(row, dict)
    ))


def _run_installer(arg: str) -> dict | None:
    node = resolve_node()
    if not node or not package_available():
        return None
    try:
        out = subprocess.run(
            [node, install_js(), arg],
            capture_output=True, text=True, timeout=90,
        )
        return json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return None


def check() -> dict | None:
    return _run_installer("--check")


def needs_install(status: dict | None, setup_done: bool) -> bool:
    """Whether a launch should run ``--install``, given what ``--check`` reported and
    whether this machine's setup has already been settled.

    One routine because it is one decision made in two places (this applet and the
    macOS one, whose `DeviceAllocator.needsInstall` is its twin) covering three
    situations that look alike and must not be confused:

    * **First run** — nothing installed, nothing settled. Install. A failure here
      leaves ``setup_done`` false, so the next launch retries; that is how a machine
      with no node yet eventually gets set up.
    * **Stale** — installed, but the deployed skill/rule/CLAUDE.md/registration no
      longer match this checkout. Re-install: ``--install`` rewrites every artifact,
      so it is also the repair.
    * **Settled uninstall** — the user removed it in Settings. Leave it alone. This is
      the one an "is everything in place?" check gets wrong, and getting it wrong
      means silently reinstalling something the user deliberately took off.

    Derives current from ``installed and not outdated`` rather than reading a positive
    flag: an installer predating drift detection reports neither, and keying off a
    missing flag would reinstall on every launch forever.
    """
    status = status or {}
    installed = bool(status.get("installed"))
    if setup_done and not installed:
        return False
    return not (installed and not status.get("outdated"))


def install() -> dict | None:
    return _run_installer("--install")


def uninstall() -> dict | None:
    return _run_installer("--uninstall")


def start_daemon() -> dict | None:
    """Start the daemon if none answers. The reply has no ``installed``: see
    :func:`with_install_status`."""
    return _run_installer("--start-daemon")


def restart_daemon() -> dict | None:
    """The one way to replace a daemon from older code: ``--install`` and
    ``--start-daemon`` keep any that answers."""
    return _run_installer("--restart-daemon")


def with_install_status(reply: dict | None, previous: dict | None) -> dict | None:
    """``reply``, keeping ``previous``'s install status when the reply does not report
    one, so a reply without ``installed`` never reads as "not installed"."""
    if reply is None or "installed" in reply or not previous or "installed" not in previous:
        return reply
    return {**reply, "installed": previous["installed"]}


# MARK: device-entry interpretation (mirrors DeviceAllocation in Swift)

def is_allocated(dev: dict) -> bool:
    owner = dev.get("owner")
    return bool(owner and owner.get("ownerPid") is not None) or dev.get("status") == "repairing"


def allocated_count(state: dict) -> int:
    return sum(1 for d in state.get("devices", []) if is_allocated(d))


def free_count(state: dict) -> int:
    devices = state.get("devices", [])
    return len(devices) - allocated_count(state)


def is_remote(dev: dict) -> bool:
    """A daemon without a remote pool writes no ``source``: its rows are all local."""
    return dev.get("source") == "remote"


#: Per-platform tint, as ``DeviceAllocation.platformTint`` on macOS.
PLATFORM_TINT = {"ios": "#0A84FF", "apple-tv": "#0A84FF", "android": "#34C759",
                 "android-tv": "#34C759", "vega": "#FF9500"}
GREY = "#8E8E93"


def status_badge(dev: dict) -> tuple[str, str]:
    status = dev.get("status", "free")
    if status == "ready":
        return ("in use", "#34C759") if is_allocated(dev) else ("free", GREY)
    if status == "booting":
        return ("attaching" if is_remote(dev) else "booting", "#FF9500")
    if status == "repairing":
        return ("repairing", "#AF52DE")
    if status == "error":
        return ("error", "#FF3B30")
    return ("free", GREY)


def idle_minutes(dev: dict) -> int | None:
    """Whole minutes idle, floored as the daemon does; None under a minute."""
    ms = dev.get("idleMs")
    if not _is_number(ms) or not math.isfinite(ms) or ms < 60_000:
        return None
    return int(ms // 60_000)


def idle_color(minutes: int, limit: int) -> str | None:
    """``limit``: the leased idle minutes after which the daemon takes the device back,
    0 = never. None is the ordinary muted text."""
    if limit <= 0:
        return None
    if minutes >= limit - 1:
        return "#FF3B30"
    if minutes >= limit * 2 // 3:
        return "#FF9500"
    return None
