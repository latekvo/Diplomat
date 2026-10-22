# Diplomat (Szpont Yon)

<img width="1146" height="904" alt="The Diplomat panel" src="https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02" />

A menu-bar (macOS) and system-tray (Linux) app that keeps a GitHub repo's review
and fix queue moving by running coding agents for you. It watches
`software-mansion/argent` through your `gh` login, and opens each agent in a terminal
window you can watch and type into.

Hacky on purpose, and built for one maintainer's workflow rather than the public.

## What it does

**On its own** (on by default):

- **Reviews PRs that request your review.** It runs a full-depth review and leaves
  inline comments. It never approves or requests changes unless you opt in.
- **Keeps your own PRs mergeable.** It resolves merge conflicts and works through
  reviewers' threads, then pushes to your branch.
- **Unsticks agents.** An agent stalled on a transient Claude API error gets a
  "continue" typed into its session.

**When you ask** (one card each in the panel):

- **Review PRs** - one PR, or every PR by a given author.
- **Fix issues** - reproduce, fix, and open a draft PR per issue.
- **Resolve conflicts** - merge `main` into a set of PRs and fix whatever conflicts.
- **Full E2E test** - a swarm audits the whole repo and reports findings ranked by
  severity, optionally with a draft PR for each.

**Around that:**

- **Triage lists** - six live lists, such as *My Approved PRs*, *My Unaddressed
  Reviews* and *Stale Ready >10d*, plus a search box that tells you which lists a
  PR/issue number is on.
- **A queue you control** - automatic work waits for a free slot (two at a time by
  default). You can reorder it, start an item now or cancel it.
- **A spending brake** - automatic work pauses when your rate-limit window or credit
  balance gets too low to afford another task, and resumes when it refills.
- **Telemetry** - cost per task, rate limit left, backlog and throughput over time.
- **One device per agent** - a bundled MCP server hands each agent its own
  simulator/emulator, so parallel agents never share one.
- **Mesh** *(experimental, off)* - several of your machines split the work between
  them, over the LAN or across networks.

## ⚠️ Billing and usage - read this first

- **Under the Claude Code runner, use only usage-based Anthropic API billing.** That
  means an API key from the [Anthropic Console](https://console.anthropic.com),
  governed by the [Commercial Terms](https://www.anthropic.com/legal/commercial-terms)
  and set as `ANTHROPIC_API_KEY`. Claude Code prefers it over a logged-in plan.
- **Never use it with a personal Claude Free / Pro / Max plan**
  ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)). Diplomat runs
  unattended background agents, and that is service-style use. The same applies to
  any provider: a plan sold for interactive personal use is the wrong place for it.
- **Under OpenCode or Hermes, your provider's bill and terms apply.** Diplomat never
  holds that credential.
- **No warranty, no liability.** The software is provided as is. You alone are
  responsible for your spend, for the commits and pushes agents make, and for
  complying with your provider's terms.

## Install

```bash
npx szpont                       # or:  pip install szpont && szpont
```

This works on macOS 13+ and Linux. It clones Diplomat into `~/.diplomat/checkout`,
builds it and starts it. Diplomat runs from that checkout and updates itself in
place. You need **git**, a **Swift toolchain** (Xcode on macOS,
[swiftly](https://swift.org/install) on Linux), **python3** on Linux to run the
applet, and an authenticated [`gh`](https://cli.github.com). `szpont` checks for all
but `gh` and names whichever is missing before it downloads anything.

```bash
szpont --plan          # print what it would do, and do none of it
szpont --no-update     # start the checkout as it stands
DIPLOMAT_SELF_REPO=~/dev/diplomat szpont   # use your own checkout (never pulled)
```

Optional extras: **Node.js** for the device allocator, **python3** for the mesh on
macOS, and **tmux** if you want Ghostty windows or are on Linux.

## Defaults worth knowing

The automatic monitors **open real terminal windows and push to your PR branches**.
Every switch below is in Settings (⚙︎).

| Behaviour | Default |
|---|---|
| Fix my PRs: conflicts and review threads | **on** |
| Review PRs that request me | **on**, from any author (an allowlist narrows it) |
| Submit approve / request-changes verdicts | **off**, a neutral `COMMENT` review |
| Thank-you review (body only) on a perfectly clean PR | **on** |
| Nudge agents stalled on API errors | **on** |
| Automatic agents running at once | **2** (1-16) |
| Hold automatic work when the limit runs low | **on**: 95% confidence; until a task can be priced, keeps 20% of a window or $1 |
| Give up on an automatic run after 4 h | **on** |
| Agent runner | **Claude Code** (or OpenCode, Hermes) |
| Mesh | **off** |

Agents use your full `gh` login unless you give them
[a narrow token](docs/settings.md#a-narrow-github-token-for-agents) limited to
contents and pull requests. That is worth doing.

## Running from a checkout

```bash
# macOS - a menu-bar app, no Dock icon
cd packages/diplomat-platform/macos
./install/build-app.sh && open ./Diplomat.app
./install/install-autostart.sh   # start at login, update daily at 06:00, restart after a crash

# Linux - a Qt6 tray applet (XFCE, KDE, GNOME with AppIndicator)
cd packages/diplomat-platform/linux
pip install -r requirements.txt && ./install/build-core.sh && ./diplomat
```

Linux has the same feature set, minus the per-row Merge button and clickable agent
rows. See [its README](packages/diplomat-platform/linux/README.md).

## Learn more

| Topic | |
|---|---|
| [The panel](docs/panel.md) | the six lists, reverse lookup, the activity feed, the Agent-tasks queue |
| [Actions](docs/actions.md) | every option of the four wizards |
| [Automatic work](docs/automation.md) | the monitors, the task cap, when a run counts as done, the spending budget |
| [Settings](docs/settings.md) | every setting, agent runners, the narrow GitHub token, list definitions |
| [Telemetry](docs/telemetry.md) | what each figure means and where it comes from |
| [Mesh](docs/mesh.md) | multi-machine coordination, WAN transports, the trust model |
| [Development](docs/development.md) | design, headless self-tests, CI, repository layout |

## How it's built

Both apps are thin UIs over one shared core. GraphQL queries, filters and prompt
fragments live once in [`assets/`](packages/diplomat-core/assets/README.md), and
golden-prompt tests on both platforms fail CI if their prompts drift apart.

| Package | What it is |
|---|---|
| [`diplomat-core`](packages/diplomat-core) | the shared brain: assets, plus Foundation-only Swift that builds on macOS and Linux |
| [`diplomat-runtime`](packages/diplomat-runtime/README.md) | its Python twin, everything below the UI, with no Qt or AppKit |
| [`diplomat-platform`](packages/diplomat-platform) | the macOS SwiftUI app and the Linux PySide6 applet |
| [`device-allocator`](packages/device-allocator/README.md) | the one-device-per-agent MCP server and its daemon |
| [`szpontnet-core`](packages/szpontnet-core/README.md) / [`szpontnet-spec`](packages/szpontnet-spec/docs/README.md) | the mesh protocol: a stdlib-only reference node, the spec and a conformance suite |
| [`szpont`](packages/szpont/README.md) / [`szpont-npm`](packages/szpont-npm/README.md) | the `szpont` launcher on PyPI and npm |
