"""What the Telemetry screen says about a given ledger.

The maths behind the figures is pinned across platforms by
``test_telemetry_parity.py``; the ledger's IO by ``test_telemetry.py``. What is
left, and what these cover, is the screen's own judgement — the places where it
decides what a number *means* rather than what it is:

* an empty ledger must not draw four cards of zeroes and dashes as if that were a
  measurement;
* a machine whose quota probe never answered has token counts but no way to turn
  them into a share of the limit, and must say so instead of inventing a
  percentage;
* a small sample must be labelled as one, because a bell curve drawn through four
  points looks exactly as authoritative as one drawn through four hundred;
* a probe that has been silent for an hour must not blank a rate-limit figure it
  measured perfectly well an hour ago.

Every test drives the real widget under the offscreen Qt platform, over a ledger
written through the real recorder into the per-test temp dir.
"""

from __future__ import annotations

import time

import pytest

from diplomat_runtime import core, telemetry
from diplomat_app.store import Store

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(app):
    s = Store()
    s.me = "latekvo"
    s.has_loaded = True
    return s


def _view(store):
    from diplomat_app.telemetryview import TelemetryView

    return TelemetryView(store)


def _labels(widget) -> list[str]:
    """Every piece of text the screen is currently showing."""
    from PySide6.QtWidgets import QLabel

    return [lbl.text() for lbl in widget.findChildren(QLabel)]


def _seed(*, samples: bool = True, tasks: int = 8, priced: bool = True,
          week_moves: bool = True, session_moves: bool = True) -> None:
    """A ledger with `tasks` finished local tasks. ``priced`` decides whether the
    samples carry quota readings — i.e. whether either window can be priced at all —
    and ``samples`` whether there are any samples to begin with.

    ``week_moves`` off holds the weekly reading still, which is what a quiet
    account's ledger looks like: the 5-hour window is measurable and the week isn't.
    ``session_moves`` off is the other way round — the 5-hour reading RISES between
    every pair of samples, as it does on a ledger whose samples all straddle one of
    its resets, so it prices nothing while the week prices fine.

    Priced, the readings make the 5-hour window worth 2.5M tokens and the week 25M,
    so the default eight tasks (40k-61k tokens) cost 1.6-2.44% of one and
    0.16-0.24% of the other."""
    now = time.time()
    if samples:
        for i in range(4):
            session = (1.0 - 0.2 * i) if session_moves else (0.4 + 0.2 * i)
            telemetry.append({
                "at": now - (4 - i) * 3600, "ev": "sample",
                "sessionLeft": session if priced else None,
                "weekLeft": (1.0 - 0.02 * i if week_moves else 1.0) if priced else None,
                "repoTokens": 400_000.0 * i, "otherTokens": 100_000.0 * i,
            })
    for i in range(tasks):
        key = f"review:h/o/r#{i}@aa{i}"
        at = now - 6 * 3600 + i * 60
        telemetry.append({"at": at, "ev": "queued", "key": key,
                          "duty": "review", "pr": i})
        telemetry.append({"at": at + 120, "ev": "started", "key": key,
                          "remote": False, "attempt": 1})
        telemetry.append({"at": at + 900, "ev": "done", "key": key,
                          "tokens": 40_000.0 + 3_000.0 * i})
    telemetry._reset_cache()


#: The series colours, read from the shared model rather than restated here — the
#: palette is the one thing the two platforms' charts must agree on.
_TINT = {m["id"]: m["colorHex"].lower() for m in core.telemetry()["metrics"]}


def _chart_colours(view) -> set[str]:
    """Every colour the spread chart actually paints. The stats lines below it are
    built from the summary separately, so they would still read correctly if the
    chart itself drew one window and dropped the other."""
    from diplomat_app.telemetryview import SpreadChart

    chart = view.findChild(SpreadChart)
    assert chart is not None, "the cost card drew no spread chart"
    chart.resize(400, 150)  # the view is never shown, so nothing else sizes it
    image = chart.grab().toImage()
    return {image.pixelColor(x, y).name()
            for y in range(image.height()) for x in range(image.width())}


def _spread_lines(view) -> list[str]:
    """The cost card's per-window figures — the only labels carrying an interval."""
    return [t for t in _labels(view) if "95% CI" in t]


def test_an_empty_ledger_says_so_instead_of_drawing_zeroes(store):
    view = _view(store)
    assert view.empty.isVisibleTo(view)
    assert not view.body.isVisibleTo(view)
    assert not view.coverage.isVisibleTo(view)


def test_a_ledger_with_work_draws_the_cards(store):
    _seed()
    view = _view(store)
    assert view.body.isVisibleTo(view)
    assert not view.empty.isVisibleTo(view)


def test_the_share_of_the_limit_is_shown_once_the_window_has_a_price(store):
    _seed()
    view = _view(store)
    text = "\n".join(_labels(view))
    assert "of the 5-hour window, per task" in text
    assert "5-hour window priced at" in text, "the coverage line hid the calibration"
    assert "7-day window at" in text, "the coverage line quoted one window of two"
    assert "95% CI" in text


def test_the_chart_paints_one_series_per_priced_window(store):
    """Red for the 5-hour window, yellow for the week, both on the one axis. The
    fitted normal and the mean rule are stroked at full opacity, so each series'
    tint lands in the image exactly."""
    _seed()
    view = _view(store)
    colours = _chart_colours(view)
    assert _TINT["spreadSession"] in colours, "the 5-hour series was not drawn"
    assert _TINT["spreadWeek"] in colours, "the weekly series was not drawn"


def test_the_chart_paints_nothing_for_a_window_with_no_price(store):
    _seed(week_moves=False)
    colours = _chart_colours(_view(store))
    assert _TINT["spreadSession"] in colours
    assert _TINT["spreadWeek"] not in colours, (
        "a window the ledger cannot price was drawn as a series at zero"
    )


def test_the_spread_measures_each_window_separately(store):
    """The chart draws both, so both need their own figures under it: a mean and an
    interval per window, in the order the bars are grouped. A screen that rescaled
    one series from the other would print the same spread twice."""
    _seed()
    view = _view(store)
    lines = _spread_lines(view)
    assert len(lines) == 2, "a window was drawn without figures of its own"
    assert lines[0].startswith("◼ 5-hour  2.0%"), "50.5k of a 2.5M window"
    assert lines[1].startswith("◼ 7-day  0.20%"), "the same 50.5k of a 25M week"
    assert "0.20% of the 7-day window" in "\n".join(_labels(view))


def test_a_week_the_samples_never_priced_says_so_instead_of_drawing_zero(store):
    """The weekly window barely moves, so a quiet fortnight can price the 5-hour one
    and not the week. Its series is absent rather than flat at zero, which would
    read as "a task costs the week nothing"."""
    _seed(week_moves=False)
    view = _view(store)
    lines = _spread_lines(view)
    assert len(lines) == 1 and lines[0].startswith("◼ 5-hour")
    text = "\n".join(_labels(view))
    assert "The 7-day window has no price yet" in text
    assert "of the 7-day window" not in text, "a figure was printed for it anyway"


def test_a_session_the_samples_never_priced_still_draws_the_week(store):
    """The mirror of the case above, and the one the dispatch gate acts on: the
    5-hour window resets on its own cycle, so a ledger whose samples all straddle a
    reset prices only the week. Drawing nothing here would say "unpriced" on a
    screen whose figures the gate is already holding work against."""
    _seed(session_moves=False)
    view = _view(store)
    lines = _spread_lines(view)
    assert len(lines) == 1 and lines[0].startswith("◼ 7-day  0.20%"), (
        "the one window the ledger could price was left off the card"
    )
    colours = _chart_colours(view)
    assert _TINT["spreadWeek"] in colours, "the priced window was not drawn"
    assert _TINT["spreadSession"] not in colours
    text = "\n".join(_labels(view))
    # The headline is still the 5-hour window's, and it has no price — so it falls
    # back to raw tokens rather than borrowing the week's percentage.
    assert "tokens per task" in text
    assert "of the 5-hour window, per task" not in text
    assert "The 7-day window has no price yet" not in text


def test_without_two_quota_readings_it_reports_tokens_not_a_percentage(store):
    """Anthropic publishes a utilization percentage and never a token budget, so a
    machine whose probe never answered cannot honestly convert a task's tokens into
    a share of the limit. It shows the tokens and says what is missing — the one
    thing it must not do is print a made-up percentage."""
    _seed(priced=False)
    view = _view(store)
    text = "\n".join(_labels(view))
    assert "tokens per task" in text
    assert "needs two quota readings" in text
    assert "of the 5-hour window, per task" not in text
    assert "5-hour window priced at" not in text
    assert "7-day window at" not in text


def test_the_rate_limit_card_shows_the_latest_reading_of_each_window(store):
    """The quota percentages are measured, not derived — the probe reports them
    every sample — so this card is the one that stays truthful on a machine whose
    window was never priced."""
    now = time.time()
    telemetry.append({"at": now - 1800, "ev": "sample", "sessionLeft": 0.60,
                      "weekLeft": 0.95, "repoTokens": 1000.0, "otherTokens": 1000.0})
    telemetry.append({"at": now - 300, "ev": "sample", "sessionLeft": 0.42,
                      "weekLeft": 0.91, "repoTokens": 2000.0, "otherTokens": 2000.0})
    telemetry._reset_cache()
    view = _view(store)
    text = "\n".join(_labels(view))
    assert "5-hour 42.0%" in text
    assert "7-day 91.0%" in text


def test_a_silent_probe_keeps_the_last_reading_it_did_take(store):
    """A missing reading is the probe failing to answer, not the window emptying.
    Blanking the figure — or worse, drawing a plunge to zero — would report an
    exhaustion that never happened."""
    now = time.time()
    telemetry.append({"at": now - 1800, "ev": "sample", "sessionLeft": 0.42,
                      "weekLeft": 0.91, "repoTokens": 1000.0, "otherTokens": 1000.0})
    telemetry.append({"at": now - 300, "ev": "sample", "sessionLeft": None,
                      "weekLeft": None, "repoTokens": 1000.0, "otherTokens": 1000.0})
    telemetry._reset_cache()
    view = _view(store)
    text = "\n".join(_labels(view))
    assert "5-hour 42.0%" in text
    assert "1 reading missing" in text


def test_a_thin_sample_is_labelled_as_one(store):
    """A bell curve through three points looks exactly as authoritative as one
    through three hundred, which is the whole reason for the warning."""
    _seed(tasks=3)
    view = _view(store)
    assert any("the curve is a guess" in t for t in _labels(view))


def test_the_thin_sample_warning_counts_the_window_that_was_drawn(store):
    """With only the week priced the 5-hour series is empty, and reading ITS count
    would warn about "0 finished tasks" on a card drawing three of them."""
    _seed(tasks=3, session_moves=False)
    view = _view(store)
    assert any("Only 3 finished tasks" in t for t in _labels(view))


def test_a_full_sample_carries_no_warning(store):
    _seed(tasks=8)
    view = _view(store)
    assert not any("the curve is a guess" in t for t in _labels(view))


def test_the_lookback_changes_what_is_counted(store):
    """The range buttons are the screen's only control, and they must actually
    re-scope the figures rather than just re-style themselves."""
    now = time.time()
    for days, i in ((2, 1), (20, 2)):
        key = f"review:h/o/r#{i}@bb{i}"
        at = now - days * 86400
        telemetry.append({"at": at, "ev": "queued", "key": key,
                          "duty": "review", "pr": i})
        telemetry.append({"at": at + 60, "ev": "started", "key": key,
                          "remote": False, "attempt": 1})
        telemetry.append({"at": at + 660, "ev": "done", "key": key,
                          "tokens": 50_000.0})
    telemetry._reset_cache()

    view = _view(store)
    view._set_days(7)
    assert any("1 started" in t for t in _labels(view))
    view._set_days(30)
    assert any("2 started" in t for t in _labels(view))


def test_flipping_the_lookback_leaves_exactly_one_live_chart_per_card(store):
    """Rebuilding a card destroys everything in it, charts included — Qt just defers
    the destruction to the next event-loop turn. A chart held on the view therefore
    survives the first refresh and is a dangling C++ wrapper by the second, so each
    rebuild has to make its own."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QWidget

    _seed()
    view = _view(store)
    for days in (7, 30, 14, 60):
        view._set_days(days)
        # Qt flushes deferred deletes only when the event loop turns, which is what
        # turns a re-added chart into a dangling wrapper; force it here.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        alive = sorted(type(w).__name__ for w in view.findChildren(QWidget)
                       if type(w).__name__.endswith("Chart"))
        assert alive == ["PendingChart", "QuotaChart", "SpreadChart"], (
            f"at {days}d the cards held {alive}")


def _session_fill_columns(chart, width: int = 400, height: int = 120) -> list[int]:
    """The x columns where the 5-hour window's fill was actually painted.

    Picked out by hue AND by where it is looked for. The 30%-alpha orange fill lifts
    red over blue by ~74 whatever grey it lands on (blending is linear, and a grey
    background contributes nothing to the difference), while the half-way rule is
    white and the 7-day line purple, both of which have blue at or above red.

    Hue alone is not enough, though: a font stack that antialiases with LCD subpixel
    filtering fringes its glyph edges red on one side and blue on the other, which
    reads as a weak orange — so the axis labels would count as fill on some machines
    and not others. The band scanned is therefore the lower middle of the chart,
    which the fill always covers and no label is ever drawn in.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    chart.resize(width, height)
    image = QImage(chart.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    chart.render(image)
    band = range(int(height * 0.6), int(height * 0.85))
    return [
        x for x in range(width)
        if any((lambda c: c.alpha() > 0 and c.red() > c.blue() + 50 and
                          c.green() > c.blue())(image.pixelColor(x, y))
               for y in band)
    ]


def test_the_quota_axis_spans_the_lookback_not_just_the_readings(store):
    """A probe that stopped answering days ago must leave visible empty axis. Scaling
    the axis to the readings instead would stretch two days of history across a 60-day
    chart and label its right edge `now`, which reports a currency the data doesn't
    have."""
    from diplomat_app.telemetryview import QuotaChart

    now = 1_785_000_000.0
    day = 86400.0
    # Two days of readings at the end of a sixty-day lookback.
    points = tuple(
        telemetry.QuotaPoint(at=now - 2 * day + i * 900, session_pct=80.0, week_pct=90.0)
        for i in range(192)
    )
    chart = QuotaChart()
    chart.set_series(points, 60.0, now)
    columns = _session_fill_columns(chart)

    assert columns, "the 5-hour window was not drawn at all"
    # 2 of 60 days is the last 3.3% of the width — allow slack for the line width and
    # antialiasing, but nothing near the left half.
    assert min(columns) > 400 * 0.9, (
        f"the fill starts at x={min(columns)} of 400 — the axis was scaled to the "
        f"readings rather than to the 60-day lookback"
    )
    assert max(columns) >= 400 * 0.98, "the newest reading is not at the right edge"


def _owed_chart(reviews: list[int], conflicts: list[int]):
    """The owed-work chart rendered 400x160, over a fortnight owing those counts.

    Rendered without ``DrawWindowBackground``, so every pixel left transparent is one
    the chart did not paint. The widget's background is whatever palette the host Qt
    hands it — light on a bare CI runner, dark on a desktop — and reading ink off it
    by brightness passes on one and not the other.
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QRegion
    from PySide6.QtWidgets import QWidget

    from diplomat_app.telemetryview import PendingChart

    now = 1_785_000_000.0
    steps = len(reviews)
    chart = PendingChart()
    chart.set_series(
        tuple(telemetry.PendingPoint(at=now - (steps - i) * 900, reviews=r, conflicts=c)
              for i, (r, c) in enumerate(zip(reviews, conflicts))),
        14.0,
    )
    chart.resize(400, 160)
    image = QImage(chart.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    chart.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return image


def _pending_fill_rows(image) -> tuple[list[int], list[int]]:
    """The y rows each owed-work series painted, as ``(reviews, fixes)``.

    Picked out by hue: reviews are pink (``#FF2D78``, red well over blue) and fixes
    blue (``#32ADE6``, blue well over red), while the card's background is near-black
    and the gridlines white, neither of which leans either way. Scanned down the
    middle of the chart, clear of the ``peak`` label in the top-left corner.
    """
    reviews, fixes = [], []
    for y in range(image.height()):
        colors = [image.pixelColor(x, y)
                  for x in range(image.width() // 3, image.width() * 2 // 3)]
        if any(c.red() > c.blue() + 50 for c in colors):
            reviews.append(y)
        if any(c.blue() > c.red() + 50 for c in colors):
            fixes.append(y)
    return reviews, fixes


def test_owed_work_stacks_rather_than_overlaying(app):
    """Both kinds of work queue for the same executors, so the bands cumulate: the
    fixes ride on top of the reviews and the top edge is the whole backlog. Drawn
    from the axis up instead, the two would cover the same pixels and a moment owing
    three reviews and one fix would read as a backlog of three."""
    reviews, fixes = _pending_fill_rows(_owed_chart([3] * 56, [1] * 56))

    assert reviews and fixes, "a series was not drawn at all"
    # Four owed at once over a plot 136 high (160, padded 8 above and 16 below), so
    # the bands meet three quarters up it. Allowed either side: the 1.8px boundary
    # stroke and its antialiasing.
    plot_top, plot_h = 8, 160 - 8 - 16
    boundary = plot_top + plot_h * (1 - 3 / 4)
    assert min(reviews) > boundary - 4, (
        f"reviews are painted up to y={min(reviews)}, above the {boundary:.0f}px "
        f"boundary — the band was drawn from the axis rather than stacked"
    )
    assert max(fixes) < boundary + 4, (
        f"fixes are painted down to y={max(fixes)}, below the {boundary:.0f}px "
        f"boundary — the band was drawn from the axis rather than off the reviews"
    )
    assert min(fixes) < plot_top + 6, (
        f"the stack tops out at y={min(fixes)}, short of the plot's top — the axis "
        f"is not the peak of what was owed at once"
    )


def test_a_range_that_never_owed_anything_claims_no_peak(app):
    """The count axis is floored at one so an empty range can still be scaled and
    drawn. The peak label reports the data, not that floor — "peak 1 owed" over a
    fortnight in which the agents kept up is a backlog that never existed."""
    def ink(image) -> int:
        """Pixels painted in the corner the peak is written in. Nothing else is drawn
        there over an empty range: the day gridlines land at the right edge and the
        date labels below the plot."""
        return sum(image.pixelColor(x, y).alpha() > 0
                   for x in range(4, 100) for y in range(0, 22))

    # Work only in the last quarter of the range, so the bands stay flat on the floor
    # under the label and the corner holds nothing but it.
    assert ink(_owed_chart([0] * 42 + [3] * 14, [0] * 42 + [1] * 14)) > 0, (
        "nothing was drawn where the peak label belongs — the check below proves "
        "nothing"
    )
    assert ink(_owed_chart([0] * 56, [0] * 56)) == 0, (
        "a range with nothing owed still labelled a peak"
    )


def test_work_running_on_a_peer_is_not_charged_to_this_machine(store):
    """A mesh placement spends the peer's quota; counting it here would make this
    machine's cost per task depend on how busy the fleet is."""
    now = time.time()
    _seed(tasks=0)   # quota readings, so the window has a price to charge against
    for i, remote in ((1, False), (2, True)):
        key = f"review:h/o/r#{i}@cc{i}"
        telemetry.append({"at": now - 7200, "ev": "queued", "key": key,
                          "duty": "review", "pr": i})
        telemetry.append({"at": now - 7100, "ev": "started", "key": key,
                          "remote": remote, "attempt": 1})
        telemetry.append({"at": now - 6000, "ev": "done", "key": key,
                          "tokens": 50_000.0})
    telemetry._reset_cache()

    summary = telemetry.summarize(telemetry.load(), now=now, days=14.0, steps=56,
                                  bin_count=12, z=1.96)
    assert summary.started_count == 2 and summary.remote_count == 1
    assert summary.per_task.count == 1, "a peer's agent was priced against our window"
    assert summary.run_samples == 1

    view = _view(store)
    assert any("1 on mesh peers" in t for t in _labels(view))


# MARK: - The quota chart's gaps


def _quota_runs(readings: list[float | None]):
    """The runs ``QuotaChart`` would draw the 5-hour series as, over readings taken
    one sample interval apart with ``None`` where the probe could not answer."""
    from diplomat_app.telemetryview import QuotaChart

    now = 1_785_000_000.0
    step = telemetry.SAMPLE_INTERVAL_SECS
    points = tuple(
        telemetry.QuotaPoint(at=now - (len(readings) - i) * step,
                             session_pct=v, week_pct=v)
        for i, v in enumerate(readings)
    )
    return QuotaChart._runs(points, lambda p: p.session_pct)


def test_a_short_silence_is_drawn_through_rather_than_cut():
    """The usage endpoint is one per-account bucket shared with every Claude Code
    session on the machine, so a refused attempt here and there is routine. Cutting at
    each one turned a fortnight of readings into a scatter of specks."""
    assert len(_quota_runs([80.0, 70.0, None, 60.0, 50.0])) == 1


def test_a_long_silence_still_breaks_the_line():
    """The other half: joining across a probe that was down for hours would draw a
    slope nobody measured."""
    runs = _quota_runs([80.0, 70.0] + [None] * 8 + [60.0, 50.0])
    assert [len(r) for r in runs] == [2, 2]


def test_a_reading_alone_between_two_silences_is_still_a_reading():
    """It is a measurement the probe did return, and the card counts it in "N of M
    missing". Dropping it made the chart disagree with its own caption."""
    assert [len(r) for r in _quota_runs([None] * 8 + [42.0] + [None] * 8)] == [1]


def _quota_chart(readings: list[float | None]):
    """The quota chart rendered 400x120 over a fortnight of those readings, on a
    transparent ground so every painted pixel is one the chart put there."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QRegion
    from PySide6.QtWidgets import QWidget

    from diplomat_app.telemetryview import QuotaChart

    now = 1_785_000_000.0
    step = telemetry.SAMPLE_INTERVAL_SECS
    chart = QuotaChart()
    chart.set_series(
        tuple(telemetry.QuotaPoint(at=now - (len(readings) - i) * step,
                                   session_pct=v, week_pct=v)
              for i, v in enumerate(readings)),
        14.0, now,
    )
    chart.resize(400, 120)
    image = QImage(chart.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    chart.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return image


def test_a_lone_reading_is_painted_and_not_a_zero_width_nothing(app):
    """A fortnight of readings is 1300 samples across 400 pixels, so a run of one —
    or of a few minutes — is narrower than a pixel. Drawn as a polygon between its own
    two edges it covers nothing at all, however honestly it was measured."""
    def series_ink(image) -> int:
        """Pixels the two series painted. The band is the top of the plot, right of
        the "100%" caption and above both the half-way rule and the date labels, so
        nothing but a reading near the ceiling can put ink in it."""
        return sum(image.pixelColor(x, y).alpha() > 0
                   for x in range(60, 396) for y in range(8, 40))

    assert series_ink(_quota_chart([None] * 8 + [95.0] + [None] * 8)) > 0, (
        "a lone reading painted nothing"
    )
    assert series_ink(_quota_chart([None] * 17)) == 0, (
        "ink appeared with no reading at all — the check above proves nothing"
    )
