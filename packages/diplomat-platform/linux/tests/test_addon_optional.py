"""Diplomat with SzpontNet taken away.

"The mesh is an add-on" is a claim about what happens when it is **absent**, so
the only test of it worth having is one that removes the library and starts the
applet anyway. Nothing in-process can do that honestly: ``diplomat_app.szpont``
resolves the add-on once, at import, and the rest of the suite deliberately puts
this checkout's copy on ``sys.path``. So each case here is a real subprocess with
``szpontnet`` blocked at the import system itself — the closest thing to a
machine that never installed it.

Every case runs twice, blocked and not, off the same probe. The pair is the
point: on its own, "the applet started" proves nothing about the add-on being
optional, because it is also what a harness that quietly failed to block anything
would report.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_LINUX_DIR = Path(__file__).resolve().parents[1]
_SZPONTNET_DIR = _LINUX_DIR.parents[1] / "szpontnet-core"

# Imported by `site` at interpreter start, before the subprocess runs a line of
# its own — which is what it takes to be in front of `diplomat_app`'s import of
# `szpont`. Raising rather than returning None: a meta-path finder that returns
# None means "not mine, keep looking", and the next finder along would hand over
# the copy in this checkout.
_BLOCKER = '''\
import sys


class _NoSzpontNet:
    def find_spec(self, name, path=None, target=None):
        if name == "szpontnet" or name.startswith("szpontnet."):
            raise ImportError("blocked: this machine has no SzpontNet")
        return None


sys.meta_path.insert(0, _NoSzpontNet())
'''

# Reports what the applet built, from inside a process that has already imported
# it. The mesh preference is turned ON first: a machine that opted into the mesh
# and then lost the library is the case where a silent fallback would hurt most,
# and it keeps the two runs' *settings* identical so the add-on is the only
# variable between them.
_PROBE = '''\
import json
import threading

from PySide6.QtWidgets import QApplication

from diplomat_app import szpont
from diplomat_app.panel import Panel
from diplomat_app.store import Store

app = QApplication.instance() or QApplication([])
store = Store()
store.mesh_enabled = True
panel = Panel(store)

try:
    store.mesh_dispatch("review", "a prompt")
    dispatch = "accepted"
except szpont.Unavailable as exc:
    dispatch = "refused: %s" % exc

# Building a Store and a Panel starts workers, and this process is about to end.
# One still running when it does gets its widgets pulled out mid-signal, and the
# traceback lands on a stderr the finaliser is closing — a SIGABRT that reads, in
# this file's own output, exactly like the applet failing to start. So the probe
# waits it out, then counts the threads itself rather than repeating what the wait
# says it left behind: the wait is the thing under test.
store.wait_for_background(60)
left_running = sorted(
    t.name for t in threading.enumerate() if t is not threading.main_thread()
)

print("PROBE " + json.dumps({
    "available": szpont.AVAILABLE,
    "mesh_enabled": store.mesh_enabled,
    "mesh_button": panel.mesh_btn is not None,
    "mesh_screen": panel.mesh_view is not None,
    "screens": sorted(panel._screens),
    "mesh_timer": panel._mesh_timer is not None,
    "dispatch": dispatch,
    "left_running": left_running,
}))
'''


def _env(tmp_path: Path, *, blocked: bool, **extra: str) -> dict[str, str]:
    """A subprocess environment that differs from the developer's machine only in
    the two ways that matter: a private HOME (so a real ``~/.diplomat`` is neither
    read nor written) and, optionally, no SzpontNet.

    The library's directory stays on ``PYTHONPATH`` in *both* cases. The blocked
    run has to fail with the add-on sitting right there and reachable, or it is
    testing a short PYTHONPATH rather than a missing library.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    path = [str(_LINUX_DIR), str(_SZPONTNET_DIR)]
    if blocked:
        blocker = tmp_path / "blocker"
        blocker.mkdir(exist_ok=True)
        (blocker / "sitecustomize.py").write_text(_BLOCKER, encoding="utf-8")
        path.insert(0, str(blocker))
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(path),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "QT_QPA_PLATFORM": "offscreen",
        # The Settings screen kicks off an update check on construction; point it
        # at nothing so the probe never runs `git fetch` against the real checkout.
        "DIPLOMAT_SELF_REPO": "/nonexistent",
        **extra,
    }


def _run(
    args: list[str], tmp_path: Path, *, blocked: bool, **extra: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(_LINUX_DIR),
        env=_env(tmp_path, blocked=blocked, **extra),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _probe(tmp_path: Path, *, blocked: bool) -> dict:
    done = _run(["-c", _PROBE], tmp_path, blocked=blocked)
    assert done.returncode == 0, f"probe died (blocked={blocked}):\n{done.stderr}"
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("PROBE "))
    return json.loads(line[len("PROBE "):])


# ---- the applet itself ----------------------------------------------------


@pytest.mark.parametrize("blocked", [True, False])
def test_the_panel_paints_with_or_without_the_add_on(tmp_path, blocked):
    """Through the real entry point, all the way to pixels.

    ``python -m diplomat_app`` is what a user starts, and the headless render
    walks the same construction path the visible applet does — Panel, Settings,
    the wizards, a repaint — so a missing screen or an import that escaped its
    gate lands here as a non-zero exit rather than as a subtly emptier PNG.
    """
    out = tmp_path / "panel.png"
    done = _run(
        ["-m", "diplomat_app"], tmp_path, blocked=blocked,
        DIPLOMAT_RENDER="panel", DIPLOMAT_RENDER_OUT=str(out),
    )
    assert done.returncode == 0, done.stderr
    assert out.stat().st_size > 0


@pytest.mark.parametrize("blocked", [True, False])
def test_nothing_the_probe_started_outlives_it(tmp_path, blocked):
    """Every other case in this file reads the probe's exit code, so a worker left
    running past the end of it can abort the process and be indistinguishable, in
    the log, from the applet failing to start. Both parametrisations: with the
    add-on gone the mesh dispatch never gets a thread, but Settings still puts two
    of its own in the air while the panel is being built."""
    assert _probe(tmp_path, blocked=blocked)["left_running"] == []


def test_the_applet_starts_without_the_library(tmp_path):
    """The whole claim, in one line: no SzpontNet, no ImportError."""
    probe = _probe(tmp_path, blocked=True)
    assert probe["available"] is False


def test_the_block_is_what_makes_the_difference(tmp_path):
    """The control. Same command, same PYTHONPATH, same settings — only the
    import blocker removed — and now every mesh-shaped thing is there. Without
    this, a probe that silently failed to import ``szpontnet`` for some unrelated
    reason would read exactly like a successfully-optional add-on."""
    probe = _probe(tmp_path, blocked=False)
    assert probe["available"] is True
    assert probe["mesh_enabled"] is True
    assert probe["mesh_button"] is True
    assert probe["mesh_screen"] is True
    assert probe["screens"] == ["main", "mesh", "settings", "telemetry"]
    assert probe["mesh_timer"] is True
    assert probe["dispatch"] == "accepted"


# ---- what the applet does with the mesh gone ------------------------------


def test_a_machine_without_the_library_is_not_on_the_mesh(tmp_path):
    """``mesh_enabled`` is the lever every mesh path already asks about, so it is
    the one that has to answer honestly — the preference was set to True right
    before this and still reads False."""
    assert _probe(tmp_path, blocked=True)["mesh_enabled"] is False


def test_no_control_opens_a_screen_that_was_never_built(tmp_path):
    """The ⬡ button, the screen behind it and the entry that maps one to the
    other go together. Keeping the button while dropping the page is the failure
    this pins: the stack would flip to whatever index happened to be there."""
    probe = _probe(tmp_path, blocked=True)
    assert probe["mesh_button"] is False
    assert probe["mesh_screen"] is False
    assert probe["screens"] == ["main", "settings", "telemetry"]


def test_nothing_polls_a_mesh_that_cannot_exist(tmp_path):
    """The 2s topology poll is not merely inert without the add-on — it is not
    started, because no setting the user can reach would give it a node to read."""
    assert _probe(tmp_path, blocked=True)["mesh_timer"] is False


def test_a_mesh_command_names_what_is_missing(tmp_path):
    """A control round-trip is only reachable from a mesh control, so reaching one
    without the library is a caller bug — and it has to read as one. The bare
    ``ImportError`` this replaces named a module the operator never chose to
    install and no path to fix it."""
    dispatch = _probe(tmp_path, blocked=True)["dispatch"]
    assert dispatch.startswith("refused: ")
    assert "SzpontNet is not installed" in dispatch


def test_the_render_that_needs_the_screen_refuses_by_name(tmp_path):
    """``DIPLOMAT_RENDER=mesh`` asks for the one screen that isn't built. It has
    to fail — a zero exit and a PNG of the Actions screen is a render that lies
    about what it drew — and it has to fail saying which add-on is missing."""
    done = _run(
        ["-m", "diplomat_app"], tmp_path, blocked=True,
        DIPLOMAT_RENDER="mesh", DIPLOMAT_RENDER_OUT=str(tmp_path / "mesh.png"),
    )

    assert done.returncode != 0
    assert "SzpontNet is not installed" in done.stderr
    assert not (tmp_path / "mesh.png").exists()
