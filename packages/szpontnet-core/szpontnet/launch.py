"""Staging a prompt and firing a child that outlives us.

Both halves are load-bearing for every runner the node starts — the operator's
personal template, the confinement sandbox, the result handler — and both are
easy to get subtly wrong, so they live in one place rather than in each caller.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile


class JobSpawnError(RuntimeError):
    """This machine could not start the job it was handed."""


def write_prompt(prompt: str) -> str:
    """Stage a prompt to a private temp file and return its path.

    0600 via ``mkstemp``: the temp dir is world-readable and multi-user, and a
    dispatched prompt lands here — don't leave it readable to other local users
    (nor world-readable by umask).
    """
    try:
        fd, path = tempfile.mkstemp(prefix="szpont-job-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError as exc:
        raise JobSpawnError(f"couldn't stage prompt: {exc}") from exc
    return path


def popen_detached(target: list[str] | str, *, shell: bool = False,
                   env: dict | None = None) -> None:
    """Launch a process and forget it, raising ``OSError`` if it won't start.

    Two properties, both load-bearing:

    * **its own session** (``start_new_session``) — the child outlives the node
      that spawned it. Without it the child shares the process group and dies
      with the node, killing a running job mid-flight;
    * **no inherited stdio** — the node's stdin/stdout may be a closed pipe, a
      tty it no longer owns, or the journal. A child writing into it blocks on a
      full pipe or scribbles over the parent's own output.

    ``target`` is an argv list, or a shell string with ``shell=True``. ``env``
    replaces the inherited environment (the confined foreign runner passes a
    credential-scrubbed one). The ``OSError`` is left for the caller to translate,
    which is the only part that differs between them.
    """
    subprocess.Popen(  # noqa: S603 - argv list, or the operator's own template
        target,
        shell=shell,
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def fill(template: str, **subs: str) -> str:
    """Substitute ``{name}`` tokens in a command template with shell-quoted values.

    A ``{prompt_file}``-less template gets the prompt path appended, so the
    shortest useful runner is just the command that consumes it.
    """
    cmd = template
    for name, value in subs.items():
        cmd = cmd.replace("{" + name + "}", shlex.quote(value))
    if "{prompt_file}" not in template and "prompt_file" in subs:
        cmd = f"{cmd} {shlex.quote(subs['prompt_file'])}"
    return cmd


def detached(cmd: str, what: str, env: dict | None = None) -> None:
    """Fire-and-forget a shell command in its own session, reporting a failure to
    start as :class:`JobSpawnError` named after ``what``. The node never waits on
    the child: a personal spawn is hand-off-only, a confined one is polled via its
    result file."""
    try:
        popen_detached(cmd, shell=True, env=env)
    except OSError as exc:
        raise JobSpawnError(f"{what} failed: {exc}") from exc
