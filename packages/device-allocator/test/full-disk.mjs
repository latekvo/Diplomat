// The daemon on a disk that fills up, against a real full filesystem
// (scripts/full-volume.sh), not a mocked ENOSPC.
//
// 1. A daemon started with space keeps serving every route while the volume is
//    full (pool/reap ticks, leases, a reap, a repair dispatch, ban and unban), leaves
//    no tmp file, and once space returns its state.json, leases.json and daemon.json
//    catch up to what happened meanwhile, with no restart.
// 2. A daemon started on an already-full volume, both fresh and over a crashed
//    daemon's stale socket: it serves wherever the kernel can still bind a socket
//    (tmpfs always, APFS when a sliver is left), and otherwise exits 1 rather than
//    claim a live peer holds the socket. One serving without the daemon.json the
//    disk refused is still found on its socket: an MCP tool call starts no
//    throwaway daemon, --check sees it and --uninstall stops it. Once space
//    returns, its own tick writes daemon.json with no request to prompt it, and
//    every daemon catches up, or starts.
// 3. A starting daemon removes the tmp files of writers that died mid-write.
//
// A machine that cannot mount a volume skips (exit 77 from the fixture), unless
// DIPLOMAT_FULL_VOLUME_REQUIRED=1. Run: node test/full-disk.mjs

import { spawn, spawnSync } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');
const MCP = path.join(__dirname, '..', 'src', 'mcp.js');
const INSTALL = path.join(__dirname, '..', 'src', 'install.js');
const VOLUME_SH = path.join(__dirname, '..', '..', '..', 'scripts', 'full-volume.sh');

// Short: the daemon's socket path lives under it and sun_path caps at 104 bytes.
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'da-full-'));
const VOL = path.join(ROOT, 'v');
const BIN = path.join(ROOT, 'bin');
const FAKE = path.join(ROOT, 'fake.json');
const CONFIG = path.join(ROOT, 'config.json');
const TERMINAL_RECORD = path.join(ROOT, 'terminal-argv');
const HOME = path.join(ROOT, 'home');
// Every node process a client starts appends its script here, off the full volume.
const TRACE = path.join(ROOT, 'node-starts');
const TRACER = path.join(ROOT, 'trace.cjs');
fs.writeFileSync(TRACER, `require('fs').appendFileSync(${JSON.stringify(TRACE)}, process.argv[1] + '\\n');\n`);
fs.mkdirSync(HOME);

fs.writeFileSync(FAKE, JSON.stringify([
  ...['A', 'B', 'C', 'D', 'E'].map((id) => ({
    key: `ios:${id}`, platform: 'ios', handle: id, udid: id, name: `iPhone ${id}`, version: '18.5',
    apiVersion: '18', format: 'phone', state: id === 'B' ? 'booted' : 'shutdown',
  })),
  { key: 'android:P', platform: 'android', avd: 'P', name: 'P', version: '14', apiVersion: '34', format: 'phone', serial: null, state: 'shutdown' },
]));
// The repair window opens through a recording stand-in: osascript on macOS, xterm
// (the published terminal) on Linux.
fs.mkdirSync(BIN);
for (const t of ['osascript', 'xterm']) {
  fs.writeFileSync(path.join(BIN, t), `#!/bin/sh\necho "$@" >> '${TERMINAL_RECORD}'\n`, { mode: 0o755 });
}
fs.writeFileSync(CONFIG, JSON.stringify({ spawnTerminal: 'xterm' }));

const ME = process.pid; // an alive owner the reaper must never free
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

function volume(cmd) {
  return spawnSync('bash', [VOLUME_SH, cmd, VOL], { encoding: 'utf8' });
}

// A running daemon frees and takes space as its writes fail and clean up, so a fill
// can find room left behind it, and on APFS a drain's first unlinks can themselves
// hit ENOSPC. Each converges on a retry.
async function settle(cmd) {
  let r;
  for (let i = 0; i < 20; i++) {
    r = volume(cmd);
    if (r.status === 0) return;
    await delay(250);
  }
  const df = spawnSync('df', ['-k', VOL], { encoding: 'utf8' }).stdout;
  const ls = spawnSync('ls', ['-laR', VOL], { encoding: 'utf8' }).stdout;
  assert.fail(`${cmd} failed: ${r.stderr}\n${df}\n${ls}`);
}

function call(base, method, route, body, timeout = 8000) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({ socketPath: path.join(base, 'daemon.sock'), path: route, method, timeout,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } },
      (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => {
        let p; try { p = b ? JSON.parse(b) : {}; } catch { p = { raw: b }; }
        res.statusCode >= 200 && res.statusCode < 300 ? resolve(p)
          : reject(Object.assign(new Error(p.error || `status ${res.statusCode}`), { statusCode: res.statusCode })); }); });
    req.on('error', reject); req.on('timeout', () => req.destroy(new Error('timeout')));
    if (data) req.write(data); req.end();
  });
}

const daemons = [];
function startDaemon(base) {
  const child = spawn(process.execPath, [DAEMON], {
    stdio: ['ignore', 'ignore', 'pipe'],
    env: { ...process.env, DA_BASE_DIR: base, DA_BAN_DIR: path.join(VOL, 'ban'), DA_FAKE_DEVICES: FAKE,
      PATH: `${BIN}:/usr/bin:/bin`, DIPLOMAT_CONFIG: CONFIG, DIPLOMAT_TMUX: path.join(ROOT, 'no-tmux'),
      // Fast ticks: a full volume sees dozens of pool refreshes and reaps.
      DA_ALLOC_GRACE_MS: '100', DA_REAP_INTERVAL_MS: '50', DA_POOL_INTERVAL_MS: '50',
      DA_IDLE_INTERVAL_MS: '100', DA_IDLE_LIMIT_MS: '999999' },
  });
  child.stderrText = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (c) => { child.stderrText += c; });
  daemons.push(child);
  return child;
}
// A client's environment: the sandboxed HOME, and every node it starts traced.
const clientEnv = (base) => ({ ...process.env, HOME, DA_BASE_DIR: base, DA_BAN_DIR: path.join(VOL, 'ban'),
  DA_FAKE_DEVICES: FAKE, NODE_OPTIONS: `--require ${TRACER}` });
const daemonStarts = () => (fs.existsSync(TRACE) ? fs.readFileSync(TRACE, 'utf8').split('\n') : [])
  .filter((l) => l.endsWith(`${path.sep}daemon.js`));
// One tool call through the real stdio MCP server; the result text once it has exited.
function mcpCall(base, name, args) {
  const child = spawn(process.execPath, [MCP], { stdio: ['pipe', 'pipe', 'ignore'], env: clientEnv(base) });
  const send = (m) => child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', ...m })}\n`);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`${name}: no answer`)); }, 20000);
    let buf = '';
    let text;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (c) => {
      buf += c;
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        const m = JSON.parse(line);
        if (m.id === 1) {
          send({ method: 'notifications/initialized' });
          send({ id: 2, method: 'tools/call', params: { name, arguments: args } });
        } else if (m.id === 2) { text = m.result.content[0].text; child.stdin.end(); }
      }
    });
    child.on('exit', () => { clearTimeout(timer); text === undefined ? reject(new Error(`${name}: exited unanswered`)) : resolve(text); });
    send({ id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'full-disk', version: '0' } } });
  });
}
function installer(base, arg) {
  const r = spawnSync(process.execPath, [INSTALL, arg], { encoding: 'utf8', env: clientEnv(base), timeout: 30000 });
  return JSON.parse(r.stdout);
}
const discoveredPid = (base) => { try { return readJson(path.join(base, 'daemon.json')).pid; } catch { return null; } };
const gone = (d) => d.exitCode !== null || d.signalCode !== null;

async function waitHealth(base) {
  for (let i = 0; i < 60; i++) { try { return await call(base, 'GET', '/health'); } catch { await delay(100); } }
  throw new Error(`daemon on ${base} never became healthy`);
}
async function waitExit(child, ms = 8000) {
  for (let i = 0; i < ms / 50 && child.exitCode === null && child.signalCode === null; i++) await delay(50);
}
async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  await waitExit(child, 3000);
  if (child.exitCode === null && child.signalCode === null) { child.kill('SIGKILL'); await waitExit(child, 3000); }
}
function assertAlive(child, what) {
  assert.ok(child.exitCode === null && child.signalCode === null,
    `${what}: daemon died (exit ${child.exitCode}, signal ${child.signalCode})\n${child.stderrText}`);
}

function tmpFiles(dir = VOL) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === '.ballast') continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...tmpFiles(p));
    else if (/\.tmp\.\d+$/.test(e.name)) out.push(p);
  }
  return out;
}
// A live daemon's in-flight write shows a tmp file for a moment; a leaked one
// stays. Only tmp files present in every scan count.
async function leakedTmpFiles() {
  let seen = tmpFiles();
  for (let i = 0; i < 5 && seen.length; i++) {
    await delay(100);
    const now = tmpFiles();
    seen = seen.filter((f) => now.includes(f));
  }
  return seen;
}
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
// The launcher each repair window was opened on.
const opened = () => (fs.existsSync(TERMINAL_RECORD)
  ? fs.readFileSync(TERMINAL_RECORD, 'utf8').trim().split('\n').map((l) => l.match(/[^\s']+\.sh/)[0]) : []);
async function waitOpened(n) {
  for (let i = 0; i < 60 && opened().length < n; i++) await delay(50);
  return opened().length;
}
const launchers = (base) => {
  const dir = path.join(base, 'repairs');
  return fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith('.sh')).map((f) => path.join(dir, f)) : [];
};
// Every launcher on disk is whole, and every window opened on one of them.
function assertLaunchersWhole(base) {
  const onDisk = launchers(base);
  for (const l of onDisk) assert.ok(fs.readFileSync(l, 'utf8').endsWith('read _\n'), `half-written launcher ${l}`);
  for (const l of opened()) assert.ok(onDisk.includes(l), `a repair window opened on a missing launcher ${l}`);
  return onDisk.length;
}
const ownedOf = (state) => Object.fromEntries(state.devices.filter((d) => d.owner)
  .map((d) => [d.key, { status: d.status, ownerPid: d.owner.ownerPid }]));
const leasesOf = (base) => Object.fromEntries(readJson(path.join(base, 'leases.json')).leases
  .map((a) => [a.key, { status: a.status, ownerPid: a.ownerPid }]));
const devicesOf = (state) => state.devices.map((d) => ({ key: d.key, status: d.status, owner: d.owner }));

// Until the files on disk match what the daemon serves, or the deadline.
async function caughtUp(base, pid) {
  let last;
  for (let i = 0; i < 60; i++) {
    try {
      const live = await call(base, 'GET', '/state');
      const disk = readJson(path.join(base, 'state.json'));
      assert.equal(readJson(path.join(base, 'daemon.json')).pid, pid, 'daemon.json names the running daemon');
      assert.equal(disk.daemonPid, pid, 'state.json was written by the running daemon');
      assert.deepEqual(devicesOf(disk), devicesOf(live), 'state.json matches /state');
      assert.deepEqual(leasesOf(base), ownedOf(live), 'leases.json matches the leases /state shows');
      return live;
    } catch (e) { last = e; await delay(100); }
  }
  throw last;
}

let ok = 0;
function pass(msg) { ok++; console.log(`  PASS ${msg}`); }

async function scenarioRunningDaemon() {
  console.log('scenario 1: a running daemon while the disk fills, then drains');
  const base = path.join(VOL, 'a');
  const d = startDaemon(base);
  await waitHealth(base);
  // Leases made with space: one kept, one quarantined (its repair window opens and
  // creates repairs/), one owned by a process that dies while the disk is full.
  const kept = await call(base, 'POST', '/request', { ownerPid: ME, platform: 'ios', agentName: 'kept' });
  assert.equal(kept.deviceId, 'B');
  await call(base, 'POST', '/request', { ownerPid: ME, platform: 'ios', agentName: 'swap' });
  const swapped = await call(base, 'POST', '/broken', { ownerPid: ME, deviceId: 'A', reason: 'pre-fill' });
  const owner = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60000)'], { stdio: 'ignore' });
  await call(base, 'POST', '/request', { ownerPid: owner.pid, platform: 'android', agentName: 'doomed' });
  await call(base, 'POST', '/report-injection', { person: 'alice', evidence: 'pre-fill ban' });
  await caughtUp(base, d.pid);
  assert.equal(await waitOpened(1), 1);
  assert.equal(assertLaunchersWhole(base), 1);
  pass('leases, a repair dispatch and a ban are persisted while there is space');

  await settle('fill');
  let answered = 0;
  const expectCode = async (code, p) => {
    try { await p; assert.fail(`expected ${code}, got 200`); } catch (e) { assert.equal(e.statusCode, code, e.message); return e; }
  };
  await delay(1000); // ~20 pool refreshes and reaps against a full disk
  assertAlive(d, 'after pool ticks on a full disk');
  owner.kill('SIGKILL');
  let reaped = false;
  for (let i = 0; i < 40 && !reaped; i++) {
    reaped = (await call(base, 'GET', '/state')).devices.find((x) => x.key === 'android:P').owner == null;
    answered++;
    if (!reaped) await delay(50);
  }
  assert.ok(reaped, 'the dead owner\'s lease was not reaped');
  for (let i = 0; i < 10; i++) {
    const r = await call(base, 'POST', '/request', { ownerPid: ME, platform: 'ios', agentName: `churn${i}` });
    await call(base, 'POST', '/release', { ownerPid: ME, deviceId: r.deviceId });
    answered += 2;
  }
  const changed = await call(base, 'POST', '/change', { ownerPid: ME, deviceId: swapped.deviceId, platform: 'ios' });
  assert.notEqual(changed.deviceId, swapped.deviceId);
  const replacement = await call(base, 'POST', '/broken', { ownerPid: ME, deviceId: changed.deviceId, reason: 'while full' });
  assert.equal(replacement.outcome, 'allocated');
  answered += 2;
  const ban = await expectCode(507, call(base, 'POST', '/report-injection', { person: 'mallory', evidence: 'while full' }));
  assert.match(ban.message, /ban on @mallory not recorded: .*ENOSPC/);
  const unban = await expectCode(507, call(base, 'POST', '/unban', { login: 'alice' }));
  assert.match(unban.message, /unban of @alice not recorded: .*ENOSPC/);
  assert.equal((await call(base, 'GET', '/health')).pid, d.pid);
  answered += 3;
  await delay(1000);
  assertAlive(d, 'after requests on a full disk');
  pass(`daemon alive and all ${answered} requests answered on a full disk; ban/unban refused with 507`);

  // APFS can refuse even the unlink of a failed write's tmp; the daemon's next write
  // reuses that name. tmpfs never refuses.
  const ownTmp = process.platform === 'darwin' ? `.tmp.${d.pid}` : null;
  assert.deepEqual((await leakedTmpFiles()).filter((f) => !ownTmp || !f.endsWith(ownTmp)), [],
    'tmp files left on the full volume');
  await delay(500); // a window opened on the launcher written while full, if it landed
  assertLaunchersWhole(base);
  pass('no tmp file and no half-written repair launcher left behind');
  await settle('drain');

  const live = await caughtUp(base, d.pid);
  assert.deepEqual(ownedOf(live), {
    'ios:A': { status: 'repairing', ownerPid: null },
    'ios:B': { status: 'ready', ownerPid: ME },
    [changed.key]: { status: 'repairing', ownerPid: null },
    [replacement.key]: { status: 'ready', ownerPid: ME },
  });
  pass('state.json, leases.json and daemon.json caught up with no restart');
  assert.deepEqual(readJson(path.join(VOL, 'ban', 'banned.json')).banned.map((b) => b.login), ['alice']);
  await call(base, 'POST', '/report-injection', { person: 'mallory', evidence: 'after drain' });
  assert.deepEqual(readJson(path.join(VOL, 'ban', 'banned.json')).banned.map((b) => b.login), ['alice', 'mallory']);
  pass('the refused ban left banned.json intact, and bans record again once space returns');
  const before = opened().length;
  await call(base, 'POST', '/broken', { ownerPid: ME, deviceId: 'B', reason: 'after drain' });
  assert.equal(await waitOpened(before + 1), before + 1);
  assert.equal(assertLaunchersWhole(base), before + 1);
  pass('repair windows open again once space returns');
  const logText = fs.readFileSync(path.join(base, 'daemon.log'), 'utf8');
  // Space can come and go while the fill races the daemon, so there may be several
  // outages, but each logs one failure and one recovery.
  const outages = (logText.match(/persist (failed|recovered)/g) || []).join(',');
  assert.match(outages, /^(persist failed,persist recovered,?)+$/, `persist failures spammed the log:\n${logText}`);
  pass('one log line per outage, not one per tick');
  await stop(d);
  assert.deepEqual(tmpFiles(), []);
}

// Serving, or exited; null if neither within the deadline.
async function outcome(d, base) {
  for (let i = 0; i < 60; i++) {
    if (d.exitCode !== null || d.signalCode !== null) return 'exited';
    try { await call(base, 'GET', '/health', null, 500); return 'serving'; } catch { await delay(100); }
  }
  return null;
}

async function scenarioStartFull() {
  console.log('scenario 2: a daemon started on an already-full disk');
  const fresh = path.join(VOL, 'b');
  const stale = path.join(VOL, 'c');
  fs.mkdirSync(fresh);
  // A crashed daemon: SIGKILL leaves its socket file and its lease behind.
  const crashed = startDaemon(stale);
  await waitHealth(stale);
  await call(stale, 'POST', '/request', { ownerPid: ME, platform: 'ios', agentName: 'survivor' });
  crashed.kill('SIGKILL');
  await waitExit(crashed);
  assert.ok(fs.existsSync(path.join(stale, 'daemon.sock')));

  await settle('fill');
  const bases = [fresh, stale];
  const started = bases.map(startDaemon);
  const outcomes = [];
  for (const [i, d] of started.entries()) outcomes.push(await outcome(d, bases[i]));
  // A socket needs space on APFS but only an inode on tmpfs.
  const expected = process.platform === 'linux' ? ['serving'] : ['serving', 'exited'];
  for (const [i, d] of started.entries()) {
    assert.ok(expected.includes(outcomes[i]), `${bases[i]}: ${outcomes[i]}, expected ${expected}\n${d.stderrText}`);
    if (outcomes[i] === 'exited') {
      assert.equal(d.exitCode, 1, `${bases[i]}: exit ${d.exitCode}, not a failed start\n${d.stderrText}`);
      assert.equal(d.stderrText, '', `${bases[i]} crashed`);
    }
  }
  if (outcomes[0] === 'serving') await call(fresh, 'POST', '/request', { ownerPid: ME, platform: 'ios', agentName: 'full-start' });
  if (outcomes[1] === 'serving') {
    assert.deepEqual(ownedOf(await call(stale, 'GET', '/state')), { 'ios:B': { status: 'ready', ownerPid: ME } });
  }
  await delay(1000);
  for (const [i, d] of started.entries()) if (outcomes[i] === 'serving') assertAlive(d, 'started on a full disk');
  pass(`started on a full disk: fresh ${outcomes[0]}, over a stale socket ${outcomes[1]}; none crashed`);
  // Only the new daemons' own: the crashed one, killed mid-write, can have left one.
  const ours = (f) => started.some((d) => f.endsWith(`.tmp.${d.pid}`));
  assert.deepEqual((await leakedTmpFiles()).filter(ours), [], 'tmp files left on the full volume');
  pass('no tmp file left behind');

  const serving = bases.filter((_, i) => outcomes[i] === 'serving');
  for (const base of serving) {
    const d = started[bases.indexOf(base)];
    assert.notEqual(discoveredPid(base), d.pid, `${base}: daemon.json landed on a full disk; nothing to prove`);
    assert.match(await mcpCall(base, 'free-device', {}), /You held no device to release/);
    assert.deepEqual(daemonStarts(), [], 'a tool call started a throwaway daemon beside the serving one');
    assert.equal(installer(base, '--check').daemonRunning, true, `${base}: --check missed the serving daemon`);
  }
  if (outcomes[0] === 'serving') {
    installer(fresh, '--uninstall');
    await waitExit(started[0]);
    assert.ok(gone(started[0]), '--uninstall left the serving daemon running');
  }
  if (serving.length) pass(`clients find ${serving.length} daemon(s) serving without daemon.json`);
  await settle('drain');

  const kept = started.filter((d) => !gone(d));
  for (const d of kept) {
    const base = bases[started.indexOf(d)];
    let landed = false;
    for (let t = 0; t < 60 && !landed; t++) { landed = discoveredPid(base) === d.pid; if (!landed) await delay(50); }
    assert.ok(landed, `${base}: daemon.json never named the serving daemon`);
  }
  if (kept.length) pass('daemon.json lands once space returns, with no request to prompt it');

  for (const [i, base] of bases.entries()) {
    if (gone(started[i])) started[i] = startDaemon(base);
    await waitHealth(base);
    await caughtUp(base, started[i].pid);
  }
  assert.ok(!fs.existsSync(path.join(stale, 'daemon.sock.lock')), 'the startup lock was left behind');
  assert.deepEqual(leasesOf(stale)['ios:B'], { status: 'ready', ownerPid: ME }, 'the crashed daemon\'s lease was lost');
  for (const d of started) await stop(d);
  pass('once space returns every daemon serves with its files caught up and the lease kept');
}

async function scenarioDeadWritersTmp() {
  console.log('scenario 3: tmp files of writers that died mid-write');
  const base = path.join(VOL, 'c');
  // As in the incident: daemons that died with their tmp files still on disk.
  const deadPid = spawnSync(process.execPath, ['-e', 'process.stdout.write(String(process.pid))'], { encoding: 'utf8' }).stdout;
  const dead = ['daemon.json', 'state.json', 'leases.json'].map((f) => path.join(base, f))
    .concat(path.join(VOL, 'ban', 'banned.json'))
    .map((f) => `${f}.tmp.${deadPid}`);
  for (const f of dead) fs.writeFileSync(f, '{');
  const live = path.join(base, `state.json.tmp.${ME}`);
  fs.writeFileSync(live, '{');
  const d = startDaemon(base);
  await waitHealth(base);
  assert.deepEqual(dead.filter((f) => fs.existsSync(f)), [], 'a dead writer\'s tmp file survived a daemon start');
  assert.ok(fs.existsSync(live), 'a live writer\'s tmp file was swept');
  await stop(d);
  fs.unlinkSync(live);
  assert.deepEqual(tmpFiles(), []);
  pass('a starting daemon sweeps dead writers\' tmp files and keeps a live writer\'s');
}

const mount = volume('mount');
if (mount.status === 77) {
  console.log(`SKIP full-disk: ${mount.stderr.trim()}`);
  fs.rmSync(ROOT, { recursive: true, force: true });
  process.exit(0);
}
assert.equal(mount.status, 0, `mount failed: ${mount.stderr}`);
try {
  await scenarioRunningDaemon();
  await scenarioStartFull();
  await scenarioDeadWritersTmp();
  console.log(`\nall ${ok} full-disk checks passed`);
} catch (e) {
  for (const d of daemons) if (d.stderrText) console.error(`--- daemon ${d.pid} stderr ---\n${d.stderrText}`);
  throw e;
} finally {
  for (const d of daemons) await stop(d);
  volume('unmount');
  fs.rmSync(ROOT, { recursive: true, force: true });
}
