# Diplomat (Szpont Yon)

<img width="1146" height="904" alt="The Diplomat panel" src="https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02" />

Diplomat lives in the macOS menu bar or the Linux system tray and runs coding agents
against a GitHub repo's review and fix queue. It watches `software-mansion/argent`
through your `gh` login. Each agent opens in a terminal window, so you can watch it
and type into it.

It's a personal tool, built around one maintainer's workflow, and it's hacky.

## What it does

Left alone, it:

- reviews PRs that request your review, at full depth, with inline comments. It
  doesn't approve or request changes unless you turn that on;
- keeps your own PRs mergeable: it resolves merge conflicts, works through review
  threads and pushes to your branch;
- reproduces every new issue filed from outside the org, and opens a draft PR with
  the fix when it reproduces;
- types "continue" into an agent stuck on a transient Claude API error;
- watches your Slack through Claude Code's Slack login: a DM, an @-mention or a keyword becomes
  a ping on its own screen, and a request in it is acted on - a review or fix is
  queued, queued work is bumped, and a bug report gets an agent investigating it
  before you have read the message. See [Comms](docs/comms.md).

On request, from the panel:

- review one PR, or every PR by an author;
- fix issues: reproduce each one, fix it and open a draft PR;
- merge `main` into a set of PRs and resolve the conflicts;
- run a full E2E test, where a swarm audits the whole repo and ranks what it finds
  by severity, optionally with a draft PR per finding.

The panel also has six triage lists (*My Approved PRs*, *My Unaddressed Reviews*,
*Stale Ready >10d* and so on) and a search box that tells you which lists a PR or
issue is on. Automatic work queues for a free slot, two at a time by default; you
can reorder the queue, start an item now, or cancel work you asked for. When your
rate-limit window or credit balance is too low to pay for another task, automatic
work pauses until the window refills or you top up. A Telemetry card shows cost per
task, rate limit left, backlog and throughput.

A bundled MCP server gives each agent its own simulator or emulator, so parallel
agents never share a device; a [Devices screen](docs/devices.md) shows who holds what and can
lend agents devices from a remote pool. There's also an experimental mesh, off by default, that
splits the work between several of your machines over the LAN or across networks.

## Billing

Under the Claude Code runner, use usage-based Anthropic API billing only: an API key
from the [Anthropic Console](https://console.anthropic.com), set as
`ANTHROPIC_API_KEY` and covered by the
[Commercial Terms](https://www.anthropic.com/legal/commercial-terms). Claude Code
prefers it over a logged-in plan.

Don't use it with a personal Claude Free, Pro or Max plan
([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)). Diplomat runs
unattended background agents, which is service-style use. The same goes for any
provider whose plan is sold for interactive personal use.

Under OpenCode or Hermes, your provider's bill and terms apply. Diplomat never holds
that credential.

There's no warranty. You're responsible for what you spend, for the commits and
pushes the agents make, and for following your provider's terms.

## Install

```bash
npx szpont                       # or:  pip install szpont && szpont
```

Works on macOS 13+ and Linux. It clones Diplomat into `~/.diplomat/checkout`, builds
it and starts it. Diplomat runs from that checkout and updates itself in place.

You need git, a Swift toolchain (Xcode on macOS, [swiftly](https://swift.org/install)
on Linux), python3 on Linux, and a logged-in [`gh`](https://cli.github.com). `szpont`
checks for everything except `gh` before downloading anything.

```bash
szpont --plan          # print what it would do, and do none of it
szpont --no-update     # start the checkout as it stands
DIPLOMAT_SELF_REPO=~/dev/diplomat szpont   # use your own checkout (never pulled)
```

Optional: Node.js for the device allocator, python3 for the mesh on macOS, and tmux
for Ghostty windows or on Linux.

## Defaults

The automatic monitors open real terminal windows and push to your PR branches.
Everything below can be changed in Settings (⚙︎).

| Behaviour | Default |
|---|---|
| Fix my PRs: conflicts and review threads | **on** |
| Review PRs that request me | **on**, from any author (an allowlist narrows it) |
| Submit approve / request-changes verdicts | **off**, a neutral `COMMENT` review |
| Thank-you review (body only) on a perfectly clean PR | **on** |
| Reproduce and fix new community issues | **on**, issues filed from then on |
| Nudge agents stalled on API errors | **on** |
| Automatic agents running at once | **2** (1-16) |
| Hold automatic work when the limit runs low | **on**: 95% confidence; until a task can be priced, keeps 20% of a window (or $1 of credit once Hermes has billed a task) |
| Give up on an automatic run after 4 h | **on** |
| Agent runner | **Claude Code** (or OpenCode, Hermes) |
| Mesh | **off** |
| Comms (Slack watcher) | **off** |

Agents use your full `gh` login unless you give them
[a narrower token](docs/settings.md#a-narrow-github-token-for-agents) limited to
contents and pull requests, which is safer.

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

Linux has everything except the per-row Merge button and clickable agent rows. See
[its README](packages/diplomat-platform/linux/README.md).

## Docs

- [The panel](docs/panel.md): the lists, reverse lookup, the activity feed, the
  agent-tasks queue
- [Actions](docs/actions.md): every option of the four wizards
- [Automatic work](docs/automation.md): the monitors, the task cap, when a run
  counts as done, the spending budget
- [Settings](docs/settings.md): every setting, agent runners, the narrow GitHub
  token, list definitions
- [Telemetry](docs/telemetry.md): what each figure means and where it comes from
- [Mesh](docs/mesh.md): multi-machine coordination, WAN transports, the trust model
- [Comms](docs/comms.md): the Slack watcher - what it reads, how triage decides, what
  it may do, the Slack login it borrows
- [Development](docs/development.md): design, headless self-tests, CI, repo layout

## Layout

Both apps are thin UIs over one shared core. GraphQL queries, filters and prompt
fragments live in [`assets/`](packages/diplomat-core/assets/README.md), and
golden-prompt tests on both platforms fail CI if the prompts drift apart.

| Package | |
|---|---|
| [`diplomat-core`](packages/diplomat-core) | the shared assets, plus Foundation-only Swift that builds on macOS and Linux |
| [`diplomat-runtime`](packages/diplomat-runtime/README.md) | the Python side of everything below the UI, with no Qt or AppKit |
| [`diplomat-platform`](packages/diplomat-platform) | the macOS SwiftUI app and the Linux PySide6 applet |
| [`device-allocator`](packages/device-allocator/README.md) | the one-device-per-agent MCP server and its daemon |
| [`szpontnet-core`](packages/szpontnet-core/README.md) / [`szpontnet-spec`](packages/szpontnet-spec/docs/README.md) | the mesh protocol: a stdlib-only reference node, the spec and a conformance suite |
| [`szpont`](packages/szpont/README.md) / [`szpont-npm`](packages/szpont-npm/README.md) | the `szpont` launcher on PyPI and npm |
