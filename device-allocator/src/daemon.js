// The allocator daemon — a single long-lived arbiter process.
//
// Why a daemon (not per-agent logic): allocation is a shared-arbitration
// problem. One process holding the table in memory makes "never hand the same
// device to two agents" trivial and race-free, and gives us one place to run
// the reclaim loops. Each agent's stdio MCP server is a thin client of this.
//
// It listens on a unix socket and exposes:
//   POST /request  {ownerPid, agentName, platform, version}      -> allocate (boots if needed)
//   POST /release  {ownerPid, deviceId?}                          -> free (all of owner's if id omitted)
//   POST /change   {ownerPid, deviceId?, platform?, version?}     -> free old + allocate new
//   POST /broken   {ownerPid, deviceId?, reason?}                 -> quarantine + repair + allocate other
//   GET  /state                                                   -> public snapshot
//   GET  /health
//
// Reclamation: a device is freed when the owning agent's MCP-server PID dies
// (reaper) or when its screen shows zero motion for >1h (idle sweep).

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import {
  SOCKET_PATH, DISCOVERY_PATH, REPAIRS_DIR,
  IDLE_LIMIT_MS, REAP_INTERVAL_MS, IDLE_INTERVAL_MS, POOL_INTERVAL_MS, ALLOC_GRACE_MS,
  QUOTA, AWAIT_TIMEOUT_MS,
} from './paths.js';
import { writeDiscovery, writeState, pidAlive, ensureDirs } from './state.js';
import { log } from './log.js';
import * as dev from './devices.js';

const allocations = new Map(); // device key -> allocation record
let lastPool = []; // cached enumeration, refreshed on a timer
let lastState = { updatedAt: null, devices: [] };

// Serialize selection + mutation so two concurrent /request calls can't pick
// the same free device. Boot happens *outside* the lock (it's slow).
let mutex = Promise.resolve();
function withLock(fn) {
  const run = mutex.then(fn, fn);
  mutex = run.then(() => {}, () => {});
  return run;
}

function httpError(code, message) { return Object.assign(new Error(message), { statusCode: code }); }
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- selection ------------------------------------------------------------

async function currentPool() {
  return [...await dev.listApple(), ...await dev.listAndroid()];
}

async function selectDevice(req, exclude = new Set()) {
  const all = await currentPool();
  const free = all.filter(
    (d) => !allocations.has(d.key) && !exclude.has(d.key) && dev.matchesRequirements(d, req),
  );
  if (!free.length) return null;
  // Prefer a device that is already booted (cheaper, and "prefer running devices").
  free.sort((a, b) => (b.state === 'booted' ? 1 : 0) - (a.state === 'booted' ? 1 : 0));
  return free[0];
}

// A specific device the agent named (e.g. one it just created) — for claiming it.
async function findPoolDevice(deviceId) {
  const all = await currentPool();
  return all.find(
    (d) => d.handle === deviceId || d.udid === deviceId || d.serial === deviceId
      || d.key === deviceId || d.avd === deviceId,
  ) || null;
}

function allocatedByHandle(deviceId) {
  return [...allocations.values()].find(
    (a) => a.handle === deviceId || a.udid === deviceId || a.serial === deviceId
      || a.key === deviceId || a.avd === deviceId);
}

// Devices currently held by a live agent — these count toward the quota.
function activeCount() {
  return [...allocations.values()].filter((a) => a.ownerPid != null).length;
}

function reqOf(p) {
  return {
    platform: (p.platform || 'any').toLowerCase(),
    format: p.format ? String(p.format).toLowerCase() : undefined,
    version: p.version && String(p.version).toLowerCase() !== 'any' ? String(p.version) : 'any',
  };
}
function publicAlloc(a) {
  return {
    outcome: 'allocated', deviceId: a.handle, key: a.key, platform: a.platform,
    format: a.format || null, name: a.name, version: a.version, apiVersion: a.apiVersion,
    status: a.status, agentName: a.agentName, allocatedAt: a.allocatedAt,
  };
}

function reserve(chosen, p, req) {
  const wasBooted = chosen.state === 'booted';
  const alloc = {
    key: chosen.key, platform: chosen.platform, name: chosen.name, version: chosen.version,
    apiVersion: chosen.apiVersion, format: chosen.format || null,
    handle: chosen.handle, udid: chosen.udid, avd: chosen.avd, serial: chosen.serial,
    ownerPid: p.ownerPid || null, ownerTty: p.tty || null,
    agentName: p.agentName || `pid:${p.ownerPid}`, requirements: req,
    status: 'booting', bootedByUs: !wasBooted,
    allocatedAt: Date.now(), lastMotionAt: Date.now(), motionHash: null,
  };
  allocations.set(chosen.key, alloc);
  publish();
  return alloc;
}

function findOwned(ownerPid, deviceId) {
  return [...allocations.values()].filter(
    (a) => a.ownerPid === ownerPid &&
      (!deviceId || a.handle === deviceId || a.udid === deviceId || a.serial === deviceId || a.key === deviceId),
  );
}

// ---- handlers -------------------------------------------------------------

async function handleRequest(p, exclude = new Set()) {
  const req = reqOf(p);
  const reserved = await withLock(async () => {
    reapDeadOwners();
    // Quota gate first. The pool is effectively unbounded (agents create devices
    // on demand), so we cap CONCURRENCY, not inventory. Exhausted -> the agent is
    // told to await a free slot; it is NOT free to squat a device on its own.
    if (activeCount() >= QUOTA) {
      return { outcome: 'exhausted', quota: QUOTA, active: activeCount(), requirements: req };
    }
    // Claim a specific device the agent created and named.
    if (p.deviceId) {
      if (allocatedByHandle(p.deviceId)) throw httpError(409, `device ${p.deviceId} is already allocated`);
      const specific = await findPoolDevice(p.deviceId);
      if (specific && !exclude.has(specific.key)) return reserve(specific, p, req);
      // named device not visible yet — fall through to create guidance
    }
    const chosen = await selectDevice(req, exclude);
    if (!chosen) {
      // Under quota but nothing matches -> the agent must create a device to spec
      // (there is NO fixed pool) and then call request-device again.
      return { outcome: 'needs-create', requirements: req };
    }
    return reserve(chosen, p, req);
  });
  if (reserved.outcome) return reserved; // exhausted / needs-create: nothing to boot
  await bootAlloc(reserved); // outside the lock
  if (reserved.status === 'error') {
    // Don't pin a device that failed to boot — return it to the pool and report,
    // so the agent can retry or report-device-broken rather than hold a zombie.
    await withLock(async () => {
      if (allocations.get(reserved.key) === reserved) { allocations.delete(reserved.key); publish(); }
    });
    throw httpError(503, `device ${reserved.name || reserved.key} failed to boot`);
  }
  return publicAlloc(reserved);
}

// Wait for a concurrency slot to free (called by an agent after 'exhausted').
async function handleAwait(p) {
  const deadline = Date.now() + AWAIT_TIMEOUT_MS;
  for (;;) {
    await withLock(async () => { reapDeadOwners(); });
    if (activeCount() < QUOTA) {
      return { outcome: 'slot-available', active: activeCount(), quota: QUOTA };
    }
    if (Date.now() > deadline) {
      return { outcome: 'await-timeout', active: activeCount(), quota: QUOTA };
    }
    await delay(2000);
  }
}

async function bootAlloc(alloc) {
  try {
    if (dev.isApplePlatform(alloc.platform)) {
      const r = await dev.bootIOS(alloc.udid);
      alloc.handle = r.handle || alloc.udid;
      alloc.status = r.ok ? 'ready' : 'error';
    } else {
      const r = await dev.bootAndroid(alloc.avd, { wipe: false });
      alloc.serial = r.serial; alloc.handle = r.serial;
      alloc.status = r.serial ? 'ready' : 'error';
    }
  } catch (e) {
    alloc.status = 'error';
    log('boot error', alloc.key, String(e));
  }
  alloc.lastMotionAt = Date.now();
  refreshPool();
}

async function handleRelease(p, { shutdown = true } = {}) {
  return withLock(async () => {
    const targets = findOwned(p.ownerPid, p.deviceId);
    for (const a of targets) {
      allocations.delete(a.key);
      if (shutdown && a.bootedByUs) shutdownAlloc(a);
    }
    publish();
    return { released: targets.length, keys: targets.map((t) => t.key) };
  });
}

async function handleChange(p) {
  const [old] = findOwned(p.ownerPid, p.deviceId);
  const exclude = new Set(old ? [old.key] : []);
  if (old) await handleRelease({ ownerPid: p.ownerPid, deviceId: old.handle });
  return handleRequest({
    ownerPid: p.ownerPid,
    agentName: p.agentName || old?.agentName,
    platform: p.platform || old?.requirements?.platform,
    format: p.format || old?.requirements?.format,
    version: p.version || old?.requirements?.version,
    tty: p.tty || old?.ownerTty,
  }, exclude);
}

async function handleBroken(p) {
  const [old] = findOwned(p.ownerPid, p.deviceId);
  if (old) {
    // Quarantine: keep it in the table (so it can't be re-handed out) but flip
    // it to 'repairing' and drop the owner so the reaper leaves it alone.
    old.status = 'repairing';
    old.brokenReason = p.reason || '';
    old.ownerPid = null;
    old.agentName = 'repair';
    old.repairStartedAt = Date.now();
    publish();
    dispatchRepair(old);
  }
  // Immediately hand the reporting agent a different device with the same needs.
  const exclude = new Set(old ? [old.key] : []);
  return handleRequest({
    ownerPid: p.ownerPid,
    agentName: p.agentName,
    platform: old?.requirements?.platform || p.platform,
    format: old?.requirements?.format || p.format,
    version: old?.requirements?.version || p.version,
    tty: p.tty || old?.ownerTty,
  }, exclude);
}

// ---- repair ---------------------------------------------------------------

function dispatchRepair(alloc) {
  ensureDirs();
  try { fs.mkdirSync(REPAIRS_DIR, { recursive: true }); } catch {}
  log('repair: starting', alloc.key, alloc.brokenReason);
  // Per spec, a repair AGENT is dispatched to diagnose and fix the device (it
  // decides whether cleaning / resetting / uninstalling is needed) — we do NOT
  // unconditionally wipe a device, which would silently destroy the user's sim
  // state on a transient report. Opt in to a daemon-driven destructive reset
  // (erase / -wipe-data) with DA_AUTO_REPAIR=1 for zero-agent recovery.
  if (process.env.DA_AUTO_REPAIR === '1') {
    automatedRepair(alloc)
      .then((ok) => {
        if (ok) {
          allocations.delete(alloc.key); // back into the pool
          publish();
          log('repair: automated fix succeeded, returned to pool', alloc.key);
        } else {
          spawnRepairAgent(alloc);
        }
      })
      .catch((e) => { log('repair: automated fix threw', alloc.key, String(e)); spawnRepairAgent(alloc); });
  } else {
    spawnRepairAgent(alloc);
  }
}

// Opt-in first-line automated repair (DA_AUTO_REPAIR=1): the cheap, DESTRUCTIVE
// "clean / reset" that wipes device state. iOS: shutdown + erase. Android: cold
// boot with -wipe-data. Off by default — see dispatchRepair.
async function automatedRepair(alloc) {
  try {
    if (alloc.platform === 'ios') {
      const r = await dev.eraseIOS(alloc.udid);
      return r.ok;
    }
    const r = await dev.wipeAndroid(alloc.avd);
    return r.ok;
  } catch { return false; }
}

// Escalation: hand the broken device to a fresh, visible agent to diagnose.
function spawnRepairAgent(alloc) {
  const id = `${alloc.platform}-${(alloc.avd || alloc.udid || 'dev')}-${Date.now()}`.replace(/[^A-Za-z0-9-]/g, '');
  const logPath = path.join(REPAIRS_DIR, `${id}.log`);
  alloc.repairLog = logPath;
  alloc.status = 'repairing';
  publish();
  if (process.env.DA_NO_SPAWN === '1') { log('repair: agent spawn suppressed (DA_NO_SPAWN)', alloc.key); return; }
  // Collapse newlines: AppleScript `do script` literals are single-line, so a
  // newline in the (agent-supplied) broken reason would otherwise produce a
  // malformed program and the repair would silently never spawn.
  const prompt = repairPrompt(alloc).replace(/[\r\n]+/g, ' ');
  const inner = `claude --dangerously-skip-permissions ${shq(prompt)} 2>&1 | tee ${shq(logPath)}`;
  // Open a visible Terminal so the user can watch the unattended repair.
  const osa = `tell application "Terminal" to do script ${aq(inner)}`;
  try {
    spawn('osascript', ['-e', osa], { detached: true, stdio: 'ignore' }).unref();
    log('repair: agent dispatched', alloc.key, logPath);
  } catch (e) { log('repair: spawn failed', alloc.key, String(e)); }
}

function repairPrompt(alloc) {
  const what = alloc.platform === 'ios'
    ? `iOS simulator "${alloc.name}" (UDID ${alloc.udid})`
    : `Android emulator AVD "${alloc.avd}"`;
  return [
    `A device managed by the local device-allocator was reported BROKEN and taken out of the pool: ${what}.`,
    alloc.brokenReason ? `The reporting agent said: "${alloc.brokenReason}".` : '',
    `Diagnose and fix THIS device only — do NOT request a device from the allocator; you are the repair worker assigned to it directly.`,
    alloc.platform === 'ios'
      ? `Try: \`xcrun simctl shutdown ${alloc.udid}\`, then \`xcrun simctl erase ${alloc.udid}\`, then \`xcrun simctl boot ${alloc.udid}\` and confirm it reaches the home screen. If a specific app is the culprit, uninstall it with \`xcrun simctl uninstall\`.`
      : `Try: \`adb -s <serial> emu kill\` if running, then cold-boot clean with \`${'$ANDROID_HOME'}/emulator/emulator -avd ${alloc.avd} -wipe-data\`, wait for \`getprop sys.boot_completed\` = 1, and confirm the home screen. Recreate the AVD with avdmanager only if wiping does not help.`,
    `When it boots cleanly, it is healthy again. Report exactly what was wrong and what you did to fix it.`,
  ].filter(Boolean).join(' ');
}

const shq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`; // single-quote for the shell
const aq = (s) => `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`; // AppleScript string literal

// ---- reclamation loops ----------------------------------------------------

function reapDeadOwners() {
  let changed = false;
  for (const [key, a] of allocations) {
    if (a.status === 'repairing') continue;
    // Don't reap a device mid-boot: its handle/serial isn't known yet, so a
    // shutdown would no-op and orphan the booting emulator. It becomes
    // reapable once it reaches 'ready'/'error' (a bounded ≤180s window).
    if (a.status === 'booting') continue;
    if (Date.now() - a.allocatedAt < ALLOC_GRACE_MS) continue;
    if (a.ownerPid && !pidAlive(a.ownerPid)) {
      log('reap: owner pid dead, freeing', key, 'pid', a.ownerPid);
      allocations.delete(key);
      if (a.bootedByUs) shutdownAlloc(a);
      changed = true;
    }
  }
  if (changed) publish();
}

async function idleSweep() {
  let changed = false;
  for (const a of [...allocations.values()]) {
    if (a.status !== 'ready') continue;
    const h = await dev.motionHash(a);
    if (h == null) continue;
    // The screenshot above yields for seconds; the allocation may have been
    // freed and the device reallocated meanwhile. Only act if this exact
    // allocation still holds the key (identity, not just key match).
    if (allocations.get(a.key) !== a) continue;
    if (a.motionHash && h === a.motionHash) {
      if (Date.now() - a.lastMotionAt > IDLE_LIMIT_MS) {
        log('reap: idle >limit, freeing', a.key, 'idleMs', Date.now() - a.lastMotionAt);
        allocations.delete(a.key);
        if (a.bootedByUs) shutdownAlloc(a);
        changed = true;
      }
    } else {
      a.motionHash = h;
      a.lastMotionAt = Date.now();
    }
  }
  if (changed) publish();
}

function shutdownAlloc(a) {
  try {
    if (dev.isApplePlatform(a.platform)) dev.shutdownIOS(a.udid);
    else dev.shutdownAndroid(a.serial);
  } catch (e) { log('shutdown error', a.key, String(e)); }
}

// ---- public snapshot ------------------------------------------------------

async function refreshPool() {
  try { lastPool = [...await dev.listIOS(), ...await dev.listAndroid()]; }
  catch (e) { log('pool refresh error', String(e)); }
  publish();
}

function publish() {
  const devices = lastPool.map((d) => {
    const a = allocations.get(d.key);
    return {
      key: d.key, platform: d.platform, format: d.format || null,
      name: d.name, version: d.version, apiVersion: d.apiVersion,
      handle: a?.handle || d.handle || null,
      status: a ? a.status : (d.state === 'booted' ? 'running-free' : 'free'),
      owner: a ? { agentName: a.agentName, ownerPid: a.ownerPid } : null,
      allocatedAt: a?.allocatedAt || null,
      // Floored to whole minutes: the UI shows idle in minutes, and this keeps the
      // state file stable between polls (no per-8s rewrite/redraw churn).
      idleMs: a?.lastMotionAt ? Math.floor((Date.now() - a.lastMotionAt) / 60000) * 60000 : null,
      brokenReason: a?.brokenReason || null,
      repairLog: a?.repairLog || null,
    };
  });
  // Quarantined/repairing devices may have dropped out of the live pool listing.
  for (const [key, a] of allocations) {
    if (!devices.find((d) => d.key === key)) {
      devices.push({
        key, platform: a.platform, format: a.format || null,
        name: a.name, version: a.version, apiVersion: a.apiVersion,
        handle: a.handle || null, status: a.status,
        owner: { agentName: a.agentName, ownerPid: a.ownerPid }, allocatedAt: a.allocatedAt,
        idleMs: null, brokenReason: a.brokenReason || null, repairLog: a.repairLog || null,
      });
    }
  }
  const counts = devices.reduce((m, d) => {
    const bucket = d.owner ? 'allocated' : (d.status === 'free' ? 'free' : 'free-running');
    m[bucket] = (m[bucket] || 0) + 1; return m;
  }, {});
  lastState = { updatedAt: new Date().toISOString(), daemonPid: process.pid, counts, devices };
  writeState(lastState);
}

// ---- http server ----------------------------------------------------------

const server = http.createServer((req, res) => {
  const send = (code, obj) => {
    const s = JSON.stringify(obj);
    res.writeHead(code, { 'content-type': 'application/json' });
    res.end(s);
  };
  if (req.method === 'GET' && req.url === '/health') return send(200, { ok: true, pid: process.pid });
  if (req.method === 'GET' && req.url === '/state') return send(200, lastState);

  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', async () => {
    let p = {};
    try { p = body ? JSON.parse(body) : {}; } catch { return send(400, { error: 'invalid json' }); }
    try {
      let result;
      switch (req.url) {
        case '/request': result = await handleRequest(p); break;
        case '/await': result = await handleAwait(p); break;
        case '/release': result = await handleRelease(p); break;
        case '/change': result = await handleChange(p); break;
        case '/broken': result = await handleBroken(p); break;
        default: return send(404, { error: `unknown route ${req.url}` });
      }
      send(200, result);
    } catch (e) {
      send(e.statusCode || 500, { error: e.message || String(e) });
    }
  });
});

// Is a *live* daemon already answering on the socket? (vs. a stale socket file
// left by a crashed one.) Resolves false on any connection error/timeout.
function socketHealthy() {
  return new Promise((resolve) => {
    const req = http.request(
      { socketPath: SOCKET_PATH, path: '/health', method: 'GET', timeout: 1000 },
      (res) => { res.resume(); resolve(res.statusCode === 200); },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}

// Best-effort synchronous shutdown of devices we booted, on daemon exit, so a
// SIGTERM (e.g. uninstall) doesn't leave orphaned simulators/emulators running.
function shutdownBootedSync() {
  if (process.env.DA_FAKE_DEVICES != null) return;
  for (const a of allocations.values()) {
    if (!a.bootedByUs) continue;
    try {
      if (dev.isApplePlatform(a.platform) && a.udid) {
        execFileSync('xcrun', ['simctl', 'shutdown', a.udid], { timeout: 15000, stdio: 'ignore' });
      } else if (dev.isAndroidPlatform(a.platform) && a.serial) {
        execFileSync('adb', ['-s', a.serial, 'emu', 'kill'], { timeout: 15000, stdio: 'ignore' });
      }
    } catch {}
  }
}

function onListening() {
  try { fs.chmodSync(SOCKET_PATH, 0o600); } catch {}
  writeDiscovery();
  log('daemon listening', SOCKET_PATH, 'pid', process.pid);
  refreshPool();
  setInterval(reapDeadOwners, REAP_INTERVAL_MS);
  setInterval(refreshPool, POOL_INTERVAL_MS);
  setInterval(idleSweep, IDLE_INTERVAL_MS);
}

// Singleton acquisition without a pre-emptive unlink (which would clobber a live
// peer's socket and split-brain the allocator). We let listen() fail with
// EADDRINUSE and use a /health probe to distinguish "a live daemon owns it"
// (defer + exit) from "a stale socket file remains" (unlink + retry once).
let retriedListen = false;
server.on('error', async (e) => {
  if (e.code === 'EADDRINUSE') {
    if (await socketHealthy()) { log('a live daemon already owns the socket; exiting'); process.exit(0); }
    if (!retriedListen) {
      retriedListen = true;
      try { fs.unlinkSync(SOCKET_PATH); } catch {}
      server.listen(SOCKET_PATH, onListening);
      return;
    }
    log('could not acquire the socket (contended); deferring'); process.exit(0);
  }
  log('server error', String(e));
  process.exit(1);
});

async function start() {
  ensureDirs();
  // If a healthy daemon is already serving, never start a second one.
  if (await socketHealthy()) { log('another daemon is already live; exiting'); process.exit(0); }
  // NB: no pre-listen unlink — see the error handler above.
  server.listen(SOCKET_PATH, onListening);
  const bye = () => {
    shutdownBootedSync();
    try { fs.unlinkSync(SOCKET_PATH); } catch {}
    try { fs.unlinkSync(DISCOVERY_PATH); } catch {}
    process.exit(0);
  };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
}

start();
