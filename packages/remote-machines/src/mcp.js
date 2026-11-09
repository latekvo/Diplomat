#!/usr/bin/env node
// The per-agent stdio MCP server. Claude Code launches one per session; it is a thin
// forwarder to the shared remote-machines daemon, which it starts. Its pid tells the
// daemon a session is still around (the daemon exits once none is).
//
// The steering lives in three places an agent cannot miss: the `instructions` on the
// handshake (injected whenever the server is connected), `list-machines` marked
// alwaysLoad, and the tool description itself.

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { callDaemon, daemonHealth, spawnDaemon } from './ipc.js';
import { formatSnapshot } from './format.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OWNER_PID = process.pid;

const INSTRUCTIONS =
  'Other machines on this network are available to you over SSH, and this machine is shared by ' +
  'many agents at once. For ANY task that produces an artifact - a build, a compiled binary or app ' +
  'bundle, a test or lint run, a package, a container image, a render or video, generated docs, a ' +
  'dataset, a benchmark - PREFER running it on a remote machine instead of this one. Before such ' +
  'work, call list-machines and pick a machine without warnings and with spare cores, RAM and disk. ' +
  'Then copy the inputs there (`rsync -az --exclude node_modules --exclude .git <dir>/ ' +
  '<target>:offload/<task>/`, or `git clone`/`git fetch` of pushed work), run the work with ' +
  '`ssh <target> \'cd offload/<task> && ...\'`, copy back only the artifacts and results you need, and ' +
  'delete offload/<task> on the remote when you are done. Run it here instead when list-machines shows ' +
  'no usable machine, when the work needs something only this machine has (its simulators and ' +
  'devices, its GUI, its local services, credentials that must not leave it), or when the remote lacks ' +
  'the toolchain. Never copy secrets (.env files, keys, tokens) to a remote, and leave nothing running ' +
  'there when you finish.';

const TOOLS = [
  {
    name: 'list-machines',
    description:
      'List the machines you can offload work to over SSH, best first, with what each can do (OS, CPU, ' +
      'GPU, RAM, toolchains) and how loaded it is right now (load average now, over 5 minutes and since ' +
      'the last scan, free RAM, swap, free disk), plus this machine for comparison. Call it BEFORE any ' +
      'build, test run or other work that produces an artifact, and run that work on the best machine ' +
      'it lists. Load is at most a minute old; pass refresh: true to rediscover machines and re-read ' +
      'everything now (takes a few seconds).',
    inputSchema: {
      type: 'object',
      properties: {
        refresh: { type: 'boolean', description: 'Rediscover machines and re-read their capabilities and load before answering.' },
      },
      required: [],
    },
    _meta: { 'anthropic/alwaysLoad': true },
  },
];

async function ensureDaemon() {
  if (!(await daemonHealth())) await spawnDaemon(12000);
}

const VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8')).version;

const server = new Server(
  { name: 'diplomat-remote-machines', version: VERSION },
  { capabilities: { tools: {} }, instructions: INSTRUCTIONS },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name;
  const args = req.params.arguments || {};
  try {
    if (name !== 'list-machines') throw new Error(`unknown tool ${name}`);
    await ensureDaemon();
    const snapshot = args.refresh
      ? await callDaemon('POST', '/scan', { ownerPid: OWNER_PID, full: true }, { timeout: 120000 })
      : (await callDaemon('POST', '/attach', { ownerPid: OWNER_PID }), await callDaemon('GET', '/state', null, { timeout: 120000 }));
    return { content: [{ type: 'text', text: formatSnapshot(snapshot) }] };
  } catch (e) {
    return { content: [{ type: 'text', text: `remote-machines error: ${e.message || e}` }], isError: true };
  }
});

const transport = new StdioServerTransport();
transport.onclose = () => process.exit(0); // stdin closed ⇒ agent gone
await server.connect(transport);

// Start scanning as the session starts, so the table is warm by the first call.
ensureDaemon()
  .then(() => callDaemon('POST', '/attach', { ownerPid: OWNER_PID }, { timeout: 5000 }))
  .catch(() => {});
