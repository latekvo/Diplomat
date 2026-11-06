"""One-time startup migrations: the pre-rename state directory and the login entry.

Diplomat's mesh state used to live under ``~/.argent/mesh`` (back when the app was
"Argent Utils"); the rename moved it to ``~/.diplomat/mesh``. The mesh identity
(``device.key`` + ``node.json``) is what *other* nodes pin their trust to, so a
fresh, empty ``~/.diplomat/mesh`` would mint a NEW identity and silently break this
node's standing across the whole fleet. This preserves the keypair by moving it
once, at applet startup, before the node comes up.

Scope is deliberately narrow:

* Only the **mesh** subdir is moved here — it is owned exclusively by the applet's
  own node. The ``device-allocator`` and ``pr-monitor`` subdirs belong to the Node
  daemon and are migrated by ``device-allocator/src/install.js``, which stops the
  daemon first so it never moves a dir out from under a live writer.
* ``~/.argent`` itself is left alone — it is shared with the separate Argent
  (device-control) tool, whose ``tool-server.json`` lives there.

:func:`migrate_autostart_entry` brings an installed login entry up to what
``install/install-autostart.sh`` writes, since nothing re-runs the installer.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .selfupdate import BACKGROUND_ENV

#: The application-menu entry ``install/install-autostart.sh`` writes, byte for byte.
MENU_ENTRY = """[Desktop Entry]
Type=Application
Name=Diplomat
Comment=software-mansion/argent triage tools
Exec={launcher}
Icon=applications-development
Terminal=false
Categories=Development;Utility;
"""


def migrate_legacy_state_dir() -> None:
    """Move ``~/.argent/mesh`` to ``~/.diplomat/mesh`` once, preserving identity.

    Idempotent and best-effort: a no-op on a clean machine or once migrated, and a
    failure never prevents the app from launching.
    """
    # A SZPONTNET_DIR override means the caller owns the path (tests, custom
    # deploys) — never migrate under it. Both spellings, because the library honours
    # the pre-rename one too: reading only the new name here would move a real
    # ~/.argent/mesh into a directory the node is not using.
    if os.environ.get("SZPONTNET_DIR") or os.environ.get("DIPLOMAT_MESH_DIR"):
        return
    home = Path.home()
    _merge_move(home / ".argent" / "mesh", home / ".diplomat" / "mesh")


def _merge_move(src: Path, dst: Path) -> None:
    """Move entries from ``src`` into ``dst`` without overwriting anything already
    present, so a partially-created new dir neither blocks the move nor clobbers
    newer data. Drops ``src`` once emptied."""
    try:
        if not src.is_dir():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            target = dst / entry.name
            if target.exists():
                continue  # keep the newer copy
            try:
                os.rename(entry, target)
            except OSError:
                # cross-device or a race — copy then drop the source.
                if entry.is_dir():
                    shutil.copytree(entry, target)
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    shutil.copy2(entry, target)
                    entry.unlink(missing_ok=True)
        try:
            src.rmdir()  # only succeeds if we emptied it
        except OSError:
            pass
    except Exception:  # noqa: BLE001 — migration must never break startup
        pass


def migrate_autostart_entry() -> None:
    """Add ``DIPLOMAT_BACKGROUND=1`` to the XDG autostart entry, and write the
    application-menu entry beside it, starting the same launcher, if it is missing.

    An entry installed before unattended launches started hidden runs the bare
    launcher, so every login would open and focus the window. A background start
    leaves the menu entry as the way back to the window where there is no tray.
    Idempotent and best-effort: an absent autostart entry (never installed, or
    uninstalled) is left absent, and a failure never prevents the app from launching.
    """
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    autostart = Path(config) / "autostart" / "diplomat.desktop"
    menu = Path(data) / "applications" / "diplomat.desktop"
    prefix = f"Exec=env {BACKGROUND_ENV}=1 "
    try:
        if not autostart.is_file():
            return
        lines = autostart.read_text().splitlines(keepends=True)
        rewritten = [prefix + ln.removeprefix("Exec=")
                     if ln.startswith("Exec=") and BACKGROUND_ENV not in ln else ln
                     for ln in lines]
        if rewritten != lines:
            _write_atomic(autostart, "".join(rewritten))
        launcher = next((ln.removeprefix(prefix).rstrip("\r\n") for ln in rewritten
                         if ln.startswith(prefix)), None)
        if launcher and not menu.exists():
            menu.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(menu, MENU_ENTRY.format(launcher=launcher))
    except Exception:  # noqa: BLE001 - migration must never break startup
        pass


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
