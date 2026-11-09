"""Goal tasks on Linux: the grouper's tick, the GOAL section and graph of the Tasks
screen, its agent overlay, the ``+`` fix, and the automatic pass of an ``auto`` goal,
through the real ``diplomat-core`` in this test's ``DIPLOMAT_TASKS_DIR`` against a stub
``gh`` and ``claude``.
"""

from __future__ import annotations

import os
import threading

import pytest
from diplomat_app import autofixmonitor, bans
from diplomat_app.render import gh_candidate, gh_issue, gh_pr, write_claude_stub, write_gh_stub
from diplomat_app.store import GoalOverlay, goal_overlay
from diplomat_app.tasksview import TasksView
from diplomat_runtime import agentregistry, agentstate, autofix, core, tasks
from diplomat_runtime.autofix import PRSnapshot
from test_autofix import _spawn_recorder
from test_tasks_store import _create, settle, watched
from test_tasks_store import app, github, tstore  # noqa: F401 — fixtures
from test_tasks_view import opened  # noqa: F401 — fixture

pytest.importorskip("PySide6")

pytestmark = pytest.mark.skipif(not os.environ.get("DIPLOMAT_CORE_BIN"),
                                reason="DIPLOMAT_CORE_BIN not set")

#: Issue #1801 is closed by PR #1810, which #1811 is stacked on; #1802 has no PR yet;
#: #1812 is the operator's own PR.
ISSUE, LONE = watched(1801), watched(1802)
PR, STACKED, MINE = watched(1810, "pull"), watched(1811, "pull"), watched(1812, "pull")


def _key(n: int) -> str:
    cfg = core.config()
    return f"{cfg['owner']}/{cfg['repo']}#{n}".lower()


def _github(github, *, candidates: bool = True) -> None:
    """GitHub as the goal's items stand: every item, and the grouper's candidates."""
    github.answers.update({
        _key(1801): gh_issue(ISSUE, "Duo crashes on fold"),
        _key(1802): gh_issue(LONE, "Duo hinge angle", author="kacper"),
        _key(1810): gh_pr(PR, "Duo support", closes=(ISSUE,), head="duo", oid="aaa1810",
                          author="hubert"),
        _key(1811): gh_pr(STACKED, "Duo layout", base="duo", oid="aaa1811",
                          author="hubert"),
        _key(1812): gh_pr(MINE, "Duo docs", oid="aaa1812", author="alice"),
    })
    if candidates:
        github.answers["candidates"] = {
            "issues": {"nodes": [gh_candidate(ISSUE, "Duo crashes on fold"),
                                 gh_candidate(LONE, "Duo hinge angle")]},
            "pullRequests": {"nodes": [gh_candidate(PR, "Duo support", closes=(1801,)),
                                       gh_candidate(STACKED, "Duo layout")]},
        }
    write_gh_stub(github.dir, github.answers)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """A ``claude`` that groups the four Duo items into one goal."""
    stub = write_claude_stub(tmp_path / "claude", [{
        "task": None, "title": "iPhone Duo support", "tags": ["area:devices"],
        "items": [1801, 1802, 1810, 1811], "blocks": []}])
    monkeypatch.setenv("DIPLOMAT_CLAUDE", stub)
    return tmp_path / "claude"


@pytest.fixture
def no_agents(monkeypatch):
    """Prompts assembled without the core, and every spawn recorded, never run."""
    monkeypatch.setattr(
        "diplomat_runtime.promptcore.build_prompt",
        lambda cfg: f"PROMPT:{cfg.get('kind')}:{cfg.get('specificPR') or cfg.get('specificIssue')}")
    monkeypatch.setattr("diplomat_runtime.reviewaudit.audit", lambda *a: [])
    return _spawn_recorder(monkeypatch)


def _propose(store, github) -> str:
    """The grouper's goal, proposed and synced."""
    _github(github)
    store.run_goal_grouper_async(force=True)
    settle(store)
    [view] = [v for v in store.tracked_tasks if v["task"]["title"] == "iPhone Duo support"]
    return view["task"]["id"]


def _task(task_id: str) -> dict:
    return next(v for v in tasks.views(archived=True, me="alice")
                if v["task"]["id"] == task_id)


def _edits(monkeypatch) -> list[dict]:
    """Every ``tasks-edit`` op, as sent."""
    sent, real = [], tasks.edit
    monkeypatch.setattr(tasks, "edit", lambda op: sent.append(op) or real(op))
    return sent


def _expanded(store, task_id: str) -> tuple[TasksView, object]:
    view = TasksView(store)
    view.card(task_id).history_btn.click()
    return view, view.card(task_id)


# MARK: - the grouper


def test_the_tick_runs_the_grouper_once_due_and_reloads_and_syncs(tstore, github, claude,
                                                                  monkeypatch):
    _github(github)
    asked, real = [], tasks.group
    monkeypatch.setattr(tasks, "group", lambda *, if_due: asked.append(if_due) or real(
        if_due=if_due))
    tstore.run_goal_grouper_async()
    settle(tstore)
    assert asked == [True]
    assert len((claude / "claude-calls").read_text().splitlines()) == 1
    [view] = tstore.tracked_tasks
    assert (view["task"]["origin"], view["task"]["mode"]) == ("grouper", "proposed")
    # The new links' states: the sync a run that proposed something is followed by.
    assert view["linkStates"] == {ISSUE: "open", LONE: "open", PR: "open", STACKED: "open"}
    ran = tstore.tracker_grouped_at
    assert ran is not None and tstore.tracker_grouper_error is None
    assert not tstore.tracker_grouping

    # Not due again: the CLI says so, with the mark of the run that was.
    tstore.run_goal_grouper_async()
    settle(tstore)
    assert asked == [True, True]
    assert len((claude / "claude-calls").read_text().splitlines()) == 1
    assert abs((tstore.tracker_grouped_at - ran).total_seconds()) < 5


def test_never_two_groupers_at_once(tstore, monkeypatch):
    calls, release = [], threading.Event()

    def group(*, if_due):
        calls.append(if_due)
        release.wait(30)
        return {"ok": True, "created": 0, "added": 0}

    monkeypatch.setattr(tasks, "group", group)
    tstore.run_goal_grouper_async(force=True)
    view = TasksView(tstore)
    tstore.run_goal_grouper_async()
    tstore.run_goal_grouper_async(force=True)
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()
    assert not view.goals_btn.isEnabled() and "looking for goals…" in view.status.text()
    release.set()
    settle(tstore)
    assert calls == [False]
    assert view.goals_btn.isEnabled()


def test_a_failed_grouper_run_is_a_banner(tstore, github):
    _github(github)
    view = TasksView(tstore)
    view.goals_btn.click()
    settle(tstore)
    assert view.error_banner.text().startswith("⚠ Finding goals failed: claude could not be"
                                               " started")
    assert "goals looked for " in view.status.text()


def test_the_grouper_child_is_given_its_gh_call_and_claude(monkeypatch):
    monkeypatch.setattr(tasks, "tunables", lambda: {"grouper": {"timeoutSecs": 300}})
    assert tasks.group_timeout() == 2 * 65 + 1 + 300 + 5 + 2 * 5 + 30
    seen = []

    class Done:
        returncode, stdout, stderr = 0, '{"ok":true,"skipped":"not due"}', ""

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", lambda: "/bin/core")
    monkeypatch.setattr("subprocess.run",
                        lambda argv, **kw: seen.append((argv[1:], kw["timeout"])) or Done())
    assert tasks.group(if_due=True) == {"ok": True, "skipped": "not due"}
    assert tasks.group(if_due=False) == {"ok": True, "skipped": "not due"}
    assert seen == [(["tasks-group", "--if-due"], 476), (["tasks-group"], 476)]


# MARK: - the GOAL section


@pytest.mark.parametrize("button, mode", [("dismiss", "dismissed"), ("observe", "observe"),
                                          ("auto", "auto")])
def test_a_proposed_goal_offers_its_three_answers(tstore, github, claude, no_agents,
                                                  monkeypatch, button, mode):
    polls = []
    monkeypatch.setattr(type(tstore), "run_autofix_poll_async", lambda self: polls.append(1))
    task_id = _propose(tstore, github)
    view, card = _expanded(tstore, task_id)
    assert card.mode_chip.text() == "✦ PROPOSED"
    assert card.goal_line.text() == "Proposed from 4 open issues and PRs"
    assert list(card.goal_buttons) == ["dismiss", "observe", "auto"]
    assert [card.goal_buttons[k].text() for k in card.goal_buttons] == [
        "Dismiss", "Observe", "Automate"]
    sent = _edits(monkeypatch)
    card.goal_buttons[button].click()
    settle(tstore)
    assert sent[0] == {"op": "mode", "id": task_id, "mode": mode}
    if mode == "dismissed":
        assert all(v["task"]["id"] != task_id for v in tasks.views(archived=True))
        assert view.card(task_id) is None
        return
    assert _task(task_id)["task"]["mode"] == mode
    card = view.card(task_id)
    assert (card.mode_chip.text() if card.mode_chip else None) == (
        "↯ AUTO" if mode == "auto" else None)
    # Switching to auto starts its pass at once: the fix #1802 has no PR for yet.
    assert polls == ([1] if mode == "auto" else [])
    assert [r.key for r in tstore.requested_work] == (
        ["issues:1802"] if mode == "auto" else [])


def test_observe_auto_and_review_on_new_commits(tstore, github, no_agents, monkeypatch):
    monkeypatch.setattr(type(tstore), "run_autofix_poll_async", lambda self: None)
    _github(github, candidates=False)
    # A task the operator made: no Dismiss, and the same toggle.
    task_id = _create(tstore, "Duo", links=[ISSUE, PR])
    view, card = _expanded(tstore, task_id)
    assert card.mode_chip is None and card.goal_line is None
    assert list(card.goal_buttons) == ["observe", "auto"]
    assert "font-weight: 700" in card.goal_buttons["observe"].styleSheet()
    assert "font-weight: 700" not in card.goal_buttons["auto"].styleSheet()
    sent = _edits(monkeypatch)

    def click(key: str):
        view.card(task_id).goal_buttons[key].click()
        settle(tstore)
        return view.card(task_id)

    card = click("auto")
    assert sent[-1] == {"op": "mode", "id": task_id, "mode": "auto"}
    assert list(card.goal_buttons) == ["observe", "auto", "review"]
    assert card.goal_buttons["review"].text() == "Review on new commits"
    assert "font-weight: 700" in card.goal_buttons["auto"].styleSheet()
    assert "font-weight: 700" not in card.goal_buttons["review"].styleSheet()
    card = click("review")
    assert sent[-1] == {"op": "mode", "id": task_id, "reviewOnPush": True}
    assert _task(task_id)["task"]["reviewOnPush"] is True
    assert "font-weight: 700" in card.goal_buttons["review"].styleSheet()
    card = click("review")
    assert sent[-1] == {"op": "mode", "id": task_id, "reviewOnPush": False}
    assert "reviewOnPush" not in _task(task_id)["task"]
    card = click("observe")
    assert sent[-1] == {"op": "mode", "id": task_id, "mode": "observe"}
    assert list(card.goal_buttons) == ["observe", "auto"]


def test_a_grouper_goal_keeps_its_dismiss(tstore, github, claude):
    task_id = _propose(tstore, github)
    tasks.edit({"op": "mode", "id": task_id, "mode": "observe"})
    tstore.refresh_tracked_tasks()
    _, card = _expanded(tstore, task_id)
    assert list(card.goal_buttons) == ["observe", "auto", "dismiss"]


# MARK: - the graph


def _labels(card) -> list[list[str]]:
    return [[p.text.text() for p in row] for row in card.graph_rows]


def test_the_graph_is_one_line_per_row_and_a_pill_opens_its_item(tstore, github, claude,
                                                                 opened):
    from PySide6.QtWidgets import QLabel

    task_id = _propose(tstore, github)
    _, card = _expanded(tstore, task_id)
    assert _labels(card) == [["#1801", "#1810", "#1811"], ["#1802", "+"]]
    arrows = [w.text() for w in card.graph.widget().findChildren(QLabel) if w.text() == "→"]
    assert len(arrows) == 3
    assert [[p.state for p in row] for row in card.graph_rows] == [
        ["open", "open", "open"], ["open", None]]
    # One line high each: two rows are two pills tall, not a tree.
    pill = card.graph_rows[0][0].sizeHint().height()
    assert card.graph.height() < 2 * pill + 2 * 2 + 30
    for row in card.graph_rows:
        for pill_ in row[:-1] if row[-1].kind == "add" else row:
            pill_.mouseReleaseEvent(_left_click())
    assert opened == [ISSUE, PR, STACKED, LONE]


def _left_click():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    return QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(2, 2), QPointF(2, 2),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_the_plus_starts_a_fix_now_and_records_it(tstore, github, claude, no_agents):
    task_id = _propose(tstore, github)
    view, card = _expanded(tstore, task_id)
    plus = card.graph_rows[1][1]
    assert plus.node["work"] == f"fix:{_key(1802)}"
    assert "no fix asked yet" in plus.toolTip()
    plus.mouseReleaseEvent(_left_click())
    settle(tstore)
    # Started outside the cap, as the Fix-issues wizard's one-issue press starts it.
    assert [c["prompt"] for c in no_agents] == ["PROMPT:issues:1802"]
    [run] = agentregistry.load()
    assert (run.kind, run.pr_number, run.source) == ("issues", None, autofix.SOURCE_PANEL)
    assert run.label.startswith("Issues · #1802 · ")
    assert f"fix:{_key(1802)}" in _task(task_id)["task"]["dispatched"]
    plus = view.card(task_id).graph_rows[1][1]
    assert "fix asked 0m ago" in plus.toolTip()
    # The run is the badge now, and a second press starts nothing.
    assert (plus.badge, plus.text.text()) == ("agent", "AGENT")
    plus.mouseReleaseEvent(_left_click())
    settle(tstore)
    assert len(no_agents) == 1


def test_badges_are_the_overlay_sets(tstore, github, claude, no_agents, monkeypatch):
    task_id = _propose(tstore, github)
    monkeypatch.setattr(type(tstore), "goal_overlay", lambda self: GoalOverlay(
        agent_prs=frozenset({1810}), queued_prs=frozenset({1811}),
        agent_issues=frozenset({1801}), queued_issues=frozenset({1802})))
    _, card = _expanded(tstore, task_id)
    # An issue's own pill is never badged: its fix is the + after it.
    assert [[p.badge for p in row] for row in card.graph_rows] == [
        [None, "agent", "queued"], [None, "queued"]]
    assert [p.badge_chip.text() for p in card.graph_rows[0][1:]] == ["AGENT", "QUEUED"]
    assert not hasattr(card.graph_rows[0][0], "badge_chip")
    plus = card.graph_rows[1][1]
    assert plus.text.text() == "QUEUED" and "a fix is queued" in plus.toolTip()
    plus.mouseReleaseEvent(_left_click())
    settle(tstore)
    assert no_agents == []


def test_an_archived_goal_shows_its_graph_and_no_actions(tstore, github, claude, no_agents):
    task_id = _propose(tstore, github)
    tasks.edit({"op": "archive", "id": task_id})
    tstore.tracker_show_archived = True
    tstore.refresh_tracked_tasks()
    _, card = _expanded(tstore, task_id)
    assert card.goal_buttons == {}
    assert _labels(card) == [["#1801", "#1810", "#1811"], ["#1802", "+"]]
    card.graph_rows[1][1].mouseReleaseEvent(_left_click())
    settle(tstore)
    assert no_agents == []


def test_the_overlay_is_live_runs_and_queued_asks():
    def run(n, label, kind, *, pr=None, released=False):
        return agentstate.RunRecord(run_id=f"r{n}", dispatched_at=0, pr_number=pr,
                                    kind=kind, label=label, released=released)

    records = [
        run(1, "Auto · Review-req · #1810 (@hubert)", "review", pr=1810),
        run(2, "Auto · Issues · #1802 · deep · retry 2", "issues"),
        run(3, "Issues · #1803 · Quick", "issues"),
        # Ended, released, or not an issue fix: none is on anything.
        run(4, "Issues · #1804 · deep", "issues"),
        run(5, "Issues · #1805 · deep", "issues", released=True),
        run(6, "Review · #1806 · deep", "review", pr=1806),
        run(7, "Audit · Issues · #1807 · x", "audit"),
    ]
    states = {f"r{n}": agentstate.Resolution(f"r{n}", s, "") for n, s in (
        (1, agentstate.RUNNING), (2, agentstate.STARTING), (3, agentstate.AWAITING_INPUT),
        (4, agentstate.FINISHED), (5, agentstate.RUNNING), (6, agentstate.MERGED),
        (7, agentstate.RUNNING))}
    asks = [autofix.RequestedWork(1811, "", "", {"kind": "review"}),
            autofix.RequestedWork(1808, "", "", {"kind": "issues"})]
    queued = [autofix.QueuedTask("conflicts:1812", autofix.AgentJob(
        kind="conflicts", audit_action="conflicts", label="", prompt="", pr_number=1812), 1),
        autofix.QueuedTask("issues:1809", autofix.AgentJob(
            kind="issues", audit_action="issues", label="", prompt=""), 1)]
    assert goal_overlay(records, states, asks, queued) == GoalOverlay(
        agent_prs=frozenset({1810}), agent_issues=frozenset({1802, 1803}),
        queued_prs=frozenset({1811, 1812}), queued_issues=frozenset({1808, 1809}))


# MARK: - the automatic pass


def _auto_goal(store, github, mode: str | None) -> str:
    """The operator's goal over #1802 (no PR), #1810 (hubert's) and #1812 (mine), in
    ``mode`` with review on new commits."""
    _github(github, candidates=False)
    task_id = _create(store, "Duo", links=[LONE, PR, MINE, "https://github.com/o/r/issues/5"])
    if mode:
        tasks.edit({"op": "mode", "id": task_id, "mode": mode, "reviewOnPush": True})
    return task_id


def _poll(store, monkeypatch) -> None:
    """One monitor poll: my PR #1812 owes a reply on one thread; nothing else on
    GitHub."""
    monkeypatch.setattr(bans, "read", lambda: [])
    monkeypatch.setattr(autofixmonitor, "fetch_snapshots", lambda *a, **k: [PRSnapshot(
        number=1812, title="Duo docs", url=MINE, is_draft=False, mergeable="MERGEABLE",
        review_decision="", threads_unresolved=1, threads_i_owe=1, head_sha="aaa1812")])
    monkeypatch.setattr(autofixmonitor, "fetch_review_requests", lambda *a, **k: [])
    monkeypatch.setattr(autofixmonitor, "fetch_closed_prs", lambda *a, **k: set())
    monkeypatch.setattr(autofixmonitor, "fetch_open_issues", lambda *a, **k: [])
    store._autofix_poll_once()
    settle(store)
    assert store.autofix_poll_error is None


def test_an_auto_goal_queues_its_fixes_and_reviews_and_fixes_my_findings(
        tstore, github, no_agents, monkeypatch):
    tstore.pr_autofix_enabled = False
    task_id = _auto_goal(tstore, github, "auto")
    _poll(tstore, monkeypatch)
    asks = {r.key: r for r in tstore.requested_work}
    assert sorted(asks) == ["issues:1802", "review:1810", "review:1812"]
    fix = asks["issues:1802"]
    assert (fix.url, fix.author, fix.config["target"], fix.config["specificIssue"],
            fix.config["includeFeatures"]) == (LONE, "kacper", "specific", "1802", True)
    review = asks["review:1810"]
    assert (review.author, review.config["depth"], review.config["specificPR"],
            review.config["leaveReviews"], review.config["specificAuthor"]) == (
        "hubert", "max", "1810", True, "theirs")
    # My own PR is reviewed as mine, and no author is banned-checked for it.
    assert (asks["review:1812"].author, asks["review:1812"].config["specificAuthor"]) == ("", "mine")
    # My PR's threads: the my-review path, though the PR auto-fix toggle is off.
    assert [c["prompt"] for c in no_agents] == ["PROMPT:review:1812"]
    assert [r.label for r in agentregistry.load()] == ["Auto · Review · #1812"]
    assert sorted(_task(task_id)["task"]["dispatched"]) == [
        f"fix:{_key(1802)}", f"review:{_key(1810)}@aaa1810", f"review:{_key(1812)}@aaa1812"]
    assert _task(task_id)["work"] == [{"kind": "findings", "url": MINE, "number": 1812,
                                       "author": "alice", "key": f"findings:{_key(1812)}"}]
    # The next poll asks for nothing again (and drains what it asked for).
    lines, asked, request = len(tasks_events()), [], tstore._request
    monkeypatch.setattr(tstore, "_request", lambda a: asked.append(a) or request(a))
    _poll(tstore, monkeypatch)
    assert asked == [] and len(tasks_events()) == lines


def tasks_events() -> list[str]:
    from pathlib import Path

    return (Path(os.environ["DIPLOMAT_TASKS_DIR"]) / "events.jsonl").read_text().splitlines()


@pytest.mark.parametrize("mode", [None, "observe"])
def test_an_observed_goal_queues_nothing(tstore, github, no_agents, monkeypatch, mode):
    tstore.pr_autofix_enabled = False
    task_id = _auto_goal(tstore, github, mode)
    _poll(tstore, monkeypatch)
    assert tstore.requested_work == [] and no_agents == []
    # The monitor's own find, held by the toggle as ever.
    assert [t.id for t in tstore.queued_tasks] == ["review-reply:1812"]
    assert "dispatched" not in _task(task_id)["task"]


def test_a_proposed_goal_queues_nothing(tstore, github, claude, no_agents, monkeypatch):
    tstore.pr_autofix_enabled = False
    task_id = _propose(tstore, github)
    tasks.edit({"op": "link", "id": task_id, "url": MINE})
    _poll(tstore, monkeypatch)
    assert tstore.requested_work == [] and no_agents == []
    assert "dispatched" not in _task(task_id)["task"]


def test_an_ask_whose_prompt_fails_is_left_for_the_next_pass(tstore, github, no_agents,
                                                             monkeypatch):
    from diplomat_runtime import activity, promptcore

    logged = []
    monkeypatch.setattr(activity, "log", lambda *a: logged.append(a))
    task_id = _auto_goal(tstore, github, "auto")
    built = promptcore.build_prompt

    def wedged(cfg):
        if cfg.get("specificPR") == "1810":
            raise RuntimeError("diplomat-core prompt failed: wedged")
        return built(cfg)

    monkeypatch.setattr(promptcore, "build_prompt", wedged)
    _poll(tstore, monkeypatch)
    assert sorted(r.key for r in tstore.requested_work) == ["issues:1802", "review:1812"]
    assert sorted(_task(task_id)["task"]["dispatched"]) == [
        f"fix:{_key(1802)}", f"review:{_key(1812)}@aaa1812"]
    assert ("auto", "goal", f"could not queue review:{_key(1810)}@aaa1810: "
            "diplomat-core prompt failed: wedged") in logged


def test_a_dispatched_record_that_fails_is_logged_not_fatal(tstore, github, no_agents,
                                                           monkeypatch):
    from diplomat_runtime import activity

    logged = []
    monkeypatch.setattr(activity, "log", lambda *a: logged.append(a))
    _auto_goal(tstore, github, "auto")
    real = tasks.edit

    def refuse(op):
        if op["op"] == "dispatched":
            raise RuntimeError("diplomat-core tasks-edit failed: locked")
        return real(op)

    monkeypatch.setattr(tasks, "edit", refuse)
    _poll(tstore, monkeypatch)
    assert sorted(r.key for r in tstore.requested_work) == [
        "issues:1802", "review:1810", "review:1812"]
    assert ("auto", "goal", "could not record " + ", ".join([
        f"fix:{_key(1802)}", f"review:{_key(1810)}@aaa1810", f"review:{_key(1812)}@aaa1812"])
        + ": diplomat-core tasks-edit failed: locked") in logged
