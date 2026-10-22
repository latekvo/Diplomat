# Automatic work

The monitors that dispatch agents without being asked, the task cap, how a run is known to be over, and the spending budget that holds work when the account runs low.

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
  authors are never auto-reviewed, and Settings carries an **author allowlist**
  (*Only these authors*) that narrows the monitor further: blank means anyone who
  requests me, a list means only those logins and everyone else is left to me. It
  speaks for this monitor alone - conflicts, my own review threads, a sweep and a
  wizard press are untouched by it, so the Review wizard still reviews anyone. The
  ban wins where both apply.
- **Claude API-error watcher** - Claude Code runs only; the banners it matches are
  Claude Code's, and an OpenCode or Hermes agent that errors reads as a finished
  turn instead - its runner says so, so the run is retired, its bay and its PR are
  freed, and whichever monitor owed the work dispatches it again. Every ~20s it
  reads each agent session's visible tail (macOS: iTerm and Terminal sessions, plus
  the tmux panes neither of them shows - which is every Ghostty agent, since Ghostty
  can be told to open a window but never asked what is in one; Linux: **tmux panes
  only** - there's no portable way to read or type into an arbitrary Linux emulator,
  so the Linux spawner opens each agent in a tmux session of its own and an agent
  started outside one is not watched). A screen with
  nobody's agent behind it is never written to, whatever it happens to show: the
  nudge is submitted as a line of input, so in a plain shell it would run as a
  command. The process table decides that, since nothing on a screen can - a shell
  can be showing a banner because it printed one. On macOS the session a terminal
  reports is often not the one the agent is on (tmux, or a shell wrapper), so the
  walk that a row click uses to find an agent's window decides it there - which
  resolves to the terminal the agent's session is displayed in, so the line lands in
  that session's active pane. The spawner gives each agent a session of its own,
  which is what keeps those the same screen. An agent
  stalled on a transient API error (overloads, connection failures, a turn cut
  short mid-stream, bare `429` rate-limits, status-page errors) gets a continue
  nudge typed into that exact session, with a per-session 2m → 3h backoff so a
  persistently broken one isn't hammered. A single erroring scan never nudges:
  the tail must come back **byte-identical on the next scan** before it counts
  as a stall, so the real floor is ~2 scans. An **out-of-quota** banner is never
  nudged - it's not transient - and it suppresses any API error sharing the same
  tail. An org **budget cap** (`403 … budget limit exceeded`) counts as one of
  those, whatever its status code: it holds until the window rolls over or an
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
the work a sweep queues, and any work a mesh peer routes here, and it counts
agents that are really running (`ps`, so it survives an applet restart) rather than
a tally that can drift. The agent a wizard press opens on the spot is outside it
and is never refused; what a press leaves in the queue instead - the reviews of a
whose-PRs sweep, the fixes of a Fix-issues one - is on the cap like every other
queued task.

An agent is spawned into an *interactive* session, so finishing its work is not
exiting - it waits at its prompt until someone closes the window, and `ps` shows
it either way. What frees its bay is therefore the turn, not the process, and the
agent reports its own: a Claude Code spawn is handed hooks
(`AgentCompletion`, staged into the run directory and passed with `--settings`)
that append one line to the run's `activity` file as each turn opens and closes,
and the last line is the answer. `Stop` is not taken at face value - a turn that
dispatched subagents or backgrounded a shell hands control back while they are
still running, so the hook writes *busy* instead whenever the payload it is given
still lists background tasks.

A runner with no hooks is asked rather than read: an OpenCode agent's session is
served over loopback while it works (by the run's own server on 1.x, by the per-user
service on 2.x) and a Hermes agent writes its to SQLite, and
either one reporting the turn over ends the run exactly as a hook does - the same
fact from the same kind of source. Only a run nothing answers for - a Claude spawn
whose settings would not stage, a server that never came up - falls back to the
CLI's own status bar (`AgentActivity`) read off whatever each platform can see of a
terminal - iTerm/Terminal sessions and tmux panes on macOS, tmux panes on Linux; an agent whose
status bar shows the CLI back at its prompt reads *awaiting input* and gives its
slot back while keeping its row. That is the one source here that cannot end a run:
it is an inference from someone else's UI, not a report. Behind all three sits a
twenty-minute quiescence backstop for a session wedged mid-turn: a screen that has
not changed in that long - bar the terminal's own clock, which is not the agent
moving - is over, and its window is closed.

(Only positive evidence counts. An agent that reports nothing and whose terminal
can't be read - one outside tmux, or a dump that failed - keeps its bay: the
failure direction is deferring work, never doubling up on it. Its PR stays
in-flight regardless, since that session still holds the context, so nothing else
is dispatched onto the same PR.)

<a id="giving-up-on-a-run"></a>
**Giving up on a run.** Every rung above reads the run itself, so a run none of them
can reach - a runner with no hooks, settings that would not stage, an agent wedged
behind a screen nothing can dump - holds its bay until you close the window. One
switch answers that (Settings → **STALLED AGENTS** → *Give up on a task after 4h*,
**default on**): an automatic run this machine is executing that has gone on for four
hours and that the ladder above still calls *working* is called done anyway, its record
retired and its window closed with it.

It is the last rung of the ladder, not the first, and that is the whole of its
safety. Every answer above it is a better one than a clock, so each of them wins:
a run that ended the ordinary way keeps its window, a pid missing from a table that
was actually read ends the run for that reason instead (the reaper walks out of the
*remembered* tty, and `pts/<n>` is recycled freely), a session already back at its
prompt has handed its bay back and is holding a finished task worth reading, and a
tick whose process table could not be read at all resolves `unknown` and ends
nothing. It fires only while a spending limit both reads and has room left - agents
parked waiting for a window to refill age exactly like wedged ones, so the day a
limit ran out the whole board would otherwise be given up on at once, and a machine
with no limit it can read leaves the backstop off entirely. A peer's run is not
touched (its claim is the machine that can see the process talking), nor is an agent
found by the scan rather than dispatched (retiring one only has the next tick find it
again), nor one you started by hand from the panel - that never took a bay of the
automatic cap, so there is nothing to hand back. Four hours because the longest runs
measured here - a swarm review, an issue reproduced from scratch - are hours long.

Nothing is dropped - work over the cap gets no attempt record, so
the next 3-minute tick offers it again as soon as a bay comes back, and it waits
visibly in the panel's [Agent tasks](panel.md#agent-tasks) list meanwhile, where it can be
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
[mesh row](panel.md#agent-tasks) on its own Agent-tasks list for as long as the claim is
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

## The spending budget

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

**Two currencies, because there are two ways an agent is paid for.** Which one
applies follows from the [runner](settings.md#agent-runner) a spawn uses, and the two never
substitute for each other - a Hermes task held against a Claude window is gated on a
limit it never touches, and a machine not logged into Claude Code at all would have
no gate whatsoever.

- **Claude Code** spends Anthropic **rate-limit windows**, which that account
  publishes only as a percentage. Both windows gate - a task has to fit inside what
  is left of the 5-hour one *and* the 7-day one, since either can be the one that
  runs out. Each window is bounded from its own distribution - the same pair the
  Telemetry screen draws - so a window the ledger cannot price yet leaves the other
  one gated on what was actually measured.
- **Hermes** (and any other runner billed by a provider) spends **money**. Hermes
  prices each of its own sessions against the model it ran on, so the ledger carries
  what the task cost in dollars and which model's rates it was charged at - and the
  gate prices the next task from that model's runs alone, since rates differ by two
  orders of magnitude and a mean across models describes no task that ever ran. What
  is left is read from OpenRouter: the **key limit** (the cap on the API key itself,
  which resets on whatever period the account chose) and the **credit balance** (what
  was bought less what has been spent, which does not reset at all). Both gate, and
  the tighter one answers. The key comes from `~/.hermes/.env`, where Hermes' own
  login wizard puts it - Diplomat stores the choice of runner and model, never a
  secret.

What is *left* comes from the live probe rather than the ledger's last sample:
samples are 15 minutes apart, and several agents' worth of spending fits in that gap.

Four knobs, in **Settings → PR AUTO-FIX**, and in `~/.diplomat/config.json` beside
the task cap so the mesh node reads the same ones:

- **Hold automatic work when the limit runs low** (`autoBudgetGate`, on) - the
  master switch.
- **Confidence** (`autoBudgetConfidence`, `95`) - 50 / 80 / 90 / 95 / 99. Higher is
  stricter. A hand-edited value the table has no quantile for rounds *up*, never
  down.
- **Kept in hand** (`autoBudgetFloorPct`, `20`) - what a window must still have left
  while the ledger is too thin to price a task. Until at least five auto-tasks have
  finished and been priced, this is the whole of the answer, so a fresh install
  spends down to 20% and no further.
- **Kept on the account** (`autoBudgetReserveUsd`, `1.00`) - the same, in dollars,
  for a runner billed in money. A separate knob because a percentage of a credit
  balance is a percentage of however much was last topped up.

Two failure directions, both deliberate:

- **No reading at all is no opinion.** A probe can be off (`DIPLOMAT_QUOTA_PROBE=0`,
  `DIPLOMAT_SPEND_PROBE=0`), logged out, or simply offline. Nothing is held then -
  a gate that read silence as "no budget" would take a machine's automatic work down
  with the network every time it dropped. **A machine that has never been charged a
  dollar is the same silence**: the reserve engages only once something has actually
  been billed here, so a runner pointed at a local model - or one Diplomat cannot
  price - is not held against an account it never draws on merely because a key for
  that account is sitting on disk.
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
banned or outside the auto-review list. Every poll also re-checks the rows it is about to run against the fetches it
has just made - a conflict fix on a PR GitHub no longer calls conflicting, or a
reply on threads that have been answered, leaves the list instead of opening an
agent on work somebody already did. **A PR that has merged or closed retires every
kind of row that names it**, whoever queued it: waiting for a bay can take a day at
two at a time, and a review of a landed diff is one nobody will read. (A queued fix
is numbered in the ISSUE space and no PR speaks for it, so that pass steps over it:
the fix's own prompt re-reads the issue when its turn comes and stops if somebody has
closed it since.) (Not on a mesh claim: the cap outranks the mesh gate, so a machine with
anything queued is one that never asked a peer - peer-owned work leaves when the
drain reaches it and the mesh answers.) The key order is remembered, so your
arrangement survives the rebuild and a restart.

The one exception is the work **you** ask for by sweeping - a review per PR, or a fix
per issue (both below): nothing on GitHub records that a PR or an issue was swept, so
those are remembered instead of re-derived, offered on every poll until each is
dispatched, wear your own label rather than `Auto · `, and carry a **cancel** button
beside *execute now* because otherwise only a ban on the author would retire one
while the PR is open - and nothing at all would retire a fix. They wait in a band of
their own - behind everything GitHub is already owed (a review requested of you, a
thread waiting on your reply), ahead of the conflict fixes - so sweeping fifty drafts
does not bury a review request behind them for a day. A swept PR that lands before
its turn comes is dropped and forgotten, with a line in the feed saying so - a row
that vanished silently would read exactly like one that ran.

*Execute now* keeps the task automatic in every other respect:
same label, same auto-handled counter, same mesh routing - the cap and
the rate-limit budget are the two holds it overrides. So a click can land the agent on a peer rather than
here, and what the starting row becomes is a mesh row saying which; it occupies a
slot of this machine's cap only when it actually runs on this machine.
