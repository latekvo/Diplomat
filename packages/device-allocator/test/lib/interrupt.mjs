// A signal skips a test's finally, and a daemon started detached (by the test, mcp.js or
// install.js) sits in a process group of its own, out of reach of a group signal (^C, a
// CI cancel). What a test registers here runs on SIGINT, SIGTERM or SIGHUP instead.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const DAEMON_JS = fileURLToPath(new URL('../../src/daemon.js', import.meta.url));

// A process's environment, as words: Linux /proc, else (macOS) after the command in ps -E.
function environ(pid) {
  try { return fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0'); } catch {}
  try { return execFileSync('ps', ['-E', '-ww', '-o', 'command=', '-p', String(pid)], { encoding: 'utf8' }).split(/\s+/); }
  catch { return []; }
}

/// The daemons running under DA_BASE_DIR `base`. The user's own daemon may run this same
/// daemon.js, so the directory is what tells a test's daemons apart.
export function daemonsUnder(base) {
  let pids;
  try { pids = execFileSync('pgrep', ['-f', DAEMON_JS], { encoding: 'utf8' }).trim().split('\n').map(Number); }
  catch { return []; }
  return pids.filter((pid) => environ(pid).includes(`DA_BASE_DIR=${base}`));
}

/// SIGKILLs every daemon under `base`, a stopped one included.
export function killDaemonsUnder(base) {
  for (const pid of daemonsUnder(base)) {
    try { process.kill(pid, 'SIGCONT'); process.kill(pid, 'SIGKILL'); } catch {}
  }
}

const undo = [];
/// Runs `fn` when a signal ends the test, which then exits 1.
export function onInterrupt(fn) {
  if (undo.length === 0) {
    for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
      process.on(sig, () => {
        for (const f of undo) { try { f(); } catch {} }
        process.exit(1);
      });
    }
  }
  undo.push(fn);
}
