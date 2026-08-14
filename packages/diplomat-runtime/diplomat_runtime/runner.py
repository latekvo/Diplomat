"""Which agent CLI a spawn actually runs.

Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
setting, because the applet's whole job — dispatch, track, price, reap — is the
same whichever it is:

* ``claude`` — Claude Code, the default and what every existing run used;
* ``opencode`` — OpenCode, whose model comes from whichever provider the user
  configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …);
* ``hermes`` — Hermes Agent, likewise;
* ``freebuff`` — Freebuff, Codebuff's free tier, on the account its own login
  wizard connects.

Only the *agent word and its flags* differ. Everything the spawn is built out of
— the interactive shell, the pid file written before the shell execs the agent
over itself, the completion sentinel — is identical, and deliberately so: those
mechanisms are what :mod:`agentregistry` and :mod:`probes` identify a run by, and
a second spawn shape would be a second set of them to keep true.

Three of the four also take their prompt on the command line, as ``$(cat …)``.
Freebuff takes none: its CLI accepts a prompt argument only when the same binary
runs as ``codebuff``, and under the ``freebuff`` name the parser both restricts
its one positional to ``login`` and hard-codes the initial prompt to nothing. So a
Freebuff spawn opens on an empty composer and is handed its prompt afterwards, by
typing — see :func:`prompt_handoff` and :func:`takes_typed_prompt`.

That has one consequence beyond the spawn, and it is a *reduction* in what can be
seen rather than a wrong answer. A Freebuff agent's ``ps`` line is
``freebuff --cwd <repo>`` and nothing else, so the scan that reads a PR number out
of an agent's own prompt text (``probes.live_agents``) can never match one. Both
things that scan is for lose a case: an untracked Freebuff agent — one this applet
has no record of — is not drawn or counted, and a run the MESH placed here, which
has no pid file to be found by instead, never acquires a tty, so its screen is
never read and its prompt is never typed into it. Every run this applet spawned
itself is unaffected: those are identified by the pid their own shell wrote.

What OpenCode and Hermes are *doing* is asked of the runner rather than read off
its screen, and each answers from a different place: OpenCode over a loopback port
of its own (:mod:`opencodeapi`), Hermes out of the SQLite store it keeps every
session in (:mod:`hermesstore`). Both come back as the same typed answer, so
:mod:`agentstate` never learns which runner it is looking at. Freebuff has no such
store to ask — its session lives on its server, and all it keeps on disk is a
diagnostic log — so a Freebuff run is read off its screen, exactly as a Claude Code
run is.

Credentials are the one thing this module refuses to hold. Each runner has its own
provider store and its own login wizard, and that is where a key belongs — not in
``~/.diplomat/config.json``, which is world-readable by default, is copied around
by the mesh, and would then need a secret-handling story per provider. Diplomat
stores the *choice* of runner and model; the runner stores the secret.

Stdlib-only, like :mod:`appconfig` and :mod:`autofix`, because a mesh node spawns
agents from its own Qt-free process and has to reach the same answer.

:mod:`appconfig` is imported per-call, not at module scope: ``appconfig`` imports
:mod:`autofix`, which imports this module for :func:`is_agent_line`. Hoisting that
import closes the cycle.
"""

from __future__ import annotations

import shlex

#: The runners, by the name of the CLI each one runs. The value is also what ``ps``
#: shows, which is what :func:`is_agent_line` matches on.
CLAUDE = "claude"
OPENCODE = "opencode"
HERMES = "hermes"
FREEBUFF = "freebuff"
RUNNERS = (CLAUDE, OPENCODE, HERMES, FREEBUFF)

#: What Settings shows for each.
LABELS = {CLAUDE: "Claude Code", OPENCODE: "OpenCode", HERMES: "Hermes",
          FREEBUFF: "Freebuff"}

#: The runners that take ``-m <model>``. Freebuff is absent because its CLI has no
#: such flag: the free tier picks the model server-side, and Settings hides the field
#: rather than offering one whose value nothing would read.
MODEL_RUNNERS = (OPENCODE, HERMES)

#: OpenCode's permission gate, opened for a spawned agent.
#:
#: The Claude runner gets its autonomy from the user's own `claude` alias (that is
#: what ``--dangerously-skip-permissions`` in it is for, and why the spawn goes
#: through an interactive shell at all). OpenCode has no alias to carry it, so the
#: equivalent is set here — an agent that stops to ask permission in a window
#: nobody is watching is an agent that never finishes, and the applet would hold
#: its task-cap bay until someone noticed.
#:
#: Carried as an assignment *inside the command*, never in the launcher's
#: environment: on neither spawn path is that environment the agent's.
#: ``tmux new-session`` hands the command to the already-running tmux server, so the
#: session gets the SERVER's environment; the macOS spawner has no environment
#: channel at all, typing a line into a fresh window via AppleScript.
OPENCODE_PERMISSION_ENV = "OPENCODE_PERMISSION"
OPENCODE_PERMISSION_VALUE = (
    '{"edit":"allow","bash":"allow","webfetch":"allow",'
    '"external_directory":"allow","doom_loop":"allow"}'
)


def selected() -> str:
    """The configured runner, falling back to Claude Code.

    Read from the shared :mod:`appconfig` file rather than this front-end's
    QSettings for the same reason the repo root is: a mesh node spawns agents from
    a process that has no Store and no Qt to ask. Re-read per call, so changing the
    setting reaches a running node on its next spawn.

    An unrecognised value degrades to Claude Code rather than failing the spawn —
    a hand-edited or newer config must not leave the applet unable to dispatch.
    """
    from . import appconfig

    value = appconfig.get(appconfig.AGENT_RUNNER).strip()
    return value if value in RUNNERS else CLAUDE


def model() -> str:
    """The model the selected runner is pinned to, or "" to let it pick.

    Empty is a real choice, not a missing one: both OpenCode and Hermes already
    remember a default model per install, and overriding it with a guess would
    silently move a user off the model their own picker selected. Claude Code and
    Freebuff take no such flag here and ignore it.
    """
    from . import appconfig

    return appconfig.get(appconfig.AGENT_MODEL).strip()


def takes_typed_prompt(name: str) -> bool:
    """Whether a spawn of this runner opens on an EMPTY session, so its prompt has to
    be typed into it afterwards rather than passed on the command line.

    True for Freebuff alone. Every caller that dispatches has to ask, because a run
    that never receives its prompt is the worst shape of failure the applet has: the
    process is up, ``ps`` sees it, it holds a bay of the task cap, and it will sit
    there doing nothing until a human closes the window.
    """
    return name == FREEBUFF


def prompt_handoff(prompt_file: str) -> str:
    """The single line typed into a Freebuff composer to start its run.

    A pointer at the staged prompt rather than the prompt itself, and that is the
    whole reason this is one line: the terminal channels that can type into a live
    session submit a LINE (tmux ``send-keys`` + Enter on Linux, iTerm ``write text`` /
    Terminal ``do script … in tab`` on macOS), so a multi-line review prompt sent
    through either would submit at its first newline and hand the agent a fragment.
    Both channels already exist for the API-error nudge, and this reuses them
    unchanged.

    The file is the run's own ``prompt.txt`` (:mod:`agentregistry`), which outlives the
    hand-off and is deleted only when the run is retired. It sits outside the repo, so
    the line says how to reach it as well as what to do with it — Codebuff's file tools
    are scoped to the project it opened, while its terminal tool is not.
    """
    return (f"Read the instructions in {prompt_file} (cat it in the terminal — it is "
            f"outside this project) and follow them exactly.")


def agent_command(prompt_file: str, port: int | None = None) -> str:
    """The one command that runs the agent: ``<cli> <prompt-bearing args>``.

    This is the *whole* of what a runner changes about a spawn, and it is a shell
    snippet rather than an argv because the prompt reaches the agent as
    ``"$(cat <file>)"`` — see :func:`review.shell_command` for why a staged file
    beats threading a multi-line prompt through nested quoting.

    It must stay a *simple command* with the agent word first. Under Claude Code
    that word has to be alias-expandable (the alias is what carries
    ``--dangerously-skip-permissions``); for every runner it has to be the shell's
    last command, so the shell execs the agent over itself and the pid already
    written to the run's ``pid`` file is the agent's own. A leading variable
    assignment keeps both properties — the shell still execs the command it prefixes,
    so the recorded pid stays the agent's.

    ``port`` puts an OpenCode run's own server on a port the applet already knows,
    which is what lets :mod:`opencodeapi` ask the agent what it is doing instead of
    reading it off the agent's screen. It is ignored by the other three, which have no
    such server — Hermes answers the same question from its own session store, and
    Freebuff's is on Freebuff's own machines. Omitting it is a supported spawn, not a
    broken one: the run works exactly as before and is tracked by its screen.

    ``prompt_file`` is ignored for Freebuff, which takes no prompt argument at all
    (:func:`prompt_handoff`). It is the one runner whose returned command does not
    carry the prompt, and the one whose spawn is not finished when the command has
    run.
    """
    pf = shlex.quote(prompt_file)
    chosen = selected()
    if chosen == CLAUDE:
        return f'claude "$(cat {pf})"'
    if chosen == FREEBUFF:
        # `--cwd` rather than leaning on the `cd` the spawn already did, because the
        # two fail differently. That `cd` is deliberately quiet (`2>/dev/null`), and
        # where the other runners would then simply work in the wrong directory,
        # Freebuff opens a full-screen directory PICKER when its working directory is
        # not a project — an agent that can never start, holding a bay of the task cap
        # until someone closes the window.
        from . import review

        return f"freebuff --cwd {shlex.quote(review.repo_path())}"
    pinned = model()
    flag = f" -m {shlex.quote(pinned)}" if pinned else ""
    if chosen == HERMES:
        # `--yolo` bypasses the approval prompts, the same autonomy the Claude alias
        # carries and `OPENCODE_PERMISSION` grants below. `-q` submits the prompt into
        # the TUI, so this is a windowed agent the user can watch and type into, and
        # the query is stored verbatim as the session's opening message — which is how
        # `hermesstore.is_ours` tells this run's session from a sibling's in the same
        # checkout.
        return f'hermes chat --tui --yolo{flag} -q "$(cat {pf})"'
    # OpenCode's default hostname is loopback, so this exposes the run to other users
    # of this machine and to nothing else. It cannot also be password-protected: the
    # server takes one, but OpenCode's own TUI sends none, so a run started with
    # `OPENCODE_SERVER_PASSWORD` set exits on `Unauthorized` before doing any work.
    listen = f" --port {int(port)}" if port else ""
    grant = f"{OPENCODE_PERMISSION_ENV}={shlex.quote(OPENCODE_PERMISSION_VALUE)}"
    # `--prompt` starts the TUI with the prompt already submitted, which is what
    # makes this a windowed agent the user can watch and type into — the same
    # affordance the Claude runner has. `opencode run` would be headless and leave
    # no session to attach to. It also lands verbatim as the session's opening
    # message, which is how `opencodeapi.is_ours` tells this run's session from a
    # sibling's in the same checkout.
    return f'{grant} opencode{listen}{flag} --prompt "$(cat {pf})"'


def setup_command() -> str:
    """The command that lets a user connect a provider to the selected runner.

    Diplomat deliberately does not ask for a provider and an API key itself. OpenCode
    and Hermes each ship a wizard that knows their whole provider catalog, which
    entries take an OAuth flow rather than a key, and where each one's credentials
    belong — and each writes them to its own store, the only place its agent reads
    them from anyway. A key field here would be a worse copy of that which also put a
    secret in Diplomat's config file.

    The listing command runs after, so the window the user is left looking at states
    what is now connected rather than making them trust that it worked.

    Freebuff is the exception to the *provider* half: it has no catalog to choose
    from, only the one account its own site signs a user into, so its wizard is the
    whole of it and there is nothing to list afterwards. The window stays on the
    command, which is what prints the URL the user has to open.
    """
    chosen = selected()
    if chosen == FREEBUFF:
        return "freebuff login"
    if chosen == HERMES:
        return "hermes setup; hermes status"
    return "opencode providers login; opencode providers list"


def is_agent_line(line: str) -> bool:
    """Whether a ``ps`` line is an agent of *any* runner.

    Every scan that counts, adopts or reaps an agent asks this, so the answer stays
    in one place: a runner the applet can spawn but a scan cannot see is an agent
    that runs forever without holding a bay, and one the panel redraws as untracked
    on every tick.

    Deliberately as loose as the test it replaces — a wrapper shell and the agent
    both carry the word, which is exactly what the age half of the pid-adoption
    guard exists to disambiguate.
    """
    return any(name in line for name in RUNNERS)
