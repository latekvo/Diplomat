"""Small reusable Qt widgets for the panel (cards, chips, rows)."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetricsF, QIcon, QPainter, QPixmap
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

from . import glyphs


def tint_bg(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.3f})"


#: The panel's card fill - the soft neutral tint behind a grouped block.
CARD_FILL = "rgba(128,128,128,0.07)"
#: The ban list's card fill; the one block that is tinted rather than neutral.
CARD_FILL_ALERT = "rgba(255,59,48,0.06)"


def card_host(*, fill: str = CARD_FILL, padding: int = 7,
              spacing: int = 6) -> tuple[QWidget, QVBoxLayout]:
    """A grouped block: padded content on a soft rounded card fill.

    Every such block on both screens — the device pool, the activity feed, the ban
    list, the mesh topology/nodes/duties — is this same widget-with-a-column, so the
    fill and the 8px radius live here. Twin of ``cardChrome`` in Components.swift.

    Returns the host widget and its layout: add children to the layout, the host to
    the parent (callers set their own visibility and size policy).
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(padding, padding, padding, padding)
    layout.setSpacing(spacing)
    host.setStyleSheet(f"background-color: {fill}; border-radius: 8px;")
    return host, layout


def muted(size: int = 10, *, mono: bool = False, bold: bool = False) -> str:
    """The stylesheet for secondary text: dimmed to the theme's `palette(mid)`.

    Every screen carries captions, hints and status lines in this one style — the
    panel, the mesh screen, Settings, the wizards — and Qt stylesheets have no
    variables to share it with. Building it here gives "muted" one definition and
    puts the per-label size in an argument, where a screen that disagrees about it
    is visible rather than buried in an otherwise identical string.

    `mono` for anything column-aligned (ids, counts, timings); `bold` for a muted
    heading over a group.
    """
    family = " font-family: monospace;" if mono else ""
    weight = " font-weight: 700;" if bold else ""
    return f"color: palette(mid);{weight}{family} font-size: {size}px;"


# Reference size at which we measure a glyph's intrinsic ink extent before
# scaling it to the target. Large enough that tightBoundingRect is precise.
_MEASURE_PX = 128


def _draw_glyph(painter: QPainter, box: QRectF, glyph: str, color: str,
                target_px: int) -> None:
    """Paint a glyph normalised to a uniform optical size, ink-centred in ``box``.

    Two problems make a raw text glyph a poor icon:

    * **Position** — Qt centres on the font line-box (full ascent/descent), so
      glyphs from different Unicode blocks land at visibly different heights.
    * **Size** — at one fixed point size, a full-height block like ``▤`` dwarfs a
      small mark like ``↩``; the set reads as a jumble, not an icon row.

    So we normalise both. ``target_px`` is the desired *optical* size: we measure
    the glyph's intrinsic ink box at a fixed reference size, pick the pixel size
    that scales its larger dimension to ``target_px`` (fit-to-square, so nothing
    overflows), then ink-centre it. Every glyph then occupies the same footprint
    and lines up like a real, uniform icon set — the point of the tinted set.
    """
    font = QFont(painter.font())
    font.setPixelSize(_MEASURE_PX)
    intrinsic = QFontMetricsF(font).tightBoundingRect(glyph)
    extent = max(intrinsic.width(), intrinsic.height()) or float(_MEASURE_PX)
    px = max(1, round(_MEASURE_PX * (target_px / extent)))

    font.setPixelSize(px)
    painter.setFont(font)
    painter.setPen(QColor(color))
    ink = QFontMetricsF(font).tightBoundingRect(glyph)
    baseline_x = box.center().x() - (ink.x() + ink.width() / 2)
    baseline_y = box.center().y() - (ink.y() + ink.height() / 2)
    painter.drawText(QPointF(baseline_x, baseline_y), glyph)


def glyph_icon(glyph: str, px: int, color: str) -> QIcon:
    """A QIcon of a single glyph, size-normalised and ink-centred - for icon
    buttons/tray whose raw text glyphs would otherwise render at inconsistent
    sizes/positions."""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    _draw_glyph(painter, QRectF(0, 0, px, px), glyph, color, int(px * 0.78))
    painter.end()
    return QIcon(pm)


class GlyphLabel(QLabel):
    """A bare (no background) monochrome glyph, ink-centred at a fixed size."""

    def __init__(self, glyph: str, size: int, color: str,
                 font_px: int | None = None) -> None:
        super().__init__()
        self._glyph = glyph
        self._color = color
        self._font_px = font_px if font_px is not None else int(size * 0.85)
        self.setFixedSize(size, size)

    def set_glyph(self, glyph: str, color: str) -> None:
        self._glyph = glyph
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        _draw_glyph(painter, QRectF(self.rect()), self._glyph, self._color,
                    self._font_px)
        painter.end()


class ElidedLabel(QLabel):
    """A single-line label that elides its text with … to the available width.

    Custom-painted so the grid never miscomputes a wrapped height (which made
    rows overlap); font size + colour are explicit because QPainter.drawText
    ignores the stylesheet pen.
    """

    def __init__(self, text: str, font_px: int, color: str) -> None:
        super().__init__()
        self._full = text
        self._color = QColor(color)
        f = self.font()
        f.setPixelSize(font_px)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.fontMetrics().height())

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter

        painter = QPainter(self)
        painter.setPen(self._color)
        elided = self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, self.width()
        )
        painter.drawText(
            self.rect(),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            elided,
        )
        painter.end()


def _elided_label(text: str) -> ElidedLabel:
    return ElidedLabel(text, 9, "#9aa0a6")


class IconChip(QLabel):
    """A rounded, tinted square holding a monochrome glyph, macOS-SF-Symbol style.

    Fully custom-painted: the glyph is size-normalised and ink-centred (see
    :func:`_draw_glyph`) to a uniform optical size so every tool's icon lines up,
    and drawn white on the solid tint. ``active=False`` renders the muted "off"
    state (neutral fill, grey glyph) used by the device pool and reverse-lookup
    rows.
    """

    def __init__(self, glyph: str, hex_color: str, size: int = 26,
                 *, active: bool = True) -> None:
        super().__init__()
        self._glyph = glyph
        self._tint = hex_color
        self._active = active
        self._size = size
        self.setFixedSize(size, size)

    def set_tint(self, hex_color: str) -> None:
        self._tint = hex_color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        box = QRectF(self.rect())
        fill = QColor(self._tint) if self._active else QColor(glyphs.CHIP_OFF)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 6, 6)
        glyph_color = "white" if self._active else glyphs.MUTED
        _draw_glyph(painter, box, self._glyph, glyph_color, int(self._size * 0.64))
        painter.end()


class ClickableFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _Tile(ClickableFrame):
    """The shared body of the panel's two 52px tiles: a tinted chip on the left, a
    title/subtitle column that stretches, and one trailing widget the subclass adds
    to ``self.row``.

    ToolCard (a tool, trailing its live count) and ActionCard (an action pane,
    trailing a chevron) differ only in that last widget — the selected-state fill,
    the border, the height, the margins and the text column are one design.
    """

    def __init__(self, *, emoji: str, title: str, subtitle: str,
                 hex_color: str, selected: bool) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(52)
        bg = tint_bg(hex_color, 0.16) if selected else "rgba(128,128,128,0.08)"
        border = hex_color if selected else "transparent"
        # The selector is the concrete subclass, so each tile styles only itself
        # (a bare rule here would repaint every descendant of _Tile).
        self.setStyleSheet(
            f"{type(self).__name__} {{ background-color: {bg};"
            f" border: 1.2px solid {border}; border-radius: 8px; }}"
        )

        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(7, 6, 7, 6)
        self.row.setSpacing(8)
        self.row.addWidget(IconChip(emoji, hex_color), 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11px;")
        text.addWidget(t)
        text.addWidget(_elided_label(subtitle))
        self.row.addLayout(text, 1)


class ToolCard(_Tile):
    """A tool tile: tinted emoji chip + title/subtitle + live count."""

    def __init__(
        self, *, emoji: str, title: str, subtitle: str, hex_color: str,
        count: int | None, selected: bool,
    ) -> None:
        super().__init__(emoji=emoji, title=title, subtitle=subtitle,
                         hex_color=hex_color, selected=selected)
        c = QLabel("…" if count is None else str(count))
        c.setStyleSheet(f"color: {hex_color}; font-weight: 700; font-size: 14px;")
        c.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.row.addWidget(c)


class ActionCard(_Tile):
    """A grid tile that opens an action pane (e.g. Review PRs)."""

    def __init__(
        self, *, emoji: str, title: str, subtitle: str, hex_color: str, selected: bool
    ) -> None:
        super().__init__(emoji=emoji, title=title, subtitle=subtitle,
                         hex_color=hex_color, selected=selected)
        chevron = QLabel("›")
        chevron.setStyleSheet(
            f"color: {hex_color if selected else 'palette(mid)'};"
            " font-size: 16px; font-weight: 700;"
        )
        self.row.addWidget(chevron)


class ResultRow(ClickableFrame):
    """One dense, clickable result row → opens the PR/issue in the browser."""

    def __init__(self, *, badge: str, title: str, line2: str, line3: str | None,
                 hex_color: str) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "ResultRow { background-color: rgba(128,128,128,0.06); border-radius: 6px; }"
            "ResultRow:hover { background-color: rgba(128,128,128,0.13); }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(6)

        b = QLabel(badge)
        b.setStyleSheet(
            f"color: {hex_color}; font-weight: 700; font-family: monospace; font-size: 11px;"
        )
        b.setFixedWidth(42)
        b.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(b)

        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(title)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 11px;")
        col.addWidget(t)
        l2 = QLabel(line2)
        l2.setStyleSheet(muted(9))
        col.addWidget(l2)
        if line3:
            l3 = QLabel(line3)
            l3.setStyleSheet(muted(9, mono=True))
            l3.setWordWrap(True)
            col.addWidget(l3)
        row.addLayout(col, 1)

        arrow = QLabel("↗")
        arrow.setStyleSheet(muted(10))
        arrow.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(arrow)


class SectionHeader(ClickableFrame):
    """A collapsible left-pane section header: glyph + TITLE + count + caption + chevron.

    Emits ``clicked`` (via ClickableFrame) so the panel can toggle the section body;
    call :meth:`set_expanded` to flip the chevron.
    """

    def __init__(self, *, glyph: str, title: str, count: int | None = None,
                 caption: str | None = None, expanded: bool = True,
                 glyph_color: str = glyphs.MUTED) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyph, 14, glyph_color, font_px=12))
        t = QLabel(title.upper())
        t.setStyleSheet(
            muted(10, bold=True)
        )
        row.addWidget(t)
        if count is not None:
            c = QLabel(str(count))
            c.setStyleSheet(muted(10, mono=True))
            row.addWidget(c)
        if caption:
            cap = QLabel(caption)
            cap.setStyleSheet(muted(9))
            row.addWidget(cap)
        row.addStretch(1)
        self._chev = QLabel("▾" if expanded else "▸")
        self._chev.setStyleSheet(muted(10))
        row.addWidget(self._chev)

    def set_expanded(self, expanded: bool) -> None:
        self._chev.setText("▾" if expanded else "▸")


class ActivityRow(QFrame):
    """One line in the activity feed: action glyph + detail + source badge + time."""

    def __init__(self, *, glyph: str, detail: str, source: str,
                 source_color: str, clock: str | None,
                 glyph_color: str = glyphs.MUTED) -> None:
        super().__init__()
        self.setStyleSheet(
            "ActivityRow { background-color: rgba(128,128,128,0.05); border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        row.addWidget(GlyphLabel(glyph, 18, glyph_color, font_px=13),
                      0, Qt.AlignmentFlag.AlignTop)
        d = QLabel(detail)
        d.setWordWrap(True)
        d.setStyleSheet("font-size: 10px;")
        row.addWidget(d, 1)
        if source:
            badge = QLabel(source)
            badge.setStyleSheet(
                f"color: {source_color}; background-color: {tint_bg(source_color, 0.15)};"
                " border-radius: 5px; padding: 1px 5px; font-size: 8px; font-weight: 700;"
            )
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        if clock:
            ts = QLabel(clock)
            ts.setStyleSheet(muted(9, mono=True))
            row.addWidget(ts, 0, Qt.AlignmentFlag.AlignTop)


class QueuedTaskRow(QFrame):
    """One unit of automatic work nothing has started yet.

    It carries the two things the rest of the panel has no use for: a handle to start
    it now regardless of what is holding it (``run_requested``), and a drag grip that
    sets the order the queue drains in — drop this row on another and that one emits
    ``dropped`` with the dragged row's queue key.
    """

    run_requested = Signal()
    #: The queue key of the row dropped onto this one.
    dropped = Signal(str)

    _HELD_HELP = (
        "Start this agent now, without waiting for a free slot. It then counts "
        "against the cap like any automatic agent."
    )
    _PAUSED_HELP = (
        "Start this agent now. Its monitor is switched off, so nothing else will — "
        "but once running it counts against the cap like any automatic agent."
    )

    def __init__(self, *, task_id: str, label: str, glyph: str, hex_color: str,
                 paused: bool) -> None:
        super().__init__()
        self._task_id = task_id
        self._press: QPointF | None = None
        self.setAcceptDrops(True)
        self._set_targeted(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)

        grip = GlyphLabel(glyphs.G_GRIP, 14, glyphs.MUTED, font_px=12)
        grip.setToolTip("Drag onto another queued row to set the order the queue runs in.")
        grip.setCursor(Qt.CursorShape.OpenHandCursor)
        row.addWidget(grip, 0, Qt.AlignmentFlag.AlignVCenter)
        # The panel's "off" chip (neutral fill, grey glyph), as the free devices and
        # lookup misses wear — nothing has started yet.
        row.addWidget(IconChip(glyph, hex_color, 22, active=False),
                      0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(1)
        title = ElidedLabel(label, 10, "#d8dbde")
        col.addWidget(title)
        status = QLabel(
            f"{glyphs.G_TASKS} queued" + (" · monitor off" if paused else "")
        )
        status.setStyleSheet(muted(9))
        col.addWidget(status)
        row.addLayout(col, 1)

        run = QPushButton("execute now")
        run.setCursor(Qt.CursorShape.PointingHandCursor)
        run.setToolTip(self._PAUSED_HELP if paused else self._HELD_HELP)
        run.setStyleSheet(
            f"QPushButton {{ color: {hex_color}; background-color: {tint_bg(hex_color, 0.16)};"
            " border: none; border-radius: 8px; padding: 2px 7px;"
            " font-size: 9px; font-weight: 600; }"
            f"QPushButton:hover {{ background-color: {tint_bg(hex_color, 0.30)}; }}"
        )
        run.clicked.connect(self.run_requested.emit)
        row.addWidget(run, 0, Qt.AlignmentFlag.AlignVCenter)

    # Drag out: the whole row is the handle (the grip says so), so a press-and-move
    # anywhere on it starts the drag — except on the button, which gets the press first.
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._press is not None and (
            (event.position() - self._press).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._press = None
            mime = QMimeData()
            mime.setText(self._task_id)
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
        super().mouseMoveEvent(event)

    # Drop in: another queued row landed on this one. A drag this row refuses is one
    # it never becomes a target for, so the highlight can only appear on a drop that
    # would land (and the rebuild that follows one takes the row with it).
    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._dragged_key(event) is None:
            return
        event.acceptProposedAction()
        self._set_targeted(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._set_targeted(False)

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = self._dragged_key(event)
        if key is None:
            return
        event.acceptProposedAction()
        self.dropped.emit(key)

    def _set_targeted(self, targeted: bool) -> None:
        """Outline the row while a drop would land on it."""
        border = "palette(highlight)" if targeted else "transparent"
        self.setStyleSheet(
            "QueuedTaskRow { background-color: rgba(128,128,128,0.06);"
            f" border-radius: 6px; border: 1px solid {border}; }}"
        )

    def _dragged_key(self, event) -> str | None:
        """The queue key being dragged, or None when this drag is not one of ours.

        A drop onto the row that is being dragged is not a rearrangement, so it is
        refused here rather than reordering to the same list."""
        mime = event.mimeData()
        if not mime.hasText():
            return None
        key = mime.text()
        return None if not key or key == self._task_id else key


class StartingTaskRow(QFrame):
    """A queued task whose dispatch is under way — clicked, or reached by the drain —
    waiting on a mesh round-trip and a terminal spawn that take seconds between them.

    It is the queued row minus the two handles that would now be lies (the order no
    longer decides when it runs; it is already running past the cap) and with the lit
    chip of the agent it is about to be. Everything else — same label, same list — is
    deliberately the same, so what the operator watches is one row changing rather
    than rows appearing and disappearing under a click.
    """

    _HELP = (
        "Starting this agent — waiting on the spawn. It is holding a slot of the cap "
        "already, like any automatic agent."
    )

    def __init__(self, *, label: str, glyph: str, hex_color: str) -> None:
        super().__init__()
        self.setToolTip(self._HELP)
        self.setStyleSheet(
            "StartingTaskRow { background-color: rgba(128,128,128,0.06);"
            " border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        # Lit, where the queued row's is off: the first thing the click changes, read
        # before a word of the row is.
        row.addWidget(IconChip(glyph, hex_color, 22, active=True),
                      0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(ElidedLabel(label, 10, "#d8dbde"))
        status = QLabel(f"{glyphs.G_STARTING} starting")
        status.setStyleSheet(f"color: {glyphs.STARTING}; font-size: 9px;")
        col.addWidget(status)
        row.addLayout(col, 1)


class FreeSlotRow(QFrame):
    """One slot of this device's automatic-task cap with nothing running in it.

    Drawn as an outline rather than left out, so the cap is something the panel shows
    rather than something the operator has to remember: an idle machine reads as two
    open bays waiting for work, and a full one has none.
    """

    _HELP = (
        "A free slot of this machine's automatic-task cap. The next poll starts "
        "queued work here, unless its monitor is switched off. The cap is in Settings."
    )

    def __init__(self) -> None:
        super().__init__()
        self.setToolTip(self._HELP)
        self.setStyleSheet(
            "FreeSlotRow { border: 1px dashed rgba(128,128,128,0.45);"
            " border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        # The hollow twin of a queued row's IconChip, at the same size, so every row
        # lines up down the left edge.
        bay = QLabel()
        bay.setFixedSize(22, 22)
        bay.setStyleSheet(
            "border: 1px dashed rgba(128,128,128,0.55); border-radius: 5px;"
        )
        row.addWidget(bay, 0, Qt.AlignmentFlag.AlignVCenter)
        # The whole row is a status, so it wears a status line rather than a label.
        text = QLabel(f"{glyphs.G_FREE_SLOT} free slot")
        text.setStyleSheet(muted(9))
        row.addWidget(text, 1)


class BanRow(QFrame):
    """One banned author: raised-hand glyph + @login + reason."""

    def __init__(self, *, login: str, reason: str | None) -> None:
        super().__init__()
        self.setStyleSheet(
            "BanRow { background-color: rgba(128,128,128,0.06); border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        row.addWidget(GlyphLabel(glyphs.G_BAN, 16, "#FF3B30", font_px=14),
                      0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(f"@{login}")
        t.setStyleSheet("font-size: 10px; font-weight: 600;")
        col.addWidget(t)
        if reason:
            r = QLabel(reason)
            r.setWordWrap(True)
            r.setStyleSheet(muted(9))
            col.addWidget(r)
        row.addLayout(col, 1)


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(128,128,128,0.3);")
    return line


def dispatch_status_text(verdict: str, terminal_title: str) -> str:
    """The wizard status line for one ``Store.dispatch_agent`` verdict - shared by
    all three wizards so refusals read identically everywhere (macOS twin:
    ``statusText(for:terminal:)``)."""
    from . import autofix

    if verdict == "spawned":
        return f"Launched {terminal_title}"
    if verdict == autofix.VERDICT_IN_FLIGHT:
        return "An agent is already on this PR - see its session above."
    if verdict == autofix.VERDICT_BANNED:
        return "Author is banned for prompt injection - un-ban to review."
    if verdict == autofix.VERDICT_STAND_DOWN:
        return "Another mesh node originates this work."
    return "Spawn failed - see the activity feed."


# ---- Spawn-wizard chrome --------------------------------------------------
#
# The Review / Resolve-conflicts / Full-E2E wizards are three renderers over one
# layout: a title, contextual rows, a mesh row + SPAWN button, a status line. Each
# piece of it lives here once, including the SPAWN button's fill rule — the audit's
# config is always valid, so that wizard is the one most likely to end up with a
# hardcoded fill that no longer matches its neighbours. The macOS twins live in
# Components.swift.

# The two chrome styles that are not plain muted text (see :func:`muted`).
_TITLE_CSS = "font-weight: 700; font-size: 13px;"
_WARNING_CSS = "color: #e0563f; font-size: 10px;"
# The fill a SPAWN button takes while its config is not spawnable.
SPAWN_DISABLED_TINT = "#888888"


def wizard_title(glyph: str, text: str) -> QLabel:
    """A wizard's heading: its glyph, then the tool name."""
    label = QLabel(f"{glyph}  {text}")
    label.setStyleSheet(_TITLE_CSS)
    return label


def wizard_blurb(text: str) -> QLabel:
    """The grey explainer paragraph under a heading or a toggle."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(muted())
    return label


def wizard_warning() -> QLabel:
    """The red note under the single-PR field (starts empty and hidden)."""
    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(_WARNING_CSS)
    return label


def wizard_status() -> QLabel:
    """The monospaced line under SPAWN that reports what the click did."""
    label = QLabel("")
    label.setStyleSheet(muted(mono=True))
    label.setWordWrap(True)
    return label


def spawn_button(on_click) -> QPushButton:
    """The SPAWN AGENT button. Style it with :func:`style_spawn_button`, which
    every wizard must call whenever its validity can have changed."""
    btn = QPushButton("▶  SPAWN AGENT")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.clicked.connect(on_click)
    return btn


def style_spawn_button(btn: QPushButton, tint: str, is_valid: bool) -> None:
    """Enable/disable the button and fill it with ``tint`` or the disabled grey,
    so it never looks armed while a click would do nothing."""
    btn.setEnabled(is_valid)
    fill = tint if is_valid else SPAWN_DISABLED_TINT
    btn.setStyleSheet(
        f"QPushButton {{ background-color: {fill}; color: white; font-weight: 700;"
        f" padding: 8px; border-radius: 7px; }}"
    )
