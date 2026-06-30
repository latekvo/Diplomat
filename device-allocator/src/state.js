// Persistence helpers: atomic JSON writes, the daemon discovery/liveness file,
// the public state snapshot the applet polls, and PID/daemon liveness checks.

import fs from 'node:fs';
import { BASE_DIR, DISCOVERY_PATH, STATE_PATH, SOCKET_PATH } from './paths.js';

export function ensureDirs() { fs.mkdirSync(BASE_DIR, { recursive: true }); }

export function atomicWrite(file, obj, mode = 0o644) {
  ensureDirs();
  const tmp = `${file}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, `${JSON.stringify(obj, null, 2)}\n`, { mode });
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

export function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8')); } catch { return null; }
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
