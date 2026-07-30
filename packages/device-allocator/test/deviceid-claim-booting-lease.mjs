// Regression: the /request deviceId claim path must not overwrite a BOOTING lease.
// During the Android boot window a lease reserved for a not-yet-running AVD keeps
// serial=null (bootAlloc stamps it only after bootAndroid resolves, up to 180s, OUTSIDE
// the lock), while the live pool surfaces the emulator's serial much earlier. So
// allocatedByHandle(serial) misses the lease and findPoolDevice(serial) resolves it to
// the lease's key; without a `!allocations.has(key)` backstop (which the general
// selectDevice path has) reserve() OVERWRITES the committed lease — handing one agent's
// exclusive, actually-booted device to another (exclusivity violation + a spurious 409 to
// the victim after its full boot wait).
//
// Drives the REAL handleRequest (DA_EXPOSE_TEST seam, no socket bound).
// Run: node test/deviceid-claim-booting-lease.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-lease-claim-'));
const FAKE = path.join(BASE, 'fake.json');
// Boot-window pool state: Pixel_7 already answers adb as 'emulator-5554' (booted),
// even though the lease below still has serial=null.
fs.writeFileSync(FAKE, JSON.stringify([
  { key: 'android:Pixel_7', platform: 'android', avd: 'Pixel_7', name: 'Pixel_7',
    handle: 'emulator-5554', serial: 'emulator-5554', version: null, apiVersion: null,
    format: 'phone', state: 'booted' },
]));

process.env.DA_EXPOSE_TEST = '1';       // import without binding the socket
process.env.DA_BASE_DIR = BASE;
process.env.DA_FAKE_DEVICES = FAKE;
process.env.DA_NO_SPAWN = '1';

const { ensureDirs } = await import('../src/state.js');
ensureDirs();
const { allocations, handleRequest } = await import('../src/daemon.js');

// Agent1's committed, quota-counted lease, mid-boot with serial not yet stamped.
// ownerPid = this live process so reapDeadOwners() keeps it (a dead owner would be reaped).
const alloc1 = {
  key: 'android:Pixel_7', platform: 'android', avd: 'Pixel_7', name: 'Pixel_7',
  handle: null, udid: undefined, serial: null, ownerPid: process.pid,
  agentName: 'agent1', status: 'booting', bootedByUs: true,
  allocatedAt: 0, requirements: { platform: 'android', version: 'any' },
};
allocations.set('android:Pixel_7', alloc1);

// Agent2 claims the victim's in-flight serial (obtainable via `adb devices`).
let statusCode = 0;
try {
  await handleRequest({ ownerPid: 999999, agentName: 'agent2', platform: 'android',
    deviceId: 'emulator-5554' });
} catch (e) { statusCode = e && e.statusCode; }

assert.equal(statusCode, 409, 'BUG: claim on an already-held key was not refused (409)');
assert.ok(allocations.get('android:Pixel_7') === alloc1,
  'BUG: booting lease was overwritten (exclusivity violation)');
assert.equal(allocations.get('android:Pixel_7').ownerPid, process.pid,
  'Agent1 still owns the device it booted');

try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
console.log('ok  deviceId claim on an already-held key is refused, booting lease intact');
process.exit(0);
