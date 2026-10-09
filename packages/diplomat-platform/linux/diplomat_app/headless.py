"""The one-shot modes ``__main__.main`` runs, and the variables that ask for one it
does not (the twin of the macOS ``Headless.swift``).

``__main__`` consults it before anything else runs.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Every mode ``__main__.main`` dispatches before it launches the tray: ``True`` for
#: one that takes a value, ``False`` for a flag set to 1. ``test_headless_modes.py``
#: holds it to the ladder.
MODES = {
    "DIPLOMAT_SELF_UPDATE": False,
    "DIPLOMAT_WATCHDOG": False,
    "DIPLOMAT_AGENTS": False,
    "DIPLOMAT_DUMP": False,
    "DIPLOMAT_LOOKUP": True,
    "DIPLOMAT_PRINT_PROMPT": True,
    "DIPLOMAT_RENDER": True,
}

_MODE_SUFFIXES = ("_TEST", "_DUMP", "_SCAN", "_POLL")


def _turns_on(name: str, value: str) -> bool:
    takes_value = MODES.get(name)
    if takes_value is None:
        return False
    return bool(value) if takes_value else value == "1"


def looks_like_mode(name: str) -> bool:
    """Whether a variable is one a caller sets to ask for a one-shot run: a mode, or
    named the way modes are on either platform. No setting either app reads is named
    this way."""
    if not name.startswith("DIPLOMAT_"):
        return False
    return name in MODES or name.endswith(_MODE_SUFFIXES)


def unrunnable(env: Mapping[str, str]) -> list[str]:
    """The variables in ``env`` that ask for a one-shot this build will not run, as
    sorted ``NAME=value``s. Started with only these, the applet would launch the tray:
    newest-wins terminates the running one, and it acts on the real state."""
    return sorted(f"{name}={value}" for name, value in env.items()
                  if looks_like_mode(name) and not _turns_on(name, value))


def refusal(names: list[str]) -> str:
    """What ``__main__`` prints before exiting, when ``unrunnable`` names anything."""
    known = ", ".join(name + ("=<value>" if takes else "=1")
                      for name, takes in sorted(MODES.items()))
    return (f"Diplomat: refusing to start: {', '.join(names)} asks for a one-shot mode "
            "this build does not run, and without one it would replace the running "
            "Diplomat and act on its real state.\n"
            f"Modes this build runs: {known}\n")
