# Diplomat — Linux applet (Qt6 / PySide6)

The Linux port of the macOS menu-bar wrench: a **system-tray applet** with the
same dense panel of six `software-mansion/argent` triage tools, reverse lookup,
the Review-PRs / Resolve-conflicts / Full-E2E-test wizards, the full autonomous
monitor stack, a Devices view of the device-allocator pool, the Mesh topology
screen, and settings. It's a thin UI renderer over the shared [`assets/`](../../diplomat-core/assets)
assets — and it doesn't re-implement prompt assembly at all: it shells out to the
`diplomat-core` Swift binary, so the two front-ends are identical by construction.

**Still macOS-only:** the per-row **Merge** button, and reading/typing into
*arbitrary* terminal windows — Linux has no portable hook for that, so the
API-error watcher drives **tmux panes** instead and is inert for agents not
running inside tmux.

Universal across desktops via Qt6's `QSystemTrayIcon` (StatusNotifierItem /
XEmbed): works on **XFCE** (Notification Area / Status Notifier panel plugin),
**KDE**, and **GNOME** (with an AppIndicator extension).

## Requirements

- Python 3.10+
- PySide6 (`pip install -r requirements.txt`)
- GitHub CLI `gh`, authenticated (`gh auth login`)
- The `diplomat-core` binary for prompt assembly — build it with
  `./install/build-core.sh` (needs a Swift toolchain once), or point
  `DIPLOMAT_CORE_BIN` at a prebuilt one
- A terminal emulator for the wizards' SPAWN (auto-detected:
  `x-terminal-emulator`, `xfce4-terminal`, `gnome-terminal`, `konsole`, `kitty`,
  `alacritty`, `xterm`); **tmux** additionally, if you want the API-error watcher
  to be able to see your agents
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
(`~/.config/diplomat/…`); the repo root and the automatic-task cap are the
exceptions (see their bullets):

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
  a friendly thank-you note, never an APPROVE action).
- **Run at most N automatic tasks at a time** — this machine's hard cap on
  concurrent automatic agents (default **2**, range 1–16), spanning both monitors
  above and any work a mesh peer routes here. Panel spawns are never capped and
  don't count against it; work over the cap waits for the next poll rather than
  being dropped. In `~/.diplomat/config.json` for the same reason as the repo
  root — the node that runs peer-routed work has no Qt, and a machine with two
  answers to "how many at once" has no cap at all.
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
export SZPONTNET_HOST=diplomat_app.szponthost PYTHONPATH=../../szpontnet-core

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
    QT_QPA_PLATFORM=offscreen python -m diplomat_app   # panel|lookup|wizard|conflicts|audit|settings|devices|mesh
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
  edge/level triggers, backoff, mesh routing, the device's automatic-task cap.
  Pinned against the Swift twin.
- `tests/test_apiwatch.py` - the API-error matcher + the tmux watcher's backoff
  and two-scan stall confirmation.
- `tests/test_activity.py` - the audit feed: action → category taxonomy, filtering.
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
- `tests/conftest.py` - redirects `QSettings`, the shared `~/.diplomat/config.json`
  and the activity feed to per-test temp dirs, so tests never read (or scribble on)
  your live config; also clears `DIPLOMAT_MESH_*` so a pre-rename variable in your
  shell can't answer for a `SZPONTNET_*` one the tests mean to leave unset.

Tests that need no part of Diplomat live with the library, in
[`szpontnet-core/tests/`](../../szpontnet-core/tests) - the Tor transport, the one-node-per-state-dir
lock, the control-edit state flush, the host seam and the env namespace. They run in
CI with no Qt and no Diplomat on the path, which is what makes "SzpontNet is
independent" checkable rather than merely asserted.

## Layout

```
diplomat_app/
  core.py         loads the shared assets/ (from the diplomat-core package)
  gh.py           gh CLI shell-out (GraphQL)
  models.py       domain models, Filters, Fmt, API (from assets/)
  store.py        state, QSettings, tool catalog, row mapping, lookup
  appconfig.py    ~/.diplomat/config.json — the settings a stdlib-only mesh node must read too
                  (the repo root, and the cap on concurrent automatic agents)
  prref.py        single-PR reference parsing (number / URL / owner-repo#337)
  prtarget.py     the whose-PRs axis shared by the wizards
  promptcore.py   shells out to the diplomat-core binary — the ONLY prompt assembly
  review.py       ReviewConfig + terminal spawner
  conflicts.py    ConflictConfig
  audit.py        AuditConfig - the Full E2E test
  autofix.py      pure monitor decisions: dispatch gate, triggers, backoff, mesh, task cap
  autofixmonitor.py  the monitors' GitHub reads (monitor-prs / review-requests)
  apiwatch.py     "is this a Claude API error?" matcher + nudge bookkeeping
  tmuxwatch.py    tmux capture-pane / send-keys — the Linux stand-in for AppleScript
  activity.py     the unified audit feed (audit.jsonl) + its category taxonomy
  bans.py         the allocator daemon's prompt-injection ban list
  selfupdate.py   fetch/merge, rebuild diplomat-core, relaunch (button + 6AM timer)
  migrate.py      one-time ~/.argent → ~/.diplomat state move
  glyphs.py       monochrome tool glyphs, size-normalised and tinted
  deviceallocator.py  bridge to the allocator daemon's state.json + Node installer
  szpont.py       the one gate on "is the SzpontNet add-on here?"
  szponthost.py   Diplomat's answers to the six questions a mesh node asks its host
  meshspawn.py    the wizards' "⬡ Run on mesh" row
  meshview.py     the ⬡ Mesh topology screen
  widgets.py      cards, chips, rows
  panel.py        the popup panel (header, search, grid, results, devices)
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
tests/          the offline, headless pytest suite
```

`argent-utils` is a deprecated launcher shim kept only so pre-rename installs keep
working; it forwards to `./diplomat`.
