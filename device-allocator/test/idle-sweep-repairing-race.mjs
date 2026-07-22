// Regression: idleSweep must NOT reap a device that /broken quarantined into
// 'repairing' during idleSweep's `await dev.motionHash()` yield.
//
// idleSweep checks `status === 'ready'` BEFORE the screenshot await, but after the
// await only re-checks IDENTITY (same object still in the map) — not status. So if
// handleBroken flips the same object to 'repairing' (owner cleared, kept in the map
// as quarantine) during the await, idleSweep resumes and still deletes it + shuts it
// down: the repair agent's later /repaired 404s, and selectDevice can re-hand the
// device mid-repair (the double-use the quarantine exists to prevent).
//
// Deterministic: DA_FAKE_MOTION_DELAY_MS makes the fake motionHash yield the event
// loop for a fixed window, and we inject the /broken flip inside that window. Drives
// the REAL idleSweep + handleBroken (DA_EXPOSE_TEST seam, no socket bound).
// Run: node test/idle-sweep-repairing-race.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-idle-race-'));
const FAKE = path.join(BASE, 'fake.json');
fs.writeFileSync(FAKE, JSON.stringify([
  { key: 'ios:REPL', platform: 'ios', handle: 'REPL', udid: 'REPL', name: 'iPhone spare',
    version: '18.5', apiVersion: '18', format: 'phone', state: 'shutdown' },
]));

process.env.DA_EXPOSE_TEST = '1';       // import without binding the socket
process.env.DA_BASE_DIR = BASE;
process.env.DA_FAKE_DEVICES = FAKE;
process.env.DA_NO_SPAWN = '1';          // repair agent spawn suppressed
process.env.DA_FAKE_MOTION_DELAY_MS = '200';  // the yield window we inject into
process.env.DA_IDLE_LIMIT_MS = '10';    // any idle > 10ms is reap-eligible

const { ensureDirs } = await import('../src/state.js');
ensureDirs();
const { allocations, idleSweep, handleBroken } = await import('../src/daemon.js');

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// A device allocated to us, frozen (stable hash) and idle past the limit — exactly
// what idleSweep would reap. bootedByUs:false so the assertion is purely about the
// allocation record (no simctl shutdown noise).
const D = {
  key: 'ios:FROZEN', handle: 'FROZEN', udid: 'FROZEN', platform: 'ios',
  status: 'ready', ownerPid: process.pid, agentName: 'agentA',
  requirements: { platform: 'ios' }, bootedByUs: false,
  motionHash: 'frozen', lastMotionAt: Date.now() - 60_000,
};
allocations.set(D.key, D);

// Start the sweep: it passes the pre-await status check, then enters
// `await dev.motionHash(D)` (200ms fake yield).
const sweep = idleSweep();

// Mid-await: agent A reports D broken. handleBroken flips the SAME object to
// 'repairing', clears the owner, keeps it in the map (quarantine), dispatches repair.
await delay(60);                        // 60ms < 200ms: sweep is inside the await
assert.equal(D.status, 'ready', 'precondition: still ready before /broken');
Promise.resolve(handleBroken({ ownerPid: process.pid, deviceId: 'FROZEN' })).catch(() => {});
assert.equal(D.status, 'repairing', 'handleBroken flipped D to repairing');

await sweep;                            // sweep resumes after the yield

// The fix: idleSweep re-checks status after the await, sees 'repairing', and leaves
// the quarantine record alone. The bug: it deletes D and (if bootedByUs) shuts it down.
assert.ok(allocations.has(D.key),
  'BUG: idleSweep deleted a device that /broken quarantined into repairing');
assert.equal(allocations.get(D.key).status, 'repairing',
  'the quarantine record must survive the idle sweep intact');

try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
console.log('ok  idle-sweep does not reap a device quarantined into repairing mid-await');
process.exit(0);
