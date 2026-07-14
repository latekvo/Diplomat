"""Run a dispatched job on *this* machine — the mesh's landing pad.

A job is a staged prompt + a terminal window running ``claude``, exactly like
a local SPAWN AGENT. Resolution order:

1. ``ARGENT_MESH_SPAWN`` — a command template (``{prompt_file}`` substituted,
   or the path appended). How tests and headless boxes (no display) take
   dispatches; also the hook for custom runners.
2. macOS — ``osascript`` opens Terminal.app on the same shell command the
   Linux spawner uses.
3. Linux — the applet's own terminal spawner (``argent_utils.review.spawn``),
   which is stdlib-only and auto-detects an installed terminal emulator.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from .. import review


class JobSpawnError(RuntimeError):
    pass


def _spawn_override(prompt_file: str, template: str) -> None:
    if "{prompt_file}" in template:
        cmd = template.replace("{prompt_file}", shlex.quote(prompt_file))
    else:
        cmd = f"{template} {shlex.quote(prompt_file)}"
    try:
        subprocess.Popen(  # noqa: S602 — the template is the operator's own config
            cmd, shell=True, start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise JobSpawnError(f"ARGENT_MESH_SPAWN failed: {exc}") from exc


def _spawn_macos(prompt_file: str) -> None:
    shell_cmd = review.shell_command(prompt_file)
    script = f'tell application "Terminal" to do script {_applescript_quote(shell_cmd)}'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise JobSpawnError(f"osascript failed: {exc}") from exc


def _applescript_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def spawn_job(prompt: str) -> str:
    """Stage the prompt and launch the agent. Returns the prompt-file path;
    raises :class:`JobSpawnError` when this machine can't take the job (the
    dispatcher then fails over to the next candidate)."""
    template = os.environ.get("ARGENT_MESH_SPAWN")
    if template:
        prompt_file = review.write_prompt(prompt)
        _spawn_override(prompt_file, template)
        return prompt_file

    import platform

    if platform.system() == "Darwin":
        prompt_file = review.write_prompt(prompt)
        _spawn_macos(prompt_file)
        return prompt_file

    # Linux: reuse the applet's spawner (terminal auto-detection included).
    try:
        return review.spawn(prompt, None)
    except review.SpawnError as exc:
        raise JobSpawnError(str(exc)) from exc
