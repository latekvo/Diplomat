"""Which agent CLI a spawn actually runs.

Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
setting, because the applet's whole job — dispatch, track, price, reap — is the
same either way:

* ``claude`` — Claude Code, the default and what every existing run used;
* ``opencode`` — OpenCode, whose model comes from whichever provider the user
  configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …).

Only the *agent word* differs. Everything the spawn is built out of — the
interactive shell, the ``$(cat …)`` prompt hand-off, the pid file written before
the shell execs the agent over itself, the completion sentinel — is identical,
and deliberately so: those mechanisms are what :mod:`agentregistry` and
:mod:`probes` identify a run by, and a second spawn shape would be a second set
of them to keep true.

Credentials are the one thing this module refuses to hold. OpenCode has its own
provider store and its own login wizard (``opencode providers login``), and that
is where a key belongs — not in ``~/.diplomat/config.json``, which is
world-readable by default, is copied around by the mesh, and would then need a
second secret-handling story per provider. Diplomat stores the *choice* of
runner and model; OpenCode stores the secret.

Stdlib-only, like :mod:`appconfig` and :mod:`autofix`, because a mesh node spawns
agents from its own Qt-free process and has to reach the same answer.

:mod:`appconfig` is imported per-call, not at module scope: ``appconfig`` imports
:mod:`autofix`, which imports this module for :func:`is_agent_line`. Hoisting that
import closes the cycle.
"""

from __future__ import annotations

import shlex

#: The two runners, by the name of the CLI each one runs. The value is also what
#: ``ps`` shows, which is what :func:`is_agent_line` matches on.
CLAUDE = "claude"
OPENCODE = "opencode"
RUNNERS = (CLAUDE, OPENCODE)

#: What Settings shows for each.
LABELS = {CLAUDE: "Claude Code", OPENCODE: "OpenCode"}

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


def opencode_model() -> str:
    """The ``provider/model`` OpenCode is pinned to, or "" to let it pick.

    Empty is a real choice, not a missing one: OpenCode already remembers a default
    model per install, and overriding it with a guess would silently move a user off
    the model they configured in OpenCode's own picker.
    """
    from . import appconfig

    return appconfig.get(appconfig.OPENCODE_MODEL).strip()


def agent_command(prompt_file: str, port: int | None = None) -> str:
    """The one command that runs the agent: ``<cli> <prompt-bearing args>``.

    This is the *whole* of what a runner changes about a spawn, and it is a shell
    snippet rather than an argv because the prompt reaches the agent as
    ``"$(cat <file>)"`` — see :func:`review.shell_command` for why a staged file
    beats threading a multi-line prompt through nested quoting.

    It must stay a *simple command* with the agent word first. Under Claude Code
    that word has to be alias-expandable (the alias is what carries
    ``--dangerously-skip-permissions``); for both runners it has to be the shell's
    last command, so the shell execs the agent over itself and the pid already
    written to the run's ``pid`` file is the agent's own. A leading variable
    assignment keeps both properties — the shell still execs the command it prefixes,
    so the recorded pid stays the agent's.

    ``port`` puts an OpenCode run's own server on a port the applet already knows,
    which is what lets :mod:`opencodeapi` ask the agent what it is doing instead of
    reading it off the agent's screen. It is ignored by the Claude runner, which has
    no such server. Omitting it is a supported spawn, not a broken one: the run works
    exactly as before and is tracked by its screen.
    """
    pf = shlex.quote(prompt_file)
    if selected() != OPENCODE:
        return f'claude "$(cat {pf})"'
    model = opencode_model()
    flag = f" -m {shlex.quote(model)}" if model else ""
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
    """The command that lets a user connect a provider to OpenCode.

    Diplomat deliberately does not ask for a provider and an API key itself.
    OpenCode ships a wizard that knows its whole provider catalog, which entries take
    an OAuth flow rather than a key, and where each one's credentials belong — and it
    writes them to its own store, the only place the agent reads them from anyway. A
    second key field here would be a worse copy of it that also puts a secret in
    Diplomat's config file.

    ``providers list`` runs after, so the window the user is left looking at states
    what is now connected rather than making them trust that it worked.
    """
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
