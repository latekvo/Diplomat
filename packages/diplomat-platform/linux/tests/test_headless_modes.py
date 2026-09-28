"""Every one-shot mode the macOS app dispatches is one `Headless.active` knows about.

`Headless.active` is the single env-var list the AppDelegate and the Store share, and
its own docstring names the two costs of a mode that is missing from it: the launch
block it guards runs `SingleInstance.terminateOthers()`, which kills the operator's
live menu-bar applet, and the Store starts real polls — and potentially agent dispatch
— underneath a check that is supposed to be reading, not acting.

The list and the dispatch ladder in `DiplomatApp.applicationDidFinishLaunching` are two
hand-maintained copies of the same set of modes, and adding a mode means editing both.
`DIPLOMAT_SPAWN_SCRIPT_TEST` was added to one of them: the mode `ci.yml` prescribes,
and the obvious pre-push check for anyone touching the spawn script, terminated the
applet every time it was run locally. Nothing was red.

So: the two sets are the same set. Deliberately a grep and not a build — the drift is
exactly the shape a grep can see, and this job has the whole checkout but no Swift
toolchain.

The Linux applet has the same two lists: the ladder in `__main__.main`, and
`headless.MODES`, which its newest-wins singleton spares. And a mode one build lacks,
the other platform's included, has to be refused at the entry point rather than
started as the live app, so the two apps' refusals are held to each other's ladders.
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGES = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_APP = os.path.join(_PACKAGES, "diplomat-platform", "macos", "Sources", "Diplomat")

# Both spellings of the read, and tolerant of the whitespace either can be written
# with: `env` is the local alias the launch ladder binds, while
# `ProcessInfo.processInfo.environment` is the long form used elsewhere in the same
# file. A dispatch written the long way is exactly the one this test exists to catch,
# so a pattern that only knew the alias would go quiet on it.
_ENV_READ = re.compile(
    r'(?:env|environment)\s*\[\s*"(DIPLOMAT_[A-Z0-9_]+)"\s*\]')


def _source(name: str) -> str:
    with open(os.path.join(_APP, name), encoding="utf-8") as f:
        return f.read()


def _dispatched() -> set[str]:
    """The modes the launch ladder acts on. Every one of them ends in `exit()`, which
    is what makes being on the list below mandatory rather than a nicety."""
    return set(_ENV_READ.findall(_source("DiplomatApp.swift")))


def _swift_block(start_at: str) -> str:
    """The first `= [...]` literal in Headless.swift after `start_at`."""
    text = _source("Headless.swift")
    start = text.index("= [", text.index(start_at))
    return text[start:text.index("]", start)]


def _headless() -> set[str]:
    """The modes `Headless.active` answers yes for: the keys of `Headless.modes`.
    Scoped to that one table: `isRender` reads the environment again for its own
    reasons, and counting it would make the comparison pass on a name only IT still
    spells."""
    return set(re.findall(r'"(DIPLOMAT_[A-Z0-9_]+)"\s*:',
                          _swift_block("static let modes")))


def test_the_grep_finds_both_lists():
    """The check is a grep, so it is worth proving the grep finds anything at all: a
    renamed file or a re-spelled environment read would otherwise turn the comparison
    below into two empty sets that match forever."""
    dispatched, headless = _dispatched(), _headless()
    assert len(dispatched) >= 15, dispatched
    assert len(headless) >= 15, headless
    # One that takes a value and one that is a flag, so a regex that stopped seeing
    # either spelling is a failure here rather than a quieter comparison later.
    assert {"DIPLOMAT_RENDER", "DIPLOMAT_QUEUE_TEST"} <= dispatched
    assert {"DIPLOMAT_RENDER", "DIPLOMAT_QUEUE_TEST"} <= headless


def test_every_dispatched_mode_is_headless():
    """The direction that costs the operator their applet."""
    missing = sorted(_dispatched() - _headless())
    assert not missing, (
        "these modes are dispatched in DiplomatApp.applicationDidFinishLaunching but "
        "are not in Headless.active, so running one kills the live menu-bar app "
        "(SingleInstance.terminateOthers) and starts the Store's real polls: "
        + ", ".join(missing)
    )


def test_every_headless_mode_is_dispatched():
    """The other direction. A flag left in the list after its dispatch is gone excuses
    nothing today, and silently excuses whatever the name is next attached to."""
    orphans = sorted(_headless() - _dispatched())
    assert not orphans, (
        "Headless.active names these modes but DiplomatApp dispatches none of them: "
        + ", ".join(orphans)
    )


# MARK: - The Linux applet's two lists

_LINUX_APP = os.path.join(_PACKAGES, "diplomat-platform", "linux", "diplomat_app")

# A mode is read bare, `env.get("DIPLOMAT_X")`. The one parameter a mode takes,
# `DIPLOMAT_RENDER_OUT`, is read with a default, which is what keeps it out.
_LINUX_MODE_READ = re.compile(r'env\.get\(\s*"(DIPLOMAT_[A-Z0-9_]+)"\s*\)')


def _linux_dispatched() -> set[str]:
    """The modes `__main__.main` dispatches before it launches the GUI."""
    with open(os.path.join(_LINUX_APP, "__main__.py"), encoding="utf-8") as f:
        return set(_LINUX_MODE_READ.findall(f.read()))


def test_the_linux_grep_finds_the_ladder():
    dispatched = _linux_dispatched()
    assert len(dispatched) >= 6, dispatched
    assert {"DIPLOMAT_RENDER", "DIPLOMAT_SELF_UPDATE"} <= dispatched


def test_the_linux_modes_are_the_ladder():
    """Both directions at once: a dispatched mode missing from `MODES` is refused at
    the entry point and SIGTERMed by a tray starting beside it, and a listed mode no
    one dispatches starts the tray."""
    from diplomat_app import headless

    assert set(headless.MODES) == _linux_dispatched()


def test_the_singleton_spares_every_linux_mode():
    from diplomat_app import headless, singleton

    for name in headless.MODES:
        assert singleton._environ_is_headless(f"{name}=1\0".encode()), name


# MARK: - A mode only the other platform runs is refused, not started as the live app


def test_the_macos_app_refuses_every_linux_only_mode():
    """`Headless.linuxOnlyModes` names exactly the Linux modes macOS has no twin for,
    which is what makes `DIPLOMAT_AGENTS=1 swift run Diplomat` a refusal."""
    named = set(re.findall(r'"(DIPLOMAT_[A-Z0-9_]+)"',
                           _swift_block("static let linuxOnlyModes")))
    assert named == _linux_dispatched() - _dispatched()


def test_the_linux_applet_refuses_every_macos_only_mode():
    from diplomat_app import headless

    macos_only = _dispatched() - set(headless.MODES)
    assert "DIPLOMAT_QUEUE_TEST" in macos_only
    for name in macos_only:
        assert headless.unrunnable({name: "1"}) == [f"{name}=1"], name


def test_both_apps_name_modes_the_same_way():
    from diplomat_app import headless

    swift = set(re.findall(r'"(_[A-Z]+)"', _swift_block("static let modeSuffixes")))
    assert swift == set(headless._MODE_SUFFIXES)
