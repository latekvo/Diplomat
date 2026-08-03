"""The Telemetry screen — what the monitors cost and what they still owe.

The Linux face of the ledger (:mod:`telemetry`), and one of the panel's four
screens: Actions · Mesh · **Telemetry** · Settings. It reads
``~/.diplomat/pr-monitor/telemetry.jsonl``, folds it through the shared
arithmetic, and draws seven figures:

* what share of the 5-hour rate-limit window one auto-task consumes, on average;
* how that share is distributed, as a histogram with a fitted normal and a
  confidence interval on the mean;
* how many auto-reviews were owed but unstarted, over the lookback;
* the same for auto-fixes;
* mean time from an agent starting to its completion sentinel;
* mean time from the monitor first seeing the work to an agent taking it;
* how much of this machine's Claude spend went on this repo rather than
  everything else.

Read-only: the one control is the lookback, and flipping it recomputes from the
same fold. The two charts are painted rather than assembled from widgets — a
histogram and a two-series time plot are one ``paintEvent`` each, and the
alternative is a few hundred stacked QFrames that Qt lays out on every repaint.

The macOS twin is ``TelemetryView.swift``; the numbers both draw come from the
shared model in ``assets/telemetry.json`` and the shared math, so the two can
only differ in how they look.
"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import core, glyphs, telemetry
from .store import Store
from .widgets import GlyphLabel, card_host, muted, tint_bg

#: Tints, read from the shared model so a figure reads the same colour on both
#: platforms and beside its twin in the mesh duty table.
_METRIC = {m["id"]: m for m in core.telemetry()["metrics"]}


def _tint(metric_id: str) -> str:
    return _METRIC.get(metric_id, {}).get("colorHex", "#8E8E93")


def _glyph(metric_id: str) -> str:
    return _METRIC.get(metric_id, {}).get("linuxGlyph", "•")


def _title(metric_id: str) -> str:
    return _METRIC.get(metric_id, {}).get("title", metric_id)


def _blurb(metric_id: str) -> str:
    return _METRIC.get(metric_id, {}).get("blurb", "")


def _qcolor(hex_color: str, alpha: float = 1.0) -> QColor:
    """A paintable colour with an alpha channel. ``widgets.tint_bg`` builds the CSS
    ``rgba(...)`` spelling for stylesheets, which QColor does not parse — a chart
    fed one draws in default black, which looks like a paint bug rather than a
    colour bug."""
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def _legend(entries: list[tuple[str, str]]) -> QHBoxLayout:
    """A chart key: one swatch-coloured label per series. One label with several
    markers in it can only carry a single colour, which makes the key claim both
    series are the colour of the first — the one thing a key must not do."""
    row = QHBoxLayout()
    row.setSpacing(12)
    for color, text in entries:
        label = QLabel(f"◼ {text}")
        label.setStyleSheet(f"color: {color}; font-size: 9px; font-family: monospace;")
        row.addWidget(label)
    row.addStretch(1)
    return row


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


# MARK: - charts


class SpreadChart(QWidget):
    """The bell curve: a histogram of per-task cost, the fitted normal over it, and
    the confidence interval on the mean as a shaded band.

    The band is deliberately drawn *behind* the bars and the mean as a solid rule,
    so the eye reads "the average is here, and this is how well we know it" rather
    than mistaking the interval for the spread of the tasks themselves — which is
    the histogram, and is much wider.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(150)
        self._dist: telemetry.Distribution | None = None
        self._tint = _tint("limitSpread")

    def set_distribution(self, dist: telemetry.Distribution | None) -> None:
        self._dist = dist
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        d = self._dist
        if d is None or d.count == 0 or not d.bins:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        pad_l, pad_r, pad_t, pad_b = 4.0, 4.0, 8.0, 16.0
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b
        hi = d.bins[-1].upper or 1.0
        # The curve's peak can exceed the tallest bar (a tight distribution
        # sampled into wide bins), so both share one scale or the fit would be
        # clipped where it matters most.
        top = max(max((b.count for b in d.bins), default=1),
                  max(d.curve, default=0.0), 1.0)

        def x_of(value: float) -> float:
            return pad_l + w * min(1.0, max(0.0, value / hi))

        def y_of(count: float) -> float:
            return pad_t + h * (1.0 - min(1.0, count / top))

        # Confidence band on the mean, behind everything else so it reads as
        # context for the mean rule rather than as another series.
        if d.ci_high > d.ci_low:
            lo, hi_ci = x_of(d.ci_low), x_of(d.ci_high)
            painter.fillRect(QRectF(lo, pad_t, max(1.0, hi_ci - lo), h),
                             _qcolor(self._tint, 0.16))

        # Histogram bars.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qcolor(self._tint, 0.55))
        for b in d.bins:
            if b.count <= 0:
                continue
            x0, x1 = x_of(b.lower), x_of(b.upper)
            y = y_of(b.count)
            painter.drawRoundedRect(QRectF(x0 + 0.8, y, max(1.0, x1 - x0 - 1.6),
                                           pad_t + h - y), 2, 2)

        # The fitted normal.
        if len(d.curve) > 1:
            path = QPainterPath()
            for i, value in enumerate(d.curve):
                px = pad_l + w * i / (len(d.curve) - 1)
                py = y_of(value)
                if i == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            pen = QPen(QColor(self._tint))
            pen.setWidthF(1.8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawPath(path)

        # The mean, as a full-height rule.
        mean_x = x_of(d.mean)
        pen = QPen(QColor("#FFFFFF"))
        pen.setWidthF(1.2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(int(mean_x), int(pad_t), int(mean_x), int(pad_t + h))

        # Axis: 0 on the left, the largest observation on the right.
        painter.setPen(QColor(glyphs.MUTED))
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 60, 12),
                         int(Qt.AlignmentFlag.AlignLeft), "0%")
        painter.drawText(QRectF(pad_l + w - 60, pad_t + h + 2, 60, 12),
                         int(Qt.AlignmentFlag.AlignRight), telemetry.percent(d.max))
        painter.end()


class PendingChart(QWidget):
    """Owed-but-unstarted work over the lookback: two filled series, reviews and
    conflict fixes, on a shared count axis with day gridlines."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(160)
        self._points: tuple[telemetry.PendingPoint, ...] = ()
        self._days = 14.0

    def set_series(self, points: tuple[telemetry.PendingPoint, ...], days: float) -> None:
        self._points = points
        self._days = days
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        pts = self._points
        if len(pts) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        pad_l, pad_r, pad_t, pad_b = 4.0, 4.0, 8.0, 16.0
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b
        top = max(1, max(max(p.reviews, p.conflicts) for p in pts))

        def x_of(i: int) -> float:
            return pad_l + w * i / (len(pts) - 1)

        def y_of(count: int) -> float:
            return pad_t + h * (1.0 - count / top)

        # Day gridlines, so a fortnight of backlog reads as a fortnight.
        span = pts[-1].at - pts[0].at
        if span > 0:
            pen = QPen(QColor(255, 255, 255, 18))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            step = max(1, round(self._days / 7))
            day = 86400.0
            t = pts[-1].at
            while t > pts[0].at:
                gx = pad_l + w * (t - pts[0].at) / span
                painter.drawLine(int(gx), int(pad_t), int(gx), int(pad_t + h))
                t -= step * day

        for series, metric in ((tuple(p.reviews for p in pts), "pendingReviews"),
                               (tuple(p.conflicts for p in pts), "pendingFixes")):
            if not any(series):
                continue
            color = _tint(metric)
            fill = QPainterPath()
            fill.moveTo(x_of(0), pad_t + h)
            for i, value in enumerate(series):
                fill.lineTo(x_of(i), y_of(value))
            fill.lineTo(x_of(len(series) - 1), pad_t + h)
            fill.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(color, 0.22))
            painter.drawPath(fill)

            line = QPainterPath()
            for i, value in enumerate(series):
                px, py = x_of(i), y_of(value)
                line.moveTo(px, py) if i == 0 else line.lineTo(px, py)
            pen = QPen(QColor(color))
            pen.setWidthF(1.8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawPath(line)

        painter.setPen(QColor(glyphs.MUTED))
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignLeft), _day_label(pts[0].at))
        painter.drawText(QRectF(pad_l + w - 90, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignRight), "now")
        # Peak, on the count axis, so the height means something without a full
        # y-axis eating the width.
        painter.drawText(QRectF(pad_l + 2, pad_t - 1, 60, 12),
                         int(Qt.AlignmentFlag.AlignLeft), f"peak {top}")
        painter.end()


#: Month names, spelled out rather than left to ``strftime("%b")`` — that follows
#: the process locale, so on a machine set to anything but English the chart axis
#: would come out in a different language from every other word on the screen.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day_label(epoch: float) -> str:
    try:
        d = datetime.fromtimestamp(epoch)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{d.day} {_MONTHS[d.month - 1]}"


# MARK: - the screen


class TelemetryView(QWidget):
    """The Telemetry screen. Emits ``done`` when the user is finished, like the
    Mesh and Settings screens."""

    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setStyleSheet("TelemetryView { background: transparent; }")

        model = core.telemetry()
        self._ranges = model["ranges"]
        self._days = float(model["defaultRangeDays"])
        self._steps = int(model["series"]["steps"])
        self._bins = int(model["series"]["bins"])
        self._z = float(model["confidence"]["z"])
        self._ci_title = model["confidence"]["title"]
        self._min_sample = int(model["minSample"])
        self._range_buttons: dict[int, QToolButton] = {}

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        col.addLayout(self._build_header())

        # Empty state, shown until the ledger has anything at all in it.
        self.empty = QLabel(
            "Nothing recorded yet.\n\n"
            "The monitors write to the telemetry ledger as they work: what they "
            "find owed, when an agent takes it, how long it runs and what it cost. "
            "Leave Diplomat running and this fills in."
        )
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(muted(11) + " padding: 40px 24px;")
        col.addWidget(self.empty)

        self.body = QWidget()
        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.cost_host, self.cost_col = card_host()
        left.addWidget(self.cost_host)
        self.tokens_host, self.tokens_col = card_host()
        left.addWidget(self.tokens_host)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        self.pending_host, self.pending_col = card_host()
        right.addWidget(self.pending_host)
        self.timing_host, self.timing_col = card_host()
        right.addWidget(self.timing_host)
        right.addStretch(1)

        body.addLayout(left, 1)
        body.addLayout(right, 1)
        col.addWidget(self.body)

        self.coverage = QLabel("")
        self.coverage.setWordWrap(True)
        self.coverage.setStyleSheet(muted(9))
        col.addWidget(self.coverage)
        col.addStretch(1)

        for host, layout in ((self.cost_host, self.cost_col),
                             (self.tokens_host, self.tokens_col),
                             (self.pending_host, self.pending_col),
                             (self.timing_host, self.timing_col)):
            host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        self.spread_chart = SpreadChart()
        self.pending_chart = PendingChart()

        store.telemetry_changed.connect(self.rebuild)
        self.rebuild()

    # MARK: header

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_TELEMETRY, 16, glyphs.MUTED, font_px=14))
        title = QLabel("Telemetry")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        row.addStretch(1)

        for spec in self._ranges:
            days = int(spec["days"])
            btn = QToolButton()
            btn.setText(spec["title"])
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, d=days: self._set_days(d))
            self._range_buttons[days] = btn
            row.addWidget(btn)
        self._style_range_buttons()

        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _set_days(self, days: int) -> None:
        self._days = float(days)
        self._style_range_buttons()
        self.rebuild()

    def _style_range_buttons(self) -> None:
        for days, btn in self._range_buttons.items():
            active = days == int(self._days)
            btn.setChecked(active)
            btn.setStyleSheet(
                "QToolButton { border: none; font-size: 9px; font-weight: 700;"
                f" padding: 2px 7px; border-radius: 6px;"
                f" color: {'palette(text)' if active else 'palette(mid)'};"
                f" background-color: {tint_bg('#8E8E93', 0.22) if active else 'transparent'}; }}"
            )

    # MARK: rebuild

    def rebuild(self) -> None:
        """Re-fold the ledger and repaint every card. Cheap enough to run on a
        range flip and on every sample: :func:`telemetry.load` caches the fold
        until the file actually changes."""
        ledger = telemetry.load()
        summary = telemetry.summarize(
            ledger, now=time.time(), days=self._days, steps=self._steps,
            bin_count=self._bins, z=self._z)

        has_data = bool(ledger.tasks) or bool(ledger.samples)
        self.empty.setVisible(not has_data)
        self.body.setVisible(has_data)
        self.coverage.setVisible(has_data)
        if not has_data:
            return

        self._rebuild_cost(summary)
        self._rebuild_tokens(summary)
        self._rebuild_pending(summary)
        self._rebuild_timing(summary)
        self._rebuild_coverage(summary)

    # MARK: cards

    def _card_head(self, layout, metric_id: str, value: str, *,
                   caption: str | None = None) -> None:
        """A card's heading row: tinted glyph, title, the headline number on the
        right — the one shape all four cards share."""
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(GlyphLabel(_glyph(metric_id), 15, _tint(metric_id), font_px=13))
        title = QLabel(_title(metric_id).upper())
        title.setStyleSheet(muted(9, bold=True) + " letter-spacing: 1px;")
        row.addWidget(title)
        row.addStretch(1)
        big = QLabel(value)
        big.setStyleSheet(
            f"color: {_tint(metric_id)}; font-weight: 700; font-size: 17px;"
        )
        row.addWidget(big)
        layout.addLayout(row)
        if caption:
            cap = QLabel(caption)
            cap.setWordWrap(True)
            cap.setStyleSheet(muted(9))
            layout.addWidget(cap)

    def _rebuild_cost(self, s: telemetry.Summary) -> None:
        _clear_layout(self.cost_col)
        d = s.per_task
        priced = s.session_limit_tokens is not None and d.count > 0

        if priced:
            headline = telemetry.percent(d.mean)
            caption = (f"of the 5-hour window, per task · median "
                       f"{telemetry.percent(d.median)} · "
                       f"{telemetry.percent(s.per_task_week_mean)} of the week")
        elif d.count == 0 and s.per_task_tokens_mean > 0:
            headline = telemetry.tokens(s.per_task_tokens_mean)
            caption = ("tokens per task. The share of the limit needs two quota "
                       "readings from the OAuth usage probe — is Claude Code "
                       "logged in on this machine?")
        else:
            headline = "—"
            caption = "No finished auto-task in this range yet."
        self._card_head(self.cost_col, "limitPerTask", headline, caption=caption)

        if not priced:
            return

        self.cost_col.addWidget(self.spread_chart)
        self.spread_chart.set_distribution(d)

        stats = QLabel(
            f"{self._ci_title} {telemetry.percent(d.ci_low)} – "
            f"{telemetry.percent(d.ci_high)}  ·  sd {telemetry.percent(d.sd)}  ·  "
            f"n={d.count}"
        )
        stats.setStyleSheet(muted(9, mono=True))
        self.cost_col.addWidget(stats)

        if d.count < self._min_sample:
            warn = QLabel(
                f"Only {d.count} finished task{'' if d.count == 1 else 's'} — the "
                f"curve is a guess until there are {self._min_sample}."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #FF9500; font-size: 9px;")
            self.cost_col.addWidget(warn)

        note = QLabel(_blurb("limitSpread"))
        note.setWordWrap(True)
        note.setStyleSheet(muted(9))
        self.cost_col.addWidget(note)

    def _rebuild_tokens(self, s: telemetry.Summary) -> None:
        _clear_layout(self.tokens_col)
        total = s.repo_tokens + s.other_tokens
        headline = telemetry.percent(s.repo_share_pct) if total > 0 else "—"
        self._card_head(self.tokens_col, "tokenShare", headline,
                        caption=_blurb("tokenShare"))
        if total <= 0:
            hint = QLabel("No Claude turns recorded in this range.")
            hint.setStyleSheet(muted(9))
            self.tokens_col.addWidget(hint)
            return

        bar = _SplitBar(s.repo_tokens, s.other_tokens,
                       _tint("tokenShare"), "#8E8E93")
        self.tokens_col.addWidget(bar)

        self.tokens_col.addLayout(_legend([
            (_tint("tokenShare"), f"this repo {telemetry.tokens(s.repo_tokens)}"),
            ("#8E8E93", f"everything else {telemetry.tokens(s.other_tokens)}"),
        ]))

    def _rebuild_pending(self, s: telemetry.Summary) -> None:
        _clear_layout(self.pending_col)
        self._card_head(
            self.pending_col, "pendingWork",
            f"{s.pending_reviews_now} / {s.pending_conflicts_now}",
            caption=f"owed right now: {_title('pendingReviews').lower()} / "
                    f"{_title('pendingFixes').lower()}")

        self.pending_col.addWidget(self.pending_chart)
        self.pending_chart.set_series(s.pending, self._days)

        self.pending_col.addLayout(_legend([
            (_tint("pendingReviews"),
             f"{_title('pendingReviews').lower()} (peak {s.peak_reviews})"),
            (_tint("pendingFixes"),
             f"{_title('pendingFixes').lower()} (peak {s.peak_conflicts})"),
        ]))

        found = QLabel(
            f"{s.queued_count} unit{'' if s.queued_count == 1 else 's'} of work found "
            f"in this range, {s.started_count} started"
            + (f", {s.remote_count} on mesh peers" if s.remote_count else "")
            + ". Work picked up between two points on the chart never shows as a "
              "backlog — that is the chart working, not a gap."
        )
        found.setWordWrap(True)
        found.setStyleSheet(muted(9))
        self.pending_col.addWidget(found)

    def _rebuild_timing(self, s: telemetry.Summary) -> None:
        _clear_layout(self.timing_col)
        self._card_head(self.timing_col, "startLag",
                        telemetry.duration(s.avg_wait_secs, samples=s.wait_samples),
                        caption=_blurb("startLag"))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(GlyphLabel(_glyph("completeTime"), 15,
                                 _tint("completeTime"), font_px=13))
        label = QLabel(_title("completeTime").upper())
        label.setStyleSheet(muted(9, bold=True) + " letter-spacing: 1px;")
        row.addWidget(label)
        row.addStretch(1)
        value = QLabel(telemetry.duration(s.avg_run_secs, samples=s.run_samples))
        value.setStyleSheet(
            f"color: {_tint('completeTime')}; font-weight: 700; font-size: 17px;"
        )
        row.addWidget(value)
        self.timing_col.addLayout(row)

        note = QLabel(
            f"{_blurb('completeTime')}  Measured over {s.run_samples} finished and "
            f"{s.wait_samples} started task{'' if s.wait_samples == 1 else 's'}."
        )
        note.setWordWrap(True)
        note.setStyleSheet(muted(9))
        self.timing_col.addWidget(note)

    def _rebuild_coverage(self, s: telemetry.Summary) -> None:
        parts: list[str] = []
        if s.first_sample_at:
            parts.append(f"quota readings since {_day_label(s.first_sample_at)}")
        if s.session_limit_tokens:
            parts.append(
                f"5-hour window priced at ≈{telemetry.tokens(s.session_limit_tokens)} "
                "tokens, measured"
            )
        if s.unattributed_count:
            parts.append(
                f"{s.unattributed_count} finished task"
                f"{'' if s.unattributed_count == 1 else 's'} could not be matched to "
                "a transcript, so they carry no cost"
            )
        self.coverage.setText(" · ".join(parts))


class _SplitBar(QWidget):
    """A single horizontal bar split between two quantities — the Diplomat share of
    this machine's tokens against everything else."""

    def __init__(self, left: float, right: float,
                 left_color: str, right_color: str) -> None:
        super().__init__()
        self.setFixedHeight(18)
        self._left = max(0.0, left)
        self._right = max(0.0, right)
        self._left_color = left_color
        self._right_color = right_color

    def paintEvent(self, event) -> None:  # noqa: N802
        total = self._left + self._right
        if total <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        box = QRectF(self.rect())
        split = box.width() * self._left / total
        path = QPainterPath()
        path.addRoundedRect(box, 5, 5)
        painter.setClipPath(path)
        painter.fillRect(QRectF(0, 0, split, box.height()), QColor(self._left_color))
        painter.fillRect(QRectF(split, 0, box.width() - split, box.height()),
                         _qcolor(self._right_color, 0.45))
        painter.end()
