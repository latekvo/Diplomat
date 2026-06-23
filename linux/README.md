# Argent Utils — Linux applet (Qt6 / PySide6)

The Linux port of the macOS menu-bar wrench: a **system-tray applet** with the
same dense panel of six `software-mansion/argent` triage tools, reverse lookup,
the Review-PRs and Resolve-conflicts wizards, and settings. It's a thin UI renderer over the shared
[`core/`](../core) assets — the triage logic is identical to the macOS app.

Universal across desktops via Qt6's `QSystemTrayIcon` (StatusNotifierItem /
XEmbed): works on **XFCE** (Notification Area / Status Notifier panel plugin),
**KDE**, and **GNOME** (with an AppIndicator extension).

## Requirements

- Python 3.10+
- PySide6 (`pip install -r requirements.txt`)
- GitHub CLI `gh`, authenticated (`gh auth login`)
- A terminal emulator for the Review wizard's SPAWN (auto-detected:
  `x-terminal-emulator`, `xfce4-terminal`, `gnome-terminal`, `konsole`, `kitty`,
  `alacritty`, `xterm`)

## Run

```bash
cd linux
pip install -r requirements.txt
./argent-utils                 # tray applet (left-click the wrench)
```

Quit from the panel's ⏻ button, the tray right-click menu, or `pkill -f "python -m argent_utils"`.

## Autostart on login

```bash
./scripts/install-autostart.sh    # XDG autostart .desktop + starts it now
./scripts/uninstall-autostart.sh  # removes it and stops the app
```

Installs `~/.config/autostart/argent-utils.desktop` so the wrench reappears every
login (the cross-desktop analogue of the macOS LaunchAgent).

## Settings

Persist via `QSettings` (`~/.config/argent-utils/…`):

- **GitHub username** — overrides the `gh`-authenticated login for the "My …" tools.
- **Tools — colour & visibility** — retint or hide any tool card.
- **Spawn terminal** — which terminal SPAWN AGENT opens.

Override the agent's working directory with `ARGENT_UTILS_REPO` (default `~/dev/argent`).

## Headless self-tests (no display needed)

```bash
ARGENT_UTILS_DUMP=1        python -m argent_utils   # real fetch+filter, prints all 6 tools
ARGENT_UTILS_LOOKUP=337    python -m argent_utils   # reverse-lookup one number
ARGENT_UTILS_PRINT_PROMPT=mine python -m argent_utils  # assemble a Review prompt (mine|user|single)
                                                       #   conflicts-mine|conflicts-user|conflicts-single → Resolve-conflicts prompt

# Snapshot a panel state to PNG (no real display required):
ARGENT_UTILS_RENDER=panel ARGENT_UTILS_RENDER_OUT=/tmp/p.png \
    QT_QPA_PLATFORM=offscreen python -m argent_utils   # panel|lookup|wizard|conflicts|settings
ARGENT_UTILS_REFRESH_SECS=30 ./argent-utils            # faster auto-refresh, for tuning
```

## Tests

```bash
python tests/test_logic.py        # filters + reverse lookup + prompt assembly
```

## Layout

```
linux/argent_utils/
  core.py         loads the shared core/ assets
  gh.py           gh CLI shell-out (GraphQL)
  models.py       domain models, Filters, Fmt, API (from core/)
  store.py        state, QSettings, tool catalog, row mapping, lookup
  review.py       ReviewConfig + prompt builder (from core/), terminal spawner
  conflicts.py    ConflictConfig + prompt builder (from core/)
  widgets.py      cards, chips, rows
  panel.py        the popup panel (header, search, grid, results)
  settingsview.py settings screen
  wizardview.py   Review-PRs wizard
  conflictwizardview.py  Resolve-conflicts wizard
  singleton.py    newest-wins pidfile
  render.py       headless PNG snapshots (UI checks)
  app.py          QSystemTrayIcon + lifecycle
```
