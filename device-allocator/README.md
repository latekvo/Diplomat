# Diplomat Device Allocator

An **arbitrator for the simulators and emulators on one machine.** Several AI coding
agents running at once will otherwise all reach for the same iPhone simulator, and
the result is not a crash — it is two agents interleaving taps on one screen,
navigating each other's app, and each debugging the other's actions for an hour. This
hands out exclusive devices, reclaims them when an agent dies or goes idle, and makes
every agent ask first.

An MCP server, so agents talk to it in the one protocol they all speak. It does not
need Diplomat: point any MCP client at `src/mcp.js` and it works. Diplomat installs
and updates it, and renders its pool in the Devices panel, but that is a convenience
on top rather than a dependency underneath.

## Standing on its own

One runtime dependency — `@modelcontextprotocol/sdk`, and only `src/mcp.js` imports
it, so the daemon runs on a bare Node with nothing installed. No build step, no
transpile: `src/*.js` is what runs.

```bash
npm ci
node src/install.js --install    # register with Claude Code, start the daemon
node src/install.js --check      # what's installed, and whether it's current
node src/install.js --uninstall  # take it all back off
```

Everything it owns lives under `~/.diplomat/device-allocator/` (`DA_BASE_DIR`
redirects it, which is how the tests never touch a real pool).

## The three pieces

| | |
|---|---|
| [`src/daemon.js`](src/daemon.js) | the **arbiter**: one per machine, HTTP over a unix socket. Owns the pool, the leases, the reaper and the idle sweep. |
| [`src/mcp.js`](src/mcp.js) | the **per-agent MCP server**: one per Claude Code session, a thin forwarder that auto-starts the daemon. |
| [`src/install.js`](src/install.js) | the **installer**: registers the server, lays down the skill and the always-on rule, injects the CLAUDE.md block, starts the daemon. |

The MCP process is deliberately disposable: it lives exactly as long as its agent
session, so the daemon uses **its PID as the ownership token**. When the agent dies,
its server dies, and the reaper frees whatever it held. Nothing has to be trusted to
clean up after itself.

## The tools an agent sees

| tool | |
|---|---|
| `request-device` | reserve one exclusively — the call every other one presumes |
| `await-device` | block until a slot frees, when the quota is reached |
| `free-device` | release it |
| `change-device` | swap platform/format/version in one step |
| `report-device-broken` | quarantine one that won't boot; a repair is dispatched and a replacement handed over |
| `report-prompt-injection` | ban an author from automated review, capture the evidence, and terminate the targeted agent |

## Making agents actually use it

A tool an agent may ignore is a tool that collides with another agent, so the
installer applies the coercion at every layer at once — the MCP handshake's
`instructions` field (always connected, therefore always injected), an
`alwaysApply` rule in `~/.claude/rules/`, a managed block in `~/.claude/CLAUDE.md`,
a skill in `~/.claude/skills/`, and the MUST-allocate-first contract restated in
every tool description. `request-device` is marked `alwaysLoad` so progressive
tool-loading can never hide it.

## Staying current

Every one of those is a **copy** of something in this checkout, and a `git pull`
moves the originals alone. So `--check` compares them by content and reports
`outdated` with a `drift` list naming what no longer matches; `--install` rewrites
all of them, and is therefore also the repair. Both applets run that check on launch
and re-install a stale copy — but never an install the user deliberately removed,
which is the one distinction worth getting right
([`test/install-drift.mjs`](test/install-drift.mjs)).

The MCP registration is judged by *which file* it names rather than how the path is
spelled: this checkout is reachable as both `~/dev/diplomat` and `~/dev/Diplomat` on
a case-insensitive volume, and a string compare there is a permanent, self-inflicted
"out of date" on every launch.

## Reclaiming

Three independent ways a device comes back, because an agent that promises to free
one is exactly the thing that just died:

* **the reaper** (every 10s) frees anything whose owning PID is gone;
* **the idle sweep** (every 2 min) frees a device with no screen motion for 15 min;
* **a repair TTL** (2h) returns a quarantined device to the pool even if the repair
  agent never reports back — without it, one broken report shrinks the pool forever.

Concurrency is capped at 5 devices across all agents (`DA_QUOTA`). The *pool* is
unbounded — agents create devices on demand — so this caps how many run at once, not
how many exist.

## Tests

```bash
npm test
```

No real simulators or emulators: `DA_FAKE_DEVICES` points enumeration at a JSON pool,
so the full request / change / broken / free / reap / idle path runs on Linux CI.
Each test drives the daemon over its real unix socket rather than importing it, so
what is exercised is the process an agent actually talks to.

[`test/run-all.mjs`](test/run-all.mjs) **discovers** `test/*.mjs` rather than listing
them, and CI runs that same `npm test` — so there is no enumeration anywhere that a
new test can be left out of, and a file dropped in `test/` runs from that moment. A
test that exists but is never run is indistinguishable from one that passes, which is
the failure mode worth designing out rather than watching for.
