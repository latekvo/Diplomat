"""Small reusable Qt widgets for the panel (cards, chips, rows)."""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QMimeData,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetricsF, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
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

    def set_active(self, active: bool) -> None:
        self._active = active
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


class RunningTaskRow(QFrame):
    """An automatic agent that is up: a slot of this device's cap with something in
    it, drawn where the free bay it took would be.

    A spawn here is a detached ``Popen`` in a terminal this applet does not own, so
    the row is a *status* and nothing else — there is no window handle to click and
    nothing to stop tracking. What it can say it says: which work, how long it has
    been going, and — when the record behind it was lost to a restart — that the PR
    number is all this machine still knows about it.
    """

    _HELP = (
        "An automatic agent running on this machine. It holds a slot of the "
        "automatic-task cap while it works."
    )
    _UNTRACKED_HELP = (
        "A Claude agent running on this machine, found by scanning processes — this "
        "applet has no record of starting it (a restart loses the record, not the "
        "agent). It counts against the automatic-task cap while it works."
    )
    _AWAITING_HELP = (
        "This agent has finished its turn and is waiting at its prompt — an agent is "
        "spawned into an interactive session, which stays open until you close it. "
        "Its slot of the automatic-task cap has already been given back."
    )

    def __init__(self, *, label: str, detail: str, glyph: str, hex_color: str,
                 tracked: bool, awaiting_input: bool = False) -> None:
        super().__init__()
        if awaiting_input:
            self.setToolTip(self._AWAITING_HELP)
        else:
            self.setToolTip(self._HELP if tracked else self._UNTRACKED_HELP)
        self.setStyleSheet(
            "RunningTaskRow { background-color: rgba(128,128,128,0.06);"
            " border-radius: 6px; }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        # Lit, like the starting row it replaces: the chip is on for as long as
        # there is an agent behind it — including one that is only waiting, which is
        # a window still open and still holding its context.
        row.addWidget(IconChip(glyph, hex_color, 22, active=True),
                      0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(ElidedLabel(label, 10, "#d8dbde"))
        # The status line is where the two part: live blue is what the panel uses for
        # work in progress, and an agent waiting on a human is not that. Grey, against
        # the free bay now drawn beside it, is the pair the operator reads as "this
        # one is done with, and its slot is already back".
        status = QLabel(
            f"{glyphs.G_AWAITING if awaiting_input else glyphs.G_RUNNING} {detail}"
        )
        tint = glyphs.MUTED if awaiting_input else glyphs.AGENT_LIVE
        status.setStyleSheet(f"color: {tint}; font-size: 9px;")
        col.addWidget(status)
        row.addLayout(col, 1)


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
        status.setStyleSheet(f"color: {glyphs.AGENT_LIVE}; font-size: 9px;")
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
    # A wizard SPAWN is a panel dispatch, so none of the mesh gate, the task cap and
    # the rate-limit budget applies to it. Spelled out anyway, and matching the Swift
    # twin word for word, because the fallback below reads every unknown verdict as a
    # spawn failure — the one answer that would send someone looking at a terminal
    # that was never asked to open.
    if verdict == autofix.VERDICT_AT_CAPACITY:
        return "This machine is at its cap of concurrent automatic tasks."
    if verdict == autofix.VERDICT_UNAFFORDABLE:
        return "Too little rate limit left for automatic work."
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


# ---- Settings chrome ------------------------------------------------------
#
# Settings is a form, and a form is the same few shapes over and over: a titled
# card, a named row with a control on its right, a small state token, and the
# controls themselves. Qt ships none of the controls this screen wants — its
# checkbox is a tick in a box and its only multiple-choice widget is a dropdown —
# so the switch, the segmented picker and the chips are drawn here. The macOS
# twins live in Components.swift.

#: Track size of :class:`SwitchToggle`, and the knob's inset within it.
_SWITCH_W, _SWITCH_H, _SWITCH_PAD = 34, 19, 2
#: The default "on" fill for a switch — the same blue the panel's accents use.
SWITCH_TINT = "#0A84FF"


class SwitchToggle(QAbstractButton):
    """A sliding on/off switch: a filled track with a knob that travels across it.

    Qt's ``QCheckBox`` is a tick in a box, which reads as "tick this to agree"
    rather than "this is on" — wrong for a screen of live behaviour switches, and
    unable to say at a glance which of a column of settings are running.

    The knob animates, except while the widget is not yet shown: ``setChecked``
    during construction would otherwise leave every switch frozen part-way across
    in a headless render, since no event loop runs between build and grab.
    """

    def __init__(self, tint: str = SWITCH_TINT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_SWITCH_W, _SWITCH_H)
        self._tint = tint
        self._travel = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"travel", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._on_toggled)

    def set_tint(self, tint: str) -> None:
        self._tint = tint
        self.update()

    def _get_travel(self) -> float:
        return self._travel

    def _set_travel(self, value: float) -> None:
        self._travel = value
        self.update()

    #: 0 = knob fully left (off), 1 = fully right (on). Animated, so it is a Qt
    #: property rather than a plain attribute.
    travel = Property(float, _get_travel, _set_travel)

    def _on_toggled(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if not self.isVisible():
            self._anim.stop()
            self._set_travel(target)
            return
        self._anim.stop()
        self._anim.setStartValue(self._travel)
        self._anim.setEndValue(target)
        self._anim.start()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = _SWITCH_H / 2
        off = QColor(128, 128, 128, 90)
        on = QColor(self._tint)
        track = QColor(
            round(off.red() + (on.red() - off.red()) * self._travel),
            round(off.green() + (on.green() - off.green()) * self._travel),
            round(off.blue() + (on.blue() - off.blue()) * self._travel),
            round(off.alpha() + (255 - off.alpha()) * self._travel),
        )
        if not self.isEnabled():
            track.setAlpha(round(track.alpha() * 0.4))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, _SWITCH_W, _SWITCH_H), radius, radius)

        knob = _SWITCH_H - 2 * _SWITCH_PAD
        x = _SWITCH_PAD + self._travel * (_SWITCH_W - knob - 2 * _SWITCH_PAD)
        painter.setBrush(QColor(255, 255, 255, 255 if self.isEnabled() else 150))
        painter.drawEllipse(QRectF(x, _SWITCH_PAD, knob, knob))
        painter.end()

    def sizeHint(self):  # noqa: N802 (Qt override)
        return self.size()


class Pill(QLabel):
    """A small state token: a word in a tint, on a capsule of the same tint.

    Used for everything Settings *reports* rather than sets — a monitor's
    live/idle, the allocator's version, the mesh's peer count — and, via
    :meth:`set_mark`, for one present/missing part of a multi-part install.
    """

    def __init__(self, text: str = "", tint: str = "#8E8E93") -> None:
        super().__init__()
        self.set_state(text, tint)

    def set_state(self, text: str, tint: str = "#8E8E93") -> None:
        self.setText(text)
        self.setVisible(bool(text))
        self.setStyleSheet(
            f"color: {tint}; background-color: {tint_bg(tint, 0.14)};"
            " border-radius: 7px; padding: 2px 6px;"
            " font-size: 9px; font-weight: 700;"
        )

    def set_mark(self, label: str, ok: bool) -> None:
        """One part of an install: ✓ green when present, ✗ red when missing.

        The allocator's four parts used to be a single monospaced run — "MCP ✓ ·
        skill ✓ · rule ✗" — which reads as a filename until it is parsed word by
        word."""
        self.set_state(f"{'✓' if ok else '✗'} {label}", "#34C759" if ok else "#FF3B30")


class SegmentedControl(QWidget):
    """A row of joined pills, one of which is selected — for a choice of two to
    five, where a dropdown hides every option but the current one behind a click.

    ``changed`` carries the selected key (the second half of each option pair).
    """

    changed = Signal(object)

    def __init__(self, options: list[tuple[str, object]], *,
                 tint: str = SWITCH_TINT) -> None:
        super().__init__()
        self._keys: list[object] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        self.setStyleSheet(
            "SegmentedControl { background-color: rgba(128,128,128,0.14);"
            " border-radius: 7px; }"
        )
        for index, (label, key) in enumerate(options):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 5px; padding: 4px 8px;"
                " font-size: 11px; color: palette(text); background: transparent; }"
                f"QPushButton:checked {{ background-color: {tint_bg(tint, 0.85)};"
                " color: white; font-weight: 600; }"
            )
            self._group.addButton(btn, index)
            self._keys.append(key)
            row.addWidget(btn, 1)
        self._group.idClicked.connect(self._on_clicked)

    def _on_clicked(self, index: int) -> None:
        self.changed.emit(self._keys[index])

    def set_value(self, key: object) -> None:
        """Select the segment for ``key``, without emitting ``changed`` — this is
        the store writing to the UI, not the UI writing to the store."""
        if key not in self._keys:
            return
        self._group.button(self._keys.index(key)).setChecked(True)

    def value(self) -> object | None:
        checked = self._group.checkedId()
        return self._keys[checked] if checked >= 0 else None


class ToggleChip(QPushButton):
    """A capsule that fills with its tint while on — the multi-select counterpart
    of a switch, for a set of flags short enough to name in a word each."""

    def __init__(self, label: str, *, tint: str = "#FF9500") -> None:
        super().__init__(label)
        self._tint = tint
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(lambda _: self._restyle())
        self._restyle()

    def _restyle(self) -> None:
        on = self.isChecked()
        self.setStyleSheet(
            f"QPushButton {{ color: {self._tint if on else 'palette(mid)'};"
            f" background-color: {tint_bg(self._tint, 0.18 if on else 0.07)};"
            f" border: 1px solid {tint_bg(self._tint, 0.7 if on else 0.22)};"
            " border-radius: 9px; padding: 3px 9px;"
            f" font-size: 10px; font-weight: {'700' if on else '500'}; }}"
        )


class ChoiceChips(QWidget):
    """One-of-many as a grid of chips, all visible at once.

    :class:`SegmentedControl`'s wide cousin, for a choice too long to sit in one
    row — the spawn terminal, of which Linux knows seven. A dropdown would hide
    six of them behind a click, including whichever ones are actually installed.

    ``changed`` carries the selected key.
    """

    changed = Signal(object)

    def __init__(self, options: list[tuple[str, object]], *, columns: int = 3,
                 tint: str = SWITCH_TINT) -> None:
        super().__init__()
        self._keys: list[object] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        for index, (label, key) in enumerate(options):
            chip = ToggleChip(label, tint=tint)
            self._group.addButton(chip, index)
            self._keys.append(key)
            grid.addWidget(chip, index // columns, index % columns)
        self._group.idClicked.connect(lambda i: self.changed.emit(self._keys[i]))

    def chip(self, key: object) -> ToggleChip | None:
        return (self._group.button(self._keys.index(key))
                if key in self._keys else None)

    def set_value(self, key: object) -> None:
        """Select the chip for ``key``, without emitting ``changed`` — this is the
        store writing to the UI, not the UI writing to the store."""
        if key in self._keys:
            self._group.button(self._keys.index(key)).setChecked(True)

    def value(self) -> object | None:
        checked = self._group.checkedId()
        return self._keys[checked] if checked >= 0 else None


class SliderSetting(QWidget):
    """A slider with the value in a badge beside it and the ends of the range
    named underneath.

    Replaces the spin boxes, which show a number without showing the range it
    lives in — the only way to learn a cap of 16 existed was to click sixteen
    times. ``changed`` carries the (integer) value.
    """

    changed = Signal(int)

    def __init__(self, *, label: str, minimum: int, maximum: int, step: int = 1,
                 min_label: str, max_label: str, tint: str = "#FF9500") -> None:
        super().__init__()
        self._tint = tint
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setAccessibleName(label)
        self._slider.setRange(minimum, maximum)
        self._slider.setSingleStep(step)
        self._slider.setPageStep(step)
        # A tick per stop, so the granularity the value snaps to is visible and not
        # just enforced. Mirrors `Slider(value:in:step:)` in Components.swift.
        self._slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider.setTickInterval(step)
        self._slider.valueChanged.connect(self._on_changed)
        row.addWidget(self._slider, 1)
        self._badge = QLabel("")
        self._badge.setStyleSheet(
            f"color: {tint}; font-size: 10px; font-weight: 700; font-family: monospace;"
        )
        self._badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._badge.setMinimumWidth(58)
        row.addWidget(self._badge)
        col.addLayout(row)

        ends = QHBoxLayout()
        ends.setSpacing(4)
        lo = QLabel(min_label)
        lo.setStyleSheet(muted(9))
        hi = QLabel(max_label)
        hi.setStyleSheet(muted(9))
        ends.addWidget(lo)
        ends.addStretch(1)
        ends.addWidget(hi)
        col.addLayout(ends)

        self._step = step
        self._badge_text = str

    def set_badge_text(self, fn) -> None:
        """How to render the current value in the badge — ``4`` → "4 tasks"."""
        self._badge_text = fn
        self._badge.setText(fn(self._slider.value()))

    def _on_changed(self, value: int) -> None:
        # Snap to the step the stored setting is quantised in, so dragging can
        # only produce values the store would also accept.
        snapped = round(value / self._step) * self._step
        if snapped != value:
            self._slider.setValue(snapped)
            return
        self._badge.setText(self._badge_text(value))
        self.changed.emit(value)

    def badge(self) -> str:
        return self._badge.text()

    def set_value(self, value: int) -> None:
        """Move the slider without emitting ``changed`` — the store writing to the
        UI, not the reverse."""
        blocked = self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(blocked)
        self._badge.setText(self._badge_text(self._slider.value()))

    def value(self) -> int:
        return self._slider.value()

    def minimum(self) -> int:
        return self._slider.minimum()

    def maximum(self) -> int:
        return self._slider.maximum()


def settings_card(glyph: str, title: str, tint: str) -> tuple[QWidget, QVBoxLayout, Pill]:
    """One block of Settings: a tinted glyph, a caps title, a state pill, and the
    rows under them on a soft card.

    Returns the host, the layout to add rows to, and the pill — which starts empty
    and hidden, so a card with nothing to report simply has none.
    """
    host = QWidget()
    host.setObjectName("settingsCard")
    outer = QVBoxLayout(host)
    outer.setContentsMargins(10, 9, 10, 9)
    outer.setSpacing(10)
    host.setStyleSheet(
        "#settingsCard { background-color: rgba(128,128,128,0.11);"
        " border: 1px solid rgba(128,128,128,0.22); border-radius: 10px; }"
    )

    head = QHBoxLayout()
    head.setSpacing(6)
    badge = GlyphLabel(glyph, 17, tint, 9)
    badge.setStyleSheet(f"background-color: {tint_bg(tint, 0.16)}; border-radius: 5px;")
    head.addWidget(badge)
    label = QLabel(title)
    label.setStyleSheet(
        "color: palette(mid); font-size: 10px; font-weight: 800; letter-spacing: 1px;"
    )
    head.addWidget(label)
    head.addStretch(1)
    pill = Pill("")
    head.addWidget(pill)
    outer.addLayout(head)

    body = QVBoxLayout()
    body.setSpacing(10)
    outer.addLayout(body)
    return host, body, pill


class SettingRow(QWidget):
    """One setting: its name, the control that sets it, and — under both — an
    optional one-line summary.

    ``detail`` is the long-form paragraph, drawn only while the header's *Explain*
    switch is on (:meth:`set_explain`), so the screen defaults to something you can
    scan and still holds every word of what a knob does.

    ``stacked`` puts the control under the title instead of beside it, for the wide
    ones (a text field, a segmented picker) that have no room in a trailing slot.
    """

    def __init__(self, title: str, control: QWidget, *, summary: str | None = None,
                 detail: str | None = None, stacked: bool = False) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)

        name = QLabel(title)
        name.setStyleSheet("font-size: 11px; font-weight: 600;")
        # The title is a separate label, so a screen reader on the control alone
        # would read a bare switch. Twin of `switchControl(_:_:)` in SettingsView.swift.
        self._control = control
        if not control.accessibleName():
            control.setAccessibleName(title)
        if stacked:
            col.addWidget(name)
            col.addWidget(control)
        else:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(control)
            col.addLayout(row)

        # Under the control either way: half of these lines report what the control
        # currently resolves to (which handle is in force, where `cd` will land),
        # and a consequence reads wrong above its cause.
        self._summary = QLabel(summary or "")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(muted(10))
        self._summary.setVisible(bool(summary))
        col.addWidget(self._summary)

        # Built whether or not there is a paragraph yet: a row whose detail is
        # state-dependent (the runner's, the repo root's) would otherwise have
        # nowhere to put one when it acquires it.
        self._explain = False
        self._detail = QLabel(detail or "")
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(
            muted(10) + " background-color: rgba(128,128,128,0.09);"
            " border-radius: 6px; padding: 5px 7px;"
        )
        self._detail.setVisible(False)
        col.addWidget(self._detail)

    def set_summary(self, text: str, *, color: str | None = None) -> None:
        """Rewrite the one-liner — for the rows whose summary reports live state
        (the resolved repo path, what an update check found)."""
        self._summary.setText(text)
        self._summary.setVisible(bool(text))
        self._summary.setStyleSheet(
            muted(10) if color is None else f"color: {color}; font-size: 10px;"
        )

    def summary(self) -> str:
        return self._summary.text()

    def set_detail(self, text: str | None) -> None:
        """Rewrite the paragraph — for the rows whose explanation depends on state
        (which runner is chosen, whether the repo root resolves)."""
        self._detail.setText(text or "")
        self._detail.setVisible(bool(text) and self._explain)

    def set_explain(self, on: bool) -> None:
        self._explain = on
        self._detail.setVisible(on and bool(self._detail.text()))


def nested_settings(tint: str) -> tuple[QWidget, QVBoxLayout]:
    """Settings that exist only while the switch above them is on, indented behind
    a tinted rail — so the dependency is drawn rather than left to be inferred from
    an indent, which is all that distinguished the nested verdict policy before."""
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(1, 0, 0, 0)
    row.setSpacing(9)
    rail = QFrame()
    rail.setFixedWidth(2)
    rail.setStyleSheet(f"background-color: {tint_bg(tint, 0.4)}; border-radius: 1px;")
    row.addWidget(rail)
    col = QVBoxLayout()
    col.setSpacing(9)
    row.addLayout(col, 1)
    return host, col
