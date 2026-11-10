# Diplomat Remote Machines

**The machines an agent can offload work to.** One laptop running a dozen agents is
fully used, while the machine on the next desk sits idle. This MCP server keeps a live
table of every machine this one can reach over SSH: what it can do (OS, CPU, GPU, RAM,
toolchains, free disk) and how loaded it is right now. It also tells every agent to run
anything that produces an artifact (builds, test runs, renders, packages, images) on the
best of them.

It is built like the [device allocator](../device-allocator/README.md): a per-agent
stdio MCP server, a daemon shared by all of them on a unix socket, and an installer.
The three are built on [`daemon-core`](../daemon-core/README.md), which both packages
share. Point any MCP client at `src/mcp.js`; Diplomat is not needed.

```bash
npm ci
node src/install.js --install    # register with Claude Code, start the daemon
node src/install.js --check      # what's installed, and whether it's current
node src/install.js --uninstall  # take it all back off
```

Everything it owns lives under `~/.diplomat/remote-machines/` (`RM_BASE_DIR` redirects it).

## What an agent sees

One tool, `list-machines` (marked `alwaysLoad`). It returns this machine first, for
comparison, then the usable remote machines best first, then one line on the rest:

```
1. radoniusz (also radoniusz.local) - radoniusz@radoniusz.local
   macOS 27.0 arm64 · Apple M3 Pro, 11 cores (5 performance) · GPU Apple M3 Pro (14-core) · 18 GB RAM
   load 1.04 now, 1.72 over 5 min, 1.21 since the last scan (0.16 per core over 5 min) · 10 spare cores · 5.9 GB RAM available · swap 5.0 GB of 6.0 GB used · 14 GB disk free
   tools on the login PATH: git, gh, node v26.7.0, npm, python3, docker, swift, java, ffmpeg, Xcode 27.1 · home /Users/radoniusz
   WARNING: low disk: 14 GB free
   load read 12 s ago
```

`refresh: true` rediscovers machines and re-reads everything before answering. The
steering is applied at every layer the allocator uses, so no agent can miss it: the
`instructions` on the MCP handshake, an `alwaysApply` rule in `~/.claude/rules/`, a
managed block in `~/.claude/CLAUDE.md`, a skill in `~/.claude/skills/`, and the tool
description. All of them say the same thing: before artifact-producing work, call
`list-machines`, then rsync the inputs over, run the work through a login shell
(`ssh <target> '$SHELL -l -s'` with the commands on stdin, since a plain `ssh <target>
<command>` misses Homebrew), bring back only the artifacts and clean up. They also say when to stay local: when there is no
usable machine, when the work needs this machine's devices, GUI, services or secrets, or
when the remote lacks the toolchain.

## Finding machines

[`src/discovery.js`](src/discovery.js) collects candidates from four places, without
connecting to any:

| source | |
|---|---|
| `remoteMachineHosts` in `~/.diplomat/config.json` | ssh targets to list on any network (a cloud box, say) |
| `Host` aliases in `~/.ssh/config` (and its `Include`s) | patterns skipped |
| `_ssh._tcp` over mDNS | `dns-sd` on macOS, `avahi-browse` on Linux |
| plain-text entries in `known_hosts` | hashed entries cannot be read back |

Each is resolved with `ssh -G`, so the user's own config decides the user, port and host
name. Outside `remoteMachineHosts`, only hosts on this network count: single-label and
`.local`/`.lan`/`.home`/`.home.arpa`/`.internal` names, tailnets (`.ts.net`, 100.64/10)
and private or link-local addresses. Candidates naming the same host and port merge into
one, keeping the target of the first source in the table's order (so an ssh-config alias,
with the user it sets, beats the bare names known_hosts and mDNS give). After probing, two names
reaching the same machine id merge too.

**Only a host whose key is already in `known_hosts` is ever contacted**, and every
probe runs with `StrictHostKeyChecking=yes`, whatever the user's config says. A host
nobody vetted could accept any client key and receive whatever an agent offloads to it.
Probing would also offer our keys to every laptop advertising SSH on an office network.
Such hosts are listed as `untrusted`; `ssh <host>` once by hand to vet one. A host whose
key changed since it was accepted is listed as `key-changed`, and stays unused until its
old key is removed from `known_hosts`. A host reached through a `ProxyJump` or
`ProxyCommand` is listed as `proxied` and never probed: the hop is a second ssh that
the probe's options do not reach, so it would accept a new key.

## Taking a machine out of the pool

Two keys in `~/.diplomat/config.json` take machines out of the pool, which Diplomat's
Machines screen writes. An entry is a target or host name (case-insensitive), or an alias,
which also catches its host under any other name it is found by.

| key | |
|---|---|
| `remoteMachineExclude` | a list: for good. Such a machine is never contacted. |
| `remoteMachineExcludeUntil` | `{name: ISO time}`: until then. Such a machine is still read, so its load stays on show. |

The daemon checks the file every second (`RM_CONFIG_POLL_MS`) and republishes when it
changes or a time in it passes. An excluded machine is listed under `excluded`, not
`machines`, with `excluded: {until, by}`: `until` is null for good, and `by` lists the
entries that name it, so a UI can take back exactly those. The agent output names it as
not usable. A machine excluded under one name stays excluded under all of them, across
restarts too: the daemon keeps the machine id each name last reached in
`machine-ids.json`, with the host key known_hosts held then, so a different machine later
accepted at one of those names is not taken for it.

## Reading a machine

[`src/probe.sh`](src/probe.sh) is fed to `ssh <target> sh -s` (and to a local `sh` for
this machine). It installs nothing and changes nothing. A `load` probe reads the load
averages, memory (macOS `kern.memorystatus_level` and pressure level, Linux
`MemAvailable`), swap, free disk in `$HOME`, boot time, machine id, NVIDIA GPU usage and
the number of SSH sessions open on it (other than the probe's own; on macOS, only those of
the user it logs in as, since other users' process titles are hidden).
A `full` probe adds the OS, CPU, GPUs, Xcode, and the toolchains on the login shell's
PATH (the one agents are told to run work under).

The daemon re-reads the load of every reachable machine every minute
(`RM_PROBE_INTERVAL_MS`). Every 5 minutes (`RM_DISCOVERY_INTERVAL_MS`) it also
rediscovers, re-reads capabilities and retries the machines that failed.

**Load since the last scan** needs nothing on the machine. The 5-minute load average is
an exponential moving average of the run queue with a 300 s time constant, on macOS and
Linux alike. Given two readings a minute apart, `L5(t) = L5(t0)·e^(-dt/300) +
(1 - e^(-dt/300))·n` can be solved for `n`, the near-uniformly weighted mean run queue
between them ([`meanLoadBetween`](src/probe.js)). Readings closer than 45 s apart are too
coarse for this, so a refresh right after a scan keeps the previous value.

A machine gets warnings for load above its core count, memory pressure (or under 10%
free), swap over 75% used where there is no pressure reading (Linux; macOS keeps the swap
files it grew, so its used/total stays high on a healthy machine), and under 20 GB of
free disk. Machines without warnings come
first, then the rest, each group ordered by spare cores.

Each machine in the snapshot carries `usage`, the two fractions the ranking and the
warnings judge it by: `cpu`, the 1-minute load per core (above 1 when overloaded), and
`memory`, the share of RAM not available. Every reading of them, with the SSH session
count, also goes into `history.json` as `{machines: {<id>: [[ms, cpu, memory, sessions],
...]}}`, keyed by the `id` each machine in the snapshot carries and kept for a day
(`RM_HISTORY_MS`). The daemon reads nothing while it is not running, so the history has
gaps where no agent session was open.

## Lifetime

The daemon has no work of its own: it serves agent sessions. Each session's MCP server
attaches its pid when it starts, which also starts the daemon, so the table is warm by
the first call. The daemon exits once no attached session has been alive for 10 minutes
(`RM_IDLE_EXIT_MS`), and kills any ssh it still has in flight on the way out. Each
probe runs in its own process group, killed whole at `RM_PROBE_TIMEOUT_MS`, and never
uses ControlMaster or the user's forwards. A remote command a timed-out probe started
can still finish on the remote, since a channel without a pty gets no hangup.

## Tests

```bash
npm test                  # files run concurrently, each one's output in one block
RM_TEST_JOBS=1 npm test   # one at a time
```

[`test/lib/fake-ssh.mjs`](test/lib/fake-ssh.mjs) stands in for `ssh` (`RM_SSH`), with
`RM_SSH_CONFIG`, a sandboxed `known_hosts` and mDNS off, so no test reaches a real host.
Under it, `probe.sh` still runs for real on the test machine.
