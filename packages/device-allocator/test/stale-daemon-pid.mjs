// install.js beside a daemon.json that outlived its daemon, and beside a daemon alive but
// silent (one shutting down blocks its event loop while it shuts its simulators down).
// Against a throwaway HOME + DA_BASE_DIR; the user's own daemon may run this same
// daemon.js, so only daemons under that DA_BASE_DIR are counted or signalled.
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
const { procIdentity } = await import('../src/state.js');

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
function staleDiscovery(pid, startedAt, identity) {
  fs.writeFileSync(DISCOVERY, JSON.stringify({ pid, identity, startedAt: startedAt.toISOString(), socket: SOCKET, version: 1 }));
}
// A process's environment, as words: Linux /proc, else (macOS) after the command in ps -E.
function environ(pid) {
  try { return fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0'); } catch {}
  try { return execFileSync('ps', ['-E', '-ww', '-o', 'command=', '-p', String(pid)], { encoding: 'utf8' }).split(/\s+/); }
  catch { return []; }
}
function daemons() {
  let pids;
  try { pids = execFileSync('pgrep', ['-f', DAEMON_JS], { encoding: 'utf8' }).trim().split('\n').map(Number); }
  catch { return []; }
  return pids.filter((pid) => environ(pid).includes(`DA_BASE_DIR=${BASE}`));
}

const children = [];
function victim(cmd, args) {
  const child = spawn(cmd, args, { stdio: 'ignore' });
  children.push(child);
  return child;
}
// Still running, once any exit has had time to be reaped.
async function survived(child) {
  await sleep(500);
  return child.exitCode === null && child.signalCode === null;
}

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

try {
  console.log('install: a stale daemon.json, and a silent daemon');

  const sleeper = victim('sleep', ['300']);
  await sleep(300);
  staleDiscovery(sleeper.pid, new Date());
  const uninstalled = run('--uninstall');
  ok('--uninstall leaves a process that is not a daemon alone', await survived(sleeper));
  ok('…and drops the daemon.json naming it', !fs.existsSync(DISCOVERY) && uninstalled.daemonRunning === false);

  // Its own identity, as a Linux boot-relative starttime can recur after a reboot.
  const forged = victim('sleep', ['300']);
  await sleep(300);
  staleDiscovery(forged.pid, new Date(), procIdentity(forged.pid));
  run('--uninstall');
  ok('--uninstall leaves a non-daemon whose identity daemon.json records', await survived(forged));

  // A daemon.js lookalike, which by command and start alone passes for the file's daemon.
  const lookalike = victim(process.execPath, ['-e', 'setInterval(() => {}, 1e9)', DAEMON_JS]);
  await sleep(300);
  staleDiscovery(lookalike.pid, new Date(), 'not-its-own');
  run('--uninstall');
  ok('--uninstall leaves a pid whose identity is not the one daemon.json records', await survived(lookalike));
  // No identity, and a start after the file's.
  staleDiscovery(lookalike.pid, new Date(Date.now() - 3600_000));
  const restarted = run('--restart-daemon');
  ok('--restart-daemon leaves a reused pid alone', await survived(lookalike));
  const first = await health();
  ok('…and starts a daemon', restarted.daemonRunning === true && restarted.error === undefined && first != null);
  ok('…whose daemon.json is its own', discoveryPid() === first);
  const own = JSON.parse(fs.readFileSync(DISCOVERY, 'utf8'));
  for (const c of children) c.kill('SIGKILL');
  await until(() => lookalike.signalCode != null, 5000);

  // The real daemon, stopped so it is silent, with a startedAt an hour before its start,
  // as Linux ps lstart reads it after a forward wall-clock step.
  fs.writeFileSync(DISCOVERY, JSON.stringify({ ...own, startedAt: new Date(Date.parse(own.startedAt) - 3600_000).toISOString() }));
  process.kill(first, 'SIGSTOP');
  const refused = run('--start-daemon');
  ok('--start-daemon refuses beside a silent daemon, whatever the wall clock did',
    refused.daemonRunning === false && refused.error?.includes(`pid ${first}`));
  ok('…and starts no second one', JSON.stringify(daemons()) === JSON.stringify([first]));

  const second = spawn(process.execPath, [DAEMON_JS], { env: ENV, stdio: 'ignore', detached: true });
  second.unref();
  ok('a second daemon takes the socket from the silent one',
    await until(async () => (await health()) === second.pid, 25000));
  process.kill(first, 'SIGTERM');
  process.kill(first, 'SIGCONT');
  ok('the silent daemon exits on SIGTERM', await until(() => !alive(first), 10000));
  ok('…leaving the socket to the one that took it', (await health()) === second.pid);
  ok('…and its daemon.json', discoveryPid() === second.pid);

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
