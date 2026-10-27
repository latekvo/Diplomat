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

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_autofix import _spawn_recorder, store  # noqa: F401 — fixture

from diplomat_app import bans, issues
from diplomat_app.store import Store, app_settings
from diplomat_runtime import activity, appconfig, autofix, comms, review
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


def _events() -> list[dict]:
    """The lines this front-end wrote: the core's own ping/decision lines left out."""
    path = comms.events_path()
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in lines if e["kind"] not in ("ping", "decision")]


def _feed_lines() -> list[dict]:
    return [json.loads(line) for line in activity.audit_path().read_text().splitlines()]


@pytest.fixture
def comms_store(store, monkeypatch):  # noqa: F811 — the shared Store fixture
    """The store with a panel fetch behind it — bob's PR #12, my own #13, mallory's
    #14 — one issue, a token in the environment, and mallory banned."""
    store.has_loaded = True
    store.prs = [_pr(12, "bob"), _pr(13, "alice"), _pr(14, "mallory")]
    store.issues = [_issue(7)]
    monkeypatch.setattr("diplomat_app.bans.read",
                        lambda: [bans.BannedAuthor("mallory", "prompt injection")])
    monkeypatch.setenv(comms.TOKEN_ENV, "xoxp-test")
    store.comms_enabled = True
    return store


def _tick(store, monkeypatch, *pings, error=None) -> dict:
    """One poll whose ``comms-tick`` answers ``pings`` (``(message, decision)`` pairs),
    applied as the Qt thread applies it. Returns what the tick was handed."""
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
                "error": error}

    monkeypatch.setattr(comms, "tick", fake_tick)
    store._apply_comms_tick(store._comms_poll_once())
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
    _tick(comms_store, monkeypatch, (_message(1), _decision("review", 13)))
    ask = comms_store.requested_work[0]
    assert (ask.key, ask.author, ask.config["specificAuthor"]) == ("review:13", "", "mine")


@needs_core
def test_an_ask_already_queued_keeps_its_ask_and_moves_up(comms_store, monkeypatch):
    _sweep_review(comms_store, 1, 13)
    _tick(comms_store, monkeypatch, (_message(1), _decision("review", 13)))
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
            raise RuntimeError("gh exited 1: no issue")
        return json.dumps({"number": int(args[2]), "state": state, "author": {"login": "dana"},
                           "url": f"https://github.com/o/r/issues/{args[2]}"}).encode()

    monkeypatch.setattr("diplomat_runtime.gh.run", gh)
    _tick(comms_store, monkeypatch, (_message(1), _decision("fix_issue", 8)),
          (_message(2), _decision("fix_issue", 9)), (_message(3), _decision("fix_issue", 10)))

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
          (_message(2), _decision("bump", 99)))

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

    _tick(comms_store, monkeypatch, (_message(1), _decision(action, number)))

    assert [t.id for t in comms_store.queued_tasks] == order
    assert comms_store.requested_work == asks
    switch = {"comms_act_requests": "requests", "comms_act_bumps": "bumps",
              "comms_act_investigations": "investigations"}[toggle]
    assert _events()[-1]["outcome"] == \
        f"{action} not acted on: acting on {switch} is switched off"
    assert [e["action"] for e in _feed_lines()] == ["comms-ping"]


@needs_core
def test_a_pr_that_is_not_open_queues_nothing(comms_store, monkeypatch):
    _tick(comms_store, monkeypatch, (_message(1), _decision("review", 99)))
    assert comms_store.requested_work == []
    assert comms_store.queued_tasks == []
    assert _events()[-1]["outcome"] == "#99 is not an open PR of this repo"
    assert [e["action"] for e in _feed_lines()] == ["comms-ping", "comms-review"]


@needs_core
def test_a_banned_author_is_never_queued(comms_store, monkeypatch):
    comms_store.issues = [_issue(7, author="Mallory")]
    _spawn_recorder(monkeypatch)
    _tick(comms_store, monkeypatch, (_message(1), _decision("review", 14)),
          (_message(2), _decision("fix_issue", 7)))
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
    m = _message(1)
    _tick(comms_store, monkeypatch, (m, _decision("review", 12)))
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
    comms_store._note_comms_finished()
    assert _events()[-1]["kind"] == "started", "still running"

    running.clear()
    comms_store._note_comms_finished()
    assert [e["kind"] for e in _events()][-2:] == ["started", "finished"]
    comms_store.refresh_comms_feed()
    assert comms_store.comms_items[0]["finishedAt"]
    comms_store._note_comms_finished()
    assert [e["kind"] for e in _events()].count("finished") == 1


def test_a_run_back_in_the_queue_is_not_finished(comms_store, monkeypatch):
    comms_store.comms_items = [{"message": {"id": "D1:1.1"}, "queueKey": "review:12",
                                "label": "Review · #12 · deep", "startedAt": "x", "at": "x"}]
    monkeypatch.setattr(Store, "running_tasks", property(lambda s: []))
    comms_store.queued_tasks = [autofix.QueuedTask(
        id="review:12", job=autofix.AgentJob(kind="review", audit_action="review",
                                             label="Review · #12 · deep", prompt="p"),
        attempt=1)]
    comms_store._note_comms_finished()
    assert _events() == []
    comms_store.queued_tasks = []
    comms_store._note_comms_finished()
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
    comms_store.comms_notify = False
    _tick(comms_store, monkeypatch, (_message(2), _decision("none")))
    assert shown == [("DM from kacper", "can you review #12?")]


def test_the_poll_does_nothing_while_comms_is_off(comms_store, monkeypatch):
    comms_store.comms_enabled = False
    monkeypatch.setattr(comms_store, "start_background", lambda *a, **k: pytest.fail("polled"))
    comms_store.run_comms_poll_async()


# MARK: - the Settings Test button's auth.test


@pytest.fixture
def slack_auth(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    answers: list = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, body = answers.pop(0)
            answers.append(("auth", self.path, self.headers.get("Authorization")))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DIPLOMAT_SLACK_API", f"http://127.0.0.1:{server.server_address[1]}/api")
    yield answers
    server.shutdown()
    server.server_close()


def test_auth_test_names_the_token_or_says_why_not(slack_auth):
    slack_auth.append((200, {"ok": True, "user_id": "UME", "team_id": "T1", "user": "ignacy"}))
    assert comms.auth_test("xoxp-1") == ({"userID": "UME", "teamID": "T1", "handle": "ignacy"}, "")
    assert slack_auth.pop() == ("auth", "/api/auth.test", "Bearer xoxp-1")
    cases = [
        ((200, {"ok": False, "error": "invalid_auth"}), "Slack rejected the token (invalid_auth)"),
        ((200, {"ok": False, "error": "missing_scope"}),
         "Slack: missing_scope. The token needs the user scopes search:read and users:read."),
        ((429, {}), "Slack rate-limited the watcher"),
        ((500, {}), "Slack answered HTTP 500"),
        ((200, ["not", "an", "object"]), "Slack answered something that is not its JSON"),
    ]
    for answer, why in cases:
        slack_auth.append(answer)
        assert comms.auth_test("xoxp-1") == (None, why)
        slack_auth.pop()
    assert comms.auth_test("") == (None, "no Slack token is configured")


def test_the_report_dir_follows_the_comms_dir():
    assert comms.reports_dir() == Path(os.environ["DIPLOMAT_COMMS_DIR"]) / "reports"
    assert comms.events_path() == Path(os.environ["DIPLOMAT_COMMS_DIR"]) / "events.jsonl"
