// This provider loaded by the real allocator daemon, against a fake farm (test/lib/fake-farm.mjs):
// a phone is leased, tunnelled and freed; every way of not getting one falls back to a
// local device and says why; a lease the farm ends or the network drops is handled; a
// lapsed session prompts one sign-in and a user who never answers is left alone for a while.
// Run: node test/provider.test.mjs   (DIPLOMAT_ALLOCATOR_DIR picks the allocator checkout)

import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { startFarm, API_TOKEN } from './lib/fake-farm.mjs';
import { ALLOCATOR } from './lib/allocator.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(ALLOCATOR, 'src', 'daemon.js');
const PROVIDER = path.join(__dirname, '..', 'provider.mjs');
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const PHONES = [
  { id: 'PIXEL', platform: 'android', model: 'Pixel 9a', os: 'Android 16', state: 'ready' },
  { id: 'POCO', platform: 'android', model: 'POCO X3 Pro', os: 'Android 11', state: 'ready' },
  { id: 'IPHONE', platform: 'ios', model: 'iPhone 15', os: 'iOS 18.7.8', state: 'ready' },
];
const LOCAL = [
  { key: 'android:Pixel_6_API_34', platform: 'android', avd: 'Pixel_6_API_34', name: 'Pixel_6_API_34', version: '14', apiVersion: '34', format: 'phone', serial: null, state: 'shutdown' },
  { key: 'ios:A', platform: 'ios', handle: 'A', udid: 'A', name: 'iPhone 16', version: '18.5', apiVersion: '18', format: 'phone', state: 'shutdown' },
];

// One sandbox per scenario: its own base dir, HOME (the agfleet session file), Diplomat
// config, and logging stand-ins for adb and the browser.
function sandbox({ token = API_TOKEN, opener = 'answer' } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'da-fleet-'));
  const home = path.join(dir, 'home');
  if (token) {
    fs.mkdirSync(path.join(home, '.config', 'agfleet'), { recursive: true });
    fs.writeFileSync(path.join(home, '.config', 'agfleet', 'session.json'), JSON.stringify({ farm: 'unused', token }));
  }
  fs.writeFileSync(path.join(dir, 'fake.json'), JSON.stringify(LOCAL));
  fs.writeFileSync(path.join(dir, 'config.json'), '{}');
  const adb = path.join(dir, 'adb');
  fs.writeFileSync(adb, `#!/bin/sh\necho "$*" >> ${JSON.stringify(path.join(dir, 'adb.log'))}\n`);
  // The browser: 'answer' follows the sign-in like a user at the keyboard; 'away' opens nothing.
  const open = path.join(dir, 'open');
  fs.writeFileSync(open, `#!/bin/sh\necho "$1" >> ${JSON.stringify(path.join(dir, 'open.log'))}\n`
    + (opener === 'answer' ? 'curl -sL "$1" > /dev/null\n' : ''));
  fs.chmodSync(adb, 0o755);
  fs.chmodSync(open, 0o755);
  return {
    dir, home,
    read: (f) => { try { return fs.readFileSync(path.join(dir, f), 'utf8'); } catch { return ''; } },
    session: () => JSON.parse(fs.readFileSync(path.join(home, '.config', 'agfleet', 'session.json'), 'utf8')),
  };
}

// Daemons still running when this test ends, however it ends: a failed assertion or a
// runner's SIGTERM would otherwise leave them up for good.
const live = new Set();
process.on('exit', () => { for (const c of live) c.kill('SIGTERM'); });
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => process.exit(1));

function startDaemon(box, farmUrl, extraEnv = {}, provider = PROVIDER) {
  const child = spawn(process.execPath, [DAEMON], {
    stdio: 'ignore',
    env: {
      ...process.env, HOME: box.home, DA_BASE_DIR: box.dir, DA_FAKE_DEVICES: path.join(box.dir, 'fake.json'),
      DA_NO_SPAWN: '1', DA_BAN_DIR: path.join(box.dir, 'ban'), DIPLOMAT_CONFIG: path.join(box.dir, 'config.json'),
      ...(provider ? { DA_REMOTE_PROVIDER: provider } : {}),
      AGFLEET_URL: farmUrl, ADB_PATH: path.join(box.dir, 'adb'), DA_FLEET_OPEN_CMD: path.join(box.dir, 'open'),
      DA_ALLOC_GRACE_MS: '150', DA_REAP_INTERVAL_MS: '250', DA_POOL_INTERVAL_MS: '400',
      DA_IDLE_INTERVAL_MS: '999999', DA_IDLE_LIMIT_MS: '999999',
      DA_FLEET_PING_MS: '200', DA_FLEET_RECONNECT_MS: '2000', DA_FLEET_LOGIN_TIMEOUT_MS: '1500',
      ...extraEnv,
    },
  });
  live.add(child);
  child.once('exit', () => live.delete(child));
  const call = (method, route, body, timeout = 15000) => new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({ socketPath: path.join(box.dir, 'daemon.sock'), path: route, method, timeout,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } },
    (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => {
      const p = b ? JSON.parse(b) : {};
      if (res.statusCode < 300) resolve(p);
      else reject(Object.assign(new Error(p.error || `status ${res.statusCode}`), { statusCode: res.statusCode }));
    }); });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (data) req.write(data);
    req.end();
  });
  const stop = async (signal = 'SIGTERM') => {
    const gone = new Promise((r) => { child.once('exit', r); setTimeout(r, 5000); });
    child.kill(signal);
    await gone;
  };
  return { child, call, stop };
}

async function healthy(d) {
  for (let i = 0; i < 80; i++) { try { await d.call('GET', '/health'); return; } catch { await delay(100); } }
  throw new Error('daemon never became healthy');
}

async function until(what, fn, ms = 5000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await delay(50);
  }
}

// Bytes through the tunnel come back from the fake phone, which echoes them.
function roundTrip(serial, text) {
  const [host, port] = serial.split(':');
  return new Promise((resolve, reject) => {
    const s = net.connect(Number(port), host, () => s.write(text));
    let got = '';
    s.on('data', (b) => { got += b; if (got.length >= text.length) { s.end(); resolve(got); } });
    s.on('error', reject);
    setTimeout(() => { s.destroy(); reject(new Error(`no echo through ${serial}`)); }, 3000);
  });
}

const ME = process.pid;
let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

// 1. Signed in: a fleet phone comes first, is tunnelled, does not count toward the local
//    quota, and free-device returns it.
{
  const farm = await startFarm(PHONES);
  const box = sandbox();
  const d = startDaemon(box, farm.url, { DA_QUOTA: '1' });
  await healthy(d);

  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(r.outcome, 'allocated');
  assert.equal(r.source, 'remote');
  assert.equal(r.key, 'fleet:PIXEL');
  assert.equal(r.version, '16');
  assert.match(r.deviceId, /^127\.0\.0\.1:\d+$/);
  assert.equal(await roundTrip(r.deviceId, 'host:version'), 'host:version');
  assert.match(box.read('adb.log'), new RegExp(`connect ${r.deviceId}\\n-s ${r.deviceId} wait-for-device`));
  pass('android request leases a fleet phone and tunnels adb bytes through the farm');

  const st = await d.call('GET', '/state');
  assert.equal(st.remote.state, 'on');
  const row = st.devices.find((x) => x.key === 'fleet:PIXEL');
  assert.equal(row.source, 'remote');
  assert.equal(row.handle, r.deviceId);
  const local = await d.call('POST', '/request', { ownerPid: ME, platform: 'ios' });
  assert.equal(local.outcome, 'allocated', 'quota 1 is still free: the fleet phone does not take it');
  assert.equal(local.source, 'local');
  assert.equal(local.remoteSkipped, undefined, 'the fleet lends no iOS here, so nothing is reported skipped');
  pass('the phone is in the state snapshot and leaves the local quota alone; iOS stays local and quiet');

  const v = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '11' });
  assert.equal(v.key, 'fleet:POCO', 'the version picks the matching phone');
  pass('a version requirement picks the matching fleet phone');

  await d.call('POST', '/release', { ownerPid: ME, deviceId: r.deviceId });
  await until('the lease to be returned', () => !farm.leaseFor('PIXEL'));
  assert.ok(farm.requests.some((x) => x.startsWith('DELETE /v1/leases/')));
  await until('adb disconnect', () => box.read('adb.log').includes(`disconnect ${r.deviceId}`));
  await assert.rejects(roundTrip(r.deviceId, 'x'), 'the local port is closed once the phone is freed');
  pass('free-device returns the lease, disconnects adb and closes the tunnel port');

  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
  farm.close();
}

// 2. No fleet phone to be had: falls back to a local device and says why.
{
  const farm = await startFarm(PHONES.map((p) => ({ ...p, state: p.id === 'POCO' ? 'offline' : 'leased' })));
  const box = sandbox();
  const d = startDaemon(box, farm.url);
  await healthy(d);
  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(r.source, 'local');
  assert.equal(r.key, 'android:Pixel_6_API_34');
  assert.match(r.remoteSkipped, /no Android phone is free on Argent Fleet/);
  pass('no free fleet phone: a local emulator, with the reason attached');

  const only = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.equal(only.outcome, 'remote-unavailable');
  assert.match(only.reason, /no Android phone is free/);
  const tab = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', format: 'tablet', source: 'remote' });
  assert.equal(tab.outcome, 'remote-unavailable');
  assert.match(tab.reason, /Android phones only/);
  pass("source: 'remote' reports the fleet's answer instead of falling back");

  // Listed ready, but taken before our lease lands: the next phone is tried.
  farm.devices.forEach((p) => { p.state = 'ready'; });
  farm.refuseLease.add('PIXEL');
  const raced = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.equal(raced.key, 'fleet:POCO');
  pass('a phone taken between listing and leasing is skipped for the next one');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
  farm.close();
}

// 3. The farm is unreachable: a quick local fallback.
{
  const box = sandbox();
  const closed = net.createServer();
  await new Promise((r) => closed.listen(0, '127.0.0.1', r));
  const port = closed.address().port;
  closed.close();
  const d = startDaemon(box, `http://127.0.0.1:${port}`);
  await healthy(d);
  const t0 = Date.now();
  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(r.source, 'local');
  assert.match(r.remoteSkipped, /unreachable: ECONNREFUSED/);
  assert.ok(Date.now() - t0 < 5000);
  pass('an unreachable farm falls back to a local device at once');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
}

// 4. Leases ending under their holder, and the watch socket dropping without one.
{
  const farm = await startFarm(PHONES);
  const box = sandbox();
  const d = startDaemon(box, farm.url);
  await healthy(d);
  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });

  farm.dropWatches();
  await delay(800);
  assert.ok(farm.leaseFor('PIXEL'), 'the lease is untouched');
  await until('the watch to reconnect', () => farm.leases.get(farm.leaseFor('PIXEL')).watches.size === 1);
  assert.ok((await d.call('GET', '/state')).devices.some((x) => x.key === 'fleet:PIXEL'));
  assert.equal(await roundTrip(r.deviceId, 'still'), 'still');
  pass('a dropped watch socket reconnects and the phone stays allocated');

  farm.end(farm.leaseFor('PIXEL'), 'expired');
  await until('the allocation to go', async () => !(await d.call('GET', '/state')).devices.some((x) => x.key === 'fleet:PIXEL'));
  const audit = fs.readFileSync(path.join(box.dir, 'ban', 'audit.jsonl'), 'utf8');
  assert.match(audit, /Pixel 9a \(Argent Fleet\) taken from pid:\d+: the lease reached its 4 h limit/);
  pass('a lease the farm ends drops the allocation and says why in the audit log');

  // A farm that stops answering pings is given up once the reconnect budget is spent.
  const r2 = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  assert.equal(r2.key, 'fleet:PIXEL');
  farm.mute = true;
  farm.dropWatches();
  farm.close(); // and the farm goes away entirely
  await until('the lost lease to be dropped',
    async () => !(await d.call('GET', '/state')).devices.some((x) => x.key === 'fleet:PIXEL'), 8000);
  assert.match(fs.readFileSync(path.join(box.dir, 'ban', 'audit.jsonl'), 'utf8'), /lost contact with the farm/);
  pass('a farm that stays unreachable past the reconnect budget loses the allocation');
  await d.stop();
}

// 5. The owner dying returns the phone; a broken report swaps it for another phone;
//    a daemon killed outright reattaches its leases on the same adb serial.
{
  const farm = await startFarm(PHONES);
  const box = sandbox();
  let d = startDaemon(box, farm.url);
  await healthy(d);

  const victim = spawn(process.execPath, ['-e', 'setTimeout(()=>{},60000)']);
  const v = await d.call('POST', '/request', { ownerPid: victim.pid, platform: 'android', version: '16' });
  assert.equal(v.source, 'remote');
  victim.kill('SIGKILL');
  await until('the reaper to return the lease', () => !farm.leaseFor('PIXEL'));
  pass("the reaper returns a dead owner's fleet phone");

  const a = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(a.key, 'fleet:PIXEL');
  const b = await d.call('POST', '/broken', { ownerPid: ME, deviceId: a.deviceId, reason: 'black screen' });
  assert.equal(b.returnedToPool, true);
  assert.equal(b.key, 'fleet:POCO', 'the broken phone is not handed straight back');
  await until('the broken phone to be returned', () => !farm.leaseFor('PIXEL'));
  pass('report-device-broken returns a fleet phone and hands over a different device');
  await d.call('POST', '/release', { ownerPid: ME });

  const keep = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  await d.stop('SIGKILL');
  d = startDaemon(box, farm.url);
  await healthy(d);
  await until('the tunnel to reopen', async () => {
    try { return (await roundTrip(keep.deviceId, 'back')) === 'back'; } catch { return false; }
  });
  assert.ok(farm.leaseFor('PIXEL'));
  pass('a restarted daemon reattaches a live lease on the same adb serial');

  await d.stop(); // a clean stop gives fleet phones back, as it shuts local devices down
  assert.equal(farm.leaseFor('PIXEL'), undefined);
  pass('stopping the daemon returns its leases');
  farm.close();
}

// 6. Signing in: one shared browser prompt for a lapsed session; a prompt nobody answers
//    marks the user away and is not repeated until the panel asks again.
{
  const farm = await startFarm(PHONES);
  let d;
  const lapsed = sandbox({ token: 'b'.repeat(64) });
  d = startDaemon(lapsed, farm.url);
  await healthy(d);
  const [x, y] = await Promise.all([
    d.call('POST', '/request', { ownerPid: ME, platform: 'android' }),
    d.call('POST', '/request', { ownerPid: ME, platform: 'android' }),
  ]);
  assert.deepEqual([x.source, y.source], ['remote', 'remote']);
  assert.equal(lapsed.read('open.log').trim().split('\n').length, 1, 'one prompt for both requests');
  assert.match(lapsed.read('open.log'), new RegExp(`^${farm.url}/auth/login\\?port=\\d+\\n$`));
  assert.deepEqual(lapsed.session(), { farm: farm.url, token: API_TOKEN });
  pass('a rejected token opens one browser sign-in, saves the new token where agfleet keeps it, and leases');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();

  const afk = sandbox({ token: null, opener: 'away' });
  d = startDaemon(afk, farm.url);
  await healthy(d);
  const p = d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  await until('the sign-in to be pending', async () => (await d.call('GET', '/state')).remote.state === 'signing-in');
  const first = await p;
  assert.equal(first.source, 'local');
  assert.match(first.remoteSkipped, /nobody finished the Argent Fleet sign-in within \d+ s; taken as away/);
  const t0 = Date.now();
  const second = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.ok(Date.now() - t0 < 1000, 'no second wait');
  assert.match(second.reason, /last sign-in prompt went unanswered; it is not reopened before/);
  assert.equal(afk.read('open.log').trim().split('\n').length, 1, 'the prompt is not reopened');
  assert.equal((await d.call('GET', '/state')).remote.state, 'sign-in-unanswered');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
  // Away survives a restart.
  d = startDaemon(afk, farm.url);
  await healthy(d);
  const still = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.match(still.reason, /not reopened before/);
  pass('an unanswered sign-in falls back after the timeout, and is not reopened while the user is away');
  // The panel's sign-in prompts anyway; this time the user is there.
  fs.writeFileSync(path.join(afk.dir, 'open'), `#!/bin/sh\necho "$1" >> ${JSON.stringify(path.join(afk.dir, 'open.log'))}\ncurl -sL "$1" > /dev/null\n`);
  assert.deepEqual(await d.call('POST', '/remote/sign-in'), { ok: true });
  assert.equal((await d.call('GET', '/state')).remote.state, 'on');
  const listed = await d.call('POST', '/remote/devices');
  assert.deepEqual(listed.devices.map((x) => [x.key, x.state]), [['fleet:PIXEL', 'ready'], ['fleet:POCO', 'ready'], ['fleet:IPHONE', 'not-lent']]);
  await until('the free listing', async () => (await d.call('GET', '/state')).devices.some((x) => x.source === 'remote'));
  assert.deepEqual((await d.call('GET', '/state')).devices.filter((x) => x.source === 'remote').map((x) => x.key), ['fleet:PIXEL', 'fleet:POCO'],
    'a ready iPhone is never shown free: only Android is leased');
  const now = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.equal(now.source, 'remote');
  pass("the panel's sign-in clears the away mark, and the pool lists (only what it lends as free) and leases again");
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();

  const short = sandbox({ token: null, opener: 'away' });
  d = startDaemon(short, farm.url, { DA_FLEET_AFK_MS: '300' });
  await healthy(d);
  await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  await delay(400);
  await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.equal(short.read('open.log').trim().split('\n').length, 2, 'prompted again once away has run out');
  pass('the away mark expires and the next request prompts again');
  await d.stop();

  const off = sandbox();
  d = startDaemon(off, farm.url, {}, null);
  await healthy(d);
  const before = farm.requests.length;
  const o = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(o.source, 'local');
  assert.equal(o.remoteSkipped, undefined);
  assert.equal(farm.requests.length, before, 'turned off: the farm is never called');
  assert.equal((await d.call('GET', '/state')).remote.state, 'off');
  pass('without the provider configured, a signed-in user never reaches the farm');
  await d.stop();
  farm.close();
}

console.log(`\nfleet: ${ok} checks passed`);
