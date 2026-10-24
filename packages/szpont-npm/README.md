# szpont

![The Diplomat panel](https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02)

Installs and starts **[Diplomat](https://github.com/latekvo/Diplomat)** (Szpont Yon),
a menu-bar / system-tray app that runs coding agents against a GitHub repo: it
reviews PRs, fixes your own, resolves conflicts, and gives each agent its own
device.

```bash
npx szpont          # or: npm i -g szpont && szpont
```

macOS and Linux.

## How it works

Diplomat runs from a git checkout, and its Update button and daily 06:00 update are
git operations on that checkout. So this package doesn't ship a copy of Diplomat; it
fetches the checkout and runs the scripts already in the repository:

| | macOS | Linux |
|---|---|---|
| get it | `git clone` into `~/.diplomat/checkout`, or fast-forward it | same |
| build it | `install/build-app.sh` → `Diplomat.app` | `install/build-core.sh` when the prompt binary is missing |
| set it up | - | a venv at `~/.diplomat/venv` with `requirements.txt` in it |
| start it | `open -n Diplomat.app` | the package's own `./diplomat`, on the venv's interpreter |

The venv is only rebuilt when `requirements.txt` changes, and the Swift prompt
binary only when the applet can't find one. You need `git` and a Swift toolchain; if
either is missing, `szpont` says which before downloading anything.

```
szpont --plan        # print what would be run, as JSON, and do none of it
szpont --no-update   # launch the checkout as it stands
szpont -- --dump     # everything after -- goes to the applet
```

`DIPLOMAT_SELF_REPO` points at your own checkout, which is never updated since it
may have work in it. `DIPLOMAT_REPO_URL` clones from a fork.

## PyPI

[`pip install szpont`](https://github.com/latekvo/Diplomat/tree/main/packages/szpont)
installs the same launcher, written separately in Python.
`test/parity-with-python.mjs` runs both against every machine in
`test/scenarios.mjs` and fails if their plans differ.

## Tests

```bash
npm test        # plan, cli, a real clone-build-launch, and the parity check
```

The end-to-end test clones a git repository it creates on disk and runs the whole
sequence into stub build and launch scripts, so it needs neither the network nor a
Swift toolchain.
