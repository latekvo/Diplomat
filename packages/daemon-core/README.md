# daemon-core

What Diplomat's MCP servers share: the [device allocator](../device-allocator/README.md)
and [remote machines](../remote-machines/README.md) each run a per-agent stdio MCP server
in front of one daemon per machine, installed into Claude Code the same way. No
dependencies and no build step; the packages import `src/*.js` by relative path.

| | |
|---|---|
| [`src/serve.js`](src/serve.js) | the daemon side: a JSON-over-HTTP server on a unix socket, and taking that socket as the one daemon of its state dir (a stale socket is reclaimed under a lock, never from a live peer) |
| [`src/ipc.js`](src/ipc.js) | the client side: call a daemon, check it is up, start one and wait for it to serve |
| [`src/daemon-control.js`](src/daemon-control.js) | start, stop and replace a daemon from an installer, found by its socket or its daemon.json |
| [`src/claude-install.js`](src/claude-install.js) | install into Claude Code at every layer an agent reads (MCP registration, rule, CLAUDE.md block, skill), and report drift from the checkout by content |
| [`src/fsio.js`](src/fsio.js) | atomic writes, and pid liveness and identity |
| [`src/log.js`](src/log.js) | the append-only daemon log |
| [`src/test-runner.js`](src/test-runner.js), [`src/test-interrupt.js`](src/test-interrupt.js) | each package's `npm test`: every `test/*.mjs`, discovered rather than listed, and the cleanup a signal would otherwise skip |

It has no tests of its own: both packages' suites drive it through their real daemons.
