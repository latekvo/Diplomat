# Diplomat (Szpont Yon)

<img width="1146" height="904" alt="image" src="https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02" />

## TL;DR:

- Auto reviews PRs which have you listed as reviewer
- Auto fix your own PRs once they get a review
- Auto resolve all conflicts on your PRs
- Enforces one device per agent
- Manually review all PRs of the given person
- `npx szpont` (or `pip install szpont`) installs and starts it — [Install](#install)

## Details

A tiny **menu-bar / system-tray applet** - a personal dashboard of Argent-repo
triage tools. Click the wrench, get a dense two-column panel with six utilities,
three spawn-an-agent actions (Review PRs, Resolve conflicts, Full E2E test) and
a set of [autonomous monitors](#autonomous-monitors) that spawn
those agents without being asked. Hacky on purpose, optimized for *me*, not the
public.

Targets `software-mansion/argent` and shells out to the authenticated `gh` CLI.

> **Two front-ends, one brain.** The macOS SwiftUI app and the
> [Linux Qt6/PySide6 applet](packages/diplomat-platform/linux/README.md) are thin UI renderers over a shared,
> language-neutral [`assets/`](packages/diplomat-core/assets/README.md): the GraphQL queries, tool catalog,
> filter constants and prompt fragments are single-sourced, and golden-prompt tests
> on both sides fail CI on prompt drift. Both applets run the full autonomous
> monitor stack. See **[Architecture](#architecture)**.
>
> **One pipeline, two triggers.** A wizard's SPAWN button and an auto-monitor's
> poll tick are two *triggers* for the very same dispatch pipeline
> (`Store.dispatchAgent` / `store.dispatch_agent`): the ban check, in-flight
> dedup, mesh coordination, spawn, run registration, and counters live in exactly
> one place per platform.
> The only trigger asymmetries are the documented ones in `AgentDispatchGate`
> (its Python twin `autofix.dispatch_decide`): manual spawns come to the
> foreground (macOS), are never mesh-gated and never hit the
> [automatic-task cap](#autonomous-monitors); monitor dispatches get the
> `Auto · … · retry N` label, and only they bump the auto-handled counters.
> Parity tests on both sides pin that matrix. A job arriving *over the mesh* is
> the one spawn that bypasses the pipeline - it lands through the mesh node's own
> runner, and the untracked-agent scan is what re-attaches it afterwards.
>
> **One answer about what the agents are doing.** Whether a PR is in flight, how
> many bays of the device's [task cap](#autonomous-monitors) are full, which rows
> the panel draws and which record is retired are four *projections* of a single
> resolved tick (`AgentState` / `agentstate.py`), not four derivations that can
> drift apart. Evidence reaches it typed - each probe answers *present*,
> *unavailable* or *unsupported* - and the ladder never reads "I could not look"
> as "it is gone": a run ends only on positive evidence (its sentinel, its pid
> missing from a process table that was actually read, or its mesh claim
> released), and anything else resolves to `unknown`, which keeps its slot and
> says so. A run is identified by its agent's own pid, written into
> `~/.diplomat/agents/<run-id>/pid` by the shell that then `exec`s the agent, and
> the book survives a restart on both platforms. `DIPLOMAT_AGENTS=1 python -m
> diplomat_app` prints the whole chain: every record, every probe's raw answer,
> every verdict and the one fact that decided it.

## ⚠️ Billing and usage - read this first

> **Under the Claude Code runner, this software may be used _only_ with usage-based
> Anthropic API billing** - an **Anthropic API key** from the
> [Anthropic Console](https://console.anthropic.com), metered per token and governed
> by Anthropic's
> [**Commercial Terms of Service**](https://www.anthropic.com/legal/commercial-terms).
> Point the spawned agents at it via `ANTHROPIC_API_KEY` (Claude Code prefers an API
> key in the environment over any logged-in subscription).
>
> **It may _not_ be used with a personal Claude subscription plan** - the consumer
> **Claude Free / Pro / Max** plans, governed by Anthropic's
> [**Consumer Terms of Service**](https://www.anthropic.com/legal/consumer-terms).
> Diplomat exists to spawn **automated, unattended agents** - background monitors that
> open agent windows and push to your branches with no human in the loop. That is
> programmatic / headless / service-style use, which belongs on the API, not on a
> personal subscription.
>
> **Under the [OpenCode or Hermes runner](#agent-runner), the bill and the terms are
> whichever provider you connected** - OpenRouter, an Anthropic API key, a hosted
> Ollama, a model on your own machine. Diplomat does not hold that credential and
> cannot see what it is charged; read the terms of the provider you pick. It counts
> those runs' *tokens* like any other, and counts them against no limit at all - the
> rate-limit figures on the telemetry screen are the Anthropic account's, and only
> tasks that ran on Claude Code are measured against them.
> The unattended-use point above is about
> *automation*, not about Anthropic specifically, so it applies whatever you run: a
> plan sold for interactive personal use is the wrong place for a background monitor.
>
> **No warranty, no responsibility.** This software is provided **"as is"**, without
> warranty of any kind. **The author accepts no responsibility and no liability** for
> anything arising from its use - API charges, subscription or account actions, commits
> and pushes made to your repositories, or agents spawned on your machines. **You alone
> are responsible** for your API key and your spend, and for ensuring your use complies
> with Anthropic's applicable terms.

## Install

```bash
npx szpont                       # or:  pip install szpont && szpont
```

macOS and Linux, and the same steps either way: clone this repository into
`~/.diplomat/checkout` if it isn't already there, build the front-end for the
platform, start the applet. Every step of it runs a script already in this
repository, one a human would otherwise type; nothing is packaged, because
Diplomat is built out of the checkout it runs from and [updates itself](#settings)
as one.

You need **git**, a **Swift toolchain** (Xcode on macOS, [swiftly](https://swift.org/install)
on Linux) and an authenticated [`gh`](https://cli.github.com). If one is missing,
`szpont` names it and points at it before anything is downloaded.

```bash
szpont --plan          # print what it would do, as JSON, and do none of it
szpont --no-update     # start the checkout as it stands, without fast-forwarding it
szpont -- --dump       # everything after -- is passed to the applet
```

Already have a checkout? `DIPLOMAT_SELF_REPO=~/dev/diplomat szpont` starts *that*
one, and never pulls it — a working copy may have work in it. Or skip the launcher
entirely and [run it from the checkout](#run) as before.

The two packages are the same launcher published under one name to two indexes
([`packages/szpont`](packages/szpont/README.md),
[`packages/szpont-npm`](packages/szpont-npm/README.md)); a parity test holds them to
the same plan on every machine shape either can meet. Neither installs anything but
the launcher - one file of standard library on each side.

## The library

| Tool | What it lists |
|------|---------------|
| **Stale Ready >10d** | non-draft PRs that have been ready-for-review for over 10 days |
| **Unaddressed Issues** | open issues **not** opened by an SWM org member that have no team reply and no assignee |
| **My Approved PRs** | *your* open PRs whose review decision is `APPROVED` |
| **My Unaddressed Reviews** | *your* open PRs with a review thread that's resolvable, unresolved, and that you haven't replied to |

The first two ship **hidden** on both platforms - they're the niche ones; unhide
them under Settings → *Tools - color & visibility*.

Every row is clickable → opens the PR/issue in your browser. Counts show on each
card; hit ↻ to refresh, ⏻ to quit (with a confirmation prompt). The data also
**auto-refreshes every 5 minutes** in the background, so the counts are fresh the
moment you click the wrench - even if the panel was never open.

**Reverse lookup:** type a PR/issue number in the search box (press **⌘F** to jump to
it) and it instantly shows which of the six lists that number is on - a ✓/— checklist
plus what the number is (open PR/issue, author, draft/ready). Cache-only, so it reacts
as you type. Launch with `DIPLOMAT_PREFILL=<n>` to open pre-focused on a number.

## The panel

Two columns. **Right:** everything interactive - the search box, the tool grid
(six tools + three action cards), and whatever a card opens: a tool's result
list, the reverse lookup, or an action wizard. On **My Approved PRs** each row
carries a **Merge** (squash) button - or **Resolve conflicts** when GitHub
reports the PR conflicting, which spawns the fix agent for exactly that PR.

**Left:** the monitoring surfaces. The monitor **status pill** shows whenever a
monitor is enabled (heartbeat: PRs watched, conflicts/reviews handled; "offline"
when polling stops for 15 minutes). The rest appear only when non-empty: the
**banned authors** list (prompt-injection bans, with the captured evidence and an
inline un-ban); the collapsible **agent tasks** list (below); the **devices**
pool (who holds which simulator/emulator, for how long, with a per-device kill -
clicking an in-use device focuses the holding agent's terminal); and the
**activity** log, one unified audit feed
(`~/.diplomat/pr-monitor/audit.jsonl`) of panel actions, monitor dispatches,
nudges, and daemon-side bans.

The activity feed is **filterable in place**: it heads a row of per-category chips
with counts - Reviews · Replies · Conflicts · Audit · API restart · Out of quota ·
Merges · Bans · Mesh · System - and tapping one mutes that category and drops its
rows. The taxonomy (which raw action verb maps to which category, plus its icon and
tint) is shared in [`assets/audit-categories.json`](packages/diplomat-core/assets/audit-categories.json), with
`packages/diplomat-core/Sources/DiplomatCore/AuditCategory.swift` as the Swift source of truth.

### Agent tasks

One list for what the machine is doing, what it is about to do, and how much room
it has left - the agents it has **spawned**, the automatic work it is **holding**,
and an empty bay per free slot of its [task cap](#autonomous-monitors). Rows read
in status order: *merged*, *done*, *awaiting input*, *running*, *starting*,
*free slot*, *queued*, so finished work (the only kind asking to be read) sits at
the top and everything not started yet is at the bottom. The list is always there:
an idle machine with a cap of two reads `0 · 2 free` over two empty bays.

- **Sessions** are every spawned agent, wizard- or monitor-launched. Click a row
  to focus its terminal window, ✕ to stop tracking it.
- **Mesh rows** are the work this machine originated and the mesh is running
  somewhere else, reading *running · on mesh @node*. Same row, same label, same
  place in the list - there is just no window here to click, because the agent is
  on that node. The row lives as long as the executor's
  [work claim](#autonomous-monitors) does, and leaves when the remote agent
  finishes.

  (Both kinds are macOS only - a Linux spawn is a detached `Popen` with no window
  handle to track a session by. Linux draws a local agent as a row all the same,
  from the in-flight book and a `ps` scan: the bay it took, labelled with the work
  it is on and how long it has been going, but with no window to click and no
  *done* to tell apart - that one needs to see the window close. *Awaiting input*
  it does read, off the agent's own tmux pane. One the mesh placed on a peer shows
  in the activity feed rather than on the list.)
- **Starting** is a task between the queue and its agent: the click (or the drain)
  has taken it, and the spawn - a `ps` scan, a mesh placement, a terminal - has not
  answered yet. Seconds, and a row for all of them, so *execute now* never reads as
  the click deleting the task. It holds a bay from the moment it starts, and the
  session or mesh row that replaces it takes its place in the list.
- **Free slots** are the rest of the cap. Each running automatic agent takes one, a
  review from a sweep included - the queue picked the moment for it, so it holds a
  bay like anything else that waited there. An agent a wizard press opened on the
  spot takes none, and neither does work the mesh placed on another machine - that
  spends the peer's capacity, not yours. Queued work starts here on the next poll.
- **Auto-execute queue** is the switch at the top of the queue (**default on**): off,
  nothing starts by itself and every row waits for *execute now*. The monitors keep
  finding work and queueing it either way. It stays on screen while it is off even
  with the list empty - otherwise the state that empties the list would be the one
  you cannot leave.
- **Queued** rows are auto-fixes, auto-reviews and reviews you asked for that
  nothing has started yet. Each
  carries **execute now** - start it immediately, past whatever is holding it -
  and a drag grip: drop a row on another to set the order the queue runs in. That
  order is honoured at the top of the next poll, *before* the monitors go looking
  for more work, so a slot that just freed goes to whatever you put first rather
  than to whichever PR GitHub happened to list first.
- **Requested reviews** - one row per PR of a Review-PRs sweep - run after every
  auto-fix and auto-review and before the conflict fixes, because what the monitors
  find is a debt other people can see and a sweep is work you started when you had
  the time for it. Each also carries **cancel**: you asked for it, so nothing else
  will ever take it off the list.
- **Resolve-conflicts** rows run last of all, whatever
  order the monitors found them in, and no drag lifts one out of its band (that
  drag is refused rather than sprung back on the next poll). An agent working the
  same branch lands its own merge on the way, so a conflict fix is the work most
  often made unnecessary by the work ahead of it - and the poll re-offers it for
  as long as GitHub still calls the PR conflicting, so waiting costs it nothing.

Four things hold work. The cap holds what there is no slot for, and releases it as
slots free. The **rate-limit budget** (below) holds everything when the account is
too low to afford another agent, and releases it when a window refills. A
**monitor you switched off** holds its own work indefinitely: it keeps polling and
keeps listing what it finds (reading *queued · monitor off*), but nothing starts by
itself - only *execute now* does. So the toggles decide who starts the work, not
whether you get to see it, and turning both off does not stop the 3-minute GitHub
poll. **Auto-execute queue** holds every kind at once, the reviews you asked for
included - which is the one thing no monitor toggle speaks for.

### The rate-limit budget

The cap bounds how many automatic agents run at once. The budget bounds whether any
of them should start at all - because a machine can have three empty bays and 4% of
its 5-hour window left, and spending that on an auto-review is how you find the
limit gone the next time you sit down to work.

What a task costs is measured, not guessed. The Telemetry ledger already prices
every finished agent as a share of the window it was spent from (*Limit per task*),
so the question has a statistical answer - and the one worth asking is about the
**next** task, not the average one. Half of all tasks cost more than the mean, and
the distribution is right-skewed (most small, a few enormous), so a gate set at the
mean would wave the expensive tail through every time. Diplomat therefore computes a
one-sided upper **prediction bound**, `mean + z·sd·√(1 + 1/n)`: the cost one more
task will come in under, at the confidence you pick. At the default 95%, roughly one
auto-task in twenty may still overrun what it was gated on.

Both windows gate - a task has to fit inside what is left of the 5-hour one *and*
the 7-day one, since either can be the one that runs out. The weekly figure is the
same tasks rescaled by the ratio of the two calibrations, so the two can never
disagree about how big a task is. What is *left* comes from the live usage probe
rather than the ledger's last sample: samples are 15 minutes apart, and several
agents' worth of spending fits in that gap.

Three knobs, in **Settings → PR AUTO-FIX**, and in `~/.diplomat/config.json` beside
the task cap so the mesh node reads the same ones:

- **Hold automatic work when the rate limit runs low** (`autoBudgetGate`, on) - the
  master switch.
- **Confidence** (`autoBudgetConfidence`, `95`) - 50 / 80 / 90 / 95 / 99. Higher is
  stricter. A hand-edited value the table has no quantile for rounds *up*, never
  down.
- **Kept in hand** (`autoBudgetFloorPct`, `20`) - what a window must still have left
  while the ledger is too thin to price a task. Until at least five auto-tasks have
  finished and been priced, this is the whole of the answer, so a fresh install
  spends down to 20% and no further.

Two failure directions, both deliberate:

- **No reading at all is no opinion.** The usage probe can be off
  (`DIPLOMAT_QUOTA_PROBE=0`), logged out, or simply offline. Nothing is held then -
  a gate that read silence as "no budget" would take a machine's automatic work down
  with the network every time it dropped.
- **A refusal defers, it never drops.** Held work writes no attempt record, so the
  next poll offers it again; it sits in the Agent-tasks list and starts when the
  window refills. *Execute now* overrides the budget exactly as it overrides the
  cap - you are looking at the row and know something the ledger does not. A wizard
  SPAWN that opens a terminal on the spot is never gated at all: spending your own
  last slice of the limit is your call. A review that same wizard queues instead is
  priced like the rest of the queue, because what starting it costs is the same
  whoever wanted it.

The feed carries one `no-budget` line per episode, quoting which window is short and
against what, and another when the next episode begins - not one per PR per poll.
A **mesh peer's job routed here** is declined for the same reason and the slot fails
over: the mesh ranks peers surplus-first, so the node with limit to spend is exactly
the one that picks it up.

The queue is a view of what the monitors would re-offer, not a second copy of
their state: it is rebuilt from live GitHub evidence on every 3-minute poll, so a
task drops out the moment the work is taken by an agent, resolved, or its author
banned. Every poll also re-checks the rows it is about to run against the fetch it
has just made - a conflict fix on a PR GitHub no longer calls conflicting, or a
reply on threads that have been answered, leaves the list instead of opening an
agent on work somebody already did. (Not on a mesh claim: the cap outranks the mesh gate, so a machine with
anything queued is one that never asked a peer - peer-owned work leaves when the
drain reaches it and the mesh answers.) The key order is remembered, so your
arrangement survives the rebuild and a restart.

The one exception is the reviews **you** ask for by sweeping your PRs (below):
nothing on GitHub records that a PR was swept, so those are remembered instead of
re-derived, offered on every poll until each is dispatched, wear your own label
rather than `Auto · `, and carry a **cancel** button beside *execute now* because
short of a ban on the PR's author nothing else will ever retire one. They wait in a
band of their own - behind everything GitHub is already owed (a review requested of
you, a thread waiting on your reply), ahead of the conflict fixes - so sweeping
fifty drafts does not bury a review request behind them for a day.

*Execute now* keeps the task automatic in every other respect:
same label, same auto-handled counter, same mesh routing - the cap and
the rate-limit budget are the two holds it overrides. So a click can land the agent on a peer rather than
here, and what the starting row becomes is a mesh row saying which; it occupies a
slot of this machine's cap only when it actually runs on this machine.

## Actions - Review PRs

The grid carries a **Review PRs** card alongside the tools. Click it and the wizard
opens where the PR lists normally render; dial in a few choices and hit **SPAWN
AGENT**.

A **specific PR** is one agent: a fresh terminal window (iTerm if installed, else
Terminal) running a detached review session in your **repo root** (Settings;
default `~/dev/<repo>`) that you watch and steer yourself. The prompt is staged to
a file and the window runs
`<agent> "$(cat <promptfile>)"; printf %s $? > <done>` - where `<agent>` is the
[agent runner](#agent-runner) you picked, and the trailing sentinel
(under `~/.diplomat/pr-monitor/done/`) is how the Agent-tasks list knows the
agent finished.

A **whose-PRs sweep** is not one agent for all of them. It queues one review per PR
it covers, so the task cap starts them a few at a time and each is a row you can
reorder, run ahead of the rest, or cancel - fifty drafts are fifty reviews, not one
session told to work through fifty. (Which is also why the mesh row is offered for a
single PR only: a sweep opens no session to place.) The choices are baked into each
prompt:

- **Target** — the same three-way selector the other wizards use: *Mine* (the
  resolved handle, see Settings), *Someone else's* (a handle field lights up), or
  *Specific PR* (a number/URL field lights up - review exactly that one).
- **Scope** — *Review draft PRs* and *Review ready-for-review PRs* (both on by
  default; hidden for a specific PR, which is already one exact PR). Untick both
  and SPAWN greys out - there'd be nothing left to review.
- **Review depth** — a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E, swarming until one clean pass.
- **Mark clean PRs ready for review** — *(never on someone else's)* flip
  perfectly-clean drafts to ready.
- **Leave reviews (CLAUDE.md format)** — *(never on my own)* post formal per-line reviews.
- **Reply to others' review threads** — *(never on someone else's)* answer and resolve open threads.
- **✨ Final E2E pass + verdict** - *(highlighted, off by default; others' and
  unknown-author PRs only - never my own, there is no self-approval)* appends a
  culminating full-E2E pass on the real binaries with big swarms: APPROVE
  perfectly-clean PRs (after confirming past issues are resolved),
  APPROVE-with-comments when only LOW findings are left, or leave **changes
  requested** on real blockers.

Contextual controls (the action checkboxes, the someone-else's handle field, and the
single-PR field) **appear only where they apply** - for a specific PR the wizard
polls the author first and hides the toggles that don't fit (mine → fix-on-branch,
theirs → review-only; while the poll is still in flight all four stay offered, and
banned authors get a flashing warning instead).

> Preview the exact assembled prompt without launching anything:
> ```bash
> DIPLOMAT_PRINT_PROMPT=mine swift run Diplomat   # also: =user, =single; append -final for the verdict pass
> ```

## Actions - Resolve conflicts

A second grid card, **Resolve conflicts**, spawns a detached agent the same way
(fresh terminal, staged prompt + done sentinel, in the same repo root from
Settings) but for keeping
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
> DIPLOMAT_PRINT_PROMPT=conflicts-mine swift run Diplomat   # also: =conflicts-user, =conflicts-single
> ```

## Actions - Full E2E test

The third card spawns a whole-repo audit: a swarm end-to-end tests every module,
flow, build and test in the target repo, hard-reproducing every HIGH / MEDIUM
finding before reporting it (prompt model in `assets/audit.json`). Every confirmed
finding is **classified HIGH / MEDIUM / LOW** by real impact, and that label rides
through to the report - that part is always on. The proof scales with the label:
a HIGH or MEDIUM earns the full first-hand device reproduction, a LOW earns one
short adversarial check and never a booted device or a swarm, and a nitpick is
dropped on sight without being verified at all. By default the run only finds
and reports; nothing is changed. Two escalation toggles widen the blast radius:

- **Open PRs for every finding** — one focused PR per fix, **always as a draft**,
  and only after checking the repo's open PRs (by real `gh pr diff` content, not
  titles) so it never files a duplicate. PRs are severity-gated: HIGH and MEDIUM
  always get one, a LOW only when its fix is under 20 lines of diff - anything
  bigger is reported, not PR'd. Off = a strictly read-only audit.
- **Also fix open bug issues** — reproduce + fix the repo's open BUG issues, never
  feature requests.

> Preview: `DIPLOMAT_PRINT_PROMPT=audit swift run Diplomat` (also `=audit-issues`, `=audit-prs`, `=audit-all`).

## Diplomat Mesh (experimental) — LAN P2P duty coordination

> Diplomat Mesh is the reference implementation of **SzpontNet**, a small leaderless
> LAN protocol for self-discovery, resource advertisement, and work hand-off. The
> full, independently-implementable specification (currently **v0.5.0**, wire `v: 1`)
> is in [`szpontnet-spec/docs/`](packages/szpontnet-spec/docs/README.md), and
> [`szpontnet-spec/conformance/`](packages/szpontnet-spec/conformance/README.md) is the black-box
> conformance suite that makes "independently implementable" checkable: it launches
> a candidate node as an opaque subprocess, joins it over real multicast + TCP, and
> exits non-zero if any MUST fails.

With several machines on one desk (say a Linux box and two MacBooks), the
wrench's grunt work shouldn't all land on the laptop you're typing on. **Diplomat
Mesh** makes the machines coordinate: every node self-discovers its peers over
UDP (multicast + subnet broadcast), holds heartbeat TCP links, and gossips its
status — platform, a machine *tier* (1 = strongest, auto-detected from the
hardware CPU-first; editing it pins the value), and token availability
(ok / low / out, tracked from real usage unless you pin that too).

On top of that shared view, every node runs the same **deterministic duty
assignment** — no leader, no election, no split-brain: identical inputs give
identical answers everywhere, so the moment a machine dies (heartbeat timeout)
or runs out of tokens, every survivor has *already* agreed where each duty
moved. Duties are the three spawn actions, each with a configurable placement:

- **Review PRs / Resolve conflicts** — default *surplus-first*: route to the node
  with the most spare quota relative to its reset (with no quota signal this falls
  back to *weakest-first*, keeping the strong machines free for interactive work).
- **Full E2E test** — same surplus-first default, plus a platform **spread**:
  one Linux node **and** one macOS node run the bundle E2E, each slot failing
  over within its platform.

(Strategy and spread are separate placement fields. The strategies are
`weakest-first`, `strongest-first`, `local-first` and `surplus-first` (the
default).)

Dispatching routes a staged prompt to the chosen node over the mesh; the
receiving machine opens its own terminal running its own agent runner exactly like a local
SPAWN AGENT (dispatches are the `📤/📥 mesh` rows in the activity feed). If the
first target declines — gone, or out of tokens — the dispatch fails over to the
next candidate by rank. While the mesh is live, the three wizards grow a
**⬡ Run on mesh** row (checked by default, with a preview of where the duty
currently routes): SPAWN AGENT then hands the job to the node instead of always
opening a local terminal — on both front-ends. The Review wizard offers the row for
a single PR only: a whose-PRs sweep opens no session to place, it queues one review
per PR for this machine's own cap to start.

Both front-ends grow a **Mesh screen** (the ⬡ button in the panel header, beside
[Telemetry](#telemetry) and Settings): the live node graph (link states), per-node tier/token editors (editing
a *remote* node forwards over the mesh, so one panel configures the whole fleet),
per-duty strategy + token-awareness controls (gossiped last-writer-wins), the whole
**trust surface** — the `New devices: Personal / Foreign` default, a one-time
callout when an unknown device shows up, a per-peer trust toggle, and the banned
chip with its un-ban — and **reaching machines off this network**: the mesh's
preferred WAN transport, this machine's id on each transport it runs (copyable), a
paste box that links to another machine's id, and per peer the one transport that
edge agreed on. It shouts `DEVICE IS NOT DISCOVERABLE` if every beacon send fails.

The mesh node itself is stdlib-only Python that runs on any OS — both the macOS app
and the Linux applet drive that same node (a Swift node is future work), so enabling
the mesh on macOS needs the source checkout on disk (`DIPLOMAT_SELF_REPO` if it
isn't at the default `~/dev/diplomat`):

```bash
cd packages/diplomat-runtime
# Put Diplomat behind the node: without this it runs SzpontNet's own defaults —
# the canonical v1 duties, state in ~/.szpontnet, no activity feed — and so joins
# a different mesh than the one your app is on.
# Absolute, because `--daemon` re-execs the node from the library's own directory:
# a relative PYTHONPATH entry would resolve against THAT cwd, the host module would
# not import, and the node would come up on the null host without saying so.
export SZPONTNET_HOST=diplomat_runtime.szponthost
export PYTHONPATH="$PWD:$PWD/../szpontnet-core"

python3 -m szpontnet --daemon      # join the mesh (any OS, no Qt needed)
python3 -m szpontnet --status      # live topology + duty assignments
python3 -m szpontnet --stop        # stop the running node
python3 -m szpontnet --set tokens=out          # also: tier=N name=X duty.<id>=on|off
python3 -m szpontnet --set tier=1 --node <ID>  # edit a REMOTE node over the mesh
python3 -m szpontnet --dispatch review --prompt "…"   # route a job (--prompt-file, --target)
python3 -m szpontnet --claim <KEY>             # origination-dedup lease (spec ch 12)
```

Bare `python3 -m szpontnet` runs a node in the foreground. `--help` lists
the rest (trust/ban management, `--api-key`, `--work-key`).

The canonical model lives in [`szpontnet-core/szpontnet/netmodel.json`](packages/szpontnet-core/szpontnet/netmodel.json)
and Diplomat's overlay - the duty catalog both panels render - in
[`assets/mesh.json`](packages/diplomat-core/assets/mesh.json); node state in
`~/.diplomat/mesh/` (`node.json` identity, `state.json` topology snapshot,
`device.key` + `trusted.json` + `banned.json` for trust, `peers.json` to redial
known peers — the device-allocator pattern; `SZPONTNET_DIR` relocates it).

Every knob the node itself reads is `SZPONTNET_*` — the library owns that
namespace, and the conformance tester configures a candidate through no other
channel. They were `DIPLOMAT_MESH_*` while the node lived inside this app, and the
old spelling is still honoured when the new one is unset, so a shell profile that
sets `DIPLOMAT_MESH_SECRET` keeps its mesh fenced rather than silently opening it.
Diplomat's *own* mesh knobs keep their names (`DIPLOMAT_MESH_POLL_SECS`,
`DIPLOMAT_MESH_CMD_TEST`, `DIPLOMAT_MESH_E2E`): they configure the applet, not the
node.

**Beyond one LAN.** Discovery is link-local, so on its own the mesh stops at the
subnet. A **WAN transport** is what joins several of them: once two nodes have met,
each redials known-but-unseen *personal* peers **by identity, not by address** — so a
desk at home and a desk at the office are one mesh, with no public IP, no domain and
no port forwarding. Either transport is complementary to the LAN rather than a
replacement for it: peers that share a network still find and reach each other by
multicast and direct TCP.

There are two, and a node may run either, both, or neither:

- **Tor** ([spec ch 14](packages/szpontnet-spec/docs/14-tor-transport.md)), **on by
  default**: a permanent v3 onion service, advertised inside the signed advert. A
  node with no `tor` binary installed simply does not run it, and `SZPONTNET_TOR=0`
  turns it off. Paste an address with
  `python3 -m szpontnet --tor-connect <hash>.onion`.
- **iroh** ([spec ch 15](packages/szpontnet-spec/docs/15-iroh-transport.md)), opt-in
  with `SZPONTNET_IROH=1` and the optional package (`pip install 'szpontnet[wan]'`):
  a permanent QUIC endpoint whose id is an Ed25519 public key. It reaches the same
  peers with no daemon, no multi-minute bootstrap and no rendezvous circuit per dial,
  connecting in well under a second. Paste an address with
  `python3 -m szpontnet --iroh-connect <64-hex>`.

Which of the two an edge settles on is a **mesh-wide pick** ([spec ch
6](packages/szpontnet-spec/docs/06-coordination.md#the-preferred-wan-transport)),
gossiped last-writer-wins beside the duty placements and set from the Mesh screen or
with `python3 -m szpontnet --wan iroh`. It **orders, it never excludes**: every node
keeps running every transport it can, so the pick decides an edge only where both
ends speak both, and a peer that advertises only an onion is still reached over Tor.
An edge that shares neither has no way off the LAN at all, and the Mesh screen flags
it. `--connect <id>` takes either address and reads the transport off its shape.

**Trust model.** The mesh is designed around a LAN you control (IPv4; discovery is
multicast + subnet broadcast), and — since the Tor transport is on by default — a
node also publishes an onion that peers can reach it on from anywhere. A WAN address
carries peer links only; the operator's control channel (`stop`, `trust`,
`dispatch`, `set-attr`) is refused on any connection that did not arrive on the LAN.
Note what that leaves: on an **open** mesh (no join secret) a peer holding your onion
or endpoint can link and, subject to trust, exchange gossip and dispatch from the WAN,
where before it would have had to be on your LAN. The two fences below apply
identically over every transport, and turning both transports off (`SZPONTNET_TOR=0`,
`SZPONTNET_IROH` unset) removes the WAN surface entirely.

Two independent fences:

- **Join fence** — set the same `SZPONTNET_SECRET=<token>` on every machine
  (and in the applet's environment): a node with a secret refuses peers, control
  sessions, and dispatches that don't present the matching token. The token rides
  plaintext on the LAN, so it keeps a stray machine or a colleague's mesh from
  joining yours; it does not defend against a hostile network.
- **Authenticated device keys** — every node mints an Ed25519 keypair on first
  run (`~/.diplomat/mesh/device.key`, requires the `cryptography` package; without
  it the node runs *keyless* and can never be verified). A peer must prove
  possession of its key on each link (fresh-nonce signature) before its identity
  counts; advertised names/ids grant nothing. Trust is then a **local allowlist**
  of proven key fingerprints (`~/.diplomat/mesh/trusted.json`, never gossiped),
  and it is **zero-trust by default**: a device you have not explicitly promoted
  is `foreign` no matter how empty the allowlist is. Promote from the Mesh screen
  or the CLI:
  `python3 -m szpontnet --fingerprint` (print this machine's),
  `--trust <FP> [--label <name>]`, `--untrust <FP>`. The baseline itself is a
  per-node knob (`--default-trust personal|foreign`, `SZPONTNET_DEFAULT_TRUST`,
  or `trust.default` in `assets/mesh.json`) — set it to `personal` for the old
  full-altruism behaviour where every unlisted peer is trusted.

There are three trust levels, not two:

| Level | What a request from it does |
|-------|------------------------------|
| `personal` | runs directly, exactly as if you'd triggered it locally |
| `foreign` | **declined** — unless a confinement runner is configured (`SZPONTNET_FOREIGN_SPAWN`), in which case it runs sandboxed and *response-only*: the compute happens here, the result is routed back, and this node never takes a social action on it ([spec ch 13](packages/szpontnet-spec/docs/13-foreign-execution.md)) |
| `banned` | declined outright, even with a confinement runner, and never picked as a dispatch target |

**Foreign accountability.** A foreign device that *accepts* a job takes on a
contract: deliver a result before the completion deadline (6 h by default). Miss it
and the node sends a readiness reminder; an unhelpful or absent answer — judged by
an agent you can point at with `SZPONTNET_EXTEND_DECIDER`, which may grant an
extension instead — earns a **ban**, recorded machine-local in
`~/.diplomat/mesh/banned.json` and never gossiped. Manage bans with
`--ban <FP|ID> [--ban-reason …]` / `--unban`; the macOS Mesh screen surfaces them
as a banned chip with the reason and an inline un-ban.

Nodes also gossip **per-node quota accounting** (plan, a surplus **burn-down ratio**
— budget left ÷ clock left until the quota resets — plus display-only usage-average
and quota-left figures; see `accounts` in `packages/szpontnet-core/szpontnet/netmodel.json`): the default
`surplus-first` ranking sends work to the machine that is most flush *relative to its
reset*, and each executed job books usage on the executor.

## Telemetry

The panel's third screen (the header button between **⬡** and **⚙︎**) answers the
question the monitors otherwise leave open: *what is all this costing, and is it
keeping up?* Eight figures, over a lookback you pick (**7d / 14d / 30d / 60d**,
default 14):

- **Limit per task** - the share of one 5-hour rate-limit window an auto-task
  consumes on average, plus a **bell curve** of how that is distributed, with a 95%
  confidence interval on the mean drawn behind it (the histogram is the spread of
  the tasks; the band is how well the *average* is known, and is much narrower).
- **Rate limit left** - what the usage probe measured to be left of each window,
  drawn where it was sampled rather than resampled onto a grid: the 5-hour one saws
  (it refills on its own cycle), the 7-day one is the slower ceiling. The axis is
  pinned to 0-100% rather than scaled to the data, so "we never dropped below 60%"
  and a week of exhaustion cannot come out as the same picture. A missing reading is
  a probe that could not answer, not an empty window, so the line breaks across one
  instead of diving to the floor and back.
- **Owed work** over time - work the monitors had found but nobody had started yet,
  auto-reviews and conflict fixes stacked into one area on a count axis. Stacked
  because both queue for the same executors, with reviews taking a free slot first:
  the fixes ride on top of the reviews, and the top edge is everything the pool
  owes. Work picked up between two points on the chart never appears as a backlog,
  which is the chart working rather than a gap in it.
- **Time to start** - from the monitor first seeing a unit of work to an agent
  taking it (the reconciler's backoff, an applet that was off, a busy PR).
- **Time to finish** - from an agent starting to its exit.
- **Spent on this repo** - how much of this machine's Claude spend went on the repo
  the agents work in rather than everything else. Split by the `cwd` each turn ran
  in, so it counts your own sessions in that checkout too - it is a repo split, not
  an attribution to Diplomat's agents (that is what *limit per task* measures).

### Where the numbers come from

Five of these are time-series questions a counter cannot answer ("how many were owed
last Tuesday?", "how close to the ceiling did we get?"), so the source is an
**append-only ledger** at
`~/.diplomat/pr-monitor/telemetry.jsonl` - one JSON object per line, opened
`O_APPEND` like the activity feed so this applet, its counterpart on the other OS and
a mesh node can all append to one file. The monitors write `queued` when they first
see work owed, `cleared` if it stops being owed before anyone takes it, `started` on
dispatch, `done` when the agent exited - its completion sentinel's own mtime, or the
last turn of its transcript for a run the mesh placed, which leaves no sentinel this
applet can read, but never when a poll noticed - and a `sample` every 15 minutes. It rewrites itself to the 60-day retention horizon
once it passes 4 MB.

Two gatherers fill in what GitHub doesn't know:

- **Quota** (`quota.py` / `Quota.swift`) - one GET against the OAuth usage endpoint
  with the token Claude Code already holds, i.e. the same data its `/usage` screen
  shows. Deliberately Diplomat's own probe rather than a call into the mesh library:
  the [mesh is an optional add-on](#diplomat-mesh-experimental--lan-p2p-duty-coordination),
  so a screen that reached through it for numbers would blank on exactly the
  machines least likely to have it. `DIPLOMAT_QUOTA_PROBE=0` turns it off.
- **Token attribution** (`usagescan.py` / `UsageScan.swift`) - Claude Code appends
  every turn to `~/.claude/projects/**/*.jsonl` with a `usage` block and the `cwd` it
  ran in, which is enough for both token questions. The repo-vs-everything-else split
  is that `cwd` (the checkout **and** its `-worktrees` siblings); a single task's cost
  is the transcript whose *opening user message is that task's prompt verbatim* - an
  exact identity that needs no new flag on the spawn path. Scanning is incremental
  (a byte offset per file), and the first scan seeds every existing transcript at EOF
  rather than reading gigabytes of history it could never attribute anyway.
  `DIPLOMAT_CLAUDE_DIR` moves where it reads from. A [foreign runner](#agent-runner)
  writes no such transcript, so each is priced from its own store instead - OpenCode
  summed over every message of `opencode export <session>` (it reports a turn's cost
  per message), Hermes read off the running totals on its session row - both counting
  the same three fields, so one ledger holds every runner in one unit.

The probe reports **what is left of each window** on every sample, and that reading is
what *rate limit left* draws - measured, not derived. What Anthropic never publishes is
a **token budget**, and the budget is dynamic - so the figure that cannot be looked up
is the *per-task* share, "4% of the window". The 5-hour window is instead **priced from
what actually happened**: over an interval the
account spent `Δutil` of its window while this machine logged `Δtokens`, so the window
is worth `Δtokens / Δutil`, summed across every interval that didn't span a reset. Until
two quota readings exist there is no price, and the screen says so and shows raw tokens
per task rather than inventing a percentage. Work placed on a **mesh peer** spends that
peer's quota, so it counts as started and is kept out of the cost and run-time figures.

The arithmetic is shared: [`Telemetry.swift`](packages/diplomat-core/Sources/DiplomatCore/Telemetry.swift)
and `diplomat_runtime/telemetry.py` are diffed field-for-field over one ledger by
`tests/test_telemetry_parity.py`, so the two screens cannot disagree about what a
ledger means.

## Autonomous monitors

The applets don't just render lists - they act on them. Three background
monitors ship **ON by default** (opt out in Settings). Know what that means
before running it: they **spawn real terminal windows** running agents,
and the auto-fix agents **push to your PR branches**. Those background windows
open **without stealing focus** - a monitor spawn opens the terminal behind
whatever you're working in and bounces focus straight back; only a spawn *you*
trigger (SPAWN AGENT, a panel button) brings the terminal forward. (The API-error
nudge opens no window at all - it types into a session that already exists.)

- **PR auto-fix** - polls my open PRs every 3 minutes, plus immediately on
  toggle-enable and, on macOS, on wake from sleep. A PR that turns CONFLICTING
  gets a Resolve-conflicts agent; one carrying review threads gets a
  fix-on-branch review agent. It's a level-triggered reconciler, not just an
  edge-trigger diff: a conflict or review that already existed when the monitor
  first looked (landed overnight, spawn failed, window closed) still gets an
  agent - deduped by in-flight sessions plus an exponential retry backoff
  (5m → 10m → … → 3h) that survives applet restarts. Conflicts are *only*
  level-triggered now (the edge event is a deliberate no-op) and retry on the
  plain 3-minute tick; the backoff ladder is the review path's.
- **Review requests** - polls PRs requesting *my* review and dispatches the most
  thorough review the wizard can express: Full E2E · max depth, formal per-line
  comments, hands strictly off the branch. "Owed" comes from GitHub's own
  timestamps (request newer than my last review), so a genuine re-request
  re-qualifies, and a review left unaddressed (agent died, window closed) is
  retried on the same 5m→3h backoff until the review actually lands. Force-push
  dedup: a push re-stamps the review request, which would double-spawn - a new
  request within 1h of a dispatch is treated as churn and suppressed. Banned
  authors are never auto-reviewed.
- **Claude API-error watcher** - Claude Code runs only; the banners it matches are
  Claude Code's, and an OpenCode or Hermes agent that errors reads as idle instead,
  frees its task-cap slot, and is dispatched again by whichever monitor owed the
  work. Every ~20s it reads each agent session's visible
  tail (macOS: any iTerm/Terminal session; Linux: **tmux panes only** - there's no
  portable way to read or type into an arbitrary Linux emulator, so the Linux
  spawner opens each agent in a tmux session of its own and an agent started
  outside one is not watched). An agent stalled on a transient API error
  (overloads, connection failures, bare `429` rate-limits, status-page errors) gets
  a continue nudge typed into that exact session, with a per-session 2m → 3h
  backoff so a persistently broken one isn't hammered. A single erroring scan never
  nudges: the tail must come back **byte-identical on the next scan** before it
  counts as a stall, so the real floor is ~2 scans. An **out-of-quota** banner is
  never nudged - it's not transient - and it suppresses any API error sharing the
  same tail. An org **budget cap** (`403 … budget limit exceeded`) counts as one
  of those, whatever its status code: it holds until the window rolls over or an
  admin raises it.

Poll failures (gh / auth / network) surface in Settings and the activity log
rather than silently freezing stale counts. Rate-limit note: the GitHub GraphQL
budget (5000 points/hr) is shared with the agent swarm and these searches aren't
cheap - the 3-minute cadence is deliberate; responsiveness comes from the
immediate poll on wake/enable, not from a tight loop. Both cadences are
overridable for tuning (`DIPLOMAT_AUTOFIX_SECS`, floor 60s on macOS / 30s on
Linux; `DIPLOMAT_APIWATCH_SECS`, floor 5s).

**At most 2 automatic agents run at once** (Settings; 1-16). Both monitors above
are level-triggered over everything GitHub currently owes, so one poll of a busy
day would otherwise dispatch every pending unit in a single pass - a terminal
window and an agent session per conflicted PR and per owed review, all at the
same moment. The cap is the *machine's*, not a monitor's: it spans both monitors,
the reviews a PR sweep queues, and any work a mesh peer routes here, and it counts
agents that are really running (`ps`, so it survives an applet restart) rather than
a tally that can drift. The agent a wizard press opens on the spot is outside it
and is never refused; what a press leaves in the queue instead - the reviews of a
whose-PRs sweep - is on the cap like every other queued task.

An agent is spawned into an *interactive* session, so finishing its work is not
exiting - it waits at its prompt until someone closes the window, and `ps` shows
it either way. What frees its bay is therefore the terminal, not the process: an
agent whose session shows the CLI back at its prompt reads *awaiting input*, and
gives its slot back while keeping its row. Both front-ends read that the same
way, off the CLI's own status bar (`AgentActivity`), from whatever each platform
can see of a terminal - iTerm/Terminal sessions on macOS, tmux panes on Linux.
(Only positive evidence counts. An agent whose terminal can't be read - one
outside tmux, or a dump that failed - keeps its bay: the failure direction is
deferring work, never doubling up on it. Its PR stays in-flight regardless, since
that session still holds the context, so nothing else is dispatched onto the same
PR.)

Nothing is dropped - work over the cap gets no attempt record, so
the next 3-minute tick offers it again as soon as a bay comes back, and it waits
visibly in the panel's [Agent tasks](#agent-tasks) list meanwhile, where it can be
reordered or started by hand. Whatever is left of the cap shows there too, as an
empty bay per free slot. Saturation shows up as one `at-capacity` row in the
activity feed per episode.

**With a mesh up, each unit of work runs once.** Every machine scans GitHub
independently; whoever finds a unit routes it by work key, and the node that
takes it holds the claim for its agent's lifetime, so a concurrent or repeat scan
elsewhere is suppressed (`mesh-suppressed` in the activity feed) and a node death
frees the work for failover. A machine already at its cap declines what it is
sent, and the dispatcher fails the slot over to one with room. That claim is also
what the machine that *originated* the work watches: it keeps a
[mesh row](#agent-tasks) on its own Agent-tasks list for as long as the claim is
held, so work handed to a peer reads as a task in flight rather than as a task
that disappeared. The best node is sometimes the machine that asked - a placement
that lands back home is an agent here like any other, so it takes one of this
machine's slots from the moment it is placed and is priced against this machine's
quota; only its terminal was opened by the node rather than by the applet.

### Auto-approvals (default OFF)

Whether an auto-dispatched review may *ever* submit a verdict (approve / request
changes) on my behalf is a master toggle in Settings - **default OFF**, so every
auto-review leaves inline comments only and the final call stays with me. When
opted in, three independent suppressors (each default ON) still withhold the
verdict for a PR that touches a SKILL, touches the installer/CLI, or comes from a
community author (outside `trustedAssociations` in `assets/filters.json`) - those
classes stay comments-only even with approvals enabled.

### Soft-approvals (default ON)

A separate toggle governs what a *comments-only* review does when it finds a PR
perfectly clean - **default ON**. Instead of staying silent, the agent leaves a
single friendly top-level comment (*"Ran full E2E sweep... Returned perfectly
clean. Thank you for contributing!"*) and nothing more. A soft-approval is that
comment alone - it **never** carries an `APPROVE` action, so nothing is submitted
on my behalf; it's just an acknowledgement. It's independent of the verdict toggle
above and is moot on any PR that gets a real verdict (that takes precedence). Turn
it off to make clean reviews fully silent again.

## Settings

The header **⚙︎** button (next to ↻, the **⬡** mesh button, the Telemetry button
and ⏻) swaps the panel to a settings screen:

- **GitHub username** - override the handle used by the "My …" tools, the wizards
  and the monitors. Blank = the `gh`-authenticated user (`viewer.login`), resolved
  eagerly at launch so it's the default everywhere.
- <a id="agent-runner"></a>**Agent runner** - which agent CLI a spawn runs:
  **Claude Code** (the default, and what every existing install keeps), **OpenCode**
  or **Hermes**. Only the agent word and its flags change; the prompt, the staged
  file, the completion sentinel, the pid a run is identified by and every monitor
  above it are the same whichever it is, which is the point of having one setting
  rather than a second pipeline. All three are windowed, so a run can be watched and
  typed into. Like the repo root the setting lives in the shared
  `~/.diplomat/config.json`, so a running mesh node picks it up on its next spawn -
  and which runner a given run *started* under is written into its run directory, so
  switching mid-flight can't interrogate a live agent through the wrong store.
  - **Model** (OpenCode, Hermes) - a model id such as
    `openrouter/moonshotai/kimi-k2` or `ollama-cloud/glm-5.2`. Blank leaves the
    choice to that runner's own picker rather than overriding it with a guess.
  - **Connect a provider…** (OpenCode, Hermes) - opens that runner's own login wizard
    in a terminal (`opencode providers login`, `hermes setup`). Diplomat deliberately
    has no API-key field: each runner already knows its whole provider catalog, which
    entries take OAuth rather than a key, and where each one's credentials belong -
    and each writes them to the store its agent reads from anyway. **No provider
    credential is ever stored by Diplomat**, which matters because
    `~/.diplomat/config.json` is world-readable and copied around by the mesh.
  - **How a run is watched.** Both foreign runners are *asked* whether their turn is
    over rather than having it read off their status bar - positive evidence, instead
    of whether someone else's `esc interrupt` hint happened to be drawn when the poll
    looked. They answer from different places. An OpenCode agent is spawned with
    `--port <n>` on a port Diplomat reserved for it, so it serves its own session on
    loopback while it works; the port is unauthenticated (OpenCode's server takes a
    password but its own TUI sends none), so it is reachable by other users of the
    same machine and nothing else. Hermes serves no such port, and needs none: it
    writes every session and message to `~/.hermes/state.db` as it goes, which
    Diplomat opens read-only, and a turn is over exactly when the agent stamps its own
    message `finish_reason` (`tool_calls` is mid-turn, `stop` is the end). Either way
    the session is matched to the run by the staged prompt, which both runners store
    verbatim as the session's opening message - the only exact key, since both keep
    one session store for the whole machine. A run that cannot be reached - the port
    was taken, the server has not come up, the store is not there - falls back to the
    status bar exactly as a Claude Code run does.
  - **How a run is priced.** OpenCode reports a turn's cost per message, so a
    finished run is summed from `opencode export <session>` when it ends, not from the
    poll. Hermes keeps running totals on the session row, so it is simply read. Both
    count input + output + cache *writes*, the same three the Claude Code transcript
    scan sums, so one ledger holds every runner in one unit. What those tokens are
    *not* is a share of a rate-limit window: that window is the Anthropic account's,
    priced from Claude Code's own usage probe, so **limit per task** and the
    [rate-limit budget](#the-rate-limit-budget) count the tasks that ran on Claude Code
    and leave a foreign run to the token figures beside them.
  - What does *not* carry over: the [Claude API-error watcher](#autonomous-monitors)
    - its banners are Claude Code's. A foreign agent that errors reads as idle, so
    it gives its task-cap slot back and the monitor that owed the work dispatches it
    again.
- **Repo root** - the local checkout every spawned agent `cd`s into, with a
  **Choose…** directory picker (type a path if you prefer; a leading `~` expands).
  Blank = `~/dev/<repo>` for whichever repo [`assets/config.json`](packages/diplomat-core/assets/config.json)
  targets. The hint warns when the path isn't absolute, or has no `.git` - the
  spawn's `cd` is best-effort, so an agent would otherwise start in your home
  directory unnoticed. `DIPLOMAT_REPO` still outranks the field, and says so in the
  hint when it's set. Unlike every other setting this one is **not** in UserDefaults:
  a mesh node spawns agents from its own stdlib-only process, so the pick lives in
  the shared `~/.diplomat/config.json` that both front-ends and the node re-read on
  each spawn - change it and a *running* node picks it up.
- **Auto-queue fixes for my PRs / Auto-queue reviews that request me** - the two
  monitor toggles, with live status: PRs watched, reviews done so far, "N
  unaddressed reviews - retrying", and any poll failure. (The combined *fixed N*
  counter lives on the panel's status pill, not here.) A monitor switched off keeps
  polling and keeps listing what it finds under [Agent tasks](#agent-tasks); what
  stops is the automatic start. Nested under the **review-requests** toggle -
  and visible only while it's on - the **auto-approve** master toggle and its
  three withhold-the-verdict suppressors (SKILL / installer / community).
- **Run at most N automatic tasks at a time** - this machine's hard cap on
  concurrent automatic agents (**default 2**, range 1-16), across both monitors,
  the reviews a PR sweep queues, and any work a mesh peer routes here. The agent a
  wizard press opens on the spot is never capped and doesn't count against it; work
  over the cap is deferred to the next poll, not dropped. Like the repo root and for
  the same reason, it lives in the shared `~/.diplomat/config.json` rather than
  UserDefaults - the node that runs peer-routed work is a separate stdlib-only
  process, and a machine with two answers to "how many at once" has no cap at all.
- **Hold automatic work when the rate limit runs low** - the
  [rate-limit budget](#the-rate-limit-budget) (**default on**), with the confidence
  it must reach that a task fits (**default 95%**, one-sided) and the share of a
  window to keep in hand while the ledger cannot price a task yet (**default 20%**).
  Priced from the same per-task figure the [Telemetry](#telemetry) screen shows,
  against both rate-limit windows. Held work waits under
  [Agent tasks](#agent-tasks) and starts when a window refills; *execute now*
  overrides it, a wizard spawn that opens a terminal on the spot is never gated, and
  nothing is held at all while the usage probe cannot read a window. In
  `~/.diplomat/config.json` for the same reason as the cap above.
- **Auto-continue agents on API errors** - the terminal watcher toggle, plus a
  count of nudges sent.
- **Tools - color & visibility** - a **color well** to retint each tool plus a switch
  to hide it; hidden tools drop out of the grid and the reverse-lookup checklist.
- **Spawn terminal** - which terminal SPAWN AGENT opens: **iTerm** or **Terminal**
  (iTerm is the default when installed, Terminal the always-present fallback).
- **Device allocator (MCP)** - install/uninstall the bundled allocator daemon +
  MCP server (see [`packages/device-allocator/README.md`](packages/device-allocator/README.md)), with
  install status, the installed version, and whether it is still current. It
  registers as **`diplomat-device-allocator`**; installing also clears the old
  `argent-device-allocator` registration, so a pre-rename setup migrates itself.
  Both applets install it on first run and refresh it when a `git pull` has moved
  the skill, rule, CLAUDE.md block or registration out from under an installed
  copy - the status then reads **Out of date** and names what drifted. An
  allocator you *uninstall* here stays uninstalled; only an existing install is
  ever refreshed.
- **Mesh (LAN P2P)** - opt into [Diplomat Mesh](#diplomat-mesh-experimental--lan-p2p-duty-coordination):
  a toggle that starts/stops the local node (off by default), with live node/peer
  status. The mesh itself is managed from the **⬡ Mesh screen**.
- **Update** - pull the checkout, rebuild, and relaunch in place. Shows how many
  commits the checkout is behind *and* ahead of upstream, with a ↻ re-check
  button; the button fetches and **merges** (fast-forward when strictly behind, a
  merge commit when you have local commits of your own - `--ff-only` used to refuse
  that), runs `install/build-app.sh`, and reopens the rebuilt app (the newest-wins
  singleton hands over). Uncommitted changes block it outright, and a conflicting
  merge is aborted with "merge by hand" rather than resolved unattended. Needs the
  source checkout on disk (`DIPLOMAT_SELF_REPO`). The same path also runs
  **unattended daily at 06:00** - see [Autostart on login](#autostart-on-login).

All of it persists across launches (UserDefaults, `com.ignacy.diplomat`).

### Definitions / heuristics (where it's deliberately loose)

- **"only open"** — all PR tools query `states: OPEN`; the issues tool queries open issues.
- **"ready for review for >10 days"** — `isDraft == false` and the last
  `ReadyForReviewEvent` (or `createdAt` if it was opened ready) is older than 10 days.
- **"member of the SWM org"** — derived from GitHub `authorAssociation`
  (`MEMBER`/`OWNER` = org; anything else = external). Reliable without org-admin API access.
- **"unaddressed"** (issues) — no comment from a `MEMBER`/`OWNER`/`COLLABORATOR` **and** no assignee.
- **"mine"** — authored by the *effective* handle: the Settings **GitHub username**
  override when set, otherwise the authenticated `gh` user (`viewer.login`).
- **"approved"** — GitHub's aggregate `reviewDecision == APPROVED`.
- **"unaddressed review"** — a `reviewThread` where `viewerCanResolve` (so it *can* be
  marked resolved) is true, `isResolved` is false, and the **last** comment isn't yours —
  i.e. a reviewer pinged and you neither replied nor resolved it.

All of these constants are data-driven from [`assets/filters.json`](packages/diplomat-core/assets/filters.json) -
retune them there and every front-end picks them up. (The Swift `Filters` shim lives
in `packages/diplomat-core/Sources/DiplomatCore/Models.swift`.)

Every definition above is also bounded by the queries' page caps in
[`assets/graphql/`](packages/diplomat-core/assets/graphql): the tools see the 100 newest open PRs (100 files /
50 threads each); the monitors see 30 PRs (40 threads) and 30 review requests, with
only the first 60 changed files - so a PR touching more than 60 files can slip a
SKILL or installer path past the verdict suppressors below.

### Auto-refresh

The tool data refreshes every **5 minutes**. Override the interval (seconds, min 5)
for tuning/testing:

```bash
DIPLOMAT_REFRESH_SECS=30 open ./Diplomat.app   # refresh every 30s
```

Each refresh also re-checks every tracked, unmerged PR (one `gh pr view` apiece) so
the Agent-tasks list can flip a row to *merged*. The [autonomous
monitors](#autonomous-monitors) are separate, on their own 3-minute schedule.

## Run

From a checkout you already have. (No checkout? [`npx szpont`](#install) makes one
and does everything below for you.)

```bash
cd ~/dev/diplomat/packages/diplomat-platform/macos
swift run Diplomat    # launches the menu-bar app (no Dock icon)
```

> The app is its own Swift package; the shared core is the one next door
> (`packages/diplomat-core`), which builds the Linux-buildable `DiplomatCoreSmoke`
> self-test and the `diplomat-core` prompt CLI the Linux front-end shells out to.
> Both packages hold more than one executable, so name the target.

Quit from the panel's ⏻ button, or `pkill Diplomat`.

**On Linux?** See [`packages/diplomat-platform/linux/README.md`](packages/diplomat-platform/linux/README.md)
— `cd packages/diplomat-platform/linux && ./diplomat`.

**First run from a terminal** (`swift run`, interactive TTY) offers to set itself up
as a login daemon:

```
┌─ Diplomat setup ─────────────────────────────────────────
│ Install as a background daemon? This will:
│   • build + copy Diplomat.app to /Applications
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

Everything in this section runs from the macOS package,
`packages/diplomat-platform/macos` — the bundle is built beside it, not at the
repo root.

```bash
./install/build-app.sh     # produces ./Diplomat.app (menu-bar-only, no Dock icon)
open ./Diplomat.app
```

Drag `Diplomat.app` into `/Applications` and add it under
System Settings → General → Login Items — or just use the autostart script below.

### Autostart on login

```bash
./install/install-autostart.sh     # rebuilds, installs the app + both LaunchAgents, starts it now
./install/uninstall-autostart.sh   # removes both LaunchAgents and stops the app
```

Installs a per-user LaunchAgent at `~/Library/LaunchAgents/com.ignacy.diplomat.plist`
(`RunAtLoad`), so the wrench reappears on every login. The ⏻ Quit button still works
within a session (no `KeepAlive`) — it just returns next login. The app goes to
`/Applications`, or `~/Applications` when that isn't writable.

It also installs a **second** agent, `com.ignacy.diplomat.autoupdate`, which fires
daily at **06:00** and runs the app binary headless (`DIPLOMAT_SELF_UPDATE=1`):
merge upstream if behind, rebuild the bundle, and relaunch only if an instance is
running. It's the unattended twin of the Settings **Update** button, and it logs to
`~/Library/Logs/diplomat-autoupdate.err.log`. Manage it on its own with:

```bash
./install/install-autoupdate.sh    # (also called by install-autostart.sh)
./install/uninstall-autoupdate.sh
```

### Headless self-test

Every mode runs the real pipeline once, prints, and exits - none of them start
the monitors or touch a terminal (except `TRACK_TEST` and `SPAWN_FOCUS_TEST`, whose
point is exactly that; and `RENDER=live`, which opens a window and stays up until
you stop it). `packages/diplomat-platform/macos/Sources/Diplomat/Headless.swift` is the one list that
decides what counts as headless:

```bash
DIPLOMAT_DUMP=1 swift run Diplomat            # real fetch+filter pipeline, prints all 6 tools, exits
DIPLOMAT_LOOKUP=337 swift run Diplomat        # reverse-lookup one number through the real Store
DIPLOMAT_PRINT_PROMPT=mine swift run Diplomat # assemble + print a prompt: mine|user|single (append
                                                     #   -final for the verdict pass), conflicts[-user|-single],
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
                                                     #   scroller — pair with DIPLOMAT_POPOVER_CAP=400
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
                                                     #   don't — drives two throwaway windows; exit code = verdict
DIPLOMAT_DEVICE_DUMP=1   ...                     # device-allocator paths + daemon state, printed, plus the
                                                     #   installed version and what (if anything) has drifted
DIPLOMAT_ALLOCATOR_TEST=1 ...                    # the launch-time allocator decision: reinstall a stale copy,
                                                     #   leave an uninstalled one alone. Shells no installer;
                                                     #   exit code = verdict
DIPLOMAT_AUTOFIX_POLL=1  ...                     # one real monitor poll: prints its dispatch decisions and
                                                     #   the exact prompts it would spawn, opens nothing
DIPLOMAT_APIWATCH_SCAN=1 ...                     # dry-run the API-error watcher over live sessions, sends nothing
DIPLOMAT_SELF_UPDATE=1   ...                     # the unattended 06:00 update: merge if behind, rebuild,
                                                     #   relaunch only if an instance is running

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

## Requirements

- macOS 13+ (uses SwiftUI `MenuBarExtra`) — or Linux via the [Qt6 applet](packages/diplomat-platform/linux/README.md)
- Swift toolchain (`swift build`)
- GitHub CLI `gh`, authenticated (`gh auth login`)
- **Node.js** — only for the device allocator (its daemon + MCP server)
- **python3** — only to run a mesh node (plus the optional `cryptography`
  package for device keys; without it the node is keyless and unverifiable)

## Architecture

The triage logic is single-sourced in
[`packages/diplomat-core/assets/`](packages/diplomat-core/assets/README.md) -
language-neutral GraphQL queries, the tool catalog, filter constants, and the
prompt fragments for all three actions. Both front-ends load it and assert their
assembled prompts byte-for-byte against `assets/golden-prompts/`, so they can only
drift from each other by failing a CI job. Both also run the full monitor stack;
what stays macOS-only is the per-row **Merge** button, the clickable *session* rows
of the [Agent tasks](#agent-tasks) list (a Linux spawn is a detached `Popen` with no
window handle, so a running agent gets a row there but not a window to focus, and
neither of the *done* / *merged* statuses that watching a session's window is what
yields), and reading arbitrary terminal windows (the Linux watcher drives tmux panes
instead - which is also how Linux reads *awaiting input*, the one session status it
does not need a window handle for).

This repository is a **monorepo of independent parts**: everything lives in
`packages/`, one directory per package, and CI is arranged to keep them
independent rather than merely to say they are. Each package that could stand
alone gets a job that installs *only* what that package needs, so a dependency
creeping back in fails a build instead of going unnoticed:
[`szpontnet-core`](packages/szpontnet-core/README.md) is tested with no Qt, no
`diplomat-core` and no Diplomat on the import path, and
[`device-allocator`](packages/device-allocator/README.md) with nothing but Node.
Diplomat is checked from the other side — one step deletes both SzpontNet
packages outright and renders the applet from what remains.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) is seven jobs:
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
                                 daemon restarts in ~/.diplomat/device-allocator/, idle devices reclaimed
                                 after 15 min; a prompt-injection report bans the author and terminates
                                 the reporting agent). Standalone: any MCP client can point at src/mcp.js

  diplomat-core/               ← the shared triage brain: everything both front-ends agree on
    assets/                    ← the language-neutral source of truth (see its README): GraphQL queries,
                                 tool catalog, filter constants, prompt fragments, the mesh model
      golden-prompts/            canonical prompt outputs, asserted byte-for-byte by BOTH platforms' tests
    Sources/
      DiplomatCore/            ← Foundation-only Swift; loads assets/. Builds on macOS AND Linux.
        CoreAssets.swift           resolves + decodes assets/ (config, catalog, filters, review, conflicts, audit, graphql)
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
        AgentActivity.swift        terminal-tail classification: running vs awaiting input
        AgentRunner.swift          which agent CLI a spawn runs, and the one command that runs it
        OpenCodeAPI.swift          reading an OpenCode run's own session: whose it is, mid-turn or not, spend
        HermesStore.swift          the same, for a Hermes run's session in its SQLite store
        AgentState.swift           the one resolver: typed evidence -> a state per agent run,
                                   and the four projections (dedup, cap, rows, retirement)
        AgentRegistry.swift        the durable run book both applets read/write (~/.diplomat/agents)
        ApiErrorMatch.swift        "is this a Claude API error?" matcher for the watcher
        AuditCategory.swift        audit action verb → activity-feed filter category (mirrors assets/audit-categories.json)
        Mesh.swift                 mesh model: decodes assets/mesh.json + ~/.diplomat/mesh/state.json, pure placement
        Telemetry.swift            folds the telemetry ledger + every figure on the Telemetry screen: window
                                   calibration, the distribution + its confidence interval, the quota readings,
                                   the pending series
      DiplomatCoreSmoke/       ← Linux-buildable core self-test (filters + prompts + golden files + live dump)
      DiplomatCoreCLI/         ← thin `build-prompt` CLI over the core (ships as the `diplomat-core` binary),
                                 so the Linux front-end shells out for Review/Conflicts/Audit prompts
                                 instead of reimplementing them; `tool-data` and `telemetry` expose the two
                                 engines the Linux side reimplements, so the parity tests can diff them

  diplomat-runtime/            ← the platform-neutral Python half: the twin of diplomat-core plus
                                 everything below the UI that has no Swift counterpart
    diplomat_runtime/            no Qt, no AppKit — the assets loader, PR triage, the run book, token
                                 accounting, the spawner. The Linux applet imports it; the macOS app's
                                 mesh node runs it with nothing else of Diplomat's on its path
      szponthost.py            ← Diplomat's answers to the six questions a mesh node asks its host:
                                 the duty catalog, the state dir, where events go, how a job runs here,
                                 whether an agent is already up on that work, and whether this machine
                                 has room for another and the rate limit to afford it

  diplomat-platform/           ← the platform wrappers: one UI each over that same core
    macos/                     ← macOS SwiftUI menu-bar app — thin UI over the core
      Sources/Diplomat/
        DiplomatApp.swift          @main app + MenuBarExtra + the headless self-test entry points
        Headless.swift             the single "are we a one-shot self-test?" env-var list
        ContentView.swift          two-column panel (left: monitoring lists, right: grid + wizards/results)
        Components.swift           shared UI atoms (cards, chips, badges)
        ReviewWizard.swift         Review-PRs wizard + AgentSpawner (staged prompt file, done sentinel, iTerm/Terminal)
        ConflictWizard.swift / AuditWizard.swift   the Resolve-conflicts and Full-E2E-test wizards
        SettingsView.swift         settings (username, repo root, monitors + auto-approve + task cap + rate-limit budget, watcher, tools, terminal, allocator)
        Store.swift                ObservableObject; settings + the monitor/watcher loops; logic in ToolData
        AutofixMonitor.swift       the monitors' GitHub reads (monitor-prs / review-requests queries)
        AutofixStatus.swift        the monitor heartbeat behind the status pill
        ApiErrorWatcher.swift      iTerm/Terminal session reader + continue-nudge sender
        AgentProbes.swift          the outside world, typed: `ps`, screens, sentinels, claims -> Evidence
        AgentWindows.swift         where each run's terminal window is, so a row click can raise it
        AgentSessionProbe.swift    asks each run's own agent what it is doing, through its runner's store
        OpenCodeProbe.swift        dials an OpenCode run's own server: free port, session list, messages
        HermesProbe.swift          reads a Hermes run's session out of ~/.hermes/state.db, read-only
        TrackTest.swift            E2E self-test of the run book + this platform's probes (DIPLOMAT_TRACK_TEST)
        QueueTest.swift            self-test of the deferred-task queue (DIPLOMAT_QUEUE_TEST)
        SweepTest.swift            self-test of asking each runner's own store (DIPLOMAT_SWEEP_TEST)
        BanList.swift / AuditLog.swift   ban list (the daemon's banned.json) + the unified activity feed (audit.jsonl)
        DeviceAllocator.swift      allocator daemon state reader + installer bridge
        DeviceFocus.swift          click an in-use device → focus the holding agent's terminal
        Daemon.swift               first-run login-daemon opt-in (TTY Accept [y/N])
        Render.swift               headless ImageRenderer snapshots for UI checks
        Color+Hex.swift            Color ↔ "#RRGGBB" for persisted tint overrides
        MeshBridge.swift           drives the local mesh node (spawn python3 -m szpontnet --daemon, NDJSON control)
        MeshView.swift             the ⬡ Mesh screen: node graph, tier/token/trust editors, duty table
        MeshSpawn.swift            the wizards' "⬡ Run on mesh" row + destination preview
        TelemetryView.swift        the Telemetry screen: the bell curve, the rate-limit windows, the backlog series, the token split
        TelemetryLog.swift         writes/reads ~/.diplomat/pr-monitor/telemetry.jsonl (append-only, rotated)
        UsageScan.swift            Claude Code transcript scanner: repo-vs-other tokens, per-task attribution
        Quota.swift                the OAuth usage probe — what is left of the 5-hour and 7-day windows
        AutoBudget.swift           ledger + probe + knobs -> may another automatic task start here?
        SelfUpdate.swift           fetch/merge upstream, rebuild, relaunch (Update button + the 06:00 run)
        RepoPaths.swift            locate this app's own checkout (DIPLOMAT_SELF_REPO → … → ~/dev/diplomat),
                                   the sibling packages it reaches for, and the agents' repo root
        AppConfig.swift            the cross-process settings file (~/.diplomat/config.json) the mesh node shares
      install/                 ← build-app + the autostart / auto-update (un)installers (launchd)
    linux/                     ← Linux Qt6/PySide6 tray applet (see its README)
      diplomat_app/            ← what is this front-end's own: screens, wizards, the Store driving
                                 them, its self-update and single-instance guards, and probes.py —
                                 this platform's evidence gatherer, twin of AgentProbes.swift
      install/                 ← build-core + the autostart / auto-update (un)installers (XDG + systemd)
      meshsim/                 ← the real-socket mesh simulator the mesh scenarios run through

  szpontnet-core/              ← the SzpontNet node: an independent library (see its README).
    szpontnet/                   stdlib-only Python (runs headless on macOS too) — LAN discovery,
                                 heartbeat links, gossip, deterministic duty assignment, dispatch with
                                 failover; canonical v1 model in netmodel.json, `python -m szpontnet`
      host.py                  ← the six questions a node asks whoever is running it; all six have
                                 working defaults, so a node with no host is a valid node
      env.py                   ← the SZPONTNET_* namespace, one accessor, one place the old names bridge
    tests/                     ← the library on its own terms — defaults, the host seam, the namespace,
                                 a scan that fails if a module so much as names its host, plus the
                                 integration ones (Tor transport, the startup lock, control-edit flush)

  szpontnet-spec/              ← the protocol, kept apart from any implementation of it
    docs/                      ← the normative SzpontNet spec (15 chapters, v0.5.0, wire v: 1)
    conformance/               ← black-box conformance tester: runs a candidate node as an opaque
                                 subprocess, joins over real multicast + TCP, exits non-zero on any MUST failure

  szpont/                      ← what `szpont` means on PyPI (see its README): the `szpont` command
    szpont_launcher.py         ← clone/build/launch Diplomat, out of the standard library alone. Named
                                 apart from `szpont` so the conformance tester keeps the import name

  szpont-npm/                  ← what `szpont` means on npm (see its README): the same launcher, in
                                 JavaScript, for `npx szpont`
    test/scenarios.mjs         ← the machine shapes both launchers are held to; parity-with-python.mjs
                                 runs every one of them through both and demands the same plan

.github/workflows/ci.yml       ← swift-macos · swift-core-linux · python-linux · szpontnet · szpont ·
                                 szpont-npm · node-device-allocator
.github/workflows/release-szpont.yml     ← every push to main → the next minor on PyPI + npm, from one
                                           verified commit; tag szpont-v* to release what a tree states
.github/workflows/release-szpontnet.yml  ← tag szpontnet-v* → PyPI
```
