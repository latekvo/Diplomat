"""Application state, persisted settings, and the tool catalog.

A port of Store.swift. The tool catalog (titles, subtitles, colours, order) is
loaded from the shared ``assets/catalog.json``; the row-mapping in ``items_for``
is the same dense formatting the macOS panel renders. Settings persist via
``QSettings`` (the Linux analogue of macOS UserDefaults) — except the repo root,
which lives in the shared ``~/.diplomat/config.json`` (see :mod:`appconfig`) so a
Qt-less mesh node can read it too.
"""

from __future__ import annotations

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

from . import (
    activity,
    apiwatch,
    appconfig,
    autofix,
    autofixmonitor,
    bans,
    conflicts,
    core,
    deviceallocator,
    review,
    szpont,
    telemetry,
    tmuxwatch,
)
from .models import API, Filters, Fmt, OpenIssue, OpenPR
from .prtarget import PRTarget


def _count(n: int, noun: str) -> str:
    """``3 files`` / ``1 file`` — the pluralisation two row builders share."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _installer_files(pr: OpenPR) -> list[str]:
    """The installer-owned paths in a PR — counted in line2 and listed in line3,
    so they must be the same set in both."""
    return [f for f in pr.files if Filters.is_installer_file(f)]


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

    # A tracked auto-fix agent whose completion sentinel never appears (window
    # killed, machine slept) is considered finished after this long, so a stuck
    # entry can't pin a PR as "in flight" forever.
    _AUTOFIX_INFLIGHT_TTL = 2 * 60 * 60

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
        # In-flight agents [{url, number, done, at, source}] — dedups against
        # spawning a second agent on a PR one is already working, and tells the
        # automatic-task cap which of them it is allowed to count. Registered and
        # pruned from several threads (see _prune_inflight), so it has a mutex of its
        # own; it is the shortest-held of the three and nests inside neither.
        self._autofix_inflight: list[dict] = []
        self._inflight_lock = threading.Lock()
        # Whether the "deferring auto work" note has been logged for the current
        # at-capacity episode (see _log_at_capacity).
        self._capacity_logged = False
        # Automatic work nothing has started yet — held by the task cap, or by its own
        # monitor being switched off — in the order it will run. The panel's
        # Agent-tasks list.
        #
        # Deliberately NOT persisted. A deferral writes no attempt record precisely so
        # that every poll re-offers everything GitHub still owes, which means the queue
        # is rebuilt from live evidence every 3 minutes; a stored copy would only ever
        # be a staler answer to a question already being re-asked, and would hand
        # "execute now" a prompt assembled against a PR that has since moved on. What
        # IS persisted is `queued_task_order` — the operator's arrangement, the one
        # thing a poll cannot reconstruct. Always REPLACED, never mutated in place: the
        # poll worker writes it while the GUI thread reads it to draw the rows.
        self.queued_tasks: list[autofix.QueuedTask] = []
        # This poll's deferrals, published as `queued_tasks` only once the whole cycle
        # has succeeded: a failed fetch means "we no longer know what is owed", which
        # is not the same as "nothing is owed", and must not empty the list.
        self._staged_queue: list[autofix.QueuedTask] = []
        # The last count _auto_tasks_running measured, so the panel can draw the
        # device's free slots without a `ps` scan of its own.
        self._auto_tasks_measured = 0
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
        # Brief cache over the `ps` live-agent scan (autofix.live_pr_numbers) so one
        # poll cycle costs one subprocess: (at, pr numbers).
        self._live_agents_cache: tuple[float, set[int]] | None = None
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

        threading.Thread(target=work, daemon=True).start()

    def _autofix_poll_once(self) -> None:
        self._poll_error_this_cycle = None
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
                # The queue first, in the operator's order: a slot that freed since
                # the last cycle belongs to work already waiting for it, not to
                # whichever PR this poll's fetch happens to return first.
                #
                # Only on evidence a cycle actually confirmed. The queue survives a
                # failed cycle deliberately (see _staged_queue), but surviving is not
                # the same as being current: while `gh` is down the list freezes, and
                # a drain that kept firing from it would spawn agents at work
                # answered by hand hours ago.
                if self.autofix_poll_error is None:
                    self._drain_queued_tasks()
                # Start this cycle's staging empty — the one place it is reset, so a
                # cycle that failed part-way and never committed does not carry its
                # offers into this one. They are re-offered by the cycle that
                # succeeds, since the refusal that staged them wrote no attempt
                # record.
                self._staged_queue = []
                # Both monitors run whatever their toggles say; a switched-off one
                # queues what it finds instead of dispatching it (see is_paused).
                self._poll_my_prs(owner, repo)
                self._poll_review_requests(owner, repo)
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

    def _poll_my_prs(self, owner: str, repo: str) -> None:
        try:
            snaps = autofixmonitor.fetch_snapshots(owner, repo, self.effective_me)
        except Exception as exc:  # noqa: BLE001 — any failure is a poll failure
            self._note_poll_failure(exc)
            return
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

    def _route_via_mesh(self, job: "autofix.AgentJob") -> str | None:
        """Route an auto job through the mesh (szpontnet-spec/docs/12): claim-gated
        dispatch to the best-surplus node.

        Every machine scans GitHub independently, but the mesh runs each unit of
        work **once** — ``ctl.dispatch`` claims the work key and places the run on
        the best node; the EXECUTOR holds that claim for its agent's lifetime, so a
        concurrent or repeat scan is suppressed and a node death frees it for
        failover. No node stands down on a duty ASSIGNMENT anymore — that deferred
        to a node that might not be scanning at all, silently dropping the work.

        Returns ``"spawned"`` (the mesh took it), ``VERDICT_STAND_DOWN`` (a peer's
        agent already owns it), or ``None`` to fall through to a LOCAL spawn — the
        fail-open path when the mesh is unavailable, so a wedged node never drops
        the operator's work."""
        if not self.mesh_enabled or not job.work_key:
            return None

        from szpontnet import ctl, statefile

        state = statefile.read_state()
        if not state or not statefile.node_running(state):
            return None
        try:
            results = ctl.dispatch(job.duty, job.prompt, work_key=job.work_key)
        except ctl.CtlError:
            return None  # node unreachable → fail-open to a local tracked spawn
        if not results:
            return None
        statuses = [r.get("status") for r in results]
        if statuses and all(s == "suppressed" for s in statuses):
            self._log_mesh_suppressed(job.work_key, results)
            return autofix.VERDICT_STAND_DOWN
        if all(s in ("spawned", "suppressed") for s in statuses):
            return "spawned"  # ran on the mesh (the node logs where)
        return None  # declined/failed on every slot → fall through to a local spawn

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
                       bypass_capacity: bool = False) -> str:
        """Run one agent job through the shared gate (``autofix.dispatch_decide`` -
        the pure, tested decision both platforms mirror) and, on proceed, spawn and
        register it. Returns the verdict: ``"spawned"``, an ``autofix.VERDICT_*``
        refusal, or ``"failed"`` (spawn error). Twin of Store.dispatchAgent (macOS).

        In-flight evidence is the tracked list OR a live ``claude`` visible in
        ``ps`` - the ground-truth floor that also catches agents whose local
        bookkeeping was lost (applet restart) and mesh jobs that landed on this
        very machine. ``_dispatching_prs`` is held for the whole call so an
        overlapping poll and a click can't race two spawns onto one PR.

        An AUTO job is additionally capped at ``auto_task_limit`` concurrent
        agents on this device (``_auto_tasks_running``), and held outright while
        its own monitor is switched off (:meth:`is_paused`); a panel click is
        subject to neither. Either refusal queues the job (:meth:`_stage_queued`),
        which is what the panel's Agent-tasks list shows as *queued*.

        ``bypass_capacity`` is for the two callers that have already answered the
        capacity question themselves: the queue drain (which counted the free slot
        it is filling, and skips paused work by construction) and "execute now"
        (where the operator is overriding both holds deliberately). It skips the
        measurement — not just its verdict — so neither pays for a second ``ps``
        scan, and so a forced run cannot re-queue itself."""
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
                # device's. Modelled as capacity because the answer is the same one
                # in every respect that matters here — hold the job, write no attempt
                # record, re-offer it next poll — which keeps a toggle that is the
                # front-end's own out of the dispatch gate both front-ends mirror.
                paused = self.is_paused(job.counter)
                at_capacity = full or paused
            verdict = autofix.dispatch_decide(
                source, banned, agent_on_pr, False, at_capacity
            )
            if verdict == autofix.VERDICT_AT_CAPACITY:
                # A paused monitor is not a saturated device: it queues silently,
                # because the operator switched it off on purpose and the row says
                # the rest.
                if not paused:
                    self._log_at_capacity()
                self._stage_queued(job, attempt)
                return verdict
            if verdict == autofix.VERDICT_BANNED:
                activity.log(
                    source, "ban-skip", f"{job.label} - author is banned (un-ban to review)"
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
            # An AUTO job on a live mesh runs on the best-surplus node via
            # claim-gated dispatch (every machine scans; the mesh runs it once and
            # dedups via the executor's claim). A manual spawn — or a wedged/absent
            # mesh — runs and is tracked locally instead (fail-open). Both converge
            # on the shared audit/counter tail below.
            routed = self._route_via_mesh(job) if source == autofix.SOURCE_AUTO else None
            if routed == autofix.VERDICT_STAND_DOWN:
                return routed  # a peer's agent owns it (logged once by the router)
            if routed == "spawned":
                ok = True
            elif job.pr_url is not None and job.pr_number is not None:
                ok = self._spawn_tracked(job.prompt, job.pr_url, job.pr_number,
                                         source, job.ledger_key)
            else:
                # Not PR-scoped (sweeps, audits): nothing to dedup against, so no
                # registration - but the same spawn, label and audit shape.
                try:
                    review.spawn(job.prompt, self.terminal)
                    ok = True
                except review.SpawnError:
                    ok = False
            if not ok:
                activity.log(source, "spawn-failed", f"{job.label} failed to spawn")
                self.refresh_activity()
                return "failed"
            activity.log(
                source, job.audit_action, autofix.dispatch_label(source, job.label, attempt)
            )
            # The telemetry ledger tracks the MONITORS, so only an auto dispatch is
            # recorded — a wizard click is the operator's own doing and has no queue
            # instant to be late against. A mesh placement spends a PEER's quota, so
            # it is flagged and kept out of the per-task cost figures.
            if source == autofix.SOURCE_AUTO and job.ledger_key:
                telemetry.record_started(job.ledger_key,
                                         remote=routed == "spawned", attempt=attempt)
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

    def _spawn_tracked(self, prompt: str, url: str, number: int, source: str,
                       ledger_key: str = "") -> bool:
        """Spawn an agent with a completion sentinel and record it in-flight. Returns
        whether the terminal launched.

        ``source`` is recorded because the automatic-task cap has to tell the two
        apart: a panel click spends none of the automatic budget, while a monitor
        dispatch is exactly what the budget is for.

        The prompt is kept alongside the sentinel because it is what ties the run
        back to its Claude transcript when it finishes (:func:`usagescan.task_tokens`)
        — the transcript's opening user message IS this text.
        """
        fd, done_path = tempfile.mkstemp(prefix="diplomat-autofix-done-", suffix=".txt")
        os.close(fd)
        try:
            os.unlink(done_path)  # existence of this path later == the agent finished
        except OSError:
            pass
        try:
            review.spawn(prompt, self.terminal, done_path=done_path)
        except review.SpawnError:
            return False
        with self._inflight_lock:
            self._autofix_inflight.append(
                {
                    "url": url,
                    "number": number,
                    "done": done_path,
                    "at": time.time(),
                    "source": source,
                    "key": ledger_key,
                    "prompt": prompt,
                }
            )
        return True

    def _auto_tasks_running(self) -> int:
        """How many automatic agents are up on this device right now — the number
        the cap is compared against (:func:`autofix.running_auto_tasks`).

        The tracked rows say WHO started each agent, the ``ps`` scan says which are
        really alive; neither alone is enough, so the pure helper combines them."""
        self._prune_inflight()
        auto_prs: set[int] = set()
        manual_prs: set[int] = set()
        for e in self._autofix_inflight:
            bucket = manual_prs if e["source"] == autofix.SOURCE_PANEL else auto_prs
            bucket.add(e["number"])
        n = autofix.running_auto_tasks(self._live_pr_agents(), auto_prs, manual_prs)
        self._auto_tasks_measured = n
        return n

    def refresh_auto_task_count(self) -> None:
        """Re-measure for the display alone, signalling only on a change.

        The panel calls it on its own tick, including the ticks where nothing is
        tracked: an agent can be alive in ``ps`` with no in-flight record behind it
        (an applet restart loses the book, not the agents), and that is exactly when
        a wrongly-drawn free bay would be most misleading."""
        before = self._auto_tasks_measured
        self._auto_tasks_running()
        if self._auto_tasks_measured != before:
            self.tasks_changed.emit()

    def refresh_auto_task_count_async(self) -> None:
        """Measure off the UI thread — it shells out to ``ps`` (see
        :meth:`_live_pr_agents`)."""
        threading.Thread(target=self.refresh_auto_task_count, daemon=True).start()

    @property
    def auto_tasks_shown(self) -> int:
        """How many automatic agents the panel counts as running on this device.

        The higher of the last measurement and what the tracked records themselves
        say. Each is only a lower bound on the truth — the measurement can predate a
        spawn this very poll made, the records miss agents nobody tracked — and
        between two lower bounds the larger is the safer: it errs towards drawing one
        bay fewer, never towards offering a slot the gate would refuse."""
        tracked = len(
            {
                e["number"]
                for e in self._autofix_inflight
                if e["source"] != autofix.SOURCE_PANEL
            }
        )
        return max(self._auto_tasks_measured, tracked)

    @property
    def free_auto_slots(self) -> int:
        """Slots of this device's cap with nothing in them, as the panel draws
        them."""
        return autofix.free_slots(self.auto_task_limit, self.auto_tasks_shown)

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
        # Only PR-scoped work with a monitor behind it can be queued: a task nothing
        # can name is one the next poll cannot recognise as the same one, and a task
        # no monitor owns is one nothing would re-offer — the queue would be the only
        # record of it, which is precisely what this list is not. (Every automatic job
        # is both; the sweeps that are neither are panel-only, and a click is uncapped.)
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
        while the position is the same task's either way."""
        staged = self._staged_queue
        by_id = {e.id: e for e in staged}
        ordered = autofix.queue_order([e.id for e in staged], self.queued_task_order)
        self.queued_task_order = ordered
        before = self.queued_tasks
        self.queued_tasks = [by_id[k] for k in ordered]
        if self.queued_tasks != before:
            self.tasks_changed.emit()

    def _drain_queued_tasks(self) -> None:
        """Run the queue down into whatever room this device has, in the operator's
        order. This is what makes the drag order mean anything: it runs at the TOP of
        a poll, before the monitors offer their own finds, so a slot that freed since
        the last cycle goes to the work already waiting for it rather than to whatever
        this poll's fetch happens to list first.

        Capacity is re-counted per task because each spawn fills a slot. A spawn
        failure stops the drain: it means terminal automation is broken, not that this
        one task was unlucky, and each entry is taken off the list before it is tried —
        so walking the whole queue into the same failure would clear the panel of every
        queued row at once, for a reason none of them caused."""
        for entry in self.drainable_tasks:
            if self._auto_tasks_running() >= self.auto_task_limit:
                return
            # Finding room here is what re-arms the saturation notice. The gate's own
            # reset sits behind the capacity measurement this path skips, so without
            # this the feed would carry one `at-capacity` line for an unbounded run of
            # saturate-and-drain episodes instead of one apiece.
            self._capacity_logged = False
            self._drop_queued(entry.id)
            if self._run_queued_task(entry) == "failed":
                return

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
        # Unreachable: a job with no monitor behind it is never queued (_stage_queued).
        # Answering "not paused" keeps the unreachable case from being the one that
        # silently holds work back.
        return False

    @property
    def drainable_tasks(self) -> list[autofix.QueuedTask]:
        """The queued tasks the drain may start, in the operator's order —
        everything whose monitor is still on."""
        return [e for e in self.queued_tasks if not self.is_paused(e.job.counter)]

    def _drop_queued(self, task_id: str) -> None:
        """Take one task off the published queue (it is being started)."""
        remaining = [e for e in self.queued_tasks if e.id != task_id]
        if len(remaining) != len(self.queued_tasks):
            self.queued_tasks = remaining
            self.tasks_changed.emit()

    def _run_queued_task(self, entry: autofix.QueuedTask) -> str:
        """Dispatch one queued task past the capacity check its caller already made,
        and record the attempt its monitor would have recorded.

        That record is not bookkeeping polish: the whole retry ladder hangs off it. A
        queued dispatch that wrote none would look, to the very next poll after the
        agent exits, exactly like work never attempted — so an agent that finishes
        without clearing the conflict or leaving the review would be re-dispatched
        three minutes later, and again, with no backoff ever engaging."""
        verdict = self.dispatch_agent(
            entry.job, autofix.SOURCE_AUTO, entry.attempt, bypass_capacity=True
        )
        if verdict in ("spawned", autofix.VERDICT_STAND_DOWN):
            self._record_queued_attempt(entry)
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
        """The queued row's "execute now": start this task immediately, past the cap.

        It stays AUTO work — same ``Auto · `` label, same auto-handled counter, mesh
        routing still applies, and once running it occupies a slot like any other
        automatic agent, so the rest of the queue waits behind it. Of the four
        asymmetries the gate draws between a click and a monitor tick (capacity, mesh,
        counters, label) this borrows exactly one: the cap, which is the only one the
        operator is overriding.

        On a worker thread, like every other dispatch path: it assembles nothing but
        it does spawn a terminal, and on a live mesh it waits on a node round-trip."""
        entry = next((e for e in self.queued_tasks if e.id == task_id), None)
        if entry is None:
            return
        self._drop_queued(entry.id)

        def work() -> None:
            self._execute_queued_task(entry)

        threading.Thread(target=work, daemon=True).start()

    def _execute_queued_task(self, entry: autofix.QueuedTask) -> None:
        """One "execute now", start to finish.

        The feed line is written from the OUTCOME, never ahead of it: this is an auto
        job, so a mesh peer can own the work, the PR can have gained an agent since
        the list was built, and the spawn can fail — announcing "started" before
        asking would report a launch that never happened in all three."""
        verdict = self._run_queued_task(entry)
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
            self.error = f"{label}: the PR's author is banned (un-ban to review)."
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

    def _prune_inflight(self) -> None:
        now = time.time()
        # Under the lock, because pruning REPLACES the list: three threads reach it
        # (the poll worker, a panel click, and the sweep that re-measures the free
        # slots), and a spawn registering itself against the list a prune has already
        # copied would be dropped — leaving an agent nothing counts, which is a slot
        # of the cap the machine can then spend twice.
        #
        # What the finished ones cost is recorded AFTER the lock: the ledger write and
        # the signal it fires are neither short nor free of re-entry (a slot that asks
        # the store anything can land back in here), and this mutex is meant to be
        # held for a list swap and nothing else.
        finished: list[tuple[dict, float]] = []
        with self._inflight_lock:
            live: list[dict] = []
            for e in self._autofix_inflight:
                done = e.get("done")
                if bool(done) and os.path.exists(done):
                    # The sentinel's mtime is when `claude` actually exited; `now` is
                    # whenever a poll got round to looking, which is up to a poll
                    # period later and would inflate every run time by a random few
                    # minutes.
                    try:
                        finished_at = os.stat(done).st_mtime
                    except OSError:
                        finished_at = now
                    try:
                        os.unlink(done)
                    except OSError:
                        pass
                    finished.append((e, finished_at))
                    continue
                if now - e.get("at", 0) > self._AUTOFIX_INFLIGHT_TTL:
                    continue
                live.append(e)
            self._autofix_inflight = live
        for e, finished_at in finished:
            if e.get("key"):
                telemetry.record_completion(e["key"], e.get("prompt", ""),
                                            e.get("at", finished_at), finished_at)
                self.telemetry_changed.emit()

    def _in_flight(self, url: str) -> bool:
        self._prune_inflight()
        if any(e["url"] == url for e in self._autofix_inflight):
            return True
        # The in-memory list dies with the applet while the agents run on (and its
        # TTL can lapse under a long-running agent) — any slip used to guarantee a
        # duplicate dispatch, since the retry backoff (minutes) is far shorter than
        # an agent's runtime (an hour). A live `claude` whose argv references this
        # PR is in-flight no matter what our bookkeeping remembers.
        m = re.search(r"/pull/(\d+)", url)
        return m is not None and int(m.group(1)) in self._live_pr_agents()

    def _live_pr_agents(self) -> set[int]:
        """PR numbers with a live claude agent visible in ``ps`` right now (see
        :func:`autofix.live_pr_numbers`), cached briefly so one poll cycle costs
        one subprocess. Fails open to an empty set — the tracked list still
        dedups the common case."""
        now = time.time()
        cached = self._live_agents_cache
        if cached is not None and now - cached[0] < 5:
            return cached[1]
        try:
            out = subprocess.run(
                ["ps", "-eo", "args="], capture_output=True, text=True, timeout=10
            ).stdout
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            # UnicodeDecodeError: text=True decodes strict UTF-8, and any process on
            # the box with a non-UTF-8 byte in its argv makes `ps` output undecodable.
            # It is a ValueError, not an OSError/SubprocessError, so it must be caught
            # explicitly or it escapes this fail-open guard and wedges the poll worker.
            out = ""
        cfg = core.config()
        refs = autofix.live_pr_numbers(out, cfg["owner"], cfg["repo"])
        self._live_agents_cache = (now, refs)
        return refs

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
        """
        if not telemetry.sample_due():
            return

        def work() -> None:
            from . import quota, usagescan

            try:
                session, week = quota.fractions_left()
                totals = usagescan.totals()
            except OSError:
                return  # an unreadable ~/.claude costs this sample, nothing else
            telemetry.record_sample(session, week, totals.repo, totals.other)
            self.telemetry_changed.emit()

        threading.Thread(target=work, daemon=True).start()

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

        threading.Thread(target=work, daemon=True).start()

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
        threading.Thread(target=work, daemon=True).start()

    # MARK: activity feed + bans

    def refresh_activity(self) -> None:
        """Re-read the shared activity feed (audit.jsonl) and ban list (cheap tail /
        small-file reads) and signal on change. Runs on the panel's 8s poll."""
        from . import activity, bans

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
        threading.Thread(target=work, daemon=True).start()

    def install_allocator_async(self) -> None:
        def work() -> None:
            deviceallocator.ensure_deps()
            self.allocator_install = deviceallocator.install()
            self.allocator_setup_done = True
            self.allocator_changed.emit()
            self.refresh_device_state()
        threading.Thread(target=work, daemon=True).start()

    def uninstall_allocator_async(self) -> None:
        def work() -> None:
            self.allocator_install = deviceallocator.uninstall()
            # An explicit uninstall is a settled choice — don't auto-reinstall.
            self.allocator_setup_done = True
            self.allocator_changed.emit()
            self.refresh_device_state()
        threading.Thread(target=work, daemon=True).start()

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
        threading.Thread(target=work, daemon=True).start()

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
        threading.Thread(target=work, daemon=True).start()

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

            linux_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            # The node is a separate process and gets no say in who its host is, so
            # hand it both halves: the path to import Diplomat and the library from,
            # and the module that registers Diplomat behind it. Without the second
            # the node comes up on the library's own defaults — its own state
            # directory, no duty catalog of ours, no activity feed.
            env = {
                **os.environ,
                "SZPONTNET_HOST": "diplomat_app.szponthost",
                "PYTHONPATH": os.pathsep.join(
                    [linux_dir, szpont.package_dir(), os.environ.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep),
            }
            try:
                subprocess.Popen(  # noqa: S603 — relaunch ourselves as a node
                    [sys.executable, "-m", "szpontnet", "--daemon"],
                    cwd=linux_dir,
                    env=env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:  # noqa: BLE001
                self.mesh_error = f"could not start mesh node: {exc}"
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

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

        threading.Thread(target=work, daemon=True).start()

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

        threading.Thread(target=work, daemon=True, name=f"mesh-{what}").start()

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

        threading.Thread(target=work, daemon=True).start()

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
