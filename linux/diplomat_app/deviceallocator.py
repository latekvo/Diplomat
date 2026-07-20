"""Bridge to the local device-allocator daemon + installer (Linux parity).

Mirrors ``Sources/Diplomat/DeviceAllocator.swift``: the applet is a *viewer*
of the daemon's public ``~/.diplomat/device-allocator/state.json`` (the device pool
+ who holds what) and a *driver* of the Node installer (``device-allocator/src/
install.js``) for the Settings install controls. It never allocates devices itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _home() -> Path:
    return Path.home()


def package_dir() -> str:
    """Where the Node package lives; overridable for non-standard checkouts."""
    env = os.environ.get("DIPLOMAT_DEVICE_ALLOCATOR_DIR")
    if env:
        return env
    # Sibling of the linux/ front-end in this same checkout — resolved from our
    # own path so it works wherever (and however cased) the repo is cloned. This
    # file is <repo>/linux/diplomat_app/deviceallocator.py, so parents[2] = <repo>.
    sibling = Path(__file__).resolve().parents[2] / "device-allocator"
    if sibling.exists():
        return str(sibling)
    return str(_home() / "dev" / "diplomat" / "device-allocator")


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


def state_path() -> Path:
    return _home() / ".diplomat" / "device-allocator" / "state.json"


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
    """Decode the daemon's public snapshot. None if it has never run."""
    try:
        with open(state_path(), "r") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — absent/partial file is a normal state
        return None


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


def install() -> dict | None:
    return _run_installer("--install")


def uninstall() -> dict | None:
    return _run_installer("--uninstall")


# MARK: device-entry interpretation (mirrors DeviceAllocation in Swift)

def is_allocated(dev: dict) -> bool:
    owner = dev.get("owner")
    return bool(owner and owner.get("ownerPid") is not None) or dev.get("status") == "repairing"


def allocated_count(state: dict) -> int:
    return sum(1 for d in state.get("devices", []) if is_allocated(d))


def free_count(state: dict) -> int:
    devices = state.get("devices", [])
    return len(devices) - allocated_count(state)
