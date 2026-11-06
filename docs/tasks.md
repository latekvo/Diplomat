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
| a linked PR closes an issue, or a linked issue is closed by a PR, the task does not link yet | `linked`, `via:closes` - it becomes a link |
| GitHub no longer has it (or your login cannot see it) | `link:missing`, once |

Comments and reviews already there when an item is first linked are not replayed, so linking an old
PR adds one line, not its whole thread.

The same sync folds in [Comms](comms.md): a Slack ping that names one of the task's links (a GitHub
URL, `owner/repo#12`, a `#12` of the watched repo, or the Slack link itself) joins its history with
the sender, the channel or `dm`, and what Comms triage made of it (`triage:review`, ...). Nothing new
is asked of Slack. A ping waits for its triage, up to two days, before it is added without one; a
banned sender's ping, or one triaged as an injection, is never added.

Your own edits are history too: `created`, `tag:+x` / `tag:-x`, `link:+` / `link:-`, `pinned:done`,
`unpinned`, `archived`, and the notes you add.

## The Tasks screen

The checklist button in the panel header opens it; its red badge counts the blocked tasks.

- **Status line** - when the last sync finished and how often it runs, then the last sync error, if
  any: a `gh` failure (nothing is written, the task files stay as they were) or links it had to skip.
- **Tasks** - not-done ones first, then by their newest event. Each row shows the status, the title,
  the reason tags, the task's own tags, and its links as chips: `#123` for the watched repo,
  `owner/repo#12` elsewhere, `slack`, or the site's host name. A GitHub link carries a dot for its
  state (open, draft, merged, closed, missing) as its history last told it. Every chip opens in the
  browser.
- Click a row to unfold it: its actions and its **history**, newest first - when, where from (GitHub,
  Slack, a note, Diplomat), the item it is about, its tags and its excerpt. A line with a link opens
  it. The newest twelve are listed; the rest are a click away.
- **Actions** - **New task** (a title, tags, and links one per line), and per task **Add link**,
  **Add note** (text and tags), **Edit tags**, **Pin done** / **Pin paused** or **Unpin**, and
  **Archive** / **Unarchive**. **Show archived** lists the archived ones. **Sync now** syncs at once.

A link can be a PR or issue URL (anything after the number is dropped), `owner/repo#12`, `#12` (the
watched repo), a Slack permalink, or any `http(s)` URL. `owner/repo#12` and `#12` are stored as
issues until the first sync says they are PRs. Tags are lowercased, spaces become `-`, and repeats
are dropped; in the screen's fields they are separated by spaces or commas.

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

A task that is neither done nor paused also gets `stale` once its newest event is more than seven
days old. Items outside the watched repo read `owner/repo#12`.

## Where it keeps things

`~/.diplomat/tasks/` (`DIPLOMAT_TASKS_DIR` moves it), readable by you alone:

- `tasks.json` - the tasks: title, tags, links, pin, archived.
- `events.jsonl` - the history, one event per line; trimmed to its newest half past 2 MB.
- `mirror.json` - what the last sync saw of each linked item, and how far into Comms' log it read.
  Delete it and the next sync sees every item for the first time again.
- `tasks.lock` - held by every writer, the app's and the CLI's alike.

The tunables are in `assets/tasks.json`. `DIPLOMAT_TASKS_SECS` overrides the five-minute sync (one
minute to a day).

## The CLI, for agents and scripts

`diplomat-core` (built by `swift build` in `packages/diplomat-core`) reads and writes the same files,
JSON in and out, so an agent can keep a task up to date while it works:

```sh
diplomat-core tasks-list [--archived]      # every task, its status, reasons and history (newest 200)
diplomat-core tasks-sync                   # one sync: {"ok":true,"events":3}, or ok:false and the error
echo '{"op":"create","title":"Tor transport","tags":["area:mesh"],"links":["#1601"]}' \
  | diplomat-core tasks-edit               # prints the task as it now is
```

`tasks-edit` takes one `op` on stdin: `create` (`title`, `tags`, `links`), `update` (`id`, and any of
`title`, `tags`, `pinnedStatus`: `done`, `paused`, or `""` to unpin), `link` / `unlink` (`id`, `url`),
`note` (`id`, and any of `text`, `tags`, `url`), `archive` / `unarchive` (`id`). An unknown op or
task exits 1 with the reason on stderr. `DIPLOMAT_TASKS_DIR`, `DIPLOMAT_COMMS_DIR` and `DIPLOMAT_GH`
apply. The screen picks up a CLI edit on its next sync, or when it is opened.

## Limits

- A sync reads the newest 20 comments and 20 reviews of each item; more than that between two syncs,
  and the older ones are never told.
- A closing reference is followed one hop per sync: the issue a PR closes is linked now and fetched on
  the next sync.
- Slack is only what Comms saw: with Comms off, or for a message that was not a ping, nothing reaches
  a task. The first sync only marks where Comms' log is; earlier pings are not added.
- GitHub only: other forges are plain links, with no state and no history.
- A link chip's dot comes from the task's history, so a link whose events have aged out of the
  newest 200 has none.
- Removing a link is CLI-only (`unlink`).
