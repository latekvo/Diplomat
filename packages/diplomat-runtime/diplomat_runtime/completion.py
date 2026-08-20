"""Whether an agent's turn is over, asked of the agent's own CLI.

Three other mechanisms watch a run and none of them can answer it, because all
three describe a process and the question is about a *turn*:

* the exit-code sentinel (``review.shell_command``) fires only when the agent
  EXITS, and a spawned agent is interactive — finishing its work is not exiting;
* the pid probe sees the same live process before and after the turn;
* the screen scrape reads ``esc to interrupt`` off someone else's status bar, and
  its absence is an inference — the same absence a redraw, a resize or a reworded
  hint produces.

Left to those three, a finished run reads as ``awaiting_input`` for as long as its
window stays open, which is not a retirable state, so nothing ever marks it complete.

The CLI already knows, and will say so: a **hook** is a command it runs itself at a
named point in its own lifecycle. Two of them bracket a turn —

    UserPromptSubmit   a turn begins
    Stop               the turn ended

— and a third, ``SessionEnd``, fires when the session itself is over. Each appends
one line to the run's ``activity`` file, and **the last line is the answer**. No
polling, no scraping, no inference: the transition is reported by the process that
performs it, at the instant it performs it.

``SubagentStop`` is deliberately not among them. A subagent finishing is not the
task finishing, and treating it as one would retire every run that delegated at the
moment its first helper returned.

The hooks are injected per run via ``claude --settings <file>``, which MERGES with
the user's own settings rather than replacing them (verified against 2.1.237), so a
spawned agent keeps whatever hooks its user configured.

This module owns the *format* — the verbs, the file, the settings that write it — so
the writer (:mod:`review`) and the reader (:mod:`agentregistry`) cannot spell it
differently. Pure and stdlib-only: a mesh node builds the same spawn from a process
with no Qt and no front-end.
"""

from __future__ import annotations

import json
import shlex

#: A turn is in flight — ``UserPromptSubmit`` ran and no ``Stop`` has since.
BUSY = "busy"
#: The turn ended. The agent is alive at its prompt, and its work is done.
IDLE = "idle"
#: The session itself is over — the agent exited rather than returning to a prompt.
ENDED = "ended"

#: The verbs, in the file, as the hooks write them.
VERBS = (BUSY, IDLE, ENDED)

#: Hook event → the verb it records. The whole mapping, and the whole reason the
#: file is trustworthy: every verb here is written by the CLI at the moment the
#: transition happens, never inferred afterwards by something looking at it.
EVENTS = {"UserPromptSubmit": BUSY, "Stop": IDLE, "SessionEnd": ENDED}


def hook_settings(activity_path: str, done_path: str | None = None) -> dict:
    """The ``--settings`` payload that makes a run report its own turn boundaries.

    Append is the whole concurrency story: each line is a single small ``O_APPEND``
    write, so two hooks firing together interleave as whole lines rather than tearing
    one, and a reader never sees a half-written state.

    The timestamp is the hook's own ``date``, not the poll's clock — it is when the
    turn actually ended, which is what the ledger prices a run by. A poll can be a
    period late, which would make every run look that much longer.

    ``done_path`` is the mesh's reader of this same report. A szpontnet executor holds
    its claim on a work key until the agent's exit-code sentinel appears
    (``node._watch_agent``), and that sentinel fires on EXIT — which on its own
    would hold the key for however long the finished agent's window stays open, up to
    the backstop. Writing it on the terminal verbs releases the key when the turn
    ends. It is done HERE, in the settings, rather than in szpontnet: the node is a
    standalone library that must not import this one, and it already watches the file.
    """
    return {"hooks": {event: [{"hooks": [{"type": "command",
                                          "command": _append(verb, activity_path,
                                                             done_path)}]}]
                      for event, verb in EVENTS.items()}}


def _append(verb: str, activity_path: str, done_path: str | None = None) -> str:
    cmd = f"printf '%s %s\\n' {verb} \"$(date +%s)\" >> {shlex.quote(activity_path)}"
    if done_path and verb in (IDLE, ENDED):
        # `>` not `>>`: the sentinel is read by existence and dated by mtime, and a
        # second turn's line would only move that date later.
        cmd += f"; printf 0 > {shlex.quote(done_path)}"
    return cmd


def settings_json(activity_path: str, done_path: str | None = None) -> str:
    """:func:`hook_settings` as the file's bytes."""
    return json.dumps(hook_settings(activity_path, done_path))


def parse(text: str | None) -> tuple[str, float] | None:
    """The run's current state and when it was reached, from the activity file's LAST
    recognised line — or ``None`` when the file says nothing yet.

    Last wins because the file is a log of transitions, not a set of flags: a run
    that finished, was nudged, and finished again has three lines and is idle. Reading
    it any other way (does ``idle`` appear? did ``busy`` ever?) answers a question
    about the run's history instead of its state, and every such run is stuck in the
    first state it ever reached.

    Scanned from the end so the cost is the tail rather than the file, and unparseable
    lines are skipped rather than ending the scan — a torn final line from a hook
    killed mid-write must not hide the good state under it. ``None`` is what a caller
    turns into "unavailable", never into "finished".
    """
    if not text:
        return None
    for line in reversed(text.splitlines()):
        parts = line.split()
        if len(parts) != 2 or parts[0] not in VERBS:
            continue
        try:
            return parts[0], float(parts[1])
        except ValueError:
            continue
    return None


def is_over(state: str | None) -> bool:
    """Is the run's work done? Both terminal verbs, and only on a state actually
    read: ``None`` is the absence of an answer and can never end a run."""
    return state in (IDLE, ENDED)
