// Drives the real stdio MCP server (src/mcp.js) over JSON-RPC, the same way
// Claude Code does, against a fake device pool. Proves: the handshake carries
// the coercive `instructions`, request-device is advertised with the alwaysLoad
// meta, and a tools/call actually allocates a device through the daemon.
// Run: node test/mcp-protocol.mjs

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const MCP = path.join(__dirname, '..', 'src', 'mcp.js');

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-mtest-'));
const FAKE = path.join(BASE, 'fake.json');
fs.writeFileSync(FAKE, JSON.stringify([
  { key: 'ios:UDID-1', platform: 'ios', handle: 'UDID-1', udid: 'UDID-1', name: 'iPhone 16', version: '18.5', apiVersion: '18', state: 'booted' },
]));

const child = spawn(process.execPath, [MCP], {
  stdio: ['pipe', 'pipe', 'inherit'],
  env: { ...process.env, DA_BASE_DIR: BASE, DA_FAKE_DEVICES: FAKE, DA_NO_SPAWN: '1', DA_NO_AUTO_REPAIR: '1' },
});

const pending = new Map();
let buf = '';
child.stdout.on('data', (chunk) => {
  buf += chunk.toString();
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg; try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id != null && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  }
});

let nextId = 1;
function rpc(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, (m) => (m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result)));
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(`timeout: ${method}`)); } }, 15000);
  });
}
function notify(method, params) { child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`); }

let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

try {
  const init = await rpc('initialize', {
    protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' },
  });
  assert.ok(/MANDATORY|MUST/.test(init.instructions || ''), 'handshake instructions missing the coercion');
  pass('initialize handshake carries the coercive instructions');
  notify('notifications/initialized');

  const list = await rpc('tools/list', {});
  const names = list.tools.map((t) => t.name);
  for (const n of ['request-device', 'free-device', 'change-device', 'report-device-broken']) {
    assert.ok(names.includes(n), `tool ${n} missing`);
  }
  pass(`tools/list advertises all 4 tools: ${names.join(', ')}`);

  const reqTool = list.tools.find((t) => t.name === 'request-device');
  assert.equal(reqTool._meta?.['anthropic/alwaysLoad'], true, 'request-device must be alwaysLoad');
  pass('request-device is marked alwaysLoad (always in the tool list)');

  const called = await rpc('tools/call', { name: 'request-device', arguments: { platform: 'ios', agentName: 'mtest' } });
  const text = called.content?.[0]?.text || '';
  assert.ok(/UDID-1/.test(text) && /Allocated/.test(text), `unexpected allocation result: ${text}`);
  pass('tools/call request-device allocates a device end-to-end through the daemon');

  const freed = await rpc('tools/call', { name: 'free-device', arguments: { deviceId: 'UDID-1' } });
  assert.ok(/Released/.test(freed.content?.[0]?.text || ''), 'free-device did not release');
  pass('tools/call free-device releases the device');

  console.log(`\nMCP PROTOCOL OK — ${ok} assertions passed`);
} catch (e) {
  console.error('\nMCP PROTOCOL FAILED:', e.message);
  process.exitCode = 1;
} finally {
  try { child.stdin.end(); } catch {}
  try { child.kill('SIGTERM'); } catch {}
  // stop the daemon this test spawned
  try {
    const disc = JSON.parse(fs.readFileSync(path.join(BASE, 'daemon.json'), 'utf8'));
    if (disc.pid) process.kill(disc.pid, 'SIGTERM');
  } catch {}
  try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
}
