"""OpenCode 2.x, beside 1.x: spawned, asked, priced and stopped through its per-user
service instead of a server of the run's own.

Every seam 1.x runs through is gone in 2.x — no ``--port``, no ``-m``, a ``--prompt``
that does not submit, an ``OPENCODE_PERMISSION`` the TUI ignores, no ``opencode
export`` — so each group below pins one of them to the version the CLI reports, and
the 1.x half of each stays what it was.

The service cases run against a real authenticated server on a real socket, found the
way the probe finds the real one — through ``service.json`` in ``$XDG_STATE_HOME``,
which conftest points at this test's tmp dir so nothing here can reach the operator's
own service.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from diplomat_runtime import (
    agentregistry, appconfig, opencodeapi, review, runner, szponthost, usagescan,
)
from diplomat_runtime.agentstate import RunRecord, SessionState
from diplomat_app import probes

T0 = 1_000_000.0
PROMPT = "Review PR #7 in o/r\nwith a second line and a 'quote'"
SID = "ses_diplomat_0123456789abcdef0123456789abcdef"
PASSWORD = "s3cret-service-password"

#: Captured at import, before conftest's fence replaces it for every test.
_real_spawn_macos = szponthost._spawn_macos


def fake_opencode(tmp_path: Path, monkeypatch, body: str) -> Path:
    """An ``opencode`` on PATH, a shell script answering whatever argv it is given."""
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + "/usr/bin:/bin")
    # The resolver asks the user's shell first, and a developer's rc names their own
    # install; a shell that sources nothing answers from the PATH above.
    monkeypatch.setenv("DIPLOMAT_SHELL", "/bin/sh")
    return exe


V2 = 'if [ "$1" = --version ]; then echo "opencode v2.0.18"; exit 0; fi\n'
V1 = 'if [ "$1" = --version ]; then echo "1.18.33"; exit 0; fi\n'


@pytest.fixture
def opencode(monkeypatch, tmp_path):
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.OPENCODE)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(review, "repo_path", lambda: str(repo))
    return repo


@pytest.fixture
def as_v2(monkeypatch):
    monkeypatch.setattr(usagescan, "opencode_is_v2", lambda: True)


# MARK: - Which OpenCode this is


@pytest.mark.parametrize("output, v2", [
    ("opencode v2.0.18", True),
    ("opencode v3.1.0", True),
    ("1.18.33", False),
    ("1.4.3", False),
    ("opencode dev build", False),
    ("", False),
])
def test_the_major_version_decides_which_opencode_this_is(tmp_path, monkeypatch,
                                                         output, v2):
    fake_opencode(tmp_path, monkeypatch,
                  f'[ "$1" = --version ] && echo {shlex.quote(output)}\n')
    assert usagescan.opencode_is_v2() is v2


def test_a_version_query_that_fails_is_1x(tmp_path, monkeypatch):
    """The version is printed, but the command failed — which is not an answer."""
    fake_opencode(tmp_path, monkeypatch, 'echo "opencode v2.0.18"; exit 1\n')
    assert usagescan.opencode_is_v2() is False


def test_a_version_query_that_hangs_is_1x(tmp_path, monkeypatch):
    fake_opencode(tmp_path, monkeypatch, 'sleep 5; echo "opencode v2.0.18"\n')
    monkeypatch.setattr(usagescan, "_VERSION_TIMEOUT", 0.2)
    assert usagescan.opencode_is_v2() is False


def test_no_opencode_at_all_is_1x(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    shell = tmp_path / "noshell"
    shell.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("DIPLOMAT_SHELL", str(shell))
    assert usagescan.opencode_is_v2() is False


def test_the_install_the_users_shell_runs_decides_not_the_applets_path(tmp_path,
                                                                     monkeypatch):
    """The applet's own PATH holds a 1.x; the user's rc puts a 2.x ahead of it. The
    agent runs in that shell, so it runs the 2.x — and a 1.x spawn string handed to a
    2.x CLI never submits its prompt."""
    fake_opencode(tmp_path, monkeypatch, V1)
    rc_install = tmp_path / "rc-bin" / "opencode"
    rc_install.parent.mkdir()
    rc_install.write_text("#!/bin/sh\n" + V2, encoding="utf-8")
    rc_install.chmod(0o755)
    shell = tmp_path / "rcshell"
    shell.write_text("#!/bin/sh\n"
                     f"export PATH={shlex.quote(str(rc_install.parent))}:$PATH\n"
                     'exec /bin/sh "$@"\n', encoding="utf-8")
    shell.chmod(0o755)
    monkeypatch.setenv("DIPLOMAT_SHELL", str(shell))
    assert usagescan.opencode_is_v2() is True


def test_an_upgrade_is_seen_by_the_next_spawn(tmp_path, monkeypatch):
    """The resolved path is remembered; the version at it is not — an upgrade lands in
    place, and a spawn asking a stale answer would start 2.x the 1.x way."""
    exe = fake_opencode(tmp_path, monkeypatch, V1)
    assert usagescan.opencode_is_v2() is False
    exe.write_text("#!/bin/sh\n" + V2, encoding="utf-8")
    assert usagescan.opencode_is_v2() is True


# MARK: - The 2.x spawn


def test_the_2x_agent_command_is_spelled_out(opencode):
    """Spelled out rather than built from parts: the Swift front-end builds the same
    string, and a mesh job can be handed from one platform to the other."""
    assert runner.agent_command("/tmp/p.txt", opencode_session=SID) == (
        "opencode api session.create -d \"$(cat /tmp/p.txt.session.json)\""
        f" && opencode api session.prompt --param sessionID={SID}"
        " -d \"$(cat /tmp/p.txt.prompt.json)\" || exit; opencode --session " + SID)


def test_the_2x_command_quotes_the_staged_paths_as_the_prompt_is_quoted(opencode):
    cmd = runner.agent_command("/tmp/my runs/p.txt", opencode_session=SID)
    assert "\"$(cat '/tmp/my runs/p.txt.session.json')\"" in cmd
    assert "\"$(cat '/tmp/my runs/p.txt.prompt.json')\"" in cmd


def test_the_2x_command_carries_none_of_the_1x_flags(opencode):
    """Each one is a 1.x seam 2.x does not have: ``--port`` and ``-m`` are rejected,
    ``--prompt`` does not submit, ``OPENCODE_PERMISSION`` is ignored by the TUI."""
    appconfig.set_value(appconfig.AGENT_MODEL, "openrouter/moonshotai/kimi-k3")
    cmd = runner.agent_command("/tmp/p.txt", port=47910, opencode_session=SID)
    for flag in ("--port", " -m ", "--prompt", runner.OPENCODE_PERMISSION_ENV, "--auto"):
        assert flag not in cmd


def test_the_2x_command_ends_on_the_tui_alone(opencode):
    """After a ``;``, the one position both bash 5.3 and zsh 5.9 exec over themselves,
    so the pid file names OpenCode's own TUI."""
    cmd = runner.agent_command("/tmp/p.txt", opencode_session=SID)
    assert cmd.rsplit("; ", 1)[1] == f"opencode --session {SID}"


def test_a_session_only_reaches_an_opencode_command(monkeypatch):
    """A Claude or Hermes spawn is never given the 2.x spelling, whatever it is
    handed."""
    assert runner.agent_command("/tmp/p.txt", opencode_session=SID) == \
        'claude "$(cat /tmp/p.txt)"'
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.HERMES)
    assert runner.agent_command("/tmp/p.txt", opencode_session=SID).startswith("hermes ")


def test_minted_ids_are_fresh_and_shaped_for_the_service():
    """Creating a session with an existing id returns that session rather than failing,
    so two spawns sharing an id would share a session."""
    a, b = runner.new_opencode_session(), runner.new_opencode_session()
    assert a != b
    for sid in (a, b):
        assert sid.startswith("ses_diplomat_")
        tail = sid.removeprefix("ses_diplomat_")
        assert len(tail) == 32 and all(c in "0123456789abcdef" for c in tail)


def _staged(prompt_file: Path) -> tuple[dict, dict]:
    session, prompt = runner.opencode_staged(str(prompt_file))
    return (json.loads(Path(session).read_text()), json.loads(Path(prompt).read_text()))


def _prompt_file(tmp_path: Path) -> Path:
    path = tmp_path / "run" / "prompt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROMPT, encoding="utf-8")
    return path


def test_staging_writes_both_bodies_beside_the_prompt(opencode, as_v2, tmp_path):
    prompt_file = _prompt_file(tmp_path)
    sid = review.stage_opencode_session(str(prompt_file))
    assert sid and sid.startswith("ses_diplomat_")
    session, prompt = _staged(prompt_file)
    assert session == {
        "id": sid,
        "location": {"directory": str(opencode.resolve())},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }, "no model is sent unpinned — the service picks its own"
    assert prompt == {"text": PROMPT}
    for path in runner.opencode_staged(str(prompt_file)):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("pin, model", [
    ("openai/gpt-5-mini", {"providerID": "openai", "id": "gpt-5-mini"}),
    ("openai/gpt-5.2#high", {"providerID": "openai", "id": "gpt-5.2", "variant": "high"}),
    ("openrouter/moonshotai/kimi-k3",
     {"providerID": "openrouter", "id": "moonshotai/kimi-k3"}),
    ("openrouter/a/b#c#fast",
     {"providerID": "openrouter", "id": "a/b#c", "variant": "fast"}),
])
def test_a_pinned_model_is_split_the_way_the_2x_tui_splits_it(opencode, as_v2,
                                                               tmp_path, pin, model):
    appconfig.set_value(appconfig.AGENT_MODEL, pin)
    prompt_file = _prompt_file(tmp_path)
    review.stage_opencode_session(str(prompt_file))
    assert _staged(prompt_file)[0]["model"] == model


def test_the_session_is_created_in_the_checkouts_real_path(opencode, as_v2, tmp_path,
                                                           monkeypatch):
    link = tmp_path / "link"
    link.symlink_to(opencode)
    monkeypatch.setattr(review, "repo_path", lambda: str(link))
    prompt_file = _prompt_file(tmp_path)
    review.stage_opencode_session(str(prompt_file))
    assert _staged(prompt_file)[0]["location"]["directory"] == str(opencode.resolve())


def test_a_1x_spawn_stages_nothing(opencode, tmp_path, monkeypatch):
    monkeypatch.setattr(usagescan, "opencode_is_v2", lambda: False)
    prompt_file = _prompt_file(tmp_path)
    assert review.stage_opencode_session(str(prompt_file)) is None
    assert sorted(p.name for p in prompt_file.parent.iterdir()) == ["prompt.txt"]


def test_another_runner_never_asks_which_opencode_is_installed(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("asked OpenCode's version for a Claude spawn")

    monkeypatch.setattr(usagescan, "opencode_is_v2", refuse)
    assert review.stage_opencode_session(str(_prompt_file(tmp_path))) is None


def test_a_2x_spawn_without_a_pid_file_still_exits_its_own_shell(opencode, monkeypatch):
    """The mesh node spawns with no pid file, and the 2.x string's ``|| exit`` would
    otherwise end the WINDOW's shell before the exit sentinel is written."""
    monkeypatch.setattr(review, "user_shell", lambda: "/bin/zsh")
    cmd = review.shell_command("/tmp/p.txt", "/tmp/done", opencode_session=SID)
    agent = runner.agent_command("/tmp/p.txt", opencode_session=SID)
    assert f"; /bin/zsh -i -c {shlex.quote(agent)}; {{ printf %s $? > /tmp/done;" in cmd


def test_a_2x_spawn_with_a_pid_file_records_the_pid_first(opencode, monkeypatch):
    monkeypatch.setattr(review, "user_shell", lambda: "/bin/zsh")
    cmd = review.shell_command("/tmp/p.txt", "/tmp/done", "/tmp/pid",
                               opencode_session=SID)
    agent = runner.agent_command("/tmp/p.txt", opencode_session=SID)
    assert shlex.quote(f"printf %s $$ > /tmp/pid; {agent}") in cmd


# MARK: - Binding at spawn


def _spawned(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_spawn(prompt, preferred, **kwargs):
        calls.append(kwargs)
        return kwargs.get("prompt_file") or "/tmp/p.txt"

    monkeypatch.setattr(review, "spawn", fake_spawn)
    return calls


def _spawn_tracked() -> str:
    from diplomat_app.store import Store

    assert Store._spawn_tracked(Store(), PROMPT, "https://github.com/o/r/pull/7", 7,
                                "click")
    return agentregistry.load()[0].run_id


def test_a_2x_run_is_bound_at_spawn_and_given_no_port(opencode, as_v2, monkeypatch):
    calls = _spawned(monkeypatch)
    run_id = _spawn_tracked()
    sid = agentregistry.bound_session(run_id)
    assert sid.startswith("ses_diplomat_")
    assert calls[0]["opencode_session"] == sid
    assert calls[0]["port"] is None
    assert not agentregistry.port_path(run_id).exists()
    assert agentregistry.service_session(run_id) == sid
    session, prompt = _staged(agentregistry.prompt_path(run_id))
    assert session["id"] == sid and prompt == {"text": PROMPT}


def test_a_1x_run_gets_its_port_and_binds_later(opencode, monkeypatch):
    monkeypatch.setattr(usagescan, "opencode_is_v2", lambda: False)
    calls = _spawned(monkeypatch)
    run_id = _spawn_tracked()
    assert "opencode_session" not in calls[0]
    assert calls[0]["port"] == agentregistry.port(run_id) is not None
    assert agentregistry.bound_session(run_id) == ""
    assert agentregistry.service_session(run_id) == ""


def test_a_mesh_job_on_2x_is_started_through_the_service(opencode, as_v2, monkeypatch,
                                                         tmp_path):
    """No run directory to bind into, but the spawn itself must still be the 2.x one —
    the 1.x spelling starts nothing on 2.x."""
    monkeypatch.setattr(szponthost.platform, "system", lambda: "Linux")
    monkeypatch.setattr(review, "write_prompt",
                        lambda prompt: str(_prompt_file(tmp_path)))
    calls = _spawned(monkeypatch)
    szponthost.DiplomatHost().run_job(PROMPT, str(tmp_path / "job.done"))
    sid = calls[0]["opencode_session"]
    assert sid.startswith("ses_diplomat_")
    assert _staged(Path(calls[0]["prompt_file"]))[0]["id"] == sid


def test_a_mesh_job_on_macos_is_started_through_the_service(opencode, as_v2,
                                                            monkeypatch, tmp_path):
    launched: list[list] = []
    monkeypatch.setattr(szponthost, "_spawn_macos", _real_spawn_macos)
    monkeypatch.setattr(szponthost.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(review, "write_prompt",
                        lambda prompt: str(_prompt_file(tmp_path)))
    monkeypatch.setattr(review, "popen_detached", lambda argv: launched.append(argv))
    szponthost.DiplomatHost().run_job(PROMPT, None)
    assert "opencode api session.create" in launched[0][2]
    assert "--prompt" not in launched[0][2]


# MARK: - Asking the service


class _Service(BaseHTTPRequestHandler):
    """A stand-in 2.x service: Basic auth on every ``/api`` route, canned JSON."""

    routes: dict[str, tuple[int, object]] = {}
    seen: list[tuple[str, str, str | None]] = []

    def _answer(self):
        type(self).seen.append((self.command, self.path,
                                self.headers.get("Authorization")))
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            status, body = 401, {"_tag": "Unauthorized"}
        else:
            status, body = self.routes.get((self.command, self.path),
                                           (404, {"_tag": "SessionNotFoundError"}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args):
        pass


def _service_json(url: str, password: str | None = PASSWORD) -> None:
    path = Path(os.environ["XDG_STATE_HOME"]) / "opencode" / "service.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"id": "svc", "version": "2.0.18", "url": url, "pid": 1}
    if password is not None:
        info["password"] = password
    path.write_text(json.dumps(info), encoding="utf-8")


@pytest.fixture
def service():
    _Service.routes = {}
    _Service.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _Service.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    _service_json(_Service.url)
    yield _Service
    httpd.shutdown()
    httpd.server_close()


def _session(**time) -> tuple[int, dict]:
    return 200, {"data": {"id": SID, "time": {"created": 1.0, "updated": 2.0, **time}}}


def test_a_session_in_the_active_map_is_busy(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {SID: {"type": "running"}}})
    assert opencodeapi.service_state(SID) == SessionState(busy=True)


def test_a_session_whose_turn_ended_is_idle(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {"ses_other": {}}})
    assert opencodeapi.service_state(SID) == SessionState(busy=False)


def test_a_session_whose_first_turn_has_not_ended_is_busy(service):
    """Created but not yet running is absent from the map too — the moment between
    ``session.create`` and the turn starting. Idle would retire the run at launch."""
    service.routes[("GET", f"/api/session/{SID}")] = _session()
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert opencodeapi.service_state(SID) == SessionState(busy=True)


def test_a_session_the_service_does_not_know_is_unreachable(service):
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert opencodeapi.service_state(SID) is None


def test_an_active_map_the_service_will_not_give_is_unreachable(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (500, {"_tag": "Boom"})
    assert opencodeapi.service_state(SID) is None


@pytest.mark.parametrize("body", [{"data": []}, {"nope": 1}, [1, 2]])
def test_a_malformed_answer_is_unreachable(service, body):
    service.routes[("GET", f"/api/session/{SID}")] = (200, body)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert opencodeapi.service_state(SID) is None


def test_every_request_carries_the_services_password(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    opencodeapi.service_state(SID)
    want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    assert [auth for _m, _p, auth in service.seen] == [want, want]


def test_without_the_password_the_service_answers_nothing(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    _service_json(service.url, password=None)
    assert opencodeapi.service_state(SID) is None
    assert service.seen and service.seen[0][2] is None


def test_no_service_file_is_unreachable():
    assert opencodeapi.service_state(SID) is None


def test_a_service_that_is_gone_is_unreachable():
    _service_json(f"http://127.0.0.1:{opencodeapi.free_port()}")
    assert opencodeapi.service_state(SID) is None


def test_the_service_file_is_found_under_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert opencodeapi.service_file() == str(tmp_path / "s" / "opencode" / "service.json")
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert opencodeapi.service_file() == str(
        tmp_path / "home" / ".local" / "state" / "opencode" / "service.json")


def _v2_run(run_id: str = "r1", session_id: str = SID) -> RunRecord:
    record = RunRecord(run_id=run_id, dispatched_at=T0, pid=4242)
    agentregistry.create_run(record, PROMPT)
    agentregistry.runner_path(run_id).write_text(runner.OPENCODE, encoding="utf-8")
    agentregistry.bind_session(run_id, session_id)
    return record


def test_a_2x_run_is_asked_of_the_service_by_the_probe(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session()
    service.routes[("GET", "/api/session/active")] = (200, {"data": {SID: {"type": "running"}}})
    obs = probes.agent_sessions([_v2_run()], "/repo", T0)
    assert obs.ok and obs.value == {"r1": SessionState(busy=True)}
    probes.reset_cache()
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=9.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    obs = probes.agent_sessions([_v2_run()], "/repo", T0 + 60)
    assert obs.ok and obs.value == {"r1": SessionState(busy=False)}


def test_a_1x_run_is_never_sent_to_the_service(service):
    """A bound 1.x run has a port, and its session lives on that port's server."""
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=9.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    record = _v2_run()
    agentregistry.port_path("r1").write_text(str(opencodeapi.free_port()))
    obs = probes.agent_sessions([record], "/repo", T0)
    assert obs.ok and "r1" not in obs.value
    assert service.seen == []


# MARK: - Pricing


EXPORT = {"info": {"id": SID}, "messages": [
    {"type": "user", "id": "m1", "text": "hi"},
    {"type": "assistant", "id": "m2", "cost": 0.01,
     "tokens": {"input": 12510, "output": 2, "reasoning": 49,
                "cache": {"read": 900, "write": 7}}},
    {"type": "idle", "id": "m3", "outcome": "succeeded"},
]}


def _exporter(version: str, subcommand: str) -> str:
    """A CLI that prints its version, and the export only under ``subcommand``."""
    return (version
            + f'if [ "$*" = {shlex.quote(subcommand + " " + SID)} ]; then\n'
            + "cat <<'JSON'\n" + json.dumps(EXPORT) + "\nJSON\nexit 0; fi\n"
            + 'echo "unknown command: $*" >&2; exit 1\n')


def test_a_2x_run_is_priced_from_session_export(tmp_path, monkeypatch):
    fake_opencode(tmp_path, monkeypatch, _exporter(V2, "session export"))
    assert usagescan.opencode_task_tokens(SID) == 12510 + 2 + 7


def test_a_1x_run_is_priced_from_export(tmp_path, monkeypatch):
    fake_opencode(tmp_path, monkeypatch, _exporter(V1, "export"))
    assert usagescan.opencode_task_tokens(SID) == 12510 + 2 + 7


# MARK: - Stopping


def test_an_interrupt_is_posted_with_the_password(service):
    service.routes[("POST", f"/api/session/{SID}/interrupt")] = (200, {"interrupted": True})
    assert opencodeapi.interrupt(SID) is True
    want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    assert service.seen == [("POST", f"/api/session/{SID}/interrupt", want)]


def test_an_interrupt_nobody_takes_never_raises(service):
    assert opencodeapi.interrupt("ses_unknown") is False
    (Path(os.environ["XDG_STATE_HOME"]) / "opencode" / "service.json").unlink()
    assert opencodeapi.interrupt(SID) is False


def _reap(monkeypatch, records: list[RunRecord]) -> list[tuple[str, str]]:
    """Close each run's window the way the backstops do, recording in order what was
    interrupted and what was killed."""
    from diplomat_app.store import Store
    from diplomat_runtime import tmuxwatch

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(opencodeapi, "interrupt",
                        lambda sid: events.append(("interrupt", sid)) or True)
    monkeypatch.setattr(tmuxwatch, "kill_session",
                        lambda name: events.append(("kill", name)) or True)
    tick = types.SimpleNamespace(
        reapable=records, now=T0,
        states={r.run_id: types.SimpleNamespace(reason="wedged") for r in records})
    assert Store._reap_wedged_windows(Store(), tick) == set()
    return events


def test_closing_a_2x_runs_window_interrupts_its_turn_first(monkeypatch):
    from diplomat_runtime import tmuxwatch

    events = _reap(monkeypatch, [_v2_run()])
    assert events == [("interrupt", SID), ("kill", tmuxwatch.session_name("r1"))]


def test_closing_a_1x_or_claude_runs_window_interrupts_nothing(monkeypatch):
    one_x = _v2_run("r1", "ses_1x")
    agentregistry.port_path("r1").write_text("47910")
    claude = RunRecord(run_id="r2", dispatched_at=T0, pid=4243)
    agentregistry.create_run(claude, PROMPT)
    agentregistry.runner_path("r2").write_text(runner.CLAUDE, encoding="utf-8")
    events = _reap(monkeypatch, [one_x, claude])
    assert [kind for kind, _ in events] == ["kill", "kill"]
