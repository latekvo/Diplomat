"""Comms in the Linux Store: its settings, the Slack token, and what a triaged ping
does to the queue.

The tick itself is the core's (``test_comms_core.py`` drives it through the binary);
here its answer is a literal, so what is under test is the front-end's half: the
plan comes from the real ``diplomat-core comms-effect``, and the Store acts on it
through the same ``_request`` a sweep uses, the queue bands, the ban list and the
events log the real ``comms-feed`` folds.

The store fixture and the spawn recorder come from ``test_autofix``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_autofix import _spawn_recorder, store  # noqa: F401 — fixture
from test_comms_core import _stub_claude, slack  # noqa: F401 — fixture

from diplomat_app import bans, issues
from diplomat_app.store import Store, app_settings
from diplomat_runtime import activity, appconfig, autofix, comms, gh, review
from diplomat_runtime.models import OpenIssue, OpenPR

pytest.importorskip("PySide6")

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
needs_core = pytest.mark.skipif(not CORE_BIN, reason="DIPLOMAT_CORE_BIN not set")

NOW = datetime.now(timezone.utc)
ME = {"userID": "UME", "teamID": "TOURS", "handle": "operator"}


def _pr(number: int, author: str) -> OpenPR:
    return OpenPR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}",
                  False, author, NOW, None, [], None, [])


def _issue(number: int, author: str = "carol") -> OpenIssue:
    return OpenIssue(number, f"Issue {number}", f"https://github.com/o/r/issues/{number}",
                     author, "CONTRIBUTOR", NOW, NOW, 0, [], [], False)


def _message(n: int, text: str = "hello", *, user: str = "kacper", dm: bool = True) -> dict:
    ts = f"1761580{n:03d}.000100"
    channel = "D1" if dm else "C1"
    return {"id": f"{channel}:{ts}", "channelID": channel,
            "channelName": "UK" if dm else "general", "isDM": dm, "userID": "UK",
            "userName": user, "teamID": "TOURS", "text": text, "ts": ts,
            "permalink": f"https://ours.slack.com/archives/{channel}/p{ts.replace('.', '')}",
            "reasons": ["dm"] if dm else ["mention"]}


def _decision(action: str, number: int = 0, summary: str = "asks something") -> dict:
    return {"action": action, "number": number, "summary": summary, "reason": "r"}


def _ask(n: int, action: str, number: int) -> tuple[dict, dict]:
    """A ping asking for ``action`` on ``#number``, in words that name the number."""
    return _message(n, f"{action} #{number} please"), _decision(action, number)


def _events() -> list[dict]:
    """The lines this front-end wrote: the core's own ping/decision lines left out."""
    path = comms.events_path()
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in lines if e["kind"] not in ("ping", "decision")]


def _feed_lines() -> list[dict]:
    return [json.loads(line) for line in activity.audit_path().read_text().splitlines()]


def _no_such_number(args, timeout=60.0):
    """How gh 2.x fails a `pr view` or `issue view` of a number GitHub does not know."""
    what = "a PullRequest" if args[0] == "pr" else "an issue or pull request"
    raise gh.GHError(f"gh exited 1: GraphQL: Could not resolve to {what} with the number "
                     f"of {args[2]}. (repository.{'pullRequest' if args[0] == 'pr' else 'issue'})")


@pytest.fixture
def comms_store(store, monkeypatch):  # noqa: F811 — the shared Store fixture
    """The store with a panel fetch behind it — bob's PR #12, my own #13, mallory's
    #14 — one issue, a token in the environment, and mallory banned. GitHub knows
    nothing the panel did not fetch, unless a test says otherwise."""
    monkeypatch.setattr("diplomat_runtime.gh.run", _no_such_number)
    store.has_loaded = True
    store.prs = [_pr(12, "bob"), _pr(13, "alice"), _pr(14, "mallory")]
    store.issues = [_issue(7)]
    monkeypatch.setattr("diplomat_app.bans.read",
                        lambda: [bans.BannedAuthor("mallory", "prompt injection")])
    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-test")
    store.comms_enabled = True
    return store


def _tick(store, monkeypatch, *pings, error=None, notices=(), between=None) -> dict:
    """One poll whose ``comms-tick`` answers ``pings`` (``(message, decision)`` pairs)
    and ``notices``, applied as the Qt thread applies it, with ``between`` run after
    the worker's half and before the Qt thread's. Returns what the tick was handed."""
    handed: dict = {}

    def fake_tick(settings, token):
        handed.update(settings=settings, token=token)
        # The two lines the core's tick logs for each ping it hands back.
        comms.comms_dir().mkdir(parents=True, exist_ok=True)
        with open(comms.events_path(), "a") as f:
            for m, d in pings:
                for kind, extra in (("ping", {"message": m}), ("decision", {"decision": d})):
                    f.write(json.dumps({"at": comms.iso(), "kind": kind, "id": m["id"],
                                        **extra}) + "\n")
        return {"me": ME, "items": [{"message": m, "decision": d} for m, d in pings],
                "error": error, "notices": list(notices)}

    monkeypatch.setattr(comms, "tick", fake_tick)
    plan = store._comms_poll_once()
    if between is not None:
        between()
    store._apply_comms_tick(plan)
    return handed


def _sweep_review(store, *numbers):
    store.prs = store.prs + [_pr(n, "alice") for n in numbers if n not in
                             {p.number for p in store.prs}]
    store.request_review_sweep(review.ReviewConfig(
        target=review.PRTarget.MINE, me="alice", include_drafts=True, include_ready=True))


# MARK: - settings


def test_every_setting_round_trips_under_the_contract_key():
    st = Store()
    assert (st.comms_enabled, st.comms_keywords_raw, st.comms_sender_allowlist_raw,
            st.comms_act_requests, st.comms_act_bumps, st.comms_act_investigations,
            st.comms_notify, st.comms_model, st.comms_seen_at) == (
        False, "", "", True, True, True, True, "haiku", "")
    st.comms_enabled = True
    st.comms_keywords_raw = "argent, release train"
    st.comms_sender_allowlist_raw = "@kacper U2"
    st.comms_act_requests = False
    st.comms_act_bumps = False
    st.comms_act_investigations = False
    st.comms_notify = False
    st.comms_model = "sonnet"
    st.comms_seen_at = "2026-10-27T10:00:00Z"
    st.slack_token_file = " /run/me/slack "
    raw = app_settings()
    assert {k: raw.value(k) for k in (
        "commsEnabled", "commsKeywordsRaw", "commsSenderAllowlistRaw", "commsActRequests",
        "commsActBumps", "commsActInvestigations", "commsNotify", "commsModel",
        "commsSeenAt")} == {
        "commsEnabled": True, "commsKeywordsRaw": "argent, release train",
        "commsSenderAllowlistRaw": "@kacper U2", "commsActRequests": False,
        "commsActBumps": False, "commsActInvestigations": False, "commsNotify": False,
        "commsModel": "sonnet", "commsSeenAt": "2026-10-27T10:00:00Z"}
    # The token's path is in the shared config, the token itself nowhere.
    assert appconfig.read()["slackTokenFile"] == "/run/me/slack"
    again = Store()
    assert (again.comms_enabled, again.comms_model, again.comms_act_bumps,
            again.slack_token_file) == (True, "sonnet", False, "/run/me/slack")


def test_the_tick_is_handed_the_settings_and_the_token(comms_store, monkeypatch):
    comms_store.comms_keywords_raw = "argent"
    comms_store.comms_sender_allowlist_raw = "kacper"
    comms_store.comms_model = "sonnet"
    handed = _tick(comms_store, monkeypatch)
    assert handed["token"] == "xoxp-test"
    assert handed["settings"]["keywordsRaw"] == "argent"
    assert handed["settings"]["senderAllowlistRaw"] == "kacper"
    assert handed["settings"]["model"] == "sonnet"
    assert handed["settings"]["login"] == "alice"
    assert comms_store.comms_identity == ME
    assert comms_store.comms_last_poll is not None


def test_the_token_reaches_the_tick_child_alone(monkeypatch):
    """The token goes into the ``comms-tick`` child's environment, and no other
    ``diplomat-core`` run inherits it from this process."""
    seen = []

    class Done:
        returncode, stdout, stderr = 0, '{"me": null, "items": [], "error": null}', ""

    def fake_run(argv, **kw):
        seen.append((argv[1], kw["env"].get(comms.TOKEN_ENV)))
        return Done()

    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-from-env")
    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", lambda: "/bin/core")
    monkeypatch.setattr("subprocess.run", fake_run)
    comms.tick({}, "xoxp-handed")
    comms._core(["comms-feed"])
    assert seen == [("comms-tick", "xoxp-handed"), ("comms-feed", None)]


def test_a_ticks_notices_are_handed_on_and_default_to_none(fake_core):
    tick = {"me": None, "items": [], "error": None}
    fake_core({**tick, "notices": ["Comms caught up past 24 h", 7]})
    assert comms.tick({}, "xoxp")["notices"] == ["Comms caught up past 24 h"]
    for body in (tick, {**tick, "notices": None}, {**tick, "notices": "one"}):
        fake_core(body)
        assert comms.tick({}, "xoxp")["notices"] == []
    for body, code in (("not json", 0), ('{"items": 1}', 0), ("", 3)):
        fake_core(body, code)
        assert comms.tick({}, "xoxp")["notices"] == []


def test_each_notice_of_a_tick_is_one_line_of_the_activity_feed(comms_store, monkeypatch):
    refreshed = []
    monkeypatch.setattr(comms_store, "refresh_activity", lambda: refreshed.append(1))
    notices = ["Comms was away 30 h: messages from before 2026-10-26 09:00 were not triaged",
               "A search for argent had more than 3 pages; older matches went unread"]
    _tick(comms_store, monkeypatch, notices=notices)
    assert [(e["source"], e["action"], e["detail"]) for e in _feed_lines()] == [
        ("auto", "comms-notice", n) for n in notices]
    assert refreshed, "the feed is re-read"
    assert comms_store.comms_error is None, "a notice is not an error"
    assert _events() == []
    _tick(comms_store, monkeypatch)
    assert len(_feed_lines()) == 2


# MARK: - the token file


def test_the_token_file_is_read_like_the_agent_token(tmp_path, monkeypatch):
    path = tmp_path / "slack-token"

    def use(p):
        appconfig.set_value(appconfig.SLACK_TOKEN_FILE, str(p))

    use(path)
    assert comms.read_token() == ("", f"no Slack token at {path}")
    path.write_text("  \n\0 ")
    assert comms.read_token() == ("", f"{path} is empty")
    path.write_text("xoxp-1-2-3\n")
    assert comms.read_token() == ("xoxp-1-2-3", "")
    use(tmp_path)
    assert comms.read_token() == ("", f"{tmp_path} is not a regular file")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    use(fifo)
    assert comms.read_token() == ("", f"{fifo} is not a regular file")
    use("relative/token")
    assert comms.read_token() == ("", "slackTokenFile is not an absolute path: relative/token")
    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-env")
    assert comms.read_token() == ("xoxp-env", "")


def test_the_default_token_file_is_under_the_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert comms.token_file() == str(tmp_path / ".diplomat" / "slack-token")


def test_no_token_is_the_ticks_error_and_runs_nothing(comms_store, monkeypatch):
    monkeypatch.delenv(comms.TOKEN_ENV)
    appconfig.set_value(appconfig.SLACK_TOKEN_FILE, "/nonexistent/slack-token")
    monkeypatch.setattr(comms, "tick", lambda *a: pytest.fail("ran the tick"))
    comms_store._apply_comms_tick(comms_store._comms_poll_once())
    assert comms_store.comms_error == "no Slack token at /nonexistent/slack-token"


# MARK: - what a triaged ping does


@needs_core
def test_a_review_ask_is_queued_at_the_front_of_band_1(comms_store, monkeypatch):
    _sweep_review(comms_store, 1, 2)
    comms_store.request_issue_sweep(issues.IssueConfig(target=issues.Target.ALL, me="alice"))
    assert [t.id for t in comms_store.queued_tasks] == ["review:13", "review:1", "review:2",
                                                        "issues:7"]
    m = _message(1, "can you review #12?")
    _tick(comms_store, monkeypatch, (m, _decision("review", 12)))

    assert [t.id for t in comms_store.queued_tasks] == [
        "review:12", "review:13", "review:1", "review:2", "issues:7"]
    ask = next(r for r in comms_store.requested_work if r.key == "review:12")
    # The Review wizard's single-PR default for someone else's PR.
    assert (ask.author, ask.url) == ("bob", "https://github.com/o/r/pull/12")
    assert ask.config["specificPR"] == "12"
    assert ask.config["target"] == "specific"
    assert ask.config["specificAuthor"] == "theirs"
    assert ask.config["depth"] == review.default_depth_id()
    action = [e for e in _events() if e["kind"] == "action"]
    assert action == [{"at": action[0]["at"], "id": m["id"], "kind": "action",
                       "label": ask.label, "queueKey": "review:12",
                       "outcome": "review of #12 queued, at the front of its band"}]
    feed = [(e["action"], e["detail"]) for e in _feed_lines()]
    assert feed == [
        ("comms-ping", "DM from kacper: can you review #12?"),
        ("comms-review", f"{ask.label} — review of #12 queued, at the front of its band;"
                         " asked by kacper in DM")]
    assert comms_store.comms_items[0]["queueKey"] == "review:12"


@needs_core
def test_a_review_of_my_own_pr_is_the_mine_disposition(comms_store, monkeypatch):
    _tick(comms_store, monkeypatch, _ask(1, "review", 13))
    ask = comms_store.requested_work[0]
    assert (ask.key, ask.author, ask.config["specificAuthor"]) == ("review:13", "", "mine")


@needs_core
def test_an_ask_already_queued_keeps_its_ask_and_moves_up(comms_store, monkeypatch):
    _sweep_review(comms_store, 1, 13)
    _tick(comms_store, monkeypatch, _ask(1, "review", 13))
    assert [t.id for t in comms_store.queued_tasks][:1] == ["review:13"]
    assert [r.key for r in comms_store.requested_work].count("review:13") == 1
    assert _events()[-1]["outcome"] == \
        "review of #13 was already queued, at the front of its band"


@needs_core
def test_a_fix_ask_is_queued_at_the_front_of_band_2(comms_store, monkeypatch):
    comms_store.issues = [_issue(31), _issue(7)]
    comms_store.request_issue_sweep(issues.IssueConfig(target=issues.Target.ALL, me="alice"))
    _sweep_review(comms_store, 1)
    assert [t.id for t in comms_store.queued_tasks] == [
        "review:13", "review:1", "issues:31", "issues:7"]
    _tick(comms_store, monkeypatch, (_message(1, "please fix #7"), _decision("fix_issue", 7)))
    assert [t.id for t in comms_store.queued_tasks] == [
        "review:13", "review:1", "issues:7", "issues:31"]
    assert _events()[-1]["outcome"] == "fix of issue #7 was already queued, at the front of its band"


@needs_core
def test_a_fix_for_an_issue_the_panel_did_not_fetch_is_asked_of_github(comms_store,
                                                                         monkeypatch):
    asked = []

    def gh(args, timeout=60.0):
        asked.append(args)
        state = {"8": "OPEN", "9": "CLOSED"}.get(args[2])
        if state is None:
            _no_such_number(args)
        return json.dumps({"number": int(args[2]), "state": state, "author": {"login": "dana"},
                           "url": f"https://github.com/o/r/issues/{args[2]}"}).encode()

    monkeypatch.setattr("diplomat_runtime.gh.run", gh)
    _tick(comms_store, monkeypatch, _ask(1, "fix_issue", 8), _ask(2, "fix_issue", 9),
          _ask(3, "fix_issue", 10))

    assert [a[:3] for a in asked] == [["issue", "view", "8"], ["issue", "view", "9"],
                                      ["issue", "view", "10"]]
    assert asked[0][3:] == ["--repo", "software-mansion/argent", "--json",
                            "number,url,author,state"]
    ask = comms_store.requested_work[0]
    assert (ask.key, ask.author, ask.config["specificIssue"], ask.config["target"]) == (
        "issues:8", "dana", "8", "specific")
    assert [r.key for r in comms_store.requested_work] == ["issues:8"]
    outcomes = [e["outcome"] for e in _events() if e["kind"] == "action"]
    assert outcomes[1:] == ["issue #9 is closed", "#10 is not an issue of this repo"]


@needs_core
def test_a_bump_moves_everything_queued_for_that_number(comms_store, monkeypatch):
    comms_store.issues = [_issue(31), _issue(2)]
    comms_store.request_issue_sweep(issues.IssueConfig(target=issues.Target.ALL, me="alice"))
    _sweep_review(comms_store, 1, 2)
    assert [t.id for t in comms_store.queued_tasks] == [
        "review:13", "review:1", "review:2", "issues:31", "issues:2"]
    before = list(comms_store.requested_work)

    _tick(comms_store, monkeypatch, (_message(1, "any news on #2?"), _decision("bump", 2)),
          _ask(2, "bump", 99))

    assert [t.id for t in comms_store.queued_tasks] == [
        "review:2", "review:13", "review:1", "issues:2", "issues:31"]
    assert comms_store.requested_work == before, "a bump asks for nothing new"
    outcomes = [e["outcome"] for e in _events() if e["kind"] == "action"]
    assert outcomes == ["moved review:2, issues:2 to the front of its band",
                        "nothing queued for #99"]
    assert all("queueKey" not in e for e in _events())


@needs_core
def test_an_investigation_is_band_3_and_outlives_a_restart(comms_store, monkeypatch):
    _sweep_review(comms_store, 1)
    m = _message(1, "the screenshot tool hangs on Android 14")
    _tick(comms_store, monkeypatch, (m, _decision("investigate", 0, "screenshot hangs")))

    number = int(m["ts"].replace(".", ""))
    key = f"investigate:{number}"
    assert [t.id for t in comms_store.queued_tasks] == ["review:13", "review:1", key]
    assert autofix.queue_band(key) == 3
    task = comms_store.queued_tasks[-1]
    assert task.job.label == "Investigate · Slack · kacper · screenshot hangs"
    assert (task.job.pr_url, task.job.pr_number, task.job.kind) == (None, None, "investigate")

    again = Store()
    ask = next(r for r in again.requested_work if r.key == key)
    assert ask.config["kind"] == "investigate"
    assert ask.config["reportPath"] == str(comms.reports_dir() / f"{number}.md")
    assert ask.url == m["permalink"]
    assert _events()[-1]["queueKey"] == key
    assert _events()[-1]["outcome"] == "investigation queued"


@needs_core
@pytest.mark.parametrize("toggle, action, number", [
    ("comms_act_requests", "review", 12),
    ("comms_act_requests", "fix_issue", 7),
    ("comms_act_bumps", "bump", 13),
    ("comms_act_investigations", "investigate", 0),
])
def test_each_switch_turns_its_kind_into_a_ping_alone(comms_store, monkeypatch, toggle,
                                                      action, number):
    _sweep_review(comms_store, 1)
    order = [t.id for t in comms_store.queued_tasks]
    asks = list(comms_store.requested_work)
    setattr(comms_store, toggle, False)

    _tick(comms_store, monkeypatch, _ask(1, action, number))

    assert [t.id for t in comms_store.queued_tasks] == order
    assert comms_store.requested_work == asks
    switch = {"comms_act_requests": "requests", "comms_act_bumps": "bumps",
              "comms_act_investigations": "investigations"}[toggle]
    assert _events()[-1]["outcome"] == \
        f"{action} not acted on: acting on {switch} is switched off"
    assert [e["action"] for e in _feed_lines()] == ["comms-ping"]


@needs_core
def test_a_pr_that_is_not_open_queues_nothing(comms_store, monkeypatch):
    _tick(comms_store, monkeypatch, _ask(1, "review", 99))
    assert comms_store.requested_work == []
    assert comms_store.queued_tasks == []
    assert _events()[-1]["outcome"] == "#99 is not a PR of this repo"
    assert [e["action"] for e in _feed_lines()] == ["comms-ping", "comms-review"]


@needs_core
def test_a_banned_author_is_never_queued(comms_store, monkeypatch):
    comms_store.issues = [_issue(7, author="Mallory")]
    _spawn_recorder(monkeypatch)
    _tick(comms_store, monkeypatch, _ask(1, "review", 14), _ask(2, "fix_issue", 7))
    assert comms_store.requested_work == []
    assert comms_store.queued_tasks == []
    outcomes = [e["outcome"] for e in _events() if e["kind"] == "action"]
    assert outcomes == ["PR #14 is by @mallory, who is banned; not queued",
                        "issue #7 is by @Mallory, who is banned; not queued"]


@needs_core
def test_a_ping_the_triage_left_alone_is_recorded_as_such(comms_store, monkeypatch):
    _tick(comms_store, monkeypatch, (_message(1, "thanks!"), _decision("none")))
    assert comms_store.queued_tasks == []
    assert _events()[-1]["outcome"] == "no action"
    assert comms_store.comms_items[0]["outcome"] == "no action"


# MARK: - started / finished


@needs_core
def test_a_comms_run_records_when_it_started_and_finished(comms_store, monkeypatch):
    _spawn_recorder(monkeypatch)
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))
    _sweep_review(comms_store, 1)
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:1")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert [e["kind"] for e in _events()] == ["action"], "not a Comms ask"

    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    started = _events()[-1]
    assert (started["kind"], started["id"], started["label"]) == (
        "started", m["id"], "Review · #12 · " + review.default_depth_id())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", started["at"])
    comms_store.refresh_comms_feed()

    running = [autofix.RunningAgent(12, started["label"], "review", True)]
    monkeypatch.setattr(Store, "running_tasks", property(lambda s: running))
    comms_store._note_comms_runs()
    assert _events()[-1]["kind"] == "started", "still running"

    running.clear()
    comms_store._note_comms_runs()
    assert [e["kind"] for e in _events()][-2:] == ["started", "finished"]
    comms_store.refresh_comms_feed()
    assert comms_store.comms_items[0]["finishedAt"]
    comms_store._note_comms_runs()
    assert [e["kind"] for e in _events()].count("finished") == 1


def test_a_run_back_in_the_queue_is_not_finished(comms_store, monkeypatch):
    comms_store.comms_items = [{"message": {"id": "D1:1.1"}, "queueKey": "review:12",
                                "label": "Review · #12 · deep", "startedAt": "x", "at": "x"}]
    monkeypatch.setattr(Store, "running_tasks", property(lambda s: []))
    comms_store.queued_tasks = [autofix.QueuedTask(
        id="review:12", job=autofix.AgentJob(kind="review", audit_action="review",
                                             label="Review · #12 · deep", prompt="p"),
        attempt=1)]
    comms_store._note_comms_runs()
    assert _events() == []
    comms_store.queued_tasks = []
    comms_store._note_comms_runs()
    assert [e["kind"] for e in _events()] == ["finished"]


# MARK: - the events Python writes, as the core folds them


@needs_core
def test_the_lines_python_writes_fold_through_the_real_cli():
    m = _message(4, "screenshot hangs")
    comms.comms_dir().mkdir(parents=True)
    t0 = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    with open(comms.events_path(), "w") as f:
        # The two lines the core's tick writes.
        f.write(json.dumps({"at": "2026-10-27T10:00:00Z", "kind": "ping", "id": m["id"],
                            "message": m}) + "\n")
        f.write(json.dumps({"at": "2026-10-27T10:00:00Z", "kind": "decision", "id": m["id"],
                            "decision": _decision("investigate")}) + "\n")
    assert comms.append_event("action", m["id"], outcome="investigation queued",
                              queue_key="investigate:1", label="Investigate · Slack · x",
                              when=t0 + timedelta(seconds=1))
    assert comms.append_event("started", m["id"], label="Investigate · Slack · x",
                              when=t0 + timedelta(minutes=2))
    assert comms.append_event("finished", m["id"], label="Investigate · Slack · x",
                              when=t0 + timedelta(minutes=9, microseconds=500))
    [item] = comms.feed()
    assert item["message"] == m
    assert (item["outcome"], item["queueKey"], item["label"]) == (
        "investigation queued", "investigate:1", "Investigate · Slack · x")
    assert (item["at"], item["startedAt"], item["finishedAt"]) == (
        "2026-10-27T10:00:00Z", "2026-10-27T10:02:00Z", "2026-10-27T10:09:00Z")


# MARK: - unread, errors, notifications


def test_unread_counts_the_pings_after_the_screen_was_last_opened(comms_store):
    comms_store.comms_items = [{"at": "2026-10-27T10:05:00Z"}, {"at": "2026-10-27T10:03:00Z"},
                               {"at": "2026-10-27T10:01:00Z"}]
    assert comms_store.comms_unread == 3
    comms_store.comms_seen_at = "2026-10-27T10:03:00Z"
    assert comms_store.comms_unread == 1
    fired = []
    comms_store.comms_changed.connect(lambda: fired.append(1))
    comms_store.mark_comms_seen()
    assert comms_store.comms_unread == 0
    assert fired == [1]


def test_an_error_is_audited_once_on_the_way_in_and_once_out(comms_store, monkeypatch):
    for _ in range(2):
        _tick(comms_store, monkeypatch, error="Slack rejected the token (invalid_auth)")
    assert comms_store.comms_error == "Slack rejected the token (invalid_auth)"
    _tick(comms_store, monkeypatch)
    assert comms_store.comms_error is None
    assert [(e["action"], e["detail"]) for e in _feed_lines()] == [
        ("comms-error", "Slack rejected the token (invalid_auth)"),
        ("comms-recovered", "Slack watcher working again as @operator")]


@needs_core
def test_new_pings_notify_only_while_notify_is_on(comms_store, monkeypatch):
    shown = []
    comms_store.comms_pinged.connect(lambda t, b: shown.append((t, b)))
    _tick(comms_store, monkeypatch, (_message(1, "can you review #12?"), _decision("none")))
    _tick(comms_store, monkeypatch)
    # One notification per ping, as CommsNotifier.post does, however many a tick found.
    _tick(comms_store, monkeypatch, (_message(3, "any news?", user="ola"), _decision("none")),
          (_message(4, "@me look\nat this", dm=False), _decision("none")))
    comms_store.comms_notify = False
    _tick(comms_store, monkeypatch, (_message(2), _decision("none")))
    assert shown == [("Slack · DM from kacper", "can you review #12?"),
                     ("Slack · DM from ola", "any news?"),
                     ("Slack · #general from kacper", "@me look at this")]


def test_the_poll_does_nothing_while_comms_is_off(comms_store, monkeypatch):
    comms_store.comms_enabled = False
    monkeypatch.setattr(comms_store, "start_background", lambda *a, **k: pytest.fail("polled"))
    comms_store.run_comms_poll_async()


# MARK: - what GitHub is asked when the panel's fetch does not hold the number


def _github(answers: dict, asked: list):
    """A ``gh`` that knows ``answers``: ``(verb, number) -> (state, url, author)``."""

    def run(args, timeout=60.0):
        asked.append(args)
        hit = answers.get((args[0], int(args[2])))
        if hit is None:
            _no_such_number(args)
        state, url, author = hit
        return json.dumps({"number": int(args[2]), "state": state, "url": url,
                           "author": {"login": author}}).encode()

    return run


@needs_core
def test_a_review_of_a_pr_the_panel_did_not_fetch_is_asked_of_github(comms_store,
                                                                     monkeypatch):
    asked: list = []
    pull = "https://github.com/o/r/pull/{}".format
    monkeypatch.setattr("diplomat_runtime.gh.run", _github({
        ("pr", 500): ("OPEN", pull(500), "dana"),
        ("pr", 501): ("MERGED", pull(501), "dana"),
        ("pr", 503): ("OPEN", pull(503), "Mallory"),
    }, asked))
    _tick(comms_store, monkeypatch, _ask(1, "review", 500), _ask(2, "review", 501),
          _ask(3, "review", 502), _ask(4, "review", 503), _ask(5, "review", 12))

    # #12 is in the panel's fetch, so only the other four are asked about.
    assert [a[:3] for a in asked] == [["pr", "view", "500"], ["pr", "view", "501"],
                                      ["pr", "view", "502"], ["pr", "view", "503"]]
    assert asked[0][3:] == ["--repo", "software-mansion/argent", "--json",
                            "number,url,author,state"]
    assert [r.key for r in comms_store.requested_work] == ["review:500", "review:12"]
    ask = comms_store.requested_work[0]
    assert (ask.author, ask.url, ask.config["specificAuthor"]) == ("dana", pull(500), "theirs")
    outcomes = [e["outcome"] for e in _events() if e["kind"] == "action"]
    assert outcomes == ["review of #500 queued, at the front of its band",
                        "PR #501 is merged",
                        "#502 is not a PR of this repo",
                        "PR #503 is by @Mallory, who is banned; not queued",
                        "review of #12 queued, at the front of its band"]


@needs_core
def test_a_fix_of_a_pull_requests_number_queues_nothing(comms_store, monkeypatch):
    """``gh issue view`` answers a PR's number with the PR, state OPEN: the url is
    what says it is not an issue."""
    asked: list = []
    monkeypatch.setattr("diplomat_runtime.gh.run", _github({
        ("issue", 42): ("OPEN", "https://github.com/o/r/pull/42", "dana"),
    }, asked))
    _tick(comms_store, monkeypatch, _ask(1, "fix_issue", 42))
    assert [a[:3] for a in asked] == [["issue", "view", "42"]]
    assert comms_store.requested_work == []
    assert comms_store.queued_tasks == []
    assert _events()[-1]["outcome"] == "#42 is a pull request, not an issue"


@needs_core
def test_a_gh_that_could_not_ask_is_not_read_as_no_such_number(comms_store, monkeypatch):
    """A PR opened since the last fetch, asked about while gh is down or logged out:
    GitHub never answered, so the ping must not say the PR does not exist."""
    unauthed = ("gh exited 1: HTTP 401: Bad credentials (https://api.github.com/graphql)\n"
                "Try authenticating with:  gh auth login -h github.com")

    def down(args, timeout=60.0):
        raise gh.GHError("gh timed out after 30s" if args[2] == "600" else unauthed)

    monkeypatch.setattr("diplomat_runtime.gh.run", down)
    _tick(comms_store, monkeypatch, _ask(1, "review", 600), _ask(2, "fix_issue", 601))
    assert comms_store.requested_work == [] and comms_store.queued_tasks == []
    assert [e["outcome"] for e in _events() if e["kind"] == "action"] == [
        "couldn't ask GitHub about PR #600 (gh timed out after 30s)",
        "couldn't ask GitHub about issue #601 "
        f"({Store.comms_snippet(unauthed, 120)})"]
    assert "Bad credentials" in Store.comms_snippet(unauthed, 120)


# MARK: - the folded log, read from more than one thread


@needs_core
def test_a_feed_read_before_a_pings_line_never_lands_after_it(comms_store, monkeypatch):
    """The agents' settle re-reads the log on a worker thread. A read taken before a
    ping's `action` line that assigned its fold after the Qt thread's own refresh
    would leave the ping owning nothing, and its review's dispatch would record no
    start for it."""
    _spawn_recorder(monkeypatch)
    real_feed = comms.feed
    read_early, go = threading.Event(), threading.Event()

    def feed(limit=200):
        items = real_feed(limit)
        if threading.current_thread().name == "settle":
            read_early.set()
            go.wait(5)
        return items

    monkeypatch.setattr(comms, "feed", feed)
    settle = threading.Thread(target=comms_store.refresh_comms_feed, name="settle")
    settle.start()
    assert read_early.wait(5)
    m, d = _ask(1, "review", 12)
    ticked = threading.Thread(target=lambda: _tick(comms_store, monkeypatch, (m, d)))
    ticked.start()
    ticked.join(1)          # the tick is held behind the read, or it ran ahead of it
    go.set()
    settle.join(10)
    ticked.join(10)
    assert not settle.is_alive() and not ticked.is_alive()

    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert _kinds_by_id()[m["id"]] == ["action", "started"]


@needs_core
def test_a_dispatch_while_the_ask_is_being_taken_still_records_its_start(comms_store,
                                                                         monkeypatch):
    """The drain runs on the poll worker, and can reach an ask the moment it is
    queued - before the ping that asked for it has been made its owner."""
    _spawn_recorder(monkeypatch)
    queued, go = threading.Event(), threading.Event()
    real_request_one = Store.request_one

    def request_one(self, work, *, front=False):
        fresh = real_request_one(self, work, front=front)
        queued.set()
        go.wait(5)
        return fresh

    monkeypatch.setattr(Store, "request_one", request_one)
    m, d = _ask(1, "review", 12)
    ticked = threading.Thread(target=lambda: _tick(comms_store, monkeypatch, (m, d)))
    ticked.start()
    assert queued.wait(10)
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    verdicts: list = []
    drain = threading.Thread(
        target=lambda: verdicts.append(comms_store._run_queued_task(entry, forced=True)))
    drain.start()
    drain.join(1)           # its start waits for the ping's line, or it ran ahead of it
    go.set()
    drain.join(10)
    ticked.join(10)
    assert not drain.is_alive() and not ticked.is_alive()
    assert verdicts == ["spawned"]
    assert _kinds_by_id()[m["id"]] == ["action", "started"]


# MARK: - started / finished, beyond the one ping and the tick


def _kinds_by_id() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in _events():
        out.setdefault(e["id"], []).append(e["kind"])
    return out


@needs_core
def test_every_ping_that_asked_for_the_work_sees_it_start_and_finish(comms_store,
                                                                    monkeypatch):
    _spawn_recorder(monkeypatch)
    first, second = _ask(1, "review", 12), _ask(2, "review", 12)
    _tick(comms_store, monkeypatch, first)
    _tick(comms_store, monkeypatch, second)
    assert [e["outcome"] for e in _events()] == [
        "review of #12 queued, at the front of its band",
        "review of #12 was already queued, at the front of its band"]

    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    ids = (first[0]["id"], second[0]["id"])
    assert _kinds_by_id() == {i: ["action", "started"] for i in ids}

    monkeypatch.setattr(Store, "running_tasks", property(lambda s: []))
    comms_store._note_comms_runs()
    assert _kinds_by_id() == {i: ["action", "started", "finished"] for i in ids}
    assert [bool(i.get("finishedAt")) for i in comms_store.comms_items] == [True, True]


@needs_core
def test_a_start_waits_out_a_settle_already_under_way(comms_store, monkeypatch):
    """A settle that took its tick before the spawn registered must not read the
    `started` line, or it finds the run in no row and finishes it on the spot."""
    _spawn_recorder(monkeypatch)
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    comms_store._settle_lock.acquire()
    done = threading.Event()
    worker = threading.Thread(target=lambda: (comms_store._note_comms_started(entry),
                                              done.set()))
    try:
        worker.start()
        assert not done.wait(0.5), "wrote `started` in the middle of a settle"
        assert _kinds_by_id()[m["id"]] == ["action"]
    finally:
        comms_store._settle_lock.release()
    assert done.wait(10)
    worker.join()
    assert _kinds_by_id()[m["id"]] == ["action", "started"]


@needs_core
def test_work_a_peer_already_runs_has_started_too(comms_store, monkeypatch):
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))
    monkeypatch.setattr(comms_store, "dispatch_agent",
                        lambda *a, **k: autofix.VERDICT_STAND_DOWN)
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == autofix.VERDICT_STAND_DOWN
    assert _kinds_by_id() == {m["id"]: ["action", "started"]}


@needs_core
def test_every_asker_is_owned_before_the_feed_is_read_back(comms_store, monkeypatch):
    """Until the folded log is read back the asks this applet just made are known
    only from making them — and a log the core cannot fold leaves it that way."""
    _spawn_recorder(monkeypatch)

    def unreadable(limit=200):
        raise RuntimeError("diplomat-core comms-feed failed: no")

    monkeypatch.setattr(comms, "feed", unreadable)
    first, second = _ask(1, "review", 12), _ask(2, "review", 12)
    _tick(comms_store, monkeypatch, first, second)
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert _kinds_by_id() == {first[0]["id"]: ["action", "started"],
                              second[0]["id"]: ["action", "started"]}


@needs_core
def test_the_jobs_column_moves_on_at_the_spawn_not_at_the_next_tick(comms_store,
                                                                    monkeypatch):
    _spawn_recorder(monkeypatch)
    _tick(comms_store, monkeypatch, _ask(1, "review", 12))
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    fired = []
    comms_store.comms_changed.connect(lambda: fired.append(1))
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert fired
    [item] = comms_store.comms_items
    assert item["startedAt"]


@needs_core
def test_a_run_that_ends_while_comms_is_off_is_still_finished(comms_store, monkeypatch):
    """The agents' settle notices the end, whatever Watch Slack is set to: nothing
    else would while Comms is off."""
    _spawn_recorder(monkeypatch, finish=True)
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))
    comms_store.comms_enabled = False
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert _kinds_by_id()[m["id"]] == ["action", "started"]

    # The agent wrote its done sentinel; the next settle retires it.
    comms_store.refresh_auto_task_count()
    assert _kinds_by_id()[m["id"]] == ["action", "started", "finished"]
    [item] = comms_store.comms_items
    assert item["finishedAt"]
    comms_store.refresh_auto_task_count()
    assert _kinds_by_id()[m["id"]].count("finished") == 1


# MARK: - work that is already running, or queued by a monitor


DISPATCHED = 1761500000.0


def _rows(monkeypatch, rows: list) -> list:
    monkeypatch.setattr(Store, "running_tasks", property(lambda s: list(rows)))
    return rows


def _review_req(number: int, author: str = "bob") -> autofix.QueuedTask:
    """The review-request monitor's queued find on PR ``number``."""
    return autofix.QueuedTask(
        id=f"review-req:{number}", attempt=1,
        job=autofix.AgentJob(kind="review", audit_action="review-req",
                             label=f"Review-req · #{number} (@{author}) +verdict",
                             prompt="p", pr_url=f"https://github.com/o/r/pull/{number}",
                             pr_number=number, author_login=author,
                             counter="review_requests"))


@needs_core
def test_a_review_an_agent_is_already_on_is_not_asked_for_again(comms_store, monkeypatch):
    row = autofix.RunningAgent(12, "Auto · Review-req · #12 (@bob) +verdict", "review", True,
                               started_at=DISPATCHED)
    rows = _rows(monkeypatch, [row])
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))

    assert comms_store.queued_tasks == [] and comms_store.requested_work == []
    action, started = _events()
    assert (action["outcome"], action["queueKey"], action["label"]) == (
        "already running", "review:12", row.label)
    assert (started["kind"], started["id"], started["label"], started["at"]) == (
        "started", m["id"], row.label,
        comms.iso(datetime.fromtimestamp(DISPATCHED, timezone.utc)))
    assert ("comms-review", f"{row.label} — already running; asked by kacper in DM") in \
        [(e["action"], e["detail"]) for e in _feed_lines()]

    comms_store._note_comms_runs()
    assert _kinds_by_id()[m["id"]] == ["action", "started"], "still running"
    rows.clear()
    comms_store._note_comms_runs()
    assert _kinds_by_id()[m["id"]] == ["action", "started", "finished"]


@needs_core
@pytest.mark.parametrize("label, running", [
    ("Issues · #9005 · deep", True),
    ("Issues · #9005 · Deep · retry 2", True),
    ("Issues · #900501 · deep", False),
    ("Issues · #19005 · deep", False),
])
def test_a_fix_already_running_is_known_by_its_issues_label(comms_store, monkeypatch,
                                                          label, running):
    comms_store.issues = [_issue(9005)]
    _rows(monkeypatch, [autofix.RunningAgent(0, label, "issues", True,
                                             started_at=DISPATCHED)])
    _tick(comms_store, monkeypatch, _ask(1, "fix_issue", 9005))
    outcome = next(e for e in _events() if e["kind"] == "action")["outcome"]
    if running:
        assert outcome == "already running"
        assert comms_store.queued_tasks == []
    else:
        assert outcome == "fix of issue #9005 queued, at the front of its band"
        assert [t.id for t in comms_store.queued_tasks] == ["issues:9005"]


@needs_core
def test_a_review_the_monitor_has_queued_is_not_asked_for_again(comms_store, monkeypatch):
    _spawn_recorder(monkeypatch)
    rows = _rows(monkeypatch, [])
    twin = _review_req(12)
    comms_store.queued_tasks = [_review_req(40, "carol"), twin]
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))

    assert [t.id for t in comms_store.queued_tasks] == ["review-req:12", "review-req:40"]
    assert comms_store.requested_work == []
    [action] = _events()
    assert (action["outcome"], action["queueKey"], action["label"]) == (
        "already queued, at the front of its band", "review-req:12", twin.job.label)

    # The monitor dispatches its own find: its row, not the queue, says it started,
    # under the label the row wears.
    monkeypatch.setattr(comms_store, "dispatch_agent", lambda *a, **k: "spawned")
    assert comms_store._run_queued_task(twin, forced=True) == "spawned"
    assert _kinds_by_id()[m["id"]] == ["action"]
    comms_store.queued_tasks = [t for t in comms_store.queued_tasks if t.id != twin.id]
    row = autofix.RunningAgent(12, "Auto · " + twin.job.label, "review", True,
                               started_at=DISPATCHED)
    rows.append(row)
    comms_store._note_comms_runs()
    started = _events()[-1]
    assert (started["kind"], started["label"], started["at"]) == (
        "started", row.label, comms.iso(datetime.fromtimestamp(DISPATCHED, timezone.utc)))
    comms_store._note_comms_runs()
    assert _kinds_by_id()[m["id"]] == ["action", "started"], "still running"
    rows.clear()
    comms_store._note_comms_runs()
    assert _kinds_by_id()[m["id"]] == ["action", "started", "finished"]


@needs_core
def test_a_review_the_switched_off_monitor_holds_is_asked_for_in_its_own_right(
        comms_store, monkeypatch):
    """A switched-off monitor still queues its finds and starts none of them; a ping
    that took one would wait with it for an "execute now" nobody knows to press."""
    _rows(monkeypatch, [])
    comms_store.review_requests_enabled = False
    comms_store.queue_auto_run = True
    comms_store.queued_tasks = [_review_req(12)]
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))

    assert [r.key for r in comms_store.requested_work] == ["review:12"]
    [action] = _events()
    assert (action["outcome"], action["queueKey"]) == (
        "review of #12 queued, at the front of its band", "review:12")
    assert [t.id for t in comms_store.drainable_tasks] == ["review:12"]


@needs_core
def test_a_run_whose_terminal_never_ran_it_is_not_done(comms_store, monkeypatch):
    """This spawn does not wait for the pid file (it is the GUI thread's), so a
    `spawned` dispatch is not yet a start: the tick finds the command never ran, past
    the grace, and resolves the run FAILED. That is no work done."""
    from dataclasses import replace

    from diplomat_app import commsview
    from diplomat_runtime import agentregistry
    from diplomat_runtime import agentstate as A

    _spawn_recorder(monkeypatch)
    m, d = _ask(1, "review", 12)
    _tick(comms_store, monkeypatch, (m, d))
    entry = next(t for t in comms_store.queued_tasks if t.id == "review:12")
    assert comms_store._run_queued_task(entry, forced=True) == "spawned"
    assert _kinds_by_id()[m["id"]] == ["action", "started"]

    agentregistry.update(lambda book: [
        replace(r, dispatched_at=r.dispatched_at - (A.SPAWN_GRACE + 5)) for r in book])
    comms_store._settle_agents()
    comms_store._settle_agents()

    assert agentregistry.load() == [], "the tick did resolve it FAILED"
    assert _kinds_by_id()[m["id"]] == ["action", "started", "action"]
    [item] = comms_store.comms_items
    assert re.fullmatch(re.escape(comms.NEVER_STARTED) + r" \(its terminal never ran the "
                        r"command \(no pid file \d+s after dispatch\)\)", item["outcome"])
    assert not item.get("finishedAt")
    assert commsview.job_status(item, set()) == "not queued"
    assert commsview.job_status(item, {"review:12"}) == "queued"


@pytest.mark.parametrize("key, waiting", [("review-req:12", True), ("review:12", False)])
def test_only_a_monitors_find_that_left_the_queue_starts_from_the_rows(comms_store,
                                                                       monkeypatch, key,
                                                                       waiting):
    """A find still queued has not started, whoever is on its PR; an ask starts at its
    own dispatch (`_note_comms_started`), never from somebody else's agent."""
    _rows(monkeypatch, [autofix.RunningAgent(12, "Review · #12 · deep", "review", True)])
    comms_store.comms_items = [{"message": {"id": "D1:1.1"}, "queueKey": key,
                                "label": "Review-req · #12 (@bob)", "at": "x"}]
    comms_store.queued_tasks = [_review_req(12)] if waiting else []
    comms_store._note_comms_runs()
    assert _events() == []


def test_a_settle_with_nothing_to_start_or_finish_reads_no_rows(comms_store, monkeypatch):
    """Every settle runs this; the rows are a fresh read of every agent's evidence."""
    monkeypatch.setattr(Store, "running_tasks",
                        property(lambda s: pytest.fail("read the agent rows")))
    comms_store.comms_items = [
        {"message": {"id": "D1:1.1"}, "queueKey": "review:12", "label": "Review · #12 · deep",
         "at": "x"},
        {"message": {"id": "D1:1.2"}, "queueKey": "review-req:40", "label": "Review-req · #40",
         "at": "x"},
        {"message": {"id": "D1:1.3"}, "queueKey": "review:13", "label": "Review · #13 · deep",
         "at": "x", "startedAt": "x", "finishedAt": "x"}]
    comms_store.queued_tasks = [_review_req(40)]
    comms_store._note_comms_runs()
    assert _events() == []


@needs_core
def test_a_tick_landing_after_comms_was_switched_off_acts_on_nothing(comms_store,
                                                                     monkeypatch):
    shown = []
    comms_store.comms_pinged.connect(lambda t, b: shown.append(t))
    comms_store.comms_notify = True
    m, d = _ask(1, "review", 12)

    def switch_off():
        comms_store.comms_enabled = False

    _tick(comms_store, monkeypatch, (m, d), between=switch_off)
    assert comms_store.queued_tasks == [] and comms_store.requested_work == []
    [action] = _events()
    assert action == {"at": action["at"], "id": m["id"], "kind": "action",
                      "outcome": "Comms was switched off, so ping only"}
    assert [e["action"] for e in _feed_lines()] == ["comms-ping"]
    assert shown == []


@needs_core
def test_a_catch_up_reaches_the_activity_feed_as_a_notice(
        tmp_path, slack, comms_store, monkeypatch):  # noqa: F811 — the stub Slack
    """The Store's own tick through the real binary: a watermark past ``catchUpHours``
    is moved up, and the operator reads why in the activity feed."""
    monkeypatch.setenv("DIPLOMAT_SLACK_API", slack)
    monkeypatch.setenv("DIPLOMAT_CLAUDE", str(_stub_claude(tmp_path)))

    def tick() -> list[dict]:
        comms_store._apply_comms_tick(comms_store._comms_poll_once())
        lines = activity.audit_path().read_text().splitlines() \
            if activity.audit_path().exists() else []
        return [e for e in map(json.loads, lines) if e["action"] == "comms-notice"]

    assert tick() == [], "the first tick only finds where now is"
    state = comms.comms_dir() / "state.json"
    away = json.loads(state.read_text())
    away["watermark"] = f"{int(datetime.now(timezone.utc).timestamp()) - 30 * 3600}.000000"
    state.write_text(json.dumps(away))

    [notice] = tick()
    assert notice["source"] == "auto"
    assert notice["detail"].startswith("Comms had read Slack only up to ")
    assert "more than 24 hours ago" in notice["detail"]
    assert comms_store.comms_error is None
    assert tick() == [notice], "a caught-up watermark says so once"


# MARK: - the dry run


def test_the_dry_run_ticks_the_last_hour_in_a_scratch_dir(tmp_path, monkeypatch):
    """``DIPLOMAT_COMMS_POLL=1`` ticks a fresh scratch dir whose watermark is an hour
    back with nothing seen, as the gh login, whatever the live dir holds; the live dir
    is left as it was and the scratch one is gone afterwards."""
    from diplomat_app import selftest

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DIPLOMAT_COMMS_DIR")
    real = home / ".diplomat" / "comms"
    real.mkdir(parents=True)
    state = json.dumps({"watermark": "1761580000.000000", "seen": ["D1:1761579999.000100"]})
    (real / "state.json").write_text(state)
    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-test")
    monkeypatch.setattr(Store, "fetch_me", lambda self: setattr(self, "me", "octocat"))
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    ticked = []
    error = None

    def fake_tick(settings, token):
        d = comms.comms_dir()
        ticked.append((d, json.loads((d / "state.json").read_text()), settings["login"],
                       stat.S_IMODE(os.stat(d / "state.json").st_mode), time_now()))
        # What the core's tick does to the directory it is pointed at.
        (d / "state.json").write_text(json.dumps({"watermark": "1761589999.000000"}))
        comms.append_event("action", "D1:1761580001.000100")
        return {"me": ME, "items": [], "error": error, "notices": []}

    def time_now() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    monkeypatch.setattr(comms, "tick", fake_tick)
    assert selftest.run_comms_poll() == 0
    error = "Slack rejected the token (invalid_auth)"
    assert selftest.run_comms_poll() == 1

    assert len(ticked) == 2
    for d, started, login, mode, now in ticked:
        assert d != real and d.parent == scratch
        secs = int(started["watermark"].split(".")[0])
        assert now - 3600 - 5 <= secs <= now - 3600
        assert started == {"watermark": f"{secs}.000000", "seen": []}
        assert (login, mode) == ("octocat", 0o600)
    assert (real / "state.json").read_text() == state
    assert sorted(p.name for p in real.iterdir()) == ["state.json"]
    assert list(scratch.iterdir()) == []
    assert "DIPLOMAT_COMMS_DIR" not in os.environ


def test_a_dry_run_into_a_named_dir_ticks_that_dir(monkeypatch):
    from diplomat_app import selftest

    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-test")
    monkeypatch.setattr(Store, "fetch_me", lambda self: None)
    ticked = []
    monkeypatch.setattr(comms, "tick", lambda s, t: ticked.append(comms.comms_dir()) or {
        "me": None, "items": [], "error": None})
    assert selftest.run_comms_poll() == 0
    assert ticked == [Path(os.environ["DIPLOMAT_COMMS_DIR"])]


# MARK: - the Settings Test button's auth.test


@pytest.fixture
def fake_core(tmp_path, monkeypatch):
    """A ``diplomat-core`` that answers ``comms-auth`` with what the test puts in
    ``answer`` (and exits with ``status``), recording its argv, the token in its
    environment and its stdin."""
    record = tmp_path / "core-record"
    answer = tmp_path / "core-answer"
    status = tmp_path / "core-status"
    status.write_text("0")
    script = tmp_path / "diplomat-core"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" \"$DIPLOMAT_SLACK_TOKEN\" > {record}\n"
        f"cat >> {record}\n"
        f"cat {answer}\n"
        "echo 'it went wrong' >&2\n"
        f"exit $(cat {status})\n")
    script.chmod(0o755)
    monkeypatch.setenv("DIPLOMAT_CORE_BIN", str(script))

    def answering(body, code: int = 0):
        answer.write_text(body if isinstance(body, str) else json.dumps(body))
        status.write_text(str(code))

    answering.record = record
    return answering


def test_auth_test_asks_the_core_and_says_what_it_said(fake_core):
    fake_core({"ok": True, "user": "ignacy", "userId": "UME", "team": "Ours",
               "teamId": "T1"})
    assert comms.auth_test("xoxp-1") == (
        {"userID": "UME", "teamID": "T1", "handle": "ignacy", "team": "Ours"}, "")
    # The subcommand, the token in the child's environment alone, and no stdin.
    assert fake_core.record.read_text() == "comms-auth\nxoxp-1\n"

    cases = [
        ({"ok": False, "error": "Slack rejected the token (invalid_auth)"},
         0, "Slack rejected the token (invalid_auth)"),
        ({"ok": False}, 0, "Slack did not say who the token is"),
        ('["ok"]', 0, "diplomat-core comms-auth answered something that is not an answer"),
        ("not json", 0, "diplomat-core comms-auth answered something that is not an answer"),
        ("", 3, "diplomat-core comms-auth failed: it went wrong"),
    ]
    for body, code, why in cases:
        fake_core(body, code)
        assert comms.auth_test("xoxp-1") == (None, why)
    assert comms.auth_test("") == (None, "no Slack token is configured")


def test_auth_test_without_a_core_says_so(monkeypatch):
    def missing():
        raise comms.promptcore.CoreBinaryMissing("diplomat-core not found")

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", missing)
    assert comms.auth_test("xoxp-1") == (None, "diplomat-core not found")


# MARK: - timings


def test_the_tick_is_given_every_bound_the_child_holds_itself_to(monkeypatch):
    """Killed inside its own bounds, the child loses the messages it already marked
    seen, so the front-end's limit is the sum of them, keywords included."""
    t = comms.tunables()
    http = t["httpTimeoutSecs"] + 5
    for keywords in (0, 3):
        searches = (1 + (2 + keywords) * t["maxSearchPages"]) * http
        triage = t["maxPerTick"] * t["classifierTimeoutSecs"]
        assert comms.tick_timeout(keywords) >= searches + 10 + triage
    seen = []

    class Done:
        returncode, stdout, stderr = 0, '{"me": null, "items": [], "error": null}', ""

    def fake_run(argv, **kw):
        seen.append(kw["timeout"])
        return Done()

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", lambda: "/bin/core")
    monkeypatch.setattr("subprocess.run", fake_run)
    comms.tick({"keywordsRaw": "argent, release train\nargent\n, "}, "xoxp")
    assert seen == [comms.tick_timeout(2)]


@pytest.mark.parametrize("raw", ["inf", "1e12", "-inf", "nan"])
def test_an_extreme_period_is_held_to_one_a_timer_can_take(monkeypatch, raw):
    monkeypatch.setenv("DIPLOMAT_COMMS_SECS", raw)
    secs = comms.poll_secs()
    assert comms.tunables()["minPollSecs"] <= secs <= comms.MAX_POLL_SECS
    # What app.py hands QTimer.setInterval, an int32 of milliseconds.
    assert int(secs * 1000) < 2 ** 31


# MARK: - the log's file


def test_the_log_is_private_to_the_operator():
    assert comms.append_event("action", "D1:1.0", outcome="no action")
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
    assert mode(comms.comms_dir()) == 0o700
    assert mode(comms.events_path()) == 0o600
    assert mode(comms.comms_dir() / "events.lock") == 0o600


def test_an_append_waits_for_the_log_lock():
    """The lock every writer of the log takes, so the core's rotation — read, rewrite,
    rename — cannot land between this line's open and its write."""
    comms.comms_dir().mkdir(parents=True)
    held = os.open(str(comms.comms_dir() / "events.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    done = threading.Event()
    worker = threading.Thread(target=lambda: (comms.append_event("action", "D1:1.0"),
                                              done.set()))
    worker.start()
    try:
        assert not done.wait(0.5), "appended while another writer held the lock"
        assert not comms.events_path().exists()
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert done.wait(5)
    worker.join()
    assert [e["id"] for e in _events()] == ["D1:1.0"]


def test_the_report_dir_follows_the_comms_dir():
    assert comms.reports_dir() == Path(os.environ["DIPLOMAT_COMMS_DIR"]) / "reports"
    assert comms.events_path() == Path(os.environ["DIPLOMAT_COMMS_DIR"]) / "events.jsonl"
