// Persistence helpers: the daemon discovery file, the public state snapshot the applet
// polls and the lease table, over the shared atomic writes and PID checks.

import fs from 'node:fs';
import { atomicWrite, procIdentity } from '../../daemon-core/src/fsio.js';
import { BASE_DIR, DISCOVERY_PATH, STATE_PATH, LEASES_PATH, SOCKET_PATH, CONFIG_PATH } from './paths.js';

export { atomicWrite, writeAtomic, sweepDeadTmp, pidAlive, procIdentity } from '../../daemon-core/src/fsio.js';

export function ensureDirs() { fs.mkdirSync(BASE_DIR, { recursive: true }); }

export function writeDiscovery() {
  atomicWrite(DISCOVERY_PATH, {
    pid: process.pid,
    identity: procIdentity(process.pid),
    startedAt: new Date().toISOString(),
    socket: SOCKET_PATH,
    version: 1,
  }, 0o600);
}

export function readDiscovery() {
  try { return JSON.parse(fs.readFileSync(DISCOVERY_PATH, 'utf8')); } catch { return null; }
}

// The applet reads this; world-readable, secrets-free.
export function writeState(snapshot) { atomicWrite(STATE_PATH, snapshot, 0o644); }

// Full allocation records (owner pids, boot ownership, requirements) so a restarted
// daemon can rehydrate its lease table instead of silently dropping every lease —
// clients auto-respawn a dead daemon, and an empty table would re-hand a device
// that another agent is still driving. Private (0600): not for the applet.
export function writeLeases(allocs) { atomicWrite(LEASES_PATH, { version: 1, leases: allocs }, 0o600); }

export function readLeases() {
  try {
    const d = JSON.parse(fs.readFileSync(LEASES_PATH, 'utf8'));
    return Array.isArray(d.leases) ? d.leases : [];
  } catch { return []; }
}

// Diplomat's shared settings, or {} when the file is absent, corrupt or not an object.
export function readConfig() {
  try {
    const v = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  } catch { return {}; }
}
