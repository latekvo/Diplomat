"""The one-node-per-state-dir startup lock (diplomat_app.mesh.singlelock).

Guards against the failure that ran four "ignacy" node processes at once: the
pre-launch node_running() checks are a time-of-check/time-of-use race, and
_start_tcp's first-free-port scan lets the duplicates coexist instead of the
second failing to bind. They then share one mesh_dir → one identity → one
state.json, which they clobber, so the panel shows a one-way sighting. The lock
makes the kernel arbitrate: exactly one holder per state dir.

Offline, no sockets, no Qt. Run with ``python -m pytest linux/tests`` or
dependency-free via ``python linux/tests/test_mesh_singlelock.py``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diplomat_app.mesh import singlelock  # noqa: E402
from diplomat_app.mesh import __main__ as mesh_main  # noqa: E402


def test_second_acquire_in_same_dir_is_refused(tmp_path):
    """A held lock refuses a second acquire on the SAME state dir — exactly the
    duplicate-node case. flock binds to the open file description, so a second
    open+lock contends even inside one process (no subprocess needed)."""
    first = singlelock.acquire(tmp_path)
    assert first is not None and first >= 0, "first acquire should hold the lock"
    try:
        assert singlelock.acquire(tmp_path) is None, "same dir must be refused"
    finally:
        singlelock.release(first)


def test_release_frees_the_dir_for_the_next_node(tmp_path):
    """Releasing the lock (a clean node stop) lets the next node take the dir —
    a restart must not be permanently fenced out by its predecessor."""
    first = singlelock.acquire(tmp_path)
    assert first is not None and first >= 0
    singlelock.release(first)
    second = singlelock.acquire(tmp_path)
    assert second is not None and second >= 0, "released dir must be re-acquirable"
    singlelock.release(second)


def test_lock_is_keyed_per_state_dir(tmp_path):
    """Two DIFFERENT state dirs never contend — this is what keeps the tests'
    and sim's many-nodes-on-one-host affordance (a distinct DIPLOMAT_MESH_DIR
    each) working after the guard lands."""
    a, b = tmp_path / "node-a", tmp_path / "node-b"
    la = singlelock.acquire(a)
    lb = singlelock.acquire(b)
    try:
        assert la is not None and la >= 0
        assert lb is not None and lb >= 0, "a distinct dir must lock independently"
    finally:
        singlelock.release(la)
        singlelock.release(lb)


def test_release_tolerates_sentinels_and_none():
    """The fail-open sentinel (-1) and None must be no-op releases — never raise
    from a clean-stop path that never really held a lock."""
    singlelock.release(None)
    singlelock.release(-1)  # must not raise


def test_lockfile_lives_in_the_state_dir(tmp_path):
    """The lock materializes as <mesh_dir>/node.lock, so it is co-located with
    the identity/state it protects (and cleaned up with the dir)."""
    fd = singlelock.acquire(tmp_path)
    try:
        assert (tmp_path / "node.lock").exists()
    finally:
        singlelock.release(fd)


def test_run_node_backs_off_when_the_dir_is_locked(tmp_path, monkeypatch):
    """The wiring: _run_node must exit 0 WITHOUT constructing a node when the
    lock is already held — the duplicate simply stands down."""
    monkeypatch.setattr(singlelock, "acquire", lambda mesh_dir=None: None)

    def _boom(*a, **k):  # constructing a node here means the guard failed to fence it
        raise AssertionError("MeshNode must not be built when the state dir is locked")

    monkeypatch.setattr("diplomat_app.mesh.node.MeshNode", _boom)
    assert mesh_main._run_node() == 0


if __name__ == "__main__":  # dependency-free run
    import tempfile
    from pathlib import Path

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            argc = fn.__code__.co_argcount
            if argc == 0:
                fn()
            else:
                with tempfile.TemporaryDirectory() as d:
                    # Only the tmp_path-only tests are runnable without pytest's
                    # monkeypatch fixture; skip the wiring test in this mode.
                    if "monkeypatch" in fn.__code__.co_varnames[:argc]:
                        print(f"skip {name} (needs pytest monkeypatch)")
                        continue
                    fn(Path(d))
            print(f"ok   {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
