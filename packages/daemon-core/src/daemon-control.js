// Starting, stopping and replacing a daemon from an installer, found through the
// socket it serves and the daemon.json it writes once listening.

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { pidAlive, procIdentity, readJson } from './fsio.js';

/// `restartExitMs` is how long a replaced daemon gets to exit on SIGTERM.
export function daemonControl({ client, discoveryPath, restartExitMs }) {
  // The pid daemon.json names while it is still the daemon that wrote it: one killed past
  // its exit handler leaves the file, and its pid can pass to any process. It must run a
  // daemon.js and carry the file's procIdentity, which on Linux can recur after a reboot;
  // a file without one is matched by a start no later than the file instead.
  function discoveredDaemonPid(disc) {
    const pid = disc?.pid;
    if (!Number.isInteger(pid) || pid <= 0 || !pidAlive(pid)) return null;
    let out;
    try {
      out = execFileSync('ps', ['-o', 'lstart=,command=', '-p', String(pid)],
        { encoding: 'utf8', timeout: 2000, env: { ...process.env, LC_ALL: 'C' } });
    } catch { return null; }
    // "Tue Oct 27 22:05:29 2026  node /x/src/daemon.js"
    const words = out.trim().split(/\s+/);
    if (!words.slice(5).some((w) => path.basename(w) === 'daemon.js')) return null;
    if (disc.identity) return procIdentity(pid) === disc.identity ? pid : null;
    const wrote = Date.parse(disc.startedAt);
    const started = Date.parse(words.slice(0, 5).join(' '));
    // lstart has whole seconds; startedAt is written once the daemon listens.
    return Number.isNaN(wrote) || Number.isNaN(started) || started > wrote + 1000 ? null : pid;
  }

  /// daemon.json's pid while it is a live daemon; a stale file is removed.
  function liveDiscoveredPid() {
    const disc = readJson(discoveryPath);
    if (!disc) return null;
    const pid = discoveredDaemonPid(disc);
    if (pid == null && readJson(discoveryPath)?.startedAt === disc.startedAt) {
      try { fs.unlinkSync(discoveryPath); } catch {}
    }
    return pid;
  }

  /// {running, error?}. Never spawns beside a live but silent daemon (one shutting down):
  /// the new one would take its socket, leaving two daemons serving one state dir.
  async function startDaemon() {
    if (await client.daemonHealth()) return { running: true };
    const silent = liveDiscoveredPid();
    if (silent) {
      return { running: false, error: `the daemon (pid ${silent}) is running but not answering; it may be shutting down` };
    }
    try {
      await client.spawnDaemon(6000);
      return { running: true };
    } catch (e) {
      return { running: false, error: e.message };
    }
  }

  /// The pid it signalled, else null.
  async function stopDaemon() {
    try {
      // The serving daemon's own pid, else daemon.json's for one too wedged to answer.
      // Both come from the client's paths (honoring a test's base-dir override) — a
      // hardcoded ~/.diplomat path here would SIGTERM the user's real daemon from a
      // test-sandboxed uninstall.
      const pid = (await client.daemonHealth())?.pid || liveDiscoveredPid();
      if (pid) { process.kill(pid, 'SIGTERM'); return pid; }
    } catch {}
    return null;
  }

  // The one way to replace a daemon from older code, as startDaemon() keeps any that
  // answers. An error message, or null.
  async function restartDaemon() {
    const pid = await stopDaemon();
    const deadline = Date.now() + restartExitMs;
    while (pid && pidAlive(pid) && Date.now() < deadline) await delay(200);
    if (pid && pidAlive(pid)) {
      return `the running daemon (pid ${pid}) did not exit within ${restartExitMs / 1000} s`;
    }
    return (await startDaemon()).error ?? null;
  }

  return { discoveredDaemonPid, startDaemon, stopDaemon, restartDaemon };
}
