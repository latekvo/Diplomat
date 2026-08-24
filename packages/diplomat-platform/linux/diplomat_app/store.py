"""Application state, persisted settings, and the tool catalog.

A port of Store.swift. The tool catalog (titles, subtitles, colours, order) is
loaded from the shared ``assets/catalog.json``; the row-mapping in ``items_for``
is the same dense formatting the macOS panel renders. Settings persist via
``QSettings`` (the Linux analogue of macOS UserDefaults) — except the repo root,
which lives in the shared ``~/.diplomat/config.json`` (see :mod:`appconfig`) so a
Qt-less mesh node can read it too.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QSettings, Signal

import json
import os
import re
import subprocess
import tempfile
import threading
import time

from diplomat_runtime import (
    activity,
    agentregistry,
    agentstate,
    apiwatch,
    appconfig,
    autobudget,
    autofix,
    core,
    promptcore,
    review,
    runner,
    telemetry,
    tmuxwatch,
)
from diplomat_runtime.models import API, Filters, Fmt, OpenIssue, OpenPR
from diplomat_runtime.prtarget import PRTarget
from . import (
    autofixmonitor,
    bans,
    conflicts,
    deviceallocator,
    issues,
    probes,
    szpont,
)


def _count(n: int, noun: str) -> str:
    """``3 files`` / ``1 file`` — the pluralisation two row builders share."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _installer_files(pr: OpenPR) -> list[str]:
    """The installer-owned paths in a PR — counted in line2 and listed in line3,
    so they must be the same set in both."""
    return [f for f in pr.files if Filters.is_installer_file(f)]


def _run_prompt(run_id: str) -> str:
    """What a run was asked — the string :func:`usagescan.task_run` matches
    transcripts against. Empty when the run directory is gone, which prices the run
    as unattributed rather than as some other agent's transcript."""
    try:
        return agentregistry.prompt_path(run_id).read_text(encoding="utf-8")
    except OSError:
        return ""


# MARK: - Value types


@dataclass(frozen=True)
class DisplayItem:
    id: int
    badge: str  # "#337"
    title: str
    url: str
    line2: str  # primary metadata
    line3: str | None = None  # optional detail (skills / files / labels)


@dataclass(frozen=True)
class Tool:
    """One entry in the tool library, hydrated from assets/catalog.json."""

    id: str
    title: str
    subtitle: str
    emoji: str
    glyph: str
    color_hex: str


@dataclass(frozen=True)
class LookupResult:
    number: int
    on_lists: list[str]  # tool ids
    presence: str
    url: str | None

    @property
    def is_on_any_list(self) -> bool:
        return bool(self.on_lists)


def tools() -> list[Tool]:
    return [
        Tool(
            id=t["id"],
            title=t["title"],
            subtitle=t["subtitle"],
            emoji=t["emoji"],
            glyph=t.get("linuxGlyph", t["emoji"]),
            color_hex=t["colorHex"],
        )
        for t in core.catalog()
    ]


def tool_by_id(tool_id: str) -> Tool | None:
    return next((t for t in tools() if t.id == tool_id), None)


# MARK: - Store


class Store(QObject):
    # Emitted (on the main thread) whenever the rendered data/settings change.
    changed = Signal()
    # Emitted with the loading flag when a refresh starts/ends.
    loading_changed = Signal(bool)
    # Emitted when the device-allocator pool snapshot changes (light, not a full
    # data refresh) and when its install status is re-checked.
    devices_changed = Signal()
    allocator_changed = Signal()
    # Emitted when the activity feed (audit.jsonl) or ban list snapshot changes.
    activity_changed = Signal()
    # Emitted when the mesh topology snapshot (state.json) meaningfully changes.
    # Live-ish (a 2s poll drives it), so it fires far more often than `changed`;
    # the MeshView rebuilds in place from it, so the rebuild stays cheap.
    mesh_changed = Signal()
    # Emitted when the self-update status/progress changes.
    update_changed = Signal()
    # Emitted after each PR auto-fix monitor poll (status pill, counts, poll error).
    autofix_changed = Signal()
    # Emitted when the Agent-tasks list changes: the queue behind the automatic-task
    # cap, or how many slots of that cap are standing empty.
    tasks_changed = Signal()
    # Emitted after each Claude-API-error watcher scan (status pill + continue count).
    apiwatch_changed = Signal()
    # Emitted when a telemetry sample lands, so an open Telemetry screen refreshes
    # instead of waiting for the user to flip a range.
    telemetry_changed = Signal()

    _ORG = "diplomat"
    _APP = "diplomat"

    # How many agent screens must be read without once showing the CLI's interrupt
    # hint before that is worth reporting. High, because a quiet machine legitimately
    # produces the same reading: every agent really can be sitting at its prompt.
    _MARKER_SAMPLE = 40

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[OpenPR] = []
        self.issues: list[OpenIssue] = []
        self.is_loading = False
        self.error: str | None = None
        self.last_updated: datetime | None = None
        self.selected: str = tools()[0].id
        self.has_loaded = False
        self.me = ""

        # Live device-allocator state (pool + holders) and install status.
        self.device_state: dict | None = None
        self.allocator_install: dict | None = None

        # Live telemetry read from the shared ~/.diplomat files (activity feed + bans).
        self.audit_entries: list = []
        self.banned_authors: list = []

        # Self-update progress for the Settings UPDATE section. None until the
        # first check; then {"phase": "checking"|"idle"|"updating"|"restarting"
        # |"error", ...} — "idle" carries the selfupdate.check() result,
        # "updating" a human-readable "step", "error" the failure reason.
        self.update_state: dict | None = None

        # Live mesh topology (state.json snapshot; None until a node has run here)
        # and the last control-edit error surfaced to the MeshView as a red line.
        self.mesh_state: dict | None = None
        self.mesh_error: str | None = None
        # Render-only: force mesh_enabled on without persisting to real QSettings.
        self._mesh_enabled_override: bool | None = None

        # PR auto-fix monitor: live-only runtime state (the toggles/counters persist
        # via QSettings below). Mirrors AutofixStatus.swift + the monitor's poll-error
        # + unaddressed-review signals.
        self.autofix_status: dict | None = None
        self.autofix_poll_error: str | None = None
        self.autofix_poll_error_at: float | None = None
        self.unaddressed_reviews = 0
        self._poll_error_this_cycle: str | None = None
        # The last resolved tick — what every agent question is answered from (the
        # dedup, the cap, the rows, the retirement). None until the first one.
        # Replaced, never mutated: the poll worker writes it while the GUI thread
        # draws from it. Its own short mutex guards the swap and the cache stamp.
        self._tick: agentstate.Tick | None = None
        self._tick_lock = threading.Lock()
        # What the last merged-status probe found. It costs a `gh` call per PR, so it
        # rides the slow refresh rather than the 8-second tick, and the fast ticks
        # carry the answer forward (UNAVAILABLE until the first one runs).
        self._merged_prs = agentstate.Observation.unavailable(
            "have not been probed yet")
        # Which probes have already been reported silent, so the feed gets one line
        # per episode rather than one per tick, and another when they come back.
        self._probe_warned: dict[str, bool] = {}
        self._marker_warned = False
        # Whether the "deferring auto work" note has been logged for the current
        # at-capacity episode (see _log_at_capacity), and for the current
        # out-of-budget one (see _log_unaffordable). Two flags, not one: a machine
        # can saturate and drain several times over inside a single spell of having
        # nothing left to spend, and each episode is worth one line of its own.
        self._capacity_logged = False
        self._budget_logged = False
        # Automatic work nothing has started yet — held by the task cap, by the
        # spending budget, or by its own monitor being switched off — in the order
        # it will run. The panel's Agent-tasks list.
        #
        # Deliberately NOT persisted. A deferral writes no attempt record precisely so
        # that every poll re-offers everything GitHub still owes, which means the queue
        # is rebuilt from live evidence every 3 minutes; a stored copy would only ever
        # be a staler answer to a question already being re-asked, and would hand
        # "execute now" a prompt assembled against a PR that has since moved on. What
        # IS persisted is `queued_task_order` — the operator's arrangement — and
        # `requested_work`, the sweeps the operator asked for, both being things a
        # poll cannot reconstruct. Always REPLACED, never mutated in place: the poll
        # worker writes it while the GUI thread reads it to draw the rows.
        self.queued_tasks: list[autofix.QueuedTask] = []
        # Queued work whose dispatch is under way: it has left the queue and its spawn
        # has not answered yet.
        #
        # That span is seconds long — a `ps` scan, a mesh round-trip, a terminal — and
        # for all of it the task is neither queued nor yet an agent this panel counts.
        # It is held here so that it stays a ROW throughout, saying what the click just
        # did to it, rather than a gap where the operator's task used to be. In memory
        # only, like the queue it comes from. Replaced, never mutated: the worker
        # thread writes it while the GUI thread draws from it.
        self.starting_tasks: list[autofix.QueuedTask] = []
        # This poll's deferrals, published as `queued_tasks` only once the whole cycle
        # has succeeded: a failed fetch means "we no longer know what is owed", which
        # is not the same as "nothing is owed", and must not empty the list.
        self._staged_queue: list[autofix.QueuedTask] = []
        # The task each stored ask has been built into, as (config, task) by queue key
        # — an assembled prompt is the expensive half and cannot change while the ask
        # stands, so it is built once rather than once per poll (:meth:`_requested_task`).
        self._requested_tasks: dict[str, tuple[dict, autofix.QueuedTask]] = {}
        # Asks already reported as un-assemblable, so the poll says it once rather
        # than every cycle for as long as the ask stands (:meth:`_note_unbuildable_ask`).
        self._unbuildable_logged: set[str] = set()
        # Serializes the read-modify-write of the stored asks. Three threads reach it:
        # the fan-out worker adding a sweep's, the poll worker dropping the one it
        # just dispatched, and the GUI thread cancelling a row. Unguarded, a sweep that
        # read the list before a dispatch removed an entry would write that entry back,
        # and the item would be worked twice.
        self._requested_lock = threading.Lock()
        # Guards the _dispatching_prs set (below). MUST stay distinct from the
        # poll-overlap guard: run_autofix_poll_async holds that one across the whole
        # worker, and the worker's dispatch_agent re-takes THIS one — sharing a single
        # non-reentrant lock across both self-deadlocks the worker (it would re-acquire
        # a lock it already holds).
        self._autofix_lock = threading.Lock()
        # Serializes autofix polls so two never overlap. Acquired in the caller
        # thread and released by the worker (see run_autofix_poll_async), so it must
        # be a plain Lock (cross-thread release), never nested with _autofix_lock.
        self._poll_lock = threading.Lock()
        # PR numbers with a dispatch_agent call in flight - a click and an
        # overlapping poll can't race two spawns onto one PR. Guarded by its own
        # short mutex: _autofix_lock is the whole-poll overlap guard (held for the
        # entire poll by run_autofix_poll_async), and a poll reaches dispatch_agent
        # while holding it - reusing that non-reentrant lock here would self-deadlock.
        self._dispatching_prs: set[int] = set()
        self._dispatching_lock = threading.Lock()
        # Work keys a peer's agent already owns, so the "claimed elsewhere" note is
        # logged once per key rather than every poll (szpontnet-spec/docs/12).
        self._mesh_suppressed_logged: set[str] = set()

        # Claude-API-error watcher: live-only runtime state (the toggle/count persist
        # via QSettings below). Mirrors the per-tty backoff + idle-confirmation maps in
        # Store.swift, keyed by tmux pane_id.
        self.apiwatch_status: dict | None = None
        self._apiwatch_backoff: dict[str, dict] = {}  # pane_id -> {nextAllowed, interval}
        self._apiwatch_seen_tail: dict[str, str] = {}  # pane_id -> last erroring tail
        self._apiwatch_lock = threading.Lock()
        self._telemetry_sample_lock = threading.Lock()

        # Pruned on every start — a Store outlives thousands of polls.
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

        # Honor the process-wide default format (NativeFormat unless overridden):
        # the two-arg QSettings(org, app) constructor is hardwired to NativeFormat,
        # which on macOS ignores QSettings.setPath — so the test suite couldn't
        # redirect it and would read/write the real user settings.
        self._settings = QSettings(
            QSettings.defaultFormat(), QSettings.Scope.UserScope, self._ORG, self._APP
        )

        # Re-point a hidden default selection.
        if self.selected in self.hidden_tools:
            vis = self.visible_tools
            if vis:
                self.selected = vis[0].id

    # MARK: background work

    def start_background(self, work, name: str | None = None) -> threading.Thread:
        """Run ``work`` off the GUI thread, on a worker the process can wait for.

        These workers signal back into Qt when they are done, so one still in flight
        while the process tears its widgets down raises ``RuntimeError: Signal source
        has been deleted`` there. An unhandled exception on a thread prints a
        traceback to ``sys.stderr``, and an interpreter already finalising cannot take
        the buffer lock the worker is holding to write it — so the process dies of
        SIGABRT rather than exiting. Hence the handle: :meth:`wait_for_background` is
        where a caller about to end the process lets them land first.

        Daemon, still: the wait is bounded and a wedged worker must never be able to
        hold the applet open past it.
        """
        thread = threading.Thread(target=work, daemon=True, name=name)
        with self._workers_lock:
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(thread)
            thread.start()
        return thread

    def wait_for_background(self, timeout: float = 5.0) -> list[str]:
        """Wait out the workers :meth:`start_background` started, and name the ones
        still running when ``timeout`` is spent.

        Best effort by construction, so the return value is the caller's evidence of
        what it is leaving behind rather than a promise there is nothing.
        """
        deadline = time.monotonic() + timeout
        with self._workers_lock:
            pending = list(self._workers)
        for thread in pending:
            thread.join(max(0.0, deadline - time.monotonic()))
        return [t.name for t in pending if t.is_alive()]

    # MARK: persisted settings

    @property
    def username_override(self) -> str:
        return self._settings.value("usernameOverride", "", str)

    @username_override.setter
    def username_override(self, value: str) -> None:
        self._settings.setValue("usernameOverride", value)

    @property
    def hidden_tools(self) -> set[str]:
        # SKILL.md + Installer/CLI tools ship hidden (absent key => default); any
        # Settings toggle persists the explicit set from then on.
        if not self._settings.contains("hiddenTools"):
            return {"skillPRs", "installerPRs"}
        raw = self._settings.value("hiddenTools", [], list) or []
        return set(raw)

    @hidden_tools.setter
    def hidden_tools(self, value: set[str]) -> None:
        self._settings.setValue("hiddenTools", list(value))

    @property
    def color_overrides(self) -> dict[str, str]:
        raw = self._settings.value("colorOverrides", {}) or {}
        return dict(raw)

    @color_overrides.setter
    def color_overrides(self, value: dict[str, str]) -> None:
        self._settings.setValue("colorOverrides", value)

    @property
    def terminal_choice(self) -> str:
        return self._settings.value("terminalChoice", review.default_terminal().key, str)

    @terminal_choice.setter
    def terminal_choice(self, value: str) -> None:
        self._settings.setValue("terminalChoice", value)

    @property
    def repo_path_override(self) -> str:
        """The repo root every spawned agent ``cd``s into (Settings → REPO ROOT).
        Empty => ``review.default_repo_path()``; ``DIPLOMAT_REPO`` outranks both.
        Stored raw (a typed ``~/…`` is expanded at use) so the field shows back
        exactly what was entered.

        The one setting NOT in QSettings: a mesh node spawns agents from its own
        stdlib-only process, which has no Qt — see :mod:`appconfig`."""
        return appconfig.get(appconfig.REPO_ROOT)

    @repo_path_override.setter
    def repo_path_override(self, value: str) -> None:
        appconfig.set_value(appconfig.REPO_ROOT, value)

    @property
    def agent_runner(self) -> str:
        """Which agent CLI a spawn runs (Settings → AGENT RUNNER). In
        :mod:`appconfig` rather than QSettings for the same reason the repo root is:
        a mesh node spawns agents from a process with no Qt to ask."""
        return runner.selected()

    @agent_runner.setter
    def agent_runner(self, value: str) -> None:
        appconfig.set_value(appconfig.AGENT_RUNNER, value)

    @property
    def agent_model(self) -> str:
        """The model the selected runner is pinned to; empty leaves the choice to that
        runner's own picker. A model id, never a credential — those live in the
        runner's provider store."""
        return appconfig.get(appconfig.AGENT_MODEL)

    @agent_model.setter
    def agent_model(self, value: str) -> None:
        appconfig.set_value(appconfig.AGENT_MODEL, value)

    @property
    def allocator_setup_done(self) -> bool:
        """True once the one-time automatic device-allocator install has been
        settled — either it succeeded, or the user made an explicit choice in
        Settings. Gates the auto-install so it never re-installs after an
        intentional uninstall."""
        return self._settings.value("allocatorSetupDone", False, bool)

    @allocator_setup_done.setter
    def allocator_setup_done(self, value: bool) -> None:
        self._settings.setValue("allocatorSetupDone", bool(value))

    @property
    def settings_explain(self) -> bool:
        """Whether Settings draws each row's long-form explanation (the header's
        *Explain* switch). Off by default: the paragraphs answer questions a first
        read raises and are noise on every read after it. Persisted, so the answer
        to "do I want these" is given once rather than on every visit."""
        return self._settings.value("settingsExplain", False, bool)

    @settings_explain.setter
    def settings_explain(self, value: bool) -> None:
        self._settings.setValue("settingsExplain", bool(value))

    @property
    def mesh_enabled(self) -> bool:
        """Opt-in: whether this machine joins the LAN P2P mesh. Off by default so
        Diplomat never opens a UDP/TCP node on the network unasked; the app
        auto-starts a node only once the user enables it in Settings.

        ``_mesh_enabled_override`` lets the headless render force it on without
        writing (and persisting) to the real user QSettings.

        A machine with no SzpontNet installed is not on the mesh whatever its
        preference says, and this is where that becomes true rather than at each
        of the dozen call sites: every mesh-shaped path in the applet already
        asks this question, so answering it honestly is what makes the add-on
        optional. The stored preference is left alone — install the library and
        the machine rejoins the mesh it was already opted into."""
        if not szpont.AVAILABLE:
            return False
        if self._mesh_enabled_override is not None:
            return self._mesh_enabled_override
        return self._settings.value("meshEnabled", False, bool)

    @mesh_enabled.setter
    def mesh_enabled(self, value: bool) -> None:
        self._settings.setValue("meshEnabled", bool(value))

    # MARK: PR auto-fix monitor settings

    @property
    def pr_autofix_enabled(self) -> bool:
        """Watch my open PRs and auto-resolve conflicts + address review threads.
        On by default (matches macOS). Switched off, the poll keeps looking and
        queues what it finds for the panel (:meth:`is_paused`); what stops is the
        automatic start."""
        return self._settings.value("prAutofixEnabled", True, bool)

    @pr_autofix_enabled.setter
    def pr_autofix_enabled(self, value: bool) -> None:
        self._settings.setValue("prAutofixEnabled", bool(value))

    @property
    def review_requests_enabled(self) -> bool:
        """Full-E2E review PRs that request my review (read-only, never touches their
        branch), retrying an unaddressed review until it lands. On by default;
        switched off it queues rather than starts, like the toggle above."""
        return self._settings.value("reviewRequestsEnabled", True, bool)

    @review_requests_enabled.setter
    def review_requests_enabled(self, value: bool) -> None:
        self._settings.setValue("reviewRequestsEnabled", bool(value))

    @property
    def auto_task_limit(self) -> int:
        """How many automatic agents this machine runs at once — 2 by default.

        The monitors are level-triggered over everything GitHub currently owes,
        so without a cap one poll of a busy day dispatches every pending unit in
        a single pass: a terminal window and a ``claude`` session per conflicted
        PR and per owed review, all at once, on one machine. Work over the cap is
        not dropped — the poll that refuses it writes no attempt record, so the
        next tick offers it again as soon as an agent finishes, and it waits
        visibly in the panel's Agent-tasks list meanwhile.

        The second setting NOT in QSettings, for the same reason as the repo root:
        a mesh peer can route work here, and the node that spawns it is a separate
        Qt-less process (see :mod:`appconfig`)."""
        return appconfig.auto_task_limit()

    @auto_task_limit.setter
    def auto_task_limit(self, value: int) -> None:
        appconfig.set_int(
            appconfig.AUTO_TASK_LIMIT, autofix.clamp_auto_task_limit(int(value))
        )

    @property
    def queued_task_order(self) -> list[str]:
        """The operator's drag order for the deferred-work queue, by queue key.

        In QSettings, unlike the cap above it: the arrangement is this front-end's
        view of its own list, and a mesh node has no queue to read it from. Pruned to
        what is still offered on every commit, so it can't grow past the work it
        describes."""
        return [str(k) for k in (self._settings.value("queuedTaskOrder", [], list) or [])]

    @queued_task_order.setter
    def queued_task_order(self, value: list[str]) -> None:
        self._settings.setValue("queuedTaskOrder", list(value))

    @property
    def queue_auto_run(self) -> bool:
        """Whether a free bay starts the next queued task by itself. On by default.

        Off, nothing automatic starts on this machine: the monitors keep finding
        work and every find queues, including the reviews a sweep asked for — which
        the monitor toggles do not speak for. Rows then move on "execute now" only."""
        return self._settings.value("queueAutoRun", True, bool)

    @queue_auto_run.setter
    def queue_auto_run(self, value: bool) -> None:
        self._settings.setValue("queueAutoRun", bool(value))

    @property
    def requested_work(self) -> list[autofix.RequestedWork]:
        """The work the operator has asked for by sweeping a scope and nothing has
        started yet, in the order their sweeps asked for it.

        Persisted, alone among the things the queue is built from, because it is the
        only one GitHub cannot answer for: a poll can always re-derive that a PR
        conflicts or that a thread is waiting on me, but nothing about a PR records
        that somebody swept it into a review, nor about an issue that somebody swept it
        into a fix. Losing this list on a restart would silently drop the rest of a
        fifty-item sweep, which is exactly the run long enough to be interrupted.

        Stored under the name this list has always had, so the asks a Review-PRs sweep
        left standing outlive the upgrade that widened it to issue fixes."""
        raw = self._settings.value("requestedReviews", "", str)
        try:
            rows = json.loads(raw) if raw else []
        except ValueError:
            return []
        if not isinstance(rows, list):
            return []
        parsed = [autofix.RequestedWork.from_json(r) for r in rows if isinstance(r, dict)]
        return [r for r in parsed if r is not None]

    @requested_work.setter
    def requested_work(self, value: list[autofix.RequestedWork]) -> None:
        self._settings.setValue(
            "requestedReviews", json.dumps([r.to_json() for r in value])
        )

    @property
    def auto_approve_enabled(self) -> bool:
        """Whether a clean auto-review may submit a verdict. Off by default: an
        auto-review never approves / requests-changes on my behalf until I opt in."""
        return self._settings.value("autoApproveEnabled", False, bool)

    @auto_approve_enabled.setter
    def auto_approve_enabled(self, value: bool) -> None:
        self._settings.setValue("autoApproveEnabled", bool(value))

    @property
    def soft_approve_enabled(self) -> bool:
        """Whether a clean comments-only auto-review leaves a friendly thank-you note
        (no APPROVE action) instead of staying silent. On by default; moot on any PR
        that gets a real verdict."""
        return self._settings.value("softApproveEnabled", True, bool)

    @soft_approve_enabled.setter
    def soft_approve_enabled(self, value: bool) -> None:
        self._settings.setValue("softApproveEnabled", bool(value))

    @property
    def verdict_withhold_skill(self) -> bool:
        return self._settings.value("verdictWithholdSkill", True, bool)

    @verdict_withhold_skill.setter
    def verdict_withhold_skill(self, value: bool) -> None:
        self._settings.setValue("verdictWithholdSkill", bool(value))

    @property
    def verdict_withhold_installer(self) -> bool:
        return self._settings.value("verdictWithholdInstaller", True, bool)

    @verdict_withhold_installer.setter
    def verdict_withhold_installer(self, value: bool) -> None:
        self._settings.setValue("verdictWithholdInstaller", bool(value))

    @property
    def verdict_withhold_community(self) -> bool:
        return self._settings.value("verdictWithholdCommunity", True, bool)

    @verdict_withhold_community.setter
    def verdict_withhold_community(self, value: bool) -> None:
        self._settings.setValue("verdictWithholdCommunity", bool(value))

    @property
    def verdict_policy(self) -> autofix.VerdictPolicy:
        return autofix.VerdictPolicy(
            self.verdict_withhold_skill,
            self.verdict_withhold_installer,
            self.verdict_withhold_community,
        )

    # Monitor counters — persisted so the "fixed N" pills survive a restart.

    @property
    def autofix_conflicts_handled(self) -> int:
        return self._settings.value("autofixConflicts", 0, int)

    @autofix_conflicts_handled.setter
    def autofix_conflicts_handled(self, value: int) -> None:
        self._settings.setValue("autofixConflicts", int(value))

    @property
    def autofix_reviews_handled(self) -> int:
        return self._settings.value("autofixReviews", 0, int)

    @autofix_reviews_handled.setter
    def autofix_reviews_handled(self, value: int) -> None:
        self._settings.setValue("autofixReviews", int(value))

    @property
    def review_requests_handled(self) -> int:
        return self._settings.value("reviewRequestsHandled", 0, int)

    @review_requests_handled.setter
    def review_requests_handled(self, value: int) -> None:
        self._settings.setValue("reviewRequestsHandled", int(value))

    # Claude-API-error watcher (mirrors apiWatchEnabled / apiWatchContinues in
    # Store.swift). On by default, matching macOS.

    @property
    def api_watch_enabled(self) -> bool:
        """Whether the terminal watcher nudges any agent that stalls on a transient
        Claude API error to continue. On by default (matches macOS)."""
        return self._settings.value("apiWatchEnabled", True, bool)

    @api_watch_enabled.setter
    def api_watch_enabled(self, value: bool) -> None:
        self._settings.setValue("apiWatchEnabled", bool(value))

    @property
    def api_watch_continues(self) -> int:
        return self._settings.value("apiWatchContinues", 0, int)

    @api_watch_continues.setter
    def api_watch_continues(self, value: int) -> None:
        self._settings.setValue("apiWatchContinues", int(value))

    # MARK: derived settings

    @property
    def effective_me(self) -> str:
        o = self.username_override.strip()
        return o if o else self.me

    def tint(self, tool_id: str) -> str:
        """A tool's tint as #RRGGBB: the user's override if set, else its default."""
        override = self.color_overrides.get(tool_id)
        if override:
            return override
        t = tool_by_id(tool_id)
        return t.color_hex if t else "#888888"

    def set_tint(self, color_hex: str, tool_id: str) -> None:
        overrides = self.color_overrides
        overrides[tool_id] = color_hex
        self.color_overrides = overrides
        self.changed.emit()

    @property
    def terminal(self) -> review.SpawnTerminal:
        return review.terminal_by_key(self.terminal_choice) or review.default_terminal()

    @property
    def visible_tools(self) -> list[Tool]:
        hidden = self.hidden_tools
        return [t for t in tools() if t.id not in hidden]

    def set_tool(self, tool_id: str, visible: bool) -> None:
        hidden = self.hidden_tools
        if visible:
            hidden.discard(tool_id)
        else:
            hidden.add(tool_id)
            if self.selected == tool_id:
                vis = [t for t in tools() if t.id not in hidden]
                if vis:
                    self.selected = vis[0].id
        self.hidden_tools = hidden
        self.changed.emit()

    # MARK: data

    def fetch_me(self) -> None:
        """Cheap single-query fetch of the gh viewer login (the default identity)."""
        if self.me:
            return
        try:
            self.me = API.fetch_viewer_login()
            self.changed.emit()
        except Exception:  # noqa: BLE001 — best-effort identity resolution
            pass

    def refresh(self) -> None:
        """Synchronous full refresh. The GUI runs this on a worker thread."""
        self.is_loading = True
        self.error = None
        self.loading_changed.emit(True)
        try:
            me = API.fetch_viewer_login()
            prs = API.fetch_open_prs()
            issues = API.fetch_open_issues()
            self.me = me
            self.prs = prs
            self.issues = issues
            self.last_updated = datetime.now().astimezone()
            self.has_loaded = True
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            self.is_loading = False
            self.loading_changed.emit(False)
            self.changed.emit()

    # MARK: PR auto-fix monitor
    #
    # The Linux port of Store.swift's autofix monitor. A background poll (driven by
    # a QTimer in app.py, independent of the panel) fetches my open PRs + the PRs
    # requesting my review, edge-triggers on new conflicts / review threads, and
    # spawns the same conflict-fix / review agents the panel wizards do — deduped by
    # an in-flight sentinel and rate-limited by ReviewReconcile backoff. The pure
    # decision logic lives in autofix.py; the GitHub reads in autofixmonitor.py.

    def run_autofix_poll_async(self) -> None:
        """Kick one monitor poll on a worker thread (guarded against overlap). Safe to
        call from a QTimer whether or not the panel is open.

        It polls with the toggles off too: a switched-off monitor still finds what it
        would have done and queues it, because the panel is where the operator sees
        what their PRs owe and that question does not go away with the toggle that
        answers it automatically."""
        if not self._poll_lock.acquire(blocking=False):
            return  # a poll is already running

        def work() -> None:
            try:
                self._autofix_poll_once()
            finally:
                self._poll_lock.release()
                self.autofix_changed.emit()

        self.start_background(work)

    def _autofix_poll_once(self) -> None:
        self._poll_error_this_cycle = None
        # Settle the agents before anything reads them. This is the one pass that runs
        # whatever the operator is looking at: the panel's own tick is gated on the
        # panel being VISIBLE, and this is a tray applet whose panel is shut most of
        # the time — so with retirement only on that tick, a finished agent's record
        # was never dropped, its bay never came back, its PR stayed deduped and its
        # cost never reached the ledger, on exactly the machines that leave the tray
        # alone. (Seen live: three runs, panel closed, nothing retiring.)
        self._settle_agents()
        try:
            if not self.effective_me:
                self.fetch_me()
            if not self.effective_me:
                self._note_poll_failure(
                    "GitHub login unknown — is `gh` installed and authenticated?"
                )
            else:
                cfg = core.config()
                owner, repo = cfg["owner"], cfg["repo"]
                # One fetch of my PRs per cycle, taken before anything acts on it:
                # the drain re-checks the waiting queue against it and the
                # reconcilers below diff from the same list. Fetching it here rather
                # than inside the monitor is what lets the queue be checked at all —
                # the drain runs first, and the whole point of the check is that it
                # reads THIS cycle's evidence, not the one the queue was built from.
                snaps = self._fetch_my_snapshots(owner, repo)
                # And which PRs have left the open state — the one thing the fetch
                # above cannot answer, because it lists what is open and the queue
                # has to be checked against what is not. Every verb answers to it,
                # the operator's own ask included (:func:`autofix.still_owed`).
                closed = self._fetch_closed_prs(owner, repo)
                # The queue first, in the operator's order: a slot that freed since
                # the last cycle belongs to work already waiting for it, not to
                # whichever PR this poll's fetch happens to return first.
                #
                # Only on evidence a cycle actually confirmed. The queue survives a
                # failed cycle deliberately (see _staged_queue), but surviving is not
                # the same as being current: while `gh` is down the list freezes, and
                # a drain that kept firing from it would spawn agents at work
                # answered by hand hours ago. A fetch that failed just now is the
                # same blindness one cycle earlier, so it holds the drain too —
                # either of them, since a missing answer is not a pass.
                if (self.autofix_poll_error is None and snaps is not None
                        and closed is not None):
                    self._drain_queued_tasks(snaps, closed)
                # Start this cycle's staging empty — the one place it is reset, so a
                # cycle that failed part-way and never committed does not carry its
                # offers into this one. They are re-offered by the cycle that
                # succeeds, since the refusal that staged them wrote no attempt
                # record.
                self._staged_queue = []
                # Both monitors run whatever their toggles say; a switched-off one
                # queues what it finds instead of dispatching it (see is_paused).
                if snaps is not None:
                    self._poll_my_prs(snaps)
                self._poll_review_requests(owner, repo)
                # Last, so the monitors' finds keep the places they were found in:
                # the operator's own sweep bands behind them anyway, and a fifty-item
                # ask offered first would otherwise decide the arrangement of a queue
                # it is meant to wait in.
                self._offer_requested_work()
                # A cycle that failed part-way knows what it fetched, not what is
                # owed — committing then would drop every task the failing half would
                # have re-offered, and with it the operator's arrangement of them.
                if self._poll_error_this_cycle is None:
                    self.commit_queue()
        except Exception as exc:  # noqa: BLE001 — any poll failure surfaces, never kills the worker
            # The per-poll helpers guard only their fetch_* calls; a failure deeper in the
            # poll — notably a diplomat-core build-prompt subprocess error
            # (RuntimeError/CoreBinaryMissing) raised while EAGERLY building an
            # AgentJob(prompt=...) in a _dispatch_* helper — would otherwise escape here, past
            # _settle_poll_error, out of the try/finally-only work() wrapper, and KILL the daemon
            # poll worker thread (after which every poll silently no-ops and a stale error never
            # clears). Route it through the same poll-failure path as a fetch error instead.
            self._note_poll_failure(exc)
        self._settle_poll_error()

    def _fetch_my_snapshots(self, owner: str, repo: str) -> list | None:
        """This cycle's read of my open PRs, or ``None`` if the read failed — in which
        case the failure is already noted and every consumer of it stands down."""
        try:
            return autofixmonitor.fetch_snapshots(owner, repo, self.effective_me)
        except Exception as exc:  # noqa: BLE001 — any failure is a poll failure
            self._note_poll_failure(exc)
            return None

    def _fetch_closed_prs(self, owner: str, repo: str) -> set[int] | None:
        """This cycle's read of the PRs that have merged or closed, or ``None`` if the
        read failed — which is not the same as an empty set, and is why the failure
        stands the drain down rather than letting it read every PR as open."""
        try:
            return autofixmonitor.fetch_closed_prs(owner, repo)
        except Exception as exc:  # noqa: BLE001 — any failure is a poll failure
            self._note_poll_failure(exc)
            return None

    def _poll_my_prs(self, snaps: list) -> None:
        now = time.time()
        events, fps = autofix.compute_diff(self._load_fingerprints(), snaps)
        for kind, snap in events:
            if kind == "review":
                self._dispatch_my_review(snap, 1)
            # "conflict" events are intentionally a no-op here: the same poll's
            # _reconcile_my_conflicts sees the CONFLICTING state and handles it
            # (also covering conflicts that predate the baseline + failed spawns).
        self._save_fingerprints(fps)
        # Record what is owed BEFORE reconciling, so a unit of work is queued in
        # the ledger before the same poll can start it — otherwise the first
        # dispatch of every item would look like it came from nowhere and its
        # time-to-start would be unmeasurable.
        telemetry.observe_owed(autofix.WORK_REVIEW_REPLY, "review", {
            autofix.ledger_key(autofix.WORK_REVIEW_REPLY, s.url, s.head_sha): s.number
            for s in snaps if s.threads_i_owe > 0
        })
        telemetry.observe_owed(autofix.WORK_CONFLICTS, "conflicts", {
            autofix.ledger_key(autofix.WORK_CONFLICTS, s.url, s.head_sha): s.number
            for s in snaps if s.mergeable == "CONFLICTING"
        })
        self._reconcile_my_reviews(snaps, now)
        self._reconcile_my_conflicts(snaps, now)
        self.autofix_status = {
            "updatedAt": now,
            "watching": len(snaps),
            "conflictsHandled": self.autofix_conflicts_handled,
            "reviewsHandled": self.autofix_reviews_handled,
        }

    def _reconcile_my_reviews(self, snaps: list, now: float) -> None:
        """Level-triggered safety net: any PR of mine with unresolved threads I owe a
        reply on but no agent on it gets a (re)dispatch, deduped by in-flight + backoff."""
        key = "myReviewAttempts"
        attempts = self._load_attempts(key)
        owed = [s for s in snaps if s.threads_i_owe > 0]
        for s in owed:
            k = str(s.number)
            action, val = autofix.decide(
                attempts.get(k),
                autofix.STAMP_UNRESOLVED_REVIEW,
                self._in_flight(s.url),
                False,
                now,
            )
            if action == "dispatch" and self._dispatch_my_review(s, int(val)):
                attempts[k] = autofix.ReviewAttempt(
                    autofix.STAMP_UNRESOLVED_REVIEW, now, int(val)
                )
        owed_keys = {str(s.number) for s in owed}
        self._save_attempts(key, {k: v for k, v in attempts.items() if k in owed_keys})

    def _reconcile_my_conflicts(self, snaps: list, now: float) -> None:
        """Level-triggered: any CONFLICTING PR of mine with no agent on it gets a
        (re)dispatch. Records are kept while the PR is CONFLICTING/UNKNOWN and pruned
        once it goes MERGEABLE."""
        key = "myConflictAttempts"
        attempts = self._load_attempts(key)
        conflicted = [s for s in snaps if s.mergeable == "CONFLICTING"]
        for s in conflicted:
            k = str(s.number)
            action, val = autofix.decide(
                attempts.get(k),
                autofix.STAMP_CONFLICTING,
                self._in_flight(s.url),
                False,
                now,
            )
            if action == "dispatch" and self._dispatch_conflict_fix(
                s.number, s.url, int(val), "auto", head_sha=s.head_sha
            ):
                attempts[k] = autofix.ReviewAttempt(
                    autofix.STAMP_CONFLICTING, now, int(val)
                )
        keep = {str(s.number) for s in snaps if s.mergeable != "MERGEABLE"}
        self._save_attempts(key, {k: v for k, v in attempts.items() if k in keep})

    def _poll_review_requests(self, owner: str, repo: str) -> None:
        try:
            reqs = autofixmonitor.fetch_review_requests(
                owner, repo, self.effective_me, include_files=self.auto_approve_enabled
            )
        except Exception as exc:  # noqa: BLE001
            self._note_poll_failure(exc)
            return
        now = time.time()
        banned = bans.read()
        key = "reviewReqAttempts"
        attempts = self._load_attempts(key)
        owed = [r for r in reqs if r.owe_review]
        # Before dispatching, so the ledger has a queue instant to measure the
        # time-to-start against. A banned author's request is owed by GitHub's
        # reckoning but will never be dispatched, so it is left out — counting it
        # would show a review pending forever that nothing is meant to pick up.
        telemetry.observe_owed(autofix.WORK_REVIEW_REQ, "review", {
            autofix.ledger_key(autofix.WORK_REVIEW_REQ, r.url, r.head_sha): r.number
            for r in owed if not bans.is_banned(r.author, banned)
        })
        for r in owed:
            k = str(r.number)
            stamp = r.stamp
            action, val = autofix.decide(
                attempts.get(k),
                stamp,
                self._in_flight(r.url),
                bans.is_banned(r.author, banned),
                now,
            )
            if action == "dispatch" and self._dispatch_review_request(r, int(val)):
                attempts[k] = autofix.ReviewAttempt(stamp, now, int(val))
        # Prune records older than the retry ceiling (a request that's long gone).
        self._save_attempts(
            key,
            {
                k: v
                for k, v in attempts.items()
                if now - v.last_dispatched_at < autofix.RETRY_MAX_BACKOFF
            },
        )
        self.unaddressed_reviews = sum(
            1
            for r in owed
            if not self._in_flight(r.url) and not bans.is_banned(r.author, banned)
        )

    # MARK: monitor dispatch + tracking

    def _route_via_mesh(self, job: "autofix.AgentJob") -> tuple[str | None, bool, str]:
        """Route an auto job through the mesh (szpontnet-spec/docs/12): claim-gated
        dispatch to the best-surplus node.

        Every machine scans GitHub independently, but the mesh runs each unit of
        work **once** — ``ctl.dispatch`` claims the work key and places the run on
        the best node; the EXECUTOR holds that claim for its agent's lifetime, so a
        concurrent or repeat scan is suppressed and a node death frees it for
        failover. No node stands down on a duty ASSIGNMENT anymore — that deferred
        to a node that might not be scanning at all, silently dropping the work.

        Returns ``(verdict, ran_here, node)``. The verdict is ``"spawned"`` (the mesh
        took it), ``VERDICT_STAND_DOWN`` (a peer's agent already owns it), or ``None``
        to fall through to a LOCAL spawn — the fail-open path when the mesh is
        unavailable, so a wedged node never drops the operator's work.

        ``ran_here`` says the placement landed on THIS machine — the best node the
        mesh could find was the one that asked. Such a run is local in every way the
        applet cares about (it burns this device's cap and this device's quota) and
        differs from a local spawn only in who opened the terminal. The node ids to
        compare are already in hand: the dispatch result names its executor, and the
        snapshot names us.

        ``node`` is that executor's name, and it is what a peer-placed run is judged
        by afterwards: no probe on this machine can see a process on that one, so the
        run's liveness is the executor's origination claim and nothing else."""
        if not self.mesh_enabled or not job.work_key:
            return None, False, ""

        from szpontnet import ctl, statefile

        state = statefile.read_state()
        if not state or not statefile.node_running(state):
            return None, False, ""
        try:
            results = ctl.dispatch(job.duty, job.prompt, work_key=job.work_key)
        except ctl.CtlError:
            return None, False, ""  # node unreachable → fail-open to a local spawn
        if not results:
            return None, False, ""
        statuses = [r.get("status") for r in results]
        if statuses and all(s == "suppressed" for s in statuses):
            self._log_mesh_suppressed(job.work_key, results)
            return autofix.VERDICT_STAND_DOWN, False, ""
        if all(s in ("spawned", "suppressed") for s in statuses):
            me = (state.get("self") or {}).get("id")
            spawned = [r for r in results if r.get("status") == "spawned"]
            here = bool(me) and any(r.get("node") == me for r in spawned)
            node = next((r.get("nodeName") or r.get("node") or "" for r in spawned), "")
            return "spawned", here, node  # ran on the mesh (the node logs where)
        return None, False, ""  # declined everywhere → fall through to a local spawn

    def _log_mesh_suppressed(self, work_key: str, results: list) -> None:
        """A peer's agent owns this work — note it once per key, not per poll."""
        if work_key in self._mesh_suppressed_logged:
            return
        if len(self._mesh_suppressed_logged) > 256:
            self._mesh_suppressed_logged.clear()
        self._mesh_suppressed_logged.add(work_key)
        owner = next((r.get("nodeName") for r in results if r.get("nodeName")), "a peer")
        activity.log("auto", "mesh-suppressed",
                     f"Work claimed by {owner} — running there")

    # MARK: - the one dispatch pipeline (buttons and monitors are triggers, not paths)

    def dispatch_agent(self, job: autofix.AgentJob, source: str, attempt: int = 1,
                       bypass_capacity: bool = False,
                       bypass_budget: bool = False) -> str:
        """Run one agent job through the shared gate (``autofix.dispatch_decide`` -
        the pure, tested decision both platforms mirror) and, on proceed, spawn and
        register it. Returns the verdict: ``"spawned"``, an ``autofix.VERDICT_*``
        refusal, or ``"failed"`` (spawn error). Twin of Store.dispatchAgent (macOS).

        In-flight evidence is the tracked list OR a live ``claude`` visible in
        ``ps`` - the ground-truth floor that also catches agents whose local
        bookkeeping was lost (applet restart) and peers' agents that landed on this
        very machine. ``_dispatching_prs`` is held for the whole call so an
        overlapping poll and a click can't race two spawns onto one PR.

        An AUTO job is additionally capped at ``auto_task_limit`` concurrent
        agents on this device (``_auto_tasks_running``), held outright while its own
        monitor is switched off (:meth:`is_paused`) or the queue is
        (:attr:`queue_auto_run`), and held again when what is left of the limits it
        spends against will not cover it (:mod:`autobudget`); a panel click is subject to
        none of them. Every one of those refusals
        queues the job (:meth:`_stage_queued`), which is what the panel's
        Agent-tasks list shows as *queued*.

        The cap counts an agent by where it RUNS, not by who dispatched it: work the
        mesh places back on this machine is booked here (:meth:`_track_agent`) and
        spends a slot, work it places on a peer spends that peer's.

        ``bypass_capacity`` is for the two callers that have already answered the
        capacity question themselves: the queue drain (which counted the free slot
        it is filling, and skips paused work by construction) and "execute now"
        (where the operator is overriding both holds deliberately). It skips the
        measurement — not just its verdict — so neither pays for a second ``ps``
        scan, and so a forced run cannot re-queue itself.

        ``bypass_budget`` is only the second of those. The drain runs the machine's
        own automatic work, and work that could not be afforded when it was found
        cannot be afforded by having waited in a list; only the operator pressing
        "execute now" overrides the budget, exactly as only they override the cap."""
        if job.pr_number is not None:
            with self._dispatching_lock:
                if job.pr_number in self._dispatching_prs:
                    return autofix.VERDICT_IN_FLIGHT
                self._dispatching_prs.add(job.pr_number)
        try:
            banned = bool(job.author_login) and bans.is_banned(
                job.author_login, bans.read()
            )
            agent_on_pr = bool(job.pr_url) and self._in_flight(job.pr_url)
            # Measured only for an auto job that would otherwise run: the count
            # costs a `ps` scan, a panel click is never capped, and an in-flight
            # PR spawns nothing either way — so in both of those the answer would
            # be discarded. Finding room is also what re-arms the "deferring" note.
            at_capacity = False
            paused = False
            if source == autofix.SOURCE_AUTO and not agent_on_pr and not bypass_capacity:
                full = self._auto_tasks_running() >= self.auto_task_limit
                if not full:
                    self._capacity_logged = False
                # A switched-off monitor has no room for its own work, whatever the
                # device's, and neither has a switched-off queue — for anything. Both
                # are modelled as capacity because the answer is the same one in every
                # respect that matters here — hold the job, write no attempt record,
                # re-offer it next poll — which keeps two toggles that are the
                # front-end's own out of the dispatch gate both front-ends mirror.
                # The queue switch has to hold HERE and not only at the drain: a find
                # that meets a free bay never reaches the queue at all.
                paused = self.is_paused(job.counter) or not self.queue_auto_run
                at_capacity = full or paused
            # Measured after capacity and under the same conditions: a device with
            # no free bay has nothing to spend a budget on, so the probe and the
            # ledger fold are work that would be thrown away. The drain reaches here
            # with `bypass_capacity` set and this one clear — that is the whole
            # difference between deferring work and forcing it.
            budget = autofix.Budget(affordable=True)
            if (source == autofix.SOURCE_AUTO and not agent_on_pr
                    and not bypass_budget and not at_capacity
                    and autobudget.enabled()):
                budget = autobudget.decide()
                if budget.affordable:
                    self._budget_logged = False
            verdict = autofix.dispatch_decide(
                source, banned, agent_on_pr, False, at_capacity,
                not budget.affordable,
            )
            if verdict == autofix.VERDICT_AT_CAPACITY:
                # A paused monitor is not a saturated device: it queues silently,
                # because the operator switched it off on purpose and the row says
                # the rest.
                if not paused:
                    self._log_at_capacity()
                self._stage_queued(job, attempt)
                return verdict
            if verdict == autofix.VERDICT_UNAFFORDABLE:
                self._log_unaffordable(budget)
                self._stage_queued(job, attempt)
                return verdict
            if verdict == autofix.VERDICT_BANNED:
                # A monitor's find comes back on its own once the ban lifts, so "un-ban
                # to review" is a whole instruction for one. An ask does not: this
                # verdict is one of the three that retire it (:meth:`_settle_requested`),
                # so the sweep has to be told the review is gone rather than waiting.
                remedy = ("the ask is dropped, sweep again to re-queue it"
                          if job.requested else "un-ban to review")
                activity.log(
                    source, "ban-skip", f"{job.label} - author is banned ({remedy})"
                )
                self.refresh_activity()
                return verdict
            if verdict == autofix.VERDICT_IN_FLIGHT:
                # A monitor tick hitting a busy PR is routine (stays silent); a
                # click deserves an answer for why nothing opened.
                if source == autofix.SOURCE_PANEL:
                    activity.log(
                        "panel", "in-flight", f"{job.label} - an agent is already on this PR"
                    )
                    self.refresh_activity()
                return verdict
            # What this dispatch is called wherever it is named: the activity line it
            # writes, and — while it runs — the row it wears in the Agent-tasks list.
            # One string, so a retry cannot read as a first attempt in one of them.
            row_label = autofix.dispatch_label(source, job.label, attempt,
                                               requested=job.requested)
            # An AUTO job on a live mesh runs on the best-surplus node via
            # claim-gated dispatch (every machine scans; the mesh runs it once and
            # dedups via the executor's claim). A manual spawn — or a wedged/absent
            # mesh — runs and is tracked locally instead (fail-open). Both converge
            # on the shared audit/counter tail below.
            routed, ran_here, node = (
                self._route_via_mesh(job) if source == autofix.SOURCE_AUTO
                else (None, False, "")
            )
            if routed == autofix.VERDICT_STAND_DOWN:
                return routed  # a peer's agent owns it (logged once by the router)
            if routed == "spawned":
                # Booked wherever the mesh put it. A placement back on this machine is
                # one of the agents the cap counts, and must be booked before the next
                # job of this poll asks how many are running — left unbooked, every
                # dispatch of a burst measured the same empty machine and the cap held
                # back nothing at all. A placement on a peer spends that peer's budget,
                # but it is still work this device is waiting on, so it gets a row
                # rather than leaving a gap.
                self._track_mesh_run(job.pr_url, job.pr_number, source,
                                     job.ledger_key, job.prompt, node=node,
                                     work_key=job.work_key, here=ran_here,
                                     label=row_label, kind=job.kind)
                ok = True
            else:
                # Registered whether or not it is PR-scoped. A sweep or an audit has
                # nothing to dedup against, but it is still an agent this applet opened
                # a window on, and a run with no record is a row the panel cannot draw
                # and a directory nothing ever cleans up. What it costs depends on how
                # it was dispatched, not on having a PR: the cap counts automatic runs
                # and pricing follows the ledger key.
                ok = self._spawn_tracked(job.prompt, job.pr_url, job.pr_number,
                                         source, job.ledger_key,
                                         label=row_label, kind=job.kind)
            if not ok:
                activity.log(source, "spawn-failed", f"{job.label} failed to spawn")
                self.refresh_activity()
                return "failed"
            activity.log(source, job.audit_action, row_label)
            # The telemetry ledger tracks the MONITORS, so only an auto dispatch is
            # recorded — a wizard click is the operator's own doing and has no queue
            # instant to be late against. A mesh placement on a PEER spends that
            # peer's quota, so it is flagged and kept out of the per-task cost
            # figures; one the mesh placed back here spent ours and is priced like
            # any other agent that ran on this machine.
            if source == autofix.SOURCE_AUTO and job.ledger_key:
                telemetry.record_started(job.ledger_key,
                                         remote=routed == "spawned" and not ran_here,
                                         attempt=attempt)
            # Retries are re-dispatches, not new work handled - count once, and
            # only for the monitor (a manual run is the user's own action).
            if autofix.dispatch_bumps_counter(source, attempt):
                if job.counter == "review_requests":
                    self.review_requests_handled += 1
                elif job.counter == "my_reviews":
                    self.autofix_reviews_handled += 1
                elif job.counter == "conflicts":
                    self.autofix_conflicts_handled += 1
            if source == autofix.SOURCE_PANEL:
                self.refresh_activity()
            return "spawned"
        finally:
            if job.pr_number is not None:
                with self._dispatching_lock:
                    self._dispatching_prs.discard(job.pr_number)

    def _dispatch_conflict_fix(
        self, number: int, url: str, attempt: int, source: str, head_sha: str = ""
    ) -> bool:
        job = autofix.AgentJob(
            kind="conflicts",
            audit_action="conflicts",
            label=f"Resolve · #{number}",
            prompt=conflicts.ConflictConfig(
                target=PRTarget.SPECIFIC, me=self.effective_me, specific_pr=str(number)
            ).build_prompt(),
            pr_url=url,
            pr_number=number,
            duty="conflicts",
            work_key=autofix.work_key(autofix.WORK_CONFLICTS, url, head_sha),
            ledger_key=autofix.ledger_key(autofix.WORK_CONFLICTS, url, head_sha),
            counter="conflicts",
            attempt_stamp=autofix.STAMP_CONFLICTING,
        )
        return self.dispatch_agent(job, source, attempt) in ("spawned", autofix.VERDICT_STAND_DOWN)

    def _dispatch_my_review(self, s, attempt: int = 1) -> bool:
        job = autofix.AgentJob(
            kind="review",
            audit_action="review-reply",
            label=f"Review · #{s.number}",
            prompt=review.ReviewConfig(
                depth="deep",
                target=PRTarget.SPECIFIC,
                me=self.effective_me,
                mark_ready=False,
                leave_reviews=False,
                reply_to_reviews=True,
                specific_pr=str(s.number),
                specific_author=review.SpecificAuthor.MINE,
            ).build_prompt(),
            pr_url=s.url,
            pr_number=s.number,
            duty="review",
            work_key=autofix.work_key(autofix.WORK_REVIEW_REPLY, s.url, s.head_sha),
            ledger_key=autofix.ledger_key(autofix.WORK_REVIEW_REPLY, s.url, s.head_sha),
            counter="my_reviews",
            attempt_stamp=autofix.STAMP_UNRESOLVED_REVIEW,
        )
        return self.dispatch_agent(job, autofix.SOURCE_AUTO, attempt) in ("spawned", autofix.VERDICT_STAND_DOWN)

    def _dispatch_review_request(self, r, attempt: int = 1) -> bool:
        reasons = self.verdict_policy.withhold_reasons(r.files, r.author_association)
        verdict = self.auto_approve_enabled and not reasons
        # Without a real verdict, a clean review still soft-approves (friendly comment, no
        # APPROVE) unless the user turned that off too. Moot when verdict is True.
        soft = self.soft_approve_enabled
        if verdict:
            tag = " +verdict"
        else:
            why = "auto-approvals off" if not self.auto_approve_enabled else ", ".join(reasons)
            tag = f" ~soft-approve ({why})" if soft else f" -verdict ({why})"
        job = autofix.AgentJob(
            kind="review",
            audit_action="review-req",
            label=f"Review-req · #{r.number} (@{r.author}){tag}",
            prompt=review.ReviewConfig(
                depth="max",
                target=PRTarget.SPECIFIC,
                me=self.effective_me,
                mark_ready=False,
                leave_reviews=True,
                reply_to_reviews=False,
                specific_pr=str(r.number),
                final_pass=verdict,
                soft_approve=soft,
                specific_author=review.SpecificAuthor.THEIRS,
            ).build_prompt(),
            pr_url=r.url,
            pr_number=r.number,
            author_login=r.author,
            duty="review",
            work_key=autofix.work_key(autofix.WORK_REVIEW_REQ, r.url, r.head_sha),
            ledger_key=autofix.ledger_key(autofix.WORK_REVIEW_REQ, r.url, r.head_sha),
            counter="review_requests",
            attempt_stamp=r.stamp,
        )
        return self.dispatch_agent(job, autofix.SOURCE_AUTO, attempt) in ("spawned", autofix.VERDICT_STAND_DOWN)

    def _spawn_tracked(self, prompt: str, url: str | None, number: int | None,
                       source: str, ledger_key: str = "", label: str = "",
                       kind: str = "") -> bool:
        """Register a run, then spawn its agent into it. Returns whether the terminal
        launched.

        The record is written BEFORE the spawn, not after. A terminal takes seconds to
        open and the poll that dispatched it can ask about the same PR again inside
        that window; a run booked only on success is a PR that reads free while its
        agent is starting.

        The agent's shell writes its own pid into the run directory and then execs, so
        what identifies this run afterwards is that pid rather than the wording of its
        prompt (:func:`review.shell_command`). The hooks staged beside it are what let
        the run say when its turn is over (:mod:`diplomat_runtime.completion`), which
        is the only evidence that distinguishes a finished agent from a working one —
        both are the same live process at the same pid.

        Which runner is spawned is written down here rather than re-read later: the
        setting is what the NEXT spawn will use, so a run started under one runner and
        asked about after the operator switched would be interrogated through the
        wrong store. An OpenCode run also gets a port reserved for its own server. A
        port that cannot be had is not a failure to spawn — the run goes ahead without
        one and is read off its screen, exactly as a Claude Code run is.
        """
        now = time.time()
        record = agentregistry.create_run(
            agentstate.RunRecord(
                run_id=agentregistry.new_run_id(now), dispatched_at=now,
                pr_number=number, pr_url=url or "", kind=kind, label=label,
                source=source, placement=agentstate.PLACEMENT_LOCAL,
                ledger_key=ledger_key),
            prompt)
        chosen = agentregistry.stage_runner(record.run_id)
        port = (agentregistry.stage_port(record.run_id)
                if chosen == runner.OPENCODE else None)
        try:
            review.spawn(prompt, self.terminal,
                         done_path=str(agentregistry.done_path(record.run_id)),
                         pid_path=str(agentregistry.pid_path(record.run_id)),
                         prompt_file=str(agentregistry.prompt_path(record.run_id)),
                         port=port,
                         settings_file=agentregistry.stage_hooks(record.run_id))
        except review.SpawnError:
            agentregistry.forget({record.run_id})
            return False
        return True

    def _track_mesh_run(self, url: str | None, number: int | None, source: str,
                        ledger_key: str, prompt: str, node: str, work_key: str,
                        here: bool, label: str = "", kind: str = "") -> None:
        """Book a run the mesh placed, which this applet did not spawn itself.

        There is no pid to record: the executor opened the terminal. A placement that
        landed back HERE is still a process on this box, so it spends a bay and the
        untracked scan will find it; one on a peer is judged only by the executor's
        origination claim, which is the sole evidence that crosses the machine
        boundary.

        Which runner it is under is recorded for a landing HERE and only there. The
        node spawns through the same seam this applet does (:func:`runner.agent_command`
        by way of :mod:`szponthost`), so the answer is the same one and the run is
        asked of the right store and priced by it. A run on a PEER is a process on
        another machine: our stores hold nothing about it, and a runner written here
        would point its probe at a session that is somebody else's.
        """
        now = time.time()
        record = agentregistry.create_run(
            agentstate.RunRecord(
                run_id=agentregistry.new_run_id(now), dispatched_at=now,
                pr_number=number, pr_url=url or "", kind=kind, label=label,
                source=source,
                placement=(agentstate.PLACEMENT_MESH_HERE if here
                           else agentstate.PLACEMENT_MESH_PEER),
                node=node, work_key=work_key, ledger_key=ledger_key),
            prompt)
        if here:
            agentregistry.stage_runner(record.run_id)

    def _auto_tasks_running(self) -> int:
        """How many bays of this device's cap are held right now — the number the cap
        is compared against."""
        return len(self._agent_tick().cap_load)

    def refresh_auto_task_count(self) -> None:
        """Re-resolve for the display alone, signalling only on a change.

        The panel calls it on its own tick, including the ticks where nothing is
        registered: an agent can be alive with no record behind it (one this applet
        never spawned), and that is exactly when a wrongly-drawn free bay would be
        most misleading.

        A change is a change of the resolved states, not of their number: one agent
        finishing as another starts leaves the count alone while both rows are now
        wrong, and an agent going quiet moves no count at all while the bay it hands
        back is drawn from that same measure."""
        before = self._state_signature()
        self._settle_agents()
        if self._state_signature() != before:
            self.tasks_changed.emit()

    def _state_signature(self) -> frozenset:
        """What "the agent picture changed" means, for the redraw: every run and the
        state it is in. A count would miss two agents swapping PRs, and an agent
        going quiet."""
        t = self._tick
        if t is None:
            return frozenset()
        return frozenset((r.run_id, s.state) for r, s in t.rows)

    def refresh_auto_task_count_async(self) -> None:
        """Resolve off the UI thread — the probes shell out (see
        :mod:`diplomat_app.probes`)."""
        self.start_background(self.refresh_auto_task_count)

    @property
    def running_tasks(self) -> list[autofix.RunningAgent]:
        """Every agent run the panel draws, in reading order.

        Every one — both sources and both placements, tracked or not. The two
        front-ends used to disagree about this list (this one hid panel spawns and
        drew untracked agents; macOS did the reverse), which meant the rows and the
        cap were answering different questions about the same machine.

        A record carries the label, kind and start time its dispatch logged. A run
        nobody dispatched has none of those and is drawn by its PR alone, which is
        still a great deal more than the blank the operator gets otherwise.

        Runs that are over are left out. The same tick that resolves one retires it
        (:meth:`_retire_finished`), so drawing it would put a row on screen for one
        redraw and then take it away again — and which redraw catches it depends on
        when the poll happened to land, which is exactly the kind of answer this
        module exists to stop producing. What a finished run leaves behind is its
        activity line and its ledger entry.
        """
        return [
            autofix.RunningAgent(
                pr_number=r.pr_number or 0,
                label=r.label,
                kind=r.kind,
                tracked=not r.untracked,
                started_at=0.0 if r.untracked else r.dispatched_at,
                mesh=r.placement != agentstate.PLACEMENT_LOCAL,
                state=s.state,
                reason=s.reason,
            )
            for r, s in self._agent_tick().rows
            if s.state not in (agentstate.FINISHED, agentstate.MERGED)
        ]

    @property
    def auto_tasks_shown(self) -> int:
        """How many bays of this device's cap the drawn agents hold.

        Not every row: a session waiting at its prompt keeps its place in the list
        and gives its bay back, so the list is longer than the cap exactly as often
        as a finished window is left open."""
        return len(self._agent_tick().cap_load)

    @property
    def free_auto_slots(self) -> int:
        """Slots of this device's cap with nothing in them, as the panel draws them.

        Work that is starting holds one. Its spawn has not registered anywhere yet,
        but the bay is spoken for — and drawn as free it would put a row that is
        launching next to the empty slot it is launching into, which is one row more
        than the cap allows."""
        return autofix.free_slots(
            self.auto_task_limit, self.auto_tasks_shown + len(self.starting_tasks)
        )

    def _log_at_capacity(self) -> None:
        """Note that automatic work is being held back — once per episode, not once
        per PR per poll. Cleared the moment a dispatch finds room again, so the feed
        gets one line when the device saturates and another when it drains."""
        if self._capacity_logged:
            return
        self._capacity_logged = True
        limit = self.auto_task_limit
        activity.log(
            "auto", "at-capacity",
            f"Deferring auto work — this machine already runs its cap of "
            f"{limit} automatic {'task' if limit == 1 else 'tasks'}",
        )
        self.refresh_activity()

    def _log_unaffordable(self, budget: autofix.Budget) -> None:
        """Note that automatic work is being held for want of budget — once per
        episode, like :meth:`_log_at_capacity`, and cleared the moment a dispatch
        finds room again. Without that, a machine sitting under its floor would write
        one of these per owed PR per poll, for hours."""
        if self._budget_logged:
            return
        self._budget_logged = True
        activity.log("auto", "no-budget",
                     f"Deferring auto work — {autobudget.shortfall(budget)}")
        self.refresh_activity()

    # MARK: - the queue behind the cap
    #
    # A refusal writes no attempt record, so every poll re-offers whatever GitHub
    # still owes: that is where the queue's contents come from, and why nothing here
    # is a second copy of monitor state. What the queue adds is a list the panel can
    # show and the operator can arrange, drained in THEIR order at the top of a cycle,
    # before the monitors go looking for more.
    #
    # Two holds put work here. The device's cap holds work it has no slot for, and the
    # drain releases it as slots free. A switched-off monitor holds its own work
    # indefinitely: it is queued so the panel can show what the PRs owe, and only a
    # click ("execute now") or the toggle coming back on starts it.

    def _stage_queued(self, job: autofix.AgentJob, attempt: int) -> None:
        """Remember one at-capacity refusal as a queued task. Called from the single
        dispatch gate, so every deferral is queued however it was triggered — the two
        reconcilers, the review-request monitor, or the review edge-trigger. One poll
        can offer the same key twice; which of the two runs is decided in
        :meth:`commit_queue`."""
        # Only PR-scoped work with a monitor behind it is queued by a REFUSAL: a task
        # nothing can name is one the next poll cannot recognise as the same one, and a
        # task no monitor owns is one nothing would re-offer from here — so the refusal
        # would be the only record of it, and a poll that never happened would lose it.
        # (Every automatic job is both. A review the operator asked for is neither, and
        # is re-offered from the list that remembers the ask instead —
        # :meth:`_offer_requested_work`. A wizard click is uncapped and never
        # refused.)
        if job.pr_number is None or job.counter is None:
            return
        entry = autofix.QueuedTask(
            id=autofix.queue_key(job.audit_action, job.pr_number),
            job=job,
            attempt=attempt,
        )
        self._staged_queue = self._staged_queue + [entry]

    def commit_queue(self) -> None:
        """Publish this poll's deferrals as the queue, arranged by the operator's
        saved order. Called only after a fully successful cycle — see
        ``_staged_queue``.

        The LAST offer of a key wins, and its place in the queue is where it was
        first offered: within one poll the reconcilers run after the edge-trigger and
        carry the backoff-aware attempt number, so theirs is the job that should run,
        while the position is the same task's either way.

        Work already starting is left out of the published list. It is still offered —
        the attempt record that stops it being offered is written when its spawn
        answers — so a poll landing mid-dispatch would draw it a second time, back in
        the queue it just left. It keeps its place in the saved ARRANGEMENT, because a
        start that fails is re-offered, and dropping the key would send it to the
        back."""
        staged = self._staged_queue
        by_id = {e.id: e for e in staged}
        ordered = autofix.queue_order([e.id for e in staged], self.queued_task_order)
        self.queued_task_order = ordered
        starting = {e.id for e in self.starting_tasks}
        before = self.queued_tasks
        self.queued_tasks = [by_id[k] for k in ordered if k not in starting]
        if self.queued_tasks != before:
            self.tasks_changed.emit()

    # MARK: - the work the operator asks for
    #
    # A Review-PRs or Fix-issues sweep is expanded here into one queued task per PR /
    # per issue, rather than handed to a single agent as "review every draft PR of
    # mine" or "fix every open issue". On a repo with fifty of either that agent is
    # fifty jobs in one session: one context, one terminal, one machine, and no way to
    # see where it has got to or to stop it halfway. Split, each item is a row of the
    # Agent-tasks list, gets a bay of the task cap to itself, and runs when the cap has
    # room — behind the monitors' finds, ahead of the conflict fixes
    # (:func:`autofix.queue_band`).

    def request_review_sweep(self, cfg: review.ReviewConfig) -> tuple[int, int]:
        """Queue one review per PR ``cfg`` sweeps. Returns ``(queued, already)``.

        The PRs come from the panel's own last fetch rather than a fresh one: it is
        the list the operator was looking at when they pressed the button, which is
        the list they meant. One that merges or closes before its turn comes is
        dropped by the drain (:func:`autofix.still_owed`); one the sweep should not
        have caught in the first place is what **cancel** is for.

        Assembles a prompt per PR, so it runs OFF the UI thread (see
        :meth:`request_sweep_async`)."""
        targets = Filters.swept_prs(self.prs, cfg.sweep_author,
                                    cfg.include_drafts, cfg.include_ready)
        return self._request([
            autofix.RequestedWork(
                number=pr.number,
                url=pr.url,
                # The ban dimension, and only someone else's PR has one.
                author=pr.author if cfg.disposition == review.SpecificAuthor.THEIRS else "",
                config=cfg.for_pr(pr.number).prompt_payload(),
            )
            for pr in targets
        ])

    def request_issue_sweep(self, cfg: issues.IssueConfig) -> tuple[int, int]:
        """Queue one fix per issue ``cfg`` sweeps. Returns ``(queued, already)``.

        The issues come from the panel's own last fetch, for the same reason the PRs
        above do. What the drain cannot do for these is retire them: the monitor poll
        reads PRs, so nothing there notices an issue closing under a waiting ask. The
        swept prompt covers it instead — it re-reads the issue's state and stops if it
        has been dealt with since — and **cancel** is the way out of an ask the sweep
        should not have caught.

        Assembles a prompt per issue, so it runs OFF the UI thread (see
        :meth:`request_sweep_async`)."""
        targets = Filters.swept_issues(self.issues, cfg.target, cfg.sweep_author,
                                       cfg.unassigned_only)
        return self._request([
            autofix.RequestedWork(
                number=issue.number,
                url=issue.url,
                # The ban dimension. Only a scope that names one person has one,
                # matching the login the wizard would ban-check.
                author=issue.author if cfg.target == issues.Target.SOMEONE else "",
                config=cfg.for_issue(issue.number).prompt_payload(),
            )
            for issue in targets
        ])

    def _request(self, asks: list[autofix.RequestedWork]) -> tuple[int, int]:
        """Store and publish whichever of ``asks`` is not already queued, and say how
        many of each there were.

        An item already waiting keeps the ask it has instead of gaining a second: the
        queue is keyed by the ask, so two would be one row that dispatches twice (and
        the second dispatch would find the first agent still on it). That makes
        pressing SPAWN twice, or sweeping a scope that overlaps an earlier one,
        idempotent rather than a way to double up."""
        known = {r.key for r in self.requested_work}
        fresh = [a for a in asks if a.key not in known]
        if fresh:
            # Assembled BEFORE the asks are stored, so a core binary that cannot build
            # a prompt costs this press an error message rather than a list of asks
            # that fails the same way on every poll from here on.
            built = {a.key: self._requested_task(a) for a in fresh}
            with self._requested_lock:
                # Re-read under the lock and drop whatever arrived while the prompts
                # were being assembled. The set read above is stale by then: assembly
                # is a core subprocess per item, which is long enough for a second
                # sweep of an overlapping scope to have stored its own asks in the
                # meantime, and appending against the stale set would store those
                # items twice.
                stored = self.requested_work
                taken = {r.key for r in stored}
                fresh = [a for a in fresh if a.key not in taken]
                self.requested_work = stored + fresh
            self._publish_requested([built[a.key] for a in fresh])
        return len(fresh), len(asks) - len(fresh)

    def request_sweep_async(self, cfg, done) -> None:
        """The sweep behind ``cfg`` on a worker thread, reporting
        ``(queued, already, error)`` through ``done``.

        On a thread because it assembles a prompt per item, and each of those is a
        ``diplomat-core`` subprocess: fifty of them on the UI thread is a frozen tray
        for as long as it takes. ``done`` is called from the worker, so a Qt caller
        marshals it back itself (the wizards emit a signal)."""
        fan_out = (self.request_issue_sweep if isinstance(cfg, issues.IssueConfig)
                   else self.request_review_sweep)

        def work() -> None:
            try:
                queued, already = fan_out(cfg)
            except Exception as exc:  # noqa: BLE001 — a missing/wedged core binary
                done(0, 0, str(exc))
                return
            done(queued, already, "")

        self.start_background(work)

    def _requested_task(self, entry: autofix.RequestedWork) -> autofix.QueuedTask:
        """One ask as the queued task it stands for, memoised by ask and config.

        The memo is what keeps the prompt assembly to once per ask rather than once
        per poll: a task can wait in this list for hours, every poll re-offers it, and
        re-running the core binary fifty times each cycle to rebuild strings that
        cannot have changed is a subprocess storm for nothing.

        Keyed by the config as well as the ask, because the same item can be asked for
        twice with different answers — cancel a sweep, change the depth, sweep again —
        and a memo that matched on the key alone would hand the second ask the first
        one's prompt.

        Carries no ``counter``, no ``work_key`` and no ``ledger_key``: no monitor owns
        it (so no toggle pauses it and no auto-handled counter counts it), and the
        telemetry ledger measures the monitors, not the operator."""
        cached = self._requested_tasks.get(entry.key)
        if cached is not None and cached[0] == entry.config:
            return cached[1]
        # A fix is deliberately NOT PR-scoped. The dispatch pipeline's dedup is
        # PR-shaped throughout — the in-flight check matches a ``/pull/<n>`` URL and
        # keys on a PR number — so handing it an issue number would collide with the PR
        # that happens to share it. What keeps two agents off one issue instead is the
        # assignee claim, which every machine can see rather than just this one.
        is_pr = entry.action == autofix.QUEUE_REVIEW_ACTION
        task = autofix.QueuedTask(
            id=entry.key,
            job=autofix.AgentJob(
                kind=entry.action,
                audit_action=entry.action,
                label=entry.label,
                prompt=promptcore.build_prompt(entry.config),
                pr_url=entry.url if is_pr else None,
                pr_number=entry.number if is_pr else None,
                author_login=entry.author or None,
                duty=entry.action,
            ),
            attempt=1,
        )
        self._requested_tasks[entry.key] = (entry.config, task)
        return task

    def _publish_requested(self, entries: list[autofix.QueuedTask]) -> None:
        """Put freshly asked-for reviews into the queue there and then.

        Without this they would appear on the next poll, up to a poll period after the
        press — and a press that leaves the panel exactly as it was reads as a press
        that did nothing. Re-arranged through the same :func:`autofix.queue_order` the
        commit uses, so the rows land in their band and in the operator's arrangement,
        not merely at the end."""
        tasks = self.queued_tasks + entries
        by_id = {t.id: t for t in tasks}
        ordered = autofix.queue_order([t.id for t in tasks], self.queued_task_order)
        self.queued_task_order = ordered
        self.queued_tasks = [by_id[k] for k in ordered]
        self.tasks_changed.emit()

    def _offer_requested_work(self) -> None:
        """Offer every ask nothing has started, alongside the monitors' own finds.

        This is the list's whole job in a poll. A monitor re-offers its work by finding
        it on GitHub again; nothing on GitHub says a PR or an issue was swept, so what
        re-offers these is the ask itself, until the dispatch that starts one takes it
        off (:meth:`_settle_requested`), the operator cancels it, or — for a review —
        the drain finds its PR closed (:meth:`_drain_queued_tasks`).

        An ask whose prompt will not assemble is skipped rather than allowed to end
        the cycle. The press assembles every prompt before storing anything, but the
        memo that holds the results is process-local, so after a restart the core
        binary is met again here — once per standing ask, inside the poll. A raise
        from one of them would escape into the cycle's handler, which answers a
        failed poll by leaving the queue uncommitted: not one row short, but empty,
        the monitors' finds included, and with no row for the operator to cancel the
        offending ask by. Skipping keeps the ask for a later poll, which is what a
        core mid-self-update wants anyway."""
        for entry in self.requested_work:
            try:
                task = self._requested_task(entry)
            except Exception as exc:  # noqa: BLE001 — a missing/wedged core binary
                self._note_unbuildable_ask(entry, exc)
                continue
            self._staged_queue = self._staged_queue + [task]

    def _note_unbuildable_ask(self, entry: autofix.RequestedWork, exc: Exception) -> None:
        """Say once, per ask, that its prompt would not assemble.

        Once because the poll meets the same ask every three minutes for as long as
        it stands, and a feed that repeats itself that often is one nobody reads."""
        if entry.key in self._unbuildable_logged:
            return
        self._unbuildable_logged.add(entry.key)
        activity.log("panel", "queue-skip",
                     f"{entry.label} — prompt would not assemble: {exc}")

    def _settle_requested(self, entry: autofix.QueuedTask, verdict: str) -> None:
        """Take an ask off the list once its dispatch has answered for it.

        Started (here or on a peer that already owns the work) is the obvious one. A
        BAN is the other: the agent would refuse to run for as long as the ban stands,
        and a row nothing will ever start is a row that lies about what this machine is
        going to do. Every other verdict leaves the ask alone — an in-flight PR, a
        window with no budget left and a terminal that failed to open are all reasons
        to try again next poll, which is exactly what staying in the list means."""
        if not entry.job.requested:
            return
        if verdict not in ("spawned", autofix.VERDICT_STAND_DOWN, autofix.VERDICT_BANNED):
            return
        self._forget_requested(entry.id)

    def _forget_requested(self, key: str) -> None:
        """Drop one ask by the queue key that is its identity, and with it the task
        built from it."""
        with self._requested_lock:
            self.requested_work = [r for r in self.requested_work if r.key != key]
        self._requested_tasks.pop(key, None)
        # A later sweep of the same item is a new ask, and is owed the same one report
        # if its prompt will not assemble either.
        self._unbuildable_logged.discard(key)

    def cancel_requested_work(self, task_id: str) -> None:
        """The queued row's "cancel": drop an ask the operator has changed their mind
        about, without starting it.

        A sweep is the one thing in this list that can be asked for by the fifty, and
        the only one GitHub does not answer for: a monitor's row leaves when the work
        is no longer owed, while an ask on a PR that is still open stands until it
        runs — and a fix stands whatever happens to its issue, the poll reading PRs
        alone. Without a way back out, a mis-aimed sweep is a day of agents nobody can
        call off."""
        entry = next((e for e in self.queued_tasks if e.id == task_id), None)
        if entry is None or not entry.job.requested:
            return
        self._forget_requested(entry.id)
        self._drop_queued_task(task_id)
        activity.log("panel", "queue-cancel", f"{entry.job.label} — cancelled, not run")
        self.refresh_activity()

    def _drain_queued_tasks(self, snaps: list, closed: set[int]) -> None:
        """Run the queue down into whatever room this device has, in the operator's
        order. This is what makes the drag order mean anything: it runs at the TOP of
        a poll, before the monitors offer their own finds, so a slot that freed since
        the last cycle goes to the work already waiting for it rather than to whatever
        this poll's fetch happens to list first.

        The list is re-checked against ``snaps`` — this cycle's read of my PRs — and
        against ``closed``, the PRs that have left the open state, before any of it is
        run: a queued task carries the verdict of the poll that staged it, which is as
        old as a whole poll period by the time a bay frees, and what filled that bay in
        the meantime was an agent working one of these same branches — or the author,
        landing the PR. Work the fetch no longer owes, and work on a PR that is no
        longer there to work on, leaves the list instead of spawning
        (:func:`autofix.still_owed`).

        That pass covers the whole queue, not the part the drain reaches: a row
        standing for work somebody already did is wrong on the panel exactly as it is
        wrong to start, and it is the rows of a machine with no room — which returns
        below on its first entry — that sit there longest.

        Capacity is re-counted per task because each spawn fills a slot. A spawn
        failure stops the drain: it means terminal automation is broken, not that this
        one task was unlucky, and each entry is taken off the list before it is tried —
        so walking the whole queue into the same failure would clear the panel of every
        queued row at once, for a reason none of them caused."""
        conflicting = {s.number for s in snaps if s.mergeable == "CONFLICTING"}
        owing_reply = {s.number for s in snaps if s.threads_i_owe > 0}
        # Dropped here rather than left for this cycle's commit to omit: the commit is
        # the far side of two monitor runs, and a row already known to be answered
        # should not be sitting in the list underneath them, one "execute now" away
        # from spawning. Paused work is swept too — a switched-off monitor's row is
        # still a claim about what the PR owes.
        for entry in list(self.queued_tasks):
            # A task with no PR to ask about stands: that is a Fix-issues ask, numbered
            # in the issue space, and pricing issue #421 against the PRs closed this
            # cycle would retire it for something that happened to the PR of the same
            # number.
            if entry.job.pr_number is None:
                continue
            if autofix.still_owed(entry.job.audit_action, entry.job.pr_number,
                                  conflicting, owing_reply, closed):
                continue
            # An ask outlives its row by design — the row is rebuilt from it on every
            # poll — so it has to be forgotten as well as dropped, or this same cycle
            # offers it straight back. Logged because it is the one retirement neither
            # a dispatch nor the operator is behind: a row that vanished in silence
            # reads exactly like the review having run.
            if entry.job.requested:
                self._forget_requested(entry.id)
                activity.log("panel", "queue-drop",
                             f"{entry.job.label} — PR no longer open, not run")
            self._drop_queued_task(entry.id)
        for entry in self.drainable_tasks:
            # The list moves under this loop: it waits on a spawn per task, and an
            # "execute now" during one of those takes its row off the queue and starts
            # it there and then. Re-reading the queue is what keeps the drain from
            # dispatching that same task a second time when it reaches it.
            if not any(e.id == entry.id for e in self.queued_tasks):
                continue
            if self._auto_tasks_running() >= self.auto_task_limit:
                return
            # Finding room here is what re-arms the saturation notice. The gate's own
            # reset sits behind the capacity measurement this path skips, so without
            # this the feed would carry one `at-capacity` line for an unbounded run of
            # saturate-and-drain episodes instead of one apiece.
            self._capacity_logged = False
            verdict = self._run_queued_task(entry, forced=False)
            if verdict == "failed":
                return
            # Every remaining entry would be priced against the same windows and get
            # the same answer, and the dispatch has already re-staged this one for the
            # commit at the end of the cycle. Draining on would cost a round of
            # refusals to no end.
            if verdict == autofix.VERDICT_UNAFFORDABLE:
                return

    def _drop_queued_task(self, task_id: str) -> None:
        """Take one task off the queue without starting it — the work it stands for
        is already done. Its place in the saved arrangement is left alone: that list
        drops keys nothing offers on the next commit (:func:`autofix.queue_order`),
        and a task that turns out to be owed after all should come back where the
        operator put it, not at the end."""
        remaining = [e for e in self.queued_tasks if e.id != task_id]
        if len(remaining) != len(self.queued_tasks):
            self.queued_tasks = remaining
            self.tasks_changed.emit()

    def is_paused(self, counter: str | None) -> bool:
        """Whether the monitor that owns this work is switched off.

        A switched-off monitor still finds its work and still queues it — what your
        PRs owe is worth seeing whether or not this machine is set to act on it — but
        nothing automatic starts it. It waits for "execute now", or for the toggle to
        come back on. That is the whole difference the two toggles make: they decide
        who starts the work, not whether it is known."""
        if counter == "review_requests":
            return not self.review_requests_enabled
        if counter in ("my_reviews", "conflicts"):
            return not self.pr_autofix_enabled
        # A review the operator asked for: no monitor owns it, so neither toggle
        # speaks for it. Switching the monitors off says what this machine may go
        # looking for, not that it should stop doing what it was told — for an ask,
        # that is what :attr:`queue_auto_run` says.
        return False

    @property
    def drainable_tasks(self) -> list[autofix.QueuedTask]:
        """The queued tasks the drain may start, in the operator's order — everything
        whose monitor is still on, plus the reviews the operator asked for, which no
        monitor speaks for either way.

        Empty while :attr:`queue_auto_run` is off, including the asks: that switch is
        over the queue itself, not over what fills it."""
        if not self.queue_auto_run:
            return []
        return [e for e in self.queued_tasks if not self.is_paused(e.job.counter)]

    def _begin_starting(self, entry: autofix.QueuedTask) -> None:
        """Move one task out of the queue and into the starting band, where it stays
        for as long as its dispatch runs.

        Idempotent, and keyed by queue key like the queue itself: "execute now" marks
        the task on the GUI thread so the row answers the press, and the worker it
        starts goes through here again."""
        if any(e.id == entry.id for e in self.starting_tasks):
            return
        self.queued_tasks = [e for e in self.queued_tasks if e.id != entry.id]
        self.starting_tasks = self.starting_tasks + [entry]
        self.tasks_changed.emit()

    def _end_starting(self, task_id: str) -> None:
        """The spawn answered: the task is a running agent, or it is nothing. Either
        way the panel has its own row for what happened next."""
        remaining = [e for e in self.starting_tasks if e.id != task_id]
        if len(remaining) != len(self.starting_tasks):
            self.starting_tasks = remaining
            self.tasks_changed.emit()

    def _run_queued_task(self, entry: autofix.QueuedTask, *, forced: bool) -> str:
        """Dispatch one queued task past the capacity check its caller already made,
        and record the attempt its monitor would have recorded.

        ``forced`` is the operator's "execute now", and is the only thing that also
        overrides the spending budget. The drain does not: it is the machine
        starting its own automatic work, and a task that could not be afforded when
        it was found is not afforded by having waited in a list.

        Dispatched as ``SOURCE_AUTO`` whatever put it in the queue, including a review
        the operator asked for. That is not about who wanted the work but about what
        starting it costs: this dispatch spends a bay of the device's cap and its
        share of whatever pays for it, and the gate's panel branch is for the agent a
        click opens *now*, outside the cap entirely. What the operator's ask does
        change is the label — see :func:`autofix.dispatch_label`.

        That record is not bookkeeping polish: the whole retry ladder hangs off it. A
        queued dispatch that wrote none would look, to the very next poll after the
        agent exits, exactly like work never attempted — so an agent that finishes
        without clearing the conflict or leaving the review would be re-dispatched
        three minutes later, and again, with no backoff ever engaging.

        This is also the one place a task crosses from the queue into the starting
        band, whether the drain reached it or the operator clicked: it is a row on the
        panel the whole way across, never drawn twice and never missing."""
        self._begin_starting(entry)
        try:
            verdict = self.dispatch_agent(
                entry.job, autofix.SOURCE_AUTO, entry.attempt, bypass_capacity=True,
                bypass_budget=forced,
            )
        finally:
            # Paired with the line above whatever the dispatch does, because the band
            # is the one list a poll cannot rebuild: :meth:`commit_queue` leaves a
            # starting key out of the published queue deliberately. A raise that got
            # past here would leave a row that never resolves, over work nothing
            # re-offers — where the same raise costs only the poll it happened in.
            self._end_starting(entry.id)
        if verdict in ("spawned", autofix.VERDICT_STAND_DOWN):
            self._record_queued_attempt(entry)
        self._settle_requested(entry, verdict)
        return verdict

    def _record_queued_attempt(self, entry: autofix.QueuedTask) -> None:
        """Write the retry-backoff record for a task the queue dispatched, into the
        same per-monitor ledger that monitor writes itself: ``AgentJob.counter`` names
        the ledger, ``attempt_stamp`` is the stamp that monitor compares against."""
        key = {
            "review_requests": "reviewReqAttempts",
            "my_reviews": "myReviewAttempts",
            "conflicts": "myConflictAttempts",
        }.get(entry.job.counter or "")
        if key is None or entry.job.pr_number is None:
            return
        attempts = self._load_attempts(key)
        attempts[str(entry.job.pr_number)] = autofix.ReviewAttempt(
            entry.job.attempt_stamp, time.time(), entry.attempt
        )
        self._save_attempts(key, attempts)

    def execute_queued_task_async(self, task_id: str) -> None:
        """The queued row's "execute now": start this task immediately, past the two
        holds that are the machine's own judgement.

        It stays AUTO work — same ``Auto · `` label, same auto-handled counter, mesh
        routing still applies, and once running it occupies a slot like any other
        automatic agent, so the rest of the queue waits behind it. Of the five
        asymmetries the gate draws between a click and a monitor tick (capacity,
        budget, mesh, counters, label) this borrows exactly two: the cap and the
        spending budget, which are the two the operator is overriding. Both are
        estimates of what this machine should do next, and the operator looking at
        the row knows something they do not.

        On a worker thread, like every other dispatch path: it assembles nothing but
        it does spawn a terminal, and on a live mesh it waits on a node round-trip.
        The row is moved into the starting band HERE, on the GUI thread, so it answers
        the press in the same repaint rather than a worker's round-trip later."""
        entry = next((e for e in self.queued_tasks if e.id == task_id), None)
        if entry is None:
            return
        self._begin_starting(entry)

        def work() -> None:
            self._execute_queued_task(entry)

        self.start_background(work)

    def _execute_queued_task(self, entry: autofix.QueuedTask) -> None:
        """One "execute now", start to finish.

        The feed line is written from the OUTCOME, never ahead of it: this is an auto
        job, so a mesh peer can own the work, the PR can have gained an agent since
        the list was built, and the spawn can fail — announcing "started" before
        asking would report a launch that never happened in all three."""
        verdict = self._run_queued_task(entry, forced=True)
        label = entry.job.label
        if verdict == "spawned":
            activity.log("panel", "queue-run",
                         f"{label} — started ahead of the task cap")
        # The rest are all logged by the step that decided them, but a feed line is
        # not an answer to a click: the row vanished and nothing opened, so say why in
        # the panel, as the wizards do for their own refusals.
        elif verdict == "failed":
            self.error = f"{label} failed to spawn — see the activity log."
        elif verdict == autofix.VERDICT_IN_FLIGHT:
            self.error = f"{label}: an agent is already on this PR."
        elif verdict == autofix.VERDICT_STAND_DOWN:
            self.error = f"{label}: a mesh peer's agent already owns this work."
        elif verdict == autofix.VERDICT_BANNED:
            remedy = ("the ask is dropped, sweep again to re-queue it"
                      if entry.job.requested else "un-ban to review")
            self.error = f"{label}: the PR's author is banned ({remedy})."
        if verdict != "spawned":
            self.changed.emit()
        self.refresh_activity()

    def move_queued_task(self, task_id: str, onto: str) -> None:
        """Reorder the queue by drag: ``task_id`` lands where it was dropped relative
        to ``onto``. The arrangement is persisted, so it survives both the poll that
        rebuilds the list and the restart that empties it."""
        current = self.queued_tasks
        ordered = autofix.queue_reorder([e.id for e in current], task_id, onto)
        by_id = {e.id: e for e in current}
        self.queued_task_order = ordered
        self.queued_tasks = [by_id[k] for k in ordered]
        self.tasks_changed.emit()

    # MARK: - the one tick every agent question is a projection of

    def _agent_tick(self) -> agentstate.Tick:
        """Resolve every registered run against one pass of evidence. READ-ONLY.

        This is the single place the applet asks what its agents are doing. The dedup,
        the cap, the panel rows and the retirement are all projections of the result
        (:mod:`diplomat_runtime.agentstate`), so they cannot come to disagree — which is
        what four independent re-derivations of the same question did.

        Nothing here writes, retires or logs. The panel asks for the rows and the free
        slots on every repaint, so a read with consequences means a screenshot retires
        records and a redraw writes to the operator's activity feed — which is exactly
        what it did until this was split (found by rendering the panel headlessly and
        finding the warnings in the real feed afterwards). What the consequences are,
        and when they happen, is :meth:`_settle_agents`.

        Deliberately not cached either. The expensive part is the I/O, and that is
        already cached where it happens (:mod:`diplomat_app.probes`), so resolving
        again costs a file read and some set arithmetic. A cache here would instead buy
        a staleness window in which an agent that just finished still holds its bay —
        the exact class of wrong answer this whole mechanism exists to remove.
        """
        now = time.time()
        records = agentregistry.adopt_pids(agentregistry.load())
        evidence = probes.gather(records, now, merged=self._merged_prs)
        t = agentstate.tick(records, evidence, now, self.auto_task_limit)
        with self._tick_lock:
            self._tick = t
        return t

    def _settle_agents(self) -> agentstate.Tick:
        """One tick, and the consequences of it: write back what was learned, retire
        what has ended, and report a probe that has gone quiet.

        Called from the poll and the display refresh — the two ticks that are meant to
        move the world on — and from nowhere that merely draws.
        """
        t = self._agent_tick()
        self._persist_run_changes(t)
        self._retire_finished(t)
        self._note_silent_probes()
        return t

    def _note_silent_probes(self) -> None:
        """Say out loud when a probe has stopped answering.

        This is the failure with no symptom of its own. Every other bug here shows up
        as a wrong row; a probe going quiet shows up as rows that are merely *less
        certain*, which looks exactly like an applet working correctly. Left unsaid,
        the operator sees agents pile up holding bays and has no way to know that the
        reason is a tmux server that died an hour ago.

        Once per probe per episode, cleared when it answers again — the same shape as
        the at-capacity note, for the same reason.
        """
        for h in probes.health():
            was = self._probe_warned.get(h.name, False)
            if h.silent and not was:
                self._probe_warned[h.name] = True
                activity.log("auto", "probe-silent",
                             f"Agent {h.name} {h.reason or 'cannot be read'} — agent "
                             f"rows will read “unknown” and keep their slots until it "
                             f"answers again")
                self.refresh_activity()
            elif not h.silent and was:
                self._probe_warned[h.name] = False
                activity.log("auto", "probe-recovered", f"Agent {h.name} readable again")
                self.refresh_activity()
        self._note_stale_busy_marker()

    def _note_stale_busy_marker(self) -> None:
        """Say out loud when no CLI's interrupt hint has ever once matched.

        Telling a working agent from one waiting at its prompt rests on literal
        strings borrowed from someone else's UI (``apiwatch.BUSY_MARKERS``). If the
        runner in use rewords its status bar, every agent reads as idle at once: every
        bay of the cap frees, and the monitors dispatch a burst onto a machine that is
        already full. Nothing else on this screen would look wrong.

        So a machine that has read plenty of agent screens and never seen a hint on
        any of them is reported. It is not proof — every agent really can be idle —
        which is why the threshold is high and the wording says what was measured
        rather than what it means.
        """
        read, seen = probes.marker_stats()
        if seen or read < self._MARKER_SAMPLE or self._marker_warned:
            return
        self._marker_warned = True
        markers = " / ".join(f"“{m}”" for m in apiwatch.BUSY_MARKERS)
        activity.log("auto", "warn",
                     f"Read {read} agent screens without once seeing {markers} — if "
                     f"the CLI reworded it, every agent now reads as idle and the "
                     f"task cap will not hold")
        self.refresh_activity()

    def _persist_run_changes(self, t: agentstate.Tick) -> None:
        """Write back the four things a tick learns about a run: the pid its shell has
        since written, the tty its process turned out to be on, when its mesh claim was
        last seen, and how its screen compared to the one the last tick saw.

        The last of those is the only one measured ACROSS ticks rather than within one,
        so it is the only one that is worthless unless written down: unpersisted, every
        tick re-reads a screen it has no memory of, the stillness clock restarts at
        zero, and the twenty-minute backstop can never elapse however long a wedged
        agent sits there.

        Merged into whatever is on disk NOW rather than replacing the book with this
        tick's copy of it. A spawn that registered while this tick was resolving would
        otherwise be dropped — an agent nothing counts, which is a bay of the cap the
        machine can then spend twice.

        Synthesized rows are not written at all: a run nobody dispatched is re-derived
        from the process table every tick and has nothing to persist.
        """
        learned = {r.run_id: r for r in t.records if not r.untracked}
        out, changed = [], False
        for r in agentregistry.load():
            fresh = learned.get(r.run_id)
            if fresh is None:
                out.append(r)
                continue
            merged = dataclasses.replace(
                r,
                pid=r.pid if r.pid is not None else fresh.pid,
                tty=r.tty or fresh.tty,
                claim_seen_at=fresh.claim_seen_at or r.claim_seen_at,
                quiet_digest=fresh.quiet_digest,
                quiet_since=fresh.quiet_since,
            )
            changed = changed or merged != r
            out.append(merged)
        if changed:
            agentregistry.save(out)

    def _retire_finished(self, t: agentstate.Tick) -> None:
        """Price what has ended and drop it from the book.

        Only on positive evidence that the agent ended — never on a record's age. The
        prompt comes out of the run directory, so an applet that restarted mid-agent
        can still attribute the run to its transcript; the in-memory list could not,
        and every such run landed in the ledger unpriced.
        """
        gone = [r for r in t.retirable if not r.untracked]
        self._reap_quiet_windows(t)
        if not gone:
            return
        # Every pricing input comes out of the run directory, so all of them must be
        # read before `forget` deletes it. A run the mesh placed leaves no sentinel
        # here, and `record_completion` dates that one from its transcript; now() is
        # only ever the instant this poll looked.
        now = time.time()
        retired = [
            (r, agentregistry.finished_at(r.run_id), _run_prompt(r.run_id),
             agentregistry.bound_session(r.run_id),
             agentregistry.run_runner(r.run_id))
            for r in gone if r.ledger_key
        ]
        # Logged with the evidence that ended it, because forgetting deletes every trace
        # a run leaves: the record, the directory and the prompt all go, and a retirement
        # that turns out to be wrong is then a row that was simply not there any more.
        # This is the one line that says which rung decided.
        for r in gone:
            verdict = t.states.get(r.run_id)
            activity.log(r.source, "retire",
                         f"{r.label or r.run_id} — "
                         f"{verdict.reason if verdict else 'no verdict'}")
        self.refresh_activity()
        agentregistry.forget({r.run_id for r in gone})
        for r, exited_at, prompt, session_id, agent_runner in retired:
            telemetry.record_completion(r.ledger_key, prompt, r.dispatched_at,
                                        exited_at, now, session_id=session_id,
                                        agent_runner=agent_runner)
        if retired:
            self.telemetry_changed.emit()

    def _reap_quiet_windows(self, t: agentstate.Tick) -> None:
        """Close the terminal of every run the quiescence backstop ended.

        Only those. A run that finished the ordinary way keeps its window — its agent
        is alive at its prompt holding the whole task, and the operator may still want
        to read it. One whose screen has not changed in twenty minutes is nobody's.

        Untracked runs are reaped too, unlike in the retirement below: this closes a
        window rather than pricing a run, and a wedged session nobody dispatched is
        just as dead as one we did. It also has no record to drop, so nothing else here
        would ever reach it.
        """
        for record, resolution in t.rows:
            if resolution.state != agentstate.FINISHED or not record.runs_here:
                continue
            if agentstate.went_quiet(record, t.now) is None:
                continue
            if tmuxwatch.kill_session_for_tty(record.tty):
                activity.log("auto", "kill-device",
                             f"closed {record.label or record.run_id}'s window — "
                             f"{resolution.reason.split('; ')[-1]}")

    def _in_flight(self, url: str) -> bool:
        """Does this PR already have an agent? Every state that is not over counts,
        including one waiting at its prompt (that session holds the PR's context) and
        one nothing is known about (releasing a PR on missing evidence is how two
        agents end up on it)."""
        m = re.search(r"/pull/(\d+)", url)
        return m is not None and self._agent_tick().in_flight(int(m.group(1)))

    def refresh_merged_statuses(self) -> None:
        """Ask GitHub which of the tracked PRs have landed — the one terminal outcome
        that outranks whatever a process is doing.

        On the slow refresh, not the 8-second tick: it costs a ``gh`` call per PR. The
        answer is carried forward by the fast ticks in between.
        """
        prs = {r.pr_number for r in agentregistry.load() if r.pr_number is not None}
        self._merged_prs = probes.merged_prs(prs)

    # MARK: monitor persistence + poll-error state

    def _note_poll_failure(self, err: object) -> None:
        if self._poll_error_this_cycle is None:  # first failure of the cycle wins
            self._poll_error_this_cycle = str(err)

    def _settle_poll_error(self) -> None:
        err = self._poll_error_this_cycle
        if err:
            if self.autofix_poll_error is None:
                activity.log("auto", "poll-failed", f"Monitor poll failing: {err[:120]}")
            self.autofix_poll_error = err
            self.autofix_poll_error_at = time.time()
        elif self.autofix_poll_error is not None:
            activity.log("auto", "poll-recovered", "Monitor polls succeeding again")
            self.autofix_poll_error = None
            self.autofix_poll_error_at = None

    def _load_fingerprints(self) -> dict:
        raw = self._settings.value("autofixFingerprints", "", str)
        try:
            obj = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        out: dict[int, autofix.PRFingerprint] = {}
        for k, v in (obj or {}).items():
            try:
                out[int(k)] = autofix.PRFingerprint(
                    mergeable=v.get("mergeable", "UNKNOWN"),
                    review_decision=v.get("reviewDecision", ""),
                    threads_unresolved=int(v.get("threadsUnresolved", 0)),
                )
            except (ValueError, AttributeError):
                continue
        return out

    def _save_fingerprints(self, fps: dict) -> None:
        obj = {
            str(k): {
                "mergeable": f.mergeable,
                "reviewDecision": f.review_decision,
                "threadsUnresolved": f.threads_unresolved,
            }
            for k, f in fps.items()
        }
        self._settings.setValue("autofixFingerprints", json.dumps(obj))

    def _load_attempts(self, key: str) -> dict:
        raw = self._settings.value(key, "", str)
        try:
            obj = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        out: dict[str, autofix.ReviewAttempt] = {}
        for k, v in (obj or {}).items():
            try:
                out[k] = autofix.ReviewAttempt(
                    requested_at=v.get("requestedAt", ""),
                    last_dispatched_at=float(v.get("lastDispatchedAt", 0.0)),
                    attempts=int(v.get("attempts", 1)),
                )
            except (ValueError, AttributeError):
                continue
        return out

    def _save_attempts(self, key: str, attempts: dict) -> None:
        obj = {
            k: {
                "requestedAt": a.requested_at,
                "lastDispatchedAt": a.last_dispatched_at,
                "attempts": a.attempts,
            }
            for k, a in attempts.items()
        }
        self._settings.setValue(key, json.dumps(obj))

    # MARK: telemetry samples

    def run_telemetry_sample_async(self) -> None:
        """Take one quota/token reading for the telemetry ledger, off the UI thread.

        Driven by its own timer rather than off the back of the auto-fix poll,
        because the two answer to different switches: the share of this machine's
        tokens that goes on the monitored repo is worth knowing whether or not
        the monitors are enabled, and pricing the rate-limit window needs an
        unbroken sample series regardless. :func:`telemetry.sample_due` does the pacing, so calling
        this more often than the sample interval is free.

        The quota probe insists (:func:`quota.fractions_left`), so a worker can outlive
        the gap between turns — and until it writes, :func:`telemetry.sample_due` still
        says a sample is owed. The lock is what keeps that from becoming several.
        """
        if not telemetry.sample_due():
            return
        if not self._telemetry_sample_lock.acquire(blocking=False):
            return  # a sample is already being taken

        def work() -> None:
            from diplomat_runtime import quota, usagescan

            try:
                try:
                    session, week = quota.fractions_left(insist=True)
                    totals = usagescan.totals()
                except OSError:
                    return  # an unreadable ~/.claude costs this sample, nothing else
                telemetry.record_sample(session, week, totals.repo, totals.other)
            finally:
                # Held until the sample is on disk: released a moment earlier, the
                # next turn would find `sample_due` still true and take a second one.
                self._telemetry_sample_lock.release()
            self.telemetry_changed.emit()

        self.start_background(work)

    # MARK: Claude-API-error watcher

    # The Linux port of Store.swift's runApiErrorScanOnce. A background scan (driven
    # by a QTimer in app.py, independent of the panel) reads every tmux pane's last
    # visible lines and, for any showing a Claude API error that has stopped changing
    # (a confirmed stall), submits the "continue" nudge to that exact pane — so an
    # agent that stalled on a transient server error (e.g. overnight 529 overload)
    # resumes on its own. The pure detection/backoff logic lives in apiwatch.py; the
    # tmux reads/writes in tmuxwatch.py.

    def run_apiwatch_poll_async(self) -> None:
        """Kick one watcher scan on a worker thread (guarded against overlap). Safe to
        call from a QTimer whether or not the panel is open; no-ops when disabled."""
        if not self.api_watch_enabled:
            return
        if not self._apiwatch_lock.acquire(blocking=False):
            return  # a scan is already running

        def work() -> None:
            try:
                self._apiwatch_scan_once()
            finally:
                self._apiwatch_lock.release()
                self.apiwatch_changed.emit()

        self.start_background(work)

    def _apiwatch_scan_once(self) -> None:
        """One scan: read every pane and nudge any confirmed-stalled erroring pane
        that's outside its backoff window."""
        if not self.api_watch_enabled:
            return
        # None = a tmux command failed unexpectedly — skip the whole scan rather than
        # treating it as "no panes", which would wrongly clear every backoff.
        panes = tmuxwatch.dump_panes()
        available = tmuxwatch.is_available()
        if panes is None:
            self.apiwatch_status = {
                "updatedAt": time.time(),
                "watching": 0,
                "continues": self.api_watch_continues,
                "tmux": available,
            }
            return
        now = time.time()
        erroring: set[str] = set()
        for p in panes:
            # Out-of-quota banners return False here: a quota-limited agent can't
            # progress until its window resets, so only transient errors are nudged.
            if not apiwatch.looks_like_api_error(p.tail):
                continue
            erroring.add(p.pane_id)
            # Idle-confirmation: only nudge a pane whose erroring tail is UNCHANGED
            # since the previous scan. An actively-working pane changes between scans
            # and must not be treated as stalled.
            stalled = apiwatch.is_confirmed_stall(
                self._apiwatch_seen_tail.get(p.pane_id), p.tail
            )
            self._apiwatch_seen_tail[p.pane_id] = p.tail
            if not stalled:
                continue
            b = self._apiwatch_backoff.get(p.pane_id)
            if b and now < b["nextAllowed"]:  # still inside this pane's backoff window
                continue
            if not tmuxwatch.send_continue(p.pane_id, apiwatch.CONTINUE_MESSAGE):
                continue  # pane vanished — don't count a nudge that never landed
            self.api_watch_continues += 1
            nxt = apiwatch.next_backoff(b["interval"] if b else None)
            self._apiwatch_backoff[p.pane_id] = {
                "nextAllowed": now + nxt,
                "interval": nxt,
            }
            activity.log(
                "auto", "nudge",
                f"Continued a stalled agent (API error) on {p.pane_id}; "
                f"next retry in ≥ {apiwatch.human_interval(nxt)}",
            )
        # Keep backoff + idle-confirmation state ONLY for currently-erroring panes: a
        # pane that stopped erroring has recovered (reset to base), and a closed pane's
        # entry must not linger. tmux never recycles a pane_id, but pruning keeps the
        # maps bounded and forces a fresh two-scan confirmation if it errors again.
        self._apiwatch_backoff = {
            k: v for k, v in self._apiwatch_backoff.items() if k in erroring
        }
        self._apiwatch_seen_tail = {
            k: v for k, v in self._apiwatch_seen_tail.items() if k in erroring
        }
        self.apiwatch_status = {
            "updatedAt": now,
            "watching": len(panes),
            "continues": self.api_watch_continues,
            "tmux": available,
        }

    # MARK: device allocator

    def refresh_device_state(self) -> None:
        """Re-read the daemon's public state file (cheap) and signal on change.

        Compares only the `devices` list, not the whole snapshot: the daemon stamps
        a fresh `updatedAt` every poll, which would otherwise force a needless
        rebuild of the device rows every 8s.
        """
        new = deviceallocator.read_state()
        new_devices = (new or {}).get("devices")
        old_devices = (self.device_state or {}).get("devices")
        if new_devices != old_devices:
            self.device_state = new
            self.devices_changed.emit()

    def refresh_allocator_install_async(self) -> None:
        """Shell the installer's --check off the UI thread; signal when done."""
        def work() -> None:
            self.allocator_install = deviceallocator.check()
            self.allocator_changed.emit()
        self.start_background(work)

    # MARK: activity feed + bans

    def refresh_activity(self) -> None:
        """Re-read the shared activity feed (audit.jsonl) and ban list (cheap tail /
        small-file reads) and signal on change. Runs on the panel's 8s poll."""
        from diplomat_runtime import activity
        from . import bans

        new_audit = activity.read()
        new_bans = bans.read()
        if new_audit != self.audit_entries or new_bans != self.banned_authors:
            self.audit_entries = new_audit
            self.banned_authors = new_bans
            self.activity_changed.emit()

    def ensure_allocator_installed_async(self) -> None:
        """Install the device-allocator MCP when Diplomat is first set up, and keep
        an existing install current afterwards. Called blindly on every launch.

        Two jobs, one shell of the installer, because they answer to the same
        status. The first is the original one-time install, which only marks itself
        done once the install actually lands so a transient failure (no node yet)
        retries on a later launch. The second is the update: everything the
        installer writes — the skill, the always-on rule, the CLAUDE.md block, the
        MCP registration — is a *copy* of something in this checkout, and a
        ``git pull`` moves the originals alone. Without this, a machine set up once
        coerces its agents with whatever text shipped that day, forever.

        Which of the three situations this is — first run, stale, or an install the
        user deliberately removed — is :func:`deviceallocator.needs_install`, shared
        with its macOS twin so the two applets can't drift on the one question where
        being wrong reinstalls something behind the user's back.
        """
        if not deviceallocator.package_available():
            return

        def work() -> None:
            status = deviceallocator.check() or {}
            if not deviceallocator.needs_install(status, self.allocator_setup_done):
                self.allocator_install = status
                if status.get("installed"):
                    self.allocator_setup_done = True
                self.allocator_changed.emit()
                return
            # A first install and a stale one are the same act: pull the MCP server's
            # runtime deps, then (re-)register — `--install` rewrites every artifact,
            # so it is also the repair.
            deviceallocator.ensure_deps()
            result = deviceallocator.install()
            self.allocator_install = result
            if result and result.get("installed"):
                self.allocator_setup_done = True
            self.allocator_changed.emit()
            self.refresh_device_state()
        self.start_background(work)

    def install_allocator_async(self) -> None:
        def work() -> None:
            deviceallocator.ensure_deps()
            self.allocator_install = deviceallocator.install()
            self.allocator_setup_done = True
            self.allocator_changed.emit()
            self.refresh_device_state()
        self.start_background(work)

    def uninstall_allocator_async(self) -> None:
        def work() -> None:
            self.allocator_install = deviceallocator.uninstall()
            # An explicit uninstall is a settled choice — don't auto-reinstall.
            self.allocator_setup_done = True
            self.allocator_changed.emit()
            self.refresh_device_state()
        self.start_background(work)

    # MARK: self-update

    def refresh_update_status_async(self) -> None:
        """Fetch origin and compare HEAD to upstream, off the UI thread."""
        if (self.update_state or {}).get("phase") in ("checking", "updating", "restarting"):
            return

        def work() -> None:
            from . import selfupdate

            self.update_state = {"phase": "idle", **selfupdate.check()}
            self.update_changed.emit()

        self.update_state = {"phase": "checking"}
        self.update_changed.emit()
        self.start_background(work)

    def update_applet_async(self) -> None:
        """Pull the checkout, rebuild diplomat-core, relaunch the applet.

        The relaunched instance terminates this one (newest-wins singleton), so
        a successful run ends in the "restarting" phase with this process about
        to be replaced; only a failure leaves state to interact with.
        """
        if (self.update_state or {}).get("phase") in ("updating", "restarting"):
            return

        def work() -> None:
            from . import selfupdate

            def step(text: str) -> None:
                self.update_state = {"phase": "updating", "step": text}
                self.update_changed.emit()

            try:
                step("pulling from origin…")
                commit = selfupdate.pull()
                step(f"building diplomat-core at {commit}…")
                selfupdate.build_core()
                step("relaunching…")
                selfupdate.relaunch()
                self.update_state = {"phase": "restarting", "commit": commit}
            except (selfupdate.UpdateError, OSError, subprocess.TimeoutExpired) as exc:
                # TimeoutExpired (black-holed network on pull's fetch, or a hung swift
                # build) and OSError are NOT UpdateError subclasses; without catching
                # them the worker thread dies with update_state stuck on "updating",
                # and the phase guard above then makes every later update a permanent
                # no-op until the app restarts. Surface it as an error phase instead.
                self.update_state = {"phase": "error", "error": str(exc)}
            self.update_changed.emit()

        # Claim the phase before the thread runs so a double-click can't
        # start two updates.
        self.update_state = {"phase": "updating", "step": "starting…"}
        self.update_changed.emit()
        self.start_background(work)

    # MARK: mesh (LAN P2P topology)

    def refresh_mesh_state(self) -> None:
        """Re-read the local node's public topology snapshot (state.json) and
        signal on a *meaningful* change. Cheap file read — driven by the panel's
        2s poll while it's visible. Never spawns a node.

        `updatedAt` is stamped every write, so comparing whole snapshots would
        fire every poll; we compare everything *but* `updatedAt`, then also allow
        link-freshness drift (a peer's `lastSeenSecsAgo` creeping up) to trigger a
        rebuild so the badges stay honest.
        """
        # ``AVAILABLE``, not ``mesh_enabled``: this also runs on the way *out* of
        # the mesh — the refresh right after ctl.stop() is what clears the topology
        # off the screen, and by then the preference is already off.
        if not szpont.AVAILABLE:
            return
        # Render mode pins a synthetic topology via the override — never let a
        # poll read (or clobber it with) the real ~/.diplomat/mesh/state.json.
        if self._mesh_enabled_override is not None:
            return

        from szpontnet import statefile

        new = statefile.read_state()
        if self._mesh_meaningfully_changed(self.mesh_state, new):
            self.mesh_state = new
            self.mesh_changed.emit()

    @staticmethod
    def _mesh_meaningfully_changed(old: dict | None, new: dict | None) -> bool:
        if old is None or new is None:
            return old is not new  # None→dict or dict→None is always meaningful

        # Fields that tick/drift every write on their own — dropping them keeps an
        # idle mesh from firing a rebuild (which tears down an open combo the user is
        # editing) twice a second. Link state (up/stale/down), the token STATE, and
        # the real session/week quota percentages (integer-grained, move ~1/min at
        # most) still live in the node dicts, so genuine transitions repaint; only
        # the continuously-moving numbers (uptime, raw quota fraction) are ignored.
        _tick_top = ("updatedAt", "pid")
        _tick_node = ("lastSeenSecsAgo", "uptimeSecs", "tokensPct")

        def strip(snap: dict) -> dict:
            out = {k: v for k, v in snap.items() if k not in _tick_top}
            me = out.get("self")
            if isinstance(me, dict):
                out["self"] = {k: v for k, v in me.items() if k not in _tick_node}
            peers = out.get("peers")
            if isinstance(peers, list):
                out["peers"] = [
                    {k: v for k, v in p.items() if k not in _tick_node} for p in peers
                ]
            return out

        return strip(old) != strip(new)

    def ensure_mesh_running_async(self) -> None:
        """Start a background mesh node iff the user enabled the mesh and none is
        already alive. No-ops when disabled — which includes having no SzpontNet
        installed — so it's safe to call blindly on app start, as the launcher
        does. Never runs in a headless render/test (guarded by mesh_enabled,
        which those paths leave off / stub)."""
        if not self.mesh_enabled:
            self.refresh_mesh_state()
            return

        from szpontnet import statefile

        if statefile.node_running():
            self.refresh_mesh_state()
            return

        def work() -> None:
            import os
            import subprocess
            import sys

            from . import RUNTIME_DIR

            # The node is a separate process and gets no say in who its host is, so
            # hand it both halves: the path to import the shared runtime and the
            # library from, and the module that registers Diplomat behind it. Without
            # the second the node comes up on the library's own defaults — its own
            # state directory, no duty catalog of ours, no activity feed. This package
            # is deliberately NOT on that path: a node needs the runtime, and nothing a
            # Qt applet adds on top of it.
            env = {
                **os.environ,
                "SZPONTNET_HOST": "diplomat_runtime.szponthost",
                "PYTHONPATH": os.pathsep.join(
                    [RUNTIME_DIR, szpont.package_dir(),
                     os.environ.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep),
            }
            try:
                subprocess.Popen(  # noqa: S603 — relaunch ourselves as a node
                    [sys.executable, "-m", "szpontnet", "--daemon"],
                    cwd=RUNTIME_DIR,
                    env=env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:  # noqa: BLE001
                self.mesh_error = f"could not start mesh node: {exc}"
            self.refresh_mesh_state()

        self.start_background(work)

    def stop_mesh_async(self) -> None:
        """Ask the local node to stop (used when the user disables the mesh)."""
        szpont.require()
        from szpontnet import ctl

        def work() -> None:
            try:
                ctl.stop()
            except ctl.CtlError:
                pass  # already down — nothing to stop
            self.refresh_mesh_state()

        self.start_background(work)

    def _mesh_command(self, run, what: str) -> None:
        """Run one mesh control round-trip on a daemon thread, then settle the view:
        a :class:`ctl.CtlError` becomes ``mesh_error`` (the mesh screen renders it)
        and the topology is re-read so the edit shows immediately.

        Every step is load-bearing, which is why the five commands below share this
        routine rather than each spelling it out: without the refresh the screen keeps
        showing pre-edit state, and without the error assignment a rejected edit looks
        like it worked. Twin of ``meshCommand`` in Store.swift.

        :func:`szpont.require` up front, here and in the two commands that don't
        share this routine: a control round-trip is only reachable from a mesh
        control, so with the add-on gone the caller has a bug rather than a
        disabled feature, and it should read as one — not as a bare ImportError
        from the line below.
        """
        szpont.require()
        from szpontnet import ctl

        def work() -> None:
            try:
                run(ctl)
                self.mesh_error = None
            except ctl.CtlError as exc:
                self.mesh_error = str(exc)
            self.refresh_mesh_state()

        self.start_background(work, f"mesh-{what}")

    def mesh_set_attr(self, node_id: str, attrs: dict) -> None:
        """Edit a node's attributes (self or a peer, forwarded over the mesh)."""
        self._mesh_command(lambda ctl: ctl.set_attr(node_id, attrs), "set-attr")

    def mesh_trust(self, fingerprint: str, label: str = "") -> None:
        """Mark a peer's device Personal — add its proven fingerprint to the local
        trusted allowlist (so its mesh requests run as if triggered here)."""
        self._mesh_command(lambda ctl: ctl.trust_device(fingerprint, label), "trust")

    def mesh_untrust(self, fingerprint: str) -> None:
        """Mark a peer's device Foreign — remove its fingerprint from the allowlist."""
        self._mesh_command(lambda ctl: ctl.untrust_device(fingerprint), "untrust")

    def mesh_unban(self, fingerprint: str, node_id: str = "") -> None:
        """Lift a ban on a peer's device (it was marked banned after accepting a
        SzpontRequest and failing to deliver it — or manually). It returns to
        Foreign; promote it via the trust toggle if it is actually yours."""
        self._mesh_command(lambda ctl: ctl.unban_device(fingerprint, node_id), "unban")

    def mesh_set_overrides(self, duty: str, placement: dict) -> None:
        """Edit one duty's mesh-wide placement (gossiped last-writer-wins)."""
        self._mesh_command(lambda ctl: ctl.set_overrides(duty, placement), "set-overrides")

    def mesh_set_wan(self, transport: str) -> None:
        """Pick the mesh's preferred WAN transport (gossiped last-writer-wins, like a
        placement edit). It orders the transports a WAN dial tries, so it lands on the
        next dial each node makes — no live link is moved."""
        self._mesh_command(lambda ctl: ctl.set_wan(transport), "set-wan")

    def mesh_connect(self, address: str) -> None:
        """Link to a peer's WAN id (an iroh endpoint id or an onion), reaching a
        machine this one may never have met on the LAN. The address shape picks the
        transport; the node dials in the background, so the peer appears in the
        topology a poll or two later."""
        self._mesh_command(lambda ctl: ctl.connect(address), "connect")

    def mesh_dispatch(self, duty: str, prompt: str, done_callback=None) -> None:
        """Route a job through the mesh; `done_callback(results, error)` fires on
        the worker thread (callers marshal back to the UI thread themselves)."""
        szpont.require()
        from szpontnet import ctl

        def work() -> None:
            results: list = []
            err: str | None = None
            try:
                results = ctl.dispatch(duty, prompt)
                self.mesh_error = None
            except ctl.CtlError as exc:
                err = str(exc)
                self.mesh_error = err
            self.refresh_mesh_state()
            if done_callback is not None:
                done_callback(results, err)

        self.start_background(work)

    def count(self, tool_id: str) -> int:
        return len(self.items_for(tool_id))

    def lookup(self, number: int) -> LookupResult:
        on_lists = [
            t.id
            for t in self.visible_tools
            if any(item.id == number for item in self.items_for(t.id))
        ]
        pr = next((p for p in self.prs if p.number == number), None)
        if pr is not None:
            return LookupResult(
                number=number,
                on_lists=on_lists,
                presence=f"open PR · @{pr.author} · {'draft' if pr.is_draft else 'ready'}",
                url=pr.url,
            )
        issue = next((i for i in self.issues if i.number == number), None)
        if issue is not None:
            return LookupResult(
                number=number,
                on_lists=on_lists,
                presence=f"open issue · @{issue.author} [{issue.author_association}]",
                url=issue.url,
            )
        return LookupResult(
            number=number,
            on_lists=on_lists,
            presence="not in open PRs/issues (closed or unknown)",
            url=None,
        )

    # One row builder per tool: the ordered source objects, then the two lines each
    # row shows. Everything else about a row — its id, its `#N` badge, its title and
    # its url — is the same for all six, so `items_for` below owns that and each tool
    # contributes only what actually differs.
    #
    # The row TEXT is duplicated across platforms by necessity — `ToolData.items` in
    # diplomat-core/Sources/DiplomatCore/ToolKind.swift renders the same six lists for the macOS
    # panel, and neither side can shell out to the other for something rebuilt on
    # every render. `diplomat-platform/linux/tests/test_tooldata_parity.py` runs both over one fixture
    # and diffs the rows, so a change to either has to be made to both.
    def _row_specs(self) -> dict:
        me = self.effective_me
        return {
            "skillPRs": (
                lambda: sorted(Filters.skill_prs(self.prs), key=lambda p: -p.number),
                lambda p: f"@{p.author} · {Fmt.age(p.created_at)} · "
                          f"{'draft' if p.is_draft else 'ready'}",
                lambda p: "skills: " + ", ".join(
                    Fmt.skill_name(f) for f in p.files if Filters.is_skill_file(f)
                ),
            ),
            "installerPRs": (
                lambda: sorted(Filters.installer_prs(self.prs), key=lambda p: -p.number),
                lambda p: f"@{p.author} · {Fmt.age(p.created_at)} · "
                          f"{_count(len(_installer_files(p)), 'file')}",
                lambda p: "\n".join(Fmt.short_path(f) for f in _installer_files(p)),
            ),
            "staleReady": (
                lambda: sorted(Filters.stale_ready_prs(self.prs), key=lambda p: p.ready_at),
                lambda p: f"@{p.author} · ready {Fmt.days(p.ready_at)}d · "
                          f"{'born-ready' if p.ready_for_review_at is None else 'converted'}",
                lambda p: None,
            ),
            "unaddressedIssues": (
                lambda: sorted(Filters.unaddressed_external_issues(self.issues),
                               key=lambda i: i.created_at),
                lambda i: f"@{i.author} [{i.author_association}] · "
                          f"{Fmt.age(i.created_at)} · {i.comment_count}c",
                lambda i: f"labels: {', '.join(i.labels)}" if i.labels else None,
            ),
            "myApproved": (
                lambda: sorted(Filters.my_approved_prs(self.prs, me), key=lambda p: -p.number),
                lambda p: f"@{p.author} · {Fmt.age(p.created_at)} · approved · "
                          f"{'draft' if p.is_draft else 'ready'}",
                lambda p: None,
            ),
            "myUnaddressed": (
                lambda: sorted(Filters.my_unaddressed_review_prs(self.prs, me),
                               key=lambda p: -p.number),
                lambda p: f"@{p.author} · {Fmt.age(p.created_at)} · "
                          f"{_count(len(p.unaddressed_threads(me)), 'open thread')}",
                lambda p: None,
            ),
        }

    def items_for(self, tool_id: str) -> list[DisplayItem]:
        spec = self._row_specs().get(tool_id)
        if spec is None:
            return []
        source, line2, line3 = spec
        return [
            DisplayItem(
                id=obj.number,
                badge=f"#{obj.number}",
                title=obj.title,
                url=obj.url,
                line2=line2(obj),
                line3=line3(obj),
            )
            for obj in source()
        ]
