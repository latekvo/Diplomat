// The idle sweep shuts down a booted device nobody holds once its screen has not
// changed for deviceIdleFreeMinutes, frees a lease after deviceIdleLeasedMinutes (both
// read from Diplomat's config.json on every sweep, 0 = never), and a request waits out
// a device the daemon is shutting down instead of being handed it.
//
// Drives the real idleSweep + handleRequest (DA_EXPOSE_TEST seam, no socket bound)
// against a fake pool; a fake shutdown changes nothing, so the test flips a device to
// 'shutdown' itself to stand for the emulator going away.
// Run: node test/idle-reap-free-devices.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-idle-free-'));
const FAKE = path.join(BASE, 'fake.json');
const CONFIG = path.join(BASE, 'config.json');
const MOTION = path.join(BASE, 'motion');
fs.mkdirSync(MOTION);

const ios = (id, version, state = 'booted') => ({
  key: `ios:${id}`, platform: 'ios', handle: id, udid: id, name: id,
  version, apiVersion: version.split('.')[0], format: 'phone', state,
});
const pool = {
  FREE: ios('FREE', '17.0'), CLAIM: ios('CLAIM', '16.0'),
  MOVING: ios('MOVING', '18.5'), HELD: ios('HELD', '18.5'), RACED: ios('RACED', '15.0', 'shutdown'),
};
const writePool = () => fs.writeFileSync(FAKE, JSON.stringify(Object.values(pool)));
writePool();
const setConfig = (obj) => fs.writeFileSync(CONFIG, JSON.stringify(obj));
const setMotion = (id, h) => fs.writeFileSync(path.join(MOTION, `ios_${id}.txt`), h);

process.env.DA_EXPOSE_TEST = '1';
process.env.DA_BASE_DIR = BASE;
process.env.DA_FAKE_DEVICES = FAKE;
process.env.DA_FAKE_MOTION_DIR = MOTION;
process.env.DA_NO_SPAWN = '1';
process.env.DIPLOMAT_CONFIG = CONFIG;
delete process.env.DA_IDLE_LIMIT_MS;
delete process.env.DA_FREE_IDLE_LIMIT_MS;

const { ensureDirs } = await import('../src/state.js');
ensureDirs();
const { allocations, idleSweep, handleRequest, refreshPool } = await import('../src/daemon.js');

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const shutdowns = () => (fs.existsSync(path.join(BASE, 'daemon.log'))
  ? fs.readFileSync(path.join(BASE, 'daemon.log'), 'utf8') : '')
  .split('\n').filter((l) => l.includes('reap: idle free device'))
  .map((l) => /shutting down (\S+)/.exec(l)[1]);
const LIMIT = { deviceIdleFreeMinutes: 0.001, deviceIdleLeasedMinutes: 0.001 }; // 60 ms

// 1. Limits from config: a static free device is shut down, a moving one is not, and
//    an idle lease is freed (not shut down: the daemon didn't boot it).
setConfig(LIMIT);
setMotion('MOVING', 'm1');
const held = await handleRequest({ ownerPid: process.pid, deviceId: 'HELD', platform: 'ios' });
assert.equal(held.deviceId, 'HELD');
await refreshPool();
await idleSweep(); // first sighting: hashes recorded, nothing reaped
assert.deepEqual(shutdowns(), [], 'a device seen once has no idle time yet');
setMotion('MOVING', 'm2');
await delay(100);
await idleSweep();
assert.deepEqual(shutdowns().sort(), ['ios:CLAIM', 'ios:FREE'],
  'exactly the static free devices are shut down');
assert.ok(!allocations.has('ios:HELD'), 'an idle lease is freed past deviceIdleLeasedMinutes');
console.log('ok  free devices idle past the configured limit are shut down; idle leases freed');

// 2. 0 means never, read fresh on the next sweep.
setConfig({ deviceIdleFreeMinutes: 0, deviceIdleLeasedMinutes: 0 });
await handleRequest({ ownerPid: process.pid, deviceId: 'HELD', platform: 'ios' });
await idleSweep();
await delay(100);
await idleSweep();
assert.deepEqual(shutdowns().sort(), ['ios:CLAIM', 'ios:FREE'], 'limit 0 shuts nothing down');
assert.ok(allocations.has('ios:HELD'), 'limit 0 frees no lease');
allocations.delete('ios:HELD');
console.log('ok  a limit of 0 never reaps');

// 3. A device mid-shutdown still enumerates as booted. A request that only it matches
//    (by selection or by claim) waits for it to go off, then boots it afresh.
const t0 = Date.now();
let selectedAt = 0;
let claimedAt = 0;
const selected = handleRequest({ ownerPid: process.pid, platform: 'ios', version: '17' })
  .then((r) => { selectedAt = Date.now(); return r; });
const claimed = handleRequest({ ownerPid: process.pid, platform: 'ios', deviceId: 'CLAIM' })
  .then((r) => { claimedAt = Date.now(); return r; });
await delay(1500);
assert.equal(selectedAt, 0, 'BUG: a request was handed a device the daemon is shutting down');
assert.equal(claimedAt, 0, 'BUG: a claim was handed a device the daemon is shutting down');
pool.FREE.state = 'shutdown';
writePool();
const s = await selected;
assert.equal(s.deviceId, 'FREE');
assert.equal(allocations.get('ios:FREE').bootedByUs, true, 'booted afresh, so ours to shut down');
assert.equal(claimedAt, 0, 'the claim still waits for its own device');
pool.CLAIM.state = 'shutdown';
writePool();
const c = await claimed;
assert.equal(c.deviceId, 'CLAIM');
assert.ok(selectedAt - t0 >= 1500 && claimedAt >= selectedAt);
console.log('ok  a request waits out a device mid-shutdown, then boots it');

// 4. A free device claimed while its screenshot is being taken is the claimant's: the
//    sweep must not shut it down when it resumes.
allocations.clear();
for (const d of Object.values(pool)) d.state = 'shutdown';
pool.RACED.state = 'booted';
writePool();
await refreshPool();
setConfig(LIMIT);
await idleSweep(); // first sighting of RACED, the only device the sweep looks at
await delay(100);
process.env.DA_FAKE_MOTION_DELAY_MS = '200';
const sweep = idleSweep();
await delay(60); // inside RACED's screenshot
await handleRequest({ ownerPid: process.pid, deviceId: 'RACED', platform: 'ios' });
await sweep;
assert.ok(!shutdowns().includes('ios:RACED'), 'BUG: the sweep shut down a device just claimed');
console.log('ok  a device claimed mid-sweep is not shut down');

try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
process.exit(0);
