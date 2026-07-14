# Argent Utils

<img width="1187" height="932" alt="image" src="https://github.com/user-attachments/assets/39fd52dc-8d12-4e50-bd45-3d0d8bf10783" />

## TL;DR:

- Auto reviews PRs which have you listed as reviewer
- Auto fix your own PRs once they get a review
- Auto resolve all conflicts on your PRs
- Enforces one device per agent
- Manually review all PRs of the given person

## Details

A tiny **menu-bar / system-tray applet** - a personal dashboard of Argent-repo
triage tools. Click the wrench, get a dense two-column panel with six utilities,
three spawn-an-agent actions (Review PRs, Resolve conflicts, Full E2E test) and,
on macOS, a set of [autonomous monitors](#autonomous-monitors-macos) that spawn
those agents without being asked. Hacky on purpose, optimized for *me*, not the
public.

Targets `software-mansion/argent` and shells out to the authenticated `gh` CLI.

> **Two front-ends, one brain.** The macOS SwiftUI app and the
> [Linux Qt6/PySide6 applet](linux/README.md) are thin UI renderers over a shared,
> language-neutral [`core/`](core/README.md): the GraphQL queries, tool catalog,
> filter constants and prompt fragments are single-sourced, and golden-prompt tests
> on both sides fail CI on prompt drift. The autonomous monitor stack is macOS-only;
> the Linux applet is a viewer + wizards port. See **[Architecture](#architecture)**.

## The library

| Icon | Tool | What it lists |
|------|------|---------------|
| 📕 (purple) | **SKILL.md PRs** | open PRs touching any `SKILL.md` |
| 📦 (orange) | **Installer/CLI PRs** | open PRs touching `packages/argent-installer/` or `packages/argent-cli/` |
| ⏳ (red) | **Stale Ready >10d** | non-draft PRs that have been ready-for-review for over 10 days |
| 💬 (teal) | **Unaddressed Issues** | open issues **not** opened by an SWM org member that have no team reply and no assignee |
| ✅ (green) | **My Approved PRs** | *your* open PRs whose review decision is `APPROVED` |
| ↩️ (indigo) | **My Unaddressed Reviews** | *your* open PRs with a review thread that's resolvable, unresolved, and that you haven't replied to |

Every row is clickable → opens the PR/issue in your browser. Counts show on each
card; hit ↻ to refresh, ⏻ to quit (with a confirmation prompt). The data also
**auto-refreshes every 5 minutes** in the background, so the counts are fresh the
moment you click the wrench - even if the panel was never open.

**Reverse lookup:** type a PR/issue number in the search box (press **⌘F** to jump to
it) and it instantly shows which of the six lists that number is on - a ✓/— checklist
plus what the number is (open PR/issue, author, draft/ready). Cache-only, so it reacts
as you type. Launch with `ARGENT_UTILS_PREFILL=<n>` to open pre-focused on a number.

## The panel

Two columns. **Right:** everything interactive - the search box, the tool grid
(six tools + three action cards), and whatever a card opens: a tool's result
list, the reverse lookup, or an action wizard. On **My Approved PRs** each row
carries a **Merge** (squash) button - or **Resolve conflicts** when GitHub
reports the PR conflicting, which spawns the fix agent for exactly that PR.

**Left:** the monitoring surfaces, each shown only when non-empty: the monitor
**status pill** (heartbeat: PRs watched, conflicts/reviews handled; "offline"
when polling stops); the **banned authors** list (prompt-injection bans, with
the captured evidence and an inline un-ban); the **agent sessions** list (every
spawned agent, wizard- or monitor-launched, with a live *running / awaiting
input / done / merged* state - click a row to focus its terminal window, ✕ to
stop tracking); the **devices** pool (who holds which simulator/emulator, for
how long, with a per-device kill - clicking an in-use device focuses the holding
agent's terminal); and the **activity** log, one unified audit feed
(`~/.argent/pr-monitor/audit.jsonl`) of panel actions, monitor dispatches,
nudges, and daemon-side bans.

## Actions - Review PRs

The grid carries a **Review PRs** card alongside the tools. Click it and the wizard
opens where the PR lists normally render; dial in a few choices and hit **SPAWN
AGENT** - it opens a fresh terminal window (iTerm if installed, else Terminal)
running a detached review session in `~/dev/argent` that you watch and steer
yourself. The prompt is staged to a file and the window runs
`claude "$(cat <promptfile>)"; printf %s $? > <done>` - the trailing sentinel
(under `~/.argent/pr-monitor/done/`) is how the sessions list knows the agent
finished. The choices are baked into the prompt:

- **Target** — *My PRs* (the resolved handle, see Settings) or *someone else's* (any handle).
- **Scope** — *Review draft PRs* and *Review ready-for-review PRs* (both on by default).
  Untick **both** and a PR-number field lights up: review exactly one PR.
- **Review depth** — a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E with a second double-pass verification.
- **Mark clean PRs ready for review** — *(my PRs only)* flip perfectly-clean drafts to ready.
- **Leave reviews** — *(others' PRs only)* post formal per-line reviews.
- **Reply to others' review threads** — *(my PRs only)* answer and resolve open threads.
- **✨ Final E2E pass + verdict** - *(highlighted, off by default; others' and
  unknown-author PRs only - never my own, there is no self-approval)* appends a
  culminating full-E2E pass on the real binaries with big swarms: APPROVE
  perfectly-clean PRs (after confirming past issues are resolved),
  APPROVE-with-nitpicks when there are only minor asks, or leave **changes
  requested** on real blockers.

Contextual controls (the action checkboxes, the someone-else's handle field, and the
single-PR field) **appear only where they apply** - for a specific PR the wizard
polls the author first and hides the toggles that don't fit (mine → fix-on-branch,
theirs → review-only; banned authors get a flashing warning instead).

> Preview the exact assembled prompt without launching anything:
> ```bash
> ARGENT_UTILS_PRINT_PROMPT=mine swift run ArgentUtils   # also: =user, =single; append -final for the verdict pass
> ```

## Actions - Resolve conflicts

A second grid card, **Resolve conflicts**, spawns a detached agent the same way
(fresh terminal, staged prompt + done sentinel, in `~/dev/argent`) but for keeping
branches merge-able. A single three-way selector picks *whose* PRs to sweep:

- **Mine** — every currently-open PR authored by the resolved handle (see Settings).
- **Someone else's** — a handle field lights up; sweep that user's open PRs.
- **Specific PR** — a PR-number field lights up; do just that one.

For each PR it merges the latest `origin/main` into the branch. **Clean merges are left
untouched** - only where the merge *conflicts* does it resolve every conflict and push the
merge commit back to the PR's remote branch. The contextual field (handle / PR number)
appears only for the target it applies to.

> Preview the assembled prompt without launching anything:
> ```bash
> ARGENT_UTILS_PRINT_PROMPT=conflicts-mine swift run ArgentUtils   # also: =conflicts-user, =conflicts-single
> ```

## Actions - Full E2E test

The third card spawns a whole-repo audit: a swarm end-to-end tests every module,
flow, build and test in the target repo, hard-reproducing every finding before
reporting it (prompt model in `core/audit.json`). By default the run only finds
and reports - nothing is changed. Two escalation toggles widen the blast radius:
**Also fix open bug issues** (reproduce + fix the repo's open BUG issues - never
feature requests) and **Open PRs for every finding** (a focused PR per confirmed
fix; off = a strictly read-only audit).

> Preview: `ARGENT_UTILS_PRINT_PROMPT=audit swift run ArgentUtils` (also `=audit-issues`, `=audit-prs`, `=audit-all`).

## Argent Mesh (experimental) — LAN P2P duty coordination

With several machines on one desk (say a Linux box and two MacBooks), the
wrench's grunt work shouldn't all land on the laptop you're typing on. **Argent
Mesh** makes the machines coordinate: every node self-discovers its peers over
UDP (multicast + subnet broadcast), holds heartbeat TCP links, and gossips its
status — platform, a user-set machine *tier* (1 = strongest), and token
availability (🟢 ok / 🟡 low / 🔴 out).

On top of that shared view, every node runs the same **deterministic duty
assignment** — no leader, no election, no split-brain: identical inputs give
identical answers everywhere, so the moment a machine dies (heartbeat timeout)
or runs out of tokens, every survivor has *already* agreed where each duty
moved. Duties are the three spawn actions, each with a configurable placement:

- **Review PRs / Resolve conflicts** — default *weakest-first*: route to the
  weakest eligible machine and keep the strong ones free for interactive work.
- **Full E2E test** — a platform *spread*: one Linux node **and** one macOS
  node run the bundle E2E, each slot failing over within its platform.

Dispatching routes a staged prompt to the chosen node over the mesh; the
receiving machine opens its own terminal running `claude` exactly like a local
SPAWN AGENT (dispatches are the `📤/📥 mesh` rows in the activity feed). If the
first target declines — gone, or out of tokens — the dispatch fails over to the
next candidate by rank.

The Linux applet grows a collapsible **topology column**: the live node graph
(link states), per-node tier/token editors (editing a *remote* node forwards
over the mesh, so one panel configures the whole fleet), and per-duty strategy
+ token-awareness controls (gossiped last-writer-wins). The mesh node itself is
stdlib-only Python — the MacBooks run it headless until a Swift port exists:

```bash
cd linux
python3 -m argent_utils.mesh --daemon      # join the mesh (any OS, no Qt needed)
python3 -m argent_utils.mesh --status      # live topology + duty assignments
python3 -m argent_utils.mesh --set tokens=out          # "this machine is out of tokens"
python3 -m argent_utils.mesh --dispatch review --prompt "…"   # route a job
```

Model + constants live in [`core/mesh.json`](core/mesh.json); node state in
`~/.argent/mesh/` (`node.json` identity, `state.json` topology snapshot — the
device-allocator pattern).

**Trust model.** The mesh is meant for a LAN you control (IPv4; discovery is
multicast + subnet broadcast). By default it's open — any machine on the network
that speaks the protocol can join and receive dispatched jobs. On a shared
office network, set the same `ARGENT_MESH_SECRET=<token>` on every machine (and
in the applet's environment): a node with a secret refuses peers, control
sessions, and dispatches that don't present the matching token. It's a join
fence, not cryptography — the token rides plaintext on the LAN — so it keeps a
stray machine or a colleague's mesh from joining yours; it does not defend
against a hostile network.

## Autonomous monitors (macOS)

The macOS applet doesn't just render lists - it acts on them. Three background
monitors ship **ON by default** (opt out in Settings). Know what that means
before running it: they **spawn real terminal windows** running `claude` agents,
and the auto-fix agents **push to your PR branches**. Those background windows
open **without stealing focus** - a monitor spawn (and the API-error nudge) opens
the terminal behind whatever you're working in and bounces focus straight back;
only a spawn *you* trigger (SPAWN AGENT, a panel button) brings the terminal
forward.

- **PR auto-fix** - polls my open PRs every 3 minutes, plus immediately on wake
  from sleep and on toggle-enable. A PR that turns CONFLICTING gets a
  Resolve-conflicts agent; one carrying review threads I owe a reply on gets a
  fix-on-branch review agent. It's a level-triggered reconciler, not just an
  edge-trigger diff: a conflict or review that already existed when the monitor
  first looked (landed overnight, spawn failed, window closed) still gets an
  agent - deduped by in-flight sessions plus an exponential retry backoff
  (5m → 10m → … → 3h) that survives applet restarts.
- **Review requests** - polls PRs requesting *my* review and dispatches the most
  thorough review the wizard can express: Full E2E ×2 depth, formal per-line
  comments, hands strictly off the branch. "Owed" comes from GitHub's own
  timestamps (request newer than my last review), so a genuine re-request
  re-qualifies, and a review left unaddressed (agent died, window closed) is
  retried on the same 5m→3h backoff until the review actually lands. Force-push
  dedup: a push re-stamps the review request, which would double-spawn - a new
  request within 1h of a dispatch is treated as churn and suppressed. Banned
  authors are never auto-reviewed.
- **Claude API-error watcher** - every ~20s reads each iTerm/Terminal session's
  visible tail; an agent stalled on a transient API error (overloads, connection
  failures) gets a continue nudge typed into that exact session, with a per-tty
  2m → 3h backoff so a persistently broken session isn't hammered.

Poll failures (gh / auth / network) surface in Settings and the activity log
rather than silently freezing stale counts. Rate-limit note: the GitHub GraphQL
budget (5000 points/hr) is shared with the agent swarm and these searches aren't
cheap - the 3-minute cadence is deliberate; responsiveness comes from the
immediate poll on wake/enable, not from a tight loop.

### Auto-approvals (default OFF)

Whether an auto-dispatched review may *ever* submit a verdict (approve / request
changes) on my behalf is a master toggle in Settings - **default OFF**, so every
auto-review leaves inline comments only and the final call stays with me. When
opted in, three independent suppressors (each default ON) still withhold the
verdict for a PR that touches a SKILL, touches the installer/CLI, or comes from a
community author (outside `trustedAssociations` in `core/filters.json`) - those
classes stay comments-only even with approvals enabled.

## Settings

The header **⚙︎** button (next to ↻ and ⏻) swaps the panel to a settings screen:

- **GitHub username** - override the handle used by the "My …" tools, the wizards
  and the monitors. Blank = the `gh`-authenticated user (`viewer.login`), resolved
  eagerly at launch so it's the default everywhere.
- **Auto-fix my PRs / Full-E2E review requests** - the two monitor toggles, with
  live status: PRs watched, conflicts/reviews handled, "N unaddressed reviews -
  retrying", and any poll failure. Nested under them, the **auto-approve** master
  toggle and its three withhold-the-verdict suppressors (SKILL / installer /
  community).
- **Auto-continue agents on API errors** - the terminal watcher toggle, plus a
  count of nudges sent.
- **Tools - color & visibility** - a **color well** to retint each tool plus a switch
  to hide it; hidden tools drop out of the grid and the reverse-lookup checklist.
- **Spawn terminal** - which terminal SPAWN AGENT opens: **iTerm** or **Terminal**
  (iTerm is the default when installed, Terminal the always-present fallback).
- **Device allocator (MCP)** - install/uninstall the bundled allocator daemon +
  MCP server (see `device-allocator/`), with install and daemon status.

All of it persists across launches (UserDefaults, `com.ignacy.argent-utils`).

### Definitions / heuristics (where it's deliberately loose)

- **"only open"** — all PR tools query `states: OPEN`; the issues tool queries open issues.
- **"ready for review for >10 days"** — `isDraft == false` and the last
  `ReadyForReviewEvent` (or `createdAt` if it was opened ready) is older than 10 days.
- **"member of the SWM org"** — derived from GitHub `authorAssociation`
  (`MEMBER`/`OWNER` = org; anything else = external). Reliable without org-admin API access.
- **"unaddressed"** (issues) — no comment from a `MEMBER`/`OWNER`/`COLLABORATOR` **and** no assignee.
- **"mine"** — authored by the authenticated `gh` user (`viewer.login`).
- **"approved"** — GitHub's aggregate `reviewDecision == APPROVED`.
- **"unaddressed review"** — a `reviewThread` where `viewerCanResolve` (so it *can* be
  marked resolved) is true, `isResolved` is false, and the **last** comment isn't yours —
  i.e. a reviewer pinged and you neither replied nor resolved it.

All of these constants are data-driven from [`core/filters.json`](core/filters.json) -
retune them there and every front-end picks them up. (The Swift `Filters` shim lives
in `Sources/ArgentUtilsCore/Models.swift`.)

### Auto-refresh

The tool data refreshes every **5 minutes**. Override the interval (seconds, min 5)
for tuning/testing:

```bash
ARGENT_UTILS_REFRESH_SECS=30 open ./ArgentUtils.app   # refresh every 30s
```

This is the tool-data cadence only - the [autonomous monitors](#autonomous-monitors-macos)
poll GitHub on their own 3-minute schedule.

## Run

```bash
cd ~/dev/argent-utils-applet
swift run ArgentUtils    # launches the menu-bar app (no Dock icon)
```

> The package now has two executables (the app + a Linux-buildable
> `ArgentUtilsCoreSmoke` core self-test), so name the target: `swift run ArgentUtils`.

Quit from the panel's ⏻ button, or `pkill ArgentUtils`.

**On Linux?** See [`linux/README.md`](linux/README.md) — `cd linux && ./argent-utils`.

**First run from a terminal** (`swift run`, interactive TTY) offers to set itself up
as a login daemon:

```
┌─ ArgentUtils setup ─────────────────────────────────────────
│ Install as a background daemon? This will:
│   • build + copy ArgentUtils.app to /Applications
│   • add a per-user LaunchAgent so the wrench boots on login
│   • start it now (it replaces this foreground instance)
│   • ask macOS for permission to control your terminal (SPAWN)
└──────────────────────────────────────────────────────────────
Accept [y/N]
```

Accept and it runs `install-autostart.sh` for you (and the daemon takes over via the
newest-wins singleton). The prompt is skipped when launched non-interactively
(`open`, launchd) or once already installed. On first launch it also pokes the
chosen terminal once so macOS shows the *"control iTerm/Terminal"* permission prompt
up front, instead of on your first SPAWN.

### Double-clickable applet (recommended)

```bash
./scripts/build-app.sh     # produces ./ArgentUtils.app (menu-bar-only, no Dock icon)
open ./ArgentUtils.app
```

Drag `ArgentUtils.app` into `/Applications` and add it under
System Settings → General → Login Items — or just use the autostart script below.

### Autostart on login

```bash
./scripts/install-autostart.sh     # installs to /Applications + a login LaunchAgent, starts it now
./scripts/uninstall-autostart.sh   # removes the LaunchAgent and stops the app
```

Installs a per-user LaunchAgent at `~/Library/LaunchAgents/com.ignacy.argent-utils.plist`
(`RunAtLoad`), so the wrench reappears on every login. The ⏻ Quit button still works
within a session (no `KeepAlive`) — it just returns next login.

### Headless self-test

Every mode runs the real pipeline once, prints, and exits - none of them start
the monitors or touch a terminal (except `TRACK_TEST`, whose point is exactly that):

```bash
ARGENT_UTILS_DUMP=1 swift run ArgentUtils            # real fetch+filter pipeline, prints all 6 tools, exits
ARGENT_UTILS_LOOKUP=337 swift run ArgentUtils        # reverse-lookup one number through the real Store
ARGENT_UTILS_PRINT_PROMPT=mine swift run ArgentUtils # assemble + print a prompt: mine|user|single (append
                                                     #   -final for the verdict pass), conflicts[-user|-single],
                                                     #   audit[-issues|-prs|-all]
ARGENT_UTILS_SETTINGS_DUMP=1 ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # resolved persisted settings
ARGENT_UTILS_RENDER=panel    ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # snapshot a screen to PNG (out
                                                     #   path: ARGENT_UTILS_RENDER_OUT). States: panel|panel-procs
                                                     #   natural|settings|settings-live|approved|unban-confirm
                                                     #   wizard[-other|-specific|-wrong|-banned]|devices[-open]
                                                     #   conflicts[-other|-specific|-wrong]|audit[-issues|-prs|-all]
ARGENT_UTILS_TRACK_TEST=1    ...                     # E2E of session tracking via a real throwaway terminal
                                                     #   window; exits non-zero on failure
ARGENT_UTILS_DEVICE_DUMP=1   ...                     # device-allocator paths + daemon state, printed
ARGENT_UTILS_AUTOFIX_POLL=1  ...                     # one real monitor poll: prints its dispatch decisions and
                                                     #   the exact prompts it would spawn, opens nothing
ARGENT_UTILS_APIWATCH_SCAN=1 ...                     # dry-run the API-error watcher over live sessions, sends nothing

# The shared core itself is independently buildable & testable (also on Linux):
swift run ArgentUtilsCoreSmoke                        # loads core/, runs filter + prompt + golden-file assertions
ARGENT_UTILS_DUMP=1 swift run ArgentUtilsCoreSmoke    # + live gh dump, cross-checks the Linux front-end
ARGENT_GOLDEN_WRITE=1 swift run ArgentUtilsCoreSmoke  # regenerate core/golden-prompts/ after an intentional change
```

The `SETTINGS_DUMP` / `RENDER` checks read UserDefaults, so run them through the
`.app` bundle's binary (it shares the GUI's `com.ignacy.argent-utils` domain).

## Requirements

- macOS 13+ (uses SwiftUI `MenuBarExtra`) — or Linux via the [Qt6 applet](linux/README.md)
- Swift toolchain (`swift build`)
- GitHub CLI `gh`, authenticated (`gh auth login`)

## Architecture

The triage logic is single-sourced in [`core/`](core/README.md) - language-neutral
GraphQL queries, the tool catalog, filter constants, and the prompt fragments for
all three actions. Both front-ends load it and assert their assembled prompts
byte-for-byte against `core/golden-prompts/`, so they can only drift from each
other by failing a CI job. The monitors, session tracking, and the audit/ban UI
are macOS-only.

```
core/                          ← shared source of truth (see core/README.md)
  golden-prompts/                canonical prompt outputs, asserted byte-for-byte by BOTH platforms' tests
device-allocator/              ← Node MCP server + daemon arbitrating simulator/emulator allocation between
                                 the agents on this machine (request/await/free/change/broken + repair; leases
                                 persist across daemon restarts, idle devices reclaimed after 15 min; a
                                 prompt-injection report bans the author and terminates the reporting agent)
Sources/
  ArgentUtilsCore/             ← Foundation-only Swift; loads core/. Builds on macOS AND Linux.
    CoreAssets.swift             resolves + decodes core/ (config, catalog, filters, review, conflicts, audit, graphql)
    GH.swift                     gh CLI shell-out (GraphQL via core/graphql)
    Models.swift                 domain models, Filters, Fmt, API
    ToolKind.swift               tool catalog enum + DisplayItem/LookupResult + pure ToolData engine
    Review.swift                 ReviewDepth + ReviewConfig prompt builder + VerdictPolicy (core/review.json)
    Conflict.swift / Audit.swift ConflictConfig + AuditConfig prompt builders (core/conflicts.json, core/audit.json)
    PRRef.swift / PRTarget.swift single-PR reference parsing + the whose-PRs axis shared by the wizards
    Autofix.swift                PRSnapshot + the monitor's edge-trigger diff
    ReviewReconcile.swift        pure retry/backoff/dedup decisions for the monitors
    AgentActivity.swift          terminal-tail classification: running vs awaiting input
    ApiErrorMatch.swift          "is this a Claude API error?" matcher for the watcher
  ArgentUtils/                 ← macOS SwiftUI app — thin UI over the core
    ArgentUtilsApp.swift         @main app + MenuBarExtra + the headless self-test entry points
    Headless.swift               the single "are we a one-shot self-test?" env-var list
    ContentView.swift            two-column panel (left: monitoring lists, right: grid + wizards/results)
    Components.swift             shared UI atoms (cards, chips, badges)
    ReviewWizard.swift           Review-PRs wizard + AgentSpawner (staged prompt file, done sentinel, iTerm/Terminal)
    ConflictWizard.swift / AuditWizard.swift   the Resolve-conflicts and Full-E2E-test wizards
    SettingsView.swift           settings (username, monitors + auto-approve, watcher, tools, terminal, allocator)
    Store.swift                  ObservableObject; settings + the monitor/watcher loops; logic in ToolData
    AutofixMonitor.swift         the monitors' GitHub reads (monitor-prs / review-requests queries)
    AutofixStatus.swift          the monitor heartbeat behind the status pill
    ApiErrorWatcher.swift        iTerm/Terminal session reader + continue-nudge sender
    ProcessTracker.swift         tracked agent sessions (liveness, focus, done sentinel, merged)
    TrackTest.swift              E2E self-test of the tracking path (ARGENT_UTILS_TRACK_TEST)
    BanList.swift / AuditLog.swift   ban list (the daemon's banned.json) + the unified activity feed (audit.jsonl)
    DeviceAllocator.swift        allocator daemon state reader + installer bridge
    DeviceFocus.swift            click an in-use device → focus the holding agent's terminal
    Daemon.swift                 first-run login-daemon opt-in (TTY Accept [y/N])
    Render.swift                 headless ImageRenderer snapshots for UI checks
    Color+Hex.swift              Color ↔ "#RRGGBB" for persisted tint overrides
  ArgentUtilsCoreSmoke/        ← Linux-buildable core self-test (filters + prompts + golden files + live dump)
linux/                         ← Linux Qt6/PySide6 tray applet (see linux/README.md)
  argent_utils/mesh/           ← Argent Mesh node: stdlib-only Python (runs headless on macOS too) — LAN
                                 discovery, heartbeat links, gossip, deterministic duty assignment,
                                 dispatch with failover; model in core/mesh.json, state in ~/.argent/mesh/
```
