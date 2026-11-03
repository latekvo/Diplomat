# Actions

The four spawn-an-agent cards in the panel's grid.

## Review PRs

The grid carries a **Review PRs** card alongside the tools. Click it and the wizard
opens where the PR lists normally render; dial in a few choices and hit **SPAWN
AGENT**.

A **specific PR** is one agent: a fresh terminal window (the
[Spawn terminal](settings.md#spawn-terminal) picked in Settings, or the first that can be
driven when that one can't - Ghostty needs tmux) running a detached review session in
your **repo root** (Settings;
default `~/dev/<repo>`) that you watch and steer yourself. The prompt is staged to
a file and the window runs
`<agent> [--settings <hooks>] "$(cat <promptfile>)"; printf %s $? > <done>` - where
`<agent>` is the [agent runner](settings.md#agent-runner) you picked, `<hooks>` is how a Claude
Code run reports its own turn boundaries, and the trailing sentinel
(`~/.diplomat/agents/<run-id>/done`) is how the Agent-tasks list knows a run that
*exited* is over.

A **whose-PRs sweep** is not one agent for all of them. It queues one review per PR
it covers, so the task cap starts them a few at a time and each is a row you can
reorder, run ahead of the rest, or cancel - fifty drafts are fifty reviews, not one
session told to work through fifty. (Which is also why the mesh row is offered for a
single PR only: a sweep opens no session to place.) The choices are baked into each
prompt:

- **Target** - the same three-way selector the other wizards use: *Mine* (the
  resolved handle, see Settings), *Someone else's* (a handle field lights up), or
  *Specific PR* (a number/URL field lights up - review exactly that one).
- **Scope** - *Review draft PRs* and *Review ready-for-review PRs* (both on by
  default; hidden for a specific PR, which is already one exact PR). Untick both
  and SPAWN greys out - there'd be nothing left to review.
- **Review depth** - a slider from a quick static read → standard swarm →
  swarm + hard reproductions → full E2E, swarming until one clean pass.
- **Mark clean PRs ready for review** - *(never on someone else's)* flip
  perfectly-clean drafts to ready.
- **Leave reviews (CLAUDE.md format)** - *(never on my own)* post formal per-line reviews;
  off, the findings stay in the agent's summary (the verdict pass below still posts them).
- **Reply to others' review threads** - *(never on someone else's)* answer and resolve open threads.
- **Soft-approve clean PRs (thank-you review)** - *(on by default; never on my own,
  and dropped when the verdict pass below is on)* on a perfectly-clean PR, submit a
  body-only COMMENT review saying what was run and thanking the author - never an
  APPROVE. It posts even with *Leave reviews* off.
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

## Fix issues

The second card, **Fix issues**, points detached agents at the repo's open *issues*
rather than its PRs (prompt model in `assets/issues.json`). One agent works one
issue: it reproduces that issue, fixes it, and re-runs the same reproduction to prove
the fix lands; anything it cannot reproduce is reported as that, never guessed at.

A **scope** is not one agent for all of them, exactly as a whose-PRs sweep is not. It
queues one fix per issue it covers, so the task cap starts them a few at a time and
each is a row you can reorder, run ahead of the rest, or cancel - forty open issues
are forty fixes, not one session told to work through forty. (Which is also why the
mesh row is offered for one hand-named issue only: a scope opens no session to place.)
The [new-community-issue monitor](automation.md#autonomous-monitors) queues the same fix
for each issue the community files, with the defaults below. Each queued fix carries the
same choices you made, aimed at its own issue, and re-reads that issue when its turn comes - hours can pass first, so a fix whose issue
has been closed or claimed since stops there and reports it as skipped.

Which issues is a six-way scope selector - wider than the three-way whose-PRs
axis the other wizards share, because an issue's **author association** is a scope
in its own right:

- **All open issues** - the whole open list.
- **Mine** - issues opened by the resolved handle (see Settings).
- **Someone else's** - a handle field lights up; that user's issues.
- **Contributors** - everything filed from *outside* the org (author association
  anything but `MEMBER` / `OWNER`, per `assets/filters.json`).
- **Org members** - everything filed from *inside* it.
- **Specific issue** - a number/URL field lights up; do just that one, on the spot,
  with no queueing. It takes an issue link, not a PR link: the two differ by one path
  segment, and a PR pasted here would otherwise be worked as the issue of the same
  number.

The scope never reaches an agent - it is applied here, against the issue list the
panel last fetched, and all it decides is which issues get queued. The two
association scopes cost nothing extra for it: the author association comes back with
every issue in that fetch already.

- **Fix depth** - a slider from a quick read-only pass → reproduce & fix →
  swarm the review moves over your own fix → drive the bug in the real app,
  swarming until one clean pass.
- **Only unassigned issues** - *(on by default; hidden for one hand-named issue)*
  an issue that already has an assignee is somebody else's to work, so it is never
  queued - and the fix re-checks its issue's assignees when its turn comes, because
  somebody may have claimed it in the meantime.
- **Assign each issue to me while working it** - *(on by default)* claim the issue
  on GitHub *before* the work starts, and hand it back if the run abandons it.
  This is what keeps two agents off one issue: a fix is deliberately not PR-scoped,
  so the dispatch pipeline's PR dedup does not apply, and the assignee is a claim
  every machine can see rather than only this one.
- **Open a draft PR per fix** - *(on by default)* one focused draft PR per fix,
  closing its issue (`Fixes #n`), carrying a regression test that the unfixed code
  would fail, and checked against the repo's open PRs by real `gh pr diff` content
  so it never files a duplicate. Off = nothing reaches the remote.
- **Comment the outcome on the issue** - *(on by default)* one comment per issue
  actually worked: what was reproduced, the cause, and where the fix is.
- **✨ Also take on feature requests** - *(highlighted, off by default)* by default
  only real bug reports are worked and every feature request, question and
  wishlist item is skipped. Ticked, enhancements are in scope too, but held to a
  different standard: only where the issue says clearly enough what is wanted, and
  anything vague or amounting to a redesign is left for you to decide.

> Preview the assembled prompt without launching anything:
> ```bash
> DIPLOMAT_PRINT_PROMPT=issues swift run Diplomat   # one issue out of a sweep; also: =issues-single (named by hand); append -features
> ```

## Resolve conflicts

A third grid card, **Resolve conflicts**, spawns a detached agent the same way
(fresh terminal, staged prompt + done sentinel, in the same repo root from
Settings) but for keeping
branches merge-able. A single three-way selector picks *whose* PRs to sweep:

- **Mine** - every currently-open PR authored by the resolved handle (see Settings).
- **Someone else's** - a handle field lights up; sweep that user's open PRs.
- **Specific PR** - a PR-number field lights up; do just that one.

For each PR it merges the latest `origin/main` into the branch. **Clean merges are left
untouched** - only where the merge *conflicts* does it resolve every conflict and push the
merge commit back to the PR's remote branch. The exception is a merge git completes cleanly
while GitHub still reports the PR conflicting: that merge is pushed, since GitHub keeps
flagging the PR until its head contains main. The contextual field (handle / PR number)
appears only for the target it applies to.

> Preview the assembled prompt without launching anything:
> ```bash
> DIPLOMAT_PRINT_PROMPT=conflicts-mine swift run Diplomat   # also: =conflicts-user, =conflicts-single
> ```

## Full E2E test

The fourth card spawns a whole-repo audit: a swarm end-to-end tests every module,
flow, build and test in the target repo, hard-reproducing every HIGH / MEDIUM
finding before reporting it (prompt model in `assets/audit.json`). Every confirmed
finding is **classified HIGH / MEDIUM / LOW** by real impact, and that label rides
through to the report - that part is always on. The proof scales with the label:
a HIGH or MEDIUM earns the full first-hand device reproduction, a LOW earns one
short adversarial check and never a booted device or a swarm, and a nitpick is
dropped on sight without being verified at all. By default the run only finds
and reports; nothing is changed. Two escalation toggles widen the blast radius:

- **Open PRs for every finding** - one focused PR per fix, **always as a draft**,
  and only after checking the repo's open PRs (by real `gh pr diff` content, not
  titles) so it never files a duplicate. PRs are severity-gated: HIGH and MEDIUM
  always get one, a LOW only when its fix is under 20 lines of diff - anything
  bigger is reported, not PR'd. Off = a strictly read-only audit.
- **Also fix open bug issues** - reproduce + fix the repo's open BUG issues, never
  feature requests.

> Preview: `DIPLOMAT_PRINT_PROMPT=audit swift run Diplomat` (also `=audit-issues`, `=audit-prs`, `=audit-all`).
