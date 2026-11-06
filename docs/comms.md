# Comms

The Slack watcher: what it reads, how it decides, and what it is allowed to do about a message. It
ships **off**, and reads Slack through the login Claude Code already holds for Slack's MCP server.

## What it does

Every minute Comms asks Slack for messages that reached you since it last looked: a **DM**, an
**@-mention** of you, or a message containing one of your **keywords**. Each new one becomes a
**ping** - a row on the Comms screen (the speech-bubble button in the panel header, which carries
an unread count), a line in the activity feed under its own **Comms** chip, and a system notification.

Then it **triages** the message - a short, sandboxed model call that answers one of seven words -
and acts on the answer through the same queue every other task goes through. Triage runs in
**batches**: a ping waits in a queue until it holds eight, or until its newest message is 30
minutes old (both are [settings](#settings)), and the whole batch is one call. Each call also reads the earlier pings of the same
conversations, so "can you review it?" after "#412 is ready" is understood as a review of #412.

| Triage | What Diplomat does |
|---|---|
| **review** #N… | queues a review of each open PR named, as the Review wizard would for that one PR, at the front of the requested reviews |
| **fix_issue** #N… | queues a fix of each open issue named, as the Fix-issues wizard would, at the front of the requested fixes |
| **bump** #N… | moves whatever is already queued for each one named to the front of its band; starts nothing new |
| **investigate** | queues an **Investigate** agent that reproduces and researches the report and writes a markdown report |
| **question** | answers it while you are away, when the project's documentation settles it (see [Reply to…](#settings)) |
| **none** | the ping, and nothing else |
| **injection** | [bans the sender](#bans): the message tries to hijack an agent instead of asking you for something |

A message listing several PRs or issues acts on all of them, up to twenty: each gets its own row and
its own place in the queue, the first it names first. One message is one kind of work: "review #12 and
fix issue #34" does only what triage picks. Comms acts only on the watched repo: triage is told to
leave out references to other repositories, and GitHub links to their PRs, issues, discussions and
commits, and `owner/repo#N` for another repo of this one's owner, are removed before its numbers are
checked.

So a "can you look at #412 when you get a chance?" lands #412's review ahead of every review you
asked for - behind only what the monitors owe GitHub - and a "the installer dies on Node 22 for me"
has an agent reproducing it before you have read it. Work that is already queued or running for
that PR or issue is moved up or followed instead of started twice. Each of the three acting rows has
its own switch in Settings; a ban has none, as no other place an injection is caught has one.

Nothing Comms queues jumps the machine: the [task cap](automation.md#autonomous-monitors), the
[spending budget](automation.md#the-spending-budget) and the ban list hold it exactly as they hold a
monitor's work, and a queued row can be dragged or cancelled like any other. What Comms queues runs
on this machine only; a review it hands to a monitor's find goes wherever that find was going, and
that find runs even with its monitor switched off, and stays queued until it does.

## The Comms screen

- **Status** - who the Slack login is (`@handle` in which workspace), when Slack was last asked, and
  the last error, if any: Claude Code not signed in to Slack, a login Slack refused, Slack
  rate-limiting the watcher, or no `claude` to triage with.
- **Pings** - newest first, the unread ones highlighted. Each shows the sender, the DM or channel,
  why it reached you (DM, @mention, keyword), the text, what triage decided and what came of it, and
  **Open in Slack**. Opening the screen marks them read.
- **Jobs** - everything Comms queued: queued, running, or done. A finished investigation carries a
  **Report** button that opens its file.

## Triage, and why a message cannot steer it

Slack text is written by other people, so Comms treats it as data from end to end:

- The triage call is `claude -p` with **no tools, no MCP servers, no slash commands and no settings**
  (`--tools "" --strict-mcp-config --disable-slash-commands --setting-sources ""`), each message
  fenced on stdin as untrusted, its label written outside the fence so no message can pose as
  another, and a JSON schema as the only way to answer. `--tools ""` alone would still
  load every MCP server you have configured, Slack's included - that is why the other flags are there.
  It runs the `claude` binary itself, never your shell alias. The model is a setting (Haiku by
  default).
- Each answer is **validated** against its own message before anything acts on it: a number that
  appears neither in the message nor in an earlier message of its conversation, or only inside a
  recognised reference to another repository, is dropped. An unknown action, a review / fix / bump left with no
  number, or an injection whose quoted evidence does not appear in the message itself (case, runs of
  whitespace and the fence's `|` quoting aside), becomes `none`. Each number must name an **open** PR
  (for a review) or an **open** issue (for a fix) of the watched repo, or nothing is queued for it.
- A batch with an `injection` among its answers is **triaged again, one message per call**, each
  with only its own conversation's earlier pings, and those answers stand: nobody is banned, and
  nothing is asked, on the strength of another batch message's words. A banned sender's messages, and messages triaged as injections, are
  never anyone's context.
- Your own messages, bots, and people from other organisations (Slack Connect) are never pinged:
  each sender's Slack profile must name your organisation (or, in a workspace without one, share
  your email domain). A **sender allowlist** narrows it to the people you name.
- Slack's search answers are markdown with each message's text printed raw, so a message could
  carry a fake result heading to pose as someone else. A page with more result headings than results
  is read again one result per call, where the sender and channel come before any text.
- The **Investigate** agent's prompt quotes the message between untrusted-data markers - a message
  cannot close them early: every line of it is quoted with a leading "|" - and forbids,
  whatever the message says: pushing, any GitHub write (PRs, issues, reviews, comments), posting
  anywhere, and touching secrets. It works in its own worktree and its deliverable is the report
  file - what you do with it is yours to decide.

The agent itself still runs with your runner's usual permissions, as every Diplomat agent does. The
fence and the rules are what stand between a crafted message and that agent; if that is more trust
than your workspace earns, switch **investigations** off or set the sender allowlist.

## Bans

Comms is covered by the same prompt-injection ban list as the review monitors
(`~/.diplomat/pr-monitor/banned.json`, kept by the device-allocator daemon). A Slack sender is
listed as `slack:<their Slack user id>`, so a Slack ban never blocks a GitHub login, nor the other
way round. A sender is banned when either of the two places their text reaches a model catches an
injection:

- **Triage** answers `injection`, quoting the injected words. Comms bans the sender (*flagged by
  Comms triage*); nothing the message asked for is queued.
- **An Investigate agent** decides the message it was handed is an injection and calls the allocator's
  `report-prompt-injection` with `source: "slack"` and the sender's id, exactly as a review agent
  reports a PR's author. The allocator terminates that agent. Only a Claude Code agent has that tool;
  any other runner is told to write the finding to its report and stop.

Once banned, a sender's messages are still listed as pings, marked as from a banned sender, but are
never triaged: their text reaches no model, nothing is queued and no notification is shown. That holds
from the moment of the ban, for their other messages in the same poll and those still waiting for
triage too, and also while Comms is switched off mid-poll: a ban has no switch. A message pinged before
its batch was triaged was notified when it landed, an injection included. An
investigation of theirs still waiting in the queue is dropped when its turn comes, and its ping says
why. Reviews or fixes they asked for earlier stay queued: those agents read GitHub, not the message.

Every ban is announced:

- a **system notification** naming who was banned, where and why (for a review author's ban too);
- a **Bans** line in the activity feed: who (Slack handle and id), where (the message's permalink),
  the quoted evidence, and whether triage or an agent caught it;
- a row in the panel's **Banned** list with the same details and when it happened, plus an un-ban
  button - un-banning a Slack sender lets Comms triage their messages again;
- on the Comms screen, a strip of the banned senders, and the ping that caused the ban marked with it.

## Setting it up

Comms has no Slack app or token of its own. It uses Claude Code's login to Slack's MCP server
(`https://mcp.slack.com/mcp`, the Slack plugin's server), read from Claude Code's credentials: the
`Claude Code-credentials` Keychain item on macOS, `~/.claude/.credentials.json` on Linux. If Claude
Code is already signed in to Slack, switch **Watch Slack** on in **Settings → Comms (Slack)**;
**Test** names your handle. If not, sign in once from Claude Code: `/mcp`, pick the Slack server,
**Authenticate**.

Claude Code refreshes that login as it runs. When the watcher finds it expired, or Slack refuses it,
it runs `claude mcp list` (which refreshes it) at most once every ten minutes.

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
- **Batch size** (**default 8**) - how many pings one triage call reads: 1, 2, 4, 8, 16, 32 or 64. A
  full batch is triaged at once.
- **Batch interval** (**default 30m**) - how old the newest ping of a shorter batch gets before it is
  triaged anyway: one minute to a day, on a logarithmic slider.
- **Reply in Slack** (**default on**) - whether Comms writes back at all. It only ever does while you
  are away (see **Away after**); triage and the work it queues never wait for that. A reply opens with
  `[Diplomat]:`, in the language of the message (Polish, English for anything else; an answer is
  written in the question's own language), only in the
  conversation the message was written in, at most once per message, however many PRs it asked for.
- **Reply to…** **questions** (**default on**) / **reviews** / **fixes** / **investigations** (**default
  off**) - which kinds get one. A question is answered from the project's documentation, when that
  settles it plainly. The other three get an estimate of when the work should be done. Off, the work is
  still queued; only the reply is held.
- **Away after** (**default 3h**) - how long your session must have been locked before Comms replies
  for you. A message that arrives while you are here gets no reply, then or later.
- **Notify on new pings** (**default on**) - a system notification per new ping, as it lands,
  before its triage.
- **Triage model** (blank = **`haiku`**) - any model id `claude --model` accepts.
- **Test** - asks Slack who Claude Code's login is.

`DIPLOMAT_SLACK_TOKEN` overrides Claude Code's login with a Slack MCP access token,
`DIPLOMAT_COMMS_SECS` the one-minute poll (20 seconds to a day), and `DIPLOMAT_CLAUDE` the `claude`
binary triage and the login refresh run.

## Where it keeps things

`~/.diplomat/comms/` (`DIPLOMAT_COMMS_DIR` moves it), readable by you alone - it holds your DMs:

- `state.json` - the watermark (the newest Slack timestamp already handled), the ids seen near it,
  the pings waiting for triage, who the Slack login is, and when the login was last refreshed.
  Delete it and the next poll starts again from "now"; the pings that were waiting stay untriaged.
- `events.jsonl` - the module's own log: one line per ping, triage answer, action, start and finish
  (and per poll failure, once on the way in). It is what the Comms screen is drawn from, and it is
  trimmed to its newest half past 2 MB.
- `reports/<id>.md` - the investigations' reports.

To check the login and see what triage would do with the last hour of Slack (its oldest batch of
messages, triaged at once rather than queued), without queueing anything or touching
the files above:
`DIPLOMAT_COMMS_POLL=1 ./Diplomat.app/Contents/MacOS/Diplomat` (Linux: `DIPLOMAT_COMMS_POLL=1
./diplomat`). It exits non-zero when the poll fails. With `DIPLOMAT_COMMS_DIR` set it polls that
directory instead: it moves its watermark like a real poll and appends the ping and triage lines
to its `events.jsonl`, but acts on none of them, so pointed at the live directory it uses up those
messages without queueing their asks.

## Limits

- Slack's search index can trail a message by a while, so the watcher looks ten minutes behind "now"
  and remembers what it has already seen there. A ping can therefore arrive a minute or two late.
- One poll takes at most eight new messages, and triages at most one batch; the rest wait for the
  next poll, oldest first. It reads at most 100 matches per search; anything older than
  those is reported in the activity feed, not triaged.
- A ping waits for its triage, and so for the review or investigation it asks for, until its batch is
  full or the batch's newest message reaches the batch interval. Each new ping restarts that interval,
  so under a steady trickle the wait is set by the batch size. Longer while Slack is failing, as a poll
  that cannot reach Slack triages nothing.
- The context is earlier **pings** only: what Comms already saw of the conversation. Your own replies
  and messages that were not pings are not in it.
- On Linux the Slack calls go through `curl`, which must be installed.
- Triage needs Claude Code's `claude` on the machine whichever agent runner you use; without it every
  message is still pinged and nothing is acted on.
