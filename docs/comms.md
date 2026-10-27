# Comms

The Slack watcher: what it reads, how it decides, what it is allowed to do about a message, and
how to give it a token. It ships **off**, and does nothing until it has one.

## What it does

Every minute Comms asks Slack for messages that reached you since it last looked: a **DM**, an
**@-mention** of you, or a message containing one of your **keywords**. Each new one becomes a
**ping** - a row on the Comms screen (the speech-bubble button in the panel header, which carries
an unread count), a line in the activity feed under its own **Comms** chip, and a system notification.

Then it **triages** the message - one short, sandboxed model call that answers one of five words -
and acts on the answer through the same queue every other task goes through:

| Triage | What Diplomat does |
|---|---|
| **review** #N | queues a review of open PR #N, as the Review wizard would for that one PR, at the front of the requested reviews |
| **fix_issue** #N | queues a fix of open issue #N, as the Fix-issues wizard would, at the front of the requested fixes |
| **bump** #N | moves whatever is already queued for #N to the front of its band; starts nothing new |
| **investigate** | queues an **Investigate** agent that reproduces and researches the report and writes a markdown report |
| **none** | the ping, and nothing else |

So a "can you look at #412 when you get a chance?" lands #412's review ahead of every review you
asked for - behind only what the monitors owe GitHub - and a "the installer dies on Node 22 for me"
has an agent reproducing it before you have read it. Work that is already queued or running for
that PR or issue is moved up or followed instead of started twice. Each of the three acting rows has
its own switch in Settings.

Nothing Comms queues jumps the machine: the [task cap](automation.md#autonomous-monitors), the
[spending budget](automation.md#the-spending-budget) and the ban list hold it exactly as they hold a
monitor's work, and a queued row can be dragged or cancelled like any other. Comms work runs on this
machine only; the mesh never sees it.

## The Comms screen

- **Status** - who the token is (`@handle` in which workspace), when Slack was last asked, and the
  last error, if any: a rejected token, a missing scope, Slack rate-limiting the watcher, or no
  `claude` to triage with.
- **Pings** - newest first, the unread ones highlighted. Each shows the sender, the DM or channel,
  why it reached you (DM, @mention, keyword), the text, what triage decided and what came of it, and
  **Open in Slack**. Opening the screen marks them read.
- **Jobs** - everything Comms queued: queued, running, or done. A finished investigation carries a
  **Report** button that opens its file.

## Triage, and why a message cannot steer it

Slack text is written by other people, so Comms treats it as data from end to end:

- The triage call is `claude -p` with **no tools, no MCP servers, no slash commands and no settings**
  (`--tools "" --strict-mcp-config --disable-slash-commands --setting-sources ""`), the message fenced
  on stdin as untrusted, and a JSON schema as the only way to answer. `--tools ""` alone would still
  load every MCP server you have configured, Slack's included - that is why the other flags are there.
  It runs the `claude` binary itself, never your shell alias. It costs about $0.001 and three seconds
  a message on Haiku; the model is a setting.
- The answer is **validated** before anything acts on it: an unknown action, or a review / fix / bump
  whose number does not appear in the message, becomes `none`. The number must name an **open** PR
  (for a review) or an **open** issue (for a fix) of the watched repo, or nothing is queued.
- Your own messages, bots, and people from other organisations (Slack Connect) are never pinged. A
  **sender allowlist** narrows it to the people you name.
- The **Investigate** agent's prompt quotes the message between untrusted-data markers - a message
  cannot close them early, however it spells the marker - and forbids,
  whatever the message says: pushing, any GitHub write (PRs, issues, reviews, comments), posting
  anywhere, and touching secrets. It works in its own worktree and its deliverable is the report
  file - what you do with it is yours to decide.

The agent itself still runs with your runner's usual permissions, as every Diplomat agent does. The
fence and the rules are what stand between a crafted message and that agent; if that is more trust
than your workspace earns, switch **investigations** off or set the sender allowlist.

## Setting it up

1. **Create a Slack app** for yourself at <https://api.slack.com/apps> (*From scratch*, your
   workspace). Under **OAuth & Permissions → User Token Scopes** add `search:read`, install it to
   the workspace, and copy the **User OAuth Token** (`xoxp-…`). A bot token cannot read
   your DMs and will not work.
2. **Store the token where Diplomat can read it - never in `config.json`**:

   ```bash
   # macOS: the login Keychain (Diplomat reads the item named in Settings, default diplomat-slack)
   security add-generic-password -a "$USER" -s diplomat-slack -w
   # Linux: a 0600 file (Settings names the path, default ~/.diplomat/slack-token)
   (umask 077; read -rs t; printf '%s\n' "$t" > ~/.diplomat/slack-token)
   ```

   `-w` last makes `security` ask for the token, so it stays out of your shell history.
3. **Settings → Comms (Slack)**: press **Test** (it should name your handle), add keywords if you
   want them, and switch **Watch Slack** on.

The first poll only records where "now" is: nothing already in Slack is triaged, so turning it on does
not unleash a day of old DMs. Later, a poll catches up on whatever arrived while Comms was off or the
machine asleep - up to 24 hours of it. Past that it starts again from "now" and says so in the
activity feed.

## Settings

All of them are on the **Comms (Slack)** card.

- **Watch Slack** (**default off**) - the watcher itself. Its pill reads *watching*, *off*, or the
  last error.
- **Keywords** - words or phrases, commas or new lines between them, that make a message a ping
  wherever you can read it. Matched as whole words, case-insensitive. Each one costs one more Slack
  search a poll.
- **Senders** - Slack handles or user IDs (`U…`); blank means anyone in your workspace, a list means
  only those people.
- **Act on asks for…** **reviews & fixes** / **bumps** / **investigations** (**default on**) - one
  switch per acting triage answer. Off, that answer is still shown on the ping, with which switch
  held it; nothing is queued.
- **Notify on new pings** (**default on**) - a system notification per new ping.
- **Triage model** (blank = **`haiku`**) - any model id `claude --model` accepts.
- **Token Keychain item** (macOS) / **Token file** (Linux) - where the token is read from, and
  **Test**, which asks Slack who it is.

`DIPLOMAT_SLACK_TOKEN` overrides the stored token, `DIPLOMAT_COMMS_SECS` the one-minute poll
(20 seconds to a day), and `DIPLOMAT_CLAUDE` the `claude` binary triage runs.

## Where it keeps things

`~/.diplomat/comms/` (`DIPLOMAT_COMMS_DIR` moves it), readable by you alone - it holds your DMs:

- `state.json` - the watermark (the newest Slack timestamp already handled), the ids seen near it, and
  who the token is. Delete it and the next poll starts again from "now".
- `events.jsonl` - the module's own log: one line per ping, triage answer, action, start and finish
  (and per poll failure, once on the way in). It is what the Comms screen is drawn from, and it is
  trimmed to its newest half past 2 MB.
- `reports/<id>.md` - the investigations' reports.

To check a token and see what triage would do with the last hour of Slack, without queueing
anything or touching the files above: `DIPLOMAT_COMMS_POLL=1 ./Diplomat.app/Contents/MacOS/Diplomat`
(Linux: `DIPLOMAT_COMMS_POLL=1 ./diplomat`). It exits non-zero when the poll fails.

## Limits

- Slack's search index can trail a message by a while, so the watcher looks ten minutes behind "now"
  and remembers what it has already seen there. A ping can therefore arrive a minute or two late.
- One poll triages at most eight messages; the rest wait for the next poll, oldest first. It reads at
  most 300 matches per search; anything older than those is reported in the activity feed, not
  triaged.
- On Linux the Slack calls go through `curl`, which must be installed.
- Triage needs Claude Code's `claude` on the machine whichever agent runner you use; without it every
  message is still pinged and nothing is acted on.
