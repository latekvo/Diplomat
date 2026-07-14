"""Review-PRs config + prompt builder, and the Linux terminal spawner.

The prompt text (depth fragments, scope templates, action blocks) all comes from
the shared ``core/review.json``; only the *assembly* order/conditions live here
as a thin glue layer, mirroring ReviewConfig's ``buildPrompt`` in
ArgentUtilsCore/Review.swift (this port covers the sweep and author-unknown
single-PR paths; the known-mine/known-theirs single-PR prompts back macOS-only
monitors and are not ported).

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
from dataclasses import dataclass

from . import core
from .prref import PRRef, parse_pr_ref
from .prtarget import PRTarget


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
    target: PRTarget = PRTarget.MINE
    username: str = ""
    me: str = ""  # authenticated viewer login, used as the @handle for "mine"

    mark_ready: bool = True
    leave_reviews: bool = True
    reply_to_reviews: bool = True

    include_drafts: bool = True
    include_ready: bool = True
    specific_pr: str = ""
    # The "final pass" escalation: a culminating full-E2E verdict pass. Off by default.
    final_pass: bool = False

    def __post_init__(self) -> None:
        if not self.depth:
            self.depth = default_depth_id()

    # The @handle whose PRs we go through (empty in single-PR mode).
    @property
    def author_handle(self) -> str:
        if self.target == PRTarget.MINE:
            return self.me or "me"
        if self.target == PRTarget.SOMEONE:
            return self.username.strip()
        return ""

    # A specific PR may be mine or someone's, so all three actions are offered.
    @property
    def can_mark_ready(self) -> bool:
        return self.target != PRTarget.SOMEONE

    @property
    def can_leave_reviews(self) -> bool:
        return self.target != PRTarget.MINE

    @property
    def can_reply_to_reviews(self) -> bool:
        return self.target != PRTarget.SOMEONE

    # The final approve/changes-requested verdict is a reviewer's call, so it never
    # applies to my own PRs (Swift: canFinalPass = disposition != .mine). A specific
    # PR's author is unknown here (the Linux port only has the author-gated prompt,
    # Swift's `.unknown` disposition), which leaves the toggle available.
    @property
    def can_final_pass(self) -> bool:
        return self.target != PRTarget.MINE

    # Review exactly one PR by number/URL instead of a whose-PRs sweep.
    @property
    def is_single_pr(self) -> bool:
        return self.target == PRTarget.SPECIFIC

    @property
    def target_repo(self) -> tuple[str, str]:
        """The configured target repo (owner, repo), from the shared core config."""
        cfg = core.config()
        return cfg["owner"], cfg["repo"]

    @property
    def pr_ref(self) -> PRRef:
        """The single-PR field parsed as a number / URL / ``owner/repo#n`` shorthand,
        checked against the target repo."""
        owner, repo = self.target_repo
        return parse_pr_ref(self.specific_pr, owner, repo)

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.pr_ref.is_valid
        # A whose-PRs sweep needs a handle and at least one PR-state box ticked.
        return bool(self.author_handle) and (self.include_drafts or self.include_ready)

    def build_prompt(self) -> str:
        # Single-sourced in Swift (ArgentUtilsCore) — assembled by the argent-core
        # CLI so the Linux applet can't drift from the macOS builder.
        from . import promptcore

        return promptcore.build_prompt({
            "kind": "review",
            "depth": self.depth,
            "target": self.target.name.lower(),
            "username": self.username,
            "me": self.me,
            "markReady": self.mark_ready,
            "leaveReviews": self.leave_reviews,
            "replyToReviews": self.reply_to_reviews,
            "includeDrafts": self.include_drafts,
            "includeReady": self.include_ready,
            "specificPR": self.specific_pr,
            "finalPass": self.final_pass,
        })


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
    # 0600 via mkstemp: /tmp is world-readable and multi-user, and a mesh
    # dispatch stages the prompt here too — don't leave it readable to other
    # local users (nor world-readable by umask).
    try:
        fd, path = tempfile.mkstemp(prefix="argent-utils-review-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError as exc:
        raise SpawnError(f"Couldn't stage prompt: {exc}") from exc
    return path


def user_shell() -> str:
    """The user's interactive login shell — so the spawned command sees the aliases
    and env exported from their rc (e.g. a `claude` alias in ~/.zshrc). Override with
    ARGENT_UTILS_SHELL; falls back to $SHELL, then bash."""
    return os.environ.get("ARGENT_UTILS_SHELL") or os.environ.get("SHELL") or "/bin/bash"


def shell_command(prompt_file: str) -> str:
    """``cd '<repo>' 2>/dev/null; claude "$(cat '<file>')"; exec "$SHELL" -i``

    Run (via :func:`user_shell`, interactively) so the user's rc is sourced and
    `claude` resolves to their alias. The trailing ``exec`` keeps the window open in
    the user's shell after the session ends.
    """
    repo = shlex.quote(repo_path())
    pf = shlex.quote(prompt_file)
    return f'cd {repo} 2>/dev/null; claude "$(cat {pf})"; exec "$SHELL" -i'


def spawn(prompt: str, preferred: SpawnTerminal | None) -> str:
    """Stage the prompt, open a new terminal window, run claude. Returns the
    prompt file path. Fully detached from the applet."""
    term = resolved(preferred)
    file = write_prompt(prompt)
    cmd = shell_command(file)
    # Run under the user's INTERACTIVE shell (-i) so their rc is sourced and the
    # `claude` alias + exported env are present — a plain `bash -c` gets neither.
    argv = [term.exec_name, *term.prefix, user_shell(), "-i", "-c", cmd]
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
