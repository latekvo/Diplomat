#!/usr/bin/env python3
"""The mesh network simulator's stand-in for a dispatched agent.

A real dispatched job opens a terminal running ``claude`` and, on completion,
writes an exit-code *sentinel* (see ``review.shell_command``'s ``done_path``).
This stub plays the same role deterministically so :mod:`mesh_sim` can watch it:

- it **records** the run (which node ran which work key, and when) to a shared
  append-only log, so the simulator can detect a **double-run** or a **drop**;
- it **holds** — keeping the executor's work-claim in flight — until the
  simulator releases it (a ``<work>.finish`` file) or a max deadline elapses;
- on exit it writes the completion **sentinel** the node hands it in
  ``DIPLOMAT_MESH_DONE_FILE`` (when the patched node sets one), which is how the
  executor learns the agent finished and releases the claim.

It is invoked through ``DIPLOMAT_MESH_SPAWN`` with the staged prompt file as its
final argument; the simulator bakes the per-node knobs in as flags. The injected
prompt *is* the work identifier, so the stub logs the prompt verbatim as ``work``.

Stdlib only, no repo imports: it runs as a detached child of a node subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    import fcntl  # POSIX only; the simulator is POSIX-only anyway.
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore


def _append(path: str, record: dict) -> None:
    """Append one JSON line under an advisory lock so concurrent agents on
    several nodes never interleave a write to the shared run log."""
    line = json.dumps(record, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mesh_sim_agent")
    ap.add_argument("--node", required=True, help="the node name this agent runs on")
    ap.add_argument("--runs", required=True, help="shared append-only run log (jsonl)")
    ap.add_argument("--hold-dir", required=True,
                    help="dir watched for <work>.finish / <work>.crash signals")
    ap.add_argument("--max-hold", type=float, default=30.0,
                    help="seconds to hold before self-releasing (deadlock guard)")
    ap.add_argument("prompt_file", help="staged prompt file (its content is the work id)")
    args = ap.parse_args(argv)

    try:
        with open(args.prompt_file, "r", encoding="utf-8") as fh:
            work = fh.read().strip()
    except OSError:
        work = ""

    done_file = os.environ.get("DIPLOMAT_MESH_DONE_FILE", "")
    pid = os.getpid()
    started = time.time()
    _append(args.runs, {"event": "start", "node": args.node, "work": work,
                        "pid": pid, "ts": started, "done_file": done_file})

    # Hold so the executor's claim stays in flight while the simulator probes for
    # a (forbidden) second run. Released by <work>.finish, or aborted early by
    # <work>.crash (agent died without resolving the work), or the deadline.
    safe = "".join(c if c.isalnum() else "_" for c in work) or "job"
    finish = os.path.join(args.hold_dir, f"{safe}.finish")
    crash = os.path.join(args.hold_dir, f"{safe}.crash")
    # A done-file-less spawn (the pre-fix node, or a plain template) is fire-and-
    # forget: record and exit immediately, exactly like the old `cp` stub.
    if done_file:
        deadline = started + args.max_hold
        crashed = False
        while time.time() < deadline:
            if os.path.exists(finish):
                break
            if os.path.exists(crash):
                crashed = True
                break
            time.sleep(0.05)
        if not crashed:
            # A finished agent writes the sentinel the executor watches on. A
            # crashed one exits WITHOUT it, modelling the terminal being killed;
            # the executor then frees the claim via the liveness lease instead.
            try:
                with open(done_file, "w", encoding="utf-8") as fh:
                    fh.write("0")
            except OSError:
                pass

    _append(args.runs, {"event": "end", "node": args.node, "work": work,
                        "pid": pid, "ts": time.time()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
