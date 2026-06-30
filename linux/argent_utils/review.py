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

    @property
    def eff_mark_ready(self) -> bool:
        return self.mark_ready and self.can_mark_ready

    @property
    def eff_leave_reviews(self) -> bool:
        return self.leave_reviews and self.can_leave_reviews

    @property
    def eff_reply_to_reviews(self) -> bool:
        return self.reply_to_reviews and self.can_reply_to_reviews

    # Review exactly one PR by number/URL instead of a whose-PRs sweep.
    @property
    def is_single_pr(self) -> bool:
        return self.target == PRTarget.SPECIFIC

    # Reviewing someone else's PRs: a hard look-don't-touch mode. We never commit
    # or push to their branch, so the no-commit guard goes in and the
    # commit-authoring guidance stays out. (A specific PR may be mine, so it is
    # deliberately NOT review-only.)
    @property
    def is_review_only(self) -> bool:
        return self.target == PRTarget.SOMEONE

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

        # A specific PR may be mine OR someone else's — which decides whether the
        # agent is allowed to touch the branch. We can't know the author up front,
        # so we hand the agent an author-gated prompt instead of guessing.
        if self.is_single_pr:
            return self._build_specific_prompt(owner, repo)

        blocks: list[str] = []
        tmpl = s["scopeMine"] if self.target == PRTarget.MINE else s["scopeOther"]
        scope = tmpl.format(prKind=self._pr_kind, handle=self.author_handle)
        blocks.append(s["multi"].format(scope=scope, owner=owner, repo=repo))

        # For someone else's PRs, frame the whole task as look-don't-touch up front.
        if self.is_review_only:
            blocks.append(blocks_src["reviewOnly"])

        depth = depth_by_id(self.depth)
        blocks.append(depth["fragment"])
        # The depth's on-branch fix step only when we may actually commit — never
        # for someone else's branch, which we don't touch.
        on_branch = depth.get("onBranch")
        if on_branch and not self.is_review_only:
            blocks.append(on_branch)
        blocks.append(blocks_src["bar"])

        if self.eff_mark_ready:
            blocks.append(blocks_src["markReady"])
        if self.eff_leave_reviews:
            blocks.append(blocks_src["leaveReviews"])
        if self.eff_reply_to_reviews:
            blocks.append(blocks_src["reply"])

        blocks.append(blocks_src["trailer"])
        # Commit-authoring guidance only when we might actually commit — never
        # for someone else's branch, which we don't touch.
        if not self.is_review_only:
            blocks.append(blocks_src["noAttribution"])
        if self.final_pass:
            blocks.append(blocks_src["finalPass"])
        return "\n\n".join(blocks)

    def _build_specific_prompt(self, owner: str, repo: str) -> str:
        """The single-PR (Specific PR) prompt. Because the PR may be mine or someone
        else's — and that decides whether the branch may be touched — this tells the
        agent to poll the author first, then split into two mutually exclusive cases:
        CASE A (mine -> fix on the branch, mark clean ready, reply, no AI attribution)
        and CASE B (theirs -> review only, never touch the branch, leave a formal
        review, and explicitly DO NOT mark it ready). The action sub-blocks are gated
        by the same toggles the wizard exposes. Identical to Review.swift's
        ``buildSpecificPrompt``.
        """
        s = core.review()["scope"]
        blocks_src = core.review()["blocks"]
        specific = core.review()["specific"]
        depth = depth_by_id(self.depth)
        handle = self.me or "me"

        def fill(text: str) -> str:
            return text.format(
                pr=self.pr_ref.number_string, owner=owner, repo=repo, me=handle
            )

        blocks: list[str] = [fill(s["single"]), fill(specific["determineAuthor"])]
        # The review work itself is the same regardless of author; only the
        # disposition differs, so the approach + bar come before the split.
        blocks.append(depth["fragment"])
        blocks.append(blocks_src["bar"])

        # CASE A — it's mine: fix it on the branch.
        blocks.append(fill(specific["mineHeader"]))
        on_branch = depth.get("onBranch")
        if on_branch:
            blocks.append(on_branch)
        if self.mark_ready:
            blocks.append(blocks_src["markReady"])
        if self.reply_to_reviews:
            blocks.append(blocks_src["reply"])
        blocks.append(blocks_src["noAttribution"])

        # CASE B — it's someone else's: review only, hands off the branch.
        blocks.append(fill(specific["otherHeader"]))
        blocks.append(blocks_src["reviewOnly"])
        if self.leave_reviews:
            blocks.append(blocks_src["leaveReviews"])
        blocks.append(fill(specific["otherNoMarkReady"]))

        blocks.append(blocks_src["trailer"])
        if self.final_pass:
            blocks.append(blocks_src["finalPass"])
        return "\n\n".join(b for b in blocks if b)


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
