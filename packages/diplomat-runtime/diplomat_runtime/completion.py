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

``Stop`` alone is not taken at face value. It ends the *model's* turn, not the work:
a turn that dispatched subagents or backgrounded a command hands control back while
they are still running, and the CLI re-enters when one reports. So the hook reads the
payload it is handed and writes ``busy`` instead whenever ``background_tasks`` still
lists something. Without that guard a run that fanned out is finished seconds after
dispatch, with its whole swarm still working.

``SubagentStop`` is deliberately not among the events. A subagent finishing is not the
task finishing, and treating it as one would retire every run that delegated at the
moment its first helper returned. The guard above is what a delegating run needs, and
it is asked at the boundary that actually decides.

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

#: Events whose verb holds only if the CLI has no background work outstanding.
#:
#: ``Stop`` marks the end of the *model's* turn, and a turn that dispatched subagents
#: or a background shell ends while they are still running — the CLI hands the turn
#: back and re-enters when one reports. Taken at face value that is a finished run
#: seconds after dispatch, with its subagents still working. ``SessionEnd`` needs no
#: such guard: background work does not outlive the process it was started from.
GUARDED = frozenset({"Stop"})

#: What a guarded event greps its own payload for, whitespace already stripped.
#:
#: The CLI hands every hook a JSON payload on stdin, and ``background_tasks`` is the
#: list of subagents and background shells still outstanding — ``[]`` exactly when
#: there are none. A pending one makes it ``[{``, which is the whole test.
#:
#: It cannot be forged from inside the payload: a decoy in ``last_assistant_message``
#: has to escape its quotes to be valid JSON, and ``background_tasks\":[{`` does not
#: match. A payload that is unreadable, absent or reworded greps clean and so reports
#: the turn over — the one direction that can be wrong, and the direction the
#: stillness backstop already covers.
PENDING = r'"background_tasks":\[{'


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
                                                             done_path,
                                                             event in GUARDED)}]}]
                      for event, verb in EVENTS.items()}}


def _append(verb: str, activity_path: str, done_path: str | None = None,
            guarded: bool = False) -> str:
    cmd = ""
    word = verb
    if guarded:
        # The payload is read from stdin once, and the verb it chooses is reused
        # below so the line and the sentinel can never disagree about it.
        cmd += (f"v={verb}; tr -d ' \\t\\n' | grep -q {shlex.quote(PENDING)}"
                f" && v={BUSY}; ")
        word = '"$v"'
    cmd += f"printf '%s %s\\n' {word} \"$(date +%s)\" >> {shlex.quote(activity_path)}"
    if done_path and verb in (IDLE, ENDED):
        # `>` not `>>`: the sentinel is read by existence and dated by mtime, and a
        # second turn's line would only move that date later.
        sentinel = f"printf 0 > {shlex.quote(done_path)}"
        # `case` rather than `[ ]` so the miss is a no-op that still exits 0: a hook
        # that exits non-zero is reported to the agent as a failing hook.
        cmd += (f'; case "$v" in {verb}) {sentinel} ;; esac' if guarded
                else f"; {sentinel}")
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
