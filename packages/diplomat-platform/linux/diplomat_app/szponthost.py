"""Diplomat's answers to the six questions a SzpontNet node asks its host.

The node knows how to find machines and choose one; it does not know that a
"duty" here means a Claude agent reviewing a pull request, that events belong in
the applet's activity feed, that an agent might already be up on this PR without
the mesh having placed it, or how many agents this machine will run at once. This
module is where all six answers live — the whole of Diplomat inside SzpontNet,
and the only file the library would need replacing to run under something else.

Registered two ways, because the node runs in two places:

* in this process — :func:`install`, called by whoever is about to read a
  placement or drive a control command;
* in the node daemon Diplomat spawns — ``SZPONTNET_HOST=diplomat_app.szponthost``
  in its environment, which the library resolves through this module's
  :func:`host` factory.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from szpontnet import host as szpont_host

# Everything except the base class is imported inside the methods that need it.
# `diplomat_app/__init__` installs this host, so this module is on the import path
# of every entry point the package has, including the stdlib-only node daemon —
# and a module that pulls the applet's world in at import time would put all of it
# between the daemon's `python -m` and its first line of work.


class DiplomatHost(szpont_host.Host):
    """Diplomat behind a node."""

    def model(self) -> dict:
        """``assets/mesh.json`` — the duty catalog both front-ends render, which is
        also the catalog this deployment routes."""
        from . import core

        return core.mesh()

    def state_dir(self) -> Path:
        """``~/.diplomat/mesh``, where this machine's node has always kept its
        identity. Every peer's trust allowlist is keyed to the device keypair in
        that directory, so it is not free to move: adopting the library's own
        default would mint a fresh key and make this machine a stranger to its
        own fleet."""
        return Path.home() / ".diplomat" / "mesh"

    def log(self, action: str, detail: str) -> None:
        """Into the shared activity feed, under the ``mesh`` source — the same
        file the macOS app and the device-allocator daemon append to, so a node
        event lands in the panel's activity screen beside everything else."""
        from . import activity

        activity.log("mesh", action, detail)

    def run_job(self, prompt: str, done_path: str | None) -> str:
        """Open a terminal running the configured agent runner on the staged prompt,
        exactly like a local SPAWN AGENT.

        macOS goes through ``osascript`` (Terminal.app on the same shell command
        the Linux spawner builds); Linux uses the applet's own spawner, which
        auto-detects an installed terminal emulator.
        """
        from . import review

        if platform.system() == "Darwin":
            return _spawn_macos(prompt, done_path)
        try:
            return review.spawn(prompt, None, done_path=done_path)
        except review.SpawnError as exc:
            raise szpont_host.NoRunner(str(exc)) from exc

    def work_already_running(self, work_key: str) -> bool:
        """Is a live ``claude`` agent for this work key's PR already up on THIS
        machine? Reuses the ORIGINATING side's matcher (``live_pr_numbers``) so
        both sides agree on what "an agent is on this PR" means. Keyed on the PR,
        not the exact work key, so a fresh push (new ``@sha``) can't dodge it.

        Fails OPEN — a ps error reads as "not seen" so a transient failure never
        drops work — the same trade the store's ``_live_pr_agents`` makes.
        """
        from . import autofix

        ref = autofix.parse_work_key(work_key)
        if ref is None:
            return False
        _kind, owner, repo, number = ref
        return number in autofix.live_pr_numbers(_ps_dump(), owner, repo)

    def at_job_capacity(self, running_keys: list[str]) -> bool:
        """Should a dispatch routed here be declined and failed over to another
        node? Two reasons say yes, and the library asks for them as one question:
        this machine is already at its cap on concurrent automatic tasks (Settings →
        PR AUTO-FIX, 2 by default), or it has too little rate limit left to afford
        the job (:mod:`autobudget`).

        The applet enforces both on the work IT originates; this is the other half —
        work a mesh peer routes in, which the applet never sees and which spends
        this machine's cap and this machine's limit just the same. Every input comes
        from the shared config file and the shared ledger
        (:func:`appconfig.auto_task_limit`, :func:`autobudget.decide`), so a device
        cannot end up with one answer for work it found itself and another for work
        it was sent.

        A budget decline is logged here rather than left to the node's own
        `mesh-at-capacity` line, which can only say the machine is busy — it is the
        difference between a peer waiting minutes for an agent to finish and waiting
        hours for a window to refill, and the operator reading the feed is the one
        who needs to know which.

        Failing the slot over is the right outcome for both: the mesh ranks peers
        surplus-first, so the node with limit to spend is exactly the one that picks
        this up.

        Counted from the ``ps`` scan unioned with the node's own live jobs: an agent
        the node started seconds ago is in ``running_keys`` before it is in ``ps``,
        and a burst of dispatches is precisely when that gap decides whether the cap
        holds. Unlike the applet's count, this one cannot subtract agents the
        operator started by hand from the panel — the node keeps no such book — so a
        machine busy with manual work declines mesh work rather than piling on. The
        peer just fails the slot over to a node with room.

        An agent waiting at its prompt is not counted, exactly as the applet does not
        count one (:meth:`Store._auto_tasks_running`): an interactive session does not
        exit when its work is done, so without that subtraction a node whose finished
        windows are still open declines every peer's work for as long as they stay
        open — hours, and with nothing running to justify it.

        An unreadable ``ps`` never stalls the node: the scan degrades to empty and
        the answer falls back to what the node itself knows it is running — the same
        fail-open direction :meth:`work_already_running` takes, for the same reason.
        An unreadable tmux degrades the other way, to "everything is working", so a
        node that cannot see its panes declines work rather than piling it on.
        """
        from . import appconfig, autobudget, autofix, core, tmuxwatch

        if autobudget.enabled():
            budget = autobudget.decide()
            if not budget.affordable:
                self.log("mesh-no-budget",
                         f"Declined a peer's job — {autobudget.shortfall(budget)}")
                return True

        cfg = core.config()
        mine = {
            ref[3] for ref in (autofix.parse_work_key(k) for k in running_keys)
            if ref is not None
        }
        dump = _ps_dump()
        live = autofix.live_pr_numbers(dump, cfg["owner"], cfg["repo"])
        # `or {}` is the "everything is working" degradation this docstring promises:
        # tmux answering `None` means it could not be read, and no tty then resolves
        # to a tail, so nothing is subtracted as idle.
        tails = tmuxwatch.pane_tails_for_ttys(
            autofix.agent_ttys(dump, cfg["owner"], cfg["repo"])
        ) or {}
        idle = autofix.idle_pr_numbers(dump, tails, cfg["owner"], cfg["repo"])
        return (autofix.running_auto_tasks(live, mine, set(), idle)
                >= appconfig.auto_task_limit())


def _ps_dump() -> str:
    """Every process's tty and argv, one per line — the evidence behind both of the
    node's ground-truth answers (is this work already running here, and is the machine
    at its cap). One place for the portable spelling and for the fail-open error
    handling, so the two answers cannot come to disagree about either.

    ``ps -Ao tty=,args=`` is that portable spelling: on macOS ``-e`` prints the
    environment, not every process, so the store's Linux-only ``-eo`` can't be
    reused here (a node runs on both OSes). The tty leads because it is what joins a
    ``claude`` process to the tmux pane showing it (:func:`autofix.idle_pr_numbers`);
    the argv scan is indifferent to it, finding its prompt wherever on the line it
    falls.

    Returns ``""`` on any failure, which is what lets both callers degrade rather
    than raise into the executor's spawn path. ``UnicodeDecodeError`` is caught by
    name: ``text=True`` decodes strict UTF-8, so any process on the box with a
    non-UTF-8 byte in its argv makes the output undecodable, and it is a
    ``ValueError`` — neither an ``OSError`` nor a ``SubprocessError`` — so without it
    the exception escapes the guard. The same catch ``Store._ps_dump`` makes for its
    identical scan.
    """
    try:
        return subprocess.run(["ps", "-Ao", "tty=,args="],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return ""


def _spawn_macos(prompt: str, done_path: str | None) -> str:
    """Terminal.app via ``osascript``, on the shell command the Linux spawner
    builds. Returns the staged prompt path."""
    from . import review

    prompt_file = review.write_prompt(prompt)
    shell_cmd = review.shell_command(prompt_file, done_path)
    script = f'tell application "Terminal" to do script {_applescript_quote(shell_cmd)}'
    try:
        review.popen_detached(["osascript", "-e", script])
    except OSError as exc:
        raise szpont_host.NoRunner(f"osascript failed: {exc}") from exc
    return prompt_file


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def host() -> DiplomatHost:
    """Factory for ``SZPONTNET_HOST=diplomat_app.szponthost``."""
    return DiplomatHost()


def install() -> None:
    """Put Diplomat behind the node modules running in *this* process."""
    szpont_host.set_host(DiplomatHost())
