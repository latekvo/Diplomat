"""The workers the applet starts, and the wait that lets them land.

Every worker in ``diplomat_app`` ends by touching Qt — a signal emit, or a
callback that does. One still running when the process tears its widgets down
raises there, and an unhandled exception on a thread prints a traceback into a
``sys.stderr`` the finaliser is already closing: the interpreter cannot take the
buffer lock the worker holds, and the process aborts (SIGABRT) instead of
exiting. It reads in a log as "the applet crashed", which is the failure this
costs the most to misread.

So the property under test is structural — every worker is born somewhere that
keeps it joinable, and the wait actually joins — plus one end-to-end run that
puts a worker in mid-``stderr``-write at the moment the process ends and asserts
it still exits cleanly.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

from diplomat_app.store import Store

_LINUX_DIR = Path(__file__).resolve().parents[1]
_APP_DIR = _LINUX_DIR / "diplomat_app"


# ---- every worker is born joinable ----------------------------------------


def _is_thread_ctor(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "Thread"
    return isinstance(func, ast.Name) and func.id == "Thread"


def _seam_span() -> tuple[int, int]:
    """The line range of ``Store.start_background`` — the one place allowed to
    construct a worker."""
    tree = ast.parse((_APP_DIR / "store.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "start_background":
            return node.lineno, node.end_lineno or node.lineno
    raise AssertionError("Store.start_background is gone")


def test_no_worker_is_started_where_nothing_can_wait_for_it():
    """A bare ``threading.Thread(...).start()`` hands back no handle, so the quit
    path cannot know it is there. This is the check that keeps the next one from
    being added: the applet constructs a worker in exactly one place, and that
    place records it."""
    born = [
        (path.name, node.lineno)
        for path in sorted(_APP_DIR.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and _is_thread_ctor(node.func)
    ]
    low, high = _seam_span()
    stray = [
        f"{name}:{line}"
        for name, line in born
        if not (name == "store.py" and low <= line <= high)
    ]
    assert stray == []
    assert len(born) == 1


# ---- the wait ---------------------------------------------------------------


def test_the_wait_joins_the_work_it_started():
    store = Store()
    landed = []

    def work() -> None:
        time.sleep(0.05)
        landed.append(True)

    store.start_background(work)
    assert store.wait_for_background(5) == []
    assert landed == [True]


def test_a_worker_that_will_not_stop_is_named_rather_than_waited_on_forever():
    """The wait is bounded on purpose: a wedged worker must not be able to hold
    the applet open at quit. What it owes the caller instead is to say which one
    it is leaving behind."""
    store = Store()
    stop = []

    def wedged() -> None:
        while not stop:
            time.sleep(0.01)

    store.start_background(wedged, "wedged")
    began = time.monotonic()
    try:
        assert store.wait_for_background(0.2) == ["wedged"]
        assert time.monotonic() - began < 2
    finally:
        stop.append(True)


def test_the_workers_do_not_pile_up():
    """The store lives as long as the applet and polls every few seconds, so the
    list has to shed what has finished rather than grow all day."""
    store = Store()
    for _ in range(20):
        store.start_background(lambda: None)
        store.wait_for_background(5)
    assert len(store._workers) <= 2


# ---- and the abort it exists to prevent ------------------------------------


_BURST = '''\
import sys
import threading

from diplomat_app.store import Store

store = Store()
writing = threading.Event()


def burst() -> None:
    writing.set()
    for _ in range(4000):
        sys.stderr.write("x" * 4096 + "\\n")


store.start_background(burst, "stderr-burst")
# Only now, with the process's own work finished, does the worker start writing:
# whatever comes next is racing the interpreter's teardown.
writing.wait()
store.wait_for_background(60)
'''


def test_a_worker_mid_stderr_write_does_not_abort_the_process(tmp_path):
    """The failure in the wild, made to happen on demand: a worker holding the
    ``stderr`` buffer lock exactly as the process ends. Without the wait the
    finaliser cannot take that lock and kills the process with SIGABRT (-6);
    with it the worker is done before teardown starts."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(_LINUX_DIR),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "QT_QPA_PLATFORM": "offscreen",
        "DIPLOMAT_SELF_REPO": "/nonexistent",
    }
    noise = tmp_path / "burst.txt"
    for attempt in range(8):
        with open(noise, "wb") as sink:
            done = subprocess.run(
                [sys.executable, "-c", _BURST],
                cwd=str(_LINUX_DIR), env=env, stdout=subprocess.PIPE,
                stderr=sink, timeout=180,
            )
        assert done.returncode == 0, (
            f"run {attempt} died ({done.returncode}):\n"
            + noise.read_text(encoding="utf-8", errors="replace")[-2000:]
        )
