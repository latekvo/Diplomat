"""Comms through the built ``diplomat-core`` CLI, never a reimplementation of the core.

The tick runs end to end: the real binary against a fake Slack MCP server on loopback
(``DIPLOMAT_SLACK_API``), signed in through a Claude Code credentials file under
``DIPLOMAT_CLAUDE_DIR``, and a stub ``claude`` (``DIPLOMAT_CLAUDE``) answering like
``claude -p --output-format json``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
    # Ends in the message's local time, so two asks with one summary wear two labels.
    sent = time.strftime("%H:%M:%S", time.localtime(1508284197))
    assert config["label"] == f"Investigate · Slack · alice · login page crashes · {sent}"
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


#: The token the fake Slack accepts by default, and the one ``_sign_in`` stores.
TOKEN = "mcp-login-test"

#: Who the fake Slack knows, as ``slack_read_user_profile`` prints them. UEXT is a Slack
#: Connect guest: no organization, another company's email.
PROFILES = {
    "UME": {"Username": "operator", "Real Name": "The Operator",
            "Email": "operator@ours.test", "Organization Name": "Ours"},
    "UALICE": {"Username": "alice", "Real Name": "Alice Doe",
               "Email": "alice@ours.test", "Organization Name": "Ours"},
    "UEXT": {"Username": "mitch", "Real Name": "Mitch", "Email": "mitch@other.test",
             "Organization Name": ""},
}


def _profile_text(user: str) -> str:
    p = PROFILES[user]
    return (f"User ID: {user}\nUsername: {p['Username']}\nDisplay Name: {p['Username']}\n"
            f"Real Name: {p['Real Name']}\nPronouns: \nTitle: \nEmail: {p['Email']}\n"
            f"Organization Name: {p['Organization Name']}\nPhone: \nStatus: \n"
            "Timezone: Europe/Warsaw\nAdmin: No\nOwner: No\nBot: No\nRestricted: No\n")


def _message(ts: str, user: str, text: str, *, channel: str = "D1",
             where: str = "DM") -> dict:
    """``where`` is the result's ``Channel:`` label: ``DM``, ``Group DM`` or ``#name``."""
    return {"ts": ts, "user": user, "text": text, "channel": channel, "where": where}


def _cursor(page: int) -> str:
    return base64.b64encode(f"CURRENT_PAGE:{page}".encode()).decode()


def _matches(m: dict, args: dict) -> bool:
    if args.get("filters", "") == "is:dm" and m["where"] not in ("DM", "Group DM"):
        return False
    for keyword in args.get("keywords") or []:
        k = keyword.strip('"')
        if k.startswith("<@") and k.endswith(">"):
            if k not in m["text"] and f"{k[:-1]}|" not in m["text"]:
                return False
        elif k.lower() not in m["text"].lower():
            return False
    return True


def _search(args: dict) -> dict:
    """One page of ``messages`` newer than ``after``, in the detailed markdown: the
    headings count the page's own results."""
    found = sorted((m for m in _Slack.messages
                    if float(m["ts"]) > int(args.get("after") or 0) and _matches(m, args)),
                   key=lambda m: float(m["ts"]), reverse=args.get("sort_dir") != "asc")
    limit = int(args.get("limit") or 20)
    page = (int(base64.b64decode(args["cursor"]).decode().split(":")[1])
            if args.get("cursor") else 1)
    hits = found[(page - 1) * limit:page * limit]
    out = (f"# Search Results for: {' '.join(args.get('keywords') or [])}\n\n"
           f"## Messages ({len(hits)} results)\n")
    for k, m in enumerate(hits, 1):
        p = PROFILES[m["user"]]
        out += (f"### Result {k} of {len(hits)}\nChannel: {m['where']} (ID: {m['channel']})\n"
                f"From: {p['Real Name']} <{p['Email']}> (ID: {m['user']}) \n"
                "Time: 2026-10-28 19:36:32 CET\n"
                f"Message_ts: {m['ts']}\n"
                f"Permalink: [link](https://ours.slack.test/archives/{m['channel']}/"
                f"p{m['ts'].replace('.', '')})\n"
                f"Text: \n{m['text']}\n\n---\n\n")
    answer = {"results": out}
    if page * limit < len(found):
        answer["pagination_info"] = (f"For the next page of results use cursor "
                                     f"`{_cursor(page + 1)}`\n")
    return answer


class _Slack(BaseHTTPRequestHandler):
    """Slack's MCP server as the core speaks to it: JSON-RPC over POST, ``initialize``
    opening a session every later request must name in ``Mcp-Session-Id``, and the two
    tools the watcher calls answering in the server's own text."""

    messages: list = []
    #: ``(method, params)`` of each request let through.
    calls: list = []
    #: ``(method, Authorization, Mcp-Session-Id)`` of every request.
    seen: list = []
    sessions: set = set()
    token = TOKEN
    #: The HTTP status every request is answered with instead, when set.
    refuse: int | None = None

    def do_POST(self):  # noqa: N802 - the http.server hook
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        method, params = body.get("method"), body.get("params") or {}
        auth, session = self.headers.get("Authorization"), self.headers.get("Mcp-Session-Id")
        _Slack.seen.append((method, auth, session))
        if _Slack.refuse or auth != f"Bearer {_Slack.token}":
            return self._send(_Slack.refuse or 401, {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32001, "message": "invalid_token"}})
        if method == "initialize":
            sid = f"session-{len(_Slack.sessions) + 1}"
            _Slack.sessions.add(sid)
            _Slack.calls.append((method, params))
            return self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": params.get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-slack", "version": "1"}}}, session=sid)
        if session not in _Slack.sessions:
            return self._send(400, {"jsonrpc": "2.0", "id": body.get("id"), "error": {
                "code": -32600, "message": "Bad Request: No valid session ID provided"}})
        _Slack.calls.append((method, params))
        if method == "notifications/initialized":
            return self._send(202, None)
        name, args = params.get("name"), params.get("arguments") or {}
        if method == "tools/call" and name == "slack_read_user_profile":
            result = {"content": [{"type": "text", "text": json.dumps(
                {"result": _profile_text(args.get("user_id") or "UME")})}]}
        elif (method == "tools/call" and name == "slack_search_public_and_private"
              and args.get("filters", "") in ("", "is:dm")):
            result = {"content": [{"type": "text", "text": json.dumps(_search(args))}]}
        else:
            result = {"isError": True, "content": [{
                "type": "text", "text": "execution_failed: parameter_validation_failed"}]}
        self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _send(self, status: int, body: dict | None, session: str | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if session:
            self.send_header("Mcp-Session-Id", session)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _sign_in(url: str, token: str = TOKEN) -> Path:
    """Claude Code's login to the Slack MCP server at ``url``, where the core reads it:
    ``.credentials.json`` under ``DIPLOMAT_CLAUDE_DIR`` (conftest's scratch one), beside
    a login to another MCP server that must never reach Slack."""
    d = Path(os.environ["DIPLOMAT_CLAUDE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    expires = int((time.time() + 3600) * 1000)
    path = d / ".credentials.json"
    path.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "not-for-slack"},
        "mcpOAuth": {
            "github|5e1f": {"serverName": "github", "accessToken": "not-for-slack",
                            "serverUrl": "https://api.githubcopilot.com/mcp/",
                            "expiresAt": expires},
            "slack|0a9c": {"serverName": "slack", "serverUrl": url, "accessToken": token,
                           "refreshToken": "r", "expiresAt": expires},
        }}))
    return path


@pytest.fixture
def slack(monkeypatch):
    """The fake Slack on loopback as ``DIPLOMAT_SLACK_API``, Claude Code signed in to it."""
    _Slack.messages, _Slack.calls, _Slack.seen, _Slack.sessions = [], [], [], set()
    _Slack.token, _Slack.refuse = TOKEN, None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slack)
    # shutdown() waits out serve_forever's poll, half a second by default.
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    monkeypatch.setenv("DIPLOMAT_SLACK_API", url)
    _sign_in(url)
    yield url
    server.shutdown()
    server.server_close()


def _stub_claude(tmp_path: Path) -> Path:
    """A ``claude`` that answers like ``-p --output-format json``, recording its stdin in
    ``stdin.txt`` and its arguments, one call a line, in ``args.txt``."""
    answer = json.dumps([
        {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
        {"type": "result", "is_error": False, "result": "",
         "structured_output": {"action": "review", "number": 12, "summary": "Review PR 12",
                               "reason": "a direct ask"}},
    ])
    # Not tmp_path/claude: that is conftest's DIPLOMAT_CLAUDE_DIR.
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {tmp_path / 'args.txt'}\n"
                    f"cat > {tmp_path / 'stdin.txt'}\nprintf '%s' '{answer}'\n")
    stub.chmod(0o755)
    return stub


def _tools(name: str) -> list[dict]:
    return [p["arguments"] for m, p in _Slack.calls
            if m == "tools/call" and p["name"] == name]


def _first_tick(settings: dict) -> int:
    """The tick that only learns who the login is; the watermark's whole seconds."""
    first = _core("comms-tick", stdin=settings)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {
        "me": {"userID": "UME", "teamID": "Ours", "handle": "operator"},
        "items": [], "error": None, "notices": []}
    watermark = json.loads((_comms_dir() / "state.json").read_text())["watermark"]
    return int(watermark.split(".")[0])


def test_comms_tick_runs_end_to_end_through_the_binary(tmp_path, slack, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_CLAUDE", str(_stub_claude(tmp_path)))
    settings = {"keywordsRaw": "argent", "login": "testuser"}

    secs = _first_tick(settings)
    assert [m for m, _ in _Slack.calls] == ["initialize", "notifications/initialized",
                                            "tools/call"]
    assert _tools("slack_read_user_profile") == [{"response_format": "detailed"}]
    # Claude Code's Slack login on every request; the session once there is one.
    assert [(m, s) for m, _, s in _Slack.seen] == [
        ("initialize", None), ("notifications/initialized", "session-1"),
        ("tools/call", "session-1")]
    assert {a for _, a, _ in _Slack.seen} == {f"Bearer {TOKEN}"}

    _Slack.calls = []
    _Slack.messages = [
        _message(f"{secs + 5}.000100", "UALICE", "can you review PR 12?"),
        _message(f"{secs + 6}.000100", "UME", "note to self"),
        _message(f"{secs - 5}.000100", "UALICE", "from before Comms was on"),
    ]
    second = _core("comms-tick", stdin=settings)
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result["error"] is None
    assert [i["message"]["text"] for i in result["items"]] == ["can you review PR 12?"]
    message = result["items"][0]["message"]
    assert (message["userID"], message["userName"], message["teamID"], message["isDM"],
            message["permalink"]) == (
        "UALICE", "alice", "Ours", True,
        f"https://ours.slack.test/archives/D1/p{secs + 5}000100")
    assert result["items"][0]["decision"] == {
        "action": "review", "number": 12, "summary": "Review PR 12", "reason": "a direct ask"}
    searches = _tools("slack_search_public_and_private")
    assert [(s.get("filters"), s.get("keywords"), s["after"]) for s in searches] == [
        ("is:dm", None, str(secs)), (None, ["<@UME>"], str(secs)),
        (None, ['"argent"'], str(secs))]
    # The operator's own message and the one under the watermark cost no profile read.
    assert [p.get("user_id") for p in _tools("slack_read_user_profile")] == [None, "UALICE"]
    stdin = (tmp_path / "stdin.txt").read_text()
    assert stdin.startswith("BEGIN UNTRUSTED SLACK MESSAGE\n| From: alice\n| Where: DM\n")
    assert "can you review PR 12?" in stdin

    feed = json.loads(_core("comms-feed").stdout)
    assert [(i["message"]["text"], i["decision"]["action"]) for i in feed] == [
        ("can you review PR 12?", "review")]

    third = json.loads(_core("comms-tick", stdin=settings).stdout)
    assert third["items"] == [], "a triaged message is never triaged again"


def test_a_sender_from_another_workspace_is_never_a_ping(tmp_path, slack, monkeypatch):
    """A Slack Connect guest's DM and @-mention are found and their profile read, which
    names no organization, so neither is triaged; a colleague's ping in the same tick
    is."""
    monkeypatch.setenv("DIPLOMAT_CLAUDE", str(_stub_claude(tmp_path)))
    secs = _first_tick({})
    _Slack.messages = [
        _message(f"{secs + 5}.000100", "UEXT", "can you review PR 12?", channel="D9"),
        _message(f"{secs + 6}.000100", "UEXT", "<@UME|The Operator> look at #3",
                 channel="C0SHARED", where="#shared-with-other"),
        _message(f"{secs + 7}.000100", "UALICE", "<@UME> review #4 please",
                 channel="C0TEAM", where="#team"),
    ]
    result = json.loads(_core("comms-tick", stdin={}).stdout)
    assert result["error"] is None
    assert [i["message"]["text"] for i in result["items"]] == ["<@UME> review #4 please"]
    assert {p.get("user_id") for p in _tools("slack_read_user_profile")} == {
        None, "UEXT", "UALICE"}
    calls = [line for line in (tmp_path / "args.txt").read_text().splitlines()
             if line.startswith("-p ")]
    assert len(calls) == 1, "one triage, alice's"
    assert "<@UME> review #4 please" in (tmp_path / "stdin.txt").read_text()


def test_the_login_is_read_from_claude_codes_credentials_on_each_use(slack, monkeypatch):
    """``comms.auth`` through the real ``comms-auth``: the Slack entry of the file under
    ``DIPLOMAT_CLAUDE_DIR``, never the other server's, re-read when Claude Code rewrites
    it; ``DIPLOMAT_SLACK_TOKEN`` reaches the core and wins."""
    me = {"userID": "UME", "teamID": "Ours", "handle": "operator", "team": "Ours"}
    assert comms.auth() == (me, "")
    assert {a for _, a, _ in _Slack.seen} == {f"Bearer {TOKEN}"}

    _Slack.seen, _Slack.token = [], "a-later-login"
    path = _sign_in(slack, "a-later-login")
    assert comms.auth() == (me, "")
    assert {a for _, a, _ in _Slack.seen} == {"Bearer a-later-login"}

    path.unlink()
    monkeypatch.setenv(comms.TOKEN_ENV, "a-later-login")
    assert comms.auth() == (me, "")


def test_without_a_login_the_tick_says_how_to_sign_in(slack):
    (Path(os.environ["DIPLOMAT_CLAUDE_DIR"]) / ".credentials.json").unlink()
    why = ("Claude Code is not signed in to Slack: in Claude Code, run /mcp and "
           "authenticate the Slack server")
    out = _core("comms-tick", stdin={})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"me": None, "items": [], "notices": [], "error": why}
    events = [json.loads(line) for line in
              (_comms_dir() / "events.jsonl").read_text().splitlines()]
    assert [(e["kind"], e["id"], e["detail"]) for e in events] == [("error", "", why)]
    assert comms.auth() == (None, why)
    assert _Slack.seen == []


def test_a_refused_login_is_refreshed_once_then_reported(tmp_path, slack, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_CLAUDE", str(_stub_claude(tmp_path)))
    _Slack.refuse = 401
    assert comms.auth() == (None, "Slack refused Claude Code's Slack login: in Claude "
                                  "Code, run /mcp and authenticate the Slack server again")
    assert (tmp_path / "args.txt").read_text() == "mcp list\n"
    assert [m for m, _, _ in _Slack.seen] == ["initialize", "initialize"]


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
    _Slack.messages = [_message(f"{now - 95}.000100", "UALICE", "can you review PR 12?"),
                       _message(f"{now - 7200}.000100", "UALICE", "from two hours ago")]
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "DIPLOMAT_COMMS_DIR"}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), TMPDIR=str(scratch),
               PATH="/usr/bin:/bin", QT_QPA_PLATFORM="offscreen", DIPLOMAT_COMMS_POLL="1",
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


