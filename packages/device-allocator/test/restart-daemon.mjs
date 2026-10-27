// `install.js --restart-daemon`, against a throwaway HOME + DA_BASE_DIR and a fake device
// pool, never the developer's real daemon, ~/.claude or simulators.
// Run: node test/restart-daemon.mjs

import { execFileSync } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INSTALL_JS = path.join(__dirname, '..', 'src', 'install.js');

// /tmp, not os.tmpdir(): macOS's per-user tmpdir pushes the socket past the length limit.
const BASE = fs.mkdtempSync('/tmp/dae.');
const HOME = path.join(BASE, 'home');
const SOCKET = path.join(BASE, 'daemon.sock');
const FAKE = path.join(BASE, 'fake.json');
fs.mkdirSync(HOME);
fs.writeFileSync(FAKE, JSON.stringify([
  { key: 'ios:B', platform: 'ios', handle: 'B', udid: 'B', name: 'iPhone 15', version: '17.5',
    apiVersion: '17', format: 'phone', state: 'booted' },
]));
const ENV = {
  ...process.env, HOME, DA_BASE_DIR: BASE, DA_FAKE_DEVICES: FAKE, DA_NO_SPAWN: '1',
  DA_BAN_DIR: path.join(BASE, 'ban'), DIPLOMAT_CONFIG: path.join(BASE, 'config.json'),
};

function run(arg) {
  return JSON.parse(execFileSync(process.execPath, [INSTALL_JS, arg], { encoding: 'utf8', env: ENV }));
}
function call(method, route, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    // No keep-alive: a pooled connection to the daemon a restart ended would EPIPE.
    const req = http.request({ socketPath: SOCKET, path: route, method, timeout: 8000, agent: false,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } },
      (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve(JSON.parse(b || '{}'))); });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (data) req.write(data);
    req.end();
  });
}
const pidOf = async () => (await call('GET', '/health')).pid;
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

try {
  console.log('install: --restart-daemon');

  const started = run('--start-daemon');
  ok('the daemon starts', started.daemonRunning === true);
  const first = await pidOf();

  const lease = await call('POST', '/request', { ownerPid: process.pid, platform: 'ios', agentName: 'restart-test' });
  ok('a device is leased before the restart', lease.key === 'ios:B');

  ok('--start-daemon keeps the running daemon', run('--start-daemon').daemonRunning && (await pidOf()) === first);

  const restarted = run('--restart-daemon');
  ok('the restart reports a running daemon', restarted.daemonRunning === true && restarted.error === undefined);
  ok('the restart reports the install status', restarted.installed === false && 'installed' in restarted);
  const second = await pidOf();
  ok('a different daemon answers', second !== first);
  ok('the old daemon is gone', !alive(first));

  const row = (await call('GET', '/state')).devices.find((d) => d.key === 'ios:B');
  ok('the lease survives the restart',
    row?.owner?.ownerPid === process.pid && row?.owner?.agentName === 'restart-test');

  process.kill(second, 'SIGTERM');
  for (let i = 0; i < 50 && alive(second); i++) await new Promise((r) => setTimeout(r, 100));
  const fromNothing = run('--restart-daemon');
  ok('with no daemon running, a restart starts one', fromNothing.daemonRunning === true);
  ok('…a new one', (await pidOf()) !== second);

  console.log(`\nrestart-daemon: ${passed} passed`);
} finally {
  try { process.kill(await pidOf(), 'SIGTERM'); } catch {}
  await new Promise((r) => setTimeout(r, 300));
  fs.rmSync(BASE, { recursive: true, force: true });
}
