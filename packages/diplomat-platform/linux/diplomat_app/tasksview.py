"""The Tasks screen - the long-running task tracker.

Draws ``Store.tracked_tasks`` (``diplomat-core tasks-list``): per task its status, the
reasons for it, its tags, its links and, opened, its history; and makes every edit
through ``diplomat-core tasks-edit``. The status, its reasons and the history are the
core's, read here as they come.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from diplomat_runtime import comms, core
from diplomat_runtime.models import Fmt
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import glyphs
from .store import GITHUB_ITEM, GoalOverlay, Store, watched_number
from .widgets import ElidedLabel, GlyphLabel, muted, plain_tooltip, tint_bg

_RED, _GREEN, _ORANGE, _BLUE, _PURPLE = "#FF3B30", "#34C759", "#FF9500", "#0A84FF", "#AF52DE"
TINT = _GREEN

#: `TaskStatus`'s raw values.
STATUS_TINT = {"open": glyphs.MUTED, "in-progress": _BLUE, "in-review": _PURPLE,
               "blocked": _RED, "done": _GREEN, "paused": glyphs.MUTED}

#: `TaskView.linkStates`' values, by the dot they draw.
STATE_TINT = {"open": _GREEN, "draft": glyphs.MUTED, "merged": _PURPLE, "closed": _RED,
              "missing": _ORANGE}

#: The row header's pill for a goal Diplomat proposed, and for one it acts on.
MODE_PILL = {"proposed": (f"{glyphs.G_FINAL} PROPOSED", _PURPLE),
             "auto": (f"{glyphs.G_AUTO} AUTO", _ORANGE)}

SOURCE_GLYPH = {"github": glyphs.G_ISSUES, "slack": glyphs.G_COMMS, "note": glyphs.G_NOTE,
                "diplomat": glyphs.G_APP}

#: An opened history shows this many of the newest events until asked for the rest.
HISTORY_SHOWN = 12



def _text_color() -> str:
    return QApplication.palette().color(QPalette.ColorRole.WindowText).name()


def _plain(text: str) -> QLabel:
    """Never markup: titles, tags and excerpts come from GitHub, Slack and agents."""
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


def _small_button(text: str, on_click) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet("font-size: 9px; padding: 1px 6px;")
    btn.clicked.connect(lambda _=False: on_click())
    return btn


def _choice(text: str, on: bool, tint: str, on_click) -> QPushButton:
    """A small button that is one side of a choice: filled with ``tint`` while it is
    the one in effect."""
    btn = _small_button(text, on_click)
    if on:
        btn.setStyleSheet(f"font-size: 9px; padding: 1px 6px; font-weight: 700; color: {tint};"
                          f" background-color: {tint_bg(tint, 0.18)}; border-radius: 4px;")
    return btn


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def split_tags(raw: str) -> list[str]:
    """Tags hold no whitespace (the core turns it into ``-``), so a space or a comma
    separates two."""
    return [t for t in re.split(r"[,\s]+", raw) if t]


def _github_key(url: str) -> str | None:
    m = GITHUB_ITEM.match(url or "")
    return f"{m[1]}/{m[2]}#{m[3]}".lower() if m else None


def link_label(url: str, kind: str) -> str:
    """``#123`` in the watched repo, ``o/r#12`` elsewhere (``GitHubRef.label``),
    ``slack``, else the host."""
    m = GITHUB_ITEM.match(url or "")
    if m and kind in ("pr", "issue"):
        cfg = core.config()
        watched = f"{cfg.get('owner', '')}/{cfg.get('repo', '')}#{m[3]}".lower()
        return f"#{m[3]}" if _github_key(url) == watched else f"{m[1]}/{m[2]}#{m[3]}"
    if kind == "slack":
        return "slack"
    return urlparse(url or "").hostname or url


def _when(iso: str | None) -> str:
    when = comms.parse_iso(iso or "")
    return when.astimezone().strftime("%b %d %H:%M") if when else "—"


class LinkChip(QFrame):
    """A linked item: its state dot and label; a click opens it, and with ``on_remove``
    a right-click offers to unlink it too."""

    def __init__(self, url: str, label: str, *, state: str | None = None,
                 title: str | None = None, on_remove=None) -> None:
        super().__init__()
        self.url = url
        self.state = state
        self._on_remove = on_remove
        self.setObjectName("linkChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"#linkChip {{ background-color: {tint_bg(_BLUE, 0.12)};"
                           " border-radius: 5px; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(5, 1, 5, 1)
        row.setSpacing(3)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(
            f"color: {STATE_TINT.get(state or '', glyphs.CHIP_OFF)}; font-size: 8px;")
        self.dot.setVisible(state is not None)
        row.addWidget(self.dot)
        self.text = _plain(label)
        self.text.setStyleSheet(f"color: {_BLUE}; font-size: 9px; font-weight: 600;")
        row.addWidget(self.text)
        self.setToolTip(plain_tooltip("\n".join(
            t for t in (title, f"{state}" if state else None, url) if t)))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            open_url(self.url)
        super().mouseReleaseEvent(event)

    def context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.addAction("Open", lambda: open_url(self.url))
        if self._on_remove is not None:
            menu.addAction("Remove link", lambda: self._on_remove(self.url))
        return menu

    def contextMenuEvent(self, event) -> None:
        self.context_menu().exec(event.globalPos())


class GraphPill(LinkChip):
    """One item of a goal's graph (``TaskGraph.Node``). A PR or an issue opens; the
    ``+`` after an issue no PR closes yet starts a fix agent for it through ``on_fix``
    (None: archived, or not the watched repo's), unless ``badge`` (``agent`` or
    ``queued``) says one is on it already, which it then shows instead."""

    def __init__(self, node: dict, *, badge: str | None, on_fix=None) -> None:
        self.node = node
        self.kind = str(node.get("kind") or "")
        self.badge = badge
        self._on_fix = on_fix
        add = self.kind == "add"
        super().__init__(str(node.get("url") or ""),
                         badge.upper() if add and badge else str(node.get("label") or ""),
                         state=None if add else node.get("state"))
        if add:
            self.text.setStyleSheet(
                f"color: {glyphs.AGENT_LIVE if badge == 'agent' else _BLUE};"
                " font-size: 9px; font-weight: 700;")
            asked = comms.parse_iso(str(node.get("dispatchedAt") or ""))
            tip = f"fix asked {Fmt.age(asked)} ago" if asked else "no fix asked yet"
            if badge:
                tip = f"a fix is {'running' if badge == 'agent' else 'queued'}; {tip}"
            elif on_fix is not None:
                tip = f"Start a fix agent for this issue; {tip}"
            self.setToolTip(plain_tooltip(tip))
        elif badge:
            self.badge_chip = _chip(badge.upper(),
                                    glyphs.AGENT_LIVE if badge == "agent" else glyphs.MUTED)
            self.layout().addWidget(self.badge_chip)

    def mouseReleaseEvent(self, event) -> None:
        if self.kind != "add":
            super().mouseReleaseEvent(event)
        elif (event.button() == Qt.MouseButton.LeftButton and self.badge is None
              and self._on_fix is not None):
            self._on_fix(self.node)


def graph_badge(node: dict, overlay: GoalOverlay) -> str | None:
    """``agent`` or ``queued`` for a node of the watched repo something is on: for a
    ``+``, a fix of its issue."""
    number = watched_number(str(node.get("url") or ""))
    if number is None:
        return None
    kind = node.get("kind")
    on_issue = kind in ("issue", "add")
    if number in (overlay.agent_issues if on_issue else overlay.agent_prs):
        return "agent"
    if number in (overlay.queued_issues if on_issue else overlay.queued_prs):
        return "queued"
    return None


class HistoryRow(QFrame):
    """One history event: time, source glyph, tags, its link, and the excerpt."""

    def __init__(self, event: dict, view: dict) -> None:
        super().__init__()
        self.item = event
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(5)
        self.when = QLabel(_when(event.get("at")))
        self.when.setStyleSheet(muted(9, mono=True))
        line.addWidget(self.when)
        source = str(event.get("source") or "")
        glyph = GlyphLabel(SOURCE_GLYPH.get(source, "·"), 12, glyphs.MUTED, font_px=10)
        glyph.setToolTip(source)
        line.addWidget(glyph)
        self.tags = [str(t) for t in event.get("tags") or []]
        for tag in self.tags:
            line.addWidget(_chip(tag, _tag_tint(tag)))
        self.link: LinkChip | None = None
        subject = str(event.get("subject") or "")
        # The most specific link; the item itself for an event that has none
        # (`link:missing`).
        url = str(event.get("url") or subject)
        if url:
            kinds = {lk.get("url"): lk.get("kind") for lk in view["task"].get("links") or []}
            label = (link_label(subject, kinds.get(subject, "pr")) if subject
                     else link_label(url, "slack" if source == "slack" else "url"))
            self.link = LinkChip(url, label)
            line.addWidget(self.link)
        self.text = ElidedLabel(" ".join(str(event.get("text") or "").split()), 9,
                                _text_color())
        if event.get("text"):
            self.text.setToolTip(plain_tooltip(str(event["text"])))
        line.addWidget(self.text, 1)


def _tag_tint(tag: str) -> str:
    """Red for what blocks, green for what moves it on; the rest neutral."""
    if tag in ("ci:failing", "review:changes-requested", "conflicts", "link:missing") \
            or tag.startswith("blocked"):
        return _RED
    if tag in ("ci:passing", "review:approved", "pr:merged", "pr:ready"):
        return _GREEN
    return glyphs.MUTED


class TaskCard(QFrame):
    """One task: status, title, reasons and tags; its links; its actions; and, opened,
    its goal (what Diplomat does about it, and its graph) and its history. Every action
    is a ``tasks-edit`` through the store, whose answer :class:`TasksView` routes back
    by ``("card", id, from_editor)``. ``overlay`` badges the graph."""

    def __init__(self, view: dict, store: Store, *, expanded: bool, full_history: bool,
                 error: str | None, on_toggle, on_older, on_editing,
                 overlay: GoalOverlay) -> None:
        super().__init__()
        self.view = view
        self.store = store
        self.task = view["task"]
        self.id = str(self.task.get("id", ""))
        self._on_toggle = on_toggle
        self._on_editing = on_editing
        self.mode: str | None = None
        #: A Save sent and not yet answered.
        self.saving = False
        self.setObjectName("taskCard")
        status = str(view.get("status") or "open")
        archived = bool(self.task.get("archivedAt"))
        self.setStyleSheet(
            "#taskCard { background-color: rgba(128,128,128,0.07); border-radius: 8px;"
            f" border-left: 3px solid {STATUS_TINT.get(status, glyphs.MUTED)}; }}")
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.status_chip = _chip(status.upper(), STATUS_TINT.get(status, glyphs.MUTED))
        head.addWidget(self.status_chip)
        title = _plain(str(self.task.get("title", "")))
        title.setStyleSheet("font-weight: 700; font-size: 11px;")
        head.addWidget(title)
        #: `TaskMode`'s raw value; observe when unset.
        self.goal_mode = str(self.task.get("mode") or "observe")
        self.mode_chip: QLabel | None = None
        if self.goal_mode in MODE_PILL:
            self.mode_chip = _chip(*MODE_PILL[self.goal_mode])
            head.addWidget(self.mode_chip)
        if archived:
            head.addWidget(_chip("ARCHIVED", glyphs.MUTED))
        self.reasons = [str(r) for r in view.get("reasons") or []]
        for reason in self.reasons:
            head.addWidget(_chip(reason, _RED if status == "blocked" and ":" in reason
                                 else glyphs.MUTED))
        head.addStretch(1)
        self.tags = [str(t) for t in self.task.get("tags") or []]
        for tag in self.tags:
            head.addWidget(_chip(tag, TINT))
        col.addLayout(head)

        links = QHBoxLayout()
        links.setSpacing(4)
        self.link_chips: list[LinkChip] = []
        states = view.get("linkStates") or {}
        for lk in self.task.get("links") or []:
            url, kind = str(lk.get("url", "")), str(lk.get("kind", "url"))
            chip = LinkChip(url, link_label(url, kind), state=states.get(url),
                            title=lk.get("title"),
                            on_remove=None if archived else lambda u: self.apply(
                                {"op": "unlink", "id": self.id, "url": u}))
            self.link_chips.append(chip)
            links.addWidget(chip)
        links.addStretch(1)
        history = view.get("history") or []
        self.history_btn = QPushButton(
            f"{'▾' if expanded else '▸'} History · {len(history)}")
        self.history_btn.setFlat(True)
        self.history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_btn.setStyleSheet(muted(9, bold=True))
        self.history_btn.clicked.connect(lambda _=False: self._on_toggle(self.id))
        links.addWidget(self.history_btn)
        col.addLayout(links)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.buttons: dict[str, QPushButton] = {}

        def button(key: str, text: str, on_click) -> None:
            self.buttons[key] = _small_button(text, on_click)
            actions.addWidget(self.buttons[key])

        if not archived:
            button("link", "+ Link", lambda: self.start("link"))
            button("note", "+ Note", lambda: self.start("note"))
            button("tags", "Tags…", lambda: self.start("tags"))
            if self.task.get("pinnedStatus"):
                button("unpin", "Unpin", lambda: self.apply({"op": "update", "id": self.id,
                                                             "pinnedStatus": ""}))
            else:
                for pin in ("done", "paused"):
                    button(f"pin-{pin}", f"Pin {pin}",
                           lambda p=pin: self.apply({"op": "update", "id": self.id,
                                                     "pinnedStatus": p}))
        actions.addStretch(1)
        if archived:
            button("unarchive", "Unarchive",
                   lambda: self.apply({"op": "unarchive", "id": self.id}))
        else:
            button("archive", "Archive", lambda: self.apply({"op": "archive", "id": self.id}))
        col.addLayout(actions)

        self.editor = QWidget()
        self.editor_row = QHBoxLayout(self.editor)
        self.editor_row.setContentsMargins(0, 0, 0, 0)
        self.editor_row.setSpacing(4)
        self.editor.setVisible(False)
        col.addWidget(self.editor)

        self.error = _plain(error or "")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {_RED}; font-size: 9px;")
        self.error.setVisible(bool(error))
        col.addWidget(self.error)

        self.history_rows: list[HistoryRow] = []
        self.older_btn: QPushButton | None = None
        self.goal_buttons: dict[str, QPushButton] = {}
        self.graph_rows: list[list[GraphPill]] = []
        self.goal_line: QLabel | None = None
        if expanded:
            self._build_goal(col, archived, overlay)
            hist = QVBoxLayout()
            hist.setContentsMargins(6, 2, 0, 0)
            hist.setSpacing(2)
            for event in history if full_history else history[:HISTORY_SHOWN]:
                row = HistoryRow(event, view)
                self.history_rows.append(row)
                hist.addWidget(row)
            if older := len(history) - len(self.history_rows):
                self.older_btn = QPushButton(f"{older} older…")
                self.older_btn.setFlat(True)
                self.older_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                self.older_btn.setStyleSheet(muted(9, bold=True))
                self.older_btn.clicked.connect(lambda _=False: on_older(self.id))
                hist.addWidget(self.older_btn, 0, Qt.AlignmentFlag.AlignLeft)
            if not history:
                none = QLabel("No history yet.")
                none.setStyleSheet(muted(9))
                hist.addWidget(none)
            col.addLayout(hist)

    # MARK: the goal

    def _build_goal(self, col: QVBoxLayout, archived: bool, overlay: GoalOverlay) -> None:
        """What Diplomat does about it (a proposal's three answers, else Observe | Auto
        and, in auto, review on new commits), then its graph: one line per row of
        ``view.graph``. An archived task shows the graph alone."""
        github = [lk for lk in self.task.get("links") or [] if lk.get("kind") in ("pr", "issue")]
        graph = [row for row in self.view.get("graph") or [] if isinstance(row, list)]
        if not github and not graph:
            return
        grouped = self.task.get("origin") == "grouper"

        def mode(key: str, text: str, op: dict, on: bool = False, tint: str = _BLUE) -> None:
            self.goal_buttons[key] = _choice(text, on, tint, lambda: self.apply(
                {"op": "mode", "id": self.id, **op}))
            line.addWidget(self.goal_buttons[key])

        if not archived:
            line = QHBoxLayout()
            line.setSpacing(4)
            if self.goal_mode == "proposed":
                self.goal_line = _plain(f"Proposed from {len(github)} open issues and PRs")
                self.goal_line.setStyleSheet(muted(9))
                line.addWidget(self.goal_line)
                mode("dismiss", "Dismiss", {"mode": "dismissed"})
                mode("observe", "Observe", {"mode": "observe"})
                mode("auto", "Automate", {"mode": "auto"})
            else:
                auto = self.goal_mode == "auto"
                mode("observe", "Observe", {"mode": "observe"}, on=not auto)
                mode("auto", "Auto", {"mode": "auto"}, on=auto, tint=_ORANGE)
                if auto:
                    on = bool(self.task.get("reviewOnPush"))
                    mode("review", "Review on new commits", {"reviewOnPush": not on}, on=on,
                         tint=_ORANGE)
                if grouped:
                    mode("dismiss", "Dismiss", {"mode": "dismissed"})
            line.addStretch(1)
            col.addLayout(line)
        if not graph:
            return
        host = QWidget()
        rows = QVBoxLayout(host)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)
        for nodes in graph:
            line = QHBoxLayout()
            line.setSpacing(3)
            pills = []
            for i, node in enumerate(n for n in nodes if isinstance(n, dict)):
                if i:
                    arrow = QLabel("→")
                    arrow.setStyleSheet(muted(10))
                    line.addWidget(arrow)
                fix = None
                if (node.get("kind") == "add" and not archived
                        and watched_number(str(node.get("url") or "")) is not None):
                    fix = self.start_fix
                pill = GraphPill(node, badge=graph_badge(node, overlay), on_fix=fix)
                pills.append(pill)
                line.addWidget(pill)
            line.addStretch(1)
            rows.addLayout(line)
            self.graph_rows.append(pills)
        # Wider rows scroll sideways rather than widen the card or wrap.
        self.graph = QScrollArea()
        self.graph.setWidget(host)
        self.graph.setWidgetResizable(True)
        self.graph.setFrameShape(QFrame.Shape.NoFrame)
        self.graph.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.graph.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget"
                                 " { background: transparent; }")
        self.graph.setFixedHeight(host.sizeHint().height()
                                  + self.graph.horizontalScrollBar().sizeHint().height())
        col.addWidget(self.graph)

    def start_fix(self, node: dict) -> None:
        self.error.setVisible(False)
        self.store.start_goal_fix_async(self.id, node)

    # MARK: inline editors

    def start(self, mode: str, *, focus: bool = True) -> None:
        """Open the one inline editor: a link, a note (text and tags), or the tags."""
        _clear(self.editor_row)
        self.mode = mode
        self.fields: dict[str, QLineEdit] = {}

        def field(key: str, placeholder: str, text: str = "", stretch: int = 1) -> None:
            edit = QLineEdit(text)
            edit.setPlaceholderText(placeholder)
            edit.setStyleSheet("font-size: 10px;")
            edit.returnPressed.connect(self.save)
            self.fields[key] = edit
            self.editor_row.addWidget(edit, stretch)

        if mode == "link":
            field("url", "a PR, an issue (o/r#12, #12), a Slack link or any URL")
        elif mode == "note":
            field("text", "what happened, or why", stretch=3)
            field("tags", "tags")
        else:
            field("tags", "tags, space or comma separated", " ".join(self.tags))
        self.save_btn = _small_button("Save", self.save)
        self.editor_row.addWidget(self.save_btn)
        self.editor_row.addWidget(_small_button("Cancel", self.cancel))
        self.editor.setVisible(True)
        if focus:
            next(iter(self.fields.values())).setFocus()
        self._on_editing(True)

    def draft(self) -> dict | None:
        """The open editor as typed, for the card a rebuild puts in this one's place."""
        if self.mode is None:
            return None
        focused = next((k for k, f in self.fields.items() if f.hasFocus()), None)
        return {"mode": self.mode, "texts": {k: f.text() for k, f in self.fields.items()},
                "focus": focused,
                "cursor": self.fields[focused].cursorPosition() if focused else 0,
                "saving": self.saving}

    def restore(self, draft: dict) -> None:
        self.start(draft["mode"], focus=False)
        for key, text in draft["texts"].items():
            self.fields[key].setText(text)
        if draft["focus"]:
            self.fields[draft["focus"]].setFocus()
            self.fields[draft["focus"]].setCursorPosition(draft["cursor"])
        self.saving = draft["saving"]
        self.save_btn.setEnabled(not self.saving)

    def close_editor(self) -> None:
        self.mode = None
        self.saving = False
        self.editor.setVisible(False)

    def cancel(self) -> None:
        self.close_editor()
        self._on_editing(False)

    def save(self) -> None:
        if self.saving:
            return
        f = {k: v.text() for k, v in self.fields.items()}
        if self.mode == "link":
            op = {"op": "link", "id": self.id, "url": f["url"].strip()}
        elif self.mode == "note":
            op = {"op": "note", "id": self.id, "text": f["text"].strip(),
                  "tags": split_tags(f["tags"])}
        else:
            op = {"op": "update", "id": self.id, "tags": split_tags(f["tags"])}
        self.saving = True
        self.save_btn.setEnabled(False)
        self.apply(op, from_editor=True)

    def apply(self, op: dict, *, from_editor: bool = False) -> None:
        self.error.setVisible(False)
        self.store.edit_tracked_task_async(op, ("card", self.id, from_editor))

    def refused(self, error: str, *, from_editor: bool) -> None:
        """Why the edit did not apply, under the editor, which stays open."""
        self.error.setText(error)
        self.error.setVisible(True)
        if from_editor and self.mode is not None:
            self.saving = False
            self.save_btn.setEnabled(True)


class TasksView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("TasksView { background: transparent; }")
        #: Ids of the tasks whose history is open; survive the rebuild every change causes.
        self._expanded: set[str] = set()
        #: Ids of the tasks whose whole history is open, not only its newest
        #: `HISTORY_SHOWN`.
        self._full_history: set[str] = set()
        #: Cards with an inline editor open. A change from elsewhere waits for the last
        #: one to close rather than move the focus under the typing; the screen's own
        #: actions rebuild at once, each open editor carried over as typed.
        self._editing: set[str] = set()
        self._stale = False
        #: Why each task's last edit did not apply, until one does.
        self._errors: dict[str, str] = {}
        self.cards: list[TaskCard] = []

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
            " padding: 6px; font-size: 10px;")
        col.addWidget(self.error_banner)

        col.addWidget(self._build_new_task())

        self.list_host = QWidget()
        self.list_col = QVBoxLayout(self.list_host)
        self.list_col.setContentsMargins(0, 0, 0, 0)
        self.list_col.setSpacing(6)
        col.addWidget(self.list_host)
        col.addStretch(1)

        store.tracked_tasks_changed.connect(self.rebuild)
        store.tracked_task_edited.connect(self._edited)
        store.tasks_changed.connect(self._agents_changed)
        self.rebuild()

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_TRACKER, 16, TINT, font_px=14))
        title = QLabel("Tasks")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        row.addStretch(1)
        self.archived_box = QCheckBox("Show archived")
        self.archived_box.setStyleSheet("font-size: 10px;")
        self.archived_box.toggled.connect(self.store.set_tracker_show_archived)
        row.addWidget(self.archived_box)
        self.new_btn = QPushButton("New task")
        self.new_btn.clicked.connect(self._toggle_new)
        row.addWidget(self.new_btn)
        self.sync_btn = QPushButton("Sync now")
        self.sync_btn.setToolTip("Mirror the linked GitHub items and fold Comms now, rather"
                                 " than at the next tick")
        self.sync_btn.clicked.connect(lambda _=False: self.store.run_tracker_sync_async(
            force=True))
        row.addWidget(self.sync_btn)
        self.goals_btn = QPushButton("Find goals")
        self.goals_btn.setToolTip("Ask Claude now which open issues and PRs serve one goal,"
                                  " and propose each as a task")
        self.goals_btn.clicked.connect(lambda _=False: self.store.run_goal_grouper_async(
            force=True))
        row.addWidget(self.goals_btn)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _build_new_task(self) -> QWidget:
        host = QFrame()
        host.setObjectName("newTask")
        host.setStyleSheet("#newTask { background-color: rgba(128,128,128,0.07);"
                           " border-radius: 8px; }")
        form = QVBoxLayout(host)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(4)
        self.new_title = QLineEdit()
        self.new_title.setPlaceholderText("Title: what outlives one PR")
        form.addWidget(self.new_title)
        self.new_tags = QLineEdit()
        self.new_tags.setPlaceholderText("Tags, e.g. area:devices kind:feature")
        form.addWidget(self.new_tags)
        self.new_links = QPlainTextEdit()
        self.new_links.setPlaceholderText(
            "Links, one per line: a PR or issue URL, o/r#12, #12, a Slack link, any URL")
        self.new_links.setFixedHeight(64)
        form.addWidget(self.new_links)
        row = QHBoxLayout()
        self.new_error = _plain("")
        self.new_error.setWordWrap(True)
        self.new_error.setStyleSheet(f"color: {_RED}; font-size: 9px;")
        row.addWidget(self.new_error, 1)
        self.create_btn = QPushButton("Create")
        self.create_btn.clicked.connect(self.create)
        row.addWidget(self.create_btn)
        form.addLayout(row)
        host.setVisible(False)
        self.new_host = host
        return host

    def _toggle_new(self) -> None:
        self.new_host.setVisible(self.new_host.isHidden())
        if not self.new_host.isHidden():
            self.new_title.setFocus()

    def create(self) -> None:
        links = [ln.strip() for ln in self.new_links.toPlainText().splitlines() if ln.strip()]
        self.create_btn.setEnabled(False)
        self.store.edit_tracked_task_async({
            "op": "create", "title": self.new_title.text(),
            "tags": split_tags(self.new_tags.text()), "links": links}, ("new",))

    def open(self) -> None:
        """Re-read: an agent may have edited them through the CLI."""
        self.store.refresh_tracked_tasks_async()

    def _edited(self, origin: object, error: str | None) -> None:
        """A ``tasks-edit``'s answer, routed to what asked: the form, or a card by its
        id, since a rebuild may have replaced the card that asked."""
        if origin == ("new",):
            self.create_btn.setEnabled(True)
            self.new_error.setText(error or "")
            if error is None:
                self.new_title.clear()
                self.new_tags.clear()
                self.new_links.clear()
                self.new_host.setVisible(False)
            return
        if not (isinstance(origin, tuple) and origin[0] == "card"):
            return
        _, task_id, from_editor = origin
        card = self.card(task_id)
        if error:
            self._errors[task_id] = error
            if card is not None:
                card.refused(error, from_editor=from_editor)
            return
        self._errors.pop(task_id, None)
        if card is not None:
            # Drawn by the rebuild the edit's own change caused, before this answer.
            card.error.setVisible(False)
            if from_editor:
                card.close_editor()
                self._editing.discard(task_id)
        if self._stale:
            self.rebuild(force=True)

    # MARK: rebuild

    def _status_text(self) -> str:
        st = self.store
        live = [v for v in st.tracked_tasks if not v["task"].get("archivedAt")]
        parts = [f"{len(live)} task{'' if len(live) == 1 else 's'}"]
        if st.tracker_blocked:
            parts.append(f"{st.tracker_blocked} blocked")
        if st.tracker_syncing:
            parts.append("syncing…")
        elif st.tracker_last_sync:
            parts.append(f"last sync {Fmt.clock(st.tracker_last_sync)}, "
                         f"{st.tracker_sync_events} new event"
                         f"{'' if st.tracker_sync_events == 1 else 's'}")
        else:
            parts.append("not synced yet")
        if st.tracker_grouping:
            parts.append("looking for goals…")
        elif st.tracker_grouped_at:
            parts.append(f"goals looked for {Fmt.clock(st.tracker_grouped_at)}")
        return " · ".join(parts)

    def rebuild(self, *, force: bool = False) -> None:
        st = self.store
        self.status.setText(self._status_text())
        errors = [e for e in (st.tracker_sync_error, st.tracker_list_error) if e]
        if st.tracker_grouper_error:
            errors.append(f"Finding goals failed: {st.tracker_grouper_error}")
        self.error_banner.setVisible(bool(errors))
        self.error_banner.setText("\n".join(f"⚠ {e}" for e in errors))
        self.sync_btn.setEnabled(not st.tracker_syncing and any(
            not v["task"].get("archivedAt") for v in st.tracked_tasks))
        self.goals_btn.setEnabled(not st.tracker_grouping)
        self.archived_box.blockSignals(True)
        self.archived_box.setChecked(st.tracker_show_archived)
        self.archived_box.blockSignals(False)
        if self._editing and not force:
            self._stale = True
            return
        self._stale = False
        drafts = {c.id: d for c in self.cards if (d := c.draft()) is not None}
        # Refilled by each restored draft: a card that is gone takes its editor along.
        self._editing = set()
        _clear(self.list_col)
        self.cards = []
        if not st.tracked_tasks:
            none = QLabel("No tasks yet. A task is what outlives one PR: give it a title,"
                          " tags, and the PRs, issues and threads it spans.")
            none.setStyleSheet(muted(10))
            none.setWordWrap(True)
            self.list_col.addWidget(none)
        overlay = st.goal_overlay() if self._graphs_shown() else GoalOverlay()
        for view in st.tracked_tasks:
            task_id = str(view["task"].get("id"))
            card = TaskCard(view, st, expanded=task_id in self._expanded,
                            full_history=task_id in self._full_history,
                            error=self._errors.get(task_id),
                            on_toggle=self._toggle_history, on_older=self._show_older,
                            on_editing=lambda on, i=task_id: self._note_editing(i, on),
                            overlay=overlay)
            self.cards.append(card)
            self.list_col.addWidget(card)
            if task_id in drafts:
                card.restore(drafts[task_id])

    def _graphs_shown(self) -> bool:
        return any(v.get("graph") and str(v["task"].get("id")) in self._expanded
                   for v in self.store.tracked_tasks)

    def _agents_changed(self) -> None:
        """An agent started, ended or was queued: the graphs' badges."""
        if self._graphs_shown():
            self.rebuild()

    def _toggle_history(self, task_id: str) -> None:
        if task_id in self._expanded:
            self._expanded.discard(task_id)
            self._full_history.discard(task_id)
        else:
            self._expanded.add(task_id)
        self.rebuild(force=True)

    def _show_older(self, task_id: str) -> None:
        self._full_history.add(task_id)
        self.rebuild(force=True)

    def _note_editing(self, task_id: str, on: bool) -> None:
        (self._editing.add if on else self._editing.discard)(task_id)
        if not self._editing and self._stale:
            self.rebuild()

    def card(self, task_id: str) -> TaskCard | None:
        return next((c for c in self.cards if c.id == task_id), None)

