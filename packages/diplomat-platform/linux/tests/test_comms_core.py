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
import time
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


