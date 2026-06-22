# Argent Utils

A tiny macOS **menu-bar applet** — a personal dashboard of Argent-repo triage
tools. Click the wrench in the menu bar, get a dense panel with six utilities.
Hacky on purpose, optimized for *me*, not the public.

Targets `software-mansion/argent` and shells out to the authenticated `gh` CLI.

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

Below the tool grid sits an inline **Review PRs** wizard. Expand it, dial in a few
choices, and hit **SPAWN AGENT** — it opens a fresh **iTerm2** window running
`claude "<prompt>"` in `~/dev/argent`, a detached review session you watch and
steer yourself. The choices are baked into the prompt:

- **Target** — *My PRs* (the resolved handle, see Settings) or *someone else's* (any handle).
- **Scope** — *Review draft PRs* and *Review ready-for-review PRs* (both on by default).
  Untick **both** and a PR-number field lights up: review exactly one PR.
- **Review depth** — a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E with a second double-pass verification.
- **Mark clean PRs ready for review** — *(my PRs only)* flip perfectly-clean drafts to ready.
- **Leave reviews** — *(others' PRs only)* post formal per-line reviews.
- **Reply to others' review threads** — *(my PRs only)* answer and resolve open threads.

The action checkboxes grey out where they don't apply, so the prompt only ever asks
for actions that make sense for whose PRs you're reviewing.

> Preview the exact assembled prompt without launching anything:
> ```bash
> ARGENT_UTILS_PRINT_PROMPT=mine swift run   # also: =user (someone else's), =single (one PR)
> ```

## Settings

The header **⚙︎** button (next to ↻ and ⏻) swaps the panel to a settings screen:

- **GitHub username** — override the handle used by the "My …" tools and the Review
  wizard. Blank = the `gh`-authenticated user (`viewer.login`).
- **Visible tools** — a switch per tool to hide the ones you don't use; hidden tools
  drop out of the grid and the reverse-lookup checklist.

Both persist across launches (UserDefaults, `com.ignacy.argent-utils`).

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
swift run            # launches the menu-bar app (no Dock icon)
```

Quit from the panel's ⏻ button, or `pkill ArgentUtils`.

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
ARGENT_UTILS_DUMP=1 swift run            # real fetch+filter pipeline, prints all 6 tools, exits
ARGENT_UTILS_LOOKUP=337 swift run        # reverse-lookup one number through the real Store
ARGENT_UTILS_PRINT_PROMPT=mine swift run # assemble + print a Review-PRs prompt (mine|user|single)
ARGENT_UTILS_SETTINGS_DUMP=1 ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # resolved persisted settings
ARGENT_UTILS_RENDER=settings ./ArgentUtils.app/Contents/MacOS/ArgentUtils  # snapshot a screen to PNG (settings|wizard|panel)
```

The `SETTINGS_DUMP` / `RENDER` checks read UserDefaults, so run them through the
`.app` bundle's binary (it shares the GUI's `com.ignacy.argent-utils` domain).

## Requirements

- macOS 13+ (uses SwiftUI `MenuBarExtra`)
- Swift toolchain (`swift build`)
- GitHub CLI `gh`, authenticated (`gh auth login`)

## Layout

```
Sources/ArgentUtils/
  ArgentUtilsApp.swift   @main app + MenuBarExtra + headless dump/prompt/render modes
  ContentView.swift      SwiftUI panel (tool grid + actions panel + result rows)
  ReviewWizard.swift     Review-PRs wizard, prompt builder, iTerm2 spawner
  SettingsView.swift     settings screen (username override + tool visibility)
  Render.swift           headless ImageRenderer snapshots for UI checks
  Store.swift            ObservableObject, ToolKind metadata, settings, row mapping
  Models.swift           domain models, GraphQL queries, Filters, formatting
  GH.swift               gh CLI shell-out (GraphQL)
```
