# Diplomat — Linux applet (Qt6 / PySide6)

The Linux port of the macOS menu-bar wrench: a **system-tray applet** with the
same dense panel of six `software-mansion/argent` triage tools, reverse lookup,
the Review-PRs / Resolve-conflicts / Full-E2E-test wizards, the full autonomous
monitor stack, a Devices view of the device-allocator pool, the Mesh topology
screen, and settings. It's a thin UI renderer over the shared [`core/`](../core)
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
  `./scripts/build-core.sh` (needs a Swift toolchain once), or point
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
./scripts/install-autostart.sh    # XDG autostart .desktop + the 6AM update timer, starts it now
./scripts/uninstall-autostart.sh  # removes both and stops the app
```

Installs `~/.config/autostart/diplomat.desktop` so the wrench reappears every
login (the cross-desktop analogue of the macOS LaunchAgent).

It also installs a **systemd user timer** (`diplomat-update.timer`) that fires
daily at **06:00** and runs the launcher headless (`DIPLOMAT_SELF_UPDATE=1`):
fetch, merge if behind, rebuild `diplomat-core`, and relaunch the tray only if
one is running. `Persistent=true`, so a 06:00 missed while the machine was off
runs at the next boot. Without `systemctl` the install warns and carries on —
only the schedule is lost, the Settings ▸ UPDATE button still works. Manage it
alone with `./scripts/install-autoupdate.sh` / `./scripts/uninstall-autoupdate.sh`.

## Settings

A two-pane screen matching macOS. Persist via `QSettings` (`~/.config/diplomat/…`):

- **GitHub username** — overrides the `gh`-authenticated login for the "My …" tools.
- **PR auto-fix / Full-E2E review requests** — the two monitor toggles with live
  status, and under the review-requests one the **auto-approve** master toggle
  plus its three withhold-the-verdict suppressors (SKILL / installer / community),
  and the **soft-approve** toggle (default ON — a clean comments-only review leaves
  a friendly thank-you note, never an APPROVE action).
- **Claude API errors** — the tmux watcher toggle, plus a count of nudges sent.
- **Tools — colour & visibility** — retint or hide any tool card. (SKILL.md PRs
  and Installer/CLI PRs ship hidden.)
- **Spawn terminal** — which terminal SPAWN AGENT opens.
- **Device allocator (MCP)** - install/uninstall the bundled allocator daemon +
  MCP server (drives the Node installer in `../device-allocator/`), with status.
- **Mesh (LAN P2P)** — start/stop the local mesh node (off by default), with live
  node/peer status; the mesh itself is managed from the ⬡ Mesh screen.
- **Update** — bring this checkout up to the latest GitHub commit (merging when
  you have local commits of your own), rebuild the `diplomat-core` binary, and
  relaunch the tray app in place (the fresh instance replaces the running one,
  newest-wins).

## Diplomat Mesh (experimental)

The applet can coordinate duties with the other machines on your LAN — see the
[root README's Mesh section](../README.md#diplomat-mesh-experimental--lan-p2p-duty-coordination)
for the model. Enable it in ⚙ Settings; the panel then grows a collapsible
topology column (live nodes, link states, per-node tier/token editors, per-duty
strategy controls). The node itself is stdlib-only and runs headless anywhere:

```bash
python3 -m diplomat_app.mesh --daemon     # join the mesh (works on macOS too, no Qt)
python3 -m diplomat_app.mesh --status     # topology + duty assignments
python3 -m diplomat_app.mesh --set tokens=out tier=2
python3 -m diplomat_app.mesh --dispatch audit --prompt "…"
python3 -m diplomat_app.mesh --fingerprint    # this device's trust key; --trust/--untrust/--ban to manage
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

Also overridable: `DIPLOMAT_REPO` (the agents' working dir, default `~/dev/argent`),
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
  shared `core/golden-prompts/` files (generated by the Swift smoke test), so the
  bridge can only drift from Swift by failing CI. Needs `DIPLOMAT_CORE_BIN`.
- `tests/test_autofix.py` - the monitors' pure decisions: the dispatch gate,
  edge/level triggers, backoff, mesh stand-down. Pinned against the Swift twin.
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
- `tests/test_mesh_ctl_flush.py` / `tests/test_mesh_e2e_applet.py` - control-edit
  state flush, and the applet driving a real node end to end.
- `tests/conftest.py` - redirects `QSettings` to a per-test temp dir, so tests
  never read (or scribble on) your live config.

## Layout

```
linux/diplomat_app/
  core.py         loads the shared core/ assets
  gh.py           gh CLI shell-out (GraphQL)
  models.py       domain models, Filters, Fmt, API (from core/)
  store.py        state, QSettings, tool catalog, row mapping, lookup
  prref.py        single-PR reference parsing (number / URL / owner-repo#337)
  prtarget.py     the whose-PRs axis shared by the wizards
  promptcore.py   shells out to the diplomat-core binary — the ONLY prompt assembly
  review.py       ReviewConfig + terminal spawner
  conflicts.py    ConflictConfig
  audit.py        AuditConfig - the Full E2E test
  autofix.py      pure monitor decisions: dispatch gate, triggers, backoff, mesh stand-down
  autofixmonitor.py  the monitors' GitHub reads (monitor-prs / review-requests)
  apiwatch.py     "is this a Claude API error?" matcher + nudge bookkeeping
  tmuxwatch.py    tmux capture-pane / send-keys — the Linux stand-in for AppleScript
  activity.py     the unified audit feed (audit.jsonl) + its category taxonomy
  bans.py         the allocator daemon's prompt-injection ban list
  selfupdate.py   fetch/merge, rebuild diplomat-core, relaunch (button + 6AM timer)
  migrate.py      one-time ~/.argent → ~/.diplomat state move
  glyphs.py       monochrome tool glyphs, size-normalised and tinted
  deviceallocator.py  bridge to the allocator daemon's state.json + Node installer
  mesh/           Diplomat Mesh node (stdlib-only): identity, crypto, trust, banned,
                  protocol, assign, hardware, usage/stats, peercache, node (asyncio),
                  ctl client, spawnjob, statefile, config, __main__ CLI
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
```

`argent-utils` is a deprecated launcher shim kept only so pre-rename installs keep
working; it forwards to `./diplomat`.
