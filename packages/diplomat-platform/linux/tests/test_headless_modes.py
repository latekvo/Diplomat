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


def _headless() -> set[str]:
    """The modes `Headless.active` answers yes for. Scoped to that one property:
    `isRender` reads the environment again for its own reasons, and counting it would
    make the comparison pass on a name only IT still spells."""
    text = _source("Headless.swift")
    start = text.index("static let active")
    return set(_ENV_READ.findall(text[start:text.index("}()", start)]))


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
