# diplomat-runtime - Diplomat below the UI

Everything Diplomat does that is not a window: read the shared assets, decide what a
monitor should dispatch, keep the run book, price the ledger, open a terminal on an
agent, and answer the six questions a [SzpontNet](../szpontnet-core/README.md) node asks
its host. No Qt, no AppKit, no PySide6 - importable from a stdlib-only daemon.

Both front-ends run it, by different routes:

- the **Linux applet** imports it directly (`diplomat_app` puts this package on
  `sys.path` and every module of the front-end reaches through it);
- the **macOS app** links the Swift twins for its own screens, and hands *this* package
  to the mesh node it spawns - `SZPONTNET_HOST=diplomat_runtime.szponthost`, with
  nothing else of Diplomat's on that node's path.

That second route is the reason the package exists. A node runs on both OSes, so the
module that puts Diplomat behind one is cross-platform by definition; when it lived in
the Linux applet's package, enabling the mesh on a Mac meant putting the Linux front-end
on a `PYTHONPATH`.

## Twins

Most of what is here has a Swift counterpart in
[`diplomat-core`](../diplomat-core/README.md), pinned field-for-field by the parity
tests in [`../diplomat-platform/linux/tests`](../diplomat-platform/linux/tests) - which
is also where this package's own tests live, since they need the same fixtures the
applet's do. `tests/test_core_adoption.py` is the guard on the arrangement: a core type
that grows a twin here and is named nowhere in the macOS sources fails the suite.

## Layout

```
diplomat_runtime/
  core.py         loads the shared assets/ (from the diplomat-core package)
  gh.py           gh CLI shell-out (GraphQL)
  models.py       domain models, Filters, Fmt, API (from assets/)
  appconfig.py    ~/.diplomat/config.json — the settings a stdlib-only mesh node must read too
                  (the repo root, the cap on concurrent automatic agents, and the
                  rate-limit budget those agents are started against)
  configbase.py   what the three wizards' configs share
  prref.py        single-PR reference parsing (number / URL / owner-repo#337)
  prtarget.py     the whose-PRs axis shared by the wizards
  promptcore.py   shells out to the diplomat-core binary — the ONLY prompt assembly
  review.py       ReviewConfig + terminal spawner
  runner.py       which agent CLI a spawn runs, and the one command that runs it
  autofix.py      pure monitor decisions: dispatch gate, triggers, backoff, mesh,
                  task cap, rate-limit budget, and the queue behind the cap
                  (Autofix.swift + AgentTasks.swift's twin)
  autobudget.py   ledger + probe + knobs -> may another automatic task start here?
                  Asked by the applet's gate and by the mesh node (AutoBudget.swift's twin)
  agentstate.py   the one resolver: typed evidence -> a state per agent run, and the four
                  projections over it (per-PR dedup, the cap, the panel rows, retirement).
                  Pure - no clock, no subprocess, no filesystem (AgentState.swift's twin)
  agentregistry.py  the durable run book at ~/.diplomat/agents — one record per dispatched
                  run, plus each run's prompt, pid and completion sentinel. Same on-disk
                  format as AgentRegistry.swift, byte for byte
  opencodeapi.py  reading an OpenCode run's own session: whose it is, mid-turn or not, spend
  hermesstore.py  the same, for a Hermes run's session in its SQLite store
  apiwatch.py     "is this a Claude API error?" matcher + nudge bookkeeping
  tmuxwatch.py    tmux capture-pane / send-keys — the Linux stand-in for AppleScript
  activity.py     the unified audit feed (audit.jsonl) + its category taxonomy
  telemetry.py    the append-only telemetry ledger + the arithmetic over it (twin of Telemetry.swift)
  usagescan.py    Claude Code transcript scanner: repo-vs-other tokens, per-task attribution
  quota.py        the OAuth usage probe — what is left of the 5-hour and 7-day windows
  atomicjson.py   write-then-rename, for the files two processes share
  szponthost.py   Diplomat's answers to the six questions a mesh node asks its host
```

Each front-end keeps its own **probe** layer on top - the impure half that looks at the
outside world, which is `ps` and tmux on Linux (`diplomat_app/probes.py`) and `ps` and
AppleScript on macOS (`AgentProbes.swift`). Absence of evidence resolves to *unknown*,
never to *finished*, on both.
