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
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import glyphs
from .store import Store
from .widgets import ElidedLabel, GlyphLabel, muted, plain_tooltip, tint_bg

_RED, _GREEN, _ORANGE, _BLUE, _PURPLE = "#FF3B30", "#34C759", "#FF9500", "#0A84FF", "#AF52DE"
TINT = _GREEN

#: `TaskStatus`'s raw values.
STATUS_TINT = {"open": glyphs.MUTED, "in-progress": _BLUE, "in-review": _PURPLE,
               "blocked": _RED, "done": _GREEN, "paused": glyphs.MUTED}

#: The history tags that say what state a linked item is in, by the dot they draw.
_STATE_TAGS = {
    "pr:open": "open", "issue:open": "open", "pr:reopened": "open",
    "issue:reopened": "open", "pr:ready": "open", "pr:draft": "draft",
    "pr:merged": "merged", "pr:closed": "closed", "issue:closed": "closed",
    "link:missing": "missing",
}
STATE_TINT = {"open": _GREEN, "draft": glyphs.MUTED, "merged": _PURPLE, "closed": _RED,
              "missing": _ORANGE}

SOURCE_GLYPH = {"github": glyphs.G_ISSUES, "slack": glyphs.G_COMMS, "note": glyphs.G_NOTE,
                "diplomat": glyphs.G_APP}

_GITHUB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/(?:pull|issues)/([0-9]+)$")


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
    m = _GITHUB.match(url or "")
    return f"{m[1]}/{m[2]}#{m[3]}".lower() if m else None


def link_label(url: str, kind: str) -> str:
    """``#123`` in the watched repo, ``o/r#12`` elsewhere (``GitHubRef.label``),
    ``slack``, else the host."""
    m = _GITHUB.match(url or "")
    if m and kind in ("pr", "issue"):
        cfg = core.config()
        watched = f"{cfg.get('owner', '')}/{cfg.get('repo', '')}#{m[3]}".lower()
        return f"#{m[3]}" if _github_key(url) == watched else f"{m[1]}/{m[2]}#{m[3]}"
    if kind == "slack":
        return "slack"
    return urlparse(url or "").hostname or url


def link_state(view: dict, url: str) -> str | None:
    """The newest state the task's history gives the linked item at ``url``; None when
    the history holds none."""
    key = _github_key(url)
    for event in view.get("history") or []:
        subject = str(event.get("subject") or "")
        if subject != url and (key is None or _github_key(subject) != key):
            continue
        for tag in event.get("tags") or []:
            if tag in _STATE_TAGS:
                return _STATE_TAGS[tag]
    return None


def _when(iso: str | None) -> str:
    when = comms.parse_iso(iso or "")
    return when.astimezone().strftime("%b %d %H:%M") if when else "—"


class LinkChip(QFrame):
    """A linked item: its state dot and label; a click opens it."""

    def __init__(self, url: str, label: str, *, state: str | None = None,
                 title: str | None = None) -> None:
        super().__init__()
        self.url = url
        self.state = state
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
    its history. Every action is a ``tasks-edit`` through the store."""

    def __init__(self, view: dict, store: Store, *, expanded: bool, on_toggle,
                 on_editing) -> None:
        super().__init__()
        self.view = view
        self.store = store
        self.task = view["task"]
        self.id = str(self.task.get("id", ""))
        self._on_toggle = on_toggle
        self._on_editing = on_editing
        self.mode: str | None = None
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
        for lk in self.task.get("links") or []:
            url, kind = str(lk.get("url", "")), str(lk.get("kind", "url"))
            chip = LinkChip(url, link_label(url, kind),
                            state=link_state(view, url) if kind in ("pr", "issue") else None,
                            title=lk.get("title"))
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

        self.error = _plain("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {_RED}; font-size: 9px;")
        self.error.setVisible(False)
        col.addWidget(self.error)

        self.history_rows: list[HistoryRow] = []
        if expanded:
            hist = QVBoxLayout()
            hist.setContentsMargins(6, 2, 0, 0)
            hist.setSpacing(2)
            for event in history:
                row = HistoryRow(event, view)
                self.history_rows.append(row)
                hist.addWidget(row)
            if not history:
                none = QLabel("No history yet.")
                none.setStyleSheet(muted(9))
                hist.addWidget(none)
            col.addLayout(hist)

    # MARK: inline editors

    def start(self, mode: str) -> None:
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
        self.error.setVisible(False)
        next(iter(self.fields.values())).setFocus()
        self._on_editing(True)

    def cancel(self) -> None:
        self.mode = None
        self.editor.setVisible(False)
        self._on_editing(False)

    def save(self) -> None:
        f = {k: v.text() for k, v in self.fields.items()}
        if self.mode == "link":
            op = {"op": "link", "id": self.id, "url": f["url"].strip()}
        elif self.mode == "note":
            op = {"op": "note", "id": self.id, "text": f["text"].strip(),
                  "tags": split_tags(f["tags"])}
        else:
            op = {"op": "update", "id": self.id, "tags": split_tags(f["tags"])}
        self.apply(op)

    def apply(self, op: dict) -> bool:
        """The edit through the CLI; on failure its reason stays on the card."""
        error = self.store.edit_tracked_task(op)
        if error:
            self.error.setText(error)
            self.error.setVisible(True)
            return False
        self.cancel()
        return True


class TasksView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("TasksView { background: transparent; }")
        #: Ids of the tasks whose history is open; survive the rebuild every change causes.
        self._expanded: set[str] = set()
        #: Cards with an inline editor open: a rebuild would drop what is being typed,
        #: so it waits for the last one to close.
        self._editing: set[str] = set()
        self._stale = False
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
        error = self.store.edit_tracked_task({
            "op": "create", "title": self.new_title.text(),
            "tags": split_tags(self.new_tags.text()), "links": links})
        self.new_error.setText(error or "")
        if error is None:
            self.new_title.clear()
            self.new_tags.clear()
            self.new_links.clear()
            self.new_host.setVisible(False)

    def open(self) -> None:
        self.store.refresh_tracked_tasks()
        self.rebuild()

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
        return " · ".join(parts)

    def rebuild(self) -> None:
        st = self.store
        self.status.setText(self._status_text())
        errors = [e for e in (st.tracker_sync_error, st.tracker_list_error) if e]
        self.error_banner.setVisible(bool(errors))
        self.error_banner.setText("\n".join(f"⚠ {e}" for e in errors))
        self.sync_btn.setEnabled(not st.tracker_syncing)
        self.archived_box.blockSignals(True)
        self.archived_box.setChecked(st.tracker_show_archived)
        self.archived_box.blockSignals(False)
        if self._editing:
            self._stale = True
            return
        self._stale = False
        _clear(self.list_col)
        self.cards = []
        if not st.tracked_tasks:
            none = QLabel("No tasks yet. A task is what outlives one PR: give it a title,"
                          " tags, and the PRs, issues and threads it spans.")
            none.setStyleSheet(muted(10))
            none.setWordWrap(True)
            self.list_col.addWidget(none)
        for view in st.tracked_tasks:
            card = TaskCard(view, st, expanded=str(view["task"].get("id")) in self._expanded,
                            on_toggle=self._toggle_history,
                            on_editing=lambda on, i=str(view["task"].get("id")):
                            self._note_editing(i, on))
            self.cards.append(card)
            self.list_col.addWidget(card)

    def _toggle_history(self, task_id: str) -> None:
        (self._expanded.discard if task_id in self._expanded else self._expanded.add)(task_id)
        self.rebuild()

    def _note_editing(self, task_id: str, on: bool) -> None:
        (self._editing.add if on else self._editing.discard)(task_id)
        if not self._editing and self._stale:
            self.rebuild()

    def card(self, task_id: str) -> TaskCard | None:
        return next((c for c in self.cards if c.id == task_id), None)

