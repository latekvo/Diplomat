"""The operator's agent token: read by the spawned shell, exported as ``GH_TOKEN``.

``gh`` ranks ``GH_TOKEN`` above its keyring login, which is what lets a narrow
fine-grained token stand in for the broad one ``gh auth login`` stores. Only the
token's name is configured; these pin that the secret itself reaches the agent's
environment and nowhere else a process listing, the terminal's argv or a log could
show it, and that a token which cannot be read starts nothing.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid

import pytest

from diplomat_runtime import appconfig, review, runner, szponthost

DUMMY = "github_pat_DUMMY0123456789abcdefNOTREAL"

#: Every POSIX shell a spawn could run under on this box. The gate is typed into the
#: operator's login shell on macOS, run by ``/bin/sh`` from a Ghostty launcher, and by
#: ``$SHELL -i -c`` on Linux, so it has to mean the same thing in all of them.
_SHELLS = [sh for sh in ("/bin/sh", "/bin/bash", "/bin/zsh", "/bin/dash")
           if os.access(sh, os.X_OK)]


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "agent-token"
    path.write_text(DUMMY + "\n")
    path.chmod(0o600)
    appconfig.set_value(appconfig.AGENT_TOKEN_FILE, str(path))
    return path


_token_export = review.token_export
_check_token = review.check_token
#: The real spawners, taken before conftest's ``no_host_agent_spawn`` swaps them out;
#: every test that calls one stubs ``popen_detached`` itself.
_real_spawn = review.spawn
_real_spawn_macos = szponthost._spawn_macos

#: The git half of the gate, spelled out: the macOS twin pins the same string.
_GIT = ("GIT_CONFIG_COUNT=2 "
        "GIT_CONFIG_KEY_0=credential.https://github.com.helper GIT_CONFIG_VALUE_0= "
        "GIT_CONFIG_KEY_1=credential.https://github.com.helper "
        "GIT_CONFIG_VALUE_1='!gh auth git-credential'")

#: The stand-in agent: the token it sees, and whether the password git would send
#: github.com over HTTPS is that token. The password itself stays in a variable -
#: without the helper reset it is whatever credential helper the box has (on macOS,
#: ``osxkeychain`` and the operator's broad login).
_AGENT = ("printenv GH_TOKEN > out; "
          "pw=$(printf 'protocol=https\\nhost=github.com\\n\\n' "
          "| GIT_TERMINAL_PROMPT=0 git credential fill 2>/dev/null "
          "| sed -n 's/^password=//p'); "
          '[ -n "$pw" ] && [ "$pw" = "$GH_TOKEN" ] && : > git-ok')

#: What ``gh auth git-credential`` answers for github.com once ``GH_TOKEN`` is set.
_GH_STUB = ('#!/bin/sh\n[ "$1 $2 $3" = "auth git-credential get" ] || exit 1\n'
            "printf 'username=x-access-token\\npassword=%s\\n' \"$GH_TOKEN\"\n")


def _on_linux(monkeypatch):
    """Take the file branch whatever box this runs on: the Keychain one would read the
    real login Keychain."""
    monkeypatch.setattr(review, "token_export", lambda: _token_export("linux"))


def _spawn(tmp_path, monkeypatch, shell, agent="printenv GH_TOKEN > out", pid=True):
    """The real spawn string, with a stand-in agent and the terminal's trailing
    interactive shell cut off (it would sit at a prompt forever). ``pid=False`` is the
    form a mesh node spawns, with the agent in the terminal's own shell."""
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(review, "user_shell", lambda: shell)
    monkeypatch.setattr(runner, "agent_command", lambda *a, **k: agent)
    _on_linux(monkeypatch)
    cmd = review.shell_command("/tmp/p.txt", done_path=str(tmp_path / "done"),
                               pid_path=str(tmp_path / "pid") if pid else None)
    head, sep, _ = cmd.rpartition('; exec "$SHELL" -i')
    assert sep, cmd
    return cmd, head


def _clean_env(tmp_path) -> dict:
    """No rc of the operator's: an interactive inner shell sources it. A stand-in
    ``gh`` first on PATH."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "ZDOTDIR")}
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "gh").write_text(_GH_STUB)
    (stubs / "gh").chmod(0o755)
    return {**env, "HOME": str(tmp_path), "ZDOTDIR": str(tmp_path), "ENV": "",
            "PATH": f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}"}


# MARK: - The command


def test_no_configured_token_leaves_the_spawn_exactly_as_it_was(monkeypatch):
    monkeypatch.setattr(review, "repo_path", lambda: "/repo")
    assert review.token_export("linux") == ""
    assert review.token_export("darwin") == ""
    assert review.shell_command("/tmp/p.txt", done_path="/tmp/d") == (
        "cd /repo 2>/dev/null; claude \"$(cat /tmp/p.txt)\"; "
        "{ printf %s $? > /tmp/d; } 2>/dev/null || :; exec \"$SHELL\" -i"
    )


def test_linux_reads_the_configured_file():
    appconfig.set_value(appconfig.AGENT_TOKEN_FILE, "/secrets/agent token")
    assert review.token_export("linux") == (
        "GH_TOKEN=$(cat -- '/secrets/agent token') && [ -n \"$GH_TOKEN\" ] "
        f"&& export GH_TOKEN {_GIT}"
    )


def test_macos_reads_the_configured_keychain_item():
    """A mesh node on a Mac spawns through this side, beside an app that spawns
    through ``AgentSpawner.tokenExport``; ``DIPLOMAT_SPAWN_SCRIPT_TEST`` pins that one."""
    appconfig.set_value(appconfig.AGENT_TOKEN_KEYCHAIN_ITEM, "diplomat-agent-gh")
    assert review.token_export("darwin") == (
        "GH_TOKEN=$(security find-generic-password -s diplomat-agent-gh -w) "
        f"&& [ -n \"$GH_TOKEN\" ] && export GH_TOKEN {_GIT}"
    )


def test_each_platform_reads_only_its_own_source():
    appconfig.set_value(appconfig.AGENT_TOKEN_KEYCHAIN_ITEM, "diplomat-agent-gh")
    assert review.token_export("linux") == ""
    appconfig.set_value(appconfig.AGENT_TOKEN_KEYCHAIN_ITEM, "")
    appconfig.set_value(appconfig.AGENT_TOKEN_FILE, "/secrets/t")
    assert review.token_export("darwin") == ""


def test_a_home_relative_file_is_expanded_by_the_applet(monkeypatch, tmp_path):
    """Quoted for the shell, so the shell will not expand a ``~`` inside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    appconfig.set_value(appconfig.AGENT_TOKEN_FILE, "~/.diplomat/agent-token")
    assert f"cat -- {tmp_path}/.diplomat/agent-token)" in review.token_export("linux")


def test_the_whole_spawn_runs_behind_the_gate(tmp_path, monkeypatch, token_file):
    cmd, _ = _spawn(tmp_path, monkeypatch, "/bin/zsh")
    gate = _token_export("linux")
    assert cmd == (
        f"cd {tmp_path} 2>/dev/null; {gate} && {{ /bin/zsh -i -c "
        f"{shlex.quote('printf %s $$ > ' + str(tmp_path / 'pid') + '; printenv GH_TOKEN > out')}"
        f"; {{ printf %s $? > {tmp_path / 'done'}; }} 2>/dev/null || :; }}; "
        'exec "$SHELL" -i'
    )


def test_the_secret_is_in_no_argv_the_terminal_is_given(tmp_path, monkeypatch,
                                                        token_file):
    """What a launcher, tmux and every wrapper shell carry in argv is what ``ps`` shows
    to every user on the machine. Only the file's path may be there."""
    _on_linux(monkeypatch)
    argv = review.agent_argv("/tmp/p.txt", "/tmp/done", "/tmp/pid", session="s")
    assert all(DUMMY not in arg for arg in argv)
    assert any(str(token_file) in arg for arg in argv)


# MARK: - The command, run


@pytest.mark.parametrize("pid", [True, False], ids=["local", "mesh"])
@pytest.mark.parametrize("shell", _SHELLS)
def test_the_agent_gets_the_token(shell, pid, tmp_path, monkeypatch, token_file):
    _, cmd = _spawn(tmp_path, monkeypatch, shell, agent=_AGENT, pid=pid)
    env = {**_clean_env(tmp_path), "GH_TOKEN": "broad-from-the-launcher"}
    subprocess.run([shell, "-c", cmd], cwd=tmp_path, env=env, capture_output=True,
                   timeout=30)
    assert (tmp_path / "out").read_text().strip() == DUMMY
    assert (tmp_path / "git-ok").exists()
    assert (tmp_path / "pid").exists() == pid
    assert (tmp_path / "done").read_text() == "0"


@pytest.mark.parametrize("pid", [True, False], ids=["local", "mesh"])
@pytest.mark.parametrize("contents", [None, "", "\n"], ids=["missing", "empty", "blank"])
@pytest.mark.parametrize("shell", _SHELLS)
def test_an_unreadable_token_starts_nothing(shell, contents, pid, tmp_path, monkeypatch,
                                            token_file):
    """Not a fallback to the broad login: nothing runs. No pid file, so the tick
    resolves a local run FAILED past its grace, and no exit sentinel, which reads as a
    run that finished - to the resolver, and to a mesh node holding the work's claim."""
    if contents is None:
        token_file.unlink()
    else:
        token_file.write_text(contents)
    _, cmd = _spawn(tmp_path, monkeypatch, shell, pid=pid)
    done = subprocess.run([shell, "-c", cmd], cwd=tmp_path, env=_clean_env(tmp_path),
                          capture_output=True, timeout=30)
    assert done.returncode != 0
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "pid").exists()
    assert not (tmp_path / "done").exists()


def test_the_secret_is_absent_from_the_process_table(tmp_path, monkeypatch, token_file):
    _, cmd = _spawn(tmp_path, monkeypatch, "/bin/sh",
                    agent="printenv GH_TOKEN > out; sleep 30")
    proc = subprocess.Popen(["/bin/sh", "-c", cmd], cwd=tmp_path,
                            env=_clean_env(tmp_path), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        out = tmp_path / "out"
        deadline = time.monotonic() + 15
        while not (out.exists() and out.read_text().strip()):
            assert time.monotonic() < deadline, "the stand-in agent never started"
            time.sleep(0.05)
        table = subprocess.run(["ps", "-A", "-ww", "-o", "args="], capture_output=True,
                               text=True, check=True).stdout
        assert "sleep 30" in table  # the listing does reach the agent
        assert DUMMY not in table
        assert out.read_text().strip() == DUMMY
    finally:
        os.killpg(proc.pid, 9)
        proc.wait()


# MARK: - A token that cannot be read fails the spawn itself


@pytest.mark.parametrize("contents", [None, "", "\n"], ids=["missing", "empty", "blank"])
def test_an_unreadable_token_file_fails_the_spawn_before_a_window(contents, token_file,
                                                                  monkeypatch):
    """Before the terminal, so the caller hears the spawn failed: the local tracker
    logs ``spawn-failed``, and a mesh executor declines instead of holding the
    work's claim on an agent that never ran."""
    if contents is None:
        token_file.unlink()
    else:
        token_file.write_text(contents)
    monkeypatch.setattr(review, "check_token", lambda: _check_token("linux"))
    launched = []
    monkeypatch.setattr(review, "popen_detached", lambda *a, **k: launched.append(a))
    with pytest.raises(review.SpawnError, match=str(token_file)):
        _real_spawn("p", None, done_path="/tmp/d")
    assert launched == []


def test_a_readable_token_file_spawns(token_file, monkeypatch):
    monkeypatch.setattr(review, "check_token", lambda: _check_token("linux"))
    launched = []
    monkeypatch.setattr(review, "popen_detached", lambda *a, **k: launched.append(a))
    _real_spawn("p", None, done_path="/tmp/d")
    assert len(launched) == 1


def test_no_configured_token_is_never_checked():
    _check_token("linux")
    _check_token("darwin")


def test_a_missing_keychain_item_fails_the_mesh_spawn(monkeypatch):
    """The real ``security``, asked for an item nobody has: the mesh's macOS spawner
    raises ``NoRunner`` - the executor answers "failed" and takes no claim - and never
    reaches ``osascript``."""
    if not shutil.which("security"):
        pytest.skip("no macOS security(1)")
    from szpontnet import host as szpont_host

    appconfig.set_value(appconfig.AGENT_TOKEN_KEYCHAIN_ITEM,
                        f"diplomat-selftest-{uuid.uuid4()}")
    monkeypatch.setattr(review, "check_token", lambda: _check_token("darwin"))
    launched = []
    monkeypatch.setattr(review, "popen_detached", lambda *a, **k: launched.append(a))
    with pytest.raises(szpont_host.NoRunner, match="Keychain item"):
        _real_spawn_macos("p", "/tmp/d")
    assert launched == []

