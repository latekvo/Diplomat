# szpont

![The Diplomat panel](https://github.com/user-attachments/assets/bffd7a4b-2859-48ee-bffb-9da8221a4b02)

Installs and starts **[Diplomat](https://github.com/latekvo/Diplomat)** (Szpont Yon),
a menu-bar / system-tray app that runs coding agents against a GitHub repo: it
reviews PRs, fixes your own, resolves conflicts, and gives each agent its own
device.

```bash
pip install szpont && szpont     # or:  npx szpont
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

## npm

`npx szpont` runs the same launcher, written separately in JavaScript. Neither has
dependencies. `packages/szpont-npm/test/parity-with-python.mjs` runs both against
every machine in `test/scenarios.mjs` and fails if their plans differ.

Diplomat's machines find each other over
**[SzpontNet](https://github.com/latekvo/Diplomat/tree/main/packages/szpontnet-core)**,
published separately as `szpontnet`. This package doesn't install it.

## Tests

```bash
pip install pytest
pytest packages/szpont/tests -q
```

## The name

`szpont` is also the import name of the SzpontNet
[conformance tester](https://github.com/latekvo/Diplomat/tree/main/packages/szpontnet-spec/conformance),
run as `python -m szpont` from its own directory. They don't collide: this package
installs a `szpont` console script and a `szpont_launcher` module, and never the
`szpont` import name. CI checks that the built wheel keeps it that way.
