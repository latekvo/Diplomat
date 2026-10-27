// install.js against a daemon.json that outlived its daemon, and beside a daemon that is
// alive but not answering.
//
// A daemon killed without its exit handler leaves daemon.json naming its pid, which any
// later process of the user's may hold: --uninstall and --restart-daemon must not signal
// it. A daemon that is alive but silent (one shutting down blocks its event loop while it
// shuts its simulators down) must not get a second daemon started beside it, and a
// daemon that took the socket from it must keep it once the silent one exits.
//
// Against a throwaway HOME + DA_BASE_DIR and a fake device pool.
// Run: node test/stale-daemon-pid.mjs

import { execFileSync, spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INSTALL_JS = path.join(__dirname, '..', 'src', 'install.js');
const DAEMON_JS = path.join(__dirname, '..', 'src', 'daemon.js');

// /tmp, not os.tmpdir(): macOS's per-user tmpdir pushes the socket past the length limit.
const BASE = fs.mkdtempSync('/tmp/dae.');
const HOME = path.join(BASE, 'home');
const SOCKET = path.join(BASE, 'daemon.sock');
const DISCOVERY = path.join(BASE, 'daemon.json');
const FAKE = path.join(BASE, 'fake.json');
fs.mkdirSync(HOME);
fs.writeFileSync(FAKE, '[]');
const ENV = {
  ...process.env, HOME, DA_BASE_DIR: BASE, DA_FAKE_DEVICES: FAKE, DA_NO_SPAWN: '1',
  DA_BAN_DIR: path.join(BASE, 'ban'), DIPLOMAT_CONFIG: path.join(BASE, 'config.json'),
};

const run = (arg) =>
  JSON.parse(execFileSync(process.execPath, [INSTALL_JS, arg], { encoding: 'utf8', env: ENV }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
async function until(cond, ms) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(100)) if (await cond()) return true;
  return false;
}
function health() {
  return new Promise((resolve) => {
    const req = http.request({ socketPath: SOCKET, path: '/health', timeout: 1500, agent: false },
      (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve(JSON.parse(b || '{}').pid ?? null)); });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.end();
  });
}
const discoveryPid = () => { try { return JSON.parse(fs.readFileSync(DISCOVERY, 'utf8')).pid; } catch { return null; } };
function staleDiscovery(pid, startedAt) {
  fs.writeFileSync(DISCOVERY, JSON.stringify({ pid, startedAt: startedAt.toISOString(), socket: SOCKET, version: 1 }));
}
// Daemons running this checkout's daemon.js; only this test starts one.
const daemons = () => {
  try { return execFileSync('pgrep', ['-f', DAEMON_JS], { encoding: 'utf8' }).trim().split('\n').map(Number); }
  catch { return []; }
};

const children = [];
function victim(cmd, args) {
  const child = spawn(cmd, args, { stdio: 'ignore' });
  children.push(child);
  return child;
}
// Whether a victim still runs, once an exit (if any) has been reaped.
async function survived(child) {
  await sleep(500);
  return child.exitCode === null && child.signalCode === null;
}

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

try {
  console.log('install: a stale daemon.json, and a silent daemon');

  // A pid now held by something that is not a daemon at all.
  const sleeper = victim('sleep', ['300']);
  await sleep(300);
  staleDiscovery(sleeper.pid, new Date());
  const uninstalled = run('--uninstall');
  ok('--uninstall leaves a process that is not a daemon alone', await survived(sleeper));
  ok('…and drops the daemon.json naming it', !fs.existsSync(DISCOVERY) && uninstalled.daemonRunning === false);

  // A process that looks like a daemon, but started after the daemon.json was written.
  const lookalike = victim(process.execPath, ['-e', 'setInterval(() => {}, 1e9)', DAEMON_JS]);
  await sleep(300);
  staleDiscovery(lookalike.pid, new Date(Date.now() - 3600_000));
  const restarted = run('--restart-daemon');
  ok('--restart-daemon leaves a reused pid alone', await survived(lookalike));
  const first = await health();
  ok('…and starts a daemon', restarted.daemonRunning === true && restarted.error === undefined && first != null);
  ok('…whose daemon.json is its own', discoveryPid() === first);
  for (const c of children) c.kill('SIGKILL');
  await until(() => lookalike.signalCode != null, 5000);

  // The daemon that answers, stopped: alive, its daemon.json its own, and silent.
  process.kill(first, 'SIGSTOP');
  const refused = run('--start-daemon');
  ok('--start-daemon refuses beside a silent daemon',
    refused.daemonRunning === false && refused.error?.includes(`pid ${first}`));
  ok('…and starts no second one', daemons().length === 1);

  // A daemon that takes the socket from the silent one, which then exits.
  const second = spawn(process.execPath, [DAEMON_JS], { env: ENV, stdio: 'ignore', detached: true });
  second.unref();
  ok('a second daemon takes the socket from the silent one',
    await until(async () => (await health()) === second.pid, 25000));
  process.kill(first, 'SIGTERM');
  process.kill(first, 'SIGCONT');
  ok('the silent daemon exits on SIGTERM', await until(() => !alive(first), 10000));
  ok('…leaving the socket to the one that took it', (await health()) === second.pid);
  ok('…and its daemon.json', discoveryPid() === second.pid);

  // A silent daemon that really is this one is still stopped.
  process.kill(second.pid, 'SIGSTOP');
  run('--uninstall');
  process.kill(second.pid, 'SIGCONT');
  ok('--uninstall stops a silent daemon through its daemon.json', await until(() => !alive(second.pid), 10000));
  ok('no daemon is left', await until(() => daemons().length === 0, 5000));

  console.log(`\nstale-daemon-pid: ${passed} passed`);
} finally {
  for (const c of children) { try { c.kill('SIGKILL'); } catch {} }
  for (const pid of daemons()) { try { process.kill(pid, 'SIGCONT'); process.kill(pid, 'SIGKILL'); } catch {} }
  await sleep(300);
  fs.rmSync(BASE, { recursive: true, force: true });
}
