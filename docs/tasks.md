# Tasks

The long-running task tracker. A **task** is something that outlives one PR - "physical iPhone
support", "Tor transport" - kept as a title, a few **tags** and its **links**: PRs and issues in any
GitHub repo, Slack threads, any other URL. Diplomat follows the links, keeps the task's **history**,
and derives a **status** with the **reason tags** behind it. Not to be confused with the panel's
[Agent tasks](panel.md#agent-tasks) list, which is the agents running now.

## What it does

Every five minutes, while at least one task is not archived, a **sync** asks GitHub for every linked
PR and issue (one `gh api graphql` call per 25 of them, through your `gh` login) and compares the
answer with what the last sync saw. What changed becomes history:

| Event | Tags |
|---|---|
| a PR or issue seen for the first time | `linked`, `pr:open` / `issue:closed` / ... |
| merged, closed, reopened | `pr:merged`, `pr:closed`, `issue:closed`, `pr:reopened`, ... |
| draft to ready and back | `pr:ready`, `pr:draft` |
| CI turned | `ci:failing`, `ci:passing` (pending says nothing) |
| review decision changed | `review:approved`, `review:changes-requested`, `review:required` |
| became conflicting | `conflicts` |
| a new comment | `comment`, `@author`, with an excerpt |
| a new review | `review:<state>` (`review:approved`, `review:commented`, ...), `@author` |
| a linked PR closes an issue, or a linked issue is closed by a PR, the task does not link yet | `linked`, `via:closes` - it becomes a link, unless you removed that link before |
| GitHub no longer has it (or your login cannot see it) | `link:missing`, once |

Comments and reviews already there when an item is first linked are not replayed, so linking an old
PR adds one line, not its whole thread. Archived tasks are not synced, but keep what the last sync saw
of their items: an archived task keeps its status, and unarchiving it tells only what changed since.

The same sync folds in [Comms](comms.md): a Slack ping that names one of the task's links (a GitHub
URL, `owner/repo#12`, a `#12` of the watched repo, or the Slack link itself) joins its history with
the sender, the channel or `dm`, and what Comms triage made of it (`triage:review`, ...). Nothing new
is asked of Slack. A ping waits for its triage, up to two days, before it is added without one, and
every ping that came after it waits too, so one untriaged ping holds the rest back. A ping triaged as
an injection is never added, nor is one from a sender on the
[ban list](comms.md) - banned when it came or since: a ban also takes that sender's pings out of
every task's history on the next sync.

Your own edits are history too: `created`, `tag:+x` / `tag:-x`, `link:+` / `link:-`, `pinned:done` /
`pinned:paused`, `unpinned`, `archived`, `unarchived`, and the notes you add (text up to 4000
characters). Removing a link sticks: a sync does not add it back as a closing reference, until you
link it again yourself.

## The Tasks screen

Both apps show the same screen; they differ only in glyphs and labels. A header button opens it, with
a badge counting the blocked tasks.

- **Status line** - when the last sync finished, failed or not, and how often it runs; then that
  sync's error, if any: a `gh` failure (nothing is written, the task files stay as they were), or
  the links it had to skip while syncing the rest.
- **Tasks** - not-done ones first, then by their last activity. Each row shows the status, the title,
  the reason tags, the task's own tags, and its links as chips: `#123` for the watched repo,
  `owner/repo#12` elsewhere, `slack`, or the site's host name. A GitHub link's chip has a dot for its
  state as of the last sync (open, draft, merged, closed, missing); none before its first sync. A
  chip opens its link in the browser; its context menu removes the link.
- Unfolding a row shows its actions and its **history**, newest first: when, where from (GitHub,
  Slack, a note, Diplomat), the item it is about, its tags and its excerpt. A line with a link opens
  it. The newest twelve are listed, then "N older…" shows the rest.
- **Actions** - **New task** (a title, tags, and links one per line), and per task **Add link**,
  **Remove link** (from a chip), **Add note** (text and tags), **Edit tags**, **Pin done** /
  **Pin paused** or **Unpin**, and **Archive**. An archived task offers only **Unarchive**; **Show
  archived** lists the archived ones. An edit that fails says why at the editor it came from.
- **Sync now** syncs at once; it is disabled while no task is unarchived. Creating a task, adding a
  link and unarchiving sync right after; a sync asked for while one runs follows it.

A link can be a PR or issue URL (anything after the number is dropped), `owner/repo#12`, `#12` (the
watched repo), a Slack permalink, or any `http(s)` URL. `owner/repo#12` and `#12` are stored as
issues until the first sync says they are PRs. Tags are lowercased, spaces become `-`, and repeats
are dropped; in the screen's fields they are separated by spaces or commas.

## Goals

A **goal** is a task Diplomat proposes on its own: the open issues and PRs of the watched repo that
serve one outcome - five issues asking for iPhone Duo support, the PR adding it, and the three PRs
fixing bugs that block that PR. Every six hours the **grouper** shows the repo's newest 100 open
issues and 100 open PRs (title, labels, the start of the body, what each PR closes) and the goals
already tracked to one `claude -p` (sonnet, no tools, no MCP servers, no settings), and asks which
of them serve one goal. Issue and PR text is untrusted: it reaches the model on stdin, every line
quoted, never as instructions, and the answer can only name the numbers it was shown.

- A new goal needs at least three items no other unarchived task links; at most five are proposed
  per run. It arrives as a **proposed** task tagged `goal`, its history saying `created`,
  `via:grouper`.
- The grouper can also add newly filed items to a goal it proposed earlier (`linked`, `via:grouper`),
  never to a task you made yourself, and never a link you removed from it.
- **Find goals** on the Tasks screen runs it at once.

### Modes

A proposed goal shows its graph and three choices:

| Choice | What happens |
|---|---|
| **Dismiss** | Archived and never listed again, archived or not; the grouper does not propose it again (a proposal whose items are mostly a dismissed goal's is dropped) |
| **Observe** | You see the graph and act on it by hand |
| **Automate** | Diplomat works on it, below |

Any task with GitHub links, including your own, can switch between **Observe** and **Auto**. In
**auto**, on every poll of the [automatic-work monitors](automation.md):

- each open issue that no open, draft or merged PR of the task closes gets a [Fix-issues](actions.md#fix-issues)
  fix of that one issue, feature requests included (a goal's issues often are), in the issue-fix band
  of the queue, under the task cap like any queued work; while still no PR closes it, it is asked for
  again a day later (`fixRetrySecs`)
- with **Review on new commits** on, each open PR is reviewed once per head commit, at max depth with
  per-line comments and no verdict (your own PR as yours)
- your own open PRs get their unresolved review threads fixed, through the PR auto-fix path and its
  backoff, even with that monitor switched off

Only items in the watched repo are worked on; the others are shown. A pinned task does nothing.
What was started is history (`dispatch:fix-issue`, `dispatch:review`).

### The graph

Unfolding a task with GitHub links shows them as flat rows of pills, each row what must land before
what, left to right:

```
( #1234 ) ──▶ ( #4321 )        issue #1234 is closed by PR #4321
( #31 ) ──▶ ( #25 )            PR #25 is stacked on #31 (its base branch is #31's head), or the grouper said #31 blocks it
( #1240 ) ──▶ ( + )            no PR closes issue #1240 yet
```

A pill's dot is its state as of the last sync. A pill opens its item; `+` starts the same fix agent
for that issue at once, outside the cap, as the Fix-issues wizard's single-issue press does. A PR
pill shows **agent** while an agent works on it and **queued** while one waits for a slot; for an
issue, the `+` after it does. Rows whose items are all merged
or closed come last.

## Statuses and reason tags

The first rule that holds decides the status:

| Status | When | Reasons |
|---|---|---|
| **paused** / **done** | you pinned it | `pinned` |
| **open** | it has no PR or issue links | `no-github-links` |
| **done** | every linked PR and issue is merged or closed | `all-closed` |
| **blocked** | an open PR fails CI, has changes requested, or conflicts | `#12:ci-failing`, `#12:changes-requested`, `#12:conflicts`, one per cause |
| **in-review** | an open PR is ready for review | `#12:approved` / `#12:awaiting-review` per ready PR |
| **in-progress** | otherwise | `#12:draft` / `#12:open` per open item, `#12:missing`, `#12:unsynced` (not fetched yet) |

A task that is neither done nor paused also gets `stale` once its last activity is more than seven
days old: its newest event, which the task keeps even after the history has dropped it. Items
outside the watched repo read `owner/repo#12`.

## Where it keeps things

`~/.diplomat/tasks/` (`DIPLOMAT_TASKS_DIR` moves it), readable by you alone:

- `tasks.json` - the tasks: title, tags, links, pin, archived, the links you removed, and when
  its newest event happened.
- `events.jsonl` - the history, one event per line. Past 2 MB it keeps the newest lines that fit in
  1 MB, and always at least the newest one.
- `mirror.json` - what the last sync saw of each linked item, and how far into Comms' log it read.
  Delete it and the next sync sees every item for the first time again.
- `grouper.json` - when the grouper last ran, and its error. Delete it and the next tick runs it.
- `tasks.lock` - held by every writer, the app's and the CLI's alike.

The tunables are in `assets/tasks.json`. `DIPLOMAT_TASKS_SECS` overrides the five-minute sync (one
minute to a day), `DIPLOMAT_GOALS_SECS` the grouper's six hours (ten minutes at least).

## The CLI, for agents and scripts

`diplomat-core` reads and writes the same files, JSON in and out, so an agent can keep a task up to
date while it works. It is not on `PATH`:

- **Linux** - the installer builds it to `~/.local/share/diplomat/diplomat-core`
  (`packages/diplomat-platform/linux/install/build-core.sh`).
- **macOS** - the app links the core and ships no CLI; build it from the checkout (`~/.diplomat/checkout`
  when installed with `szpont`): `swift build --package-path <checkout>/packages/diplomat-core
  --product diplomat-core`, then run `<checkout>/packages/diplomat-core/.build/debug/diplomat-core`.

It reads its tunables from `$DIPLOMAT_CORE`, else from `assets/` or `packages/diplomat-core/assets/`
under the working directory, else from the checkout it was built in. Run from inside another
checkout, or after its own moved, it needs `DIPLOMAT_CORE=<checkout>/packages/diplomat-core/assets`.

```sh
diplomat-core tasks-list [--archived] [--me LOGIN]  # every task: status, reasons, history (newest 200), linkStates, graph, work
diplomat-core tasks-sync                   # one sync: {"ok":true,"events":3}, or ok:false and the error
diplomat-core tasks-group [--if-due]       # one grouper run: {"ok":true,"created":1,"added":0};
                                           # not due yet: {"ok":true,"skipped":"not due","ranAt":…,"lastError"?}
echo '{"op":"create","title":"Tor transport","tags":["area:mesh"],"links":["#1601"]}' \
  | diplomat-core tasks-edit               # prints the task as it now is
```

`tasks-sync` exits 0 either way. It can be `ok:true` with an `error` too: the links GitHub refused
(`skipped o/r#7: ...`), left as they were while the rest synced.

`tasks-edit` takes one `op` on stdin: `create` (`title`, `tags`, `links`), `update` (`id`, and any of
`title`, `tags`, `pinnedStatus`: `done`, `paused`, or `""` to unpin), `link` / `unlink` (`id`, `url`),
`note` (`id`, and any of `text`, `tags`, `url`), `archive` / `unarchive` (`id`), `mode` (`id`, and
any of `mode`: `observe`, `auto` or `dismissed`, and `reviewOnPush`), `dispatched` (`id`, `work`: the
`key`s of `work` items just started). `work` is what an auto task wants started: `fix-issue`,
`review`, and `findings` for `LOGIN`'s own PRs. An unknown op, key
or task, a note over 4000 characters, or an edit that does not apply exits 1 with the reason on
stderr. An edit saved whose history line could not be written exits 0 with the task, and a warning
on stderr: do not repeat it. `DIPLOMAT_TASKS_DIR`, `DIPLOMAT_COMMS_DIR` and `DIPLOMAT_GH` apply. The
screen picks up a CLI edit on its next sync, or when it is opened.

## Limits

- A sync reads the newest 20 comments and 20 reviews of each item; more than that between two syncs,
  and the older ones are never told.
- A closing reference is followed one hop per sync: the issue a PR closes is linked now and fetched on
  the next sync.
- Slack is only what Comms saw: with Comms off, or for a message that was not a ping, nothing reaches
  a task. The first sync only marks where Comms' log is; earlier pings are not added.
- A ban takes a sender's pings out of a task's history only while Comms' own log still holds them
  (it keeps its newest 1-2 MB): an older one stays.
- GitHub only: other forges are plain links, with no state and no history.
- A link chip's dot is what the last sync saw, so a link added since has none yet.
- Two syncs at once (the app's and an agent's `tasks-sync`): an item the other one wrote while this
  one was fetching is left as the other wrote it, and catches up on the next sync.
