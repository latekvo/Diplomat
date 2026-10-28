// The remote device pool through the real daemon, with a test provider
// (test/lib/fake-provider.mjs): a remote device comes first, is attached, stays out of
// the local quota and goes back when freed, reaped, reported broken or the daemon stops;
// every way of not getting one falls back to a local device and says why; a lease the
// pool ends drops the allocation; a restarted daemon reattaches on the same port.
// Run: node test/remote-pool.mjs

import { spawn, spawnSync } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');
const PROVIDER = path.join(__dirname, 'lib', 'fake-provider.mjs');
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const REMOTE = [
  { key: 'pool:P16', platform: 'android', name: 'Pixel (pool)', version: '16', apiVersion: '36', format: 'phone', free: true },
  { key: 'pool:P11', platform: 'android', name: 'POCO (pool)', version: '11', apiVersion: '30', format: 'phone', free: true },
];
const LOCAL = [
  { key: 'android:Pixel_6_API_34', platform: 'android', avd: 'Pixel_6_API_34', name: 'Pixel_6_API_34', version: '14', apiVersion: '34', format: 'phone', serial: null, state: 'shutdown' },
  { key: 'ios:A', platform: 'ios', handle: 'A', udid: 'A', name: 'iPhone 16', version: '18.5', apiVersion: '18', format: 'phone', state: 'shutdown' },
];

function sandbox({ remote = REMOTE, mode = 'ok', config = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'da-remote-'));
  fs.mkdirSync(path.join(dir, 'pool'));
  fs.writeFileSync(path.join(dir, 'pool', 'devices.json'), JSON.stringify(remote));
  fs.writeFileSync(path.join(dir, 'pool', 'mode'), mode);
  fs.writeFileSync(path.join(dir, 'fake.json'), JSON.stringify(LOCAL));
  fs.writeFileSync(path.join(dir, 'config.json'), JSON.stringify(config));
  const pool = (f) => path.join(dir, 'pool', f);
  return {
    dir,
    mode: (m) => fs.writeFileSync(pool('mode'), m),
    events: () => { try { return fs.readFileSync(pool('events.log'), 'utf8').trim().split('\n').filter(Boolean); } catch { return []; } },
    // Renamed into place: the provider polls for this file and must never read it half-written.
    end: (leaseId, why) => { fs.writeFileSync(pool('end.tmp'), why); fs.renameSync(pool('end.tmp'), pool(`end-${leaseId}`)); },
    leases: () => JSON.parse(fs.readFileSync(path.join(dir, 'leases.json'), 'utf8')).leases,
    unconfirmed: () => { try { return JSON.parse(fs.readFileSync(path.join(dir, 'remote-releases.json'), 'utf8')).releases.map((x) => x.lease.key); } catch { return []; } },
  };
}

// Every daemon still running when this test ends, however it ends: a failed assertion
// or a SIGTERM from a runner's timeout would otherwise leave them up for good.
const live = new Set();
process.on('exit', () => { for (const c of live) c.kill('SIGTERM'); });
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => process.exit(1));

function startDaemon(box, extraEnv = {}, provider = PROVIDER) {
  const child = spawn(process.execPath, [DAEMON], {
    stdio: 'ignore',
    env: {
      ...process.env, DA_BASE_DIR: box.dir, DA_FAKE_DEVICES: path.join(box.dir, 'fake.json'),
      DA_NO_SPAWN: '1', DA_BAN_DIR: path.join(box.dir, 'ban'), DIPLOMAT_CONFIG: path.join(box.dir, 'config.json'),
      FAKE_POOL_DIR: path.join(box.dir, 'pool'), ...(provider ? { DA_REMOTE_PROVIDER: provider } : {}),
      DA_ALLOC_GRACE_MS: '150', DA_REAP_INTERVAL_MS: '250', DA_POOL_INTERVAL_MS: '400',
      DA_IDLE_INTERVAL_MS: '999999', DA_IDLE_LIMIT_MS: '999999',
      ...extraEnv,
    },
  });
  live.add(child);
  child.once('exit', () => live.delete(child));
  const call = (method, route, body) => new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({ socketPath: path.join(box.dir, 'daemon.sock'), path: route, method, timeout: 15000,
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
    if (child.exitCode != null) return;
    const gone = new Promise((r) => child.once('exit', () => r(true)));
    child.kill(signal);
    // A stop gives its releases 3 s, and a loaded machine needs headroom past that.
    const late = new Promise((r) => setTimeout(r, 20000, false).unref());
    assert.ok(await Promise.race([gone, late]), 'the daemon did not exit');
  };
  return { call, stop };
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

// Whether the attached device's port answers; `echoes.why` says what it saw when it did not.
function echoes(serial) {
  const [host, port] = serial.split(':');
  return new Promise((resolve) => {
    let got = '';
    const done = (ok, why) => { s.destroy(); echoes.why = why; resolve(ok); };
    const s = net.connect(Number(port), host, () => s.write('ping'));
    s.on('data', (b) => { got += b; if (got.length >= 4) done(got === 'ping', `got ${JSON.stringify(got)}`); });
    s.on('error', (e) => done(false, e.code));
    setTimeout(() => done(false, `no answer in 5 s (got ${JSON.stringify(got)})`), 5000).unref();
  });
}

const stateOf = async (d) => d.call('GET', '/state');
const ME = process.pid;
let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

// 1. A remote device first: attached, out of the local quota, in the snapshot, freed back.
{
  const box = sandbox();
  const d = startDaemon(box, { DA_QUOTA: '1' });
  await healthy(d);
  await until('the provider in the snapshot', async () => (await stateOf(d)).remote.provider === 'Test Pool');
  assert.deepEqual((await stateOf(d)).remote, { provider: 'Test Pool', state: 'on' });

  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(r.outcome, 'allocated');
  assert.equal(r.source, 'remote');
  assert.equal(r.key, 'pool:P16');
  assert.match(r.deviceId, /^127\.0\.0\.1:\d+$/);
  assert.ok(await echoes(r.deviceId), echoes.why);
  assert.deepEqual(box.events(), ['lease pool:P16', 'attach pool:P16']);
  const row = (await stateOf(d)).devices.find((x) => x.key === 'pool:P16');
  assert.deepEqual([row.source, row.status, row.handle, row.idleMs], ['remote', 'ready', r.deviceId, 0]);
  pass('android goes to the remote pool first; the device is attached and in the snapshot');

  const ios = await d.call('POST', '/request', { ownerPid: ME, platform: 'ios' });
  assert.equal(ios.source, 'local', 'quota 1 is still free: the remote device does not take it');
  assert.equal(ios.remoteSkipped, undefined, 'a platform the pool does not lend is not reported skipped');
  const v = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '11' });
  assert.equal(v.key, 'pool:P11');
  pass('remote devices leave the local quota alone; requirements pick among them; other platforms stay quiet');

  await d.call('POST', '/release', { ownerPid: ME, deviceId: r.deviceId });
  assert.ok(box.events().includes('release pool:P16'));
  assert.equal(await echoes(r.deviceId), false);
  pass('free-device gives the lease back');

  const ch = await d.call('POST', '/change', { ownerPid: ME, deviceId: v.deviceId, version: '16' });
  assert.equal(ch.key, 'pool:P16');
  await until('the old lease to go back', () => box.events().filter((e) => e === 'release pool:P11').length === 1);
  pass('change-device keeps the source and returns the old lease');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
}

// 2. Every way of not getting one falls back to a local device, with the reason.
{
  const box = sandbox({ remote: REMOTE.map((x) => ({ ...x, free: false })) });
  // The slow provider outlasts the budget by far more than any load stretches the rest.
  const d = startDaemon(box, { DA_REMOTE_ACQUIRE_BUDGET_MS: '500', FAKE_POOL_SLOW_MS: '4000' });
  await healthy(d);
  const none = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(none.source, 'local');
  assert.equal(none.key, 'android:Pixel_6_API_34');
  assert.equal(none.remoteSkipped, 'nothing free in the test pool');
  const pinned = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', source: 'remote' });
  assert.deepEqual([pinned.outcome, pinned.reason], ['remote-unavailable', 'nothing free in the test pool']);
  pass("an empty pool falls back with its reason; source: 'remote' reports it instead");
  await d.call('POST', '/release', { ownerPid: ME });

  box.mode('throw');
  const threw = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(threw.remoteSkipped, 'Test Pool failed: boom');
  await d.call('POST', '/release', { ownerPid: ME });
  pass('a provider that throws is a fallback, not an error');

  fs.writeFileSync(path.join(box.dir, 'pool', 'devices.json'), JSON.stringify(REMOTE));
  box.mode('slow');
  const t0 = Date.now();
  const slow = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  const waited = Date.now() - t0;
  assert.ok(waited < 3000, `the agent waits the budget, not the provider (waited ${waited} ms)`);
  assert.equal(slow.source, 'local');
  assert.equal(slow.remoteSkipped, 'Test Pool did not answer within 1 s');
  await until('the late lease to go back', () => box.events().join() === 'lease pool:P16,release pool:P16', 20000);
  pass('a provider past the budget is a fallback, and the lease it lands late is handed back');
  await d.call('POST', '/release', { ownerPid: ME });

  box.mode('attach-fails');
  const att = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(att.source, 'local');
  assert.equal(att.remoteSkipped, 'Pixel (pool) was leased but could not be attached: tunnel refused');
  assert.deepEqual(box.events().slice(-2), ['lease pool:P16', 'release pool:P16']);
  assert.equal((await stateOf(d)).devices.some((x) => x.key === 'pool:P16'), false);
  pass('a lease that cannot be attached is returned and the request falls back');
  await d.call('POST', '/release', { ownerPid: ME });

  box.mode('slow-attach');
  const inFlight = d.call('POST', '/request', { ownerPid: ME, platform: 'android' }).catch((e) => e);
  await until('the attach to start', async () => (await stateOf(d)).devices.some((x) => x.key === 'pool:P16' && x.status === 'booting'));
  await d.call('POST', '/kill', { key: 'pool:P16' });
  const killed = await inFlight;
  assert.equal(killed.statusCode, 409, 'an agent is never told it holds a device that was freed under it');
  assert.deepEqual(box.events().slice(-3), ['release pool:P16', 'attach pool:P16', 'release pool:P16']);
  pass('a device killed while attaching is reported as released, and what the attach opened is closed');

  box.mode('slow-attach-strict');
  const strict = d.call('POST', '/request', { ownerPid: ME, platform: 'android' }).catch((e) => e);
  await until('the attach to start', async () => (await stateOf(d)).devices.some((x) => x.key === 'pool:P16' && x.status === 'booting'));
  await d.call('POST', '/kill', { key: 'pool:P16' });
  assert.equal((await strict).statusCode, 409, 'not a fallback to a local device');
  pass('a provider that refuses to attach a released lease still gets the agent a 409');
  await d.stop();

  const short = startDaemon(box, { DA_REMOTE_ATTACH_BUDGET_MS: '500', FAKE_POOL_SLOW_MS: '1500' });
  await healthy(short);
  box.mode('slow-attach');
  const n = box.events().length;
  const stuck = await short.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(stuck.source, 'local');
  assert.equal(stuck.remoteSkipped, 'Pixel (pool) was leased but could not be attached: not attached within 1 s');
  await until('the late attach to be released', () => box.events().slice(n).join() === 'lease pool:P16,release pool:P16,attach pool:P16,release pool:P16');
  pass('an attach past its budget is a fallback, and what it opens late is released');
  await short.stop();
}

// 2b. Leases ending while the daemon has their record in hand.
{
  const box = sandbox();
  // The slow acquire outlasts its budget, so the pool's end lands inside it.
  const d = startDaemon(box, { DA_REMOTE_ACQUIRE_BUDGET_MS: '2000', FAKE_POOL_SLOW_MS: '3000' });
  await healthy(d);
  const auditOf = () => { try { return fs.readFileSync(path.join(box.dir, 'ban', 'audit.jsonl'), 'utf8'); } catch { return ''; } };

  box.mode('slow-attach-strict');
  const attaching = d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  await until('the attach to start', async () => (await stateOf(d)).devices.some((x) => x.key === 'pool:P16' && x.status === 'booting'));
  box.end(box.leases().find((l) => l.key === 'pool:P16').remote.id, 'the host went away');
  const fell = await attaching;
  assert.equal(fell.source, 'local');
  assert.equal(fell.remoteSkipped, 'Pixel (pool) was leased, but its pool ended the lease while attaching: the host went away');
  pass('a lease the pool ends while attaching is a fallback with that reason, not a 409');
  await d.call('POST', '/release', { ownerPid: ME });

  // The same, from a provider whose attach still finishes.
  box.mode('slow-attach');
  const finishing = d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  await until('the attach to start', async () => (await stateOf(d)).devices.some((x) => x.key === 'pool:P16' && x.status === 'booting'));
  box.end(box.leases().find((l) => l.key === 'pool:P16').remote.id, 'the host went away');
  assert.equal((await finishing).remoteSkipped, 'Pixel (pool) was leased, but its pool ended the lease while attaching: the host went away');
  pass('so is one whose attach finishes after the pool ended it');
  await d.call('POST', '/release', { ownerPid: ME });

  box.mode('ok');
  const held = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '11' });
  assert.equal(held.key, 'pool:P11');
  box.mode('slow');
  const changing = d.call('POST', '/change', { ownerPid: ME, deviceId: held.deviceId, source: 'remote' });
  const p11 = box.leases().find((l) => l.key === 'pool:P11').remote.id;
  await until('the change to take the old device out of the table', async () => !(await stateOf(d)).devices.some((x) => x.key === 'pool:P11'));
  box.end(p11, 'lease expired');
  const changed = await changing;
  assert.deepEqual([changed.outcome, changed.reason], ['remote-unavailable', 'Test Pool did not answer within 2 s']);
  assert.equal(changed.keptDevice, undefined, 'an ended lease is not kept');
  assert.equal((await stateOf(d)).devices.some((x) => x.key === 'pool:P11'), false);
  assert.match(auditOf(), /"remote-lease-ended","detail":"POCO \(pool\) taken from pid:\d+: lease expired"/);
  pass('change-device does not restore a lease the pool ended while it looked for a replacement');
  await d.stop();
}

// 2c. A release the pool does not confirm is retried until it does.
{
  const box = sandbox();
  const d = startDaemon(box, { DA_REMOTE_RELEASE_RETRY_MS: '300' });
  await healthy(d);
  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  assert.equal(r.key, 'pool:P16');
  box.mode('release-fails');
  await d.call('POST', '/release', { ownerPid: ME });
  await until('the failed release to be recorded', () => box.unconfirmed().join() === 'pool:P16');
  assert.equal((await stateOf(d)).devices.some((x) => x.key === 'pool:P16'), false);
  await until('a retry', () => box.events().filter((e) => e === 'release-failed pool:P16').length >= 2);
  box.mode('ok');
  await until('the retry to land', () => box.events().at(-1) === 'release pool:P16');
  await until('the record to clear', () => box.unconfirmed().length === 0);
  pass('a release the pool does not confirm is recorded and retried until it lands');
  await d.stop();
}

// 3. No provider, or one that will not load.
{
  const box = sandbox({ config: { deviceRemoteProvider: PROVIDER } });
  let d = startDaemon(box, {}, null);
  await healthy(d);
  const r = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(r.source, 'local');
  assert.equal(r.remoteSkipped, undefined);
  assert.deepEqual(box.events(), [], "a fake pool ignores the configured provider: tests never lease the user's real devices");
  assert.deepEqual((await stateOf(d)).remote, { provider: null, state: 'off' });
  pass('without a provider the allocator is purely local and says nothing about it');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();

  d = startDaemon(box, {}, path.join(box.dir, 'missing.mjs'));
  await healthy(d);
  const bad = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(bad.source, 'local');
  assert.match(bad.remoteSkipped, /^the remote pool provider \(.*missing\.mjs\) failed to load: /);
  assert.equal((await stateOf(d)).remote.state, 'error');
  pass('a provider that fails to load is reported and falls back');
  await d.call('POST', '/release', { ownerPid: ME });
  await d.stop();
}

// 3b. The setting changing under a running allocator: the snapshot follows it, and a
//     lease is still given back through the provider that granted it. Run outside the
//     daemon, since a fake device pool ignores the setting.
{
  const box = sandbox({ config: { deviceRemoteProvider: PROVIDER } });
  const script = `
    import fs from 'node:fs';
    import * as remote from ${JSON.stringify(path.join(__dirname, '..', 'src', 'remote.js'))};
    const out = [];
    const settle = () => new Promise((r) => setTimeout(r, 200));
    out.push(remote.status()); await settle(); out.push(remote.status());
    const got = await remote.acquire({ platform: 'android', version: 'any', format: 'any' }, new Set());
    fs.writeFileSync(process.env.DIPLOMAT_CONFIG, JSON.stringify({ deviceRemoteProvider: process.env.MISSING }));
    out.push(remote.status()); await settle(); out.push(remote.status().state);
    await remote.release(got.provider, got.lease);
    fs.writeFileSync(process.env.DIPLOMAT_CONFIG, '{}');
    out.push(remote.status());
    console.log(JSON.stringify(out));`;
  const env = { ...process.env, DA_BASE_DIR: box.dir, DIPLOMAT_CONFIG: path.join(box.dir, 'config.json'),
    FAKE_POOL_DIR: path.join(box.dir, 'pool'), MISSING: path.join(box.dir, 'missing.mjs') };
  delete env.DA_FAKE_DEVICES;
  delete env.DA_REMOTE_PROVIDER;
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8', env });
  assert.equal(r.status, 0, r.stderr);
  assert.deepEqual(JSON.parse(r.stdout), [
    { provider: null, state: 'loading' }, { provider: 'Test Pool', state: 'on' },
    { provider: null, state: 'loading' }, 'error',
    { provider: null, state: 'off' },
  ]);
  assert.deepEqual(box.events(), ['lease pool:P16', 'release pool:P16']);
  pass('the snapshot follows a changed setting; a lease goes back through the provider that granted it');
}

// 4. Leases ending: by the pool, by the owner dying, by a broken report, by a stop;
//    and a daemon killed outright reattaching on the same port.
{
  const box = sandbox();
  let d = startDaemon(box);
  await healthy(d);

  const a = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  const leaseId = box.leases().find((l) => l.key === 'pool:P16').remote.id;
  box.end(leaseId, 'the device dropped off its host');
  await until('the allocation to go', async () => !(await stateOf(d)).devices.some((x) => x.key === 'pool:P16'));
  const audit = fs.readFileSync(path.join(box.dir, 'ban', 'audit.jsonl'), 'utf8');
  assert.match(audit, /"remote-lease-ended","detail":"Pixel \(pool\) taken from pid:\d+: the device dropped off its host"/);
  assert.equal(box.events().at(-1), 'release pool:P16', 'released too, in case only contact was lost');
  assert.equal(await echoes(a.deviceId), false);
  pass('a lease the pool ends drops the allocation, with the reason in the audit log');

  const victim = spawn(process.execPath, ['-e', 'setTimeout(()=>{},60000)']);
  await d.call('POST', '/request', { ownerPid: victim.pid, platform: 'android', version: '16' });
  const n = box.events().length;
  victim.kill('SIGKILL');
  await until('the reaper to return the lease', () => box.events().length > n && box.events().at(-1) === 'release pool:P16');
  pass("the reaper returns a dead owner's remote device");

  const b = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(b.key, 'pool:P16');
  const swapped = await d.call('POST', '/broken', { ownerPid: ME, deviceId: b.deviceId, reason: 'black screen' });
  assert.equal(swapped.returnedToPool, true);
  assert.equal(swapped.key, 'pool:P11', 'the broken device is not handed straight back');
  assert.ok(box.events().includes('release pool:P16'));
  assert.equal((await stateOf(d)).devices.find((x) => x.key === 'pool:P16'), undefined, 'not quarantined: its pool resets it');
  pass('report-device-broken returns a remote device to its pool and hands over another');
  await d.call('POST', '/release', { ownerPid: ME });

  const keep = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  const keep2 = await d.call('POST', '/request', { ownerPid: ME, platform: 'android' });
  assert.equal(keep2.key, 'pool:P11');
  await d.stop('SIGKILL');
  d = startDaemon(box);
  await healthy(d);
  await until('both devices to be reattached', async () => await echoes(keep.deviceId) && await echoes(keep2.deviceId));
  assert.deepEqual(box.events().slice(-2).sort(),
    [`attach pool:P11 port ${keep2.deviceId.split(':')[1]}`, `attach pool:P16 port ${keep.deviceId.split(':')[1]}`]);
  pass('a restarted daemon reattaches every live lease on the same port');

  await d.stop();
  assert.deepEqual(box.events().slice(-2).sort(), ['release pool:P11', 'release pool:P16']);
  const stopped = box.events().length;
  d = startDaemon(box);
  await healthy(d);
  assert.deepEqual((await stateOf(d)).devices.filter((x) => x.source === 'remote'), []);
  assert.deepEqual(box.leases(), []);
  assert.deepEqual(box.events().slice(stopped), [], 'nothing reattached');
  pass('stopping the daemon returns its leases, and the next one does not restore them');

  // A release slower than the stop allows is finished by a later daemon, however many
  // stop before it lands.
  await d.stop();
  d = startDaemon(box, { FAKE_POOL_SLOW_MS: '5000' });
  await healthy(d);
  const slowRel = await d.call('POST', '/request', { ownerPid: ME, platform: 'android', version: '16' });
  assert.equal(slowRel.key, 'pool:P16');
  box.mode('slow-release');
  const before = box.events().length;
  await d.stop();
  assert.equal(box.leases().some((l) => l.key === 'pool:P16'), false, 'not restored as held');
  assert.deepEqual(box.unconfirmed(), ['pool:P16']);
  d = startDaemon(box, { FAKE_POOL_SLOW_MS: '5000' });
  await healthy(d);
  await delay(1000);
  await d.stop();
  assert.deepEqual(box.unconfirmed(), ['pool:P16'], 'a second stop mid-retry keeps it on record');
  box.mode('ok');
  d = startDaemon(box);
  await healthy(d);
  await until('a later daemon to finish the release', () => box.events().slice(before).join() === 'release pool:P16');
  await until('the record to clear', () => box.unconfirmed().length === 0);
  assert.equal((await stateOf(d)).devices.some((x) => x.key === 'pool:P16'), false);
  pass('a lease whose release outlasts the stop is given back by a later daemon');

  // An owner that died while no daemon was running: the lease goes back at startup.
  const gone = spawn(process.execPath, ['-e', 'setTimeout(()=>{},60000)']);
  await d.call('POST', '/request', { ownerPid: gone.pid, platform: 'android', version: '16' });
  await d.stop('SIGKILL');
  gone.kill('SIGKILL');
  await delay(200);
  d = startDaemon(box);
  await healthy(d);
  await until('the orphaned lease to go back', () => box.events().at(-1) === 'release pool:P16');
  pass("a restarted daemon returns the leases of owners that died while it was down");
  await d.stop();
}

// 5. The panel's routes.
{
  const box = sandbox();
  const d = startDaemon(box);
  await healthy(d);
  const listed = await d.call('POST', '/remote/devices');
  assert.deepEqual(listed.devices.map((x) => x.key), ['pool:P16', 'pool:P11']);
  assert.deepEqual(await d.call('POST', '/remote/sign-in'), { ok: false, reason: 'Test Pool has no sign-in' });
  await d.stop();
  const none = startDaemon(box, {}, null);
  await healthy(none);
  assert.deepEqual(await none.call('POST', '/remote/devices'), { reason: 'no remote device pool is configured' });
  pass('/remote/devices lists the pool; /remote/sign-in says when there is nothing to sign in to');
  await none.stop();
}

console.log(`\nremote-pool: ${ok} checks passed`);
