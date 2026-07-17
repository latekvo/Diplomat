"""Small reusable Qt widgets for the panel (cards, chips, rows)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)

from . import glyphs


def tint_bg(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.3f})"


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


class ToolCard(ClickableFrame):
    """A tool tile: tinted emoji chip + title/subtitle + live count."""

    def __init__(
        self, *, emoji: str, title: str, subtitle: str, hex_color: str,
        count: int | None, selected: bool,
    ) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(52)
        self._style(hex_color, selected)

        row = QHBoxLayout(self)
        row.setContentsMargins(7, 6, 7, 6)
        row.setSpacing(8)
        row.addWidget(IconChip(emoji, hex_color), 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11px;")
        s = _elided_label(subtitle)
        text.addWidget(t)
        text.addWidget(s)
        row.addLayout(text, 1)

        c = QLabel("…" if count is None else str(count))
        c.setStyleSheet(f"color: {hex_color}; font-weight: 700; font-size: 14px;")
        c.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        row.addWidget(c)

    def _style(self, hex_color: str, selected: bool) -> None:
        bg = tint_bg(hex_color, 0.16) if selected else "rgba(128,128,128,0.08)"
        border = hex_color if selected else "transparent"
        self.setStyleSheet(
            f"ToolCard {{ background-color: {bg}; border: 1.2px solid {border};"
            f" border-radius: 8px; }}"
        )


class ActionCard(ClickableFrame):
    """A grid tile that opens an action pane (e.g. Review PRs)."""

    def __init__(
        self, *, emoji: str, title: str, subtitle: str, hex_color: str, selected: bool
    ) -> None:
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(52)
        bg = tint_bg(hex_color, 0.16) if selected else "rgba(128,128,128,0.08)"
        border = hex_color if selected else "transparent"
        self.setStyleSheet(
            f"ActionCard {{ background-color: {bg}; border: 1.2px solid {border};"
            f" border-radius: 8px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(7, 6, 7, 6)
        row.setSpacing(8)
        row.addWidget(IconChip(emoji, hex_color), 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11px;")
        s = _elided_label(subtitle)
        text.addWidget(t)
        text.addWidget(s)
        row.addLayout(text, 1)
        chevron = QLabel("›")
        chevron.setStyleSheet(
            f"color: {hex_color if selected else 'palette(mid)'}; font-size: 16px; font-weight: 700;"
        )
        row.addWidget(chevron)


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
        l2.setStyleSheet("color: palette(mid); font-size: 9px;")
        col.addWidget(l2)
        if line3:
            l3 = QLabel(line3)
            l3.setStyleSheet("color: palette(mid); font-size: 9px; font-family: monospace;")
            l3.setWordWrap(True)
            col.addWidget(l3)
        row.addLayout(col, 1)

        arrow = QLabel("↗")
        arrow.setStyleSheet("color: palette(mid); font-size: 10px;")
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
            "color: palette(mid); font-weight: 700; font-size: 10px;"
        )
        row.addWidget(t)
        if count is not None:
            c = QLabel(str(count))
            c.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 10px;")
            row.addWidget(c)
        if caption:
            cap = QLabel(caption)
            cap.setStyleSheet("color: palette(mid); font-size: 9px;")
            row.addWidget(cap)
        row.addStretch(1)
        self._chev = QLabel("▾" if expanded else "▸")
        self._chev.setStyleSheet("color: palette(mid); font-size: 10px;")
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
            ts.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 9px;")
            row.addWidget(ts, 0, Qt.AlignmentFlag.AlignTop)


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
            r.setStyleSheet("color: palette(mid); font-size: 9px;")
            col.addWidget(r)
        row.addLayout(col, 1)


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(128,128,128,0.3);")
    return line
