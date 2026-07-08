// Persistence helpers: atomic JSON writes, the daemon discovery/liveness file,
// the public state snapshot the applet polls, and PID/daemon liveness checks.

import fs from 'node:fs';
import path from 'node:path';
import { BASE_DIR, DISCOVERY_PATH, STATE_PATH, LEASES_PATH, SOCKET_PATH } from './paths.js';

export function ensureDirs() { fs.mkdirSync(BASE_DIR, { recursive: true }); }

export function atomicWrite(file, obj, mode = 0o644) {
  fs.mkdirSync(path.dirname(file), { recursive: true }); // handles files outside BASE_DIR
  const tmp = `${file}.tmp.${process.pid}`;
  // fsync before rename: without it a crash/power loss can leave a zero-length
  // file after the rename commits — fatal for banned.json (a torn read there
  // resets the ban list).
  const fd = fs.openSync(tmp, 'w', mode);
  try {
    fs.writeFileSync(fd, `${JSON.stringify(obj, null, 2)}\n`);
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  fs.renameSync(tmp, file);
}

export function writeDiscovery() {
  atomicWrite(DISCOVERY_PATH, {
    pid: process.pid,
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

export function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; }
  catch (e) { return e.code === 'EPERM'; } // alive but not ours-to-signal
}

export function daemonAlive() {
  const d = readDiscovery();
  if (!d || !pidAlive(d.pid)) return false;
  return fs.existsSync(SOCKET_PATH);
}
