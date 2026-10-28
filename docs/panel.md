# The panel

What the panel shows, and the Agent-tasks list that every automatic and requested run passes through.

## The library

| Tool | What it lists |
|------|---------------|
| **SKILL.md PRs** | open PRs editing a `SKILL.md` |
| **Installer/CLI PRs** | open PRs touching `argent-installer` / `argent-cli` |
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
it) and it instantly shows which of the six lists that number is on - a ✓/- checklist
plus what the number is (open PR/issue, author, draft/ready). Cache-only, so it reacts
as you type. Launch with `DIPLOMAT_PREFILL=<n>` to open pre-focused on a number.

## Layout

Two columns. **Right:** everything interactive - the search box, the tool grid
(six tools + four action cards), and whatever a card opens: a tool's result
list, the reverse lookup, or an action wizard. On **My Approved PRs** each row
carries a **Merge** (squash) button - or **Resolve conflicts** when GitHub
reports the PR conflicting, which spawns the fix agent for exactly that PR.

**Left:** the monitoring surfaces. The monitor **status pill** shows whenever a
monitor is enabled (heartbeat: PRs watched, conflicts/reviews handled; "offline"
when polling stops for 15 minutes). The rest appear only when non-empty: the
**banned** list (prompt-injection bans of PR authors and of [Comms](comms.md#bans) Slack
senders: who, where, when, the quoted evidence and which agent or check reported it, with the
captured evidence and an inline un-ban; a new ban also raises a system notification); the collapsible **agent tasks** list (below); the **devices**
pool (who holds which simulator/emulator, for how long, with a per-device kill -
clicking an in-use device focuses the holding agent's terminal, and the section's header
opens the [Devices screen](devices.md)); and the
**activity** log, one unified audit feed
(`~/.diplomat/pr-monitor/audit.jsonl`) of panel actions, monitor dispatches,
nudges, and daemon-side bans.

The activity feed is **filterable in place**: it heads a row of per-category chips
with counts - Reviews · Replies · Conflicts · Audit · Issues · API restart · Out of
quota · Merges · Bans · Mesh · Comms · System - and tapping one mutes that category and drops its
rows. The taxonomy (which raw action verb maps to which category, plus its icon and
tint) is shared in [`assets/audit-categories.json`](../packages/diplomat-core/assets/audit-categories.json), with
`packages/diplomat-core/Sources/DiplomatCore/AuditCategory.swift` as the Swift source of truth.

## Agent tasks

One list for what the machine is doing, what it is about to do, and how much room
it has left - the agents it has **spawned**, the automatic work it is **holding**,
and an empty bay per free slot of its [task cap](automation.md#autonomous-monitors). Rows read
in status order: *awaiting input*, *running*, *starting*, *unknown*, *free slot*,
*queued*, so the sessions asking to be read sit at the top and everything not
started yet is at the bottom. A run that is over is not on the list at all - the
pass that resolves one retires it, so its row would be up for a single redraw;
what it leaves behind is its activity line and its ledger entry. An agent it leaves
at its prompt is drawn again only if it starts working, and is otherwise closed by
the [stillness backstop](automation.md#autonomous-monitors). The list is
always there: an idle machine with a cap of two reads `0 · 2 free` over two empty
bays.

- **Sessions** are every spawned agent, wizard- or monitor-launched. Click a row
  to focus its terminal window, ✕ to stop tracking it.
- **Mesh rows** are the work this machine originated and the mesh is running
  somewhere else, reading *running · on mesh @node*. Same row, same label, same
  place in the list - there is just no window here to click, because the agent is
  on that node. The row lives as long as the executor's
  [work claim](automation.md#autonomous-monitors) does, and leaves when the remote agent
  finishes.

  (Both kinds are macOS only - a Linux spawn is a detached `Popen` with no window
  handle to track a session by. Linux draws a local agent as a row all the same,
  from the in-flight book and a `ps` scan: the bay it took, labelled with the work
  it is on and how long it has been going, and the same status - *awaiting input*
  included, read off the agent's own tmux pane - with no window to click. One the
  mesh placed on a peer shows in the activity feed rather than on the list.)
- **Starting** is a task between the queue and its agent: the click (or the drain)
  has taken it, and the spawn - a `ps` scan, a mesh placement, a terminal - has not
  answered yet. Seconds, and a row for all of them, so *execute now* never reads as
  the click deleting the task. It holds a bay from the moment it starts, and the
  session or mesh row that replaces it takes its place in the list.
- **Free slots** are the rest of the cap. Each running automatic agent takes one, a
  task from a sweep included - the queue picked the moment for it, so it holds a
  bay like anything else that waited there. An agent a wizard press opened on the
  spot takes none, and neither does work the mesh placed on another machine - that
  spends the peer's capacity, not yours. Queued work starts here on the next poll.
- **Auto-execute queue** is the switch at the top of the queue (**default on**): off,
  nothing starts by itself and every row waits for *execute now*. The monitors keep
  finding work and queueing it either way. It stays on screen while it is off even
  with the list empty - otherwise the state that empties the list would be the one
  you cannot leave.
- **Queued** rows are auto-fixes, auto-reviews and the work you asked for by
  sweeping that nothing has started yet. Each
  carries **execute now** - start it immediately, past whatever is holding it -
  and a drag grip: drop a row on another in its band to set the order the queue runs
  in (a drag into another band is refused). That
  order is honoured at the top of the next poll, *before* the monitors go looking
  for more work, so a slot that just freed goes to whatever you put first rather
  than to whichever PR GitHub happened to list first.
- **Requested work** - one row per PR of a Review-PRs sweep, one row per issue of a
  Fix-issues sweep - waits in two bands of its own, swept reviews ahead of swept issue
  fixes, both after every auto-fix and auto-review and before the conflict fixes,
  because what the monitors find is a debt other people can see and a
  sweep is work you started when you had the time for it. Each also carries
  **cancel**: nothing GitHub does takes one off the list while its PR is open - you
  asked for it - and nothing does at all for a fix, since the monitors read PRs and
  never look at an issue. A fix is keyed by its ISSUE number, so it is a row of its
  own beside a review of the PR that happens to share it.
- **Resolve-conflicts** rows run last of all, whatever
  order the monitors found them in, and no drag lifts one out of its band (a drag
  between bands is refused rather than sprung back on the next poll). An agent
  working the
  same branch lands its own merge on the way, so a conflict fix is the work most
  often made unnecessary by the work ahead of it - and the poll re-offers it for
  as long as GitHub still calls the PR conflicting, so waiting costs it nothing.

Five things hold work. The cap holds what there is no slot for, and releases it as
slots free. The [free-disk floor](automation.md#the-free-disk-floor) holds everything
while the disk agents write to is nearly full. The [spending budget](automation.md#the-spending-budget) holds everything when the account is
too low to afford another agent, and releases it when a window refills or the
balance is topped up. A
**monitor you switched off** holds its own work indefinitely: it keeps polling and
keeps listing what it finds (reading *queued · monitor off*), but nothing starts by
itself - only *execute now* does, or a [Comms](comms.md) ping that asked for that work. So the toggles decide who starts the work, not
whether you get to see it, and turning both off does not stop the 3-minute GitHub
poll. **Auto-execute queue** holds every kind at once, the reviews you asked for
included - which is the one thing no monitor toggle speaks for.
