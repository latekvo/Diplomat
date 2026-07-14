"""Review-PRs config + prompt builder, and the Linux terminal spawner.

The prompt text (depth fragments, scope templates, action blocks) all comes from
the shared ``core/review.json``; the *assembly* order/conditions live in Swift
(ArgentUtilsCore/Review.swift) and are reached by shelling out to the
``argent-core`` CLI, so the two front-ends can't drift. ``ReviewConfig`` mirrors
the Swift struct's inputs and derived toggles, including the specific-PR author
disposition (mine / theirs / unknown), which the wizard resolves via ``gh``.

The terminal spawner is the Linux analogue of the macOS AppleScript/iTerm path:
it opens a new terminal-emulator window running ``claude "<prompt>"`` detached
from the applet.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum

from . import core
from .prref import PRRef, parse_pr_ref
from .prtarget import PRTarget


# MARK: - Specific-PR author disposition


class SpecificAuthor(Enum):
    """Who authored a specific PR under review, when known (mirrors the Swift
    ``SpecificAuthor`` enum in ArgentUtilsCore/Review.swift). Selects the prompt
    (fix-on-branch vs review-only vs author-gated) and which action toggles apply.
    """

    UNKNOWN = "unknown"  # specific PR, author not polled yet / poll failed - offer everything
    MINE = "mine"        # fix on the branch (CASE A)
    THEIRS = "theirs"    # review only (CASE B)


def fetch_specific_author(owner: str, repo: str, number: int) -> str | None:
    """One ``gh pr view ... --json author`` -> the author login, or ``None`` on
    failure. Mirrors ``ReviewWizardView.fetchAuthor`` in ReviewWizard.swift. Runs
    the gh shell-out synchronously; call it OFF the UI thread (the wizard does).
    """
    from . import gh

    try:
        data = gh.run(
            ["pr", "view", str(number), "--repo", f"{owner}/{repo}", "--json", "author"]
        )
    except Exception:  # noqa: BLE001 - best-effort author resolution, None on any failure
        return None
    try:
        author = json.loads(data).get("author") or {}
        login = author.get("login")
        return login or None
    except (ValueError, AttributeError):
        return None


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

    # For a specific PR: whether it's mine, someone else's, or not yet determined.
    # The wizard polls the PR's author and sets this. Ignored unless single-PR.
    specific_author: SpecificAuthor = SpecificAuthor.UNKNOWN

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

    # The review disposition: mine (fix on branch) or theirs (review only). For a
    # whose-PRs sweep it follows the target; for a specific PR it's the polled author
    # (UNKNOWN while pending - offers every toggle, gated prompt). Mirrors Swift's
    # ReviewConfig.disposition.
    @property
    def disposition(self) -> SpecificAuthor:
        if self.target == PRTarget.MINE:
            return SpecificAuthor.MINE
        if self.target == PRTarget.SOMEONE:
            return SpecificAuthor.THEIRS
        return self.specific_author

    # Which action toggles apply. Mine-only toggles (mark-ready, reply-to-threads)
    # hide for theirs; theirs-only toggles (formal review, final verdict) hide for
    # mine. UNKNOWN (author pending) leaves all four visible. Mirrors the Swift
    # disposition-based gates verbatim.
    @property
    def can_mark_ready(self) -> bool:
        return self.disposition != SpecificAuthor.THEIRS

    @property
    def can_leave_reviews(self) -> bool:
        return self.disposition != SpecificAuthor.MINE

    @property
    def can_reply_to_reviews(self) -> bool:
        return self.disposition != SpecificAuthor.THEIRS

    # The final approve/changes-requested verdict is a reviewer's call, so it never
    # applies to my own PRs (Swift: canFinalPass = disposition != .mine).
    @property
    def can_final_pass(self) -> bool:
        return self.disposition != SpecificAuthor.MINE

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
            "specificAuthor": self.disposition.value,
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
    path = os.path.join(
        tempfile.gettempdir(), f"argent-utils-review-{uuid.uuid4().hex}.txt"
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
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
