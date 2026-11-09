// The remote-machines daemon: one per machine, keeping a table of the machines this
// one can reach over SSH, what each can do and how loaded it is right now.
//
// It listens on a unix socket and exposes:
//   POST /attach {ownerPid}       -> register an agent session's MCP server
//   POST /scan   {ownerPid, full} -> probe now (full: discover and re-read capabilities too)
//   GET  /state                   -> the snapshot (after the first scan)
//   GET  /health
//
// Every PROBE_INTERVAL_MS it re-reads the load of each machine that answered last time;
// every DISCOVERY_INTERVAL_MS it also re-discovers candidates (discovery.js), re-reads
// capabilities and retries the ones that did not answer. It exits once no session it
// serves is alive for IDLE_EXIT_MS, so nothing is probed for nobody.

import fs from 'node:fs';
import { atomicWrite, pidAlive, procIdentity, readJson, sweepDeadTmp } from '../../daemon-core/src/fsio.js';
import { createLog } from '../../daemon-core/src/log.js';
import { NO_ROUTE, jsonServer, listenSingleton, socketHealthy } from '../../daemon-core/src/serve.js';
import {
  BASE_DIR, SOCKET_PATH, DISCOVERY_PATH, STATE_PATH, LOG_PATH, CONFIG_PATH,
  PROBE_INTERVAL_MS, DISCOVERY_INTERVAL_MS, IDLE_EXIT_MS, CLIENT_CHECK_MS, LOAD_GAP_MIN_MS,
} from './paths.js';
import { candidates, mapLimit, SOURCES } from './discovery.js';
import { probe, describe, meanLoadBetween } from './probe.js';

const log = createLog(LOG_PATH);

const records = new Map(); // candidate key (host:port) -> record
let localRecord = { key: 'local', target: null, names: [], sources: [] };
let lastDiscoveryAt = 0;
let firstScan = null; // resolves once the table has been filled
let scanChain = Promise.resolve();
let scanning = false;
let snapshot = null;
const inFlight = new Set(); // ssh children, killed on exit
const clients = new Map(); // MCP server pid -> its procIdentity
let lastClientSeenAt = Date.now();

function settings() {
  const c = readJson(CONFIG_PATH);
  const list = (v) => (Array.isArray(v) ? v.filter((s) => typeof s === 'string' && s.trim()) : []);
  return { hosts: list(c?.remoteMachineHosts), exclude: list(c?.remoteMachineExclude) };
}

// ---- load since the last scan -----------------------------------------------

// Farther apart, the earlier reading has decayed to nothing.
const MAX_GAP_S = 30 * 60;

function sinceLastScan(rec, info, at) {
  const l5 = info.load.last5m;
  const prev = rec.lastLoad;
  if (l5 == null) return rec.load?.sinceLastScan ?? null;
  if (prev && prev.bootTime === info.bootTime) {
    const dt = (at - prev.at) / 1000;
    // A refresh right after a scan keeps the value it measured.
    if (dt * 1000 < LOAD_GAP_MIN_MS) return rec.load?.sinceLastScan ?? null;
    rec.lastLoad = { l5, at, bootTime: info.bootTime };
    return dt > MAX_GAP_S ? null : meanLoadBetween(prev.l5, l5, dt);
  }
  rec.lastLoad = { l5, at, bootTime: info.bootTime };
  return null;
}

// ---- probing ----------------------------------------------------------------

async function probeInto(rec, mode) {
  const at = Date.now();
  const r = await probe(rec.target, mode, (child) => {
    inFlight.add(child);
    child.on('close', () => inFlight.delete(child));
  });
  rec.probedAt = new Date(at).toISOString();
  if (!r.ok) {
    if (rec.status === 'ok') log('lost', rec.target, r.status, r.error);
    rec.status = r.status;
    rec.error = r.error;
    return;
  }
  const info = describe(r.fields);
  const caps = mode === 'full' ? info : rec.info;
  rec.info = { ...caps, ...info, ...(mode === 'full' ? {} : { gpus: info.gpus ?? caps?.gpus }) };
  if (mode === 'full') rec.capabilitiesAt = rec.probedAt;
  rec.load = { ...info.load, sinceLastScan: sinceLastScan(rec, info, at) };
  rec.latencyMs = r.latencyMs;
  if (rec.status !== 'ok' && rec.target) log('reached', rec.target);
  rec.status = 'ok';
  delete rec.error;
}

async function scanOnce(discover) {
  const full = discover || Date.now() - lastDiscoveryAt >= DISCOVERY_INTERVAL_MS;
  if (full) {
    const found = await candidates(settings());
    lastDiscoveryAt = Date.now();
    const keep = new Set(found.map((c) => c.key));
    for (const key of records.keys()) if (!keep.has(key)) records.delete(key);
    for (const c of found) {
      const rec = records.get(c.key) ?? { status: 'new' };
      records.set(c.key, Object.assign(rec, c));
      if (!c.trusted) {
        rec.status = 'untrusted';
        rec.error = 'its host key is not in known_hosts';
      }
    }
  }
  const due = [...records.values()].filter((r) => r.trusted && (full || r.status === 'ok'));
  await Promise.all([
    probeInto(localRecord, full || !localRecord.info?.os ? 'full' : 'load'),
    mapLimit(due, 16, (rec) => probeInto(rec, full || !rec.info?.os ? 'full' : 'load')),
  ]);
  publish();
}

/// Scans after any scan in flight. `discover` forces discovery and capabilities.
function scan(discover = false) {
  const run = scanChain.then(async () => {
    scanning = true;
    try { await scanOnce(discover); } catch (e) { log('scan failed', String(e)); } finally { scanning = false; }
  });
  scanChain = run;
  return run;
}

// ---- snapshot ---------------------------------------------------------------

const GB = 2 ** 30;

/// What makes this machine a poor place to run something right now.
function warnings({ info, load }) {
  const out = [];
  const now = load?.last1m;
  if (info.cores && now != null && now / info.cores > 1) out.push(`overloaded: load ${now} on ${info.cores} cores`);
  const mem = info.memory;
  if (mem?.pressure === 'warning' || mem?.pressure === 'critical') out.push(`memory pressure ${mem.pressure}`);
  else if (mem?.totalBytes && mem.availableBytes != null && mem.availableBytes / mem.totalBytes < 0.1) {
    out.push(`low memory: ${(mem.availableBytes / GB).toFixed(1)} GB available`);
  }
  const swap = info.swap;
  if (swap?.totalBytes && swap.usedBytes / swap.totalBytes > 0.75) {
    out.push(`swap ${Math.round((100 * swap.usedBytes) / swap.totalBytes)}% used`);
  }
  if (info.disk && info.disk.freeBytes < 20 * GB) out.push(`low disk: ${(info.disk.freeBytes / GB).toFixed(0)} GB free`);
  return out;
}

/// Cores left over once the current load (the freshest reading, the 1-minute one) is served.
function spareCores({ info, load }) {
  const used = load?.last1m;
  return info.cores == null || used == null ? null : Math.round(Math.max(0, info.cores - used) * 10) / 10;
}

function view(rec) {
  const m = {
    target: rec.target,
    ...(rec.names?.length > 1 ? { aliases: rec.names.filter((n) => n !== rec.target) } : {}),
    ...(rec.target ? { hostname: rec.hostname, user: rec.user, port: rec.port, sources: rec.sources } : {}),
    status: rec.status,
    ...(rec.error ? { error: rec.error } : {}),
    ...(rec.info ? { ...rec.info, load: rec.load } : {}),
    ...(rec.latencyMs != null ? { latencyMs: rec.latencyMs } : {}),
    probedAt: rec.probedAt ?? null,
    capabilitiesAt: rec.capabilitiesAt ?? null,
  };
  if (rec.status === 'ok') {
    m.spareCores = spareCores(rec);
    m.warnings = warnings(rec);
  }
  return m;
}

function publish() {
  const local = view(localRecord);
  const localId = localRecord.info?.machineId;
  const ready = new Map(); // machineId -> view: one machine reached under two names is listed once
  const unavailable = [];
  for (const rec of records.values()) {
    const m = view(rec);
    if (rec.status !== 'ok') { unavailable.push(m); continue; }
    const id = m.machineId || rec.key;
    if (id === localId) continue;
    const prev = ready.get(id);
    if (!prev) { ready.set(id, m); continue; }
    const [keep, drop] = SOURCES.indexOf(m.sources[0]) < SOURCES.indexOf(prev.sources[0]) ? [m, prev] : [prev, m];
    keep.aliases = [...new Set([...(keep.aliases ?? []), drop.target, ...(drop.aliases ?? [])])];
    ready.set(id, keep);
  }
  snapshot = {
    updatedAt: new Date().toISOString(),
    discoveredAt: lastDiscoveryAt ? new Date(lastDiscoveryAt).toISOString() : null,
    probeIntervalMs: PROBE_INTERVAL_MS,
    local,
    machines: [...ready.values()].sort((a, b) =>
      (a.warnings.length > 0) - (b.warnings.length > 0) || (b.spareCores ?? 0) - (a.spareCores ?? 0)),
    unavailable: unavailable.sort((a, b) => a.status.localeCompare(b.status) || a.target.localeCompare(b.target)),
  };
  try { atomicWrite(STATE_PATH, snapshot, 0o644); } catch (e) { log('state write failed', String(e)); }
}

// ---- sessions ---------------------------------------------------------------

function attach(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return;
  if (!clients.has(pid)) clients.set(pid, procIdentity(pid));
  lastClientSeenAt = Date.now();
}

function checkClients() {
  for (const [pid, identity] of clients) {
    // A pid reused by another process is not the session that attached.
    if (!pidAlive(pid) || (identity && procIdentity(pid) !== identity)) clients.delete(pid);
  }
  if (clients.size) lastClientSeenAt = Date.now();
  else if (Date.now() - lastClientSeenAt >= IDLE_EXIT_MS) {
    log('no agent session for', IDLE_EXIT_MS / 1000, 's; exiting');
    bye();
  }
}

// ---- server -----------------------------------------------------------------

const server = jsonServer(async (route, p) => {
  switch (route) {
    case 'GET /health': return { ok: true, pid: process.pid };
    case 'GET /state': await firstScan; return snapshot;
    case 'POST /attach': attach(p.ownerPid); return { ok: true };
    case 'POST /scan': attach(p.ownerPid); await scan(!!p.full); return snapshot;
    default: return NO_ROUTE;
  }
});

function onListening() {
  try { fs.chmodSync(SOCKET_PATH, 0o600); } catch {}
  atomicWrite(DISCOVERY_PATH, {
    pid: process.pid, identity: procIdentity(process.pid), startedAt: new Date().toISOString(), socket: SOCKET_PATH, version: 1,
  }, 0o600);
  log('daemon listening', SOCKET_PATH, 'pid', process.pid);
  firstScan = scan(true);
  setInterval(() => { if (!scanning) scan(); }, PROBE_INTERVAL_MS);
  setInterval(checkClients, CLIENT_CHECK_MS);
}

function bye() {
  for (const child of inFlight) { try { child.kill('SIGKILL'); } catch {} }
  // Unless a daemon that took over owns them by now.
  const owner = readJson(DISCOVERY_PATH)?.pid;
  if (!owner || owner === process.pid || !pidAlive(owner)) {
    try { fs.unlinkSync(SOCKET_PATH); } catch {}
    try { fs.unlinkSync(DISCOVERY_PATH); } catch {}
  }
  process.exit(0);
}

async function start() {
  fs.mkdirSync(BASE_DIR, { recursive: true });
  if (await socketHealthy(SOCKET_PATH)) { log('another daemon is already live; exiting'); process.exit(0); }
  for (const f of [DISCOVERY_PATH, STATE_PATH]) sweepDeadTmp(f);
  listenSingleton({ server, socketPath: SOCKET_PATH, log, onListening });
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
}

start();
