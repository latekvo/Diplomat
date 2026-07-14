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

import threading

from . import core, deviceallocator, review
from .models import API, Filters, Fmt, OpenIssue, OpenPR


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

    _ORG = "argent-utils"
    _APP = "argent-utils"

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
