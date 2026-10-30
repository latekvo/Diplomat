# Settings

The header **⚙︎** button (next to ↻, the Comms button, the **⬡** mesh button, the
Telemetry button and ⏻) swaps the panel to a settings screen:

- **GitHub username** - override the handle used by the "My …" tools, the wizards
  and the monitors. Blank = the `gh`-authenticated user (`viewer.login`), resolved
  eagerly at launch so it's the default everywhere.
- <a id="agent-runner"></a>**Agent runner** - which agent CLI a spawn runs:
  **Claude Code** (the default, and what every existing install keeps), **OpenCode**
  or **Hermes**. Only the agent command changes - the agent word and its flags, or
  for OpenCode 2.x a short chain of `opencode api` calls ahead of its TUI (below);
  the prompt, the staged file, the completion sentinel, the pid a run is identified
  by and every monitor above it are the same whichever it is, which is the point of having one setting
  rather than a second pipeline. All three are windowed, so a run can be watched and
  typed into. Like the repo root the setting lives in the shared
  `~/.diplomat/config.json`, so a running mesh node picks it up on its next spawn -
  and which runner a given run *started* under is written into its run directory, so
  switching mid-flight can't interrogate a live agent through the wrong store.
  - **OpenCode 1.x and 2.x** are both supported, and are told apart by `opencode
    --version` (major 2 or later is 2.x; no answer is 1.x), asked of the `opencode`
    a spawned agent would run: the one the user's login shell, running an
    interactive one, finds on its `PATH` - so an install only `~/.zprofile` or only
    an rc names still counts. That path is re-resolved once it is gone or a minute
    old, and the version re-asked whenever the binary changes on disk. They share
    almost nothing below the setting. A 1.x run is its own server: the TUI is started
    as `opencode --port <n> [-m <model>] --prompt "…"`, with the permission grant in
    `OPENCODE_PERMISSION`. 2.x has one per-user background service that every client
    talks to, a TUI with no `--port` or `-m`, and a `--prompt` that fills the composer
    without submitting it (upstream anomalyco/opencode#51135) - so a 2.x run is
    started through the service instead: Diplomat mints a fresh `ses_diplomat_<hex>`
    session id, stages its create and prompt bodies beside the prompt file (the
    checkout, an allow-all permission ruleset, and the pinned model if any), and runs
    `opencode api session.create … && opencode api session.prompt … || exit; opencode
    --session <id>` - the turn is already running when the TUI attaches to it, and the
    TUI is the process the run's pid file names.
  - **Model** (OpenCode, Hermes) - a model id such as
    `openrouter/moonshotai/kimi-k2` or `ollama-cloud/glm-5.2`. Blank leaves the
    choice to that runner rather than overriding it with a guess: its own picker, or
    for an OpenCode 2.x run the service's configured `model` (else its first available
    one). A 2.x pin is split the way 2.x itself splits one - the provider before the
    first `/`, the model after it, and a `#variant` suffix from the last `#` on.
  - **Connect a provider…** (OpenCode, Hermes) - opens that runner's own login wizard
    in a terminal (`opencode auth login`, which 1.x also accepts as an alias of
    `providers login`; `hermes setup`). Diplomat deliberately
    has no API-key field: each runner already knows its whole provider catalog, which
    entries take OAuth rather than a key, and where each one's credentials belong -
    and each writes them to the store its agent reads from anyway. **No provider
    credential is ever stored by Diplomat**, which matters because
    `~/.diplomat/config.json` is world-readable and copied around by the mesh.
  - **How a run is watched.** Both foreign runners are *asked* whether their turn is
    over rather than having it read off their status bar - positive evidence, instead
    of whether someone else's `esc interrupt` hint happened to be drawn when the poll
    looked. They answer from different places. An OpenCode 1.x agent is spawned with
    `--port <n>` on a port Diplomat reserved for it, so it serves its own session on
    loopback while it works; the port is unauthenticated (OpenCode's server takes a
    password but its own TUI sends none), so it is reachable by other users of the
    same machine and nothing else. A turn is over there when two things agree - the
    server is running no turn in that session, and the last message it wrote carries
    a completion stamp. It takes both: OpenCode writes one message per *step* of a
    turn and stamps each as it completes, so the stamp alone calls a turn over in the
    gaps between steps, while the status alone calls a session idle in the moment
    before its first turn starts. An OpenCode 2.x run is asked of the per-user
    service, found through `opencode/service.json` under the `$XDG_STATE_HOME` that
    same shell reports (else `~/.local/state`) and authenticated with the password in
    it: its session is busy while the service lists it as running - 2.x keeps a
    session listed from the start of a turn to its end, with no gaps between steps -
    and idle once it is not listed *and* carries the `time.idle` stamp a finished
    turn leaves, which a session created but not yet running has not got. Closing a
    2.x run's window does not stop its turn, which runs on in the service, so
    Diplomat interrupts the turn whenever it closes one (a backstop reaping a wedged
    run) and whenever it retires one - booked or found in the process table - with no
    2.x TUI attached to its session left in the process table (a window closed by
    hand, a TUI that quit or crashed); one retired with its TUI still open, its PR
    merged mid-turn, keeps working as a 1.x or Claude Code agent would. An idle
    session ignores the interrupt. Hermes serves no such port, and needs none: it
    writes every session and message to `~/.hermes/state.db` as it goes, which
    Diplomat opens read-only. A turn is over there when the agent stamps its own
    message `finish_reason` (`tool_calls` is mid-turn, `stop` is the end) *and*
    nothing is still coming back to it - `delegate_task(background=true)` runs its
    subagents on an executor of their own and hands the turn straight back, reporting
    later as a fresh user turn, so a fan-out whose result is still undelivered holds
    the run open exactly as an unfinished message does. A Hermes or OpenCode 1.x
    session is matched to the run by the staged prompt, which both store verbatim as
    the session's opening message - the only exact key, since neither keeps a store
    per run: Hermes' is the machine's, OpenCode's is the checkout's and every worktree
    of it, and a busy one holds hundreds of sessions that are not this run's. A 2.x
    session Diplomat spawned needs no match: it chose the id and bound it to the run
    before the spawn. One the mesh placed here has no id bound and no prompt in its
    TUI's argv (`opencode --session <id>`), so its session is taken from the process
    table and matched by the opening prompt the service holds for it - which is also
    how the `ps` scans behind the mesh's dedup and pid-less runs see a 2.x agent. And either way the answer *ends* the run, exactly as a Claude Code hook's
    does: it is the same fact from the same kind of source, the agent's own word
    rather than a screen read for signs of one. The exception is a 2.x agent with no
    run of its own - one the scan found on a PR, not dispatched - which only leaving
    the process table ends: idle, it reads as awaiting input. A run that cannot be reached - the
    port was taken, the server or service has not come up, the store is not there -
    falls back to the status bar exactly as a Claude Code run does.
  - **How a run is priced.** OpenCode reports a turn's cost per message, so a
    finished run is summed from its session export when it ends, not from the poll -
    `opencode export <session>` on 1.x, `opencode session export <session>` on 2.x. Hermes keeps running totals on the session row, so it is simply read. Both
    count input + output + cache *writes*, the same three the Claude Code transcript
    scan sums, so one ledger holds every runner in one unit. What those tokens are
    *not* is a share of a rate-limit window: that window is the Anthropic account's,
    priced from Claude Code's own usage probe, so **limit per task** counts the tasks
    that ran on Claude Code and leaves a foreign run to the token figures beside them.
    The [spending budget](automation.md#the-spending-budget) gates every runner, each in its
    own currency: Claude Code against its windows, any other runner in dollars - what
    its tasks were charged, against what is left on the OpenRouter key and balance.
    Only Hermes reports what a task was charged, and the dollar gate prices any
    non-Claude runner from the tasks billed here in the last 14 days, so it holds
    nothing until a Hermes task has been billed in that span.
  - What does *not* carry over: the [Claude API-error watcher](automation.md#autonomous-monitors)
    - its banners are Claude Code's. A foreign agent that errors reads as idle, so
    it gives its task-cap slot back and the monitor that owed the work dispatches it
    again.
- **Repo root** - the local checkout every spawned agent `cd`s into, with a
  **Choose…** directory picker (type a path if you prefer; a leading `~` expands).
  Blank = `~/dev/<repo>` for whichever repo [`assets/config.json`](../packages/diplomat-core/assets/config.json)
  targets. The hint warns when the path isn't absolute, or has no `.git` - the
  spawn's `cd` is best-effort, so an agent would otherwise start in your home
  directory unnoticed. `DIPLOMAT_REPO` still outranks the field, and says so in the
  hint when it's set. Like the runner and every other key a node reads, this one is
  **not** in UserDefaults: a mesh node spawns agents from its own stdlib-only
  process, so the pick lives in
  the shared `~/.diplomat/config.json` that both front-ends and the node re-read on
  each spawn - change it and a *running* node picks it up.
- **Auto-queue fixes for my PRs / Auto-queue reviews that request me** - the two
  monitor toggles, with live status: PRs watched, reviews done so far, "N
  unaddressed reviews - retrying", and any poll failure. (The combined *fixed N*
  counter lives on the panel's status pill, not here.) A monitor switched off keeps
  polling and keeps listing what it finds under [Agent tasks](panel.md#agent-tasks); what
  stops is the automatic start. Nested under the **review-requests** toggle: the
  **author allowlist** (*Only these authors*, blank = anyone), which stays editable
  while the toggle is off because it is what decides which requests get listed, and -
  visible only while it's on - **May approve / request changes** (the verdict switch)
  with its three withhold-the-verdict suppressors (SKILL / installer / community), and
  **Soft-approve clean PRs** (a thank-you COMMENT review, never an APPROVE; independent
  of the verdict switch).
- **Fix new community issues** - the [new-community-issue
  monitor](automation.md#autonomous-monitors) (**default on**). Unlike the two toggles above,
  switched off it queues nothing, and issues filed while it is off are left for a
  [Fix-issues](actions.md#fix-issues) sweep.
- **Run at most N automatic tasks at a time** - this machine's hard cap on
  concurrent automatic agents (**default 2**, range 1-16), across every monitor,
  the work a sweep queues, and any work a mesh peer routes here. The agent a
  wizard press opens on the spot is never capped and doesn't count against it; work
  over the cap is deferred to the next poll, not dropped. Like the repo root and for
  the same reason, it lives in the shared `~/.diplomat/config.json` rather than
  UserDefaults - the node that runs peer-routed work is a separate stdlib-only
  process, and a machine with two answers to "how many at once" has no cap at all.
- **Hold work when the limit runs low** - the
  [spending budget](automation.md#the-spending-budget) (**default on**), with the confidence
  it must reach that a task fits (**default 95%**, one-sided) and what to keep in
  hand while the ledger cannot price a task yet - a share of a window (**default
  20%**) under Claude Code, or dollars on the account (**default $1.00**) under a
  runner billed in money, once a task has been charged here in the last 14 days. Priced from the same per-task figure the
  [Telemetry](telemetry.md) screen shows, against both rate-limit windows or against
  both of the OpenRouter ceilings. Held work waits under [Agent tasks](panel.md#agent-tasks)
  and starts when a window refills or the balance is topped up; *execute now*
  overrides it, a wizard spawn that
  opens a terminal on the spot is never gated, and nothing is held at all while the
  probe cannot read a limit. In `~/.diplomat/config.json` for the same reason as the
  cap above.
- **Auto-continue agents on API errors** - the terminal watcher toggle, plus a
  count of nudges sent.
- **Give up on a task after 4h** (**default on**) - the last-resort backstop under
  every rung that reads the run itself; at four hours it closes the window of an
  automatic task the ladder still calls working and hands its bay back. See
  [Giving up on a run](automation.md#giving-up-on-a-run). In `~/.diplomat/config.json` beside the
  cap it hands bays back to.
- **Tools - color & visibility** - a **color well** to retint each tool plus a switch
  to hide it; hidden tools drop out of the grid and the reverse-lookup checklist.
- <a id="spawn-terminal"></a>**Spawn terminal** - which terminal SPAWN AGENT opens:
  **Ghostty**, **iTerm** or
  **Terminal**, tried in that order, with Terminal the always-present fallback.
  Ghostty leads because its window is created *with* the agent command in it, so
  there is no window sitting empty for the command to be typed into and dropped -
  the other two are driven by typing, which is what the five-second input settle is
  for. The trade is that Ghostty can be told to open a window but never asked what
  is in one, so its agents are run inside **tmux** and read with `capture-pane`;
  without tmux on the box Ghostty is not offered at all, because a run whose screen
  cannot be read holds its task-cap slot until a human closes the window.
  The device allocator opens its repair agents in the same terminal: the applet
  publishes the one its spawns resolve to as `spawnTerminal` in `~/.diplomat/config.json`.
- **Device allocator (MCP)** - install/uninstall the bundled allocator daemon +
  MCP server (see [`packages/device-allocator/README.md`](../packages/device-allocator/README.md)), with
  install status, the installed version, and whether it is still current. It
  registers as **`diplomat-device-allocator`**; installing also clears the old
  `argent-device-allocator` registration, so a pre-rename setup migrates itself.
  Both applets install it on first run and refresh it when a `git pull` has moved
  the skill, rule, CLAUDE.md block or registration out from under an installed
  copy - the status then reads **Out of date** and names what drifted. An
  allocator you *uninstall* here stays uninstalled; only an existing install is
  ever refreshed. Two idle limits sit under it, counted in minutes with nothing
  changing on the device's screen: a booted device no agent holds is shut down
  after **5** (0-60), and a device an agent holds is taken back after **30** (0-240),
  after which it counts as free. 0 turns either off. They are
  `deviceIdleFreeMinutes` and `deviceIdleLeasedMinutes` in `~/.diplomat/config.json`,
  which the daemon re-reads every minute. The [Devices screen](devices.md) holds the same
  controls, the device list, and the remote device pool
  ([allocator README](../packages/device-allocator/README.md#a-remote-device-pool)).
- **Mesh (LAN P2P)** - opt into [Diplomat Mesh](mesh.md):
  a toggle that starts/stops the local node (off by default), with live node/peer
  status. The node outlives the app, so a launch with the toggle off stops one an
  earlier instance left on this machine's state dir (a `mesh-stop` line in the
  activity feed); it has to answer its control port with the pid and id
  `state.json` names, so nothing else is touched. The mesh itself is managed from
  the **⬡ Mesh screen**.
- **Comms (Slack)** - the [Slack watcher](comms.md) (**default off**): the switch,
  keywords, a sender allowlist, one switch per thing it may do (queue reviews and
  fixes, bump queued work, investigate bug reports), notifications, the triage
  model, where the token is read from, and **Test**. Every one is described in
  [Comms → Settings](comms.md#settings).
- **Update** - pull the checkout, rebuild, and relaunch in place. Shows how many
  commits the checkout is behind *and* ahead of upstream, with a ↻ re-check
  button; the button fetches and **merges** (fast-forward when strictly behind, a
  merge commit when you have local commits of your own - `--ff-only` used to refuse
  that), runs `install/build-app.sh`, and reopens the rebuilt app (the newest-wins
  singleton hands over). Uncommitted changes block it outright, and a conflicting
  merge is aborted with "merge by hand" rather than resolved unattended. Needs the
  source checkout on disk (`DIPLOMAT_SELF_REPO`). The same path also runs
  **unattended daily at 06:00** - see [Autostart on login](development.md#autostart-on-login).

All of it persists across launches: in UserDefaults (`com.ignacy.diplomat`), except
the keys a mesh node reads too - repo root, agent runner and model, the automatic-task
cap, the 4h give-up switch and the spending budget's four knobs, and where the Slack
token is read from (never the token itself) - and the allocator
daemon's two idle limits, which live in the shared `~/.diplomat/config.json` ([`AppConfig.swift`](../packages/diplomat-platform/macos/Sources/Diplomat/AppConfig.swift)).

## Definitions / heuristics (where it's deliberately loose)

- **"only open"** - all PR tools query `states: OPEN`; the issues tool queries open issues.
- **"ready for review for >10 days"** - `isDraft == false` and the last
  `ReadyForReviewEvent` (or `createdAt` if it was opened ready) is older than 10 days.
- **"member of the SWM org"** - derived from GitHub `authorAssociation`
  (`MEMBER`/`OWNER` = org; anything else = external). Reliable without org-admin API access.
- **"unaddressed"** (issues) - no comment from a `MEMBER`/`OWNER`/`COLLABORATOR` **and** no assignee.
- **"mine"** - authored by the *effective* handle: the Settings **GitHub username**
  override when set, otherwise the authenticated `gh` user (`viewer.login`).
- **"approved"** - GitHub's aggregate `reviewDecision == APPROVED`.
- **"unaddressed review"** - a `reviewThread` where `viewerCanResolve` (so it *can* be
  marked resolved) is true, `isResolved` is false, and the **last** comment isn't yours -
  i.e. a reviewer pinged and you neither replied nor resolved it.

All of these constants are data-driven from [`assets/filters.json`](../packages/diplomat-core/assets/filters.json) -
retune them there and every front-end picks them up. (The Swift `Filters` shim lives
in `packages/diplomat-core/Sources/DiplomatCore/Models.swift`.)

Every definition above is also bounded by the queries' page caps in
[`assets/graphql/`](../packages/diplomat-core/assets/graphql): the tools see the 100 newest open PRs (100 files /
50 threads each); the monitors see 30 PRs (40 threads) and 30 review requests, with
only the first 60 changed files - so a PR touching more than 60 files can slip a
SKILL or installer path past the verdict suppressors below.

## Auto-refresh

The tool data refreshes every **5 minutes**. Override the interval (seconds, min 5)
for tuning/testing:

```bash
DIPLOMAT_REFRESH_SECS=30 open ./Diplomat.app   # refresh every 30s
```

Each refresh also re-checks every tracked, unmerged PR (one `gh pr view` apiece) so
a run whose PR has landed ends there and then, whatever its process is still doing,
and its row leaves the Agent-tasks list. The [autonomous
monitors](automation.md#autonomous-monitors) are separate, on their own 3-minute schedule.

## A narrow GitHub token for agents

By default a spawned agent uses whatever `gh auth login` stored, which is usually
`repo, workflow, gist`: push access to every repository you can push to, and
`workflow` lets a commit add a GitHub Actions workflow that runs with the
repository's secrets. Review, fix and conflict runs need only `contents` and
`pull-requests`. To give agents just that (phase 1 of
[#139](https://github.com/latekvo/Diplomat/issues/139)):

1. **Mint** a fine-grained personal access token at
   <https://github.com/settings/personal-access-tokens/new>: *Only select
   repositories* (the ones Diplomat works on), **Contents: Read and write**,
   **Pull requests: Read and write**, nothing else. An organization may have to
   approve it before it works on the org's repositories.
2. **Store** it where only you can read it. On macOS, in the login Keychain
   (`-w` last makes `security` prompt for it, so it stays out of your shell history):
   ```bash
   security add-generic-password -a "$USER" -s diplomat-agent-gh -w
   ```
   On Linux, in a file only you can read:
   ```bash
   (umask 077; read -rs t; printf '%s\n' "$t" > ~/.diplomat/agent-token)
   ```
3. **Name** it in `~/.diplomat/config.json` - the name, never the token, since that
   file is world-readable and copied around by the mesh. `"agentTokenKeychainItem":
   "diplomat-agent-gh"` on macOS (`plutil -replace agentTokenKeychainItem -string
   diplomat-agent-gh ~/.diplomat/config.json`), `"agentTokenFile":
   "~/.diplomat/agent-token"` on Linux.

Every spawn on the machine then reads it - panel and automatic runs on both
platforms, and runs a mesh node starts here - inside the spawned shell, and exports
it as `GH_TOKEN`, which `gh` ranks above its own login. The agent's `git` over HTTPS
to github.com gets it too, from `gh auth git-credential`, instead of any credential
helper you configured (macOS git ships `osxkeychain`, which holds your broad login).
The token itself is never in an AppleScript, an argv `ps` can show, a prompt file or
the activity feed; the feed marks each run started on it with `· agent GH token`. A
token that cannot be read (missing item, empty file) starts nothing rather than
falling back to the broad login: the spawn fails, and the work stays owed. Remove the
key to go back.

What it does not cover: `git` over SSH still pushes with your SSH keys, and a
`GH_TOKEN` or `GIT_CONFIG_COUNT` your shell rc exports replaces this one, because the
agent's shell sources your rc after Diplomat sets it. To check it took, run
`gh auth status` inside a spawned agent's window: it names `GH_TOKEN` as the source.
