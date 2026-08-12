"""The pid mechanism, run for real against a stub agent.

Everything downstream of it — the dedup, the cap, the rows, the retirement — trusts
that the pid in a run's ``pid`` file is the AGENT'S, not a wrapper shell's and not a
tmux client's. That is a claim about how a real shell behaves across ``exec``, so it
is asserted by running one rather than by inspecting the command string.

The old identity mechanism (matching ``PR #<n> in <owner>/<repo>`` against prompt
text in ``ps`` output) is what this replaces, and the last test here is the case it
could never get right: two runs on one PR.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from diplomat_app import review

pytestmark = pytest.mark.skipif(not Path("/proc").is_dir(),
                                reason="the pid identity check reads /proc")


def _stub_agent(tmp_path: Path, body: str = 'sleep 30') -> Path:
    """A fake `claude` on PATH. Named exactly that, because the whole point is that
    the applet finds the process the user's shell resolved."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    exe = d / "claude"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    return d


def _run_agent(tmp_path: Path, bindir: Path, prompt: str = "do the thing"):
    """Run the real spawn command (minus the terminal emulator and tmux, which only
    add windows) and return the Popen plus the three run-dir paths."""
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    prompt_file, pid_file, done_file = run / "prompt.txt", run / "pid", run / "done"
    prompt_file.write_text(prompt)
    cmd = review.shell_command(str(prompt_file), str(done_file), str(pid_file))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.Popen([review.user_shell(), "-i", "-c", cmd], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    return proc, pid_file, done_file


def _await_file(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace")
    except OSError:
        return ""


def _await_exec(pid: int, timeout: float = 10.0) -> str:
    """The pid's argv once the shell has replaced itself with the agent.

    The pid file is written by the ``printf`` BEFORE the exec, so the sample cannot be
    taken the moment it appears: between the two the argv is still the shell's, and
    reading it there says "wrapper" about a run that is about to be the agent. Waited
    for rather than slept on, so a shell that never execs still fails — just at the
    deadline, with the argv it was stuck on.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        argv = _cmdline(pid)
        if argv and "printf" not in argv:
            return argv
        time.sleep(0.05)
    return _cmdline(pid)


def test_the_recorded_pid_is_the_agents_own_process(tmp_path):
    """The claim the whole identity mechanism rests on: the inner shell writes its own
    ``$$`` and then ``exec``s the agent, so the pid outlives the shell it came from
    and names the agent itself."""
    bindir = _stub_agent(tmp_path)
    proc, pid_file, _done = _run_agent(tmp_path, bindir)
    try:
        assert _await_file(pid_file), "the agent's shell never wrote a pid"
        pid = int(pid_file.read_text().strip())
        cmdline = _await_exec(pid)
        assert cmdline, f"pid {pid} is not a live process"
        assert "claude" in cmdline, \
            f"pid {pid} is not the agent — its argv is {cmdline!r}"
        # Not the wrapper: the wrapper's argv carries the whole command string, which
        # still mentions the prompt file. The agent's does not.
        assert "printf" not in cmdline, \
            f"the recorded pid is a wrapper shell, not the agent: {cmdline!r}"
    finally:
        _kill(proc)


def test_the_sentinel_carries_the_agents_exit_code(tmp_path):
    """The `exec` must not cost the exit code: `$?` after the inner shell is still the
    agent's, because exec made them one process."""
    bindir = _stub_agent(tmp_path, body="exit 3")
    proc, _pid, done_file = _run_agent(tmp_path, bindir)
    try:
        assert _await_file(done_file), "no completion sentinel was written"
        assert done_file.read_text().strip() == "3"
    finally:
        _kill(proc)


def test_the_agent_reads_the_prompt_from_its_run_directory(tmp_path):
    """The prompt still reaches the agent as one argv, which is what the transcript is
    matched on later (`usagescan.task_run`).

    It is the LAST argument, not the first: the user's `claude` alias puts its own
    flags ahead of it, so pinning ``$1`` pins the absence of their alias."""
    bindir = _stub_agent(tmp_path, body=(
        'for a in "$@"; do last=$a; done\n'
        'printf %s "$last" > "$ARGV_SINK"; sleep 30'))
    sink = tmp_path / "argv.txt"
    os.environ["ARGV_SINK"] = str(sink)
    try:
        proc, _pid, _done = _run_agent(tmp_path, bindir, prompt="review PR #337 please")
        try:
            assert _await_file(sink), "the agent never received its prompt"
            assert sink.read_text() == "review PR #337 please"
        finally:
            _kill(proc)
    finally:
        os.environ.pop("ARGV_SINK", None)


def _agent_argv_seen(run: Path, bindir: Path, env: dict, *, pid: bool) -> str:
    """Spawn the stub agent through the real command and return the argv IT saw —
    after the user's shell has had its say about what `claude` means."""
    run.mkdir(parents=True)
    prompt_file, sink = run / "prompt.txt", run / "argv.txt"
    prompt_file.write_text("do the thing")
    cmd = review.shell_command(str(prompt_file), str(run / "done"),
                               str(run / "pid") if pid else None)
    proc = subprocess.Popen(
        [review.user_shell(), "-i", "-c", cmd],
        env={**os.environ, **env, "ARGV_SINK": str(sink),
             "PATH": f"{bindir}:{os.environ['PATH']}"},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert _await_file(sink), "the agent never ran"
        return sink.read_text()
    finally:
        _kill(proc)


def test_the_users_claude_alias_reaches_the_agent(tmp_path):
    """The pid wrapper must not cost the agent its flags.

    ``--dangerously-skip-permissions`` is not ours — it lives in the user's rc, as
    ``alias claude='claude --dangerously-skip-permissions …'``. So an inner shell that
    is one word away from expanding that alias silently downgrades every agent into
    one that stops for approval on each tool call, and the applet reads a queue of
    stalled agents as idle. Nothing about the command string looks wrong; only the
    argv the agent actually received shows it.

    The rc is our own, under a redirected ``HOME``: reading the developer's real one
    would let this pass on the strength of a machine setting rather than the code."""
    home = tmp_path / "home"
    home.mkdir()
    for name in (".zshrc", ".bashrc", "shrc"):
        (home / name).write_text("alias claude='claude --alias-was-expanded'\n")
    env = {"HOME": str(home), "ZDOTDIR": str(home), "ENV": str(home / "shrc")}
    bindir = _stub_agent(tmp_path, body='printf %s "$*" > "$ARGV_SINK"; sleep 30')

    # Anti-vacuity: if aliases do not reach the agent even without the pid wrapper,
    # this shell cannot demonstrate the property and a pass would mean nothing.
    plain = _agent_argv_seen(tmp_path / "plain", bindir, env, pid=False)
    if "--alias-was-expanded" not in plain:
        pytest.skip(f"{review.user_shell()} expands no alias from a test rc")

    wrapped = _agent_argv_seen(tmp_path / "wrapped", bindir, env, pid=True)
    assert "--alias-was-expanded" in wrapped, (
        "the pid wrapper swallowed the user's `claude` alias; the agent was run as "
        f"`claude {wrapped}`")


def test_the_recorded_pid_dies_with_the_agent(tmp_path):
    """What retirement rests on: the pid stops resolving once the agent is done.

    The inner shell exists solely to make this true. Recording the OUTER shell's
    ``$$`` would satisfy every other assertion here — its argv names the agent too —
    but it ends in ``exec "$SHELL" -i`` to keep the window open, so it stays alive
    forever and the run is read as still running for as long as the window is open."""
    bindir = _stub_agent(tmp_path, body="exit 0")
    proc, pid_file, done_file = _run_agent(tmp_path, bindir)
    try:
        assert _await_file(pid_file), "the agent's shell never wrote a pid"
        pid = int(pid_file.read_text().strip())
        assert _await_file(done_file), "the agent never finished"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _cmdline(pid):
            time.sleep(0.05)
        assert not _cmdline(pid), \
            f"pid {pid} outlived the agent — it is {_cmdline(pid)!r}"
    finally:
        _kill(proc)


def test_two_runs_on_one_pr_get_distinct_identities(tmp_path):
    """The case the prompt-text scan could not represent at all: it yielded a set of PR
    numbers, so a second run on #337 was indistinguishable from the first. Two pid
    files, two pids."""
    bindir = _stub_agent(tmp_path)
    procs = []
    pids = []
    try:
        for i in (1, 2):
            run = tmp_path / f"run{i}"
            run.mkdir()
            prompt_file, pid_file, done_file = (run / "prompt.txt", run / "pid",
                                                run / "done")
            prompt_file.write_text("review PR #337 in software-mansion/argent")
            cmd = review.shell_command(str(prompt_file), str(done_file), str(pid_file))
            env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
            procs.append(subprocess.Popen(
                [review.user_shell(), "-i", "-c", cmd], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
            assert _await_file(pid_file), f"run {i} never wrote a pid"
            pids.append(int(pid_file.read_text().strip()))
        assert pids[0] != pids[1], "two runs on one PR must not share an identity"
        assert all("claude" in _cmdline(p) for p in pids)
    finally:
        for p in procs:
            _kill(p)


def _kill(proc: subprocess.Popen) -> None:
    """Tear down the whole session: the wrapper, the inner shell and the stub agent
    are one process group, and leaving a 30s sleep behind would leak into the next
    test's process-table assertions."""
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
