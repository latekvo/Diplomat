# Argent Utils

A tiny **menu-bar / system-tray applet** — a personal dashboard of Argent-repo
triage tools. Click the wrench, get a dense panel with six utilities. Hacky on
purpose, optimized for *me*, not the public.

Targets `software-mansion/argent` and shells out to the authenticated `gh` CLI.

> **Two front-ends, one brain.** The macOS SwiftUI app and the
> [Linux Qt6/PySide6 applet](linux/README.md) are thin UI renderers over a shared,
> language-neutral [`core/`](core/README.md) (GraphQL queries, tool catalog, filter
> constants, review-prompt fragments). All the triage logic lives there once, so the
> two platforms can never drift — change a query or threshold in one place and both
> pick it up. See **[Architecture](#architecture)** below.

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
moment you click the wrench — even if the panel was never open.

**Reverse lookup:** type a PR/issue number in the search box (press **⌘F** to jump to
it) and it instantly shows which of the six lists that number is on — a ✓/— checklist
plus what the number is (open PR/issue, author, draft/ready). Cache-only, so it reacts
as you type. Launch with `ARGENT_UTILS_PREFILL=<n>` to open pre-focused on a number.

## Actions — Review PRs

The grid carries a **Review PRs** card alongside the tools. Click it and the wizard
opens where the PR lists normally render; dial in a few choices and hit **SPAWN
AGENT** — it opens a fresh terminal window (iTerm if installed, else Terminal)
running `claude "<prompt>"` in `~/dev/argent`, a detached review session you watch
and steer yourself. The choices are baked into the prompt:

- **Target** — *My PRs* (the resolved handle, see Settings) or *someone else's* (any handle).
- **Scope** — *Review draft PRs* and *Review ready-for-review PRs* (both on by default).
  Untick **both** and a PR-number field lights up: review exactly one PR.
- **Review depth** — a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E with a second double-pass verification.
- **Mark clean PRs ready for review** — *(my PRs only)* flip perfectly-clean drafts to ready.
- **Leave reviews** — *(others' PRs only)* post formal per-line reviews.
- **Reply to others' review threads** — *(my PRs only)* answer and resolve open threads.
- **✨ Final E2E pass + verdict** — *(highlighted, off by default)* appends a culminating
  full-E2E pass on the real binaries with big swarms: APPROVE perfectly-clean PRs
  (after confirming past issues are resolved), APPROVE-with-nitpicks when there are
  only minor asks, or leave **changes requested** on real blockers.

Contextual controls (the action checkboxes, the someone-else's handle field, and the
single-PR field) **appear only where they apply** — they animate in and out as you
change the target/scope, so the prompt only ever offers what makes sense.

> Preview the exact assembled prompt without launching anything:
> ```bash
> ARGENT_UTILS_PRINT_PROMPT=mine swift run ArgentUtils   # also: =user (someone else's), =single (one PR)
> ```

## Settings

The header **⚙︎** button (next to ↻ and ⏻) swaps the panel to a settings screen:

- **GitHub username** — override the handle used by the "My …" tools and the Review
  wizard. Blank = the `gh`-authenticated user (`viewer.login`), resolved eagerly at
  launch so it's the default everywhere.
- **Tools — color & visibility** — a **color well** to retint each tool plus a switch
  to hide it; hidden tools drop out of the grid and the reverse-lookup checklist.
- **Spawn terminal** — which terminal SPAWN AGENT opens: **iTerm** or **Terminal**
  (iTerm is the default when installed, Terminal the always-present fallback).

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

Tweak any of these in `Filters` (`Sources/ArgentUtils/Models.swift`).

### Auto-refresh

Refreshes every **5 minutes**. Override the interval (seconds, min 5) for tuning/testing:

```bash
ARGENT_UTILS_REFRESH_SECS=30 open ./ArgentUtils.app   # refresh every 30s
```

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

```bash
ARGENT_UTILS_DUMP=1 swift run ArgentUtils            # real fetch+filter pipeline, prints all 6 tools, exits
ARGENT_UTILS_LOOKUP=337 swift run ArgentUtils        # reverse-lookup one number through the real Store
ARGENT_UTILS_PRINT_PROMPT=mine swift run ArgentUtils # assemble + print a Review-PRs prompt (mine|user|single)
ARGENT_UTILS_SETTINGS_DUMP=1 ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # resolved persisted settings
ARGENT_UTILS_RENDER=settings ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # snapshot a screen to PNG (settings|wizard|panel)

# The shared core itself is independently buildable & testable (also on Linux):
swift run ArgentUtilsCoreSmoke                        # loads core/, runs filters + prompt assertions
ARGENT_UTILS_DUMP=1 swift run ArgentUtilsCoreSmoke    # + live gh dump, cross-checks the Linux front-end
```

The `SETTINGS_DUMP` / `RENDER` checks read UserDefaults, so run them through the
`.app` bundle's binary (it shares the GUI's `com.ignacy.argent-utils` domain).

## Requirements

- macOS 13+ (uses SwiftUI `MenuBarExtra`) — or Linux via the [Qt6 applet](linux/README.md)
- Swift toolchain (`swift build`)
- GitHub CLI `gh`, authenticated (`gh auth login`)

## Architecture

The triage logic is single-sourced in [`core/`](core/README.md) — language-neutral
GraphQL queries, the tool catalog, filter constants, and the review-prompt
fragments. Both front-ends load it and only differ in rendering:

```
core/                          ← shared source of truth (see core/README.md)
Sources/
  ArgentUtilsCore/             ← Foundation-only Swift; loads core/. Builds on macOS AND Linux.
    CoreAssets.swift             resolves + decodes core/ (config, catalog, filters, review, graphql)
    GH.swift                     gh CLI shell-out (GraphQL via core/graphql)
    Models.swift                 domain models, Filters, Fmt, API
    Review.swift                 ReviewDepth + ReviewConfig prompt builder (from core/review.json)
    ToolKind.swift               tool catalog enum + DisplayItem/LookupResult + pure ToolData engine
  ArgentUtils/                 ← macOS SwiftUI app — thin UI over the core
    ArgentUtilsApp.swift         @main app + MenuBarExtra + headless dump/prompt/render modes
    ContentView.swift            SwiftUI panel (tool + Review-PRs grid, result rows)
    ReviewWizard.swift           Review-PRs wizard (SwiftUI) + iTerm/Terminal spawner
    SettingsView.swift           settings screen (username, tool color/visibility, terminal)
    Store.swift                  ObservableObject; tints + settings; delegates logic to ToolData
    Color+Hex.swift              Color ↔ "#RRGGBB" for persisted tint overrides
    Daemon.swift                 first-run login-daemon opt-in (TTY Accept [y/N])
    Render.swift                 headless ImageRenderer snapshots for UI checks
  ArgentUtilsCoreSmoke/        ← Linux-buildable core self-test (filters + prompt + live dump)
linux/                         ← Linux Qt6/PySide6 tray applet (see linux/README.md)
```
