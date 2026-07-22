"""The user-preferences handle (QSettings), shared by the applet and the headless
mesh node.

The :class:`~diplomat_app.store.Store` is the applet's front door to these values,
but a mesh node runs in its OWN process with no Store — and it spawns agents, so it
has to resolve settings like the repo root the same way. Both go through
:func:`settings` so there is exactly one org/app pair (and one key spelling) to keep
in sync; the macOS front-end's equivalents live in ``Store.Keys`` / ``RepoPaths``.
"""

from __future__ import annotations

ORG = "diplomat"
APP = "diplomat"

# Keys that are read outside the Store. Mirrors `RepoPaths.agentRepoKey` in Swift.
REPO_PATH = "agentRepoPath"


def settings():  # -> QSettings
    """A QSettings pointed at the applet's own store.

    Honors the process-wide default format (NativeFormat unless overridden): the
    two-arg ``QSettings(org, app)`` constructor is hardwired to NativeFormat, which
    on macOS ignores ``QSettings.setPath`` — so the test suite couldn't redirect it
    and would read/write the user's real settings.
    """
    from PySide6.QtCore import QSettings

    return QSettings(
        QSettings.defaultFormat(), QSettings.Scope.UserScope, ORG, APP
    )
