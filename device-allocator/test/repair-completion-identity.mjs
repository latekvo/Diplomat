// Regression: automatedRepair's completion must not free a device by key without an
// identity re-check. eraseIOS/wipeAndroid await a multi-second subprocess; during it a
// /kill or /repaired can remove THIS repair record and a /request re-hand the key to a
// new owner. Deleting by key on completion would then drop the new owner's LIVE lease
// (the same physical device allocated to two agents — the exclusivity violation the
// allocator exists to prevent).
//
// Deterministic: DA_FAKE_RESET_DELAY_MS makes the fake eraseIOS yield; we replace the
// record inside that window and assert the new owner's lease survives. Drives the REAL
// dispatchRepair/automatedRepair (DA_EXPOSE_TEST seam, no socket bound).
// Run: node test/repair-completion-identity.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-repair-id-'));
const FAKE = path.join(BASE, 'fake.json');
fs.writeFileSync(FAKE, JSON.stringify([]));

process.env.DA_EXPOSE_TEST = '1';       // import without binding the socket
process.env.DA_AUTO_REPAIR = '1';       // exercise the automated (daemon-driven) repair
process.env.DA_BASE_DIR = BASE;
process.env.DA_FAKE_DEVICES = FAKE;
process.env.DA_NO_SPAWN = '1';
process.env.DA_FAKE_RESET_DELAY_MS = '200';  // the erase-await window we interleave into

const { ensureDirs } = await import('../src/state.js');
ensureDirs();
const { allocations, dispatchRepair } = await import('../src/daemon.js');

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// A device X quarantined into 'repairing' (what handleBroken produces).
const R_A = {
  key: 'ios:X', platform: 'ios', udid: 'X', handle: 'X', name: 'iPhone X',
  status: 'repairing', ownerPid: null, agentName: 'repair', bootedByUs: true,
  requirements: { platform: 'ios' },
};
allocations.set('ios:X', R_A);

dispatchRepair(R_A);                      // -> automatedRepair -> eraseIOS (200ms yield)
await delay(60);                          // mid-erase

// Concurrent supported ops: /kill removes the repairing record, then /request re-hands
// key ios:X to agent B — a NEW, live allocation object at the same key.
allocations.delete('ios:X');
const R_B = {
  key: 'ios:X', platform: 'ios', udid: 'X', handle: 'X', name: 'iPhone X',
  status: 'ready', ownerPid: 4242, agentName: 'agentB', bootedByUs: true,
  requirements: { platform: 'ios' },
};
allocations.set('ios:X', R_B);

await delay(300);                         // let eraseIOS resolve and the .then run

// The fix: the completion re-checks identity, sees ios:X now holds R_B (not R_A), and
// leaves it alone. The bug: it deletes ios:X unconditionally, dropping B's live lease.
assert.ok(allocations.get('ios:X') === R_B,
  'BUG: repair completion freed the key while a new owner held a live lease (double-alloc)');
assert.equal(allocations.get('ios:X').ownerPid, 4242, 'B still owns the device');

try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
console.log('ok  repair completion leaves a re-handed device alone (identity re-check)');
process.exit(0);
