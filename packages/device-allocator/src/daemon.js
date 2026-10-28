// The allocator daemon — a single long-lived arbiter process.
//
// Why a daemon (not per-agent logic): allocation is a shared-arbitration
// problem. One process holding the table in memory makes "never hand the same
// device to two agents" trivial and race-free, and gives us one place to run
// the reclaim loops. Each agent's stdio MCP server is a thin client of this.
//
// It listens on a unix socket and exposes:
//   POST /request  {ownerPid, agentName, platform, version, source} -> allocate (boots if needed)
//   POST /release  {ownerPid, deviceId?}                          -> free (all of owner's if id omitted)
//   POST /change   {ownerPid, deviceId?, platform?, version?}     -> allocate new + free old (old kept on failure)
//   POST /broken   {ownerPid, deviceId?, reason?}                 -> quarantine + repair + allocate other
//   POST /repaired {key | deviceId}                               -> end quarantine, device back to pool
//   POST /report-injection {person, pr?, evidence, agentName?, source?, handle?, place?, via?}
//                                                                 -> ban an author (or a Slack sender)
//   POST /unban    {login}                                        -> remove an author from the ban list
//   POST /kill     {key | deviceId}                               -> force-free + shut down (panel X)
//   POST /remote/sign-in                                          -> re-prompt the remote pool's sign-in
//   POST /remote/devices                                          -> what the remote pool lists now
//   GET  /state                                                   -> public snapshot
//   GET  /health
//
// A request tries the remote device pool (src/remote.js), when one is configured, before
// the local simulators/emulators; `source` pins it to one of the two.
//
// Reclamation: a device is freed when the owning agent's MCP-server PID dies
// (reaper) or when its screen shows zero motion past the leased idle limit, and a
// booted device nobody holds is shut down past the free idle limit (idleSweep).

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync, execFile } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';

// Async subprocess for anything that runs ON the event loop (evidence capture):
// execFileSync would freeze the single-threaded daemon for the whole `gh`/browser
// call, during which /health can't be answered and a concurrent client's liveness
// probe times out — spawning a second daemon that unlinks the live socket. Sync is
// reserved for exit-time teardown (shutdownBootedSync), where blocking is fine.
const pexecFile = promisify(execFile);
import {
  SOCKET_PATH, DISCOVERY_PATH, STATE_PATH, LEASES_PATH, REMOTE_RELEASES_PATH, REPAIRS_DIR,
  LEASED_IDLE_DEFAULT_MIN, FREE_IDLE_DEFAULT_MIN, STOPPING_GRACE_MS,
  REAP_INTERVAL_MS, IDLE_INTERVAL_MS, POOL_INTERVAL_MS, ALLOC_GRACE_MS,
  QUOTA, AWAIT_TIMEOUT_MS, REPAIR_TTL_MS, BAN_DIR, BANNED_PATH, INJECTIONS_DIR, AUDIT_PATH,
  ADB_BIN,
} from './paths.js';
import {
  writeDiscovery, readDiscovery, writeState, writeLeases, readLeases, readConfig, pidAlive, ensureDirs,
  atomicWrite, sweepDeadTmp,
} from './state.js';
import { log } from './log.js';
import * as dev from './devices.js';
import * as remote from './remote.js';
import { openRepairWindow, shq } from './terminal.js';

const allocations = new Map(); // device key -> allocation record
const freeMotion = new Map(); // key of a booted, unallocated device -> {hash, lastMotionAt}
const stopping = new Map(); // key -> deadline of a device this daemon is shutting down
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

// ---- selection ------------------------------------------------------------

async function currentPool() {
  return [...await dev.listApple(), ...await dev.listAndroid()];
}

// Shut down but still enumerated as booted: handing it out would give an agent a
// device that disappears under it.
function isStopping(d) {
  const until = stopping.get(d.key);
  if (until == null) return false;
  if (d.state === 'booted' && Date.now() < until) return true;
  stopping.delete(d.key);
  return false;
}

async function selectDevice(req, exclude = new Set()) {
  const all = await currentPool();
  const free = all.filter(
    (d) => !allocations.has(d.key) && !exclude.has(d.key) && !isStopping(d)
      && dev.matchesRequirements(d, req),
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

// Local devices currently held by a live agent — these count toward the quota, which
// guards this machine's CPU; a remote device runs elsewhere and is capped by its pool.
function activeCount() {
  return [...allocations.values()].filter((a) => a.ownerPid != null && a.source !== 'remote').length;
}

function reqOf(p) {
  return {
    platform: (p.platform || 'any').toLowerCase(),
    format: p.format ? String(p.format).toLowerCase() : undefined,
    version: p.version && String(p.version).toLowerCase() !== 'any' ? String(p.version) : 'any',
    source: ['remote', 'local'].includes(p.source) ? p.source : 'any',
  };
}
function publicAlloc(a) {
  return {
    outcome: 'allocated', deviceId: a.handle, key: a.key, platform: a.platform, source: a.source || 'local',
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
    ownerPid: p.ownerPid || null,
    agentName: p.agentName || `pid:${p.ownerPid}`, requirements: req,
    status: 'booting', bootedByUs: !wasBooted || !!chosen.lease, // a lease is always ours to give back
    allocatedAt: Date.now(), lastMotionAt: Date.now(), motionHash: null,
    ...(chosen.lease ? { source: 'remote', remote: chosen.lease, remoteProvider: chosen.provider } : {}),
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
  // The owner PID is the ownership token everything else keys on: the reaper can
  // only free leases whose owner is known-dead, and quota only counts owned leases.
  // An ownerless lease would be unreclaimable and quota-invisible — reject it.
  if (!p.ownerPid) throw httpError(400, 'ownerPid is required');
  const req = reqOf(p);
  let remoteSkipped = null;
  if (!p.deviceId && req.source !== 'local') {
    const r = await requestRemote(p, req, exclude);
    if (r.outcome) return r;
    if (req.source === 'remote') return { outcome: 'remote-unavailable', reason: r.reason, requirements: req };
    if (r.applies) remoteSkipped = r.reason;
  }
  const local = await requestLocal(p, req, exclude);
  return remoteSkipped ? { ...local, remoteSkipped } : local;
}

// A leased remote device, attached and ready, or {reason, applies} when there is none.
async function requestRemote(p, req, exclude) {
  const got = await remote.acquire(req, exclude);
  if (!got.lease) return got;
  const alloc = await withLock(async () => reserve({ ...got.device, lease: got.lease, provider: got.provider }, p, req));
  try {
    alloc.serial = alloc.handle = await remote.attach(alloc.remoteProvider, alloc.remote, (why) => remoteLeaseEnded(alloc, why));
  } catch (e) {
    log('remote: attach failed', alloc.key, String(e));
    remote.release(alloc.remoteProvider, alloc.remote);
    if (alloc.ended) return { applies: true, reason: `${alloc.name} was leased, but its pool ended the lease while attaching: ${alloc.ended}` };
    if (allocations.get(alloc.key) !== alloc) throw httpError(409, `device ${alloc.name} was released during boot`);
    await withLock(async () => {
      if (allocations.get(alloc.key) === alloc) { allocations.delete(alloc.key); publish(); }
    });
    return { applies: true, reason: `${alloc.name} was leased but could not be attached: ${e.message}` };
  }
  // Freed or killed while attaching: given back then, but the attach that finished
  // since may have opened what that release had nothing to close yet.
  if (alloc.ended || allocations.get(alloc.key) !== alloc) {
    remote.release(alloc.remoteProvider, alloc.remote);
    if (alloc.ended) {
      await withLock(async () => {
        if (allocations.get(alloc.key) === alloc) { allocations.delete(alloc.key); publish(); }
      });
      return { applies: true, reason: `${alloc.name} was leased, but its pool ended the lease while attaching: ${alloc.ended}` };
    }
    throw httpError(409, `device ${alloc.name} was released during boot`);
  }
  alloc.status = 'ready';
  alloc.lastMotionAt = Date.now();
  publish();
  return publicAlloc(alloc);
}

// The pool ended a lease under its holder: expiry, the device or its host gone.
function remoteLeaseEnded(alloc, why) {
  alloc.ended = why; // read by an attach or a change-device that has the record out of the table
  remote.release(alloc.remoteProvider, alloc.remote); // a lease we only lost contact with may still be live
  withLock(async () => {
    if (allocations.get(alloc.key) !== alloc) return;
    allocations.delete(alloc.key);
    publish();
    alloc.endAudited = true;
    appendAudit('daemon', 'remote-lease-ended', `${alloc.name} taken from ${alloc.agentName}: ${why}`);
  });
}

async function requestLocal(p, req, exclude) {
  // A device this daemon is shutting down is waited out (STOPPING_GRACE_MS at most)
  // rather than answered with needs-create, which would send the agent off to create
  // a device it doesn't need.
  let reserved;
  do {
    if (reserved) await delay(1000);
    reserved = await reserveFor(p, req, exclude);
  } while (reserved.outcome === 'stopping');
  if (reserved.outcome) return reserved; // exhausted / needs-create: nothing to boot
  await bootAlloc(reserved); // outside the lock
  // Released/killed while we were booting (bootAlloc already cleaned up): telling
  // the agent "allocated, yours exclusively" for a freed — possibly already shut
  // down, possibly re-allocated — device would be a lie.
  if (allocations.get(reserved.key) !== reserved) {
    throw httpError(409, `device ${reserved.name || reserved.key} was released during boot`);
  }
  if (reserved.status === 'error') {
    // Don't pin a device that failed to boot — return it to the pool and report,
    // so the agent can retry or report-device-broken rather than hold a zombie.
    // If we started the boot, also shut the half-booted device down (an Android
    // emulator that registered with adb but never finished booting keeps running).
    if (reserved.bootedByUs) shutdownAlloc(reserved);
    await withLock(async () => {
      if (allocations.get(reserved.key) === reserved) { allocations.delete(reserved.key); publish(); }
    });
    throw httpError(503, `device ${reserved.name || reserved.key} failed to boot`);
  }
  return publicAlloc(reserved);
}

// The locked half of handleRequest: quota, then a claimed or selected device reserved.
function reserveFor(p, req, exclude) {
  return withLock(async () => {
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
      // allocatedByHandle() scans STORED lease fields and can miss an in-flight lease:
      // an Android lease reserved for a not-yet-running AVD keeps serial=null until
      // bootAndroid resolves (up to 180s, outside the lock), while findPoolDevice()
      // resolves the id via LIVE enumeration, which surfaces the emulator's serial much
      // earlier. So mirror selectDevice's `!allocations.has(d.key)` filter on the resolved
      // key — otherwise reserve() would OVERWRITE the committed lease and hand one agent's
      // exclusive, actually-booted device to another (exclusivity violation + spurious 409
      // to the victim after its full boot wait).
      if (specific && allocations.has(specific.key)) {
        throw httpError(409, `device ${p.deviceId} is already allocated`);
      }
      if (specific && isStopping(specific)) return { outcome: 'stopping' };
      if (specific && !exclude.has(specific.key)) return reserve(specific, p, req);
      // Vega (Fire TV) has no enumeration CLI, so a created device can never show
      // up in the pool — trust the claim and track it as an unmanaged allocation
      // (no boot/shutdown/idle management; exclusivity bookkeeping only). Without
      // this the documented create-then-claim flow loops on needs-create forever.
      if (req.platform === 'vega' && !exclude.has(`vega:${p.deviceId}`)) {
        return reserve({
          key: `vega:${p.deviceId}`, platform: 'vega', name: p.deviceId,
          handle: p.deviceId, udid: null, avd: null, serial: null,
          version: req.version !== 'any' ? req.version : null, apiVersion: null,
          format: null, state: 'booted', // agent boots it itself; never shut it down
        }, p, req);
      }
      // The agent named a SPECIFIC device and it isn't visible: never silently hand
      // out a DIFFERENT one (falling through to selectDevice used to do exactly that
      // — e.g. a vega claim without platform:'vega' got a random free iPhone while
      // the agent went on driving its vega box unallocated).
      return { outcome: 'needs-create', requirements: req, missingDeviceId: p.deviceId };
    }
    const chosen = await selectDevice(req, exclude);
    if (!chosen && (await currentPool()).some((d) => !allocations.has(d.key) && !exclude.has(d.key)
        && isStopping(d) && dev.matchesRequirements(d, req))) {
      return { outcome: 'stopping' };
    }
    if (!chosen) {
      // Under quota but nothing matches -> the agent must create a device to spec
      // (there is NO fixed pool) and then call request-device again.
      return { outcome: 'needs-create', requirements: req };
    }
    return reserve(chosen, p, req);
  
  });
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
    if (alloc.platform === 'vega') {
      alloc.status = 'ready'; // unmanaged: the agent created and boots it itself
    } else if (dev.isApplePlatform(alloc.platform)) {
      const r = await dev.bootIOS(alloc.udid);
      alloc.handle = r.handle || alloc.udid;
      alloc.status = r.ok ? 'ready' : 'error';
    } else {
      const r = await dev.bootAndroid(alloc.avd, { wipe: false });
      alloc.serial = r.serial; alloc.handle = r.serial;
      // r.ok means boot COMPLETED; a serial alone means "registered with adb",
      // which a hung AVD reaches without ever becoming usable.
      alloc.status = r.ok ? 'ready' : 'error';
    }
  } catch (e) {
    alloc.status = 'error';
    log('boot error', alloc.key, String(e));
  }
  alloc.lastMotionAt = Date.now();
  // Released mid-boot (free-device / kill while we were waiting): the record is
  // gone from the table, so nobody will ever shut this device down — do it now.
  // But ONLY if nobody else claimed the device meanwhile: identity alone conflates
  // "freed" with "freed then re-allocated", and shutting it down in the second
  // case would kill the new owner's session.
  if (allocations.get(alloc.key) !== alloc) {
    if (alloc.bootedByUs && !allocations.has(alloc.key)) shutdownAlloc(alloc);
    return;
  }
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
  // Release WITHOUT shutdown first (frees the quota slot for the new request),
  // but keep the old record so we can restore it if no replacement materializes —
  // a needs-create/exhausted outcome must not leave the agent with nothing, its
  // old device already torn down.
  if (old) await handleRelease({ ownerPid: p.ownerPid, deviceId: old.handle }, { shutdown: false });
  // Returns whether old was actually put back — the freed (booted!) device is
  // exactly what a concurrent /request prefers, so it may already belong to
  // someone else by the time we try. NOTE: when that happens the quota can
  // transiently read one over (restore + the winner) — bounded, self-correcting,
  // and better than stranding the agent with nothing.
  const restoreOld = () => withLock(async () => {
    if (old?.ended) {
      if (!old.endAudited) appendAudit('daemon', 'remote-lease-ended', `${old.name} taken from ${old.agentName}: ${old.ended}`);
      return false;
    }
    if (old && !allocations.has(old.key)) { allocations.set(old.key, old); publish(); return true; }
    return false;
  });
  let result;
  try {
    result = await handleRequest({
      ownerPid: p.ownerPid,
      agentName: p.agentName || old?.agentName,
      platform: p.platform || old?.requirements?.platform,
      format: p.format || old?.requirements?.format,
      version: p.version || old?.requirements?.version,
      source: p.source || old?.requirements?.source,
    }, exclude);
  } catch (e) { await restoreOld(); throw e; }
  if (result.outcome !== 'allocated') {
    // Only claim keptDevice if the restore actually landed — reporting it when a
    // concurrent request took the device would have two agents "owning" it.
    const kept = await restoreOld();
    return kept ? { ...result, keptDevice: old.handle } : result;
  }
  // Shut the old device down ONLY if it's still unallocated: during our (possibly
  // minutes-long) boot, the freed old device sat in the pool as booted-and-free —
  // the first thing selectDevice hands to the next requester.
  if (old && old.bootedByUs && !allocations.has(old.key)) shutdownAlloc(old);
  return result;
}

async function handleBroken(p) {
  const [old] = findOwned(p.ownerPid, p.deviceId);
  if (old?.source === 'remote') {
    // Nothing here can repair a remote device; its pool resets what is returned.
    await withLock(async () => { allocations.delete(old.key); publish(); });
    shutdownAlloc(old);
    log('remote: reported broken, lease returned', old.key, p.reason || '');
  } else if (old) {
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
  const next = await handleRequest({
    ownerPid: p.ownerPid,
    agentName: p.agentName,
    platform: old?.requirements?.platform || p.platform,
    format: old?.requirements?.format || p.format,
    version: old?.requirements?.version || p.version,
    source: old?.requirements?.source || p.source,
  }, exclude);
  return old?.source === 'remote' ? { ...next, returnedToPool: true } : next;
}

// The repair agent (or the user, via curl) reports a quarantined device healthy
// again — drop the quarantine record so the device re-enters the pool. This is
// the missing half of report-device-broken: without it every broken report
// shrank the pool until a daemon restart.
async function handleRepaired(p) {
  return withLock(async () => {
    const a = p.key ? allocations.get(p.key)
      : [...allocations.values()].find((x) => p.deviceId
          && (x.handle === p.deviceId || x.udid === p.deviceId || x.avd === p.deviceId || x.serial === p.deviceId));
    if (!a || a.status !== 'repairing') {
      throw httpError(404, `no repairing device matches ${p.key || p.deviceId}`);
    }
    allocations.delete(a.key);
    publish();
    appendAudit('daemon', 'repair-done', `${a.name || a.key} repaired — returned to the pool`);
    log('repair: marked repaired, returned to pool', a.key);
    return { repaired: a.key };
  });
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
        // eraseIOS/wipeAndroid awaited a multi-second subprocess; during it a /kill or
        // /repaired could have removed THIS repair record and a /request re-handed the
        // key to a new owner. Only act if the record is still ours — else deleting by
        // key would drop the new owner's live lease (double-allocation) and the repair
        // agent would erase a device someone else now drives. Mirrors the identity
        // re-checks at handleRequest/bootAlloc/idleSweep.
        if (allocations.get(alloc.key) !== alloc) return;
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
// "clean / reset" that wipes device state. Apple (ios/apple-tv): shutdown + simctl
// erase. Android (android/android-tv): cold boot with -wipe-data. Off by default —
// see dispatchRepair.
async function automatedRepair(alloc) {
  try {
    // Route by platform FAMILY (dev.resetKind), not `=== 'ios'`: apple-tv is an
    // Apple sim (erased via simctl, and its lease carries a udid, not an avd). The
    // old `=== 'ios'` check sent apple-tv down the Android branch as
    // wipeAndroid(undefined), which never erased the tvOS sim and burned the whole
    // emulator boot-wait on a bogus `emulator -avd undefined`.
    const kind = dev.resetKind(alloc.platform);
    if (kind === 'erase') {
      const r = await dev.eraseIOS(alloc.udid);
      return r.ok;
    }
    if (kind === 'wipe') {
      const r = await dev.wipeAndroid(alloc.avd);
      return r.ok;
    }
    return false; // vega (unmanaged) / unknown: no local reset CLI — escalate to a repair agent
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
  const inner = `claude --dangerously-skip-permissions ${shq(repairPrompt(alloc))} 2>&1 | tee ${shq(logPath)}`;
  try {
    openRepairWindow(alloc.key, inner, path.join(REPAIRS_DIR, `${id}.sh`));
  } catch (e) { log('repair: spawn failed', alloc.key, String(e)); }
}

function repairPrompt(alloc) {
  const what = dev.isApplePlatform(alloc.platform)
    ? `Apple simulator "${alloc.name}" (UDID ${alloc.udid})`
    : alloc.platform === 'vega'
      ? `Vega device "${alloc.name}" (unmanaged — the allocator does not boot/shut down vega)`
      : `Android emulator AVD "${alloc.avd}"`;
  const how = dev.isApplePlatform(alloc.platform)
    ? `Try: \`xcrun simctl shutdown ${alloc.udid}\`, then \`xcrun simctl erase ${alloc.udid}\`, then \`xcrun simctl boot ${alloc.udid}\` and confirm it reaches the home screen. If a specific app is the culprit, uninstall it with \`xcrun simctl uninstall\`.`
    : alloc.platform === 'vega'
      ? `Use the Vega tooling to reset/reboot it and confirm it comes up healthy.`
      : `Try: \`adb -s <serial> emu kill\` if running, then cold-boot clean with \`${'$ANDROID_HOME'}/emulator/emulator -avd ${alloc.avd} -wipe-data\`, wait for \`getprop sys.boot_completed\` = 1, and confirm the home screen. Recreate the AVD with avdmanager only if wiping does not help.`;
  // The key is agent-supplied for vega claims — JSON-encode + shell-quote it, never
  // interpolate it raw into the command the repair agent is told to execute.
  const repairedCmd = `curl -s --unix-socket ${shq(SOCKET_PATH)} -X POST http://localhost/repaired`
    + ` -H 'content-type: application/json' -d ${shq(JSON.stringify({ key: alloc.key }))}`;
  return [
    `A device managed by the local device-allocator was reported BROKEN and taken out of the pool: ${what}.`,
    alloc.brokenReason ? `The reporting agent said: "${alloc.brokenReason}".` : '',
    `Diagnose and fix THIS device only — do NOT request a device from the allocator; you are the repair worker assigned to it directly.`,
    how,
    `When it boots cleanly, it is healthy again. Return it to the allocator pool by running:`,
    `\`${repairedCmd}\`.`,
    `Then report exactly what was wrong and what you did to fix it.`,
  ].filter(Boolean).join(' ');
}


// ---- reclamation loops ----------------------------------------------------

function reapDeadOwners() {
  let changed = false;
  for (const [key, a] of allocations) {
    if (a.status === 'repairing') {
      // Quarantine is not forever: past the TTL (repair agent died, forgot to
      // notify, or never spawned) return the device to the pool. Worst case a
      // still-broken device re-enters and is simply re-reported.
      if (Date.now() - (a.repairStartedAt || a.allocatedAt) > REPAIR_TTL_MS) {
        log('reap: repair TTL expired, returning to pool', key);
        allocations.delete(key);
        changed = true;
      }
      continue;
    }
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

// A limit from Diplomat's settings in ms, re-read each sweep so a change lands within
// a minute; 0 never reaps. The env var overrides it for tests.
function idleLimitMs(envVar, key, defaultMin) {
  const env = Number(process.env[envVar]);
  if (env > 0) return env;
  const v = readConfig()[key];
  return (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : defaultMin) * 60 * 1000;
}

async function idleSweep() {
  const leasedLimit = idleLimitMs('DA_IDLE_LIMIT_MS', 'deviceIdleLeasedMinutes', LEASED_IDLE_DEFAULT_MIN);
  const freeLimit = idleLimitMs('DA_FREE_IDLE_LIMIT_MS', 'deviceIdleFreeMinutes', FREE_IDLE_DEFAULT_MIN);
  let changed = false;
  for (const a of [...allocations.values()]) {
    if (a.status !== 'ready') continue;
    const h = await dev.motionHash(a);
    if (h == null) continue;
    // The screenshot above yields for seconds; during that yield the allocation may
    // have been freed and the device reallocated (identity changes), OR /broken may
    // have flipped THIS same object to 'repairing' in place (identity preserved,
    // owner cleared, kept in the map as quarantine). Re-check BOTH: reaping a
    // just-quarantined device would delete the record the repair agent's /repaired
    // needs and let selectDevice re-hand the device mid-repair.
    if (allocations.get(a.key) !== a || a.status !== 'ready') continue;
    if (a.motionHash && h === a.motionHash) {
      if (leasedLimit && Date.now() - a.lastMotionAt > leasedLimit) {
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
  await sweepFreeDevices(freeLimit);
}

// Booted devices no lease covers: a repair agent's cold boot, a lease freed without
// a shutdown (the daemon didn't boot it), an emulator started by hand. No other path
// ever stops these.
async function sweepFreeDevices(limit) {
  const free = lastPool.filter((d) => d.state === 'booted' && !allocations.has(d.key));
  for (const key of freeMotion.keys()) {
    if (!free.some((d) => d.key === key)) freeMotion.delete(key);
  }
  for (const d of free) {
    const h = await dev.motionHash(d);
    if (h == null) continue;
    const m = freeMotion.get(d.key);
    if (!m || m.hash !== h) { freeMotion.set(d.key, { hash: h, lastMotionAt: Date.now() }); continue; }
    if (!limit || Date.now() - m.lastMotionAt <= limit) continue;
    await withLock(async () => {
      if (allocations.has(d.key)) return; // claimed during the screenshot
      log('reap: idle free device >limit, shutting down', d.key, 'idleMs', Date.now() - m.lastMotionAt);
      freeMotion.delete(d.key);
      shutdownAlloc(d);
    });
  }
}

function shutdownAlloc(a) {
  if (a.source === 'remote') { remote.release(a.remoteProvider, a.remote); return; }
  stopping.set(a.key, Date.now() + STOPPING_GRACE_MS);
  try {
    if (dev.isApplePlatform(a.platform)) dev.shutdownIOS(a.udid);
    else dev.shutdownAndroid(a.serial);
  } catch (e) { log('shutdown error', a.key, String(e)); }
}

// Force-kill a device by key regardless of owner: free any allocation and shut the
// sim/emulator down. Backs the panel's per-device X button. Also stops a free-but-
// running (booted, unallocated) device. Idempotent-ish: a device that's already off
// returns { killed, was: 'already-off' }.
async function handleKill(p) {
  return withLock(async () => {
    // Match by deviceId ONLY when one was given — otherwise `undefined === undefined`
    // would match the first device whose serial/udid is undefined (a real footgun).
    const byId = (x) => p.deviceId && (x.handle === p.deviceId || x.udid === p.deviceId || x.serial === p.deviceId);
    const a = p.key ? allocations.get(p.key) : [...allocations.values()].find(byId);
    if (a) {
      allocations.delete(a.key);
      shutdownAlloc(a);   // force shutdown even if we didn't boot it — the user asked to kill it
      publish();
      log('killed (was allocated)', a.key, 'owner', a.agentName || a.ownerPid || '—');
      return { killed: a.key, was: 'allocated' };
    }
    const d = lastPool.find((x) => (p.key && x.key === p.key) || byId(x));
    if (d) {
      let was = 'already-off';
      if (d.state === 'booted') {
        was = 'running-free';
        shutdownAlloc(d);
      }
      publish();
      log('killed', d.key, was);
      return { killed: d.key, was };
    }
    const e = new Error(`no such device to kill: ${p.key || p.deviceId}`); e.statusCode = 404; throw e;
  });
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
      key: d.key, platform: d.platform, source: 'local', format: d.format || null,
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
        key, platform: a.platform, source: a.source || 'local', format: a.format || null,
        name: a.name, version: a.version, apiVersion: a.apiVersion,
        handle: a.handle || null, status: a.status,
        owner: { agentName: a.agentName, ownerPid: a.ownerPid }, allocatedAt: a.allocatedAt,
        // A remote device is idle-swept like a local one; a quarantined one is not.
        idleMs: a.source === 'remote' ? Math.floor((Date.now() - a.lastMotionAt) / 60000) * 60000 : null,
        brokenReason: a.brokenReason || null, repairLog: a.repairLog || null,
      });
    }
  }
  const counts = devices.reduce((m, d) => {
    const bucket = d.owner ? 'allocated' : (d.status === 'free' ? 'free' : 'free-running');
    m[bucket] = (m[bucket] || 0) + 1; return m;
  }, {});
  lastState = { updatedAt: new Date().toISOString(), daemonPid: process.pid, counts, devices, remote: remote.status() };
  persist(lastState);
}

// The daemon's background disk writes. One that fails (a full disk) is not thrown
// but retried by the next publish(), which the pool refresh makes every
// POOL_INTERVAL_MS, so the files catch up once space returns. Logged once per
// outage, not once per attempt.
let discoveryWritten = false;
let persistFailing = false;
function persist(snapshot) {
  let failure = null;
  const attempt = (write) => { try { write(); return true; } catch (e) { failure ||= e; return false; } };
  if (!discoveryWritten) discoveryWritten = attempt(writeDiscovery);
  if (snapshot) {
    attempt(() => writeState(snapshot));
    // Full lease records too, so a restarted daemon rehydrates instead of silently
    // dropping every lease (see rehydrateLeases).
    attempt(() => writeLeases([...allocations.values()]));
  }
  if (failure && !persistFailing) log('persist failed, retrying on every publish:', String(failure));
  if (!failure && persistFailing) log('persist recovered');
  persistFailing = failure != null;
}

// The lease table lives in memory, but clients auto-respawn a dead daemon — if a
// restart started from an empty table, a device still driven by a live agent
// would be enumerated as free (booted, even preferred!) and handed to a second
// agent. Restore leases whose owner is still alive; keep quarantined devices
// quarantined across restarts.
function rehydrateLeases() {
  let restored = 0;
  for (const a of readLeases()) {
    if (!a || !a.key || allocations.has(a.key)) continue;
    const keep = a.status === 'repairing' || (a.ownerPid && pidAlive(a.ownerPid));
    if (!keep) {
      if (a.source === 'remote') remote.release(a.remoteProvider, a.remote); // else it sits leased until it expires
      continue;
    }
    // A lease caught mid-boot: the boot promise died with the old daemon. Treat
    // it as ready — exclusivity is what matters; a truly broken device gets
    // reported by its owner.
    if (a.status === 'booting') a.status = 'ready';
    a.motionHash = null; // stale screenshot hash must not trigger an instant idle reap
    a.lastMotionAt = Date.now();
    allocations.set(a.key, a);
    restored++;
    // The tunnel died with the old daemon: reopen it on the same port, so the agent's
    // adb serial still works, or drop the lease if the pool has ended it meanwhile.
    if (a.source === 'remote') {
      const port = Number(String(a.serial).split(':')[1]) || 0;
      remote.attach(a.remoteProvider, a.remote, (why) => remoteLeaseEnded(a, why), port)
        .then((serial) => { a.serial = a.handle = serial; publish(); })
        .catch((e) => remoteLeaseEnded(a, e.message));
    }
  }
  if (restored) log('rehydrated', restored, 'lease(s) from a previous daemon');
}

// ---- prompt-injection reports ---------------------------------------------

// `gh`/browser live outside the launch-agent PATH; augment it when we shell them.
function toolEnv() {
  const extra = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin';
  return { ...process.env, PATH: process.env.PATH ? `${process.env.PATH}:${extra}` : extra };
}
async function resolveGh() {
  for (const c of ['gh', '/opt/homebrew/bin/gh', '/usr/local/bin/gh', '/usr/bin/gh']) {
    try { await pexecFile(c, ['--version'], { timeout: 5000, env: toolEnv() }); return c; } catch {}
  }
  return null;
}
// "owner/repo#123", a PR URL, or a bare number → {owner, repo, number} (owner/repo may be null).
function parsePrRef(ref) {
  if (!ref) return null;
  const s = String(ref).trim();
  let m = s.match(/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/i);
  if (m) return { owner: m[1], repo: m[2], number: Number(m[3]) };
  m = s.match(/^([^/\s]+)\/([^/#\s]+)#(\d+)$/);
  if (m) return { owner: m[1], repo: m[2], number: Number(m[3]) };
  m = s.match(/(\d+)/);
  if (m) return { owner: null, repo: null, number: Number(m[1]) };
  return null;
}
async function captureScreenshot(url, outPath) {
  const browsers = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/opt/homebrew/bin/chromium', 'chromium', 'google-chrome-stable',
  ];
  for (const b of browsers) {
    try {
      await pexecFile(b, ['--headless=new', '--disable-gpu', '--hide-scrollbars',
        '--window-size=1400,3200', `--screenshot=${outPath}`, url],
        { timeout: 30000, env: toolEnv() });
      if (fs.existsSync(outPath)) return true;
    } catch {}
  }
  return false;
}
// Log the exact triggering content: the PR's gh JSON + human view, and a best-effort
// page screenshot (a private repo will screenshot a login page — gh content is the
// authoritative record, per spec). Async throughout so the (multi-second) gh/browser
// shell-outs never block the daemon's event loop — see pexecFile.
async function captureEvidence(dir, ref, gh) {
  const out = { ghCaptured: false, screenshotCaptured: false, url: null };
  if (!ref || ref.owner == null || !gh) return out;
  const nwo = `${ref.owner}/${ref.repo}`;
  try {
    const { stdout: json } = await pexecFile(gh, ['pr', 'view', String(ref.number), '--repo', nwo, '--json',
      'number,title,author,url,body,createdAt,headRefName,comments,reviews'],
      { encoding: 'utf8', timeout: 30000, env: toolEnv() });
    fs.writeFileSync(path.join(dir, 'pr.json'), json);
    try { out.url = JSON.parse(json).url; } catch {}
    out.ghCaptured = true;
  } catch (e) { log('injection evidence: gh json failed', String(e)); }
  try {
    const { stdout: text } = await pexecFile(gh, ['pr', 'view', String(ref.number), '--repo', nwo, '--comments'],
      { encoding: 'utf8', timeout: 30000, env: toolEnv() });
    fs.writeFileSync(path.join(dir, 'pr.txt'), text);
  } catch {}
  if (out.url) out.screenshotCaptured = await captureScreenshot(out.url, path.join(dir, 'screenshot.png'));
  return out;
}

// Append one line to the shared audit log the applet displays. O_APPEND makes small
// writes atomic across the daemon + applet.
function appendAudit(source, action, detail) {
  try {
    fs.mkdirSync(BAN_DIR, { recursive: true });
    fs.appendFileSync(AUDIT_PATH, `${JSON.stringify({ at: new Date().toISOString(), source, action, detail })}\n`);
  } catch {}
}

// Read the ban list, NEVER silently resetting it: an unparseable-but-present
// file (torn write, disk hiccup) is moved aside for forensics instead of being
// treated as empty — otherwise one bad read plus one new ban wipes every ban.
function readBanned() {
  let raw = null;
  try { raw = fs.readFileSync(BANNED_PATH, 'utf8'); } catch { return { banned: [] }; }
  try {
    const data = JSON.parse(raw);
    if (!Array.isArray(data.banned)) data.banned = [];
    return data;
  } catch (e) {
    const aside = `${BANNED_PATH}.corrupt-${Date.now()}`;
    try { fs.renameSync(BANNED_PATH, aside); } catch {}
    log('banned.json unparseable — moved aside', aside, String(e));
    appendAudit('daemon', 'warn', `banned.json was unreadable; preserved as ${path.basename(aside)}`);
    return { banned: [] };
  }
}

// banned.json is the whole point of a ban or an unban, so a write that fails (a
// full disk) fails the request, with nothing changed.
function writeBanned(data, what) {
  try { atomicWrite(BANNED_PATH, data); }
  catch (e) { throw httpError(507, `${what} not recorded: ${e.message}`); }
}

// Un-ban an author (the applet's panel calls this instead of editing banned.json
// directly, so ban/unban writes are serialized in one process and a concurrent
// injection report can't be lost to a read-modify-write race).
async function handleUnban(p) {
  const login = String(p.login || '').trim().replace(/^@/, '');
  if (!login) throw httpError(400, 'login is required');
  return withLock(async () => {
    const data = readBanned();
    const before = data.banned.length;
    data.banned = data.banned.filter((b) => (b.login || '').toLowerCase() !== login.toLowerCase());
    if (data.banned.length !== before) {
      writeBanned(data, `unban of @${login}`);
      appendAudit('panel', 'unban', `Un-banned @${login}`);
      log('unbanned', login);
    }
    return { removed: before - data.banned.length, total: data.banned.length };
  });
}

// A Slack sender is banned under `slack:<user id>`: no GitHub login holds a colon, so the
// review monitors never match one, and Comms (diplomat-core's CommsBan) asks for exactly it.
const SLACK_PREFIX = 'slack:';
const SLACK_USER_ID = /^[UW][A-Z0-9]{2,}$/;

// Who banned whom, where, in the activity feed's words.
function banLine(entry, via) {
  const who = entry.source === 'slack'
    ? `Slack user ${entry.handle ? `@${entry.handle} ` : ''}(${entry.login.slice(SLACK_PREFIX.length)})`
    : `@${entry.login}`;
  const where = entry.pr ? ` (${entry.pr})` : '';
  const quote = entry.evidence ? `: "${entry.evidence.replace(/\s+/g, ' ').slice(0, 160)}"` : '';
  const by = via === 'comms' ? ' - flagged by Comms triage' : ' - reporting agent terminated';
  return `Banned ${who} for prompt injection${where}${quote}${by}`;
}

async function handleReportInjection(p) {
  const source = p.source === 'slack' ? 'slack' : 'github';
  let login = String(p.person || '').trim().replace(/^@/, '');
  if (!login) { const e = new Error('person (the offending PR author login) is required'); e.statusCode = 400; throw e; }
  if (source === 'slack') {
    const id = login.startsWith(SLACK_PREFIX) ? login.slice(SLACK_PREFIX.length) : login;
    if (!SLACK_USER_ID.test(id)) {
      throw httpError(400, `person must be the sender's Slack user id (U… or W…), not "${login}"`);
    }
    login = SLACK_PREFIX + id;
  }
  const at = new Date().toISOString();
  const ref = source === 'slack' ? null : parsePrRef(p.pr);
  const gh = source === 'slack' || process.env.DA_FAKE_DEVICES != null ? null : await resolveGh(); // tests: skip real gh/browser
  const slug = `${at.replace(/[:.]/g, '-')}-${login.replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const dir = path.join(INJECTIONS_DIR, slug);
  try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  try { fs.writeFileSync(path.join(dir, 'evidence.txt'), String(p.evidence || '')); } catch {}
  const cap = await captureEvidence(dir, ref, gh);
  const handle = source === 'slack' ? String(p.handle || '').trim().replace(/^@/, '') || null : null;
  const place = source === 'slack' ? String(p.place || '').trim() || null : null;
  const incident = {
    login, source, handle, place, pr: p.pr || null, prRef: ref, evidence: p.evidence || '',
    reportedBy: p.agentName || null, via: p.via || 'agent',
    at, ghCaptured: cap.ghCaptured, screenshotCaptured: cap.screenshotCaptured, url: cap.url,
  };
  try { fs.writeFileSync(path.join(dir, 'report.json'), JSON.stringify(incident, null, 2)); } catch {}

  return withLock(async () => {
    const data = readBanned();
    const entry = {
      login, reason: 'prompt injection', pr: p.pr || null, evidence: (p.evidence || '').slice(0, 800),
      evidenceDir: dir, reportedBy: p.agentName || null, at,
      screenshot: cap.screenshotCaptured, ghCaptured: cap.ghCaptured,
      ...(source === 'slack' ? { source, handle, place } : {}),
    };
    const existing = data.banned.find((b) => (b.login || '').toLowerCase() === login.toLowerCase());
    // A repeat report that names no handle or channel keeps the ones already recorded.
    if (existing) {
      Object.assign(existing, entry, { firstAt: existing.firstAt || existing.at },
        source === 'slack' ? { handle: handle || existing.handle || null, place: place || existing.place || null } : {});
    }
    else data.banned.push({ ...entry, firstAt: at });
    writeBanned(data, `ban on @${login}`);
    appendAudit(p.via === 'comms' ? 'auto' : 'agent', 'ban', banLine(entry, p.via));
    log('prompt-injection reported; banned', login, 'pr', p.pr || '?', 'gh', cap.ghCaptured, 'shot', cap.screenshotCaptured, 'dir', dir);
    return { banned: true, login, total: data.banned.length, evidenceDir: dir,
             ghCaptured: cap.ghCaptured, screenshotCaptured: cap.screenshotCaptured, url: cap.url };
  });
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
  // Decode as UTF-8 via a StringDecoder so a multi-byte character split across two
  // socket 'data' chunks is reassembled, not turned into U+FFFD. Without this, a
  // large unicode report-prompt-injection `evidence` payload (verbatim forensic
  // capture) is silently corrupted at every chunk boundary. (The client sets the
  // same on its response in ipc.js.)
  req.setEncoding('utf8');
  req.on('data', (c) => {
    body += c;
    // No legitimate request body approaches 1MB; cap the buffer so a runaway
    // (or hostile local) writer can't balloon the daemon's memory.
    if (body.length > 1_000_000) { req.destroy(); }
  });
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
        case '/repaired': result = await handleRepaired(p); break;
        case '/report-injection': result = await handleReportInjection(p); break;
        case '/unban': result = await handleUnban(p); break;
        case '/kill': result = await handleKill(p); break;
        case '/remote/sign-in': result = await remote.signIn(); publish(); break;
        case '/remote/devices': result = await remote.devices(); break;
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
      // 3s, not 1s: this probe also gates the stale-socket takeover — a live peer
      // whose event loop is briefly blocked must not get its socket unlinked.
      { socketPath: SOCKET_PATH, path: '/health', method: 'GET', timeout: 3000 },
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
    if (!a.bootedByUs || a.source === 'remote') continue;
    try {
      if (dev.isApplePlatform(a.platform) && a.udid) {
        execFileSync('xcrun', ['simctl', 'shutdown', a.udid], { timeout: 15000, stdio: 'ignore' });
      } else if (dev.isAndroidPlatform(a.platform) && a.serial) {
        execFileSync(ADB_BIN, ['-s', a.serial, 'emu', 'kill'], { timeout: 15000, stdio: 'ignore' });
      }
    } catch {}
  }
}

function onListening() {
  try { fs.chmodSync(SOCKET_PATH, 0o600); } catch {}
  persist();
  log('daemon listening', SOCKET_PATH, 'pid', process.pid);
  refreshPool();
  remote.watch(publish);
  remote.load();
  remote.startRetrying();
  setInterval(reapDeadOwners, REAP_INTERVAL_MS);
  setInterval(refreshPool, POOL_INTERVAL_MS);
  setInterval(idleSweep, IDLE_INTERVAL_MS);
}

// Startup-takeover lock. The stale-socket reclaim ("unlink the dead socket file, then
// re-listen") is unavoidably two steps, so two daemons that both see the socket as stale
// each unlink + bind — and the second unlinks the first's FRESHLY bound socket, leaving
// two live daemons (split-brain: each runs its own reap/idle/reclaim loops against the
// shared lease files). Serialize the reclaim behind an exclusive lock so exactly one
// daemon takes over. Held only for the few ms of takeover, then released; a crash while
// holding it leaves a stale lock the next daemon reclaims by pid-liveness.
const LOCK_PATH = `${SOCKET_PATH}.lock`;
const LOCK_FRESH_MS = 5000; // far past the few ms a takeover holds it

function lockAgeMs() {
  try { return Date.now() - fs.statSync(LOCK_PATH).mtimeMs; } catch { return Infinity; }
}

function acquireTakeoverLock() {
  // An open fd (lock held) or null if a LIVE daemon already holds it. Throws when the
  // lock cannot be created at all (a full disk), which is no sign of a holder.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(LOCK_PATH, 'wx'); // atomic O_CREAT | O_EXCL
      try { fs.writeSync(fd, String(process.pid)); } catch {}
      return fd;
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      let holder = 0;
      try { holder = Number(fs.readFileSync(LOCK_PATH, 'utf8').trim()) || 0; } catch {}
      if (holder && holder !== process.pid && pidAlive(holder)) return null; // a live holder
      // No pid yet: a peer between its create and its write, or one whose write a full
      // disk refused. Either is taking over right now, so a young empty lock is held.
      if (!holder && lockAgeMs() < LOCK_FRESH_MS) return null;
      // Stale lock (holder dead/unreadable): drop it and retry the exclusive create once.
      // If another recoverer wins the re-create, our second attempt sees a live holder and
      // returns null (defers) — so the O_EXCL create, not the unlink, decides the winner.
      try { fs.unlinkSync(LOCK_PATH); } catch {}
    }
  }
  return null;
}

function releaseTakeoverLock(fd) {
  try { fs.closeSync(fd); } catch {}
  try { fs.unlinkSync(LOCK_PATH); } catch {}
}

// Singleton acquisition without a pre-emptive unlink (which would clobber a live peer's
// socket). We let listen() fail with EADDRINUSE and use a /health probe to distinguish "a
// live daemon owns it" (defer + exit) from "a stale socket file remains" (take the lock,
// unlink + retry once). A second EADDRINUSE means a fresh daemon bound in the gap — defer
// to it rather than take over again.
let retriedListen = false;
let takeoverLock = null; // held across the takeover's re-listen
server.on('error', async (e) => {
  // That re-listen failed (a peer bound in the gap, or a full disk): the lock goes too.
  if (takeoverLock != null) { releaseTakeoverLock(takeoverLock); takeoverLock = null; }
  if (e.code === 'EADDRINUSE') {
    if (await socketHealthy()) { log('a live daemon already owns the socket; exiting'); process.exit(0); }
    if (retriedListen) { log('could not acquire the socket (contended); deferring'); process.exit(0); }
    retriedListen = true;
    let lockFd;
    try { lockFd = acquireTakeoverLock(); }
    catch (err) { log('cannot take the startup lock', String(err)); process.exit(1); }
    if (lockFd == null) {
      // Another daemon is (or just finished) taking over. Give it a beat, then defer to it.
      await delay(300);
      if (await socketHealthy()) { log('another daemon took the socket; exiting'); process.exit(0); }
      log('the startup lock is held by a live daemon; deferring'); process.exit(0);
    }
    // Re-probe UNDER the lock: a peer may have bound between our first probe and the lock.
    if (await socketHealthy()) {
      releaseTakeoverLock(lockFd);
      log('a live daemon already owns the socket; exiting'); process.exit(0);
    }
    try { fs.unlinkSync(SOCKET_PATH); } catch {}
    takeoverLock = lockFd;
    server.listen(SOCKET_PATH, () => { releaseTakeoverLock(takeoverLock); takeoverLock = null; onListening(); });
    return;
  }
  log('server error', String(e));
  process.exit(1);
});

async function start() {
  ensureDirs();
  // If a healthy daemon is already serving, never start a second one.
  if (await socketHealthy()) { log('another daemon is already live; exiting'); process.exit(0); }
  for (const f of [DISCOVERY_PATH, STATE_PATH, LEASES_PATH, BANNED_PATH, REMOTE_RELEASES_PATH]) sweepDeadTmp(f);
  rehydrateLeases();
  // NB: no pre-listen unlink — see the error handler above.
  server.listen(SOCKET_PATH, onListening);
  const bye = async () => {
    const leased = [...allocations.values()].filter((a) => a.source === 'remote');
    await Promise.race([Promise.all(leased.map((a) => remote.release(a.remoteProvider, a.remote))), delay(3000)]);
    remote.recordInFlight();
    // Given back, or left for the next daemon to give back: never restored as held.
    for (const a of leased) allocations.delete(a.key);
    if (leased.length) try { writeLeases([...allocations.values()]); } catch (e) { log('bye: leases write failed', String(e)); }
    shutdownBootedSync();
    // Unless a daemon that took over while this one was blocked owns them.
    const owner = readDiscovery()?.pid;
    if (!owner || owner === process.pid || !pidAlive(owner)) {
      try { fs.unlinkSync(SOCKET_PATH); } catch {}
      try { fs.unlinkSync(DISCOVERY_PATH); } catch {}
    }
    process.exit(0);
  };
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
}

// Test seam: with DA_EXPOSE_TEST=1 the module can be imported to drive the real
// handlers directly (deterministic concurrency tests) without binding the socket.
// Unset in every real run, so production behavior is unchanged.
export { allocations, idleSweep, handleBroken, dispatchRepair, handleRequest, refreshPool };

if (process.env.DA_EXPOSE_TEST !== '1') start();
