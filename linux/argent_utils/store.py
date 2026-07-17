"""Application state, persisted settings, and the tool catalog.

A port of Store.swift. The tool catalog (titles, subtitles, colours, order) is
loaded from the shared ``core/catalog.json``; the row-mapping in ``items_for``
is the same dense formatting the macOS panel renders. Settings persist via
``QSettings`` (the Linux analogue of macOS UserDefaults).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QSettings, Signal

import json
import os
import tempfile
import threading
import time

from . import (
    activity,
    apiwatch,
    autofix,
    autofixmonitor,
    bans,
    conflicts,
    core,
    deviceallocator,
    review,
    tmuxwatch,
)
from .models import API, Filters, Fmt, OpenIssue, OpenPR
from .prtarget import PRTarget


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
    """One entry in the tool library, hydrated from core/catalog.json."""

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
    # Emitted after each Claude-API-error watcher scan (status pill + continue count).
    apiwatch_changed = Signal()

    _ORG = "argent-utils"
    _APP = "argent-utils"

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

        # Live telemetry read from the shared ~/.argent files (activity feed + bans).
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
        # In-flight auto-fix agents [{url, number, done, at}] — dedups against
        # spawning a second agent on a PR one is already working.
        self._autofix_inflight: list[dict] = []
        self._autofix_lock = threading.Lock()

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
        Argent Utils never opens a UDP/TCP node on the network unasked; the app
        auto-starts a node only once the user enables it in Settings.

        ``_mesh_enabled_override`` lets the headless render force it on without
        writing (and persisting) to the real user QSettings."""
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
        On by default (matches macOS). The background poll no-ops when this and
        review_requests_enabled are both off."""
        return self._settings.value("prAutofixEnabled", True, bool)

    @pr_autofix_enabled.setter
    def pr_autofix_enabled(self, value: bool) -> None:
        self._settings.setValue("prAutofixEnabled", bool(value))

    @property
    def review_requests_enabled(self) -> bool:
        """Full-E2E review PRs that request my review (read-only, never touches their
        branch), retrying an unaddressed review until it lands. On by default."""
        return self._settings.value("reviewRequestsEnabled", True, bool)

    @review_requests_enabled.setter
    def review_requests_enabled(self, value: bool) -> None:
        self._settings.setValue("reviewRequestsEnabled", bool(value))

    @property
    def auto_approve_enabled(self) -> bool:
        """Whether a clean auto-review may submit a verdict. Off by default: an
        auto-review never approves / requests-changes on my behalf until I opt in."""
        return self._settings.value("autoApproveEnabled", False, bool)

    @auto_approve_enabled.setter
    def auto_approve_enabled(self, value: bool) -> None:
        self._settings.setValue("autoApproveEnabled", bool(value))

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
        call from a QTimer whether or not the panel is open; no-ops when both toggles
        are off."""
        if not (self.pr_autofix_enabled or self.review_requests_enabled):
            return
        if not self._autofix_lock.acquire(blocking=False):
            return  # a poll is already running

        def work() -> None:
            try:
                self._autofix_poll_once()
            finally:
                self._autofix_lock.release()
                self.autofix_changed.emit()

        threading.Thread(target=work, daemon=True).start()

    def _autofix_poll_once(self) -> None:
        self._poll_error_this_cycle = None
        if not self.effective_me:
            self.fetch_me()
        if not self.effective_me:
            self._note_poll_failure(
                "GitHub login unknown — is `gh` installed and authenticated?"
            )
        else:
            cfg = core.config()
            owner, repo = cfg["owner"], cfg["repo"]
            if self.pr_autofix_enabled:
                self._poll_my_prs(owner, repo)
            if self.review_requests_enabled:
                self._poll_review_requests(owner, repo)
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
                attempts.get(k), "unresolved", self._in_flight(s.url), False, now
            )
            if action == "dispatch" and self._dispatch_my_review(s, int(val)):
                attempts[k] = autofix.ReviewAttempt("unresolved", now, int(val))
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
                attempts.get(k), "conflicting", self._in_flight(s.url), False, now
            )
            if action == "dispatch" and self._dispatch_conflict_fix(
                s.number, s.url, int(val), "auto"
            ):
                attempts[k] = autofix.ReviewAttempt("conflicting", now, int(val))
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
        for r in owed:
            k = str(r.number)
            stamp = r.requested_at or "-"
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

    def _dispatch_conflict_fix(
        self, number: int, url: str, attempt: int, source: str
    ) -> bool:
        if self._in_flight(url):
            return False
        prompt = conflicts.ConflictConfig(
            target=PRTarget.SPECIFIC, me=self.effective_me, specific_pr=str(number)
        ).build_prompt()
        if not self._spawn_tracked(prompt, url, number):
            activity.log(source, "spawn-failed", f"Resolve #{number} failed to spawn")
            return False
        retry = f" · retry {attempt}" if attempt > 1 else ""
        label = (
            f"Auto · Resolve · #{number}{retry}"
            if source == "auto"
            else f"Resolve · #{number}"
        )
        activity.log(source, "conflicts", label)
        # Count only what the monitor auto-fixed (a manual panel resolve isn't one).
        if attempt == 1 and source == "auto":
            self.autofix_conflicts_handled += 1
        return True

    def _dispatch_my_review(self, s, attempt: int = 1) -> bool:
        if self._in_flight(s.url):
            return False
        prompt = review.ReviewConfig(
            depth="deep",
            target=PRTarget.SPECIFIC,
            me=self.effective_me,
            mark_ready=False,
            leave_reviews=False,
            reply_to_reviews=True,
            specific_pr=str(s.number),
            specific_author=review.SpecificAuthor.MINE,
        ).build_prompt()
        if not self._spawn_tracked(prompt, s.url, s.number):
            activity.log("auto", "spawn-failed", f"Review #{s.number} failed to spawn")
            return False
        retry = f" · retry {attempt}" if attempt > 1 else ""
        activity.log("auto", "review-reply", f"Auto · Review · #{s.number}{retry}")
        if attempt == 1:
            self.autofix_reviews_handled += 1
        return True

    def _dispatch_review_request(self, r, attempt: int = 1) -> bool:
        if self._in_flight(r.url):
            return False
        reasons = self.verdict_policy.withhold_reasons(r.files, r.author_association)
        verdict = self.auto_approve_enabled and not reasons
        prompt = review.ReviewConfig(
            depth="max",
            target=PRTarget.SPECIFIC,
            me=self.effective_me,
            mark_ready=False,
            leave_reviews=True,
            reply_to_reviews=False,
            specific_pr=str(r.number),
            final_pass=verdict,
            specific_author=review.SpecificAuthor.THEIRS,
        ).build_prompt()
        if not self._spawn_tracked(prompt, r.url, r.number):
            activity.log("auto", "spawn-failed", f"Review-req #{r.number} failed to spawn")
            return False
        if verdict:
            tag = " +verdict"
        elif not self.auto_approve_enabled:
            tag = " -verdict (auto-approvals off)"
        else:
            tag = f" -verdict ({', '.join(reasons)})"
        retry = f" · retry {attempt}" if attempt > 1 else ""
        activity.log(
            "auto", "review-req", f"Auto · Review-req · #{r.number} (@{r.author}){tag}{retry}"
        )
        if attempt == 1:
            self.review_requests_handled += 1
        return True

    def _spawn_tracked(self, prompt: str, url: str, number: int) -> bool:
        """Spawn an agent with a completion sentinel and record it in-flight. Returns
        whether the terminal launched."""
        fd, done_path = tempfile.mkstemp(prefix="argent-autofix-done-", suffix=".txt")
        os.close(fd)
        try:
            os.unlink(done_path)  # existence of this path later == the agent finished
        except OSError:
            pass
        try:
            review.spawn(prompt, self.terminal, done_path=done_path)
        except review.SpawnError:
            return False
        self._autofix_inflight.append(
            {"url": url, "number": number, "done": done_path, "at": time.time()}
        )
        return True

    def _prune_inflight(self) -> None:
        now = time.time()
        live: list[dict] = []
        for e in self._autofix_inflight:
            done = e.get("done")
            finished = bool(done) and os.path.exists(done)
            if finished:
                try:
                    os.unlink(done)
                except OSError:
                    pass
                continue
            if now - e.get("at", 0) > self._AUTOFIX_INFLIGHT_TTL:
                continue
            live.append(e)
        self._autofix_inflight = live

    def _in_flight(self, url: str) -> bool:
        self._prune_inflight()
        return any(e["url"] == url for e in self._autofix_inflight)

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
        """One-time automatic install of the device-allocator MCP when Argent
        Utils is first set up. Skips when the package/node isn't available or the
        user has already settled it (installed or uninstalled in Settings). Only
        marks itself done once the install actually lands, so a transient failure
        (e.g. node missing) simply retries on a later launch."""
        if self.allocator_setup_done or not deviceallocator.package_available():
            return

        def work() -> None:
            status = deviceallocator.check()
            if status and status.get("installed"):
                self.allocator_install = status
                self.allocator_setup_done = True
                self.allocator_changed.emit()
                return
            # Not installed yet: pull the MCP server's runtime deps, then register.
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
        """Pull the checkout, rebuild argent-core, relaunch the applet.

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
                step(f"building argent-core at {commit}…")
                selfupdate.build_core()
                step("relaunching…")
                selfupdate.relaunch()
                self.update_state = {"phase": "restarting", "commit": commit}
            except selfupdate.UpdateError as exc:
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
        # Render mode pins a synthetic topology via the override — never let a
        # poll read (or clobber it with) the real ~/.argent/mesh/state.json.
        if self._mesh_enabled_override is not None:
            return

        from .mesh import statefile

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
        already alive. No-ops when disabled, so it's safe to call blindly on app
        start. Never runs in a headless render/test (guarded by mesh_enabled,
        which those paths leave off / stub)."""
        from .mesh import statefile

        if not self.mesh_enabled or statefile.node_running():
            self.refresh_mesh_state()
            return

        def work() -> None:
            import os
            import subprocess
            import sys

            linux_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            try:
                subprocess.Popen(  # noqa: S603 — relaunch ourselves as a node
                    [sys.executable, "-m", "argent_utils.mesh", "--daemon"],
                    cwd=linux_dir,
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
        from .mesh import ctl

        def work() -> None:
            try:
                ctl.stop()
            except ctl.CtlError:
                pass  # already down — nothing to stop
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

    def mesh_set_attr(self, node_id: str, attrs: dict) -> None:
        """Edit a node's attributes (self or a peer, forwarded over the mesh).
        Runs on a daemon thread; a CtlError lands in `mesh_error` for the view."""
        from .mesh import ctl

        def work() -> None:
            try:
                ctl.set_attr(node_id, attrs)
                self.mesh_error = None
            except ctl.CtlError as exc:
                self.mesh_error = str(exc)
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

    def mesh_trust(self, fingerprint: str, label: str = "") -> None:
        """Mark a peer's device Personal — add its proven fingerprint to the local
        trusted allowlist (so its mesh requests run as if triggered here)."""
        from .mesh import ctl

        def work() -> None:
            try:
                ctl.trust_device(fingerprint, label)
                self.mesh_error = None
            except ctl.CtlError as exc:
                self.mesh_error = str(exc)
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

    def mesh_untrust(self, fingerprint: str) -> None:
        """Mark a peer's device Foreign — remove its fingerprint from the allowlist."""
        from .mesh import ctl

        def work() -> None:
            try:
                ctl.untrust_device(fingerprint)
                self.mesh_error = None
            except ctl.CtlError as exc:
                self.mesh_error = str(exc)
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

    def mesh_set_overrides(self, duty: str, placement: dict) -> None:
        """Edit one duty's mesh-wide placement (gossiped last-writer-wins)."""
        from .mesh import ctl

        def work() -> None:
            try:
                ctl.set_overrides(duty, placement)
                self.mesh_error = None
            except ctl.CtlError as exc:
                self.mesh_error = str(exc)
            self.refresh_mesh_state()

        threading.Thread(target=work, daemon=True).start()

    def mesh_dispatch(self, duty: str, prompt: str, done_callback=None) -> None:
        """Route a job through the mesh; `done_callback(results, error)` fires on
        the worker thread (callers marshal back to the UI thread themselves)."""
        from .mesh import ctl

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

    def items_for(self, tool_id: str) -> list[DisplayItem]:
        if tool_id == "skillPRs":
            out = []
            for p in sorted(Filters.skill_prs(self.prs), key=lambda p: -p.number):
                skills = ", ".join(
                    Fmt.skill_name(f) for f in p.files if Filters.is_skill_file(f)
                )
                out.append(
                    DisplayItem(
                        id=p.number,
                        badge=f"#{p.number}",
                        title=p.title,
                        url=p.url,
                        line2=f"@{p.author} · {Fmt.age(p.created_at)} · {'draft' if p.is_draft else 'ready'}",
                        line3=f"skills: {skills}",
                    )
                )
            return out

        if tool_id == "installerPRs":
            out = []
            for p in sorted(Filters.installer_prs(self.prs), key=lambda p: -p.number):
                fs = [f for f in p.files if Filters.is_installer_file(f)]
                plural = "" if len(fs) == 1 else "s"
                out.append(
                    DisplayItem(
                        id=p.number,
                        badge=f"#{p.number}",
                        title=p.title,
                        url=p.url,
                        line2=f"@{p.author} · {Fmt.age(p.created_at)} · {len(fs)} file{plural}",
                        line3="\n".join(Fmt.short_path(f) for f in fs),
                    )
                )
            return out

        if tool_id == "staleReady":
            out = []
            for p in sorted(Filters.stale_ready_prs(self.prs), key=lambda p: p.ready_at):
                d = Fmt.days(p.ready_at)
                kind = "born-ready" if p.ready_for_review_at is None else "converted"
                out.append(
                    DisplayItem(
                        id=p.number,
                        badge=f"#{p.number}",
                        title=p.title,
                        url=p.url,
                        line2=f"@{p.author} · ready {d}d · {kind}",
                        line3=None,
                    )
                )
            return out

        if tool_id == "unaddressedIssues":
            out = []
            for i in sorted(
                Filters.unaddressed_external_issues(self.issues),
                key=lambda i: i.created_at,
            ):
                line3 = (
                    f"labels: {', '.join(i.labels)}" if i.labels else None
                )
                out.append(
                    DisplayItem(
                        id=i.number,
                        badge=f"#{i.number}",
                        title=i.title,
                        url=i.url,
                        line2=f"@{i.author} [{i.author_association}] · {Fmt.age(i.created_at)} · {i.comment_count}c",
                        line3=line3,
                    )
                )
            return out

        if tool_id == "myApproved":
            out = []
            for p in sorted(
                Filters.my_approved_prs(self.prs, self.effective_me),
                key=lambda p: -p.number,
            ):
                out.append(
                    DisplayItem(
                        id=p.number,
                        badge=f"#{p.number}",
                        title=p.title,
                        url=p.url,
                        line2=f"@{p.author} · {Fmt.age(p.created_at)} · approved · {'draft' if p.is_draft else 'ready'}",
                        line3=None,
                    )
                )
            return out

        if tool_id == "myUnaddressed":
            out = []
            for p in sorted(
                Filters.my_unaddressed_review_prs(self.prs, self.effective_me),
                key=lambda p: -p.number,
            ):
                n = len(p.unaddressed_threads(self.effective_me))
                plural = "" if n == 1 else "s"
                out.append(
                    DisplayItem(
                        id=p.number,
                        badge=f"#{p.number}",
                        title=p.title,
                        url=p.url,
                        line2=f"@{p.author} · {Fmt.age(p.created_at)} · {n} open thread{plural}",
                        line3=None,
                    )
                )
            return out

        return []
