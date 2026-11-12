"""One-time startup migration of the login entry.

:func:`migrate_autostart_entry` brings an installed login entry up to what
``install/install-autostart.sh`` writes, since nothing re-runs the installer.
"""

from __future__ import annotations

import os
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
