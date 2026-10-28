// Persistence helpers: atomic JSON writes, the daemon discovery file,
// the public state snapshot the applet polls, and the PID liveness check.

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { BASE_DIR, DISCOVERY_PATH, STATE_PATH, LEASES_PATH, SOCKET_PATH, CONFIG_PATH } from './paths.js';

export function ensureDirs() { fs.mkdirSync(BASE_DIR, { recursive: true }); }

export function atomicWrite(file, obj, mode = 0o644) {
  fs.mkdirSync(path.dirname(file), { recursive: true }); // handles files outside BASE_DIR
  const tmp = `${file}.tmp.${process.pid}`;
  // fsync before rename: without it a crash/power loss can leave a zero-length
  // file after the rename commits — fatal for banned.json (a torn read there
  // resets the ban list).
  try {
    const fd = fs.openSync(tmp, 'w', mode);
    try {
      fs.writeFileSync(fd, `${JSON.stringify(obj, null, 2)}\n`);
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    fs.renameSync(tmp, file);
  } catch (e) {
    // A full disk fails the write or the rename. Should even this unlink fail (APFS
    // can), this process's next write of the file reuses the name.
    try { fs.unlinkSync(tmp); } catch {}
    throw e;
  }
}

// A writer that died between creating its tmp file and renaming it (killed, or on a
// full disk that refused even the unlink) leaves the tmp for good: no later process
// writes under its pid again.
export function sweepDeadTmp(file) {
  const dir = path.dirname(file);
  const prefix = `${path.basename(file)}.tmp.`;
  let names;
  try { names = fs.readdirSync(dir); } catch { return; }
  for (const n of names) {
    const pid = Number(n.slice(prefix.length));
    if (n.startsWith(prefix) && Number.isInteger(pid) && !pidAlive(pid)) {
      try { fs.unlinkSync(path.join(dir, n)); } catch {}
    }
  }
}

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

export function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; }
  catch (e) { return e.code === 'EPERM'; } // alive but not ours-to-signal
}

// One incarnation of a pid, read alike from any process and unmoved by a wall-clock step.
// Linux: /proc starttime (ticks since boot, so unique within one boot only; Linux ps
// derives lstart from the clock). Elsewhere: ps lstart in a fixed locale and zone. ''
// when unreadable.
export function procIdentity(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    // comm (field 2) may hold spaces and ')': after the last ')', field 22 is index 19.
    const tail = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/);
    if (tail[19]) return tail[19];
  } catch {}
  try {
    return execFileSync('ps', ['-o', 'lstart=', '-p', String(pid)],
      { timeout: 2000, env: { ...process.env, LC_ALL: 'C', TZ: 'UTC' } }).toString().trim();
  } catch {}
  return '';
}
