# Diplomat — Linux applet (Qt6 / PySide6)

The Linux port of the macOS menu-bar wrench: a **system-tray applet** with the
same dense panel of six `software-mansion/argent` triage tools, reverse lookup,
the Review-PRs / Resolve-conflicts / Full-E2E-test wizards, the full autonomous
monitor stack, a Devices view of the device-allocator pool, the Mesh topology
screen, and settings. It's a thin UI renderer over the shared [`assets/`](../../diplomat-core/assets)
assets — and it doesn't re-implement prompt assembly at all: it shells out to the
`diplomat-core` Swift binary, so the two front-ends are identical by construction.

**Still macOS-only:** the per-row **Merge** button; the **clickable session rows** of
the [Agent tasks](#agent-tasks) list — a spawn here is a detached `Popen` with no
window handle to track it by, so a running agent gets a row but no window to focus,
and none of the *awaiting input* / *done* / *merged* statuses that come from reading
a session's terminal. Also macOS-only: reading/typing into *arbitrary* terminal windows — Linux
has no portable hook for that, so the API-error watcher drives **tmux panes**
instead. Every SPAWN opens its agent in a tmux session of its own wherever tmux is
installed, so the watcher reaches them; an agent started any other way is outside it.

Universal across desktops via Qt6's `QSystemTrayIcon` (StatusNotifierItem /
XEmbed): works on **XFCE** (Notification Area / Status Notifier panel plugin),
**KDE**, and **GNOME** (with an AppIndicator extension).

## Agent tasks

The left pane heads with what this machine is doing, what it is about to do, and how
much room it has left: the automatic agents it is **running**, the work it is
**holding**, and an empty bay per free slot of its
[task cap](../../../README.md#autonomous-monitors). The section is always there. Its
header counts the tasks and captions them with the machine's state — an idle one with
a cap of two reads `0`, `2 free`, over two empty bays; a saturated one with three
more owed reads `5`, `3 queued`, over two running agents and the queue behind them.

Rows read in the order of the bay's own filling: **running**, **starting**, **free
slot**, then the **queue** that has no bay yet.

- **Running** is an automatic agent up on this machine, drawn in the bay it took,
  with the label its dispatch logged and how long it has been going. A spawn here is
  a detached `Popen` in a terminal the applet does not own, so the row is a status
  and nothing more — there is no window to click, and none of the *awaiting
  input* / *done* / *merged* macOS reads off a session. A job the mesh placed **back
  on this machine** is one of these like any other; one it placed on a peer shows in
  the activity feed instead, having taken no bay here.

  An agent found only by the `ps` scan — no in-flight record behind it, which is
  what an applet restart leaves — still gets a row, marked *untracked* and drawn by
  its PR number, because it still holds a bay for as long as it runs.
- **Starting** is a task between the queue and its agent: the click (or the drain)
  has taken it, and the spawn has not answered yet. Seconds, and a row for all of
  them, so *execute now* never reads as the click deleting the task. It holds a bay
  from the moment it starts, and the running row it becomes takes its place.
- **Free slots** are the rest of the cap. Each running automatic agent takes one —
  including a job the mesh placed back on this machine, which is a `claude` process
  here whoever opened its terminal, and a review from a sweep, which the queue
  merely picked the moment for; an agent a wizard press opened on the spot takes
  none. Queued work starts here on the next poll.
- **Queued** rows are auto-fixes, auto-reviews and reviews you asked for that nothing
  has started yet. Each
  carries **execute now** — start it immediately, past whatever is holding it — and a
  drag grip: drop a row on another to set the order the queue runs in. That order is
  honoured at the top of the next poll, *before* the monitors go looking for more
  work, so a slot that just freed goes to whatever you put first rather than to
  whichever PR GitHub happened to list first.
- **Requested reviews** — one row per PR of a Review-PRs sweep — run after every
  auto-fix and auto-review and before the conflict fixes, because what the monitors
  find is a debt other people can see and a sweep is work you started when you had
  the time for it. Each also carries **cancel**: you asked for it, so nothing else
  will ever take it off the list.
- **Resolve-conflicts** rows run last of all, whatever order
  the monitors found them in, and no drag lifts one out of its band (that drag is
  refused rather than sprung back on the next poll). An agent working the same branch
  lands its own merge on the way, so a conflict fix is the work most often made
  unnecessary by the work ahead of it — and the poll re-offers it for as long as
  GitHub still calls the PR conflicting, so waiting costs it nothing.

Three things hold work. The cap holds what there is no slot for, and releases it as
slots free. The [rate-limit budget](../../../README.md#the-rate-limit-budget) holds
everything when the account is too low to afford another agent, and releases it when a
window refills. A **monitor you switched off** holds its own work indefinitely: it
keeps polling and keeps listing what it finds (reading *queued · monitor off*), but
nothing starts by itself — only *execute now* does. So the toggles decide who starts
the work, not whether you get to see it, and turning both off does not stop the
3-minute GitHub poll.

The queue is a view of what the monitors would re-offer, not a second copy of their
state: it is rebuilt from live GitHub evidence on every poll, so a task drops out the
moment the work is taken by an agent, resolved, or its author banned. Every poll also
re-checks the rows it is about to run against the fetch it has just made — a conflict
fix on a PR GitHub no longer calls conflicting, or a reply on threads that have been
answered, leaves the list instead of opening an agent on work somebody already did. The key
order is remembered (in `QSettings`), so your arrangement survives the rebuild and a
restart. *Execute now* keeps the task automatic in every other
respect: same label, same auto-handled counter, same retry record, and once
running it occupies a slot like any other automatic agent, so the rest of the queue
waits behind it.

The exception is the reviews **you** ask for by sweeping your PRs in the Review
wizard, which queues one review per PR instead of handing every draft to a single
agent. Nothing on GitHub records that a PR was swept, so those asks are remembered
(`QSettings`, beside the arrangement) and re-offered every poll until each is
dispatched, they carry your own label rather than `Auto · `, and each row has a
**cancel** beside *execute now* — short of a ban on the PR's author, nothing else
will ever retire one. They wait in a
band between the monitors' finds and the conflict fixes, so a fifty-draft sweep never
holds up a review GitHub is already owed.

The rules the list obeys — the queue key, the arrangement, one drag, the free-slot
count — are `AgentTaskQueue` in
[`AgentTasks.swift`](../../diplomat-core/Sources/DiplomatCore/AgentTasks.swift), with
a twin in `autofix.py` that the tests on both sides pin case for case. What macOS has
on top is `AgentTaskStatus`, the sort that files spawned sessions above the bays;
here there are no session rows to sort, so the list starts at that order's *starting*
— what is spawning, then the bays, then the queue.

## Requirements

- Python 3.10+
- PySide6 (`pip install -r requirements.txt`)
- GitHub CLI `gh`, authenticated (`gh auth login`)
- The `diplomat-core` binary for prompt assembly — build it with
  `./install/build-core.sh` (needs a Swift toolchain once), or point
  `DIPLOMAT_CORE_BIN` at a prebuilt one
- A terminal emulator for the wizards' SPAWN (auto-detected:
  `x-terminal-emulator`, `xfce4-terminal`, `gnome-terminal`, `konsole`, `kitty`,
  `alacritty`, `xterm`)
- **tmux**, strongly recommended. A spawn runs the agent under your interactive
  shell, so your rc is sourced and a `claude` alias resolves — which also means an
  rc that starts every terminal inside tmux (`exec tmux new-session`, guarded on an
  empty `$TMUX`) would replace the agent's command with an empty session. Diplomat
  opens that session itself, which satisfies the guard and is also the only way the
  API-error watcher can see the agent. Without tmux the agent runs directly under
  `$SHELL -i`, and such an rc swallows it
- Optional: the `cryptography` package, for mesh device keys

## Run

```bash
cd linux
pip install -r requirements.txt
./diplomat                 # tray applet (left-click the wrench)
```

Quit from the panel's ⏻ button, the tray right-click menu, or `pkill -f "python -m diplomat_app"`.

## Autostart on login

```bash
./install/install-autostart.sh    # XDG autostart .desktop + the 6AM update timer, starts it now
./install/uninstall-autostart.sh  # removes both and stops the app
```

Installs `~/.config/autostart/diplomat.desktop` so the wrench reappears every
login (the cross-desktop analogue of the macOS LaunchAgent).

It also installs a **systemd user timer** (`diplomat-update.timer`) that fires
daily at **06:00** and runs the launcher headless (`DIPLOMAT_SELF_UPDATE=1`):
fetch, merge if behind, rebuild `diplomat-core`, and relaunch the tray only if
one is running. `Persistent=true`, so a 06:00 missed while the machine was off
runs at the next boot. Without `systemctl` the install warns and carries on —
only the schedule is lost, the Settings ▸ UPDATE button still works. Manage it
alone with `./install/install-autoupdate.sh` / `./install/uninstall-autoupdate.sh`.

## Settings

A two-pane screen matching macOS. Most persist via `QSettings`
(`~/.config/diplomat/…`); the repo root, the automatic-task cap and the
rate-limit budget's three knobs are the exceptions (see their bullets):

- **GitHub username** — overrides the `gh`-authenticated login for the "My …" tools.
- **Repo root** — the local checkout every spawned agent `cd`s into, with a
  **Browse…** picker. Blank = `~/dev/<repo>` for the repo `assets/config.json`
  targets; `DIPLOMAT_REPO` still outranks both. The hint warns when the path isn't
  absolute, or has no `.git` (the spawn's `cd` is best-effort, so the agent would
  otherwise start in `$HOME` unnoticed). Not in `QSettings`: it lives in the
  shared `~/.diplomat/config.json` (`appconfig.py`) because a mesh node is a
  separate, stdlib-only process with no Qt — so a job that lands over the mesh
  uses the same checkout, and a *running* node picks up a change on its next
  spawn.
- **PR auto-fix / Full-E2E review requests** — the two monitor toggles with live
  status, and under the review-requests one the **auto-approve** master toggle
  plus its three withhold-the-verdict suppressors (SKILL / installer / community),
  and the **soft-approve** toggle (default ON — a clean comments-only review leaves
  a friendly thank-you note, never an APPROVE action). A monitor switched off keeps
  polling and keeps listing what it finds under [Agent tasks](#agent-tasks); what
  stops is the automatic start.
- **Run at most N automatic tasks at a time** — this machine's hard cap on
  concurrent automatic agents (default **2**, range 1–16), spanning both monitors
  above, the reviews a PR sweep queues, and any work a mesh peer routes here. The
  agent a wizard press opens on the spot is never capped and doesn't count against
  it; work over the cap is not dropped — it waits in the [Agent tasks](#agent-tasks)
  list, in the order you put it, and whatever is left of the cap shows there as empty
  slots. In `~/.diplomat/config.json` for the same reason as the repo root — the node
  that runs peer-routed work has no Qt, and a machine with two answers to "how many
  at once" has no cap at all.
- **Hold automatic work when the rate limit runs low** — the
  [rate-limit budget](../../../README.md#the-rate-limit-budget) (default **on**),
  with the confidence it must reach that a task fits (default **95%**) and the share
  of a window to keep in hand while the ledger cannot price one yet (default
  **20%**). Priced from the same per-task figure the Telemetry screen shows, against
  both rate-limit windows. Held work waits under [Agent tasks](#agent-tasks) and
  starts when a window refills; *execute now* overrides it, a wizard spawn that
  opens a terminal on the spot is never gated, and nothing is held at all while the
  usage probe cannot read a window. In `~/.diplomat/config.json` for the same reason
  as the cap above.
- **Claude API errors** — the tmux watcher toggle, plus a count of nudges sent.
- **Tools — colour & visibility** — retint or hide any tool card. (SKILL.md PRs
  and Installer/CLI PRs ship hidden.)
- **Spawn terminal** — which terminal SPAWN AGENT opens.
- **Device allocator (MCP)** - install/uninstall the bundled allocator daemon +
  MCP server (drives the Node installer in `../../device-allocator/`), with status.
- **Mesh (LAN P2P)** — start/stop the local mesh node (off by default), with live
  node/peer status; the mesh itself is managed from the ⬡ Mesh screen.
- **Update** — bring this checkout up to the latest GitHub commit (merging when
  you have local commits of your own), rebuild the `diplomat-core` binary, and
  relaunch the tray app in place (the fresh instance replaces the running one,
  newest-wins).

## Diplomat Mesh (experimental)

The applet can coordinate duties with the other machines on your LAN — see the
[root README's Mesh section](../../../README.md#diplomat-mesh-experimental--lan-p2p-duty-coordination)
for the model. Enable it in ⚙ Settings; the panel then grows a collapsible
topology column (live nodes, link states, per-node tier/token editors, per-duty
strategy controls). The node is [SzpontNet](../../szpontnet-core/README.md), an independent
standard-library-only library the applet registers itself behind; it runs headless
anywhere.

It is genuinely an **add-on**. With the library absent the applet starts and does
everything that doesn't span machines; the ⬡ button, the Mesh screen and the
topology poll are simply not built, and ⚙ Settings shows the toggle disabled with
"SzpontNet not installed" rather than "Off". Nothing imports the library at module
scope, and `tests/test_addon_optional.py` proves it by blocking the import in a
real subprocess and rendering the panel anyway.

```bash
# From this directory. `SZPONTNET_HOST` is what puts Diplomat behind the node —
# without it the node runs SzpontNet's own defaults (canonical v1 duties, state in
# ~/.szpontnet, no activity feed) and joins a different mesh than the applet's.
export SZPONTNET_HOST=diplomat_runtime.szponthost \
       PYTHONPATH=../../diplomat-runtime:../../szpontnet-core

python3 -m szpontnet --daemon     # join the mesh (works on macOS too, no Qt)
python3 -m szpontnet --status     # topology + duty assignments
python3 -m szpontnet --set tokens=out tier=2
python3 -m szpontnet --dispatch audit --prompt "…"
python3 -m szpontnet --fingerprint    # this device's trust key; --trust/--untrust/--ban to manage
```

New devices are **foreign (zero-trust)** until you promote them — see the root
README's trust model.

## Headless self-tests (no display needed)

```bash
DIPLOMAT_DUMP=1        python -m diplomat_app   # real fetch+filter, prints all 6 tools
DIPLOMAT_LOOKUP=337    python -m diplomat_app   # reverse-lookup one number
DIPLOMAT_PRINT_PROMPT=mine python -m diplomat_app  # assemble a Review prompt (mine|user|single)
                                                       #   conflicts-mine|conflicts-user|conflicts-single → Resolve-conflicts prompt

DIPLOMAT_SELF_UPDATE=1 python -m diplomat_app       # the unattended 06:00 update, run once

# Snapshot a panel state to PNG (no real display required):
DIPLOMAT_RENDER=panel DIPLOMAT_RENDER_OUT=/tmp/p.png \
    QT_QPA_PLATFORM=offscreen python -m diplomat_app   # panel|lookup|wizard|conflicts|audit|devices|mesh
                                                       #   settings[-explain] (-explain opens every row's
                                                       #   long-form paragraph, the only state that draws them)
DIPLOMAT_REFRESH_SECS=30 ./diplomat            # faster auto-refresh, for tuning
```

Also overridable: `DIPLOMAT_REPO` (the agents' working dir — outranks Settings ▸
*Repo root*, whose own default is `~/dev/<repo>`), `DIPLOMAT_CONFIG` (where the
shared `config.json` lives),
`DIPLOMAT_CORE_BIN`, `DIPLOMAT_AUTOFIX_SECS` (floor 30s), `DIPLOMAT_APIWATCH_SECS`
(floor 5s), `DIPLOMAT_SHELL`, `DIPLOMAT_PYTHON`, `DIPLOMAT_NPM`.

## Tests

```bash
python -m pytest tests            # full suite
python tests/test_logic.py        # the logic tests, dependency-free (no pytest)
```

- `tests/test_logic.py` - filters, reverse lookup, prompt assembly, PR-ref
  parsing, the allocator bridge.
- `tests/test_golden_prompts.py` - every prompt mode is driven through the
  Python config → `diplomat-core` bridge and compared byte-for-byte against the
  shared `assets/golden-prompts/` files (generated by the Swift smoke test), so the
  bridge can only drift from Swift by failing CI. Needs `DIPLOMAT_CORE_BIN`.
- `tests/test_autofix.py` - the monitors' pure decisions: the dispatch gate,
  edge/level triggers, backoff, mesh routing, the device's automatic-task cap, the
  rate-limit budget's arithmetic, and the queue behind that cap (what a refusal defers, what a switched-off monitor
  holds, the drain order, "execute now", and what a task being started is while its
  spawn runs). Pinned against the Swift twin.
- `tests/test_agent_tasks_panel.py` - the panel half of that queue: the rows it
  draws, and the click and the drop that reach the store.
- `tests/test_requested_reviews.py` - the reviews a PR sweep asks for: what one
  press queues, the list that remembers the asks across a restart, what re-offers
  them and what finally takes each off.
- `tests/test_apiwatch.py` - the API-error matcher + the tmux watcher's backoff
  and two-scan stall confirmation.
- `tests/test_activity.py` - the audit feed: action → category taxonomy, filtering.
- `tests/test_telemetry_parity.py` - one ledger folded through both the Swift core
  and this applet, diffed field for field (floats included), so the two Telemetry
  screens cannot disagree about what a ledger means. Needs `DIPLOMAT_CORE_BIN`.
- `tests/test_telemetry.py` - the ledger's IO and the two gatherers: what a poll
  records, what it clears, incremental transcript scanning, per-task attribution.
- `tests/test_telemetry_view.py` - the screen's own judgement: the empty state, the
  no-quota-readings fallback, the thin-sample warning, what a range flip re-scopes,
  the last reading a silent probe did take, and (by rendering the widget and reading
  its pixels) the rate-limit axis spanning the lookback rather than the readings.
- `tests/test_review_author.py` - the wizard's author poll and the toggles it hides.
- `tests/test_selfupdate.py` - fetch/merge/rebuild/relaunch, incl. the divergence case.
- `tests/test_migrate.py` - the one-time `~/.argent` → `~/.diplomat` state move.
- `tests/test_mesh_logic.py` - the mesh's pure brain: assignment strategies,
  platform spread + shortfall, token failover, permutation invariance (the
  leaderless-agreement property), protocol codec, LWW overrides
  (dependency-free runnable, like `test_logic.py`).
- `tests/test_mesh_node.py` - real node subprocesses on loopback: discovery,
  cross-node assignment agreement, dispatch, remote edits, death takeover,
  restart re-linking.
- `tests/test_mesh_e2e_applet.py` - the applet driving a real node end to end.
- `tests/test_addon_optional.py` - Diplomat with SzpontNet taken away: a real
  subprocess with the import blocked, rendering the panel anyway.
- `tests/test_allocator_update.py` - when a launch installs the device allocator,
  when it refreshes a stale one, and when it must leave an uninstall alone.
- `tests/conftest.py` - redirects `QSettings`, the shared `~/.diplomat/config.json`,
  the activity feed and `~/.claude` to per-test temp dirs (and switches the quota
  probe off), so tests never read your transcripts, spend your token on a live
  request, or scribble on your config; also clears `DIPLOMAT_MESH_*` so a pre-rename variable in your
  shell can't answer for a `SZPONTNET_*` one the tests mean to leave unset.

Tests that need no part of Diplomat live with the library, in
[`szpontnet-core/tests/`](../../szpontnet-core/tests) - the Tor transport, the one-node-per-state-dir
lock, the control-edit state flush, the host seam and the env namespace. They run in
CI with no Qt and no Diplomat on the path, which is what makes "SzpontNet is
independent" checkable rather than merely asserted.

## Layout

```
diplomat_app/
  __init__.py     puts ../../diplomat-runtime on sys.path and installs the mesh host —
                  every module below imports the runtime at its own top level
  store.py        state, QSettings, tool catalog, row mapping, lookup
  conflicts.py    ConflictConfig
  audit.py        AuditConfig - the Full E2E test
  probes.py       the only thing that LOOKS: ps, tmux panes, mesh claims, gh merge state,
                  each answering present / unavailable / unsupported so a failure to look
                  is never mistaken for an answer. Tracks its own health
  agentdump.py    DIPLOMAT_AGENTS=1 — every record, every probe's raw answer, every verdict
                  and the one fact that decided it
  autofixmonitor.py  the monitors' GitHub reads (monitor-prs / review-requests)
  bans.py         the allocator daemon's prompt-injection ban list
  selfupdate.py   fetch/merge, rebuild diplomat-core, relaunch (button + 6AM timer)
  migrate.py      one-time ~/.argent → ~/.diplomat state move
  glyphs.py       monochrome tool glyphs, size-normalised and tinted
  deviceallocator.py  bridge to the allocator daemon's state.json + Node installer
  szpont.py       the one gate on "is the SzpontNet add-on here?"
  meshspawn.py    the wizards' "⬡ Run on mesh" row
  meshview.py     the ⬡ Mesh topology screen
  telemetryview.py  the Telemetry screen: the bell curve, the rate-limit windows, the backlog series, the token split
  widgets.py      cards, chips, rows
  panel.py        the popup panel (header, search, grid, results, agent tasks, devices)
  settingsview.py two-pane settings screen
  wizardview.py   Review-PRs wizard
  conflictwizardview.py  Resolve-conflicts wizard
  auditwizardview.py     Full-E2E-test wizard
  selftest.py     headless dump / lookup / prompt self-tests
  singleton.py    newest-wins pidfile
  render.py       headless PNG snapshots (UI checks)
  app.py          QSystemTrayIcon + lifecycle
  __main__.py     entry point: headless modes or the GUI
diplomat        the launcher (what the autostart .desktop execs)
install/        build-core.sh + the autostart / auto-update (un)installers
meshsim/        the real-socket mesh simulator the mesh scenarios run through
tests/          the offline, headless pytest suite — this applet's, and the shared
                runtime's and diplomat-core's parity tests along with it
```

Everything below the UI - the assets loader, PR triage, the run book, token accounting,
the spawner, the mesh host - is [`diplomat-runtime`](../../diplomat-runtime/README.md),
which the macOS app runs too.

`argent-utils` is a deprecated launcher shim kept only so pre-rename installs keep
working; it forwards to `./diplomat`.
