// Drives the real stdio MCP server (src/mcp.js) over JSON-RPC, the way Claude Code does,
// against fake hosts: the handshake carries the offloading instructions, list-machines
// is advertised with alwaysLoad, a call starts the daemon and answers with the table,
// a refresh rescans, and closing stdin ends the server.
// Run: node test/mcp-protocol.mjs

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sandbox } from './lib/sandbox.mjs';

const MCP = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'mcp.js');
const sb = sandbox({
  hosts: {
    buildbox: { hostname: 'buildbox.local', user: 'ci', overrides: { machine_id: 'M-BUILD', ncpu: '16', loadavg: '1.00 1.00 1.00' } },
    locked: { hostname: 'locked.local', behavior: 'auth' },
  },
  sshConfig: 'Host buildbox locked\n',
  known: ['buildbox.local', 'locked.local'],
});

const child = spawn(process.execPath, [MCP], { stdio: ['pipe', 'pipe', 'inherit'], env: sb.env });
const pending = new Map();
let buf = '';
child.stdout.on('data', (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    pending.get(msg.id)?.(msg);
    pending.delete(msg.id);
  }
});
let nextId = 1;
const rpc = (method, params) => new Promise((resolve, reject) => {
  const id = nextId++;
  const timer = setTimeout(() => reject(new Error(`timeout: ${method}`)), 30000);
  pending.set(id, (m) => { clearTimeout(timer); (m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result)); });
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
});

let checks = 0;
const check = async (name, fn) => { await fn(); checks++; console.log(`  PASS ${name}`); };

try {
  const init = await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } });
  child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);

  await check('the handshake steers artifact-producing work to remote machines', () => {
    assert.equal(init.serverInfo.name, 'diplomat-remote-machines');
    assert.match(init.instructions, /For ANY task that produces an artifact/);
    assert.match(init.instructions, /call list-machines/);
  });

  await check('list-machines is the one tool, always loaded', async () => {
    const { tools } = await rpc('tools/list', {});
    assert.deepEqual(tools.map((t) => t.name), ['list-machines']);
    assert.equal(tools[0]._meta['anthropic/alwaysLoad'], true);
  });

  await check('a call answers with this machine, the usable ones and the rest', async () => {
    const r = await rpc('tools/call', { name: 'list-machines', arguments: {} });
    const text = r.content[0].text;
    assert.equal(r.isError, undefined, text);
    assert.match(text, /^This machine \(/);
    assert.match(text, /\n1\. buildbox \(also buildbox\.local\) - ci@buildbox\.local\n/);
    assert.match(text, /load 1 now, 1 over 5 min/);
    assert.match(text, /15 spare cores/);
    assert.match(text, /Not usable \(1\) - SSH refused our keys: locked\./);
  });

  await check('refresh: true rescans', async () => {
    const before = sb.probed().length;
    const r = await rpc('tools/call', { name: 'list-machines', arguments: { refresh: true } });
    assert.match(r.content[0].text, /buildbox/);
    assert.ok(sb.probed().length > before, 'a refresh probes again');
  });

  await check('closing stdin ends the server', async () => {
    const exited = new Promise((resolve) => child.once('exit', resolve));
    child.stdin.end();
    assert.equal(await exited, 0);
  });

  console.log(`MCP PROTOCOL OK — ${checks} checks passed`);
} finally {
  child.kill('SIGKILL');
  sb.cleanup();
}
