"""The Telemetry screen — what the monitors cost and what they still owe.

The Linux face of the ledger (:mod:`telemetry`), and one of the panel's four
screens: Actions · Mesh · **Telemetry** · Settings. It reads
``~/.diplomat/pr-monitor/telemetry.jsonl``, folds it through the shared
arithmetic, and draws eight figures:

* what share of the 5-hour rate-limit window one auto-task consumes, on average;
* how that share is distributed against BOTH rate-limit windows, as two histograms
  on one axis, each with a fitted normal and a confidence interval on its mean;
* what the probe measured to be left of each rate-limit window, over the lookback;
* how many auto-reviews were owed but unstarted, over the lookback;
* the same for auto-fixes;
* mean time from an agent starting to its exit;
* mean time from the monitor first seeing the work to an agent taking it;
* how much of this machine's Claude spend went on this repo rather than
  everything else.

Read-only: the one control is the lookback, and flipping it recomputes from the
same fold. The three charts are painted rather than assembled from widgets — a
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

from diplomat_runtime import core, telemetry
from . import glyphs
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
    """Empty a card so its rebuild can refill it. Everything inside is destroyed,
    charts included — which is why each ``_rebuild_*`` builds its own chart rather
    than re-adding one held on the view. A held one survives the first refresh (Qt
    defers the destruction to the next event-loop turn) and is a dangling wrapper by
    the second."""
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
    """The bell curves: the same auto-tasks as a histogram of what they cost each
    rate-limit window, the fitted normal over each, and the confidence interval on
    each mean as a shaded band.

    Both windows on one x axis, because the whole question the card answers is
    *which ceiling this work is really spending against* — and that is the distance
    between the two humps. A task is a large slice of a 5-hour window and a sliver
    of a week, so the weekly series sits far to the left; the axis is honest about
    that rather than giving each series a scale of its own.

    The bars are grouped inside each bin, red then yellow, rather than stacked or
    overlaid: near zero the two series share bins, and a translucent overlay there
    would hide whichever was drawn first. Each series is scaled to its own tallest
    bar — see :func:`top_of`.

    Each band is deliberately drawn *behind* the bars, with the mean as a rule, so
    the eye reads "the average is here, and this is how well we know it" rather
    than mistaking the interval for the spread of the tasks themselves — which is
    the histogram, and is much wider.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(150)
        self._series: list[tuple[telemetry.Distribution, str]] = []

    def set_distributions(self, session: telemetry.Distribution,
                          week: telemetry.Distribution) -> None:
        """The 5-hour window first, so it is the one drawn in the left half of each
        bin — the same order the stats lines under it are listed in. A window with no
        price yet has no series."""
        self._series = [(d, _tint(mid)) for d, mid in
                        ((session, "spreadSession"), (week, "spreadWeek"))
                        if d.count > 0 and d.bins]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        drawn = self._series
        if not drawn:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        pad_l, pad_r, pad_t, pad_b = 4.0, 4.0, 8.0, 16.0
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b
        # The two share bin edges (summarize spans them together), so either names
        # the axis.
        hi = drawn[0][0].bins[-1].upper or 1.0

        def x_of(value: float) -> float:
            return pad_l + w * min(1.0, max(0.0, value / hi))

        def top_of(d: telemetry.Distribution) -> float:
            """One series' full height. Each is scaled to its OWN peak, not to a
            count axis shared with the other: both hold the same tasks, so a bin is a
            share of the same population either way — and shared, the wider spread is
            drawn as a smear along the floor of the narrower one's spike. The curve's
            peak can exceed the tallest bar (a tight distribution sampled into wide
            bins), so it is in the scale too or the fit would be clipped where it
            matters most."""
            return max([1.0] + [float(b.count) for b in d.bins] + list(d.curve))

        def y_of(count: float, top: float) -> float:
            return pad_t + h * (1.0 - min(1.0, count / top))

        # Confidence bands on the means, behind everything else so they read as
        # context for the mean rules rather than as more series.
        for d, tint in drawn:
            if d.ci_high > d.ci_low:
                lo, hi_ci = x_of(d.ci_low), x_of(d.ci_high)
                painter.fillRect(QRectF(lo, pad_t, max(1.0, hi_ci - lo), h),
                                 _qcolor(tint, 0.16))

        # Histogram bars, each series in its own half of every bin.
        painter.setPen(Qt.PenStyle.NoPen)
        for slot, (d, tint) in enumerate(drawn):
            painter.setBrush(_qcolor(tint, 0.55))
            top = top_of(d)
            for b in d.bins:
                if b.count <= 0:
                    continue
                x0, x1 = x_of(b.lower), x_of(b.upper)
                share = (x1 - x0) / len(drawn)
                y = y_of(b.count, top)
                painter.drawRoundedRect(
                    QRectF(x0 + share * slot + 0.8, y, max(1.0, share - 1.6),
                           pad_t + h - y), 2, 2)

        for d, tint in drawn:
            # The fitted normal.
            if len(d.curve) > 1:
                top = top_of(d)
                path = QPainterPath()
                for i, value in enumerate(d.curve):
                    px = pad_l + w * i / (len(d.curve) - 1)
                    py = y_of(value, top)
                    if i == 0:
                        path.moveTo(px, py)
                    else:
                        path.lineTo(px, py)
                pen = QPen(QColor(tint))
                pen.setWidthF(1.8)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(pen)
                painter.drawPath(path)

            # The mean, as a full-height rule, in the series' own colour: with two
            # of them on the axis, a white one would match neither.
            mean_x = x_of(d.mean)
            pen = QPen(QColor(tint))
            pen.setWidthF(1.2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(mean_x), int(pad_t), int(mean_x), int(pad_t + h))

        # Axis: 0 on the left, the top of the shared span on the right.
        painter.setPen(QColor(glyphs.MUTED))
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 60, 12),
                         int(Qt.AlignmentFlag.AlignLeft), "0%")
        painter.drawText(QRectF(pad_l + w - 60, pad_t + h + 2, 60, 12),
                         int(Qt.AlignmentFlag.AlignRight), telemetry.percent(hi))
        painter.end()


class PendingChart(QWidget):
    """Owed-but-unstarted work over the lookback: reviews and conflict fixes stacked
    into one area on a count axis with day gridlines.

    Stacked rather than overlaid because the two kinds of work queue for the same
    executors — the top edge is the whole backlog those executors owe, which is the
    number that decides whether anything waits. Reviews are the lower band: they
    outrank conflict fixes for a free slot (:func:`autofix.queue_band`), so the band
    above is exactly the work waiting behind the band below.
    """

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
        peak = max(p.reviews + p.conflicts for p in pts)
        top = max(1, peak)

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

        base = [0] * len(pts)
        for series, metric in ((tuple(p.reviews for p in pts), "pendingReviews"),
                               (tuple(p.conflicts for p in pts), "pendingFixes")):
            stacked = [b + v for b, v in zip(base, series)]
            if any(series):
                color = _tint(metric)
                # The band between the running total below it and its own top — not a
                # shape from the axis up, which would bury the band under it at any
                # alpha.
                fill = QPainterPath()
                fill.moveTo(x_of(0), y_of(base[0]))
                for i, value in enumerate(stacked):
                    fill.lineTo(x_of(i), y_of(value))
                for i in range(len(base) - 1, -1, -1):
                    fill.lineTo(x_of(i), y_of(base[i]))
                fill.closeSubpath()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_qcolor(color, 0.22))
                painter.drawPath(fill)

                line = QPainterPath()
                for i, value in enumerate(stacked):
                    px, py = x_of(i), y_of(value)
                    line.moveTo(px, py) if i == 0 else line.lineTo(px, py)
                pen = QPen(QColor(color))
                pen.setWidthF(1.8)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(pen)
                painter.drawPath(line)
            base = stacked

        painter.setPen(QColor(glyphs.MUTED))
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignLeft), _day_label(pts[0].at))
        painter.drawText(QRectF(pad_l + w - 90, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignRight), "now")
        # Peak, on the count axis, so the height means something without a full
        # y-axis eating the width. It is the peak of the stack — the most ever owed
        # at one moment — which the per-series peaks in the key need not add up to.
        # A range that never owed anything says nothing rather than reporting the
        # floor the axis is held at.
        if peak:
            painter.drawText(QRectF(pad_l + 2, pad_t - 1, 90, 12),
                             int(Qt.AlignmentFlag.AlignLeft), f"peak {peak} owed")
        painter.end()


#: How long a silence the quota chart draws through rather than breaks at. Isolated
#: missing readings are normal, and cutting at each one turns a fortnight into specks.
#: The same bound the headline percentage trusts a reading for, so a line that has
#: broken is never captioned with a figure.
_BRIDGE_SECS = telemetry.QUOTA_FRESH_SECS


class QuotaChart(QWidget):
    """Both rate-limit windows over the lookback, on a fixed 0-100% axis.

    Nothing here is derived: these are the readings the OAuth usage probe returned,
    drawn where they were taken. The axis is pinned to 0-100 rather than scaled to
    the data, because "we never dropped below 60%" is the answer the chart exists to
    give, and an auto-scaled one would show that week and a week of exhaustion as
    the same picture.

    The 5-hour window is drawn as a fill (it saws — it refills on its own cycle, so
    the shape matters more than any one value) and the 7-day as a line over it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(120)
        self._points: tuple[telemetry.QuotaPoint, ...] = ()
        self._days = 14.0
        self._now = 0.0

    def set_series(self, points: tuple[telemetry.QuotaPoint, ...], days: float,
                   now: float) -> None:
        """``days`` and ``now`` are the axis, not the readings: it spans the whole
        lookback rather than the span of what was sampled, so it lines up with the
        owed-work chart beside it and a probe that stopped answering three days ago
        leaves visible empty axis instead of a line that appears to reach now."""
        self._points = points
        self._days = days
        self._now = now
        self.update()

    @staticmethod
    def _runs(points, value_of) -> list[list[tuple[float, float]]]:
        """Split the readings into runs, cutting wherever the probe stayed silent for
        longer than :data:`_BRIDGE_SECS`.

        A missing reading is not a zero — it is a probe that could not answer — so the
        line must never dive to the floor and back, which would read as an exhausted
        window that recovered. Short silences are drawn through instead: the remaining
        share is a continuous quantity, and joining the readings either side of one
        missed sample says what happened more nearly than stopping there does.
        """
        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for p in points:
            value = value_of(p)
            if value is None:
                continue
            if current and p.at - current[-1][0] > _BRIDGE_SECS:
                runs.append(current)
                current = []
            current.append((p.at, value))
        if current:
            runs.append(current)
        return runs

    def paintEvent(self, event) -> None:  # noqa: N802
        pts = self._points
        span = self._days * 86400
        if len(pts) < 2 or span <= 0:
            return
        start = self._now - span
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        pad_l, pad_r, pad_t, pad_b = 4.0, 4.0, 8.0, 16.0
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b

        def x_of(at: float) -> float:
            return pad_l + w * (at - start) / span

        def y_of(pct: float) -> float:
            return pad_t + h * (1.0 - min(100.0, max(0.0, pct)) / 100.0)

        def x_end(run) -> float:
            """Where a run's drawing stops. A run narrower than a pixel — a lone
            reading, or a few taken minutes apart on a fortnight's axis — is widened to
            one, since a zero-width shape draws as nothing at all."""
            return max(x_of(run[-1][0]), x_of(run[0][0]) + 1.0)

        # The half-way rule, so a glance can place a run against "half spent".
        pen = QPen(QColor(255, 255, 255, 18))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(int(pad_l), int(y_of(50)), int(pad_l + w), int(y_of(50)))

        for run in self._runs(pts, lambda p: p.session_pct):
            fill = QPainterPath()
            fill.moveTo(x_of(run[0][0]), pad_t + h)
            for at, pct in run:
                fill.lineTo(x_of(at), y_of(pct))
            fill.lineTo(x_end(run), y_of(run[-1][1]))
            fill.lineTo(x_end(run), pad_t + h)
            fill.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(_tint("quotaLeft"), 0.30))
            painter.drawPath(fill)

        for run in self._runs(pts, lambda p: p.week_pct):
            line = QPainterPath()
            for i, (at, pct) in enumerate(run):
                px, py = x_of(at), y_of(pct)
                line.moveTo(px, py) if i == 0 else line.lineTo(px, py)
            line.lineTo(x_end(run), y_of(run[-1][1]))
            pen = QPen(QColor(_tint("quotaWeek")))
            pen.setWidthF(1.8)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawPath(line)

        painter.setPen(QColor(glyphs.MUTED))
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        painter.drawText(QRectF(pad_l, pad_t - 1, 40, 12),
                         int(Qt.AlignmentFlag.AlignLeft), "100%")
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignLeft), _day_label(start))
        painter.drawText(QRectF(pad_l + w - 90, pad_t + h + 2, 90, 12),
                         int(Qt.AlignmentFlag.AlignRight), "now")
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
        self.quota_host, self.quota_col = card_host()
        left.addWidget(self.quota_host)
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
                             (self.quota_host, self.quota_col),
                             (self.tokens_host, self.tokens_col),
                             (self.pending_host, self.pending_col),
                             (self.timing_host, self.timing_col)):
            host.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

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
        now = time.time()
        summary = telemetry.summarize(
            ledger, now=now, days=self._days, steps=self._steps,
            bin_count=self._bins, z=self._z)

        has_data = bool(ledger.tasks) or bool(ledger.samples)
        self.empty.setVisible(not has_data)
        self.body.setVisible(has_data)
        self.coverage.setVisible(has_data)
        if not has_data:
            return

        self._rebuild_cost(summary)
        self._rebuild_quota(summary, now)
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
        week = s.per_task_week
        priced = s.session_limit_tokens is not None and d.count > 0

        if priced:
            headline = telemetry.percent(d.mean)
            caption = (f"of the 5-hour window, per task · median "
                       f"{telemetry.percent(d.median)}")
            if week.count:
                caption += f" · {telemetry.percent(week.mean)} of the 7-day window"
        elif d.count == 0 and s.per_task_tokens_mean > 0:
            headline = telemetry.tokens(s.per_task_tokens_mean)
            caption = ("tokens per task. The share of the 5-hour window is Claude "
                       "Code's own — it counts only tasks that ran on it, and needs "
                       "two quota readings from the OAuth usage probe.")
        else:
            headline = "—"
            caption = "No finished auto-task in this range yet."
        self._card_head(self.cost_col, "limitPerTask", headline, caption=caption)

        # Whichever windows the ledger priced — not always the 5-hour one, which
        # resets on its own cycle, so a ledger whose samples straddle a reset prices
        # only the week. The dispatch gate holds work against that weekly figure
        # (:func:`autobudget._costs`), so drawing nothing here would read "unpriced"
        # over a measurement.
        series = [(dist, mid) for dist, mid in
                  ((d, "spreadSession"), (week, "spreadWeek")) if dist.count]
        if not series:
            return

        chart = SpreadChart()
        self.cost_col.addWidget(chart)
        chart.set_distributions(d, week)

        # One line per window, in that window's colour — the chart has no other key.
        for dist, metric_id in series:
            line = QLabel(
                f"◼ {_title(metric_id)}  {telemetry.percent(dist.mean)}  ·  "
                f"{self._ci_title} {telemetry.percent(dist.ci_low)} – "
                f"{telemetry.percent(dist.ci_high)}  ·  "
                f"sd {telemetry.percent(dist.sd)}  ·  n={dist.count}"
            )
            line.setStyleSheet(
                f"color: {_tint(metric_id)}; font-size: 9px; font-family: monospace;")
            self.cost_col.addWidget(line)

        if not week.count:
            unpriced = QLabel(
                "The 7-day window has no price yet — it moves slowly, so it takes "
                "longer than the 5-hour one to measure a task against."
            )
            unpriced.setWordWrap(True)
            unpriced.setStyleSheet(muted(9))
            self.cost_col.addWidget(unpriced)

        # Both series hold the same tasks, so whichever was priced is the sample.
        shown = max(d.count, week.count)
        if shown < self._min_sample:
            warn = QLabel(
                f"Only {shown} finished task{'' if shown == 1 else 's'} — the "
                f"curve is a guess until there are {self._min_sample}."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #FF9500; font-size: 9px;")
            self.cost_col.addWidget(warn)

        note = QLabel(_blurb("limitSpread"))
        note.setWordWrap(True)
        note.setStyleSheet(muted(9))
        self.cost_col.addWidget(note)

    def _rebuild_quota(self, s: telemetry.Summary, now: float) -> None:
        """The one figure on this screen that is measured rather than derived: what
        the usage probe says is left of each rate-limit window, drawn as it was
        sampled."""
        _clear_layout(self.quota_col)
        left = s.session_left_pct
        self._card_head(
            self.quota_col, "quotaLeft",
            telemetry.percent(left) if left is not None else "—",
            caption=_blurb("quotaLeft"))
        if not s.quota:
            hint = QLabel(
                "No quota readings in this range. The probe uses the OAuth token "
                "Claude Code already holds — is it logged in on this machine?"
            )
            hint.setWordWrap(True)
            hint.setStyleSheet(muted(9))
            self.quota_col.addWidget(hint)
            return

        # Two readings are the fewest that make a line; one is drawn as the
        # headline alone rather than as a 120px empty box.
        if len(s.quota) > 1:
            chart = QuotaChart()
            self.quota_col.addWidget(chart)
            chart.set_series(s.quota, self._days, now)

        week = s.week_left_pct
        self.quota_col.addLayout(_legend([
            (_tint("quotaLeft"),
             f"5-hour {telemetry.percent(left) if left is not None else '—'}"),
            (_tint("quotaWeek"),
             f"{_title('quotaWeek')} "
             + (telemetry.percent(week) if week is not None else "—")),
        ]))

        # A gap is the probe failing to answer, not the window emptying. The chart
        # draws through a short one and breaks across a long one; saying how many
        # keeps a blind stretch from reading as a quiet one.
        gaps = sum(1 for q in s.quota if q.session_pct is None)
        if gaps:
            note = QLabel(
                f"{gaps} reading{'' if gaps == 1 else 's'} missing of "
                f"{len(s.quota)} — the probe could not answer then. A short silence "
                f"is drawn through, a long one breaks the line; neither drops it to "
                f"zero."
            )
            note.setWordWrap(True)
            note.setStyleSheet(muted(9))
            self.quota_col.addWidget(note)

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

        chart = PendingChart()
        self.pending_col.addWidget(chart)
        chart.set_series(s.pending, self._days)

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
            + ". Fixes stack on top of reviews, which take a free slot first, so the "
              "top edge is everything the pool owes. Work picked up between two "
              "points on the chart never shows as a backlog — that is the chart "
              "working, not a gap."
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
        if s.week_limit_tokens:
            parts.append(
                f"7-day window at ≈{telemetry.tokens(s.week_limit_tokens)} tokens"
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
