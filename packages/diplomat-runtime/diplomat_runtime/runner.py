"""Which agent CLI a spawn actually runs.

Diplomat opens a terminal window and runs an agent in it. *Which* agent is one
setting, because the applet's whole job — dispatch, track, price, reap — is the
same whichever it is:

* ``claude`` — Claude Code, the default and what every existing run used;
* ``opencode`` — OpenCode, whose model comes from whichever provider the user
  configured in OpenCode itself (Anthropic, OpenRouter, a local Ollama, …);
* ``hermes`` — Hermes Agent, likewise.

What differs is the agent command: for most runners the agent word and its flags,
handed the prompt as ``$(cat …)``; for OpenCode 2.x a short chain of ``opencode api``
calls that creates and prompts the session through its per-user service, ahead of
the TUI that attaches to it (:func:`_opencode_v2_command`). Everything around that
command — the interactive shell, the pid file written before the agent starts, the
completion sentinel — is shared, and deliberately so: those mechanisms are what
:mod:`agentregistry` and :mod:`probes` identify a run by, and a second spawn shape
would be a second set of them to keep true.

What each of the two foreign runners is *doing* is asked of the runner rather than
read off its screen, and each answers from a different place: OpenCode over HTTP on
loopback — a port of the run's own on 1.x, the per-user service on 2.x
(:mod:`opencodeapi`) — and Hermes out of the SQLite store it keeps every session in
(:mod:`hermesstore`). Both come back as the same typed
answer, so :mod:`agentstate` never learns which runner it is looking at.

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

import json
import shlex
import uuid

#: The runners, by the name of the CLI each one runs. The value is also what ``ps``
#: shows, which is what :func:`is_agent_line` matches on.
CLAUDE = "claude"
OPENCODE = "opencode"
HERMES = "hermes"
RUNNERS = (CLAUDE, OPENCODE, HERMES)

#: What Settings shows for each.
LABELS = {CLAUDE: "Claude Code", OPENCODE: "OpenCode", HERMES: "Hermes"}

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

#: The same grant on OpenCode 2.x, where it cannot travel in the command: 2.x keeps
#: config and permissions in its per-user service, not in the TUI, so the variable
#: above set on a 2.x TUI changes nothing (measured on 2.0.18). A session carries its
#: own ruleset instead, given when it is created (:func:`opencode_session_body`).
OPENCODE_ALLOW_ALL = [{"action": "*", "resource": "*", "effect": "allow"}]

#: What every session id Diplomat mints for a 2.x run starts with. OpenCode takes a
#: caller-chosen id so long as it starts ``ses``, and creating one that already exists
#: returns the existing session rather than failing — so each spawn mints a fresh one.
OPENCODE_SESSION_PREFIX = "ses_diplomat_"


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
    silently move a user off the model their own picker selected. Claude Code takes
    no such flag here and ignores it.
    """
    from . import appconfig

    return appconfig.get(appconfig.AGENT_MODEL).strip()


def new_opencode_session() -> str:
    """A session id for one 2.x spawn: the prefix, then 32 hex digits of a uuid4."""
    return OPENCODE_SESSION_PREFIX + uuid.uuid4().hex


def opencode_staged(prompt_file: str) -> tuple[str, str]:
    """Where a 2.x spawn's two request bodies are staged: beside the prompt, named for
    it — the session to create, then the prompt to submit into it."""
    return prompt_file + ".session.json", prompt_file + ".prompt.json"


def opencode_model(pin: str) -> dict:
    """A pinned model id as 2.x's session API spells it.

    Split the way the 2.x TUI splits its own ids: the provider is everything before
    the FIRST slash, so ``openrouter/moonshotai/kimi-k3`` is provider ``openrouter``
    and model ``moonshotai/kimi-k3``, and a ``#`` in the rest names a variant, from
    its LAST one on.
    """
    provider, _slash, rest = pin.partition("/")
    model_id, hash_, variant = rest.rpartition("#")
    if hash_:
        return {"providerID": provider, "id": model_id, "variant": variant}
    return {"providerID": provider, "id": rest}


def opencode_session_body(session_id: str, directory: str, pin: str) -> str:
    """The ``session.create`` body for a 2.x run: its fresh id, the checkout it works
    in, the allow-all ruleset (:data:`OPENCODE_ALLOW_ALL`), and a model only when one
    is pinned. Unpinned, the service picks for itself — its configured ``model``, else
    the first one available."""
    body: dict = {"id": session_id, "location": {"directory": directory},
                  "permissions": OPENCODE_ALLOW_ALL}
    if pin:
        body["model"] = opencode_model(pin)
    return json.dumps(body)


def opencode_prompt_body(prompt: str) -> str:
    """The ``session.prompt`` body for a 2.x run: the staged prompt, verbatim."""
    return json.dumps({"text": prompt})


def agent_command(prompt_file: str, port: int | None = None,
                  settings_file: str | None = None,
                  opencode_session: str | None = None) -> str:
    """The one command that runs the agent: ``<cli> <prompt-bearing args>``.

    This is the *whole* of what a runner changes about a spawn, and it is a shell
    snippet rather than an argv because the prompt reaches the agent as
    ``"$(cat <file>)"`` — see :func:`review.shell_command` for why a staged file
    beats threading a multi-line prompt through nested quoting.

    It must END in a *simple command* with the agent word first — for every runner
    but OpenCode 2.x it is nothing else. Under Claude Code that word has to be
    alias-expandable (the alias is what carries ``--dangerously-skip-permissions``);
    for every runner it has to be the shell's last command, which is the one an
    eliding shell execs over itself so that the pid already written to the run's
    ``pid`` file is the agent's own (:func:`review.shell_command` for what that rests
    on). A leading variable assignment keeps both properties — measured under zsh 5.9
    and bash 5.3, the ``VAR=x agent`` form records the same pid the bare one does.

    ``port`` puts an OpenCode run's own server on a port the applet already knows,
    which is what lets :mod:`opencodeapi` ask the agent what it is doing instead of
    reading it off the agent's screen. It is ignored by the other two, which have no
    such server — Hermes answers the same question from its own session store.
    Omitting it is a supported spawn, not a broken one: the run works exactly as
    before and is tracked by its screen.

    ``opencode_session`` makes it an OpenCode 2.x spawn and replaces ``port``, the pin
    and ``--prompt`` alike: 2.x has no server per TUI to put on a port, takes no
    ``-m``, and its ``--prompt`` fills the composer without submitting it (upstream
    anomalyco/opencode#51135, reproduced on 2.0.18). So the run is started through
    the per-user service instead — see :func:`_opencode_v2_command`. The caller mints
    the id and stages the bodies first (:func:`review.stage_opencode_session`).

    ``settings_file`` is where Claude Code finds the hooks that make it report its own
    turn boundaries (:mod:`completion`) — the one mechanism that answers "is this run
    done" from the CLI rather than from its screen. ``--settings`` MERGES with the
    user's own settings rather than replacing them, so a spawned agent keeps whatever
    hooks its user configured. The flag goes AFTER the agent word, which is what keeps
    that word alias-expandable. The other two runners take no such flag and are read
    from their session stores instead.
    """
    pf = shlex.quote(prompt_file)
    chosen = selected()
    if chosen == CLAUDE:
        hooks = f" --settings {shlex.quote(settings_file)}" if settings_file else ""
        return f'claude{hooks} "$(cat {pf})"'
    if chosen == OPENCODE and opencode_session:
        return _opencode_v2_command(prompt_file, opencode_session)
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
    # OpenCode 1.x from here on. Its default hostname is loopback, so this exposes the
    # run to other users of this machine and to nothing else. It cannot also be
    # password-protected: the server takes one, but OpenCode's own TUI sends none, so
    # a run started with `OPENCODE_SERVER_PASSWORD` set exits on `Unauthorized` before
    # doing any work.
    listen = f" --port {int(port)}" if port else ""
    grant = f"{OPENCODE_PERMISSION_ENV}={shlex.quote(OPENCODE_PERMISSION_VALUE)}"
    # `--prompt` starts the TUI with the prompt already submitted, which is what
    # makes this a windowed agent the user can watch and type into — the same
    # affordance the Claude runner has. `opencode run` would be headless and leave
    # no session to attach to. It also lands verbatim as the session's opening
    # message, which is how `opencodeapi.is_ours` tells this run's session from a
    # sibling's in the same checkout.
    return f'{grant} opencode{listen}{flag} --prompt "$(cat {pf})"'


def _opencode_v2_command(prompt_file: str, session_id: str) -> str:
    """``opencode api session.create … && opencode api session.prompt … || exit;
    opencode --session <id>``.

    The service creates the session and starts its turn; the TUI then attaches to it,
    showing the turn in flight and taking typing like any other. Every simple command
    starts with the word ``opencode``, so a user alias for it still expands. The bodies
    travel as ``-d "$(cat …)"`` because ``opencode api`` takes its JSON inline — no
    ``@file``, no stdin.

    Shaped by which command an eliding shell execs over itself
    (:func:`review.shell_command`), measured under bash 5.3 and zsh 5.9: zsh execs the
    last command of an ``&&`` chain and bash 5.3 does not, but both exec the last
    command after a ``;``. So the TUI stands alone after one, and the pid file names
    OpenCode itself under either. ``|| exit`` ends the shell with the failing ``api``
    call's status instead, so the exit sentinel records it rather than a TUI opening on
    a session that does not exist.
    """
    session_body, prompt_body = (shlex.quote(p) for p in opencode_staged(prompt_file))
    sid = shlex.quote(session_id)
    return (f'opencode api session.create -d "$(cat {session_body})"'
            f' && opencode api session.prompt --param sessionID={sid}'
            f' -d "$(cat {prompt_body})" || exit; opencode --session {sid}')


def setup_command() -> str:
    """The command that lets a user connect a provider to the selected runner.

    Diplomat deliberately does not ask for a provider and an API key itself. Both
    foreign runners ship a wizard that knows their whole provider catalog, which
    entries take an OAuth flow rather than a key, and where each one's credentials
    belong — and each writes them to its own store, the only place its agent reads
    them from anyway. A key field here would be a worse copy of that which also put a
    secret in Diplomat's config file.

    The listing command runs after, so the window the user is left looking at states
    what is now connected rather than making them trust that it worked.

    OpenCode's is ``auth``, which 2.x names it and 1.x accepts as an alias of
    ``providers`` (checked on 1.4.3 and 1.18.33), so one command serves both.
    """
    if selected() == HERMES:
        return "hermes setup; hermes status"
    return "opencode auth login; opencode auth list"


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
