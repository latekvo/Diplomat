"""Specific-PR author auto-detection for the Review wizard.

Covers the gh resolver (mocked, no network) and the wizard's ownership -> toggles
-> config wiring (mine / theirs / unresolved), mirroring the macOS
ReviewWizardView author poll. Uses an offscreen QApplication (no display).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from argent_utils import gh, review  # noqa: E402
from argent_utils.prtarget import PRTarget  # noqa: E402
from argent_utils.review import SpecificAuthor  # noqa: E402
from argent_utils.store import Store  # noqa: E402
from argent_utils.wizardview import WizardView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# MARK: - The gh resolver (mocked, offline)


def test_fetch_specific_author_parses_login(monkeypatch):
    calls = {}

    def fake_run(args, timeout=60.0):
        calls["args"] = args
        return b'{"author": {"login": "octocat"}}'

    monkeypatch.setattr(gh, "run", fake_run)
    assert review.fetch_specific_author("software-mansion", "argent", 337) == "octocat"
    # It uses `gh pr view <n> --repo owner/repo --json author` (mirrors macOS).
    assert calls["args"] == [
        "pr", "view", "337", "--repo", "software-mansion/argent", "--json", "author"
    ]


def test_fetch_specific_author_none_on_failure(monkeypatch):
    def boom(args, timeout=60.0):
        raise gh.GHError("gh exited 1: no pull requests found")

    monkeypatch.setattr(gh, "run", boom)
    assert review.fetch_specific_author("o", "r", 999) is None


def test_fetch_specific_author_none_on_malformed(monkeypatch):
    monkeypatch.setattr(gh, "run", lambda args, timeout=60.0: b"not json")
    assert review.fetch_specific_author("o", "r", 1) is None
    # A well-formed envelope missing the author still yields None, not a crash.
    monkeypatch.setattr(gh, "run", lambda args, timeout=60.0: b"{}")
    assert review.fetch_specific_author("o", "r", 1) is None


# MARK: - The wizard: ownership -> toggles + config


def _wizard_on_specific(qapp, me="latekvo"):
    store = Store()
    store.me = me
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("337")
    return w


def test_specific_pr_resolves_to_mine(qapp):
    w = _wizard_on_specific(qapp, me="latekvo")
    # Simulate the background poll resolving the author to me.
    w._on_author_resolved(w.specific_pr.text(), "latekvo")

    assert w._specific_author == SpecificAuthor.MINE
    cfg = w._config()
    assert cfg.disposition == SpecificAuthor.MINE
    # Mine: mark-ready + reply visible, formal-review + final-verdict hidden.
    assert (not w.mark_ready.isHidden()) and (not w.reply.isHidden())
    assert w.leave_reviews.isHidden()
    assert w.final_pass.isHidden()
    assert (cfg.can_mark_ready, cfg.can_leave_reviews,
            cfg.can_reply_to_reviews, cfg.can_final_pass) == (True, False, True, False)
    # Config sent to argent-core carries the resolved disposition.
    assert cfg.specific_author == SpecificAuthor.MINE
    assert (not w.author_hint.isHidden())
    assert "Your PR" in w.author_hint.text()


def test_specific_pr_resolves_to_someone_else(qapp):
    w = _wizard_on_specific(qapp, me="latekvo")
    w._on_author_resolved(w.specific_pr.text(), "someoneelse")

    assert w._specific_author == SpecificAuthor.THEIRS
    cfg = w._config()
    assert cfg.disposition == SpecificAuthor.THEIRS
    # Theirs: formal-review + final-verdict visible, mark-ready + reply hidden.
    assert (not w.leave_reviews.isHidden()) and (not w.final_pass.isHidden())
    assert w.mark_ready.isHidden()
    assert w.reply.isHidden()
    assert (cfg.can_mark_ready, cfg.can_leave_reviews,
            cfg.can_reply_to_reviews, cfg.can_final_pass) == (False, True, False, True)
    assert cfg.specific_author == SpecificAuthor.THEIRS
    assert "Someone else's PR" in w.author_hint.text()


def test_specific_pr_unresolved_offers_everything(qapp):
    w = _wizard_on_specific(qapp, me="latekvo")
    # A failed poll (login == "") leaves the disposition UNKNOWN -> all toggles.
    w._on_author_resolved(w.specific_pr.text(), "")

    assert w._specific_author == SpecificAuthor.UNKNOWN
    cfg = w._config()
    assert all([cfg.can_mark_ready, cfg.can_leave_reviews,
                cfg.can_reply_to_reviews, cfg.can_final_pass])
    for cb in (w.mark_ready, w.leave_reviews, w.reply, w.final_pass):
        assert not cb.isHidden()
    assert cfg.specific_author == SpecificAuthor.UNKNOWN


def test_stale_author_result_is_ignored(qapp):
    # A poll that resolves for OLD input must not clobber the current PR's state
    # (mirrors the macOS `pending` supersede guard).
    w = _wizard_on_specific(qapp, me="latekvo")
    w._on_author_resolved(w.specific_pr.text(), "latekvo")
    assert w._specific_author == SpecificAuthor.MINE
    # A late result for a superseded PR number is dropped.
    w._on_author_resolved("999", "someoneelse")
    assert w._specific_author == SpecificAuthor.MINE


def test_switching_away_from_specific_resets_disposition(qapp):
    w = _wizard_on_specific(qapp, me="latekvo")
    w._on_author_resolved(w.specific_pr.text(), "someoneelse")
    assert w._specific_author == SpecificAuthor.THEIRS
    # Back to a whose-PRs sweep: disposition resets, hint hides, and the sweep's own
    # target-driven toggles apply (mine -> mark-ready + reply, no formal review).
    w.target.setCurrentIndex(w.target.findData(PRTarget.MINE))
    assert w._specific_author == SpecificAuthor.UNKNOWN
    assert w.author_hint.isHidden()
    cfg = w._config()
    assert cfg.disposition == SpecificAuthor.MINE  # follows the MINE target
    assert (not w.mark_ready.isHidden()) and w.leave_reviews.isHidden()


def test_entering_specific_pr_starts_loading(qapp, monkeypatch):
    # A fresh, valid PR ref flips the wizard into the loading state and spawns a
    # background poll (which we stub so no gh runs and no thread lingers).
    monkeypatch.setattr(
        "argent_utils.wizardview.threading.Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None})(),
    )
    store = Store()
    store.me = "latekvo"
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("337")
    assert w._author_loading is True
    assert w._specific_author == SpecificAuthor.UNKNOWN  # pending -> offer everything
    assert w._author_pending == "337"
    assert "Checking who authored" in w.author_hint.text()
