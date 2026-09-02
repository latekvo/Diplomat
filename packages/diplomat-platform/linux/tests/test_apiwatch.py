"""Tests for the Claude-API-error watcher: the pure detection/backoff logic
(apiwatch.py), the tmux parsing (tmuxwatch.py), and the Store scan orchestration
(read panes → confirm stall → nudge → backoff → prune)."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from diplomat_runtime import apiwatch, tmuxwatch


# MARK: - looks_like_api_error


def test_matches_api_error_code():
    assert apiwatch.looks_like_api_error("⏺ API Error: 529 Overloaded.") is True
    assert apiwatch.looks_like_api_error("API Error: 500 blah") is True
    assert apiwatch.looks_like_api_error("API Error:503") is True  # no space variant


def test_matches_bare_429_rate_limit():
    assert apiwatch.looks_like_api_error("429 too many requests") is True
    assert apiwatch.looks_like_api_error("✗ 429 Rate limited · retrying in 34s") is True
    # Its line, not the tail's first line — this arm needs its own mid-tail fixture,
    # since it shares no pattern with the two "API Error" rules.
    assert apiwatch.looks_like_api_error(
        "⏺ Running the suite…\n✗ 429 Rate limited · retrying in 34s\n  ? for shortcuts"
    ) is True
    # Its rate-limit context wraps like any other banner's, so it reads the rejoined copy.
    assert apiwatch.looks_like_api_error("⏺ 429 Rate\n  limited · retrying in 34s") is True
    # A 429 an agent WROTE is not a banner, however much rate-limit context surrounds it.
    # This arm carries no "API Error:" prefix to anchor, so the code itself has to open
    # the line — otherwise it is the widest hole in the predicate, matching any tail that
    # mentions both a 429 and a rate limit anywhere.
    assert apiwatch.looks_like_api_error("got a 429 rate limit, retrying") is False
    assert apiwatch.looks_like_api_error(
        "    if resp.status_code == 429:  # rate limit, back off and retry"
    ) is False
    assert apiwatch.looks_like_api_error(
        "quoting API Error: 429 Rate limit exceeded in a sentence"
    ) is False
    assert apiwatch.looks_like_api_error("这是 429 rate limit 的例子") is False
    # And a stray 429 with no rate-limit context is ordinary prose either way — including
    # one that opens its line, which the anchor alone would wave through.
    assert apiwatch.looks_like_api_error("see line 429 of config") is False
    assert apiwatch.looks_like_api_error("429 stale entries pruned from the cache") is False


def test_matches_status_page_and_connectivity():
    assert apiwatch.looks_like_api_error(
        "API Error: something — check https://status.claude.com"
    ) is True
    assert apiwatch.looks_like_api_error("API Error: Unable to connect to API") is True
    assert apiwatch.looks_like_api_error("API Error: Connection error.") is True


def test_banner_must_open_a_line():
    # Quoted mid-sentence it is prose, not a stall.
    assert apiwatch.looks_like_api_error(
        "the predicate returns True on ⏺ API Error: Connection error."
    ) is False
    assert apiwatch.looks_like_api_error(
        "quoting API Error: 529 Overloaded in a sentence"
    ) is False
    # An agent's prose is not always Latin, and it disqualifies a line just the same:
    # `[^A-Za-z]` would read 这是 as decoration and nudge the session that wrote it.
    assert apiwatch.looks_like_api_error("这是 API Error: Connection error. 的例子") is False
    # A line that merely NAMES the banner is not one either: the CLI always prints the
    # colon, and without it "API Error" is just two words a doc line can start with.
    assert apiwatch.looks_like_api_error(
        "⏺ Notes on the transport layer:\n"
        "  - API Error is surfaced verbatim to the caller\n"
        "  - the client retries on a transient network\n"
        "    error before giving up"
    ) is False
    # Decoration in front of it is not prose: the transcript bullet, the tool-result
    # elbow, a pane's left border and a log timestamp all precede a real banner. Only
    # LETTERS disqualify a line — prose reaches a quoted banner through words.
    assert apiwatch.looks_like_api_error("  ⎿  API Error: Connection error.") is True
    assert apiwatch.looks_like_api_error("│ ⏺ API Error: 503 Service Unavailable") is True
    # Casing is the CLI's to change; the watcher does not key on it — and each rule
    # carries its own flag, so the coded one needs its own lowercase banner.
    assert apiwatch.looks_like_api_error("⏺ api error: connection error.") is True
    assert apiwatch.looks_like_api_error("⏺ api error: 529 overloaded.") is True
    # Codeless on purpose: a 3-digit code would match through the code rule instead and
    # leave the digits-are-decoration intent unpinned.
    assert apiwatch.looks_like_api_error("21:28:22 API Error: Connection error.") is True
    # The banner is never the first line of a real tail — it sits under the transcript
    # and over the prompt box. Both anchored rules need a tail of this shape to pin that
    # `^` is per LINE and not per tail; the codeless one cannot reach the code rule.
    assert apiwatch.looks_like_api_error(
        "⏺ Running the suite…\n⏺ API Error: Connection error.\n  ? for shortcuts"
    ) is True
    assert apiwatch.looks_like_api_error(
        "⏺ Running the suite…\n⏺ API Error: 500 Internal Server Error\n  ? for shortcuts"
    ) is True


def test_matches_banners_wrapped_across_terminal_lines():
    # These banners run 70-90 columns, so a narrow pane wraps them mid-phrase. The
    # evidence is read off a copy with the wrapping rejoined, so the split must not hide
    # the banner.
    assert apiwatch.looks_like_api_error(
        "⏺ API Error: Your computer went to sleep mid-response. "
        "The response above may be\n  incomplete."
    ) is True
    assert apiwatch.looks_like_api_error(
        "⏺ API Error: Connection lost before a response was\n  produced. Try again."
    ) is True


def test_matches_cut_off_stream_banners():
    # Every wording the CLI builds this family from — a cause plus one of two endings.
    # The endings are what the matcher reads, so all seven have to nudge.
    for banner in (
        "Server error mid-response. The response above may be incomplete.",
        "Connection lost mid-response. The response above may be incomplete.",
        "Your computer went to sleep mid-response. The response above may be incomplete.",
        "The response stopped arriving. The response above may be incomplete.",
        "The response stalled before a response was produced. Try again.",
        "Connection lost before a response was produced. Try again.",
        "Your computer went to sleep before a response was produced. Try again.",
    ):
        assert apiwatch.looks_like_api_error(f"\u23fa API Error: {banner}") is True, banner
    # An ending without the prefix is ordinary prose, not a banner.
    assert apiwatch.looks_like_api_error(
        "note: the response above may be incomplete"
    ) is False


def test_rejoin_does_not_manufacture_a_quota_banner():
    # The rejoin fuses every adjacent pair of rows, so suppression reads the ORIGINAL:
    # off the rejoined copy, two lines of ordinary prose assemble into a limit banner
    # and silence a session that really is stalled, with no second chance at it.
    assert apiwatch.looks_like_api_error(
        "⏺ The dispatcher stops as soon as the workspace token budget\n"
        "  exceeded the daily cap.\n"
        "⏺ API Error: 529 Overloaded. If it persists, check https://status.claude.com."
    ) is True
    assert apiwatch.looks_like_api_error(
        "⏺ The client backs off long before you hit your\n"
        "  limit, so nothing is dropped.\n"
        "⏺ API Error: Connection error."
    ) is True
    # The contiguous phrase list fuses the same way the two gap regexes above do.
    assert apiwatch.looks_like_api_error(
        "⏺ The quota family is matched on the phrase the limit will reset\n"
        "  at whatever hour the window rolls over.\n"
        "⏺ API Error: Connection error."
    ) is True


def test_quota_banners_are_ignored():
    assert apiwatch.looks_like_api_error("You've hit your weekly limit.") is False
    assert apiwatch.looks_like_api_error(
        "Claude usage limit reached. Your limit will reset at 4pm."
    ) is False
    assert apiwatch.looks_like_api_error("5-hour limit reached ∙ resets 6pm") is False


def test_org_budget_caps_are_ignored():
    # A 403 spend cap prints WITH the "API Error: <code>" prefix, so it only stays
    # un-nudged for as long as the budget wording is matched ahead of the code rule.
    assert apiwatch.looks_like_api_error(
        "API Error: 403 Org member budget limit exceeded (daily limit). "
        "Contact your org admin."
    ) is False
    assert apiwatch.looks_like_api_error("Organization budget exceeded") is False
    assert apiwatch.looks_like_api_error("workspace monthly budget limit reached") is False
    # Prose about a budget that isn't a cap being hit stays nudgeable.
    assert apiwatch.looks_like_api_error(
        "API Error: 529 Overloaded\nthe budget for this run was 500k tokens"
    ) is True


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


# MARK: - looks_busy (working vs waiting at the prompt)


# The status bar the CLI draws under its prompt box, in both of its states. Verbatim
# from a real session (2026-08-05), because the whole signal is one substring of it.
_BUSY_BAR = "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents"
_IDLE_BAR = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
_PROMPT_BOX = "─" * 40 + "\n❯ \n" + "─" * 40


def test_looks_busy_reads_the_live_status_bar():
    assert apiwatch.looks_busy(f"● Reading files…\n{_PROMPT_BOX}\n{_BUSY_BAR}") is True
    # The same session one moment later: turn over, hint gone, prompt still there.
    assert apiwatch.looks_busy(f"● Posted the review.\n{_PROMPT_BOX}\n{_IDLE_BAR}") is False
    assert apiwatch.looks_busy("") is False


def test_looks_busy_ignores_the_hint_left_in_scrollback():
    """The reason this scans a far shorter tail than the error watcher: every finished
    turn leaves its own interrupt hint in the buffer above the prompt. Reaching past
    the live status bar would read every finished agent as busy forever — which is the
    bug this whole path exists to end."""
    stale = "\n".join([_BUSY_BAR] + [f"line {i}" for i in range(10)] + [_IDLE_BAR])
    assert apiwatch.looks_busy(stale) is False
    assert apiwatch.BUSY_TAIL_LINES < apiwatch.SCANNED_TAIL_LINES


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
            return "%0 /dev/pts/1\n%3 /dev/pts/7\n"
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


def test_pane_tails_for_ttys_captures_only_the_ttys_asked_for(monkeypatch):
    """The cap's idle scan runs on the panel's 8-second tick and wants the two panes
    its agents are on. Capturing every pane would put a subprocess per pane on that
    tick — so the listing is read once and only the matching panes are captured.

    Keys and lookups are the `ps` spelling of a tty; tmux's `/dev/` prefix is dropped
    on the way in, or every lookup would miss and no agent would ever read as idle."""
    captured: list[str] = []

    def fake_run(argv):
        if argv[:2] == ["tmux", "list-panes"]:
            return "\n".join(f"%{n} /dev/pts/{n}" for n in (1, 2, 3))
        if argv[:2] == ["tmux", "capture-pane"]:
            pane = argv[argv.index("-t") + 1]
            captured.append(pane)
            return f"tail of {pane}\n"
        return ""

    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(tmuxwatch, "_run", fake_run)

    tails = tmuxwatch.pane_tails_for_ttys({"pts/2"})
    assert tails == {"pts/2": "tail of %2"}
    assert captured == ["%2"], "one capture per agent, not per pane"

    # Nothing to ask about ⇒ tmux is never run at all.
    captured.clear()
    assert tmuxwatch.pane_tails_for_ttys(set()) == {}
    assert captured == []


def test_pane_tails_for_ttys_never_raises_into_its_callers(monkeypatch):
    """Its callers are the autofix poll worker and the mesh node's capacity hook.
    An exception would kill the poll for the applet's remaining life (the way a
    strict decode once killed the watcher) or fail a peer's job over an unreadable
    screen. Every failure has to read as ``None`` — "could not look" — instead."""
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")

    def boom(argv):
        raise RuntimeError("tmux server went away mid-scan")

    monkeypatch.setattr(tmuxwatch, "_run", boom)
    assert tmuxwatch.pane_tails_for_ttys({"pts/1"}) is None

    # A tmux that simply is not installed is the same answer by a quieter route.
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: None)
    assert tmuxwatch.pane_tails_for_ttys({"pts/1"}) is None


def test_a_failed_capture_is_distinguishable_from_an_agent_with_no_pane(monkeypatch):
    """The distinction this return type exists for. Both used to be ``{}``, and
    reading "could not look" as "this agent has no pane" is how an agent whose screen
    went unreadable was mistaken for one idling at its prompt — and had its bay of the
    task cap taken back while it was still working."""
    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")

    # tmux answered, and this agent's tty genuinely has no pane.
    monkeypatch.setattr(tmuxwatch, "_run",
                        lambda argv: "" if argv[:2] == ["tmux", "list-panes"] else None)
    assert tmuxwatch.pane_tails_for_ttys({"pts/1"}) == {}

    # tmux could not be asked.
    monkeypatch.setattr(tmuxwatch, "_run", lambda argv: None)
    assert tmuxwatch.pane_tails_for_ttys({"pts/1"}) is None


def test_run_survives_non_utf8_pane_bytes_and_still_scans():
    """A regression for the whole-watcher crash: ``capture-pane -p`` emits pane
    content verbatim, so a stalled agent's pane can carry a raw non-UTF-8 byte. A
    strict decode raised ``UnicodeDecodeError`` (a ``ValueError``, so it slipped past
    ``_run``'s ``(OSError, SubprocessError)`` guard, propagated out of the watcher's
    no-``except`` worker, and silently killed every poll). ``_run`` must instead
    decode leniently: no raise, and the ``API Error`` text — the very thing we scan a
    stalled pane for — is still visible."""
    # A real child (no tmux needed) emitting the error line plus a lone 0xE9 byte.
    argv = ["python3", "-c",
            r'import os; os.write(1, b"API Error: 529\n\xe9 build.log\n")']
    out = tmuxwatch._run(argv)  # must NOT raise
    assert out is not None
    assert "API Error: 529" in out  # still scannable despite the stray byte


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
    from diplomat_app.store import Store

    return Store()


def _panes(monkeypatch, sequence, sent=True, agent_ttys=frozenset({"pts/1"})):
    """Patch tmuxwatch so successive scans see ``sequence[i]`` (a list of Pane), and
    record every send_continue call. ``sequence`` may also hold ``None`` (a failed
    dump). Returns the list of nudged pane_ids.

    ``agent_ttys`` stands in for the process table, which the scan reads to decide
    which panes are somebody's agent — as ``ps`` spells a tty, so no ``/dev/``. It
    defaults to the one ``_pane`` runs on, because every test below that is about
    stalls and backoff means its pane to be an agent's. Pass an ``Observation`` to
    make the table itself unreadable.
    """
    from diplomat_app import probes

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
    obs = (agent_ttys if isinstance(agent_ttys, probes.Observation)
           else probes.Observation.present(set(agent_ttys)))
    monkeypatch.setattr(probes, "ttys_running_an_agent", lambda now: obs)
    return nudged


def _pane(pane_id="%0", tty="/dev/pts/1", tail="⏺ API Error: 529 Overloaded."):
    return tmuxwatch.Pane(pane_id=pane_id, tty=tty, tail=tail)


def test_scan_never_nudges_a_pane_no_agent_is_on(store, monkeypatch):
    """The nudge is typed into the pane and submitted. In an agent's pane that is a
    user turn; in a plain shell it is a command — ``Go: command not found`` and a line
    of junk in that shell's history. A shell can show a matching tail for entirely
    innocent reasons (``cat`` of a log holding a banner, a ``git diff`` of the
    matcher's own tests), and nothing on the screen separates those from the CLI's own
    line."""
    nudged = _panes(monkeypatch, [[_pane(tty="/dev/pts/9")]] * 2,
                    agent_ttys={"pts/1"})
    store._apiwatch_scan_once()  # sighting 1
    store._apiwatch_scan_once()  # identical tail: a stall confirmed on any other pane
    assert nudged == []
    assert store.api_watch_continues == 0


def test_scan_watches_an_agent_pane_beside_a_shell_showing_the_same_tail(
        store, monkeypatch):
    """Both panes are stalled on the same banner; only one has an agent on it. Pinned
    together so a filter that dropped every pane would still be caught."""
    nudged = _panes(
        monkeypatch,
        [[_pane(pane_id="%0", tty="/dev/pts/1"),
          _pane(pane_id="%9", tty="/dev/pts/9")]] * 2,
        agent_ttys={"pts/1"},
    )
    store._apiwatch_scan_once()
    store._apiwatch_scan_once()
    assert nudged == ["%0"]
    # And the pill counts what is being watched, not what tmux happens to be running.
    assert store.apiwatch_status["watching"] == 1


def test_scan_skips_when_the_process_table_cannot_be_read(store, monkeypatch):
    """Not knowing which panes carry an agent has to mean typing into none of them —
    the same trade the failed pane dump makes, and for a heavier reason. State
    survives, so a recovered table needs no fresh two-scan confirmation."""
    from diplomat_app import probes

    nudged = _panes(monkeypatch, [[_pane()]] * 4)
    store._apiwatch_scan_once()  # seed
    store._apiwatch_scan_once()  # nudge
    assert nudged == ["%0"]
    monkeypatch.setattr(probes, "ttys_running_an_agent",
                        lambda now: probes.Observation.unavailable("exited 1"))
    store._apiwatch_backoff["%0"]["nextAllowed"] = 0  # backoff is not what holds it
    store._apiwatch_scan_once()
    assert nudged == ["%0"]
    assert store._apiwatch_backoff  # not cleared by the skipped scan
    assert store.apiwatch_status["watching"] == 0


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
    # Exactly one nudge, on the second scan: the recovered pane must not draw
    # another. Without this the test passes even if the watcher never nudges.
    assert nudged == ["%0"]


def test_scan_noop_when_disabled(store, monkeypatch):
    nudged = _panes(monkeypatch, [[_pane()], [_pane()]])
    store.api_watch_enabled = False
    store._apiwatch_scan_once()
    store._apiwatch_scan_once()
    assert nudged == []


# MARK: - Closing a run's own window (tmuxwatch.kill_session)


def _live_sessions() -> set[str]:
    out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                         capture_output=True, text=True, check=False)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def _open_session(name: str) -> None:
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 120"], check=True)


def _close_sessions(*names: str) -> None:
    for n in names:
        subprocess.run(["tmux", "kill-session", "-t", f"={n}"],
                       capture_output=True, check=False)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on this machine")
def test_a_runs_own_session_is_closed_by_its_name():
    """Against the real tmux: the name the spawn opened is the name the reaper computes,
    and killing it takes the window with it."""
    name = tmuxwatch.session_name(f"{os.getpid()}-{uuid.uuid4().hex[:8]}")
    _open_session(name)
    try:
        assert name in _live_sessions(), "the fixture session never opened"
        assert tmuxwatch.kill_session(name) is True
        assert name not in _live_sessions()
    finally:
        _close_sessions(name)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on this machine")
def test_a_reap_whose_own_session_is_gone_does_not_take_a_neighbours():
    """The target SYNTAX, which is the whole of this call's safety. tmux resolves a bare
    target exactly, then by PREFIX, then by fnmatch — so a reap of a run whose session
    has already closed falls through to any LIVE session whose name merely starts the
    same, and ends somebody else's agent instead. `=` is what refuses that.

    Run ids are fixed-width today (`<epoch>-<hex8>`), so no two of ours can be prefixes
    of each other, and that is not a property to leave the one destructive call in the
    applet resting on."""
    gone = tmuxwatch.session_name(f"{os.getpid()}-{uuid.uuid4().hex[:8]}")
    neighbour = f"{gone}-2"
    _open_session(neighbour)
    try:
        assert tmuxwatch.kill_session(gone) is False

        assert neighbour in _live_sessions(), \
            "a prefix match closed a window this run never opened"
    finally:
        _close_sessions(neighbour)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on this machine")
def test_a_session_that_is_not_there_is_not_a_kill():
    """The reaper reads the answer: False is what sends it on to the tty walk, and a
    True here would let a run be retired with its window still open."""
    assert tmuxwatch.kill_session(
        tmuxwatch.session_name(f"never-opened-{uuid.uuid4().hex[:8]}")) is False


def test_a_run_with_no_name_is_never_killed_by_one(monkeypatch):
    """A mesh-placed run has no session of ours; an empty name must reach no tmux at
    all rather than becoming a target that matches whatever tmux feels like."""
    monkeypatch.setattr(tmuxwatch, "_run",
                        lambda argv: pytest.fail(f"tmux was run: {argv}"))
    assert tmuxwatch.kill_session("") is False


# MARK: - the reap closes a window, not the session around it


def test_the_tty_route_kills_the_panes_window_and_no_session(monkeypatch):
    """An agent the operator ran by hand sits in a window of THEIR session; reaping
    it must take that window and nothing else of theirs. A session this applet or a
    mesh node opened has one window, and ends with it."""
    calls: list[list[str]] = []

    panes = {"/dev/pts/3": "@4", "/dev/pts/9": "@7"}

    def fake_run(argv):
        calls.append(argv)
        if argv[:2] == ["tmux", "list-panes"]:
            return "".join(f"{tty} {w}\n" for tty, w in panes.items())
        if argv[:2] == ["tmux", "kill-window"]:
            for tty, w in list(panes.items()):
                if w == argv[-1]:
                    del panes[tty]
        return ""

    monkeypatch.setattr(tmuxwatch.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(tmuxwatch, "_run", fake_run)
    assert tmuxwatch.kill_window_for_tty("pts/9") is True
    assert ["tmux", "kill-window", "-t", "@7"] in calls
    assert not any(c[:2] == ["tmux", "kill-session"] for c in calls)
    assert panes == {"/dev/pts/3": "@4"}, "the other pane's window is untouched"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs a real tmux")
def test_a_one_window_session_ends_with_its_window():
    """The mesh node opens an auto-named session around its agent; the tty route
    is the only way that one is reaped, and killing its sole window must end it."""
    name = f"diplomat-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 300"], check=True)
    try:
        tty = subprocess.run(
            ["tmux", "list-panes", "-t", name, "-F", "#{pane_tty}"],
            capture_output=True, text=True, check=True).stdout.strip()
        listing = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_tty} #{window_id}"],
            capture_output=True, text=True).stdout
        assert tmuxwatch.kill_window_for_tty(tty) is True, f"tty={tty!r} panes={listing!r}"
        gone = subprocess.run(["tmux", "has-session", "-t", name],
                              capture_output=True).returncode != 0
        assert gone
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on this machine")
def test_a_client_outside_tmux_under_a_c_locale_still_reads_its_panes(monkeypatch):
    """Where launchd, an autostart entry and CI put the applet: no ``$TMUX`` and no
    UTF-8 in the locale, so tmux sanitizes every control byte of a command's output
    to ``_`` (and 3.4 escapes them as octal for any client)."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("LC_ALL", "C")
    name = f"diplomat-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 300"], check=True)
    try:
        tty = subprocess.run(
            ["tmux", "list-panes", "-t", name, "-F", "#{pane_tty}"],
            capture_output=True, text=True, check=True).stdout.strip()
        short = tty.removeprefix("/dev/")
        tails = tmuxwatch.pane_tails_for_ttys({short})
        assert tails is not None and short in tails
        assert tmuxwatch.kill_window_for_tty(tty) is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
