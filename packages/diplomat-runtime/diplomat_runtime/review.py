"""Review-PRs config + prompt builder, and the Linux terminal spawner.

The prompt text (depth fragments, scope templates, action blocks) all comes from
the shared ``assets/review.json``; the *assembly* order/conditions live in Swift
(DiplomatCore/Review.swift) and are reached by shelling out to the
``diplomat-core`` CLI, so the two front-ends can't drift. ``ReviewConfig`` mirrors
the Swift struct's inputs and derived toggles, including the specific-PR author
disposition (mine / theirs / unknown), which the wizard resolves via ``gh``.

The terminal spawner is the Linux analogue of the macOS AppleScript/iTerm path:
it opens a new terminal-emulator window running the configured agent CLI on the
staged prompt (:mod:`runner`), detached from the applet.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from enum import Enum

from . import appconfig, core, runner
from .configbase import PRSweepConfig
from .prtarget import PRTarget


# MARK: - Specific-PR author disposition


class SpecificAuthor(Enum):
    """Who authored a specific PR under review, when known (mirrors the Swift
    ``SpecificAuthor`` enum in DiplomatCore/Review.swift). Selects the prompt
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
class ReviewConfig(PRSweepConfig):
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
    # Soft-approve: when a review-only PR comes back perfectly clean, leave a friendly
    # "ran the sweep, all clean, thanks for contributing" comment - but NEVER an APPROVE
    # action. On by default. Outranked by final_pass (a real verdict) when both are set.
    soft_approve: bool = True

    # For a specific PR: whether it's mine, someone else's, or not yet determined.
    # The wizard polls the PR's author and sets this. Ignored unless single-PR.
    specific_author: SpecificAuthor = SpecificAuthor.UNKNOWN

    def __post_init__(self) -> None:
        if not self.depth:
            self.depth = default_depth_id()

    # author_handle / is_single_pr / target_repo / pr_ref are inherited verbatim
    # from PRSweepConfig (shared with ConflictConfig).

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

    # Soft-approve is a reviewer's courtesy on someone else's PR - never my own work
    # (Swift: canSoftApprove = disposition != .mine).
    @property
    def can_soft_approve(self) -> bool:
        return self.disposition != SpecificAuthor.MINE

    @property
    def is_valid(self) -> bool:
        if self.is_single_pr:
            return self.pr_ref.is_valid
        # A whose-PRs sweep needs a handle and at least one PR-state box ticked.
        return bool(self.author_handle) and (self.include_drafts or self.include_ready)

    @property
    def sweep_author(self) -> str:
        """The login whose open PRs this sweep expands into one queued review each,
        or "" when there is nothing to expand (a single PR, or my own PRs before the
        viewer login has resolved).

        Not :attr:`author_handle`, which falls back to the literal "me" for the prompt
        to address: matched against real PR authors that would sweep whatever the
        account called "me" has open."""
        if self.target == PRTarget.MINE:
            return self.me.strip()
        if self.target == PRTarget.SOMEONE:
            return self.username.strip()
        return ""

    def for_pr(self, number: int) -> "ReviewConfig":
        """This sweep, narrowed to one of the PRs it covers — the config behind one
        queued review.

        Same depth and same action toggles, because they are what the operator chose;
        only the scope changes. The disposition comes from the sweep's own target
        rather than a fresh ``gh`` poll: a sweep already knows whose PRs it asked for,
        and polling per PR would be one shell-out apiece to re-learn it."""
        return replace(
            self,
            target=PRTarget.SPECIFIC,
            specific_pr=str(number),
            specific_author=self.disposition,
        )

    def prompt_payload(self) -> dict:
        """The inputs the prompt is assembled from — everything the builder reads, and
        nothing derived. Split out of :meth:`build_prompt` because a queued review is
        stored as this payload: it is already the serialised form of the config, kept
        in step with the Swift builder by the golden-prompt tests, so persisting it
        needs no second spelling of these fields (``Store.requested_reviews``)."""
        return {
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
            "softApprove": self.soft_approve,
            "specificAuthor": self.disposition.value,
        }

    def build_prompt(self) -> str:
        # Single-sourced in Swift (DiplomatCore) — assembled by the diplomat-core
        # CLI so the Linux applet can't drift from the macOS builder.
        from . import promptcore

        return promptcore.build_prompt(self.prompt_payload())


# MARK: - A review the operator has asked for


@dataclass(frozen=True)
class RequestedReview:
    """One PR a Review-PRs sweep asked to have reviewed, waiting for a free slot.

    A sweep is expanded into one of these per PR it covers instead of one agent told
    to work through all fifty, so each gets a bay of the task cap to itself and the
    panel can show, hold and reorder them one by one.

    This is the only work in the queue the applet has to REMEMBER. Everything else
    there is a monitor's find, re-derived from GitHub on every poll — but a PR records
    nothing about somebody having wanted it reviewed, so if this list is lost the ask
    is lost with it. Hence the whole payload rather than a PR number: it is what the
    prompt is assembled from, at the press and again after a restart, so the agent
    that eventually runs is the one the wizard would have opened at the click.
    """

    number: int
    url: str
    #: Whose PR — the pipeline's ban dimension. Empty for my own.
    author: str
    #: :meth:`ReviewConfig.prompt_payload` for this one PR.
    config: dict

    @property
    def label(self) -> str:
        """The row this task wears in the panel and the activity feed. Carries the
        depth because that is the choice a sweep is worth re-reading later: the same
        PR queued from a `max` sweep and from a `quick` one are different jobs."""
        return f"Review · #{self.number} · {self.config.get('depth', '')}"

    def to_json(self) -> dict:
        return {"pr": self.number, "url": self.url, "author": self.author,
                "config": self.config}

    @staticmethod
    def from_json(obj: dict) -> "RequestedReview | None":
        """One stored row, or ``None`` when it is not one. A hand-edited or
        part-written entry drops out of the list rather than taking the applet's whole
        queue down with it — the same degradation every other state file here gets."""
        try:
            number = int(obj["pr"])
            config = obj["config"]
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(config, dict):
            return None
        return RequestedReview(number=number, url=str(obj.get("url", "")),
                               author=str(obj.get("author", "")), config=config)


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


def default_repo_path() -> str:
    """``~/dev/<repo>`` for whichever repo the shared core config targets, so the
    fallback follows a retargeted ``assets/config.json`` instead of naming one repo."""
    return os.path.expanduser(f"~/dev/{core.config()['repo']}")


def stored_repo_path() -> str:
    """The repo root picked in Settings, or "" when unset.

    Read from the shared :mod:`appconfig` file rather than this front-end's QSettings:
    a mesh node spawns agents from its own stdlib-only process, which has no Store and
    no Qt to ask. Re-read per call, so changing the setting reaches a running node."""
    return appconfig.get(appconfig.REPO_ROOT).strip()


def repo_path() -> str:
    """The local checkout the agent works in — the ``cd`` in every spawned session.

    Strongest first: the ``DIPLOMAT_REPO`` env override (the escape hatch every other
    ``DIPLOMAT_*`` knob follows), the repo root picked in Settings, then
    :func:`default_repo_path`.
    """
    chosen = os.environ.get("DIPLOMAT_REPO") or stored_repo_path()
    return os.path.expanduser(chosen) if chosen else default_repo_path()


class SpawnError(RuntimeError):
    pass


def popen_detached(target: list[str] | str, *, shell: bool = False,
                   env: dict | None = None) -> None:
    """Launch an agent process and forget it, raising ``OSError`` if it won't start.

    Every launcher in the applet wants the same two properties, and both are
    load-bearing:

    * **its own session** (``start_new_session``) — the agent outlives the applet
      that spawned it. Without it the child shares the process group and dies with
      the tray (or with the mesh node), killing a running review mid-flight;
    * **no inherited stdio** — the applet's stdin/stdout may be a closed pipe, a
      tty it no longer owns, or the journal. A child writing into it blocks on a
      full pipe or scribbles over the parent's own output.

    ``target`` is an argv list, or a shell string with ``shell=True``. ``env``
    replaces the inherited environment (the confined foreign runner passes a
    credential-scrubbed one). The ``OSError`` is left for the caller to translate
    into its own error type, which is the only part that differs between them.
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


def write_prompt(prompt: str) -> str:
    # 0600 via mkstemp: /tmp is world-readable and multi-user, and a mesh
    # dispatch stages the prompt here too — don't leave it readable to other
    # local users (nor world-readable by umask).
    try:
        fd, path = tempfile.mkstemp(prefix="diplomat-review-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except OSError as exc:
        raise SpawnError(f"Couldn't stage prompt: {exc}") from exc
    return path


def user_shell() -> str:
    """The user's interactive login shell — so the spawned command sees the aliases
    and env exported from their rc (e.g. a `claude` alias in ~/.zshrc). Override with
    DIPLOMAT_SHELL; falls back to $SHELL, then bash."""
    return os.environ.get("DIPLOMAT_SHELL") or os.environ.get("SHELL") or "/bin/bash"


def shell_command(prompt_file: str, done_path: str | None = None,
                  pid_path: str | None = None, port: int | None = None,
                  settings_file: str | None = None) -> str:
    """``cd '<repo>' 2>/dev/null; <agent>; [printf %s $? > done;] exec "$SHELL" -i``

    ``<agent>`` is :func:`runner.agent_command` — ``claude "$(cat '<file>')"`` or the
    OpenCode spelling of the same thing. Everything around it is identical for both,
    because everything around it is what a run is *identified* by.

    Run (via :func:`user_shell`, interactively) so the user's rc is sourced: that is
    what resolves a `claude` alias, and equally what puts a per-user install of either
    CLI on ``PATH``. The trailing ``exec`` keeps the window open in the user's shell
    after the session ends.

    When ``done_path`` is given, the agent's exit code is written there the moment the
    agent returns — an existence-based completion sentinel. It only ever fires on
    EXIT, and an agent is spawned interactively: finishing its work is not exiting, so
    the sentinel says nothing at all for the hours a finished session sits at its
    prompt. That is what ``pid_path`` is for.

    When ``pid_path`` is given the agent runs one shell deeper —
    ``"$SHELL" -i -c 'printf %s $$ > <pid>; <agent>'`` — and what lands in the file is
    that inner shell's ``$$``. The applet identifies a run by it instead of by matching
    ``PR #<n> in <owner>/<repo>`` against prompt text in ``ps`` output, which could not
    tell two runs on one PR apart, matched any unrelated session that mentioned the
    number, and matched the wrapper shell and tmux client as readily as the agent.

    It is the AGENT'S OWN pid wherever the shell elides the fork for the last command
    of a ``-c`` string: the shell execs the agent over itself, replacing the process
    image without changing the pid. Every shell in current use does — measured on macOS
    15.5, ``$SHELL -i -c 'printf %s $$ > p; sleep 4'`` records ``sleep`` under zsh 5.9
    and under bash 5.3. The exception is bash ``3.2``, the last GPLv2 release and what
    macOS still ships as ``/bin/bash``, which forks; ``$SHELL`` is the operator's own
    and unconstrained here, so a login shell set to that one records the wrapper.
    Nothing downstream minds: it shares the agent's controlling terminal and start
    instant, its argv carries the runner word (:func:`runner.is_agent_line`), and it
    exits when the agent does — every input :func:`agentstate._resolve_local` reads.
    What it is not, there, is a handle on the agent *process* — its argv, a signal sent
    to it — so nothing should be built on that.

    The agent must stay the LAST command inside those quotes, and that exec must stay
    the shell's own rather than the written-out `exec` keyword. Spelling it out would
    settle bash 3.2 too, and costs more than it settles: alias expansion applies to the
    first word of a simple command, so under an explicit ``exec claude`` the word
    checked is `exec`, the user's `claude` alias never expands, and the agent loses the
    ``--dangerously-skip-permissions`` that alias carries. Measured the same way, and
    under both shells.

    The inner shell is interactive for the same reason the outer one is: an alias has
    to resolve, and aliases do not survive into a non-interactive child. ``$?`` after
    it is the agent's own exit code either way — one process where the exec happened,
    and the wrapper's own status where it did not, which is the agent's.

    ``port`` is where an OpenCode run's own server answers, so :mod:`opencodeapi` can
    ask the agent whether it is working rather than inferring it from the pane. The
    Claude runner ignores it.

    ``settings_file`` carries the hooks a Claude Code run reports its turn boundaries
    through (:mod:`completion`). That is what finally answers the question the
    sentinel above cannot: the sentinel fires on EXIT, and finishing a turn is not
    exiting, so between them one covers the run that ends and the other the run that
    goes back to its prompt.
    """
    repo = shlex.quote(repo_path())
    agent_cmd = runner.agent_command(prompt_file, port, settings_file)
    done = f"printf %s $? > {shlex.quote(done_path)}; " if done_path else ""
    if pid_path is None:
        return f'cd {repo} 2>/dev/null; {agent_cmd}; {done}exec "$SHELL" -i'
    inner = f'printf %s $$ > {shlex.quote(pid_path)}; {agent_cmd}'
    agent = f"{shlex.quote(user_shell())} -i -c {shlex.quote(inner)}"
    return f"cd {repo} 2>/dev/null; {agent}; {done}exec \"$SHELL\" -i"


def agent_argv(prompt_file: str, done_path: str | None = None,
               pid_path: str | None = None, port: int | None = None,
               settings_file: str | None = None) -> list[str]:
    """What the terminal is asked to run: the agent under the user's INTERACTIVE
    shell (``-i``, so their rc is sourced and a `claude` alias resolves — a plain
    `bash -c` gets neither), inside a tmux session of its own wherever tmux exists.

    The interactive shell is also what lets an rc take the process over before it
    reaches the ``-c`` command. The widespread "start every terminal inside tmux"
    snippet ends in ``exec tmux new-session``, guarded on ``[[ -z "$TMUX" ]]``: under
    a plain ``$SHELL -i -c``, that exec replaces the shell, the agent never runs, and
    the window shows an empty session — a spawn that reports success and launched
    nothing. Opening the session ourselves satisfies that guard, so such an rc runs
    on to the aliases instead of handing off.

    It is also the only way :mod:`tmuxwatch` can reach an agent: ``capture-pane`` and
    ``send-keys`` are what the API-error watcher reads and types through on Linux, and
    they only see panes. Without tmux installed there is no session to open and no
    watcher to feed, so the bare interactive shell stands.
    """
    return terminal_argv(
        shell_command(prompt_file, done_path, pid_path, port, settings_file))


def terminal_argv(command: str) -> list[str]:
    """One shell command, wrapped the way everything Diplomat opens a window on is
    wrapped: the user's interactive shell, inside a tmux session wherever tmux exists.

    Shared by the agent spawn and the runner's own provider-login wizard, because the
    reasons are the same for both — an rc that has to be sourced, and the tmux
    hand-off guard described in :func:`agent_argv`.
    """
    shell = user_shell()
    if shutil.which("tmux") is None:
        return [shell, "-i", "-c", command]
    # One string, because that is the single shell-command argument `new-session`
    # takes; tmux hands it to `sh -c`, which splits it back into the argv above.
    return ["tmux", "new-session", f"{shlex.quote(shell)} -i -c {shlex.quote(command)}"]


def open_terminal(command: str, preferred: SpawnTerminal | None) -> None:
    """Open a new terminal window on one shell command, detached from the applet.

    The window is left sitting in the user's shell afterwards: this runs things a
    human is meant to read the outcome of, and a wizard whose window vanishes the
    instant it finishes has reported nothing.
    """
    term = resolved(preferred)
    argv = [term.exec_name, *term.prefix, *terminal_argv(f'{command}; exec "$SHELL" -i')]
    try:
        popen_detached(argv, env=spawn_env())
    except OSError as exc:
        raise SpawnError(f"failed to launch {term.title}: {exc}") from exc


def spawn_env() -> dict:
    """The environment the *launcher* gets: this process's, minus the variables that
    say we are already inside a tmux pane.

    Whoever launched the applet (or the mesh node) may have done so from one, and
    ``tmux new-session`` inherits ``$TMUX`` from there and refuses to nest — the
    window would open and close again on "sessions should be nested with care".
    Dropping them costs the child nothing: it is about to be in a pane of its own,
    which sets both to that pane's real values.

    Note the launcher, not the agent. Where tmux is in play this environment reaches
    the tmux *client* and stops there: the session's command is started by the
    already-running server, with the server's environment. Anything the agent itself
    must see belongs in the command (:func:`runner.agent_command`), not here.
    """
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def spawn(prompt: str, preferred: SpawnTerminal | None, done_path: str | None = None,
          pid_path: str | None = None, prompt_file: str | None = None,
          port: int | None = None, settings_file: str | None = None) -> str:
    """Stage the prompt, open a new terminal window, run the agent. Returns the
    prompt file path. Fully detached from the applet.

    ``done_path`` receives the agent's exit code on completion, ``pid_path`` receives
    its own pid the moment it starts, ``port`` is where an OpenCode run's server will
    answer, and ``settings_file`` holds the hooks the run reports its own turns
    through — see :func:`shell_command`. ``prompt_file`` skips the staging when
    the caller has already written the prompt somewhere it wants to keep it (the run
    directory in :mod:`.agentregistry`)."""
    term = resolved(preferred)
    file = prompt_file or write_prompt(prompt)
    argv = [term.exec_name, *term.prefix,
            *agent_argv(file, done_path, pid_path, port, settings_file)]
    try:
        popen_detached(argv, env=spawn_env())
    except OSError as exc:
        raise SpawnError(f"failed to launch {term.title}: {exc}") from exc
    return file
