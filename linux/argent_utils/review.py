"""Review-PRs config + prompt builder, and the Linux terminal spawner.

The prompt text (depth fragments, scope templates, action blocks) all comes from
the shared ``core/review.json``; only the *assembly* order/conditions live here
as a thin glue layer, identical to ReviewWizard.swift's ``buildPrompt``.

The terminal spawner is the Linux analogue of the macOS AppleScript/iTerm path:
it opens a new terminal-emulator window running ``claude "<prompt>"`` detached
from the applet.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

from . import core


# MARK: - Review depth


def depths() -> list[dict]:
    return core.review()["depths"]


def depth_ids() -> list[str]:
    return [d["id"] for d in depths()]


def depth_by_id(depth_id: str) -> dict:
    for d in depths():
        if d["id"] == depth_id:
            return d
    # Fall back to the configured default, then the first level.
    default = core.review().get("defaultDepth")
    for d in depths():
        if d["id"] == default:
            return d
    return depths()[0]


def default_depth_id() -> str:
    return core.review().get("defaultDepth", depth_ids()[0])


# MARK: - Review config + prompt builder


@dataclass
class ReviewConfig:
    depth: str = ""  # depth id; "" -> default
    target_is_mine: bool = True
    username: str = ""
    me: str = ""  # authenticated viewer login, used as the @handle for "mine"

    mark_ready: bool = True
    leave_reviews: bool = True
    reply_to_reviews: bool = True

    include_drafts: bool = True
    include_ready: bool = True
    specific_pr: str = ""

    def __post_init__(self) -> None:
        if not self.depth:
            self.depth = default_depth_id()

    # The @handle whose PRs we go through.
    @property
    def author_handle(self) -> str:
        if self.target_is_mine:
            return self.me or "me"
        return self.username.strip()

    @property
    def can_mark_ready(self) -> bool:
        return self.target_is_mine

    @property
    def can_leave_reviews(self) -> bool:
        return not self.target_is_mine

    @property
    def can_reply_to_reviews(self) -> bool:
        return self.target_is_mine

    @property
    def eff_mark_ready(self) -> bool:
        return self.mark_ready and self.can_mark_ready

    @property
    def eff_leave_reviews(self) -> bool:
        return self.leave_reviews and self.can_leave_reviews

    @property
    def eff_reply_to_reviews(self) -> bool:
        return self.reply_to_reviews and self.can_reply_to_reviews

    # With neither PR-state box ticked, we review one PR by number instead.
    @property
    def is_single_pr(self) -> bool:
        return not self.include_drafts and not self.include_ready

    @property
    def trimmed_pr(self) -> str:
        return self.specific_pr.strip()

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.trimmed_pr.isdigit()
        return bool(self.author_handle)

    @property
    def _pr_kind(self) -> str:
        s = core.review()["scope"]
        if self.include_drafts and self.include_ready:
            return s["prKindBoth"]
        if self.include_drafts and not self.include_ready:
            return s["prKindDrafts"]
        return s["prKindReady"]

    def build_prompt(self) -> str:
        cfg = core.config()
        owner, repo = cfg["owner"], cfg["repo"]
        s = core.review()["scope"]
        blocks_src = core.review()["blocks"]
        blocks: list[str] = []

        if self.is_single_pr:
            blocks.append(
                s["single"].format(pr=self.trimmed_pr, owner=owner, repo=repo)
            )
        else:
            tmpl = s["scopeMine"] if self.target_is_mine else s["scopeOther"]
            scope = tmpl.format(prKind=self._pr_kind, handle=self.author_handle)
            blocks.append(s["multi"].format(scope=scope, owner=owner, repo=repo))

        blocks.append(depth_by_id(self.depth)["fragment"])
        blocks.append(blocks_src["bar"])

        if self.eff_mark_ready:
            blocks.append(blocks_src["markReady"])
        if self.eff_leave_reviews:
            blocks.append(blocks_src["leaveReviews"])
        if self.eff_reply_to_reviews:
            blocks.append(blocks_src["reply"])

        blocks.append(blocks_src["trailer"])
        return "\n\n".join(blocks)


# MARK: - Terminal choice + spawning


@dataclass(frozen=True)
class SpawnTerminal:
    key: str
    title: str
    exec_name: str
    # argv inserted between the executable and `bash -c <cmd>`.
    prefix: tuple[str, ...]

    @property
    def is_installed(self) -> bool:
        return shutil.which(self.exec_name) is not None


# Ordered by preference. x-terminal-emulator (the Debian alternatives symlink) and
# the XFCE native terminal come first; xterm is the always-there fallback.
TERMINALS: list[SpawnTerminal] = [
    SpawnTerminal("x-terminal-emulator", "System default", "x-terminal-emulator", ("-e",)),
    SpawnTerminal("xfce4-terminal", "XFCE Terminal", "xfce4-terminal", ("-x",)),
    SpawnTerminal("gnome-terminal", "GNOME Terminal", "gnome-terminal", ("--",)),
    SpawnTerminal("konsole", "Konsole", "konsole", ("-e",)),
    SpawnTerminal("kitty", "kitty", "kitty", ()),
    SpawnTerminal("alacritty", "Alacritty", "alacritty", ("-e",)),
    SpawnTerminal("xterm", "xterm", "xterm", ("-e",)),
]


def terminal_by_key(key: str) -> SpawnTerminal | None:
    return next((t for t in TERMINALS if t.key == key), None)


def installed_terminals() -> list[SpawnTerminal]:
    return [t for t in TERMINALS if t.is_installed]


def default_terminal() -> SpawnTerminal:
    found = installed_terminals()
    return found[0] if found else TERMINALS[-1]  # xterm fallback


def resolved(preferred: SpawnTerminal | None) -> SpawnTerminal:
    """The terminal to actually drive: the preferred one if installed, else the
    first installed alternative, else xterm."""
    if preferred and preferred.is_installed:
        return preferred
    return default_terminal()


def repo_path() -> str:
    """The local checkout the agent works in (override with ARGENT_UTILS_REPO)."""
    return os.environ.get("ARGENT_UTILS_REPO") or os.path.expanduser("~/dev/argent")


class SpawnError(RuntimeError):
    pass


def write_prompt(prompt: str) -> str:
    path = os.path.join(
        tempfile.gettempdir(), f"argent-utils-review-{uuid.uuid4().hex}.txt"
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError as exc:
        raise SpawnError(f"Couldn't stage prompt: {exc}") from exc
    return path


def shell_command(prompt_file: str) -> str:
    """``cd '<repo>' 2>/dev/null; claude "$(cat '<file>')"; exec bash``

    The trailing ``exec bash`` keeps the window open after the session ends.
    """
    repo = shlex.quote(repo_path())
    pf = shlex.quote(prompt_file)
    return f'cd {repo} 2>/dev/null; claude "$(cat {pf})"; exec bash'


def spawn(prompt: str, preferred: SpawnTerminal | None) -> str:
    """Stage the prompt, open a new terminal window, run claude. Returns the
    prompt file path. Fully detached from the applet."""
    term = resolved(preferred)
    file = write_prompt(prompt)
    cmd = shell_command(file)
    argv = [term.exec_name, *term.prefix, "bash", "-c", cmd]
    try:
        subprocess.Popen(  # noqa: S603 — args are a literal list, not a shell string
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SpawnError(f"failed to launch {term.title}: {exc}") from exc
    return file
