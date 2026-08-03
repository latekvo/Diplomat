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
  points looks exactly as authoritative as one drawn through four hundred.

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
