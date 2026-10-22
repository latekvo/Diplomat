# Telemetry

The panel's third screen (the header button between **⬡** and **⚙︎**) answers the
question the monitors otherwise leave open: *what is all this costing, and is it
keeping up?* Nine figures, over a lookback you pick (**7d / 14d / 30d / 60d**,
default 14):

- **Limit per task** - the share of one 5-hour rate-limit window an auto-task
  consumes on average, plus a **bell curve** of how that is distributed, with a 95%
  confidence interval on the mean drawn behind it (the histogram is the spread of
  the tasks; the band is how well the *average* is known, and is much narrower). The
  same tasks are drawn a second time against the **7-day** window - red bars for the
  5-hour one, yellow for the week, grouped inside shared bins on one axis, each with
  its own fitted normal, interval and mean rule. Both windows are priced from their
  own quota readings, so either can be measurable while the other is not, and which
  of the two humps sits further right is the ceiling that runs out first.
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
- **Finished work** over time - how many tasks *finished* in each equal bucket of the
  lookback, as bars: 4-hour buckets over a week, 12 over a fortnight, a day over 30
  and two over 60, so every range draws 28-42 bars and each one covers the same span
  as its neighbours. Bucketed by when a run **ended**, so a run that spanned two bars
  is one delivery in the bar it finished in. Work the mesh placed on a peer counts
  here - it was delivered - even though its cost is that peer's and stays out of the
  per-task figures above. The bars are laid backwards from *now*, so the newest is a
  whole bucket rather than whatever fraction of one the range happens to end on.
- **Time to start** - from the monitor first seeing a unit of work to an agent
  taking it (the reconciler's backoff, an applet that was off, a busy PR).
- **Time to finish** - from an agent starting to its exit.
- **Spent on this repo** - how much of this machine's Claude spend went on the repo
  the agents work in rather than everything else. Split by the `cwd` each turn ran
  in, so it counts your own sessions in that checkout too - it is a repo split, not
  an attribution to Diplomat's agents (that is what *limit per task* measures).

## Where the numbers come from

Six of these are time-series questions a counter cannot answer ("how many were owed
last Tuesday?", "how much did we finish overnight?", "how close to the ceiling did we
get?"), so the source is an
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
  the [mesh is an optional add-on](mesh.md),
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
  `DIPLOMAT_CLAUDE_DIR` moves where it reads from. A [foreign runner](settings.md#agent-runner)
  writes no such transcript, so each is priced from its own store instead - OpenCode
  summed over every message of its session export (`opencode export <session>` on
  1.x, `opencode session export <session>` on 2.x; it reports a turn's cost per
  message), Hermes read off the running totals on its session row - both counting
  the same three fields, so one ledger holds every runner in one unit. Hermes' row
  also carries what the provider **charged**, and the model it charged for, which is
  the unit the [spending budget](automation.md#the-spending-budget) holds that machine to.
- **Balance** (`spend.py` / `Spend.swift`) - the money twin of the quota probe, and
  what the spending budget reads when the runner is billed by a provider rather than
  out of a rate-limit window. One GET each against OpenRouter's `/api/v1/key` and
  `/api/v1/credits`, keyed from `~/.hermes/.env`, giving dollars left on the key's own
  cap and on the account's credit balance. Cached and degraded exactly as the quota
  probe is; `DIPLOMAT_SPEND_PROBE=0` turns it off and `DIPLOMAT_HERMES_ENV` moves
  where the key is read from.

The probe reports **what is left of each window** on every sample, and that reading is
what *rate limit left* draws - measured, not derived. What Anthropic never publishes is
a **token budget**, and the budget is dynamic - so the figure that cannot be looked up
is the *per-task* share, "4% of the window". The 5-hour window is instead **priced from
what actually happened**: over an interval the
account spent `Δutil` of its window while this machine logged `Δtokens`, so the window
is worth `Δtokens / Δutil`, summed across every interval that didn't span a reset. Until
two quota readings exist there is no price, and the screen says so and shows raw tokens
per task rather than inventing a percentage. The 7-day window is priced the same way
from its own readings. It moves slowly, so it takes longer to measure - and until it
has a price the screen leaves its series off rather than rescaling the 5-hour figure
into it. Work placed on a **mesh peer** spends that peer's quota, so it counts as
started and is kept out of the cost and run-time figures.

The arithmetic is shared: [`Telemetry.swift`](../packages/diplomat-core/Sources/DiplomatCore/Telemetry.swift)
and `diplomat_runtime/telemetry.py` are diffed field-for-field over one ledger by
`tests/test_telemetry_parity.py`, so the two screens cannot disagree about what a
ledger means.
