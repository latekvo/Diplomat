// The remote-machines daemon: one per machine, keeping a table of the machines this
// one can reach over SSH, what each can do and how loaded it is right now.
//
// It listens on a unix socket and exposes:
//   POST /attach {ownerPid}       -> register an agent session's MCP server
//   POST /scan   {ownerPid, full} -> probe now (full: discover and re-read capabilities too);
//                                    without ownerPid nothing is attached
//   GET  /state                   -> the snapshot (after the first scan)
//   GET  /health
//
// Every PROBE_INTERVAL_MS it re-reads the load of each machine that has answered, now or
// before; every DISCOVERY_INTERVAL_MS it also re-discovers candidates (discovery.js),
// re-reads capabilities and retries the ones that never answered. Each reading also goes into
// history.json. It exits once no session it serves is alive for IDLE_EXIT_MS, so
// nothing is probed for nobody.
//
// The operator can take a machine out of the pool in Diplomat's settings: for good
// (remoteMachineExclude: it is not contacted at all) or until a time
// (remoteMachineExcludeUntil: it is still read, so its load stays on show). Either is
// applied within CONFIG_POLL_MS of the settings changing.

import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { atomicWrite, pidAlive, procIdentity, readJson, sweepDeadTmp } from '../../daemon-core/src/fsio.js';
import { createLog } from '../../daemon-core/src/log.js';
import { NO_ROUTE, jsonServer, listenSingleton, socketHealthy } from '../../daemon-core/src/serve.js';
import {
  BASE_DIR, SOCKET_PATH, DISCOVERY_PATH, STATE_PATH, HISTORY_PATH, IDS_PATH, LOG_PATH, CONFIG_PATH,
  PROBE_INTERVAL_MS, DISCOVERY_INTERVAL_MS, IDLE_EXIT_MS, CLIENT_CHECK_MS, LOAD_GAP_MIN_MS,
  HISTORY_MS, CONFIG_POLL_MS, RETRY_DELAY_MS,
} from './paths.js';
import { candidates, killAll, mapLimit, nameMatcher, SOURCES } from './discovery.js';
import { probe, describe, meanLoadBetween, spareCores, usage, warnings } from './probe.js';

const log = createLog(LOG_PATH);

const records = new Map(); // candidate key (host:port) -> record
let localRecord = { key: 'local', target: null, names: [], sources: [] };
let lastDiscoveryAt = 0;
let firstScan = null; // resolves once the table has been filled
let scanChain = Promise.resolve();
let scanning = false;
let snapshot = null;
const clients = new Map(); // MCP server pid -> its procIdentity
let lastClientSeenAt = Date.now();
const startedAt = new Date().toISOString();

function settings() {
  const c = readJson(CONFIG_PATH);
  const list = (v) => (Array.isArray(v) ? v.filter((s) => typeof s === 'string' && s.trim()) : []);
  const until = c?.remoteMachineExcludeUntil;
  const paused = until && typeof until === 'object' && !Array.isArray(until)
    ? Object.entries(until).map(([name, at]) => [name, typeof at === 'string' ? Date.parse(at) : NaN])
      .filter(([name, at]) => name.trim() && at > Date.now())
    : [];
  return { hosts: list(c?.remoteMachineHosts), exclude: list(c?.remoteMachineExclude), paused };
}

// ---- exclusion --------------------------------------------------------------

// The status a candidate has before any probe.
function baseStatus(c) {
  if (c.proxied) return { status: 'proxied', error: 'reached through a ProxyJump or ProxyCommand, which a probe cannot hold to known_hosts' };
  if (!c.trusted) return { status: 'untrusted', error: 'its host key is not in known_hosts' };
  return { status: 'new', error: undefined };
}

// Candidate key -> {id, hostKey}: the machine id last read through it, under the host key
// known_hosts held then. Kept on disk, so a machine excluded for good, which is never read
// again, is still known under all its names. A different machine at that address has a
// different key, and the id stops counting once known_hosts is changed to accept it.
const knownIds = new Map(Object.entries(readJson(IDS_PATH) ?? {})
  .filter(([, v]) => typeof v?.id === 'string' && typeof v?.hostKey === 'string'));

function machineIdOf(rec) {
  if (rec.info?.machineId) return rec.info.machineId;
  const known = knownIds.get(rec.key);
  return known && known.hostKey === rec.hostKey ? known.id : null;
}

function learnId(rec) {
  const id = rec.info?.machineId;
  const known = knownIds.get(rec.key);
  if (!id || !rec.key || !rec.hostKey || (known?.id === id && known.hostKey === rec.hostKey)) return;
  knownIds.set(rec.key, { id, hostKey: rec.hostKey });
  try { atomicWrite(IDS_PATH, Object.fromEntries(knownIds), 0o644); } catch (e) { log('ids write failed', String(e)); }
}

let exclusionRun = 0;
let nextExpiry = Infinity;

/// Marks each record with the operator's exclusion, read from the settings now:
/// rec.excluded is {until: null} for good or {until: iso} for a while, with `by` the
/// settings entries that name the machine, or null. A record excluded for good is not
/// probed; one let back in is probed by the next scan.
async function applyExclusions() {
  const run = ++exclusionRun;
  const s = settings();
  const matcher = async (name) => ({ name, match: await nameMatcher([name]) });
  const [forGood, paused] = await Promise.all([
    Promise.all(s.exclude.map(matcher)),
    Promise.all(s.paused.map(async ([name, at]) => ({ ...(await matcher(name)), at }))),
  ]);
  if (run !== exclusionRun) return; // a later call read newer settings
  nextExpiry = Math.min(...paused.map((p) => p.at));
  const named = (list, rec) => list.filter((e) => e.match(rec));
  // Entries that exclude a machine under one name hold all its other names too.
  const byId = (list) => {
    const out = new Map();
    for (const rec of records.values()) {
      const id = machineIdOf(rec);
      const hits = named(list, rec);
      if (id && hits.length) out.set(id, [...(out.get(id) ?? []), ...hits]);
    }
    return out;
  };
  const [gone, held] = [byId(forGood), byId(paused)];
  for (const rec of records.values()) {
    const id = machineIdOf(rec);
    const permanentBy = [...new Set([...named(forGood, rec), ...(gone.get(id) ?? [])].map((e) => e.name))];
    const pausedBy = [...new Set([...named(paused, rec), ...(held.get(id) ?? [])])];
    const by = [...new Set([...permanentBy, ...pausedBy.map((e) => e.name)])];
    const at = Math.max(...pausedBy.map((p) => p.at));
    const permanent = permanentBy.length > 0;
    rec.excluded = permanent ? { until: null, by } : at > 0 ? { until: new Date(at).toISOString(), by } : null;
    if (permanent && rec.status !== 'excluded') Object.assign(rec, { status: 'excluded', error: 'excluded in Diplomat' });
    else if (!permanent && rec.status === 'excluded') Object.assign(rec, baseStatus(rec));
  }
}

let configStamp = null;

function watchConfig() {
  const stamp = () => { try { const st = fs.statSync(CONFIG_PATH); return `${st.mtimeMs}:${st.size}:${st.ino}`; } catch { return null; } };
  configStamp = stamp();
  setInterval(async () => {
    const now = stamp();
    // A pause that ran out lets its machine back in as promptly as a settings change.
    if (now === configStamp && Date.now() < nextExpiry) return;
    configStamp = now;
    await applyExclusions();
    publish();
    // A machine let back in is read now rather than at the next interval.
    if ([...records.values()].some((r) => r.status === 'new' && r.trusted)) scan();
  }, CONFIG_POLL_MS).unref();
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
  let r = await probe(rec.target, mode);
  // A failure that comes back at once (a .local name that did not resolve for a moment)
  // gets one retry before a machine that answered is marked lost.
  if (!r.ok && rec.status === 'ok' && r.status === 'unreachable' && !r.timedOut) {
    await delay(RETRY_DELAY_MS);
    r = await probe(rec.target, mode);
  }
  // Excluded for good while the probe was out.
  if (rec.status === 'excluded') return;
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
  learnId(rec);
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
      if (c.proxied || !c.trusted) Object.assign(rec, baseStatus(c));
    }
  }
  await applyExclusions();
  // A machine that answered before and stopped is retried at every scan, not left
  // unreachable until the next discovery.
  const lost = (r) => r.status === 'unreachable' && r.info;
  const due = [...records.values()].filter((r) => r.trusted && (full ? r.status !== 'excluded' : r.status === 'ok' || r.status === 'new' || lost(r)));
  await Promise.all([
    probeInto(localRecord, full || !localRecord.info?.os ? 'full' : 'load'),
    mapLimit(due, 16, (rec) => probeInto(rec, full || !rec.info?.os ? 'full' : 'load')),
  ]);
  publish();
}

let queued = null; // the scan waiting behind the one in flight, which later callers join
let queuedDiscover = false;

/// Scans after any scan in flight. `discover` forces discovery and capabilities.
function scan(discover = false) {
  if (queued) { queuedDiscover ||= discover; return queued; }
  queuedDiscover = discover;
  const next = scanChain.then(async () => {
    const d = queuedDiscover;
    queued = null;
    scanning = true;
    try { await scanOnce(d); } catch (e) { log('scan failed', String(e)); } finally { scanning = false; }
  });
  queued = next;
  scanChain = next;
  return next;
}

// ---- snapshot ---------------------------------------------------------------

function view(rec) {
  const m = {
    id: machineIdOf(rec) || rec.key,
    target: rec.target,
    ...(rec.names?.length > 1 ? { aliases: rec.names.filter((n) => n !== rec.target) } : {}),
    ...(rec.target ? { hostname: rec.hostname, user: rec.user, port: rec.port, sources: rec.sources } : {}),
    status: rec.status,
    ...(rec.error ? { error: rec.error } : {}),
    ...(rec.info ? { ...rec.info, load: rec.load } : {}),
    ...(rec.latencyMs != null ? { latencyMs: rec.latencyMs } : {}),
    probedAt: rec.probedAt ?? null,
    capabilitiesAt: rec.capabilitiesAt ?? null,
    ...(rec.target ? { excluded: rec.excluded ?? null } : {}),
  };
  if (rec.status === 'ok') {
    m.spareCores = spareCores(rec);
    m.usage = usage(rec);
    m.warnings = warnings(rec);
  }
  return m;
}

// ---- history ----------------------------------------------------------------

// machine id -> [[at ms, cpu, memory, ssh sessions], ...], oldest first.
const history = new Map(Object.entries(readJson(HISTORY_PATH)?.machines ?? {}).filter(([, v]) => Array.isArray(v)));

/// Adds each machine's newest reading to the history, drops what is older than HISTORY_MS
/// and writes it out when anything changed.
function record(machines) {
  let changed = false;
  for (const m of machines) {
    if (m.status !== 'ok' || !m.probedAt) continue;
    const at = Date.parse(m.probedAt);
    const rows = history.get(m.id) ?? [];
    if (rows.length && rows.at(-1)[0] >= at) continue; // published again without a new reading
    rows.push([at, m.usage.cpu, m.usage.memory, m.sshSessions ?? null]);
    history.set(m.id, rows);
    changed = true;
  }
  const cutoff = Date.now() - HISTORY_MS;
  for (const [id, rows] of history) {
    const keep = rows.findIndex((r) => r[0] >= cutoff);
    if (keep === 0) continue;
    changed = true;
    if (keep === -1) history.delete(id);
    else rows.splice(0, keep);
  }
  if (!changed) return;
  try {
    atomicWrite(HISTORY_PATH, { version: 1, intervalMs: PROBE_INTERVAL_MS, machines: Object.fromEntries(history) }, 0o644);
  } catch (e) { log('history write failed', String(e)); }
}

// Best first: machines without warnings, then by spare cores.
const byRank = (a, b) => (a.warnings.length > 0) - (b.warnings.length > 0) || (b.spareCores ?? 0) - (a.spareCores ?? 0);

// The later of two exclusions (for good outlasts any time), named by the entries of both.
function longer(a, b) {
  if (!a || !b) return a ?? b;
  const until = a.until == null || b.until == null ? null : (a.until > b.until ? a : b).until;
  return { until, by: [...new Set([...a.by, ...b.by])] };
}

// Adds a view to a map by machine id, so one machine reached under two names is listed
// once: under a name it answered on, then the name from the most preferred source.
function merge(byId, m) {
  const prev = byId.get(m.id);
  if (!prev) { byId.set(m.id, m); return; }
  const rank = (v) => [v.status !== 'ok', SOURCES.indexOf(v.sources[0])];
  const [a, b] = [rank(m), rank(prev)];
  const [keep, drop] = ((a[0] - b[0]) || (a[1] - b[1])) < 0 ? [m, prev] : [prev, m];
  keep.aliases = [...new Set([...(keep.aliases ?? []), drop.target, ...(drop.aliases ?? [])])];
  keep.excluded = longer(keep.excluded, drop.excluded);
  byId.set(m.id, keep);
}

function publish() {
  const local = view(localRecord);
  const localId = localRecord.info?.machineId;
  const ready = new Map();
  const forGood = new Map();
  const paused = new Map(); // read or not: a paused machine stays listed as paused
  const unavailable = [];
  for (const rec of records.values()) {
    const m = view(rec);
    if (rec.status === 'excluded') merge(forGood, m);
    else if (rec.excluded) merge(paused, m);
    else if (rec.status !== 'ok') unavailable.push(m);
    else if (m.id !== localId) merge(ready, m);
  }
  const live = [...ready.values()];
  const pausedViews = [...paused.values()];
  const read = (list) => list.filter((m) => m.status === 'ok' && m.id !== localId);
  record([local, ...live, ...read(pausedViews)]);
  snapshot = {
    updatedAt: new Date().toISOString(),
    discoveredAt: lastDiscoveryAt ? new Date(lastDiscoveryAt).toISOString() : null,
    probeIntervalMs: PROBE_INTERVAL_MS,
    daemon: { pid: process.pid, startedAt, sessions: clients.size },
    local,
    machines: live.sort(byRank),
    // Excluded for a while (those read best first, then those that could not be), then for good.
    excluded: [
      ...read(pausedViews).sort(byRank),
      ...pausedViews.filter((m) => m.status !== 'ok').sort((a, b) => a.target.localeCompare(b.target)),
      ...[...forGood.values()].sort((a, b) => a.target.localeCompare(b.target)),
    ],
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
  watchConfig();
  setInterval(() => { if (!scanning) scan(); }, PROBE_INTERVAL_MS);
  setInterval(checkClients, CLIENT_CHECK_MS);
}

function bye() {
  killAll();
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
  for (const f of [DISCOVERY_PATH, STATE_PATH, HISTORY_PATH, IDS_PATH]) sweepDeadTmp(f);
  listenSingleton({ server, socketPath: SOCKET_PATH, log, onListening });
  process.on('SIGINT', bye);
  process.on('SIGTERM', bye);
}

start();
