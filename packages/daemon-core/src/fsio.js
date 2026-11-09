// Atomic JSON/file writes and process liveness/identity, shared by every daemon here.

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

export function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

export function atomicWrite(file, obj, mode = 0o644) {
  writeAtomic(file, `${JSON.stringify(obj, null, 2)}\n`, mode);
}

export function writeAtomic(file, data, mode = 0o644) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp.${process.pid}`;
  // fsync before rename: without it a crash/power loss can leave a zero-length
  // file after the rename commits (for the allocator's banned.json, a torn read
  // resets the ban list).
  try {
    const fd = fs.openSync(tmp, 'w', mode);
    try {
      fs.writeFileSync(fd, data);
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
