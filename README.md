# Argent Utils

A tiny macOS **menu-bar applet** — a personal dashboard of Argent-repo triage
tools. Click the wrench in the menu bar, get a dense panel with four utilities.
Hacky on purpose, optimized for *me*, not the public.

Targets `software-mansion/argent` and shells out to the authenticated `gh` CLI.

## The library

| Icon | Tool | What it lists |
|------|------|---------------|
| 📕 (purple) | **SKILL.md PRs** | open PRs touching any `SKILL.md` |
| 📦 (orange) | **Installer/CLI PRs** | open PRs touching `packages/argent-installer/` or `packages/argent-cli/` |
| ⏳ (red) | **Stale Ready >10d** | non-draft PRs that have been ready-for-review for over 10 days |
| 💬 (teal) | **Unaddressed Issues** | open issues **not** opened by an SWM org member that have no team reply and no assignee |

Every row is clickable → opens the PR/issue in your browser. Counts show on each
card; hit ↻ to refresh, ⏻ to quit.

### Definitions / heuristics (where it's deliberately loose)

- **"only open"** — all PR tools query `states: OPEN`; the issues tool queries open issues.
- **"ready for review for >10 days"** — `isDraft == false` and the last
  `ReadyForReviewEvent` (or `createdAt` if it was opened ready) is older than 10 days.
- **"member of the SWM org"** — derived from GitHub `authorAssociation`
  (`MEMBER`/`OWNER` = org; anything else = external). Reliable without org-admin API access.
- **"unaddressed"** — no comment from a `MEMBER`/`OWNER`/`COLLABORATOR` **and** no assignee.

Tweak any of these in `Filters` (`Sources/ArgentUtils/Models.swift`).

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
System Settings → General → Login Items to keep the wrench in your menu bar.

### Headless self-test

```bash
ARGENT_UTILS_DUMP=1 swift run     # runs the real fetch+filter pipeline, prints all 4 tools, exits
```

## Requirements

- macOS 13+ (uses SwiftUI `MenuBarExtra`)
- Swift toolchain (`swift build`)
- GitHub CLI `gh`, authenticated (`gh auth login`)

## Layout

```
Sources/ArgentUtils/
  ArgentUtilsApp.swift   @main app + MenuBarExtra + headless dump mode
  ContentView.swift      SwiftUI panel (tool grid + dense result rows)
  Store.swift            ObservableObject, ToolKind metadata, row mapping
  Models.swift           domain models, GraphQL queries, Filters, formatting
  GH.swift               gh CLI shell-out (GraphQL)
```
