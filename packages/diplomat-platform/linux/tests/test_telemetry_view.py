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

from diplomat_app import telemetry
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


def _seed(*, samples: bool = True, tasks: int = 8, priced: bool = True) -> None:
    """A ledger with `tasks` finished local tasks. ``priced`` decides whether the
    samples carry quota readings — i.e. whether the 5-hour window can be priced at
    all — and ``samples`` whether there are any samples to begin with."""
    now = time.time()
    if samples:
        for i in range(4):
            telemetry.append({
                "at": now - (4 - i) * 3600, "ev": "sample",
                "sessionLeft": (1.0 - 0.2 * i) if priced else None,
                "weekLeft": (1.0 - 0.02 * i) if priced else None,
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
    assert "95% CI" in text


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

    Picked out by hue: the fill is orange, the half-way rule is white (r == b) and
    the 7-day line is purple (b > r), so "red well above blue" is the fill and only
    the fill.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    chart.resize(width, height)
    image = QImage(chart.size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    chart.render(image)
    return [
        x for x in range(width)
        if any((lambda c: c.alpha() > 0 and c.red() > c.blue() + 20)(image.pixelColor(x, y))
               for y in range(height))
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
