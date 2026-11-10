"""The Comms screen — what the Slack watcher has seen, and what came of it.

Draws the folded Comms log (``Store.comms_items``, from ``diplomat-core comms-feed``)
as **Pings** (every message that reached the operator, newest first, one line each,
the read ones under a closed "Earlier" disclosure) and **Jobs** (every ping that queued
work, and how far it has got). The macOS twin ``CommsView.swift`` draws the same fold.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication,
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
from .widgets import ElidedLabel, GlyphLabel, muted, plain_tooltip, tint_bg

#: assets/audit-categories.json → comms.colorHex.
TINT = "#007AFF"
_RED, _GREEN, _ORANGE, _GREY = "#FF3B30", "#34C759", "#FF9500", glyphs.MUTED
_PURPLE = "#AF52DE"


def _text_color() -> str:
    """ElidedLabel paints with an explicit pen, which the theme does not reach."""
    return QApplication.palette().color(QPalette.ColorRole.WindowText).name()

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


def action_chip(decision: dict | None) -> tuple[str, str]:
    """The triage's pill on a ping: its word (and number, as ``CommsSettings.ref``
    writes it) and tint."""
    if not decision:
        return "AWAITING TRIAGE", _GREY
    action = str(decision.get("action", "none"))
    word = _ACTION_WORD.get(action, action)
    if decision.get("number"):
        repo = str(((decision.get("targets") or [{}])[0] or {}).get("repo") or "")
        word += f" {repo.rpartition('/')[2]}#{decision['number']}"
    return word.upper(), {"none": _GREY, "injection": _RED, "question": _PURPLE}.get(
        action, _GREEN)


def reply_chip(item: dict) -> tuple[str, str] | None:
    """What came of Comms' reply in Slack, if one was due (``replyOutcome``)."""
    outcome = str(item.get("replyOutcome") or "")
    if outcome == "sent":
        return "REPLIED", _GREEN
    if outcome == "sending":
        return "REPLYING", _ORANGE
    if outcome.startswith("failed"):
        return "REPLY FAILED", _RED
    if outcome.startswith("skipped"):
        return "NOT ANSWERED", _GREY
    return None


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _small_button(text: str, on_click) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet("font-size: 9px; padding: 1px 6px;")
    btn.clicked.connect(lambda _=False: on_click())
    return btn


class PingRow(QFrame):
    """One line: unread dot, sender, place, the triage's pill, the text cut to fit,
    the reply's pill and the time. A click opens the rest under it."""

    #: (message id, now expanded)
    toggled = Signal(str, bool)

    def __init__(self, item: dict, *, unread: bool, mark: str | None = None,
                 expanded: bool = False) -> None:
        super().__init__()
        self.item = item
        self.unread = unread
        self.mark = mark
        self.details: QWidget | None = None
        self.setObjectName("pingRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        fill = tint_bg(TINT, 0.12) if unread else "rgba(128,128,128,0.05)"
        edge = _RED if mark else TINT if unread else "transparent"
        self.setStyleSheet(
            f"#pingRow {{ background-color: {fill}; border-radius: 6px;"
            f" border-left: 3px solid {edge}; }}"
        )
        message = item.get("message") or {}
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(6, 3, 6, 3)
        self._col.setSpacing(4)

        line = QHBoxLayout()
        line.setSpacing(6)
        self.dot = QLabel("●" if unread else "")
        self.dot.setFixedWidth(8)
        self.dot.setStyleSheet(f"color: {TINT}; font-size: 8px;")
        line.addWidget(self.dot)
        who = _plain(Store.comms_sender(message))
        who.setStyleSheet("font-weight: 700; font-size: 10px;")
        line.addWidget(who)
        where = _plain(Store.comms_place(message))
        where.setStyleSheet(muted(9))
        line.addWidget(where)
        line.addWidget(_chip(*action_chip(item.get("decision"))))
        if mark:
            line.addWidget(_chip(f"{glyphs.G_BAN} {mark}", _RED))
        self.text = ElidedLabel(_flat(message.get("text", "")), 10, _text_color())
        line.addWidget(self.text, 1)
        reply = reply_chip(item)
        if reply:
            line.addWidget(_chip(*reply))
        clock = QLabel(_clock(item.get("at")))
        clock.setStyleSheet(muted(9, mono=True))
        line.addWidget(clock)
        self._col.addLayout(line)
        if expanded:
            self.set_expanded(True)

    @property
    def expanded(self) -> bool:
        return self.details is not None and not self.details.isHidden()

    def set_expanded(self, on: bool) -> None:
        """Built on the first open: a feed holds up to 200 pings."""
        if on and self.details is None:
            self.details = self._build_details()
            self._col.addWidget(self.details)
        if self.details is not None:
            self.details.setVisible(on)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_expanded(not self.expanded)
            self.toggled.emit(str((self.item.get("message") or {}).get("id", "")),
                              self.expanded)
        super().mouseReleaseEvent(event)

    def _build_details(self) -> QWidget:
        item, message = self.item, self.item.get("message") or {}
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(14, 0, 0, 2)
        col.setSpacing(3)

        def line(text: str, style: str) -> None:
            label = _plain(text)
            label.setWordWrap(True)
            label.setStyleSheet(style)
            col.addWidget(label)

        head = QHBoxLayout()
        head.setSpacing(6)
        for chip in reason_chips(message):
            head.addWidget(_chip(chip, TINT))
        head.addStretch(1)
        permalink = str(message.get("permalink") or "")
        if permalink:
            head.addWidget(_small_button(
                "Open in Slack", lambda url=permalink: QDesktopServices.openUrl(QUrl(url))))
        col.addLayout(head)
        line(str(message.get("text", "")), "font-size: 10px;")
        decision = item.get("decision")
        if decision:
            why = " — ".join(t for t in (str(decision.get("summary") or ""),
                                         str(decision.get("reason") or "")) if t)
            if why:
                line(why, muted(9))
        if item.get("outcome"):
            line(f"→ {item['outcome']}", muted(9))
        reply, outcome = item.get("reply"), str(item.get("replyOutcome") or "")
        if reply:
            line(f"↩ {reply}", "font-size: 9px;")
        if outcome and outcome != "sent":
            line(f"↩ reply {outcome}", muted(9))
        return host


class JobRow(QFrame):
    """One line: the job, its latest time (every time in the tooltip), the report of an
    investigation, and its status."""

    _STATUS_TINT = {"done": _GREEN, "running": glyphs.AGENT_LIVE, "queued": _ORANGE,
                    "not queued": _GREY}

    def __init__(self, item: dict, status: str) -> None:
        super().__init__()
        self.setObjectName("jobRow")
        self.setStyleSheet("#jobRow { background-color: rgba(128,128,128,0.05);"
                           " border-radius: 6px; }")
        line = QHBoxLayout(self)
        line.setContentsMargins(8, 3, 8, 3)
        line.setSpacing(6)
        self.label = ElidedLabel(str(item.get("label") or item.get("queueKey") or ""),
                                 10, _text_color())
        line.addWidget(self.label, 1)

        times = [f"asked {_clock(item.get('at'))}"]
        if item.get("startedAt"):
            times.append(f"started {_clock(item['startedAt'])}")
        if item.get("finishedAt"):
            times.append(f"done {_clock(item['finishedAt'])}")
        self.when = QLabel(times[-1])
        self.when.setStyleSheet(muted(9, mono=True))
        self.setToolTip(" · ".join(times))
        line.addWidget(self.when)
        report = report_path(item)
        if report is not None:
            line.addWidget(_small_button(
                "Report",
                lambda p=str(report): QDesktopServices.openUrl(QUrl.fromLocalFile(p))))
        line.addWidget(_chip(status.upper(), self._STATUS_TINT.get(status, _GREY)))


class BannedSenderRow(QFrame):
    """One line per banned Slack sender: who, where, when, and the words that did it,
    cut to fit (whole in the tooltip)."""

    def __init__(self, ban: bans.BannedAuthor) -> None:
        super().__init__()
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        line.addWidget(GlyphLabel(glyphs.G_BAN, 14, _RED, font_px=12))
        who = _plain(ban.name)
        who.setStyleSheet("font-weight: 700; font-size: 10px;")
        who.setToolTip(plain_tooltip(str(ban.slack_id)))
        line.addWidget(who)
        if ban.place:
            place = _plain(ban.place)
            place.setStyleSheet(muted(9))
            line.addWidget(place)
        when = _plain(bans.when_text(ban))
        when.setStyleSheet(muted(9))
        line.addWidget(when)
        why = ban.evidence or ban.reason or ""
        self.quote = ElidedLabel(f"“{_flat(why)}”" if ban.evidence else _flat(why), 9,
                                 _text_color())
        if why:
            self.quote.setToolTip(plain_tooltip(why))
        line.addWidget(self.quote, 1)
        url = bans.place_url(ban)
        if url:
            line.addWidget(_small_button(
                "Open in Slack", lambda u=url: QDesktopServices.openUrl(QUrl(u))))


class CommsView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("CommsView { background: transparent; }")
        #: `commsSeenAt` as of opening, which decides the rows drawn as new: opening
        #: moves the setting itself.
        self._seen_before = store.comms_seen_at
        #: Survive the rebuild every store signal causes; each opening starts closed.
        self._expanded: set[str] = set()
        self._earlier_open = False
        self.earlier_btn: QPushButton | None = None

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
        """Until a tick says who the login is. It holds the error, so the banner does
        not say it twice."""
        host, col = _card()
        col.setContentsMargins(14, 14, 14, 14)
        col.setSpacing(8)
        head = QLabel("Comms reads Slack as you, through Claude Code's Slack login")
        head.setWordWrap(True)
        head.setStyleSheet("font-weight: 700; font-size: 12px;")
        col.addWidget(head)
        self.empty_error = _plain("")
        self.empty_error.setWordWrap(True)
        self.empty_error.setStyleSheet(f"color: {_RED}; font-size: 10px;")
        col.addWidget(self.empty_error)
        return host

    def open(self) -> None:
        self._seen_before = self.store.comms_seen_at
        self._expanded.clear()
        self._earlier_open = False
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
        known = self.store.comms_identity is not None
        items = self.store.comms_items
        empty = not known and not items
        self.empty.setVisible(empty)
        self.status.setText(self._status_text())
        error = self.store.comms_error
        self.empty_error.setVisible(bool(error))
        self.empty_error.setText(error or "")
        self.error_banner.setVisible(bool(error) and not empty)
        self.error_banner.setText(f"⚠ {error}" if error else "")
        self.body.setVisible(not empty)
        seen = comms.parse_iso(self._seen_before)

        senders = sorted((b for b in self.store.banned_authors if b.is_slack),
                         key=lambda b: b.at or "", reverse=True)
        _clear(self.bans_col)
        self.bans_host.setVisible(bool(senders))
        if senders:
            head = self._section_head("BANNED SENDERS", len(senders))
            note = QLabel("prompt injection: never triaged · un-ban in the panel's "
                          "Banned list")
            note.setStyleSheet(muted(9))
            head.addWidget(note)
            self.bans_col.addLayout(head)
            for b in senders:
                self.bans_col.addWidget(BannedSenderRow(b))

        _clear(self.pings_col)
        self.pings_col.addLayout(self._section_head("PINGS", len(items)))
        if not items:
            none = QLabel("No pings yet. A DM, an @-mention or a keyword lands here.")
            none.setStyleSheet(muted(10))
            none.setWordWrap(True)
            self.pings_col.addWidget(none)
        def is_unread(item: dict) -> bool:
            at = comms.parse_iso(str(item.get("at", "")))
            return seen is None or (at is not None and at > seen)

        unread = [i for i in items if is_unread(i)]
        read = [i for i in items if not is_unread(i)]
        for item in unread:
            self.pings_col.addWidget(self._ping_row(item, unread=True))
        self.earlier_btn = None
        if read:
            if not unread:
                none = QLabel("Nothing new.")
                none.setStyleSheet(muted(10))
                self.pings_col.addWidget(none)
            self.earlier_btn = QPushButton(
                f"{'▾' if self._earlier_open else '▸'} Earlier · {len(read)}")
            self.earlier_btn.setFlat(True)
            self.earlier_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.earlier_btn.setStyleSheet(muted(10, bold=True) + " text-align: left;")
            self.earlier_btn.clicked.connect(self._toggle_earlier)
            self.pings_col.addWidget(self.earlier_btn)
            if self._earlier_open:
                for item in read:
                    self.pings_col.addWidget(self._ping_row(item, unread=False))

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

    def _ping_row(self, item: dict, *, unread: bool) -> PingRow:
        msg_id = str((item.get("message") or {}).get("id", ""))
        row = PingRow(item, unread=unread, mark=ban_mark(item),
                      expanded=msg_id in self._expanded)
        row.toggled.connect(self._note_expanded)
        return row

    def _note_expanded(self, msg_id: str, on: bool) -> None:
        (self._expanded.add if on else self._expanded.discard)(msg_id)

    def _toggle_earlier(self) -> None:
        self._earlier_open = not self._earlier_open
        self.rebuild()

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
