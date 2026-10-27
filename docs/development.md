# Development

Running from a checkout, the headless self-tests, and how the repository is put together.

## Design

**Two front-ends, one brain.** The macOS SwiftUI app and the
[Linux Qt6/PySide6 applet](../packages/diplomat-platform/linux/README.md) are thin UI renderers over a shared,
language-neutral [`assets/`](../packages/diplomat-core/assets/README.md): the GraphQL queries, tool catalog,
filter constants and prompt fragments are single-sourced, and golden-prompt tests
on both sides fail CI on prompt drift. Both applets run the full autonomous
monitor stack. See **[Architecture](#architecture)**.

**One pipeline, two triggers.** A wizard's SPAWN button and an auto-monitor's
poll tick are two *triggers* for the very same dispatch pipeline
(`Store.dispatchAgent` / `store.dispatch_agent`): the ban check, in-flight
dedup, mesh coordination, spawn, run registration, and counters live in exactly
one place per platform.
The only trigger asymmetries are the documented ones in `AgentDispatchGate`
(its Python twin `autofix.dispatch_decide`): manual spawns come to the
foreground (macOS), are never mesh-gated and never hit the
[automatic-task cap](automation.md#autonomous-monitors); monitor dispatches get the
`Auto · … · retry N` label, and only they bump the auto-handled counters.
Parity tests on both sides pin that matrix. A job arriving *over the mesh* is
the one spawn that bypasses the pipeline - it lands through the mesh node's own
runner, and the untracked-agent scan is what re-attaches it afterwards.

**One answer about what the agents are doing.** Whether a PR is in flight, how
many bays of the device's [task cap](automation.md#autonomous-monitors) are full, which rows
the panel draws, which record is retired and whose window is closed with it are
five *projections* of a single resolved tick (`AgentState` / `agentstate.py`), not
five derivations that can drift apart. Evidence reaches it typed - each probe
answers *present*, *unavailable* or *unsupported* - and the ladder never reads
"I could not look" as "it is gone": a run ends only on positive evidence (its
sentinel, its pid missing from a process table that was actually read, or its
mesh claim released), and anything else resolves to `unknown`, which keeps its
slot and says so. The one rung that ends a run on a CLOCK rather than on evidence
about that run is the [four-hour deadline](automation.md#giving-up-on-a-run) an operator can
switch off, and it is the last rung of the ladder rather than one in the middle. A run is identified by the pid the shell that runs the
agent writes into
`~/.diplomat/agents/<run-id>/pid` before handing over to it, and the book survives
a restart on both platforms. `DIPLOMAT_AGENTS=1 python -m
diplomat_app` prints the whole chain: every record, every probe's raw answer,
every verdict and the one fact that decided it.

## Run

From a checkout you already have. (No checkout? [`npx szpont`](../README.md#install) makes one
and does everything below for you.)

```bash
cd ~/dev/diplomat/packages/diplomat-platform/macos
swift run Diplomat    # launches the menu-bar app (no Dock icon)
```

> The app is its own Swift package; the shared core is the one next door
> (`packages/diplomat-core`), which builds the Linux-buildable `DiplomatCoreSmoke`
> self-test and the `diplomat-core` prompt CLI the Linux front-end shells out to.
> The core holds more than one executable, so name the target there.

Quit from the panel's ⏻ button. `pkill Diplomat` leaves no operator-quit marker, so
the [watchdog](#autostart-on-login), if installed, relaunches the app within five minutes.

**On Linux?** See [`packages/diplomat-platform/linux/README.md`](../packages/diplomat-platform/linux/README.md)
- `cd packages/diplomat-platform/linux && ./diplomat`.

**First run from a terminal** (`swift run`, interactive TTY) offers to set itself up
as a login daemon:

```
┌─ Diplomat setup ─────────────────────────────────────────
│ Install as a background daemon? This will:
│   • build Diplomat.app inside this checkout
│   • delete any Diplomat.app in /Applications or ~/Applications
│   • add a per-user LaunchAgent so the wrench boots on login
│   • start it now (it replaces this foreground instance)
│   • ask macOS for permission to control your terminal (SPAWN)
└──────────────────────────────────────────────────────────────
Accept [y/N]
```

Accept and it runs `install-autostart.sh` for you (and the daemon takes over via the
newest-wins singleton). The prompt is skipped when launched non-interactively
(`open`, launchd) or once already installed. On first launch it also pokes the
chosen terminal once so macOS shows the *"control <terminal>"* permission prompt
up front, instead of on your first SPAWN.

### Double-clickable applet (recommended)

Everything in this section runs from the macOS package,
`packages/diplomat-platform/macos` - the bundle is built beside it, not at the
repo root.

```bash
./install/build-app.sh     # produces ./Diplomat.app (menu-bar-only, no Dock icon)
open ./Diplomat.app
```

To have it back at every login, use the autostart script below: it starts the bundle
in place. Do not copy it to `/Applications` or `~/Applications` - the next install
deletes a copy there, and **Update** rebuilds only the bundle in the checkout.

### Autostart on login

```bash
./install/install-autostart.sh     # rebuilds, installs the app + all three LaunchAgents, starts it now
./install/uninstall-autostart.sh   # removes all three LaunchAgents and stops the app
```

Installs a per-user LaunchAgent at `~/Library/LaunchAgents/com.ignacy.diplomat.plist`
(`RunAtLoad`), so the wrench reappears on every login. launchd starts the bundle where
`build-app.sh` writes it, `packages/diplomat-platform/macos/Diplomat.app`, so the
instance that greets you at login is the one **Update** rebuilds; nothing is copied to
`/Applications`.

It also installs a **second** agent, `com.ignacy.diplomat.autoupdate`, which fires
daily at **06:00** and runs the app binary headless (`DIPLOMAT_SELF_UPDATE=1`):
merge upstream if behind, rebuild the bundle, and relaunch a running instance onto
it - unless the checkout's volume is below the
[free-disk floor](automation.md#the-free-disk-floor), when it logs a `skip:` and does
neither. It's the unattended twin of the Settings **Update** button, and it logs to
`~/Library/Logs/diplomat-autoupdate.log`. Manage it on its own with:

```bash
./install/install-autoupdate.sh    # (also called by install-autostart.sh)
./install/uninstall-autoupdate.sh
```

A **third**, `com.ignacy.diplomat.watchdog`, runs the binary headless every 5 minutes
(`DIPLOMAT_WATCHDOG=1`, `StartInterval`) and launches the app if no instance is up -
after a crash, a force-quit, a kill or a failed update. The 06:00 run does the same.
Neither brings back an app you quit yourself (the ⏻ button, `osascript … quit`,
Activity Monitor's Quit): that leaves `~/.diplomat/operator-quit`, which the next
launch clears. A hand-over to a newer instance leaves no mark, and the watchdog only
acts when there is no instance at all, so it never contends with the newest-wins
singleton or an update's relaunch. It launches the bundle the updater builds, and logs
what it launched to the same file. Manage it with `./install/install-watchdog.sh` /
`./install/uninstall-watchdog.sh`.

### Headless self-test

Every mode runs the real pipeline once, prints, and exits - none of them start
the monitors or touch a terminal (except `TRACK_TEST` and `SPAWN_FOCUS_TEST`, whose
point is exactly that; and `RENDER=live`, which opens a window and stays up until
you stop it). `packages/diplomat-platform/macos/Sources/Diplomat/Headless.swift` is the one list that
decides what counts as headless. A variable that asks for a mode the build does not
run - a `DIPLOMAT_*` name ending `_TEST`, `_DUMP`, `_SCAN` or `_POLL` that is not on
the list, a Linux-only mode, or a flag set to anything but `1` - stops the binary with
exit 64 before it can start as the live app:

```bash
DIPLOMAT_DUMP=1 swift run Diplomat            # real fetch+filter pipeline, prints all 6 tools, exits
DIPLOMAT_LOOKUP=337 swift run Diplomat        # reverse-lookup one number through the real Store
DIPLOMAT_PRINT_PROMPT=mine swift run Diplomat # assemble + print a prompt: mine|user|single (append
                                                     #   -final for the verdict pass), conflicts[-user|-single],
                                                     #   issues[-single] (append -features),
                                                     #   audit[-issues|-prs|-all]
DIPLOMAT_SETTINGS_DUMP=1 ./Diplomat.app/Contents/MacOS/Diplomat  # resolved persisted settings
DIPLOMAT_QUEUE_TEST=1 swift run Diplomat      # self-test: the queue behind the automatic-task cap
                                                     #   (capture, dedup, arrangement, what a paused
                                                     #   monitor holds, free slots, what a task being
                                                     #   started is while its spawn runs) and the mesh row a
                                                     #   peer-routed task leaves behind (its lease's
                                                     #   lifetime, and that neither liveness source
                                                     #   touches the other's rows). Spawns nothing;
                                                     #   redirects its own audit writes.
DIPLOMAT_LOWDISK_TEST=1 swift run Diplomat    # self-test: the free-disk floor against a real
                                                     #   volume (scripts/full-volume.sh) filled and drained,
                                                     #   through the dispatcher and the 06:00 updater.
                                                     #   Spawns nothing; skips where no volume can mount
                                                     #   unless DIPLOMAT_FULL_VOLUME_REQUIRED=1.
DIPLOMAT_RENDER=panel    ./Diplomat.app/Contents/MacOS/Diplomat  # snapshot a screen to PNG (out
                                                     #   path: DIPLOMAT_RENDER_OUT). States: panel|panel-procs
                                                     #   natural|settings[-explain]|settings-live|approved
                                                     #   unban-confirm
                                                     #   activity[-filtered] (audit feed + its filter chips)
                                                     #   wizard[-other|-specific[-mine|-theirs]|-wrong|-banned]
                                                     #   devices[-open|-procs]|conflicts[-other|-specific|-wrong]
                                                     #   audit[-issues|-prs|-all]
                                                     #   mesh (⬡ screen over a synthetic topology); mesh-blocked
                                                     #   (the not-discoverable banner); mesh-reminder (trust modal)
                                                     #   telemetry (the screen over a synthetic ledger, written to a
                                                     #   scratch dir); telemetry-panel (the same inside the panel)
                                                     #   popover (REAL NSWindow snapshot incl. the legacy
                                                     #   scroller - pair with DIPLOMAT_POPOVER_CAP=400
                                                     #   to force the scrolling state)
                                                     #   window-<state> (any state above through a real
                                                     #   window: ImageRenderer draws no AppKit control, so
                                                     #   window-settings is the only faithful Settings shot)
                                                     # DIPLOMAT_RENDER_THEME=light|dark snapshots the other
                                                     #   appearance without switching the machine over
                                                     #   live (the real popover ON-SCREEN, left running, to
                                                     #   drive the queue's drag + execute now with a mouse;
                                                     #   its queued rows resolve in-flight, so no spawn)
DIPLOMAT_TRACK_TEST=1    ...                     # E2E of session tracking via a real throwaway terminal
                                                     #   window; exits non-zero on failure
DIPLOMAT_SPAWN_FOCUS_TEST=1 ...                  # E2E that background spawns keep focus and foreground ones
                                                     #   don't - drives two throwaway windows; exit code = verdict
DIPLOMAT_DEVICE_DUMP=1   ...                     # device-allocator paths + daemon state, printed, plus the
                                                     #   installed version and what (if anything) has drifted
DIPLOMAT_ALLOCATOR_TEST=1 ...                    # the launch-time allocator decision: reinstall a stale copy,
                                                     #   leave an uninstalled one alone. Shells no installer;
                                                     #   exit code = verdict
DIPLOMAT_MESH_STRAY_TEST=1 ...                   # a launch with the mesh off stops the node state.json names,
                                                     #   and not a reused pid, another dir's node or identity.
                                                     #   Scratch state dirs; exit code = verdict
DIPLOMAT_AUTOFIX_POLL=1  ...                     # one real monitor poll: prints its dispatch decisions and
                                                     #   the exact prompts it would spawn, opens nothing
DIPLOMAT_APIWATCH_SCAN=1 ...                     # dry-run the API-error watcher over live sessions, sends nothing
DIPLOMAT_APIWATCH_TEST=1 ...                     # self-test: who a scan may type into (an agent's session,
                                                     #   including one a terminal only shows through tmux;
                                                     #   never a plain shell). Reads no terminal, sends nothing
DIPLOMAT_SELF_UPDATE=1   ...                     # the unattended 06:00 update: merge if behind, rebuild,
                                                     #   relaunch a running instance, launch a dead one
DIPLOMAT_WATCHDOG=1      ...                     # the 5-minute check: launch the app if no instance is up
                                                     #   and the operator did not quit it
DIPLOMAT_WATCHDOG_TEST=1 ...                     # self-test: who brings a dead app back and who leaves it
                                                     #   closed, over fixture job steps; exit code = verdict
DIPLOMAT_RELAUNCH_TEST=1 ...                     # self-test: a relaunch is judged on the instance `open`
                                                     #   started, which gets no headless marker; opens only
                                                     #   throwaway bundles; exit code = verdict
DIPLOMAT_REPOPATHS_TEST=1 ...                    # self-test: which checkout the bundle names, from inside
                                                     #   it or from a copy; scratch dirs; exit code = verdict

# The shared core itself is independently buildable & testable (also on Linux):
swift run DiplomatCoreSmoke                    # loads assets/, runs filter + prompt + golden-file assertions
DIPLOMAT_DUMP=1 swift run DiplomatCoreSmoke    # + live gh dump, cross-checks the Linux front-end
DIPLOMAT_GOLDEN_WRITE=1 swift run DiplomatCoreSmoke  # regenerate assets/golden-prompts/ after an intentional change
```

The `SETTINGS_DUMP` / `RENDER` checks read UserDefaults, so run them through the
`.app` bundle's binary (it shares the GUI's `com.ignacy.diplomat` domain).

Cadences and paths are overridable too, for tuning: `DIPLOMAT_AUTOFIX_SECS`,
`DIPLOMAT_APIWATCH_SECS`, `DIPLOMAT_PROC_POLL_SECS` (min 2s), `DIPLOMAT_MESH_POLL_SECS`,
`DIPLOMAT_CORE` (where the shared `assets/` live), `DIPLOMAT_DEVICE_ALLOCATOR_DIR`,
`DIPLOMAT_NODE` / `DIPLOMAT_PYTHON` (the `node` / `python3` to use),
`DIPLOMAT_REPO` (the agents' repo root - outranks Settings ▸ *Repo root*) and
`DIPLOMAT_CONFIG` (where that shared `config.json` lives).

## Architecture

The triage logic is single-sourced in
[`packages/diplomat-core/assets/`](../packages/diplomat-core/assets/README.md) -
language-neutral GraphQL queries, the tool catalog, filter constants, and the
prompt fragments for all four actions. Both front-ends load it and assert their
assembled prompts byte-for-byte against `assets/golden-prompts/`, so they can only
drift from each other by failing a CI job. Both also run the full monitor stack;
what stays macOS-only is the per-row **Merge** button, the clickable *session* rows
of the [Agent tasks](panel.md#agent-tasks) list (a Linux spawn is a detached `Popen` with no
window handle, so a running agent gets a row there but not a window to focus), and
reading arbitrary terminal windows (the Linux watcher drives tmux panes instead -
which is also how Linux reads *awaiting input*).

This repository is a **monorepo of independent parts**: everything lives in
`packages/`, one directory per package, and CI is arranged to keep them
independent rather than merely to say they are. Each package that could stand
alone gets a job that installs *only* what that package needs, so a dependency
creeping back in fails a build instead of going unnoticed:
[`szpontnet-core`](../packages/szpontnet-core/README.md) is tested with no Qt, no
`diplomat-core` and no Diplomat on the import path, and
[`device-allocator`](../packages/device-allocator/README.md) with nothing but Node.
Diplomat is checked from the other side - one step deletes both SzpontNet
packages outright and renders the applet from what remains.

CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) is seven jobs:
`swift-macos` (build both Swift packages + the core smoke + headless panel and
telemetry renders + the helper self-tests), `swift-core-linux` (proves the core builds on
Linux, and publishes a static `diplomat-core` binary), `python-linux` (pytest
against that binary, so the golden-prompt parity is proven across languages, then
the library-less start), `szpontnet` (the library's own tests plus a full
conformance run against the reference node), `szpont` (the launcher, and the built
wheel installed into a venv that has never seen the checkout), `szpont-npm` (the
same launcher in JavaScript, held to the Python one's plan machine shape for
machine shape), and `node-device-allocator`.

```
packages/
  device-allocator/            ← the `diplomat-device-allocator` MCP server + daemon (see its README),
                                 arbitrating simulator/emulator allocation between the agents on this
                                 machine (request/await/free/change/broken + repair; leases persist across
                                 daemon restarts in ~/.diplomat/device-allocator/, idle leases reclaimed
                                 after 30 min and idle unheld devices shut down after 5, both settings;
                                 a prompt-injection report bans the author and terminates
                                 the reporting agent). Standalone: any MCP client can point at src/mcp.js

  diplomat-core/               ← the shared triage brain: everything both front-ends agree on
    assets/                    ← the language-neutral source of truth (see its README): GraphQL queries,
                                 tool catalog, filter constants, prompt fragments, the mesh model
      golden-prompts/            canonical prompt outputs, asserted byte-for-byte by BOTH platforms' tests
    Sources/
      DiplomatCore/            ← Foundation-only Swift; loads assets/. Builds on macOS AND Linux.
        CoreAssets.swift           resolves + decodes assets/ (config, catalog, filters, review, conflicts, audit, comms, graphql)
        GH.swift                   gh CLI shell-out (GraphQL via assets/graphql)
        Models.swift               domain models, Filters, Fmt, API
        ToolKind.swift             tool catalog enum + DisplayItem/LookupResult + pure ToolData engine
        Review.swift               ReviewDepth + ReviewConfig prompt builder + VerdictPolicy (assets/review.json)
        Conflict.swift / Audit.swift  ConflictConfig + AuditConfig prompt builders (assets/conflicts.json, assets/audit.json)
        PRRef.swift / PRTarget.swift  single-PR reference parsing + the whose-PRs axis shared by the wizards
        Autofix.swift              PRSnapshot + the monitor's edge-trigger diff, AgentDispatchGate
                                   (the task cap and the rate-limit budget), AutofixMesh
        AgentTasks.swift           the Agent-tasks list's sort order + the queue behind the task cap
        ReviewReconcile.swift      pure retry/backoff/dedup decisions for the monitors
        ReviewAudit.swift          a retired review run's review: left PENDING, or split over several
        AgentActivity.swift        terminal-tail classification: running vs awaiting input
        AgentCompletion.swift      the turn report: the hooks a run is spawned with, and reading them back
        AgentRunner.swift          which agent CLI a spawn runs, and the one command that runs it
        OpenCodeAPI.swift          reading an OpenCode run's own session: whose it is, mid-turn or not, spend
        HermesStore.swift          the same, for a Hermes run's session in its SQLite store
        AgentState.swift           the one resolver: typed evidence -> a state per agent run,
                                   and its projections (dedup, cap, rows, retirement, reaping)
        AgentRegistry.swift        the durable run book both applets read/write (~/.diplomat/agents)
        ApiErrorMatch.swift        "is this a Claude API error?" matcher for the watcher
        AuditCategory.swift        audit action verb → activity-feed filter category (mirrors assets/audit-categories.json)
        Comms.swift                Comms (the Slack watcher): its types, the triage answer's validation, what
                                   each answer does, and the investigate prompt (assets/comms.json)
        SlackAPI.swift             Slack Web API reads (auth.test, search.messages) + who becomes a ping
        CommsWatch.swift           one watcher tick: search, watermark, the sandboxed `claude -p` classifier
        CommsLog.swift             ~/.diplomat/comms/events.jsonl, both applets' log, folded for the screen
        LoginShell.swift           locating a CLI through the operator's login shell, past any alias
        Mesh.swift                 mesh model: decodes assets/mesh.json + ~/.diplomat/mesh/state.json, pure placement
        Telemetry.swift            folds the telemetry ledger + every figure on the Telemetry screen: window
                                   calibration, the distribution + its confidence interval, the quota readings,
                                   the pending series
      DiplomatCoreSmoke/       ← Linux-buildable core self-test (filters + prompts + golden files + live dump)
      DiplomatCoreCLI/         ← thin `build-prompt` CLI over the core (ships as the `diplomat-core` binary),
                                 so the Linux front-end shells out for Review/Issues/Conflicts/Audit prompts
                                 instead of reimplementing them; `tool-data` and `telemetry` expose the two
                                 engines the Linux side reimplements, so the parity tests can diff them;
                                 `comms-tick`, `comms-feed` and `comms-effect` run Comms for the Linux applet

  diplomat-runtime/            ← the platform-neutral Python half: the twin of diplomat-core plus
                                 everything below the UI that has no Swift counterpart
    diplomat_runtime/            no Qt, no AppKit - the assets loader, PR triage, the run book, token
                                 accounting, the spawner. The Linux applet imports it; the macOS app's
                                 mesh node runs it with nothing else of Diplomat's on its path
      szponthost.py            ← Diplomat's answers to the six questions a mesh node asks its host:
                                 the duty catalog, the state dir, where events go, how a job runs here,
                                 whether an agent is already up on that work, and whether this machine
                                 has room for another and the limit - window or balance - to afford it

  diplomat-platform/           ← the platform wrappers: one UI each over that same core
    macos/                     ← macOS SwiftUI menu-bar app - thin UI over the core
      Sources/Diplomat/
        DiplomatApp.swift          @main entry + app + MenuBarExtra + the headless self-test entry points
        Headless.swift             the single "are we a one-shot self-test?" env-var list
        ContentView.swift          two-column panel (left: monitoring lists, right: grid + wizards/results)
        Components.swift           shared UI atoms (cards, chips, badges)
        ReviewWizard.swift         Review-PRs wizard + AgentSpawner (staged prompt file, done sentinel, Ghostty/iTerm/Terminal)
        IssueWizard.swift          the Fix-issues wizard
        ConflictWizard.swift / AuditWizard.swift   the Resolve-conflicts and Full-E2E-test wizards
        SettingsView.swift         settings (username, repo root, monitors + auto-approve + task cap + rate-limit budget, watcher, tools, terminal, allocator)
        Store.swift                ObservableObject; settings + the monitor/watcher loops; logic in ToolData
        AutofixMonitor.swift       the monitors' GitHub reads (monitor-prs / review-requests queries)
        AutofixStatus.swift        the monitor heartbeat behind the status pill
        ApiErrorWatcher.swift      iTerm/Terminal/tmux screen reader + continue-nudge sender
        AgentProbes.swift          the outside world, typed: `ps`, screens, sentinels, claims -> Evidence
        AgentWindows.swift         where each run's terminal window is, so a row click can raise it
        AgentSessionProbe.swift    asks each run's own agent what it is doing, through its runner's store
        OpenCodeProbe.swift        dials an OpenCode run's own 1.x server (free port, session list, messages) or the 2.x service
        HermesProbe.swift          reads a Hermes run's session out of ~/.hermes/state.db, read-only
        TrackTest.swift            E2E self-test of the run book + this platform's probes (DIPLOMAT_TRACK_TEST)
        QueueTest.swift            self-test of the deferred-task queue (DIPLOMAT_QUEUE_TEST)
        LowDiskTest.swift          self-test of the free-disk floor on a real volume (DIPLOMAT_LOWDISK_TEST)
        SweepTest.swift            self-test of asking each runner's own store (DIPLOMAT_SWEEP_TEST)
        ApiWatchTest.swift         self-test of who the API-error watcher may type into (DIPLOMAT_APIWATCH_TEST)
        PublishTest.swift          self-test of what a resolved tick hands the panel (DIPLOMAT_PUBLISH_TEST)
        BanList.swift / AuditLog.swift   ban list (the daemon's banned.json) + the unified activity feed (audit.jsonl)
        DeviceAllocator.swift      allocator daemon state reader + installer bridge
        DeviceFocus.swift          click an in-use device → focus the holding agent's terminal
        Daemon.swift               first-run login-daemon opt-in (TTY Accept [y/N])
        Render.swift               headless ImageRenderer snapshots for UI checks
        Color+Hex.swift            Color ↔ "#RRGGBB" for persisted tint overrides
        MeshBridge.swift           drives the local mesh node (spawn python3 -m szpontnet --daemon, NDJSON control)
        MeshView.swift             the ⬡ Mesh screen: node graph, tier/token/trust editors, duty table
        MeshSpawn.swift            the wizards' "⬡ Run on mesh" row + destination preview
        TelemetryView.swift        the Telemetry screen: the bell curve, the rate-limit windows, the backlog and finished-work series, the token split
        TelemetryLog.swift         writes/reads ~/.diplomat/pr-monitor/telemetry.jsonl (append-only, rotated)
        UsageScan.swift            Claude Code transcript scanner: repo-vs-other tokens, per-task attribution
        Quota.swift                the OAuth usage probe - what is left of the 5-hour and 7-day windows
        Spend.swift                the OpenRouter balance probe - dollars left on the key cap and the credits
        AutoBudget.swift           ledger + probe + knobs -> may another automatic task start here?
        SelfUpdate.swift           fetch/merge upstream, rebuild, relaunch (Update button + the 06:00 run)
        RepoPaths.swift            locate this app's own checkout (DIPLOMAT_SELF_REPO → the checkout the bundle sits in → the one it recorded → ~/dev/diplomat),
                                   the sibling packages it reaches for, and the agents' repo root
        AppConfig.swift            the cross-process settings file (~/.diplomat/config.json) the mesh node shares
      install/                 ← build-app + the autostart / auto-update (un)installers (launchd)
      tests/                   ← installers.sh: what those two installers write, run against stubs
    linux/                     ← Linux Qt6/PySide6 tray applet (see its README)
      diplomat_app/            ← what is this front-end's own: screens, wizards, the Store driving
                                 them, its self-update and single-instance guards, and probes.py -
                                 this platform's evidence gatherer, twin of AgentProbes.swift
      install/                 ← build-core + the autostart / auto-update (un)installers (XDG + systemd)
      meshsim/                 ← the real-socket mesh simulator the mesh scenarios run through

  szpontnet-core/              ← the SzpontNet node: an independent library (see its README).
    szpontnet/                   stdlib-only Python (runs headless on macOS too) - LAN discovery,
                                 heartbeat links, gossip, deterministic duty assignment, dispatch with
                                 failover; canonical v1 model in netmodel.json, `python -m szpontnet`
      host.py                  ← the six questions a node asks whoever is running it; all six have
                                 working defaults, so a node with no host is a valid node
      env.py                   ← the SZPONTNET_* namespace, one accessor, one place the old names bridge
    tests/                     ← the library on its own terms - defaults, the host seam, the namespace,
                                 a scan that fails if a module so much as names its host, plus the
                                 integration ones (Tor transport, the startup lock, control-edit flush)

  szpontnet-spec/              ← the protocol, kept apart from any implementation of it
    docs/                      ← the normative SzpontNet spec (15 chapters, v0.9.0, wire v: 1)
    conformance/               ← black-box conformance tester: runs a candidate node as an opaque
                                 subprocess, joins over real multicast + TCP, exits non-zero on any MUST failure

  szpont/                      ← what `szpont` means on PyPI (see its README): the `szpont` command
    szpont_launcher.py         ← clone/build/launch Diplomat, out of the standard library alone. Named
                                 apart from `szpont` so the conformance tester keeps the import name

  szpont-npm/                  ← what `szpont` means on npm (see its README): the same launcher, in
                                 JavaScript, for `npx szpont`
    test/scenarios.mjs         ← the machines both launchers are held to, as a directory tree to probe and
                                 as fact sets to plan from; parity-with-python.mjs runs every one through
                                 both and demands the same facts and the same plan

.github/workflows/ci.yml       ← swift-macos · swift-core-linux · python-linux · szpontnet · szpont ·
                                 szpont-npm · node-device-allocator
.github/workflows/release-szpont.yml     ← every push to main → the next minor on PyPI + npm, from one
                                           verified commit; tag szpont-v* to release what a tree states
.github/workflows/release-szpontnet.yml  ← tag szpontnet-v* → PyPI
```
