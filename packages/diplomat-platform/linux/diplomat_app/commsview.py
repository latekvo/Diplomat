"""The Comms screen — what the Slack watcher has seen, and what came of it.

Draws the folded Comms log (``Store.comms_items``, from ``diplomat-core comms-feed``)
as **Pings** (every message that reached the operator, newest first) and **Jobs**
(every ping that queued work, and how far it has got). The macOS twin
``CommsView.swift`` draws the same fold.
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
from . import bans, glyphs
from .store import Store
from .widgets import GlyphLabel, muted, plain_tooltip, tint_bg

#: assets/audit-categories.json → comms.colorHex.
TINT = "#007AFF"
_RED, _GREEN, _ORANGE, _GREY = "#FF3B30", "#34C759", "#FF9500", glyphs.MUTED

SCOPES = "search:read"


def token_command() -> str:
    """The empty state's line for storing the token: the umask creates the file 0600,
    and ``read -s`` keeps the token off screen and out of the shell's history."""
    path = comms.token_file()
    shown = (comms.DEFAULT_TOKEN_FILE if path == os.path.expanduser(comms.DEFAULT_TOKEN_FILE)
             else shlex.quote(path))
    return f"(umask 077; read -rs t; printf '%s\\n' \"$t\" > {shown})"


def _card() -> tuple[QFrame, QVBoxLayout]:
    """Not ``widgets.card_host``: its unscoped stylesheet paints the fill behind every
    label inside too, and these cards hold rows with fills of their own."""
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
                "investigate": "investigate", "injection": "injection", "none": "no action"}


def ban_mark(item: dict) -> str | None:
    """The red chip on a ping: ``INJECTION · BANNED`` for the one that got its sender
    banned, ``BANNED SENDER`` for one a banned sender sent, and ``BANNED`` for an ask
    whose run a ban stopped."""
    decision = item.get("decision") or {}
    outcome = str(item.get("outcome") or "")
    if (decision.get("action") == "injection"
            and outcome.startswith(Store.COMMS_BANNED_OUTCOME_PREFIX)):
        return "INJECTION · BANNED"
    if comms.from_banned_sender(decision) or outcome == Store.COMMS_SENDER_BANNED_OUTCOME:
        return "BANNED SENDER"
    if Store.comms_stopped_by_ban(item):
        return "BANNED"
    return None


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
    """Never markup: the text comes from Slack, directly or through a triage of it."""
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
    out = []
    for r in message.get("reasons") or []:
        if r == "keyword":
            out.append(f"keyword:{message.get('keyword') or ''}")
        else:
            out.append(_REASON_WORD.get(r, r))
    return out


def report_path(item: dict) -> Path | None:
    key = str(item.get("queueKey") or "")
    if not key.startswith("investigate:"):
        return None
    path = comms.reports_dir() / f"{key.split(':', 1)[1]}.md"
    return path if path.is_file() else None


def job_status(item: dict, waiting: set[str]) -> str:
    """``not queued``: the ask left the queue without starting (cancelled, retired by
    its PR closing, or dispatched to a terminal that never ran it)."""
    if item.get("finishedAt"):
        return "done"
    if item.get("startedAt") and not comms.never_started(item):
        return "running"
    return "queued" if item.get("queueKey") in waiting else "not queued"


class PingRow(QFrame):
    def __init__(self, item: dict, *, unread: bool, mark: str | None = None) -> None:
        super().__init__()
        self.setObjectName("pingRow")
        fill = tint_bg(TINT, 0.12) if unread else "rgba(128,128,128,0.05)"
        edge = _RED if mark else TINT if unread else "transparent"
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
        if mark:
            head.addWidget(_chip(f"{glyphs.G_BAN} {mark}", _RED))
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
        # Three lines, as on macOS: a pasted stack trace must not push every other ping
        # off the screen.
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
            tint = {"none": _GREY, "injection": _RED}.get(action, _GREEN)
            foot.addWidget(_chip(word.upper(), tint))
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


class BannedSenderRow(QFrame):
    """One banned Slack sender: who, when, the words that did it, and the message."""

    def __init__(self, ban: bans.BannedAuthor) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(GlyphLabel(glyphs.G_BAN, 14, _RED, font_px=12))
        who = _plain(ban.name)
        who.setStyleSheet("font-weight: 700; font-size: 10px;")
        head.addWidget(who)
        uid = _plain(str(ban.slack_id))
        uid.setStyleSheet(muted(9, mono=True))
        head.addWidget(uid)
        if ban.place:
            place = _plain(ban.place)
            place.setStyleSheet(muted(9))
            head.addWidget(place)
        when = _plain(bans.when_text(ban))
        when.setStyleSheet(muted(9))
        head.addWidget(when, 1)
        url = bans.place_url(ban)
        if url:
            btn = QPushButton("Open in Slack")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("font-size: 9px; padding: 2px 6px;")
            btn.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            head.addWidget(btn)
        col.addLayout(head)
        why = ban.evidence or ban.reason or ""
        if why:
            quote = _plain(f"“{' '.join(why.split())}”" if ban.evidence else why)
            quote.setWordWrap(True)
            quote.setStyleSheet("font-size: 9px; padding-left: 20px;")
            quote.setMaximumHeight(quote.fontMetrics().lineSpacing() * 2 + 2)
            quote.setToolTip(plain_tooltip(why))
            col.addWidget(quote)


class CommsView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("CommsView { background: transparent; }")
        #: `commsSeenAt` as of opening, which decides the rows drawn as new: opening
        #: moves the setting itself.
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

        self.bans_host, self.bans_col = _card()
        self.bans_host.setStyleSheet(
            f"#commsCard {{ background-color: {tint_bg(_RED, 0.07)}; border-radius: 8px; }}")
        col.addWidget(self.bans_host)

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
        # The ban list is re-read with the activity feed.
        store.activity_changed.connect(self.rebuild)
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

        senders = sorted((b for b in self.store.banned_authors if b.is_slack),
                         key=lambda b: b.at or "", reverse=True)
        _clear(self.bans_col)
        self.bans_host.setVisible(bool(senders))
        if senders:
            self.bans_col.addLayout(self._section_head("BANNED SENDERS", len(senders)))
            note = QLabel("Prompt injection: their messages are never triaged. "
                          "Un-ban in the panel's Banned list.")
            note.setStyleSheet(muted(9))
            note.setWordWrap(True)
            self.bans_col.addWidget(note)
            for b in senders:
                self.bans_col.addWidget(BannedSenderRow(b))

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
            self.pings_col.addWidget(PingRow(item, unread=unread,
                                             mark=ban_mark(item)))

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
