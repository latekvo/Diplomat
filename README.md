# Diplomat (Szpont Yon)

<img width="1146" height="904" alt="image" src="https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02" />

## TL;DR:

- Auto reviews PRs which have you listed as reviewer
- Auto fix your own PRs once they get a review
- Auto resolve all conflicts on your PRs
- Enforces one device per agent
- Manually review all PRs of the given person

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
> dedup (tracked sessions plus a `ps` ground-truth scan), mesh coordination,
> spawn, session tracking, and counters live in exactly one place per platform.
> The only trigger asymmetries are the documented ones in `AgentDispatchGate`
> (its Python twin `autofix.dispatch_decide`): manual spawns come to the
> foreground (macOS) and are never mesh-gated, monitor dispatches get the
> `Auto · … · retry N` label, and only they bump the auto-handled counters.
> Parity tests on both sides pin that matrix. A job arriving *over the mesh* is
> the one spawn that bypasses the pipeline - it lands through the mesh node's own
> runner, and the `ps` ground-truth scan is what re-attaches it afterwards.

## ⚠️ Billing and usage - read this first

> **This software may be used _only_ with usage-based Anthropic API billing** - an
> **Anthropic API key** from the [Anthropic Console](https://console.anthropic.com),
> metered per token and governed by Anthropic's
> [**Commercial Terms of Service**](https://www.anthropic.com/legal/commercial-terms).
> Point the spawned agents at it via `ANTHROPIC_API_KEY` (Claude Code prefers an API
> key in the environment over any logged-in subscription).
>
> **It may _not_ be used with a personal Claude subscription plan** - the consumer
> **Claude Free / Pro / Max** plans, governed by Anthropic's
> [**Consumer Terms of Service**](https://www.anthropic.com/legal/consumer-terms).
> Diplomat exists to spawn **automated, unattended agents** - background monitors that
> open `claude` windows and push to your branches with no human in the loop. That is
> programmatic / headless / service-style use, which belongs on the API, not on a
> personal subscription.
>
> **No warranty, no responsibility.** This software is provided **"as is"**, without
> warranty of any kind. **The author accepts no responsibility and no liability** for
> anything arising from its use - API charges, subscription or account actions, commits
> and pushes made to your repositories, or agents spawned on your machines. **You alone
> are responsible** for your API key and your spend, and for ensuring your use complies
> with Anthropic's applicable terms.

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
inline un-ban); the **agent sessions** list (every spawned agent, wizard- or
monitor-launched, with a live *running / awaiting input / done / merged* state -
click a row to focus its terminal window, ✕ to stop tracking); the **devices**
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

## Actions - Review PRs

The grid carries a **Review PRs** card alongside the tools. Click it and the wizard
opens where the PR lists normally render; dial in a few choices and hit **SPAWN
AGENT** - it opens a fresh terminal window (iTerm if installed, else Terminal)
running a detached review session in your **repo root** (Settings; default
`~/dev/<repo>`) that you watch and steer yourself. The prompt is staged to a
file and the window runs
`claude "$(cat <promptfile>)"; printf %s $? > <done>` - the trailing sentinel
(under `~/.diplomat/pr-monitor/done/`) is how the sessions list knows the agent
finished. The choices are baked into the prompt:

- **Target** — the same three-way selector the other wizards use: *Mine* (the
  resolved handle, see Settings), *Someone else's* (a handle field lights up), or
  *Specific PR* (a number/URL field lights up - review exactly that one).
- **Scope** — *Review draft PRs* and *Review ready-for-review PRs* (both on by
  default; hidden for a specific PR, which is already one exact PR). Untick both
  and SPAWN greys out - there'd be nothing left to review.
- **Review depth** — a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E with a second double-pass verification.
- **Mark clean PRs ready for review** — *(never on someone else's)* flip
  perfectly-clean drafts to ready.
- **Leave reviews (CLAUDE.md format)** — *(never on my own)* post formal per-line reviews.
- **Reply to others' review threads** — *(never on someone else's)* answer and resolve open threads.
- **✨ Final E2E pass + verdict** - *(highlighted, off by default; others' and
  unknown-author PRs only - never my own, there is no self-approval)* appends a
  culminating full-E2E pass on the real binaries with big swarms: APPROVE
  perfectly-clean PRs (after confirming past issues are resolved),
  APPROVE-with-nitpicks when there are only minor asks, or leave **changes
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
flow, build and test in the target repo, hard-reproducing every finding before
reporting it (prompt model in `assets/audit.json`). Every confirmed finding is
**classified HIGH / MEDIUM / LOW** by real impact, and that label rides through to
the report - that part is always on. By default the run only finds and reports;
nothing is changed. Two escalation toggles widen the blast radius:

- **Open PRs for every finding** — one focused PR per fix, **always as a draft**,
  and only after checking the repo's open PRs (by real `gh pr diff` content, not
  titles) so it never files a duplicate. PRs are severity-gated: HIGH and MEDIUM
  always get one, a LOW/nitpick only when its fix is under 20 lines of diff -
  anything bigger is reported, not PR'd. Off = a strictly read-only audit.
- **Also fix open bug issues** — reproduce + fix the repo's open BUG issues, never
  feature requests.

> Preview: `DIPLOMAT_PRINT_PROMPT=audit swift run Diplomat` (also `=audit-issues`, `=audit-prs`, `=audit-all`).

## Diplomat Mesh (experimental) — LAN P2P duty coordination

> Diplomat Mesh is the reference implementation of **SzpontNet**, a small leaderless
> LAN protocol for self-discovery, resource advertisement, and work hand-off. The
> full, independently-implementable specification (currently **v0.4.0**, wire `v: 1`)
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
receiving machine opens its own terminal running `claude` exactly like a local
SPAWN AGENT (dispatches are the `📤/📥 mesh` rows in the activity feed). If the
first target declines — gone, or out of tokens — the dispatch fails over to the
next candidate by rank. While the mesh is live, the three wizards grow a
**⬡ Run on mesh** row (checked by default, with a preview of where the duty
currently routes): SPAWN AGENT then hands the job to the node instead of always
opening a local terminal — on both front-ends.

Both front-ends grow a **Mesh screen** (the ⬡ button in the panel header, beside
Settings): the live node graph (link states), per-node tier/token editors (editing
a *remote* node forwards over the mesh, so one panel configures the whole fleet),
per-duty strategy + token-awareness controls (gossiped last-writer-wins), and the
whole **trust surface** — the `New devices: Personal / Foreign` default, a one-time
callout when an unknown device shows up, a per-peer trust toggle, and the banned
chip with its un-ban. It shouts `DEVICE IS NOT DISCOVERABLE` if every beacon send
fails.

The mesh node itself is stdlib-only Python that runs on any OS — both the macOS app
and the Linux applet drive that same node (a Swift node is future work), so enabling
the mesh on macOS needs the source checkout on disk (`DIPLOMAT_SELF_REPO` if it
isn't at the default `~/dev/diplomat`):

```bash
cd packages/diplomat-platform/linux
# Put Diplomat behind the node: without this it runs SzpontNet's own defaults —
# the canonical v1 duties, state in ~/.szpontnet, no activity feed — and so joins
# a different mesh than the one your app is on.
export SZPONTNET_HOST=diplomat_app.szponthost PYTHONPATH=../../szpontnet-core

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

**Trust model.** The mesh is meant for a LAN you control (IPv4; discovery is
multicast + subnet broadcast). Two independent fences:

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

## Autonomous monitors

The applets don't just render lists - they act on them. Three background
monitors ship **ON by default** (opt out in Settings). Know what that means
before running it: they **spawn real terminal windows** running `claude` agents,
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
  thorough review the wizard can express: Full E2E ×2 depth, formal per-line
  comments, hands strictly off the branch. "Owed" comes from GitHub's own
  timestamps (request newer than my last review), so a genuine re-request
  re-qualifies, and a review left unaddressed (agent died, window closed) is
  retried on the same 5m→3h backoff until the review actually lands. Force-push
  dedup: a push re-stamps the review request, which would double-spawn - a new
  request within 1h of a dispatch is treated as churn and suppressed. Banned
  authors are never auto-reviewed.
- **Claude API-error watcher** - every ~20s reads each agent session's visible
  tail (macOS: any iTerm/Terminal session; Linux: **tmux panes only** - there's no
  portable way to read or type into an arbitrary Linux emulator, so an agent must
  be running inside tmux to be watched). An agent stalled on a transient API error
  (overloads, connection failures, bare `429` rate-limits, status-page errors) gets
  a continue nudge typed into that exact session, with a per-session 2m → 3h
  backoff so a persistently broken one isn't hammered. A single erroring scan never
  nudges: the tail must come back **byte-identical on the next scan** before it
  counts as a stall, so the real floor is ~2 scans. An **out-of-quota** banner is
  never nudged - it's not transient - and it suppresses any API error sharing the
  same tail.

Poll failures (gh / auth / network) surface in Settings and the activity log
rather than silently freezing stale counts. Rate-limit note: the GitHub GraphQL
budget (5000 points/hr) is shared with the agent swarm and these searches aren't
cheap - the 3-minute cadence is deliberate; responsiveness comes from the
immediate poll on wake/enable, not from a tight loop. Both cadences are
overridable for tuning (`DIPLOMAT_AUTOFIX_SECS`, floor 60s on macOS / 30s on
Linux; `DIPLOMAT_APIWATCH_SECS`, floor 5s).

**With a mesh up, the monitors defer.** A duty the mesh has assigned to *other*
nodes makes this machine's monitors stand down entirely for it - their agents
originate over there instead - and for the remaining races (no assignee, takeover
flaps) each unit of work is claimed by key first. Those show up as
`mesh-standdown` / `mesh-resume` rows in the activity feed.

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

The header **⚙︎** button (next to ↻, the **⬡** mesh button, and ⏻) swaps the panel
to a settings screen:

- **GitHub username** - override the handle used by the "My …" tools, the wizards
  and the monitors. Blank = the `gh`-authenticated user (`viewer.login`), resolved
  eagerly at launch so it's the default everywhere.
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
- **Auto-fix my PRs / Full-E2E review requests** - the two monitor toggles, with
  live status: PRs watched, reviews done so far, "N unaddressed reviews -
  retrying", and any poll failure. (The combined *fixed N* counter lives on the
  panel's status pill, not here.) Nested under the **review-requests** toggle -
  and visible only while it's on - the **auto-approve** master toggle and its
  three withhold-the-verdict suppressors (SKILL / installer / community).
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
the sessions list can flip a row to *merged*. The [autonomous
monitors](#autonomous-monitors) are separate, on their own 3-minute schedule.

## Run

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
point is exactly that). `packages/diplomat-platform/macos/Sources/Diplomat/Headless.swift` is the one list that
decides what counts as headless:

```bash
DIPLOMAT_DUMP=1 swift run Diplomat            # real fetch+filter pipeline, prints all 6 tools, exits
DIPLOMAT_LOOKUP=337 swift run Diplomat        # reverse-lookup one number through the real Store
DIPLOMAT_PRINT_PROMPT=mine swift run Diplomat # assemble + print a prompt: mine|user|single (append
                                                     #   -final for the verdict pass), conflicts[-user|-single],
                                                     #   audit[-issues|-prs|-all]
DIPLOMAT_SETTINGS_DUMP=1 ./Diplomat.app/Contents/MacOS/Diplomat  # resolved persisted settings
DIPLOMAT_RENDER=panel    ./Diplomat.app/Contents/MacOS/Diplomat  # snapshot a screen to PNG (out
                                                     #   path: DIPLOMAT_RENDER_OUT). States: panel|panel-procs
                                                     #   natural|settings|settings-live|approved|unban-confirm
                                                     #   activity[-filtered] (audit feed + its filter chips)
                                                     #   wizard[-other|-specific[-mine|-theirs]|-wrong|-banned]
                                                     #   devices[-open]|conflicts[-other|-specific|-wrong]
                                                     #   audit[-issues|-prs|-all]
                                                     #   mesh (⬡ screen over a synthetic topology); mesh-blocked
                                                     #   (the not-discoverable banner); mesh-reminder (trust modal)
                                                     #   popover (REAL NSWindow snapshot incl. the legacy
                                                     #   scroller — pair with DIPLOMAT_POPOVER_CAP=400
                                                     #   to force the scrolling state)
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
what stays macOS-only is the per-row **Merge** button and reading arbitrary
terminal windows (the Linux watcher drives tmux panes instead).

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

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) is five jobs:
`swift-macos` (build both Swift packages + the core smoke + a headless panel
render + the helper self-tests), `swift-core-linux` (proves the core builds on
Linux, and publishes a static `diplomat-core` binary), `python-linux` (pytest
against that binary, so the golden-prompt parity is proven across languages, then
the library-less start), `szpontnet` (the library's own tests plus a full
conformance run against the reference node), and `node-device-allocator`.

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
        Autofix.swift              PRSnapshot + the monitor's edge-trigger diff, AgentDispatchGate, AutofixMesh
        ReviewReconcile.swift      pure retry/backoff/dedup decisions for the monitors
        AgentActivity.swift        terminal-tail classification: running vs awaiting input
        ApiErrorMatch.swift        "is this a Claude API error?" matcher for the watcher
        AuditCategory.swift        audit action verb → activity-feed filter category (mirrors assets/audit-categories.json)
        Mesh.swift                 mesh model: decodes assets/mesh.json + ~/.diplomat/mesh/state.json, pure placement
      DiplomatCoreSmoke/       ← Linux-buildable core self-test (filters + prompts + golden files + live dump)
      DiplomatCoreCLI/         ← thin `build-prompt` CLI over the core (ships as the `diplomat-core` binary),
                                 so the Linux front-end shells out for Review/Conflicts/Audit prompts
                                 instead of reimplementing them

  diplomat-platform/           ← the platform wrappers: one UI each over that same core
    macos/                     ← macOS SwiftUI menu-bar app — thin UI over the core
      Sources/Diplomat/
        DiplomatApp.swift          @main app + MenuBarExtra + the headless self-test entry points
        Headless.swift             the single "are we a one-shot self-test?" env-var list
        ContentView.swift          two-column panel (left: monitoring lists, right: grid + wizards/results)
        Components.swift           shared UI atoms (cards, chips, badges)
        ReviewWizard.swift         Review-PRs wizard + AgentSpawner (staged prompt file, done sentinel, iTerm/Terminal)
        ConflictWizard.swift / AuditWizard.swift   the Resolve-conflicts and Full-E2E-test wizards
        SettingsView.swift         settings (username, repo root, monitors + auto-approve, watcher, tools, terminal, allocator)
        Store.swift                ObservableObject; settings + the monitor/watcher loops; logic in ToolData
        AutofixMonitor.swift       the monitors' GitHub reads (monitor-prs / review-requests queries)
        AutofixStatus.swift        the monitor heartbeat behind the status pill
        ApiErrorWatcher.swift      iTerm/Terminal session reader + continue-nudge sender
        ProcessTracker.swift       tracked agent sessions (liveness, focus, done sentinel, merged)
        TrackTest.swift            E2E self-test of the tracking path (DIPLOMAT_TRACK_TEST)
        BanList.swift / AuditLog.swift   ban list (the daemon's banned.json) + the unified activity feed (audit.jsonl)
        DeviceAllocator.swift      allocator daemon state reader + installer bridge
        DeviceFocus.swift          click an in-use device → focus the holding agent's terminal
        Daemon.swift               first-run login-daemon opt-in (TTY Accept [y/N])
        Render.swift               headless ImageRenderer snapshots for UI checks
        Color+Hex.swift            Color ↔ "#RRGGBB" for persisted tint overrides
        MeshBridge.swift           drives the local mesh node (spawn python3 -m szpontnet --daemon, NDJSON control)
        MeshView.swift             the ⬡ Mesh screen: node graph, tier/token/trust editors, duty table
        MeshSpawn.swift            the wizards' "⬡ Run on mesh" row + destination preview
        SelfUpdate.swift           fetch/merge upstream, rebuild, relaunch (Update button + the 06:00 run)
        RepoPaths.swift            locate this app's own checkout (DIPLOMAT_SELF_REPO → … → ~/dev/diplomat),
                                   the sibling packages it reaches for, and the agents' repo root
        AppConfig.swift            the cross-process settings file (~/.diplomat/config.json) the mesh node shares
      install/                 ← build-app + the autostart / auto-update (un)installers (launchd)
    linux/                     ← Linux Qt6/PySide6 tray applet (see its README)
      diplomat_app/szponthost.py   ← Diplomat's answers to the five questions a mesh node asks its host:
                                 the duty catalog, the state dir, where events go, how a job runs here,
                                 and whether an agent is already up on that work
      install/                 ← build-core + the autostart / auto-update (un)installers (XDG + systemd)
      meshsim/                 ← the real-socket mesh simulator the mesh scenarios run through

  szpontnet-core/              ← the SzpontNet node: an independent library (see its README).
    szpontnet/                   stdlib-only Python (runs headless on macOS too) — LAN discovery,
                                 heartbeat links, gossip, deterministic duty assignment, dispatch with
                                 failover; canonical v1 model in netmodel.json, `python -m szpontnet`
      host.py                  ← the five questions a node asks whoever is running it; all five have
                                 working defaults, so a node with no host is a valid node
      env.py                   ← the SZPONTNET_* namespace, one accessor, one place the old names bridge
    tests/                     ← the library on its own terms — defaults, the host seam, the namespace,
                                 a scan that fails if a module so much as names its host, plus the
                                 integration ones (Tor transport, the startup lock, control-edit flush)

  szpontnet-spec/              ← the protocol, kept apart from any implementation of it
    docs/                      ← the normative SzpontNet spec (15 chapters, v0.4.0, wire v: 1)
    conformance/               ← black-box conformance tester: runs a candidate node as an opaque
                                 subprocess, joins over real multicast + TCP, exits non-zero on any MUST failure

.github/workflows/ci.yml       ← swift-macos · swift-core-linux · python-linux · szpontnet · node-device-allocator
```
