# szpont

Install-and-run for **[Diplomat](https://github.com/latekvo/Diplomat)** (Szpont
Yon) - the menu-bar / system-tray applet that reviews your pull requests, fixes
your own, resolves conflicts, and keeps one device per agent.

```bash
pip install szpont && szpont     # or:  npx szpont
```

That is the whole install. macOS and Linux.

## What it actually does

Diplomat is built out of the checkout it runs from - a SwiftUI bundle on macOS, a
PySide6 package plus the `diplomat-core` Swift binary on Linux - and it keeps
*being* that checkout: the Update button, the daily 06:00 timer and the commit the
panel reports are all git operations on it. A wheel holding a copy would be a
second Diplomat that no update could reach.

So this distribution is the steps in between, and every one of them runs a script
that is already in the repository:

| | macOS | Linux |
|---|---|---|
| get it | `git clone` into `~/.diplomat/checkout`, or fast-forward it | same |
| build it | `install/build-app.sh` → `Diplomat.app` | `install/build-core.sh` when the prompt binary is missing |
| set it up | - | a venv at `~/.diplomat/venv` with `requirements.txt` in it |
| start it | `open Diplomat.app` | the package's own `./diplomat`, on the venv's interpreter |

Nothing is rebuilt that does not need to be: a venv is left alone until
`requirements.txt` changes, and the Swift prompt binary is only built when the
applet would not find one. `git` is needed to fetch Diplomat, and a Swift
toolchain to build it - if one is missing you are told which and where to get it,
before anything is downloaded.

```
szpont --plan        # print what would be run, as JSON, and do none of it
szpont --no-update   # launch the checkout as it stands
szpont -- --dump     # everything after -- goes to the applet
```

`DIPLOMAT_SELF_REPO` points at a checkout of your own - the same variable the
running applet reads, and one this launcher never updates, because a working copy
may have work in it. `DIPLOMAT_REPO_URL` clones from a fork.

## The same name on both indexes

`npx szpont` runs the same launcher, and that is all either name installs: one
file of standard library on each side, no dependency on either index. What keeps
them one command is `packages/szpont-npm/test/parity-with-python.mjs` - every
machine shape in `test/scenarios.mjs` goes through both planners, and the two
answers have to be identical.

The protocol Diplomat's machines find each other over is
**[SzpontNet](https://github.com/latekvo/Diplomat/tree/main/packages/szpontnet-core)**,
published separately as `szpontnet`; nothing here imports it, and installing this
does not install it.

## Its own tests

```bash
pip install pytest
pytest packages/szpont/tests -q
```

## Note on the name

`szpont` names three things, and they share the namespace rather than compete for
it: this command, its npm twin, and the
[conformance tester](https://github.com/latekvo/Diplomat/tree/main/packages/szpontnet-spec/conformance),
run as `python -m szpont` from its own directory.

Only the tester is precarious. It is never installed, and the working directory
precedes site-packages on `sys.path`, so it wins from where it is run - but
publishing *it* under this name is what the three could not share. The console
script cannot collide with it at all: an entry point is a file in `bin/`, not a
name on the import path, and the module this one lives in is `szpont_launcher`.
