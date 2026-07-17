"""Tests for the Claude-API-error watcher: the pure detection/backoff logic
(apiwatch.py), the tmux parsing (tmuxwatch.py), and the Store scan orchestration
(read panes → confirm stall → nudge → backoff → prune)."""

from __future__ import annotations

import pytest

from argent_utils import apiwatch, tmuxwatch


# MARK: - looks_like_api_error


def test_matches_api_error_code():
    assert apiwatch.looks_like_api_error("⏺ API Error: 529 Overloaded.") is True
    assert apiwatch.looks_like_api_error("API Error: 500 blah") is True
    assert apiwatch.looks_like_api_error("API Error:503") is True  # no space variant


def test_matches_bare_429_rate_limit():
    assert apiwatch.looks_like_api_error("429 too many requests") is True
    assert apiwatch.looks_like_api_error("got a 429 rate limit, retrying") is True
    # A stray 429 without rate-limit context is NOT an error (ordinary prose).
    assert apiwatch.looks_like_api_error("see line 429 of config") is False


def test_matches_status_page_and_connectivity():
    assert apiwatch.looks_like_api_error(
        "API Error: something — check https://status.claude.com"
    ) is True
    assert apiwatch.looks_like_api_error("API Error: Unable to connect to API") is True
    assert apiwatch.looks_like_api_error("API Error: Connection error.") is True


def test_quota_banners_are_ignored():
    assert apiwatch.looks_like_api_error("You've hit your weekly limit.") is False
    assert apiwatch.looks_like_api_error(
        "Claude usage limit reached. Your limit will reset at 4pm."
    ) is False
    assert apiwatch.looks_like_api_error("5-hour limit reached ∙ resets 6pm") is False


def test_quota_banner_suppresses_cooccurring_api_error():
    tail = "API Error: 529 Overloaded\nYou've hit your weekly limit."
    assert apiwatch.looks_like_api_error(tail) is False


def test_plain_text_is_not_an_error():
    assert apiwatch.looks_like_api_error("just building the feature normally") is False
    assert apiwatch.looks_like_api_error("") is False


# MARK: - is_confirmed_stall (idle confirmation)


def test_confirmed_stall_requires_two_identical_scans():
    tail = "⏺ API Error: 529 Overloaded."
    assert apiwatch.is_confirmed_stall(None, tail) is False  # first sighting
    assert apiwatch.is_confirmed_stall(tail, tail) is True  # unchanged → stalled
    assert apiwatch.is_confirmed_stall("older different tail", tail) is False  # changed
    # A pane that stopped erroring can't be nudged on stale state.
    assert apiwatch.is_confirmed_stall("clean", "clean") is False


# MARK: - next_backoff schedule


def test_next_backoff_doubles_and_caps():
    assert apiwatch.next_backoff(None) == apiwatch.APIWATCH_COOLDOWN  # 120
    assert apiwatch.next_backoff(120) == 240
    assert apiwatch.next_backoff(240) == 480
    assert apiwatch.next_backoff(10 ** 9) == apiwatch.APIWATCH_MAX_BACKOFF  # capped 3h


def test_last_lines_keeps_tail_non_empty():
    text = "\n".join(["", "a", "  ", "b", "", "c", ""])
    assert apiwatch.last_lines(text, 2) == "b\nc"
    assert apiwatch.last_lines("only", 30) == "only"


def test_human_interval():
    assert apiwatch.human_interval(120) == "2m"
    assert apiwatch.human_interval(3 * 60 * 60) == "3h"
    assert apiwatch.human_interval(90 * 60) == "1h 30m"


# MARK: - tmuxwatch parsing (dump_panes over a stubbed tmux)


def test_dump_panes_parses_and_captures(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(argv)
        if argv[:2] == ["tmux", "list-panes"]:
            return f"%0{tmuxwatch._UNIT}/dev/pts/1\n%3{tmuxwatch._UNIT}/dev/pts/7\n"
        if argv[:2] == ["tmux", "capture-pane"]:
            pane = argv[argv.index("-t") + 1]
            return f"line one\nAPI Error: 529 on {pane}\n\n"
        return ""

    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(tmuxwatch, "_run", fake_run)
    panes = tmuxwatch.dump_panes()
    assert [p.pane_id for p in panes] == ["%0", "%3"]
    assert panes[0].tty == "/dev/pts/1"
    assert "API Error: 529 on %0" in panes[0].tail


def test_dump_panes_none_when_tmux_command_fails(monkeypatch):
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(tmuxwatch, "_run", lambda argv: None)
    monkeypatch.setattr(tmuxwatch, "_server_running", lambda: True)  # server up → failure
    assert tmuxwatch.dump_panes() is None


def test_dump_panes_empty_when_no_server(monkeypatch):
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(tmuxwatch, "_run", lambda argv: None)
    monkeypatch.setattr(tmuxwatch, "_server_running", lambda: False)  # no server → inert
    assert tmuxwatch.dump_panes() == []


def test_dump_panes_empty_when_tmux_absent(monkeypatch):
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: None)
    assert tmuxwatch.dump_panes() == []


# MARK: - Store scan orchestration


@pytest.fixture
def store():
    from argent_utils.store import Store

    return Store()


def _panes(monkeypatch, sequence, sent=True):
    """Patch tmuxwatch so successive scans see ``sequence[i]`` (a list of Pane), and
    record every send_continue call. ``sequence`` may also hold ``None`` (a failed
    dump). Returns the list of nudged pane_ids."""
    state = {"i": 0}
    nudged: list[str] = []

    def fake_dump():
        i = min(state["i"], len(sequence) - 1)
        state["i"] += 1
        return sequence[i]

    monkeypatch.setattr(tmuxwatch, "dump_panes", fake_dump)
    monkeypatch.setattr(tmuxwatch, "is_available", lambda: True)
    monkeypatch.setattr(
        tmuxwatch, "send_continue",
        lambda pane_id, msg: (nudged.append(pane_id) or True) if sent else False,
    )
    return nudged


def _pane(pane_id="%0", tty="/dev/pts/1", tail="⏺ API Error: 529 Overloaded."):
    return tmuxwatch.Pane(pane_id=pane_id, tty=tty, tail=tail)


def test_scan_no_nudge_on_first_sighting(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()]])
    store._apiwatch_scan_once()
    assert nudged == []  # needs a second identical scan to confirm the stall


def test_scan_nudges_confirmed_stall_and_counts(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()], [_pane()]])
    store._apiwatch_scan_once()  # sighting 1: seeds seen_tail
    store._apiwatch_scan_once()  # sighting 2: identical → confirmed stall → nudge
    assert nudged == ["%0"]
    assert store.api_watch_continues == 1


def test_scan_skips_actively_changing_tail(store, monkeypatch):
    # Same pane erroring but the tail keeps changing → still working, never nudged.
    nudged = _panes(
        monkeypatch,
        [
            [_pane(tail="API Error: 529 Overloaded. retry 1")],
            [_pane(tail="API Error: 529 Overloaded. retry 2")],
            [_pane(tail="API Error: 529 Overloaded. retry 3")],
        ],
    )
    for _ in range(3):
        store._apiwatch_scan_once()
    assert nudged == []


def test_scan_backoff_blocks_immediate_renudge(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()]] * 4)
    store._apiwatch_scan_once()  # seed
    store._apiwatch_scan_once()  # nudge 1
    store._apiwatch_scan_once()  # inside 120s backoff → no nudge
    store._apiwatch_scan_once()
    assert nudged == ["%0"]  # exactly one nudge despite four erroring scans
    assert store.api_watch_continues == 1


def test_scan_renudges_after_backoff_elapses(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()]] * 3)
    store._apiwatch_scan_once()  # seed
    store._apiwatch_scan_once()  # nudge 1, schedules nextAllowed = now + 120
    # Fast-forward the pane's backoff window into the past.
    store._apiwatch_backoff["%0"]["nextAllowed"] = 0
    store._apiwatch_scan_once()  # backoff elapsed + still stalled → nudge 2
    assert nudged == ["%0", "%0"]
    assert store.api_watch_continues == 2


def test_scan_ignores_quota_stall(store, monkeypatch):
    nudged = _panes(
        monkeypatch, [[_pane(tail="You've hit your weekly limit.")]] * 2
    )
    store._apiwatch_scan_once()
    store._apiwatch_scan_once()
    assert nudged == []


def test_scan_skips_when_dump_fails(store, monkeypatch):
    # A None dump (tmux command failed) must not clear backoff nor crash.
    nudged = _panes(monkeypatch, [[_pane()], [_pane()], None, [_pane()]])
    store._apiwatch_scan_once()  # seed
    store._apiwatch_scan_once()  # nudge
    assert store._apiwatch_backoff  # backoff recorded
    store._apiwatch_scan_once()  # None → skipped, state preserved
    assert store._apiwatch_backoff  # not cleared by the failed scan
    assert nudged == ["%0"]


def test_scan_prunes_recovered_pane(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()], [_pane()], [_pane(tail="all good now")]])
    store._apiwatch_scan_once()  # seed
    store._apiwatch_scan_once()  # nudge, records backoff + seen_tail
    assert "%0" in store._apiwatch_seen_tail
    store._apiwatch_scan_once()  # pane no longer erroring → pruned
    assert "%0" not in store._apiwatch_backoff
    assert "%0" not in store._apiwatch_seen_tail


def test_scan_noop_when_disabled(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()], [_pane()]])
    store.api_watch_enabled = False
    store._apiwatch_scan_once()
    store._apiwatch_scan_once()
    assert nudged == []
