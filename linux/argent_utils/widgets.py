"""Small reusable Qt widgets for the panel (cards, chips, rows)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)


def tint_bg(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.3f})"


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


def _elided_label(text: str, _style_ignored: str) -> ElidedLabel:
    return ElidedLabel(text, 9, "#9aa0a6")


class IconChip(QLabel):
    """A rounded, tinted square showing the tool's emoji glyph."""

    def __init__(self, emoji: str, hex_color: str, size: int = 26) -> None:
        super().__init__(emoji)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background-color: {hex_color}; border-radius: 6px; font-size: {int(size*0.5)}px;"
        )


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
        s = _elided_label(subtitle, "color: palette(mid); font-size: 9px;")
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
        s = _elided_label(subtitle, "color: palette(mid); font-size: 9px;")
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


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: rgba(128,128,128,0.3);")
    return line
