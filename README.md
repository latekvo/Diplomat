# Diplomat (Szpont Yon)

**Diplomat automates the maintainer's job.** It runs a team of coding agents against your GitHub
repo, around the clock: they review PRs, keep your branches mergeable, reproduce and fix incoming
issues, and act on what people ask you on Slack. You decide; they do the legwork.

<img alt="The Diplomat panel" src="docs/img/panel.png" />

## Why

| | |
|---|---|
| **Time** | A review is written before you open the PR. Conflicts and review threads on your own PRs are fixed and pushed. A new issue from outside the team is reproduced and, if it is real, comes back as a draft PR. A Slack "can you look at #412?" is queued before you have read it. |
| **Money** | Every task is priced from your real usage, and automatic work holds before your rate limit or credit runs dry. Telemetry shows what each task costs. Work spreads over the machines and devices you already have. |
| **Power** | One maintainer, a swarm: agents in parallel, each with its own simulator; full-repo audits ranked by severity; goals that span dozens of PRs and issues tracked as one, with one click on whatever is still missing. |

## How it fits together

Every source of work feeds one queue. One gate decides what runs, and everything that runs is
tracked.

```mermaid
flowchart TB
  GH["GitHub monitors<br/>review requests · your PRs · new issues"] --> Q
  SL["Comms<br/>Slack asks, triaged"] --> Q
  TK["Goals<br/>issues → PRs → blockers"] --> Q
  YOU["You<br/>panel · wizards"] --> Q
  Q["One queue · priority bands"] --> GATE{"Budget gate + task cap"}
  GATE --> AG["Agents in real terminals<br/>a simulator each · spread over your machines"]
  AG --> OUT["Reviews · pushes · draft PRs"]
  AG --> TEL["Telemetry<br/>cost · throughput"]
  OUT -. "new commits, threads" .-> GH
  OUT -. "progress" .-> TK
  TEL -. "prices the next task" .-> GATE
```

### Goals - the whole picture, kept moving

Five issues asking for iPhone Duo support, the PR adding it, three PRs unblocking that PR: Diplomat
spots them and proposes **one goal**, drawn as flat chains - issue → PR → the PRs it waits on, and a
`+` where an issue has no PR yet - one click on it starts the fix. **Dismiss** the goal,
**Observe** it and act by hand, or **Automate** it: issues without a PR get a fix, PRs can be
re-reviewed on every new commit, and review threads on yours are fixed. Any long-running task works
the same way, its PRs, issues and Slack threads followed and its status explained.
[More](docs/tasks.md)

<img alt="Goals on the Tasks screen" src="docs/img/goals.png" />

### Comms - Slack, handled before you read it

A DM, an @-mention or a keyword becomes a ping. Triage turns a request into work: a review or fix is
queued, queued work is bumped, a bug report gets an agent reproducing it, and a sender trying to
hijack an agent is banned. [More](docs/comms.md)

<img alt="Comms" src="docs/img/comms.png" />

### Telemetry and the budget gate - spend you can see

Cost and rate limit per task, what is left of the window, backlog and throughput. The same figures
drive the gate that holds automatic work before the limit runs out. [More](docs/telemetry.md)

<img alt="Telemetry" src="docs/img/telemetry.png" />

### Devices - one simulator per agent

A bundled MCP server gives every agent a simulator or emulator of its own, so parallel agents never
share one, and can lend them devices from a remote pool. [More](docs/devices.md)

<img alt="Devices" src="docs/img/devices.png" />

### Mesh - every machine you own

Experimental and off by default: several machines split the work between them over the LAN or across
networks, with no leader to lose. [More](docs/mesh.md)

<img alt="Mesh" src="docs/img/mesh.png" />

## Billing

Under the Claude Code runner, use usage-based Anthropic API billing only: an `ANTHROPIC_API_KEY` from
the [Anthropic Console](https://console.anthropic.com), under the
[Commercial Terms](https://www.anthropic.com/legal/commercial-terms). Don't use a personal Claude
Free, Pro or Max plan ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)): Diplomat
runs unattended background agents, which is service-style use. The same goes for any provider whose
plan is sold for interactive personal use. Under OpenCode or Hermes, your provider's bill and terms
apply; Diplomat never holds that credential.

There's no warranty. You're responsible for what you spend, for the commits and pushes the agents
make, and for following your provider's terms.

## Install

```bash
npx szpont                       # or:  pip install szpont && szpont
```

macOS 13+ and Linux. It clones Diplomat into `~/.diplomat/checkout`, builds it, starts it, and
updates it in place. You need git, a Swift toolchain (Xcode on macOS,
[swiftly](https://swift.org/install) on Linux), python3 on Linux, and a logged-in
[`gh`](https://cli.github.com). Optional: Node.js for the device allocator, python3 for the mesh on
macOS, tmux for Ghostty windows or on Linux.

```bash
szpont --plan          # print what it would do, and do none of it
szpont --no-update     # start the checkout as it stands
DIPLOMAT_SELF_REPO=~/dev/diplomat szpont   # use your own checkout (never pulled)
```

<details>
<summary><b>Defaults</b> - the monitors open real terminal windows and push to your PR branches</summary>

Everything here can be changed in Settings (⚙︎).

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
[a narrower token](docs/settings.md#a-narrow-github-token-for-agents) limited to contents and pull
requests, which is safer.
</details>

<details>
<summary><b>Running from a checkout</b></summary>

```bash
# macOS
cd packages/diplomat-platform/macos
./install/build-app.sh && open ./Diplomat.app
./install/install-autostart.sh   # start at login, update daily at 06:00, restart after a crash

# Linux - a Qt6 desktop app (a tray icon too, where the desktop has a tray)
cd packages/diplomat-platform/linux
pip install -r requirements.txt && ./install/build-core.sh && ./diplomat
```

Linux has everything except the per-row Merge button and clickable agent rows. See
[its README](packages/diplomat-platform/linux/README.md).
</details>

<details>
<summary><b>Layout</b></summary>

Both apps are thin UIs over one shared core. GraphQL queries, filters and prompt fragments live in
[`assets/`](packages/diplomat-core/assets/README.md), and golden-prompt tests on both platforms fail
CI if the prompts drift apart.

| Package | |
|---|---|
| [`diplomat-core`](packages/diplomat-core) | the shared assets, plus Foundation-only Swift that builds on macOS and Linux |
| [`diplomat-runtime`](packages/diplomat-runtime/README.md) | the Python side of everything below the UI, with no Qt or AppKit |
| [`diplomat-platform`](packages/diplomat-platform) | the macOS SwiftUI app and the Linux PySide6 applet |
| [`device-allocator`](packages/device-allocator/README.md) | the one-device-per-agent MCP server and its daemon |
| [`remote-machines`](packages/remote-machines/README.md) | the MCP server that lists the machines an agent can offload work to over SSH, and their live load |
| [`daemon-core`](packages/daemon-core/README.md) | what those two share: the socket daemon, its client, and their install into Claude Code |
| [`szpontnet-core`](packages/szpontnet-core/README.md) / [`szpontnet-spec`](packages/szpontnet-spec/docs/README.md) | the mesh protocol: a stdlib-only reference node, the spec and a conformance suite |
| [`szpont`](packages/szpont/README.md) / [`szpont-npm`](packages/szpont-npm/README.md) | the `szpont` launcher on PyPI and npm |
</details>

## Docs

[The panel](docs/panel.md) · [Actions](docs/actions.md) · [Automatic work](docs/automation.md) ·
[Settings](docs/settings.md) · [Telemetry](docs/telemetry.md) · [Comms](docs/comms.md) ·
[Tasks](docs/tasks.md) · [Devices](docs/devices.md) · [Mesh](docs/mesh.md) ·
[Development](docs/development.md)
