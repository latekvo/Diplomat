"""The Comms screen — what the Slack watcher has seen, and what came of it.

One of the panel's screens (``_screens["comms"]``), opened from the ✉ header button.
It draws the folded Comms log (``Store.comms_items``, from ``diplomat-core
comms-feed``) as two lists:

* **Pings**: every message that reached the operator, newest first — sender, where,
  why it counted (DM / @mention / keyword), the text, what the triage decided and
  what came of it, and a way back to it in Slack. The ones recorded since the screen
  was last opened are highlighted.
* **Jobs**: every ping that put work in the queue, and how far that work has got —
  queued, running, done — with the investigation's report once it is written.

With no token to watch with, the screen is instead what to create and where to put it.
The macOS twin is ``CommsView.swift``; both draw the same fold, so they can only
differ in how they look.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from diplomat_runtime import comms
from diplomat_runtime.models import Fmt
from . import glyphs
from .store import Store
from .widgets import GlyphLabel, muted, tint_bg

#: The Comms category's tint (assets/audit-categories.json → comms.colorHex).
TINT = "#007AFF"
_RED, _GREEN, _ORANGE, _GREY = "#FF3B30", "#34C759", "#FF9500", glyphs.MUTED

SCOPES = "search:read"


def token_command() -> str:
    """What the empty state tells the operator to run to store the token where it is
    read from: the umask creates the file 0600, and the token is read without echo,
    so it is neither on screen nor in the shell's history."""
    path = comms.token_file()
    shown = (comms.DEFAULT_TOKEN_FILE if path == os.path.expanduser(comms.DEFAULT_TOKEN_FILE)
             else shlex.quote(path))
    return f"(umask 077; read -rs t; printf '%s\\n' \"$t\" > {shown})"


def _card() -> tuple[QFrame, QVBoxLayout]:
    """A grouped block on the card fill. Not ``widgets.card_host``: its unscoped
    stylesheet paints the fill behind every label inside too, and these cards hold
    rows with fills of their own."""
    host = QFrame()
    host.setObjectName("commsCard")
    host.setStyleSheet("#commsCard { background-color: rgba(128,128,128,0.07);"
                       " border-radius: 8px; }")
    col = QVBoxLayout(host)
    col.setContentsMargins(8, 8, 8, 8)
    col.setSpacing(6)
    return host, col


_REASON_WORD = {"dm": "DM", "mention": "@mention"}
_ACTION_WORD = {"review": "review", "fix_issue": "fix issue", "bump": "bump",
                "investigate": "investigate", "none": "no action"}


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _plain(text: str) -> QLabel:
    """A label that never reads its text as markup: what these show comes from Slack,
    from a model's triage of a Slack message, or from the work that triage named."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _chip(text: str, tint: str) -> QLabel:
    chip = _plain(text)
    chip.setStyleSheet(
        f"color: {tint}; background-color: {tint_bg(tint, 0.15)}; border-radius: 5px;"
        " padding: 1px 5px; font-size: 8px; font-weight: 700;"
    )
    return chip


def _clock(iso: str | None) -> str:
    when = comms.parse_iso(iso or "")
    return Fmt.clock(when) if when else "—"


def reason_chips(message: dict) -> list[str]:
    """The ping's reasons as its chips read: ``DM``, ``@mention``, ``keyword:x``."""
    out = []
    for r in message.get("reasons") or []:
        if r == "keyword":
            out.append(f"keyword:{message.get('keyword') or ''}")
        else:
            out.append(_REASON_WORD.get(r, r))
    return out


def report_path(item: dict) -> Path | None:
    """An investigation's report, once its agent has written it."""
    key = str(item.get("queueKey") or "")
    if not key.startswith("investigate:"):
        return None
    path = comms.reports_dir() / f"{key.split(':', 1)[1]}.md"
    return path if path.is_file() else None


def job_status(item: dict, waiting: set[str]) -> str:
    """``done`` / ``running`` / ``queued`` — or ``not queued`` for an ask that left
    the queue without starting (cancelled, or retired by its PR closing)."""
    if item.get("finishedAt"):
        return "done"
    if item.get("startedAt"):
        return "running"
    return "queued" if item.get("queueKey") in waiting else "not queued"


class PingRow(QFrame):
    """One ping: who, where, why, the text, the triage and its outcome."""

    def __init__(self, item: dict, *, unread: bool) -> None:
        super().__init__()
        self.setObjectName("pingRow")
        fill = tint_bg(TINT, 0.12) if unread else "rgba(128,128,128,0.05)"
        edge = TINT if unread else "transparent"
        self.setStyleSheet(
            f"#pingRow {{ background-color: {fill}; border-radius: 6px;"
            f" border-left: 3px solid {edge}; }}"
        )
        message = item.get("message") or {}
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        who = _plain(Store.comms_sender(message))
        who.setStyleSheet("font-weight: 700; font-size: 11px;")
        head.addWidget(who)
        where = _plain(Store.comms_place(message))
        where.setStyleSheet(muted(10))
        head.addWidget(where)
        for chip in reason_chips(message):
            head.addWidget(_chip(chip, TINT))
        head.addStretch(1)
        if unread:
            head.addWidget(_chip("NEW", TINT))
        clock = QLabel(_clock(item.get("at")))
        clock.setStyleSheet(muted(9, mono=True))
        head.addWidget(clock)
        col.addLayout(head)

        text = _plain(str(message.get("text", "")))
        text.setWordWrap(True)
        text.setStyleSheet("font-size: 10px;")
        # Three lines, as the macOS row clamps it: a pasted stack trace must not push
        # every other ping off the screen.
        line = text.fontMetrics().lineSpacing()
        text.setMaximumHeight(line * 3 + 2)
        col.addWidget(text)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        decision = item.get("decision")
        if decision:
            action = str(decision.get("action", "none"))
            word = _ACTION_WORD.get(action, action)
            if decision.get("number"):
                word += f" #{decision['number']}"
            foot.addWidget(_chip(word.upper(), _GREY if action == "none" else _GREEN))
            summary = _plain(str(decision.get("summary") or decision.get("reason") or ""))
            summary.setStyleSheet(muted(9))
            summary.setWordWrap(True)
            foot.addWidget(summary, 1)
        else:
            pending = QLabel("awaiting triage")
            pending.setStyleSheet(muted(9))
            foot.addWidget(pending, 1)
        permalink = str(message.get("permalink") or "")
        if permalink:
            open_btn = QPushButton("Open in Slack")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.setStyleSheet("font-size: 9px; padding: 2px 6px;")
            open_btn.clicked.connect(
                lambda _=False, url=permalink: QDesktopServices.openUrl(QUrl(url)))
            foot.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(foot)

        if item.get("outcome"):
            outcome = _plain(f"→ {item['outcome']}")
            outcome.setWordWrap(True)
            outcome.setStyleSheet(muted(9))
            col.addWidget(outcome)


class JobRow(QFrame):
    """One piece of work a ping queued, and how far it has got."""

    _STATUS_TINT = {"done": _GREEN, "running": glyphs.AGENT_LIVE, "queued": _ORANGE,
                    "not queued": _GREY}

    def __init__(self, item: dict, status: str) -> None:
        super().__init__()
        self.setObjectName("jobRow")
        self.setStyleSheet("#jobRow { background-color: rgba(128,128,128,0.05);"
                           " border-radius: 6px; }")
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        label = _plain(str(item.get("label") or item.get("queueKey") or ""))
        label.setWordWrap(True)
        label.setStyleSheet("font-size: 10px; font-weight: 600;")
        head.addWidget(label, 1)
        head.addWidget(_chip(status.upper(), self._STATUS_TINT.get(status, _GREY)),
                       0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)

        times = [f"asked {_clock(item.get('at'))}"]
        if item.get("startedAt"):
            times.append(f"started {_clock(item['startedAt'])}")
        if item.get("finishedAt"):
            times.append(f"done {_clock(item['finishedAt'])}")
        foot = QHBoxLayout()
        when = QLabel(" · ".join(times))
        when.setStyleSheet(muted(9, mono=True))
        foot.addWidget(when, 1)
        report = report_path(item)
        if report is not None:
            btn = QPushButton("Report")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("font-size: 9px; padding: 2px 6px;")
            btn.clicked.connect(
                lambda _=False, p=str(report): QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
            foot.addWidget(btn)
        col.addLayout(foot)


class CommsView(QWidget):
    """The Comms screen. Emits ``done`` when the user is finished, like the other
    screens."""

    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("CommsView { background: transparent; }")
        #: `commsSeenAt` as it stood when the screen was opened: what decides which
        #: rows are drawn as new, since opening the screen moves the setting itself.
        self._seen_before = store.comms_seen_at

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addLayout(self._build_header())

        self.status = _plain("")
        self.status.setStyleSheet(muted(10))
        self.status.setWordWrap(True)
        col.addWidget(self.status)

        self.error_banner = _plain("")
        self.error_banner.setWordWrap(True)
        self.error_banner.setStyleSheet(
            f"background-color: {tint_bg(_RED, 0.85)}; color: white; border-radius: 6px;"
            " padding: 6px; font-size: 10px;"
        )
        col.addWidget(self.error_banner)

        self.empty = self._build_empty()
        col.addWidget(self.empty)

        self.body = QWidget()
        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        self.pings_host, self.pings_col = _card()
        self.jobs_host, self.jobs_col = _card()
        for host in (self.pings_host, self.jobs_host):
            host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        left = QVBoxLayout()
        left.addWidget(self.pings_host)
        left.addStretch(1)
        right = QVBoxLayout()
        right.addWidget(self.jobs_host)
        right.addStretch(1)
        body.addLayout(left, 3)
        body.addLayout(right, 2)
        col.addWidget(self.body)
        col.addStretch(1)

        store.comms_changed.connect(self.rebuild)
        store.tasks_changed.connect(self.rebuild)
        self.rebuild()

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_COMMS, 16, TINT, font_px=14))
        title = QLabel("Comms")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        row.addStretch(1)
        self.poll_btn = QPushButton("Poll now")
        self.poll_btn.setToolTip("Ask Slack for new pings now rather than at the next tick")
        self.poll_btn.clicked.connect(self.store.run_comms_poll_async)
        row.addWidget(self.poll_btn)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _build_empty(self) -> QWidget:
        host, col = _card()
        col.setContentsMargins(14, 14, 14, 14)
        col.setSpacing(8)
        head = QLabel("Comms needs a Slack user token")
        head.setStyleSheet("font-weight: 700; font-size: 12px;")
        col.addWidget(head)
        steps = [
            "1. Create a Slack app in your workspace (api.slack.com/apps) and give it the "
            f"User Token Scopes  {SCOPES}. Install it and copy the User OAuth Token "
            "(xoxp-…).",
            "2. Store it where only you can read it — paste it after running this, then "
            "press Return:",
        ]
        for text in steps:
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet("font-size: 10px;")
            col.addWidget(label)
        self.empty_cmd = QLabel("")
        self.empty_cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.empty_cmd.setStyleSheet(
            "font-family: monospace; font-size: 10px; padding: 6px;"
            " background-color: rgba(128,128,128,0.15); border-radius: 5px;")
        col.addWidget(self.empty_cmd)
        self.empty_path = QLabel("")
        self.empty_path.setWordWrap(True)
        self.empty_path.setStyleSheet(muted(10))
        col.addWidget(self.empty_path)
        last = QLabel("3. Turn Comms on in Settings. The token never enters "
                      "~/.diplomat/config.json — only the file's path does.")
        last.setWordWrap(True)
        last.setStyleSheet("font-size: 10px;")
        col.addWidget(last)
        return host

    def open(self) -> None:
        """The screen is being shown: remember what was unread, then mark it read."""
        self._seen_before = self.store.comms_seen_at
        self.store.mark_comms_seen()
        self.rebuild()

    # MARK: rebuild

    def _status_text(self) -> str:
        st = self.store
        if not st.comms_enabled:
            return "Off — turn Comms on in Settings to watch Slack."
        me = st.comms_identity or {}
        if me.get("handle"):
            text = f"Watching as @{me['handle']} in {me.get('teamID', '')}"
        else:
            text = "Watching Slack"
        when = st.comms_last_poll
        return text + (f" · last poll {Fmt.clock(when)}" if when
                       else " · waiting for the first poll")

    def rebuild(self) -> None:
        configured = self.store.comms_configured
        self.empty.setVisible(not configured)
        self.empty_cmd.setText(token_command())
        self.empty_path.setText(f"Comms reads it from {comms.token_file()}; "
                                "Settings → Comms changes the path.")
        self.status.setText(self._status_text())
        error = self.store.comms_error
        self.error_banner.setVisible(bool(error))
        self.error_banner.setText(f"⚠ {error}" if error else "")

        items = self.store.comms_items
        self.body.setVisible(configured or bool(items))
        seen = comms.parse_iso(self._seen_before)

        _clear(self.pings_col)
        self.pings_col.addLayout(self._section_head("PINGS", len(items)))
        if not items:
            none = QLabel("No pings yet. A DM, an @-mention or a keyword lands here.")
            none.setStyleSheet(muted(10))
            none.setWordWrap(True)
            self.pings_col.addWidget(none)
        for item in items:
            at = comms.parse_iso(str(item.get("at", "")))
            unread = seen is None or (at is not None and at > seen)
            self.pings_col.addWidget(PingRow(item, unread=unread))

        jobs = [i for i in items if i.get("queueKey")]
        waiting = ({t.id for t in self.store.queued_tasks}
                   | {t.id for t in self.store.starting_tasks})
        _clear(self.jobs_col)
        self.jobs_col.addLayout(self._section_head("JOBS", len(jobs)))
        if not jobs:
            none = QLabel("Nothing queued from Slack yet.")
            none.setStyleSheet(muted(10))
            self.jobs_col.addWidget(none)
        for item in jobs:
            self.jobs_col.addWidget(JobRow(item, job_status(item, waiting)))

    @staticmethod
    def _section_head(title: str, count: int) -> QHBoxLayout:
        row = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet(muted(10, bold=True))
        row.addWidget(t)
        c = QLabel(str(count))
        c.setStyleSheet(muted(10, mono=True))
        row.addWidget(c)
        row.addStretch(1)
        return row
