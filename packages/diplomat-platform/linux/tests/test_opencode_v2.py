"""OpenCode 2.x, beside 1.x: spawned, asked, priced and stopped through its per-user
service instead of a server of the run's own.

Every seam 1.x runs through is gone in 2.x — no ``--port``, no ``-m``, a ``--prompt``
that does not submit, an ``OPENCODE_PERMISSION`` the TUI ignores, no ``opencode
export`` — so each group below pins one of them to the version the CLI reports, and
the 1.x half of each stays what it was.

The service cases run against ``_Service``, a stand-in that checks the password and
answers canned JSON on a real socket, found the way the probe finds the real one —
through ``service.json`` in ``$XDG_STATE_HOME``, which conftest points at this test's
tmp dir so nothing here can reach the operator's own service.
"""

from __future__ import annotations

import base64
import dataclasses
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


def quiet_shell(directory: Path, prelude: str = "") -> str:
    """A user shell that runs ``prelude`` and sources nothing else, whether asked to log
    in or be interactive: ``/bin/sh -l`` would read the developer's ``~/.profile``."""
    shell = directory / "quiet-shell"
    shell.write_text("#!/bin/sh\n" + prelude +
                     'while [ "$#" -gt 0 ]; do case "$1" in -l|-i) shift ;; *) break ;; esac; done\n'
                     'exec /bin/sh "$@"\n', encoding="utf-8")
    shell.chmod(0o755)
    return str(shell)


def fake_opencode(tmp_path: Path, monkeypatch, body: str) -> Path:
    """An ``opencode`` on PATH, a shell script answering whatever argv it is given."""
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + "/usr/bin:/bin")
    # The resolver asks the user's shell, whose profile and rc name the developer's own
    # install; one that sources nothing answers from the PATH above.
    monkeypatch.setenv("DIPLOMAT_SHELL", quiet_shell(tmp_path))
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
    monkeypatch.setenv("DIPLOMAT_SHELL", quiet_shell(
        tmp_path, f"export PATH={shlex.quote(str(rc_install.parent))}:$PATH\n"))
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
    """Spelled out rather than built from parts, so a change to the command is a change
    to this string. The Swift front-end builds the same command, but quotes every path
    where this one leaves a path of safe characters bare; a shell reads both alike."""
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


def _ask(session_id: str) -> SessionState | None:
    """A session's state, asked the way one probe pass asks it."""
    return opencodeapi.service_state(session_id, opencodeapi.active_sessions())


def test_a_session_in_the_active_map_is_busy(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {SID: {"type": "running"}}})
    assert _ask(SID) == SessionState(busy=True)


def test_a_session_whose_turn_ended_is_idle(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {"ses_other": {}}})
    assert _ask(SID) == SessionState(busy=False)


def test_a_session_whose_first_turn_has_not_ended_is_busy(service):
    """Created but not yet running is absent from the map too — the moment between
    ``session.create`` and the turn starting. Idle would retire the run at launch."""
    service.routes[("GET", f"/api/session/{SID}")] = _session()
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert _ask(SID) == SessionState(busy=True)


def test_a_session_the_service_does_not_know_is_unreachable(service):
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert _ask(SID) is None


def test_an_active_map_the_service_will_not_give_is_unreachable(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (500, {"_tag": "Boom"})
    assert _ask(SID) is None


@pytest.mark.parametrize("body", [{"data": []}, {"nope": 1}, [1, 2]])
def test_a_malformed_answer_is_unreachable(service, body):
    service.routes[("GET", f"/api/session/{SID}")] = (200, body)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    assert _ask(SID) is None


def test_every_request_carries_the_services_password(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    _ask(SID)
    want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    assert [auth for _m, _p, auth in service.seen] == [want, want]


def test_without_the_password_the_service_answers_nothing(service):
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=5.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    _service_json(service.url, password=None)
    assert _ask(SID) is None
    assert service.seen and service.seen[0][2] is None


def test_no_service_file_is_unreachable():
    assert _ask(SID) is None


def test_a_service_that_is_gone_is_unreachable():
    _service_json(f"http://127.0.0.1:{opencodeapi.free_port()}")
    assert _ask(SID) is None


def test_the_service_file_is_found_under_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert opencodeapi.service_file() == str(tmp_path / "s" / "opencode" / "service.json")
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    usagescan._reset_cache()
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
    opencodeapi.interrupt(SID)
    want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    assert service.seen == [("POST", f"/api/session/{SID}/interrupt", want)]


def test_an_interrupt_nobody_takes_never_raises(service):
    opencodeapi.interrupt("ses_unknown")
    (Path(os.environ["XDG_STATE_HOME"]) / "opencode" / "service.json").unlink()
    opencodeapi.interrupt(SID)
    assert [path for _m, path, _a in service.seen] == ["/api/session/ses_unknown/interrupt"]


def _reap(monkeypatch, records: list[RunRecord]) -> list[tuple[str, str]]:
    """Close each run's window the way the backstops do, recording in order what was
    interrupted and what was killed."""
    from diplomat_app.store import Store
    from diplomat_runtime import tmuxwatch

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(opencodeapi, "interrupt",
                        lambda sid: events.append(("interrupt", sid)))
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


def _retire(monkeypatch, records: list[RunRecord], reaped: tuple[str, ...] = ()
            ) -> list[tuple[str, str]]:
    """Retire each run the way the tick does once its agent is gone, recording in order
    what was interrupted and what was priced."""
    from diplomat_app.store import Store
    from diplomat_runtime import telemetry, tmuxwatch

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(opencodeapi, "interrupt",
                        lambda sid: events.append(("interrupt", sid)))
    monkeypatch.setattr(tmuxwatch, "kill_session", lambda name: True)
    monkeypatch.setattr(telemetry, "record_completion",
                        lambda key, *a, **kw: events.append(("price", key)))
    tick = types.SimpleNamespace(
        reapable=[r for r in records if r.run_id in reaped],
        retirable=records, now=T0,
        states={r.run_id: types.SimpleNamespace(state="finished", reason="pid absent")
                for r in records})
    Store._retire_finished(Store(), tick)
    return events


def test_retiring_a_2x_run_interrupts_its_turn_before_pricing_it(monkeypatch):
    """Its TUI gone — the window closed by hand, the TUI quit or crashed — the turn
    goes on in the service; a 1.x agent died with its window."""
    record = dataclasses.replace(_v2_run(), ledger_key="k1")
    assert _retire(monkeypatch, [record]) == [("interrupt", SID), ("price", "k1")]


def test_retiring_a_1x_or_claude_run_interrupts_nothing(monkeypatch):
    one_x = _v2_run("r1", "ses_1x")
    agentregistry.port_path("r1").write_text("47910")
    claude = RunRecord(run_id="r2", dispatched_at=T0, pid=4243)
    agentregistry.create_run(claude, PROMPT)
    agentregistry.runner_path("r2").write_text(runner.CLAUDE, encoding="utf-8")
    assert [e for e in _retire(monkeypatch, [one_x, claude]) if e[0] == "interrupt"] == []


def test_a_run_the_reaper_closed_is_interrupted_once(monkeypatch):
    events = _retire(monkeypatch, [_v2_run()], reaped=("r1",))
    assert events.count(("interrupt", SID)) == 1


# MARK: - A 2.x run with no pid, found by its session


OTHER = "ses_diplomat_ffffffffffffffffffffffffffffffff"


def _opening(service, session_id: str, text: str | None) -> None:
    rows = [] if text is None else [{"type": "user", "id": "m1", "text": text}]
    service.routes[("GET", f"/api/session/{session_id}/message?limit=1&order=asc")] = (
        200, {"data": rows})


def _ps(*argvs: str) -> probes.Observation:
    """A ``pid tty etimes args`` dump, as :func:`probes._ps_dump` reads it."""
    return probes.Observation.present("".join(
        f"{900 + i} pts/{i} 30 {argv}\n" for i, argv in enumerate(argvs)))


@pytest.fixture
def repo_o_r(monkeypatch):
    from diplomat_runtime import core

    monkeypatch.setattr(core, "config", lambda: {"owner": "o", "repo": "r"})


def test_a_2x_tui_is_found_by_the_prompt_its_session_opened_on(service, repo_o_r):
    """Its argv is ``opencode --session <id>`` and nothing else; a scan of argvs alone
    reads a mesh-placed 2.x agent as no agent and retires it mid-turn."""
    _opening(service, SID, PROMPT)
    dump = _ps(f"opencode --session {SID}")
    assert probes.live_agents(dump).value == {7: "pts/0"}


#: What the process table holds around one 2.x TUI (2.0.18, one tmux pane): the
#: terminal, the tmux client and server and the pane's shells all carry the spawn
#: command, TUI invocation included, and none of them is on the TUI's tty.
WRAPPERS = (
    "script -q /dev/null tmux -L x new-session -s y zsh -i -c "
    f"'cd /tmp; zsh -i -c \"sleep 1; opencode --session {SID}\"; exec sh'",
    "tmux -L x new-session -s y zsh -i -c "
    f"'cd /tmp; zsh -i -c \"sleep 1; opencode --session {SID}\"; exec sh'",
    f"zsh -i -c cd /tmp; zsh -i -c \"sleep 1; opencode --session {SID}\"; exec sh",
    f"zsh -i -c sleep 1; opencode --session {SID}",
    f"xterm -e bash -c 'cd /tmp; opencode --session {SID}'",
)


@pytest.mark.parametrize("argv, session", [
    ("opencode --session ses_x", "ses_x"),
    ("/home/u/.npm/bin/opencode --session ses_x", "ses_x"),
    ("C:/npm/opencode.exe --session ses_A-9_z", "ses_A-9_z"),
    *[(w, None) for w in WRAPPERS],
    ("opencode --session ses_x --prompt hi", None),
    ("opencode-dev --session ses_x", None),
    ("opencode --session ses", None),
    ("opencode --session ses_x'", None),
])
def test_only_a_2x_tuis_own_argv_names_its_session(argv, session):
    assert opencodeapi.session_arg(argv) == session


def test_the_shells_around_a_2x_tui_are_not_its_sighting(service, repo_o_r):
    """Each wrapper is older than the TUI, so it comes first in the table; taken for
    the agent, the PR gets a tty no pane is on and reads as working forever."""
    _opening(service, SID, PROMPT)
    dump = probes.Observation.present(
        "".join(f"{100 + i} ? 9 {w}\n" for i, w in enumerate(WRAPPERS))
        + f"200 pts/5 8 opencode --session {SID}\n")
    assert probes.live_agents(dump).value == {7: "pts/5"}


def test_the_mesh_node_takes_a_2x_tuis_tty_not_its_wrappers(service):
    from diplomat_runtime import autofix

    _opening(service, SID, PROMPT)
    dump = ("".join(f"?? 00:09 {w}\n" for w in WRAPPERS)
            + f"ttys005 00:08 /opt/npm/bin/opencode --session {SID}\n")
    assert autofix.agent_ttys(dump, "o", "r") == {"ttys005"}
    assert autofix.agent_ttys(f"opencode --session {SID}\n", "o", "r") == {"opencode"}


def test_the_mesh_dedup_sees_a_2x_tui_too(service):
    from diplomat_runtime import autofix

    _opening(service, SID, PROMPT)
    assert autofix.live_pr_numbers(f"pts/3 00:30 opencode --session {SID}\n",
                                   "o", "r") == {7}


def test_an_opening_prompt_is_asked_once_per_session(service, repo_o_r):
    _opening(service, SID, PROMPT)
    dump = _ps(f"opencode --session {SID}")
    for _ in range(3):
        assert probes.live_agents(dump).value == {7: "pts/0"}
    assert len(service.seen) == 1


def test_a_session_not_yet_prompted_is_asked_again_after_a_while(service, repo_o_r,
                                                                  monkeypatch):
    """Its message list is empty until ``session.prompt`` lands; remembering that for
    good would hide the run for good."""
    now = [T0]
    monkeypatch.setattr(opencodeapi.time, "monotonic", lambda: now[0])
    _opening(service, SID, None)
    dump = _ps(f"opencode --session {SID}")
    assert probes.live_agents(dump).value == {}
    _opening(service, SID, PROMPT)
    now[0] += opencodeapi.MISS_TTL - 1
    assert probes.live_agents(dump).value == {}
    now[0] += 1
    assert probes.live_agents(dump).value == {7: "pts/0"}


def test_a_service_that_hangs_is_not_asked_on_every_scan(repo_o_r, monkeypatch):
    """Each ask costs :data:`opencodeapi.TIMEOUT`, per TUI, and the panel scans twice
    per rebuild on the Qt thread."""
    asked = []
    monkeypatch.setattr(opencodeapi, "_service_call",
                        lambda path, method="GET": asked.append(path))
    dump = _ps(f"opencode --session {SID}", f"opencode --session {OTHER}")
    for _ in range(3):
        assert probes.live_agents(dump).value == {}
    assert len(asked) == 2


def test_a_service_that_will_not_answer_matches_nothing(repo_o_r):
    _service_json(f"http://127.0.0.1:{opencodeapi.free_port()}")
    assert probes.live_agents(_ps(f"opencode --session {SID}")).value == {}


def _mesh_here_run(run_id: str = "m1") -> RunRecord:
    """A run the mesh placed back on this machine: the node opened its window, so there
    is no pid and no session bound at spawn."""
    from diplomat_runtime import agentstate

    record = RunRecord(run_id=run_id, dispatched_at=T0, pr_number=7,
                       placement=agentstate.PLACEMENT_MESH_HERE)
    agentregistry.create_run(record, PROMPT)
    agentregistry.runner_path(run_id).write_text(runner.OPENCODE, encoding="utf-8")
    return record


def _busy(service, session_id: str = SID) -> None:
    service.routes[("GET", f"/api/session/{session_id}")] = _session()
    service.routes[("GET", "/api/session/active")] = (200, {"data": {session_id: {}}})


def test_a_mesh_placed_2x_run_is_bound_by_its_opening_prompt_and_asked(service,
                                                                     monkeypatch):
    _opening(service, OTHER, "Review PR #8 in o/r")
    _opening(service, SID, PROMPT)
    _busy(service)
    monkeypatch.setattr(probes, "_ps_dump", lambda now: _ps(
        f"opencode --session {OTHER}", f"opencode --session {SID}"))
    obs = probes.agent_sessions([_mesh_here_run()], "/repo", T0)
    assert obs.value == {"m1": SessionState(busy=True)}
    assert agentregistry.service_session("m1") == SID


def test_a_session_another_run_holds_is_not_bound_again(service, monkeypatch):
    _opening(service, SID, PROMPT)
    _busy(service)
    monkeypatch.setattr(probes, "_ps_dump", lambda now: _ps(f"opencode --session {SID}"))
    probes.agent_sessions([_v2_run("r1"), _mesh_here_run()], "/repo", T0)
    assert agentregistry.bound_session("m1") == ""


def test_a_bound_mesh_placed_run_is_interrupted_when_retired(service, monkeypatch):
    _opening(service, SID, PROMPT)
    _busy(service)
    monkeypatch.setattr(probes, "_ps_dump", lambda now: _ps(f"opencode --session {SID}"))
    record = _mesh_here_run()
    probes.agent_sessions([record], "/repo", T0)
    assert ("interrupt", SID) in _retire(monkeypatch, [record])


def test_one_probe_pass_asks_for_the_active_map_once(service):
    for sid in (SID, OTHER):
        service.routes[("GET", f"/api/session/{sid}")] = _session()
    service.routes[("GET", "/api/session/active")] = (200, {"data": {SID: {}}})
    obs = probes.agent_sessions([_v2_run("r1"), _v2_run("r2", OTHER)], "/repo", T0)
    assert obs.value == {"r1": SessionState(busy=True), "r2": SessionState(busy=True)}
    assert [p for _m, p, _a in service.seen].count("/api/session/active") == 1


def test_a_pass_without_the_active_map_asks_nothing_more(service):
    """The map is half of every answer, so a service that will not give it has
    answered for every run already — asking each session as well is one more timeout
    per run against a service that may be hung."""
    service.routes[("GET", "/api/session/active")] = (500, {"_tag": "Boom"})
    service.routes[("GET", f"/api/session/{SID}")] = _session()
    obs = probes.agent_sessions([_v2_run("r1"), _v2_run("r2", OTHER)], "/repo", T0)
    assert obs.value == {}
    assert [p for _m, p, _a in service.seen] == ["/api/session/active"]


# MARK: - A 2.x agent nobody booked


def _untracked(pr: int = 7) -> RunRecord:
    """A row synthesized from the process table: no run directory, no runner."""
    return RunRecord(run_id=f"untracked:{pr}", dispatched_at=T0, pr_number=pr,
                     tty="pts/0", untracked=True)


def _gather(monkeypatch, records: list[RunRecord], *argvs: str):
    from diplomat_runtime import tmuxwatch

    monkeypatch.setattr(probes, "_ps_dump", lambda now: _ps(*argvs))
    monkeypatch.setattr(tmuxwatch, "pane_tails_for_ttys", lambda ttys: {})
    return probes.gather(records, T0)


def test_an_untracked_2x_agent_is_asked_of_the_service(service, repo_o_r, monkeypatch):
    """Its screen is the fallback, not the answer: a turn the service is running reads
    as running, and an ended one ends the row, exactly as for a run spawned here."""
    _opening(service, SID, PROMPT)
    _busy(service)
    evidence = _gather(monkeypatch, [_untracked()], f"opencode --session {SID}")
    assert evidence.sessions.value == {"untracked:7": SessionState(busy=True)}
    probes._sessions_cache = None
    service.routes[("GET", f"/api/session/{SID}")] = _session(idle=9.0)
    service.routes[("GET", "/api/session/active")] = (200, {"data": {}})
    evidence = _gather(monkeypatch, [_untracked()], f"opencode --session {SID}")
    assert evidence.sessions.value == {"untracked:7": SessionState(busy=False)}


def test_the_scan_names_the_session_of_a_prs_first_2x_tui(service, repo_o_r):
    _opening(service, SID, PROMPT)
    _opening(service, OTHER, PROMPT)
    sessions: dict[int, str] = {}
    probes.live_agents(_ps(f"claude 'Review PR #7 in o/r'", f"opencode --session {SID}",
                           f"opencode --session {OTHER}"), sessions)
    assert sessions == {7: SID}


def test_only_an_untracked_run_is_given_the_session_on_its_pr(service, repo_o_r,
                                                              monkeypatch):
    local = RunRecord(run_id="r1", dispatched_at=T0, pr_number=7, pid=4242)
    _opening(service, SID, PROMPT)
    _gather(monkeypatch, [local, _untracked(), _untracked(8)], f"opencode --session {SID}")
    assert [probes.service_session(r) for r in (local, _untracked(), _untracked(8))] == [
        "", SID, ""]


def test_an_untracked_2x_agent_whose_window_closed_is_interrupted(service, repo_o_r,
                                                                  monkeypatch):
    """Found once, its session is kept past the sighting — the TUI going is what
    retires the row, and the turn goes on in the service without it."""
    _opening(service, SID, PROMPT)
    _gather(monkeypatch, [_untracked()], f"opencode --session {SID}")
    _gather(monkeypatch, [_untracked()])
    assert _retire(monkeypatch, [_untracked()]) == [("interrupt", SID)]
    assert probes.service_session(_untracked()) == ""


def test_an_untracked_2x_agent_is_interrupted_when_its_window_is_reaped(
        service, repo_o_r, monkeypatch):
    from diplomat_runtime import tmuxwatch

    _opening(service, SID, PROMPT)
    _gather(monkeypatch, [_untracked()], f"opencode --session {SID}")
    assert _reap(monkeypatch, [_untracked()]) == [
        ("interrupt", SID), ("kill", tmuxwatch.session_name("untracked:7"))]


# MARK: - Where the CLI and its state are


def test_a_binary_that_is_gone_is_resolved_again(tmp_path, monkeypatch):
    """The usual 1.x to 2.x upgrade moves the binary; a path remembered past that
    spawns the 1.x way at a 2.x CLI, which rejects ``--port`` every time."""
    old = fake_opencode(tmp_path, monkeypatch, V1)
    assert usagescan.opencode_is_v2() is False
    old.unlink()
    new = tmp_path / "npm-bin" / "opencode"
    new.parent.mkdir()
    new.write_text("#!/bin/sh\n" + V2, encoding="utf-8")
    new.chmod(0o755)
    monkeypatch.setenv("PATH", f"{new.parent}:/usr/bin:/bin")
    assert usagescan.opencode_binary() == str(new)
    assert usagescan.opencode_is_v2() is True


def test_a_resolution_is_trusted_for_a_minute(tmp_path, monkeypatch):
    """An install put ahead of the old one moves nothing that exists, so only age
    retires the answer."""
    fake_opencode(tmp_path, monkeypatch, V1)
    now = [T0]
    monkeypatch.setattr(usagescan.time, "time", lambda: now[0])
    first = usagescan.opencode_binary()
    ahead = tmp_path / "ahead" / "opencode"
    ahead.parent.mkdir()
    ahead.write_text("#!/bin/sh\n" + V2, encoding="utf-8")
    ahead.chmod(0o755)
    monkeypatch.setenv("PATH", f"{ahead.parent}:{os.environ['PATH']}")
    now[0] += 59
    assert usagescan.opencode_binary() == first
    now[0] += 2
    assert usagescan.opencode_binary() == str(ahead)


def test_the_version_is_asked_once_per_binary_on_disk(tmp_path, monkeypatch):
    """Every spawn, probe and prompt build asks which major this is, on the Qt thread
    among others; the answer changes only when the file does."""
    log = tmp_path / "asked"
    tally = f"echo x >> {shlex.quote(str(log))}\n"
    exe = fake_opencode(tmp_path, monkeypatch, tally + V1)
    for _ in range(3):
        assert usagescan.opencode_major() == 1
    assert log.read_text().count("x") == 1
    exe.write_text("#!/bin/sh\n" + tally + V2, encoding="utf-8")
    assert usagescan.opencode_major() == 2
    assert log.read_text().count("x") == 2


def test_the_shell_is_asked_as_a_login_shell_running_an_interactive_one(monkeypatch):
    """What a terminal window runs: the login shell, and Diplomat's interactive one
    inside it (:func:`review.shell_command`)."""
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, stdin=kw.get("stdin"))
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setenv("DIPLOMAT_SHELL", "/bin/zsh")
    monkeypatch.setattr(usagescan.subprocess, "run", run)
    usagescan.opencode_binary()
    probe = "command -v opencode; printf '\\n@@XDG_STATE_HOME=%s\\n' \"$XDG_STATE_HOME\""
    assert seen == {"argv": ["/bin/zsh", "-l", "-c", "/bin/zsh -i -c " + shlex.quote(probe)],
                    "stdin": usagescan.subprocess.DEVNULL}


@pytest.mark.parametrize("shell, profile", [
    ("/bin/zsh", ".zprofile"),
    ("/bin/bash", ".bash_profile"),
])
def test_an_install_only_a_login_profile_names_is_found(tmp_path, monkeypatch,
                                                        shell, profile):
    """Homebrew's installer puts its PATH in ``~/.zprofile``, which only a login shell
    reads; the applet's own PATH, from a Dock icon or desktop entry, has none of it."""
    if not os.access(shell, os.X_OK):
        pytest.skip(f"no {shell}")
    exe = tmp_path / "brew" / "opencode"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n" + V2, encoding="utf-8")
    exe.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / profile).write_text(f"export PATH={shlex.quote(str(exe.parent))}:$PATH\n",
                                encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ZDOTDIR", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("DIPLOMAT_SHELL", shell)
    assert usagescan.opencode_binary() == str(exe)


def test_the_service_is_found_under_the_state_home_the_shell_gives(tmp_path,
                                                                  monkeypatch):
    """The service is started from the agent's shell, so its ``service.json`` is under
    that shell's ``$XDG_STATE_HOME`` — which an rc may set and the applet's environment
    never saw."""
    fake_opencode(tmp_path, monkeypatch, V2)
    monkeypatch.setenv("DIPLOMAT_SHELL", quiet_shell(
        tmp_path, f"export XDG_STATE_HOME={tmp_path}/rc-state\n"))
    assert opencodeapi.service_file() == f"{tmp_path}/rc-state/opencode/service.json"


def test_a_shell_with_no_state_home_means_the_default(tmp_path, monkeypatch):
    fake_opencode(tmp_path, monkeypatch, V2)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DIPLOMAT_SHELL", quiet_shell(tmp_path, "unset XDG_STATE_HOME\n"))
    assert opencodeapi.service_file() == str(
        tmp_path / "home" / ".local" / "state" / "opencode" / "service.json")


def test_an_rc_that_prints_paths_does_not_name_the_binary(tmp_path, monkeypatch):
    """The answer is the last executable path before the marker, so a banner — even
    one naming an executable — ahead of it is not mistaken for the CLI."""
    exe = fake_opencode(tmp_path, monkeypatch, V2)
    monkeypatch.setenv("DIPLOMAT_SHELL", quiet_shell(tmp_path, "echo /bin/ls\necho hi\n"))
    assert usagescan.opencode_binary() == str(exe)


# MARK: - What the rest of the applet is told


def test_a_prompt_build_is_told_which_opencode_is_installed(tmp_path, monkeypatch,
                                                            opencode):
    from diplomat_runtime import promptcore

    core_bin = tmp_path / "core"
    core_bin.write_text('#!/bin/sh\necho "major=${DIPLOMAT_OPENCODE_MAJOR-unset}"\n',
                        encoding="utf-8")
    core_bin.chmod(0o755)
    monkeypatch.setenv("DIPLOMAT_CORE_BIN", str(core_bin))
    exe = fake_opencode(tmp_path, monkeypatch, V2)
    assert promptcore.build_prompt({"kind": "review"}).strip() == "major=2"
    exe.write_text("#!/bin/sh\n" + V1, encoding="utf-8")
    assert promptcore.build_prompt({"kind": "review"}).strip() == "major=1"
    appconfig.set_value(appconfig.AGENT_RUNNER, runner.CLAUDE)
    assert promptcore.build_prompt({"kind": "review"}).strip() == "major=unset"


def test_the_prompt_dump_shows_the_2x_command(capsys, opencode, as_v2):
    from diplomat_app import selftest

    selftest._print_prompt_dump("h", "a prompt")
    command = capsys.readouterr().out.split("----- SHELL COMMAND -----", 1)[1]
    assert "opencode api session.create" in command
    assert "opencode --session ses_" in command
