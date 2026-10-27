"""Comms through the built ``diplomat-core`` CLI, never a reimplementation of the core.

The tick runs end to end: the real binary against a stub Slack on loopback
(``DIPLOMAT_SLACK_API``) and a stub ``claude`` (``DIPLOMAT_CLAUDE``) answering like
``claude -p --output-format json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diplomat_runtime import autofix, comms, core  # noqa: E402

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)")


def _core(*args: str, stdin: object = None, env: dict | None = None,
          timeout: float = 60) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.setdefault("DIPLOMAT_CORE", str(core.assets_dir()))
    full.update(env or {})
    data = None if stdin is None else (stdin if isinstance(stdin, str) else json.dumps(stdin))
    return subprocess.run([CORE_BIN, *args], input=data, capture_output=True, text=True,
                          env=full, timeout=timeout)


def _comms_dir() -> Path:
    return Path(os.environ["DIPLOMAT_COMMS_DIR"])


MESSAGE = {
    "id": "C1:1508284197.000015", "channelID": "C1", "channelName": "general", "isDM": False,
    "userID": "UALICE", "userName": "alice", "teamID": "TOURS",
    "text": "the login screen crashes on Android 14", "ts": "1508284197.000015",
    "permalink": "https://ours.slack.com/archives/C1/p1508284197000015", "reasons": ["mention"],
}


def _effect(action: str, number=0, toggles: dict | None = None) -> dict:
    decision = {"action": action, "summary": "login page crashes", "reason": "r"}
    if number is not None:
        decision["number"] = number
    out = _core("comms-effect", stdin={"decision": decision, "message": MESSAGE,
                                       "toggles": toggles or {}})
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_comms_effect_is_the_plan_python_acts_on():
    assert _effect("review", 12) == {"kind": "review", "number": 12}
    assert _effect("fix_issue", 7) == {"kind": "fix_issue", "number": 7}
    assert _effect("bump", 3) == {"kind": "bump", "number": 3}
    assert _effect("none") == {"kind": "none"}
    # Each toggle gates its own kind; a missing one reads as on.
    assert _effect("review", 12, {"requests": False}) == {"kind": "none"}
    assert _effect("bump", 3, {"requests": False}) == {"kind": "bump", "number": 3}
    assert _effect("bump", 3, {"bumps": False}) == {"kind": "none"}
    assert _effect("investigate", 0, {"investigations": False}) == {"kind": "none"}
    # No positive integer number, no action.
    for bad in (None, 0, -4, 12.5, "12", True):
        assert _effect("review", bad) == {"kind": "none"}, bad
    assert _effect("merge", 5) == {"kind": "none"}


def test_an_investigation_round_trips_into_the_queue_and_its_prompt():
    effect = _effect("investigate")
    config = effect["config"]
    assert effect["kind"] == config["kind"] == "investigate"
    assert config["number"] == 1508284197000015
    assert config["reportPath"] == str(_comms_dir() / "reports" / "1508284197000015.md")
    assert config["label"] == "Investigate · Slack · alice · login page crashes"
    assert (config["sender"], config["channel"]) == ("alice", "#general")

    # The one config, unchanged, is both the queue's ask...
    work = autofix.RequestedWork(number=config["number"], url=MESSAGE["permalink"],
                                 author="", config=config)
    assert work.key == "investigate:1508284197000015"
    assert autofix.queue_band(work.key) == 3
    assert work.label == config["label"]

    # ...and the build-prompt payload.
    out = _core("build-prompt", stdin=work.config)
    assert out.returncode == 0, out.stderr
    prompt = out.stdout
    assert "BEGIN UNTRUSTED SLACK MESSAGE\n| Triage summary: login page crashes\n| Text:\n" \
           "| the login screen crashes on Android 14\nEND UNTRUSTED SLACK MESSAGE" in prompt
    assert "you make no GitHub writes of any kind" in prompt
    assert "Never post, reply or react in Slack" in prompt
    assert f"Write the report to {config['reportPath']}" in prompt
    assert "Diplomat attribution tag" not in prompt


def _write_events(lines: list) -> None:
    d = _comms_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "events.jsonl", "a", encoding="utf-8") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def test_comms_feed_folds_what_both_sides_wrote():
    def at(minute: int, micro: int = 0) -> str:
        return datetime(2026, 10, 27, 10, minute, 0, micro, tzinfo=timezone.utc).isoformat()

    other = dict(MESSAGE, id="D1:1508284300.000001", ts="1508284300.000001", isDM=True,
                 reasons=["dm"], text="can you review PR 12?")
    _write_events([
        {"at": at(0), "kind": "ping", "id": MESSAGE["id"], "message": MESSAGE},
        {"at": at(0), "kind": "decision", "id": MESSAGE["id"],
         "decision": {"action": "investigate", "number": 0, "summary": "s", "reason": "r"}},
        {"at": at(1, 250000), "kind": "action", "id": MESSAGE["id"], "outcome": "queued",
         "queueKey": "investigate:1508284197000015", "label": "Investigate · Slack · alice · s"},
        {"at": at(2), "kind": "started", "id": MESSAGE["id"], "label": "Investigate · Slack · alice · s"},
        {"at": at(9), "kind": "finished", "id": MESSAGE["id"], "label": "Investigate · Slack · alice · s"},
        {"at": at(5), "kind": "ping", "id": other["id"], "message": other},
        {"at": at(6), "kind": "error", "id": "", "detail": "Slack rate-limited the watcher"},
        "not json",
        {"at": at(7), "kind": "nonsense", "id": other["id"]},
    ])
    # No stdin is read: an attached pipe never closed must not hold it.
    proc = subprocess.Popen([CORE_BIN, "comms-feed"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "DIPLOMAT_CORE": str(core.assets_dir())})
    try:
        proc.wait(timeout=20)
        out = proc.stdout.read()
    finally:
        proc.kill()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode == 0
    items = json.loads(out)
    assert [i["message"]["id"] for i in items] == [other["id"], MESSAGE["id"]]
    newest, oldest = items
    assert "decision" not in newest and "outcome" not in newest
    assert oldest["decision"]["action"] == "investigate"
    assert (oldest["outcome"], oldest["queueKey"], oldest["label"]) == (
        "queued", "investigate:1508284197000015", "Investigate · Slack · alice · s")
    assert oldest["at"] == "2026-10-27T10:00:00Z"
    assert oldest["startedAt"] == "2026-10-27T10:02:00Z"
    assert oldest["finishedAt"] == "2026-10-27T10:09:00Z"

    limited = _core("comms-feed", "--limit", "1")
    assert [i["message"]["id"] for i in json.loads(limited.stdout)] == [other["id"]]
    bad = _core("comms-feed", "--limit", "lots")
    assert bad.returncode == 1 and "--limit" in bad.stderr


class _Slack(BaseHTTPRequestHandler):
    """``auth.test`` and ``search.messages`` over ``messages``, newest first."""

    messages: list = []
    seen: list = []
    #: What ``auth.test`` answers instead of the operator, when set.
    auth_error: str | None = None

    def do_GET(self):  # noqa: N802 - the http.server hook
        url = urlparse(self.path)
        _Slack.seen.append((url.path, parse_qs(url.query), self.headers.get("Authorization")))
        if url.path.endswith("/auth.test") and _Slack.auth_error:
            body = {"ok": False, "error": _Slack.auth_error}
        elif url.path.endswith("/auth.test"):
            body = {"ok": True, "user_id": "UME", "team_id": "TOURS", "user": "operator",
                    "team": "Ours"}
        elif url.path.endswith("/search.messages"):
            matches = sorted(_Slack.messages, key=lambda m: m["ts"], reverse=True)
            body = {"ok": True, "messages": {"matches": matches, "paging": {"pages": 1}}}
        else:
            body = {"ok": False, "error": "unknown_method"}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def slack():
    _Slack.messages, _Slack.seen, _Slack.auth_error = [], [], None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slack)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api"
    server.shutdown()
    server.server_close()


def _stub_claude(tmp_path: Path) -> Path:
    """A ``claude`` that answers like ``-p --output-format json`` and records its stdin."""
    answer = json.dumps([
        {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
        {"type": "result", "is_error": False, "result": "",
         "structured_output": {"action": "review", "number": 12, "summary": "Review PR 12",
                               "reason": "a direct ask"}},
    ])
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\ncat > {tmp_path / 'stdin.txt'}\nprintf '%s' '{answer}'\n")
    stub.chmod(0o755)
    return stub


def test_comms_tick_runs_end_to_end_through_the_binary(tmp_path, slack):
    env = {"DIPLOMAT_SLACK_API": slack, "DIPLOMAT_SLACK_TOKEN": "xoxp-test",
           "DIPLOMAT_CLAUDE": str(_stub_claude(tmp_path))}
    settings = {"keywordsRaw": "argent", "login": "testuser"}

    first = _core("comms-tick", stdin=settings, env=env)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {
        "me": {"userID": "UME", "teamID": "TOURS", "handle": "operator"},
        "items": [], "error": None, "notices": []}
    assert [p for p, _, _ in _Slack.seen] == ["/api/auth.test"]
    watermark = json.loads((_comms_dir() / "state.json").read_text())["watermark"]

    secs = int(watermark.split(".")[0])
    _Slack.messages = [
        {"ts": f"{secs + 5}.000100", "user": "UALICE", "username": "alice", "team": "TOURS",
         "text": "can you review PR 12?", "permalink": "https://ours.slack.com/p1", "type": "im",
         "channel": {"id": "D1", "name": "UALICE", "is_im": True}},
        {"ts": f"{secs + 6}.000100", "user": "UME", "username": "operator", "team": "TOURS",
         "text": "note to self", "type": "im", "channel": {"id": "D1", "is_im": True}},
        {"ts": f"{secs - 5}.000100", "user": "UALICE", "username": "alice", "team": "TOURS",
         "text": "from before Comms was on", "type": "im", "channel": {"id": "D1", "is_im": True}},
    ]
    second = _core("comms-tick", stdin=settings, env=env)
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result["error"] is None
    assert [i["message"]["text"] for i in result["items"]] == ["can you review PR 12?"]
    assert result["items"][0]["decision"] == {
        "action": "review", "number": 12, "summary": "Review PR 12", "reason": "a direct ask"}
    queries = [q["query"][0] for p, q, _ in _Slack.seen if p.endswith("search.messages")]
    assert [q.split(" after:")[0] for q in queries] == ["is:dm", "<@UME>", '"argent"']
    assert all(auth == "Bearer xoxp-test" for _, _, auth in _Slack.seen)
    stdin = (tmp_path / "stdin.txt").read_text()
    assert stdin.startswith("BEGIN UNTRUSTED SLACK MESSAGE\n| From: alice\n| Where: DM\n")
    assert "can you review PR 12?" in stdin

    feed = json.loads(_core("comms-feed").stdout)
    assert [(i["message"]["text"], i["decision"]["action"]) for i in feed] == [
        ("can you review PR 12?", "review")]

    third = json.loads(_core("comms-tick", stdin=settings, env=env).stdout)
    assert third["items"] == [], "a triaged message is never triaged again"


def test_comms_tick_reports_a_failure_as_its_answer(tmp_path, slack):
    out = _core("comms-tick", stdin={}, env={"DIPLOMAT_SLACK_API": slack,
                                             "DIPLOMAT_SLACK_TOKEN": ""})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"me": None, "items": [], "notices": [],
                                      "error": "no Slack token is configured"}
    events = [json.loads(line) for line in
              (_comms_dir() / "events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["id"], e["detail"]) for e in events] == [
        ("error", "", "no Slack token is configured")]
    assert _Slack.seen == []


def test_the_settings_test_button_says_what_the_core_says(slack, monkeypatch):
    """``comms.auth_test`` through the real ``comms-auth``: who the token is, or the
    tick's own words for why not."""
    monkeypatch.setenv("DIPLOMAT_SLACK_API", slack)
    assert comms.auth_test("xoxp-test") == (
        {"userID": "UME", "teamID": "TOURS", "handle": "operator", "team": "Ours"}, "")
    assert _Slack.seen[-1][2] == "Bearer xoxp-test"
    for error, why in [
        ("missing_scope", "Slack: missing_scope. The token needs the user scope search:read."),
        ("invalid_auth", "Slack rejected the token (invalid_auth)"),
    ]:
        _Slack.auth_error = error
        assert comms.auth_test("xoxp-test") == (None, why)


def test_the_dry_run_through_the_entry_point_consumes_no_ping(tmp_path, slack):
    """``DIPLOMAT_COMMS_POLL=1 python -m diplomat_app``, twice: each run triages the last
    hour of Slack (not the older message) without consulting the live watermark, which
    is past them all, and writes nothing to the live dir."""
    linux = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    live = home / ".diplomat" / "comms"
    live.mkdir(parents=True)
    now = int(datetime.now(timezone.utc).timestamp())
    state = json.dumps({"watermark": f"{now - 10}.000000", "seen": []})
    (live / "state.json").write_text(state)
    _Slack.messages = [
        {"ts": f"{now - 95}.000100", "user": "UALICE", "username": "alice", "team": "TOURS",
         "text": "can you review PR 12?", "permalink": "https://ours.slack.com/p1",
         "type": "im", "channel": {"id": "D1", "name": "UALICE", "is_im": True}},
        {"ts": f"{now - 7200}.000100", "user": "UALICE", "username": "alice", "team": "TOURS",
         "text": "from two hours ago", "permalink": "https://ours.slack.com/p0",
         "type": "im", "channel": {"id": "D1", "name": "UALICE", "is_im": True}}]
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "DIPLOMAT_COMMS_DIR"}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), TMPDIR=str(scratch),
               PATH="/usr/bin:/bin", QT_QPA_PLATFORM="offscreen", DIPLOMAT_COMMS_POLL="1",
               DIPLOMAT_SLACK_API=slack, DIPLOMAT_SLACK_TOKEN="xoxp-test",
               DIPLOMAT_CLAUDE=str(_stub_claude(tmp_path)),
               DIPLOMAT_CORE=str(core.assets_dir()),
               PYTHONPATH=os.pathsep.join([str(linux), str(linux.parents[1] / "diplomat-runtime"),
                                           str(linux.parents[1] / "szpontnet-core")]))
    for _ in range(2):
        run = subprocess.run([sys.executable, "-m", "diplomat_app"], cwd=str(linux), env=env,
                             capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "1 new ping\n" in run.stdout
        assert "can you review PR 12?" in run.stdout
        assert '"kind": "review", "number": 12' in run.stdout
    assert (live / "state.json").read_text() == state
    assert sorted(p.name for p in live.iterdir()) == ["state.json"]
    assert list(scratch.iterdir()) == []
