// The remote device pool: real devices somewhere else (a device farm), lent through a
// provider module the user points the allocator at, and tried before the local
// simulators/emulators. Nothing about any particular pool lives here — the provider
// is loaded from the path in Diplomat's `deviceRemoteProvider` setting (DA_REMOTE_PROVIDER
// overrides it, and is the only source under a fake pool, so tests never lease a real
// device).
//
// A provider module's default export is create(ctx) -> provider, where ctx carries the
// allocator's helpers ({log, run, adb, stateDir, atomicWrite, matchesRequirements,
// androidApi, wsock}) and the provider is:
//
//   name                          shown in the state snapshot and to agents
//   status()                      {state, ...} for the snapshot; synchronous, no network
//   acquire(req, exclude)         {device, lease} — device: {key, platform, name, version,
//                                 apiVersion, format}, lease: JSON, persisted for restarts —
//                                 or {applies, reason}; applies=false when the pool was never
//                                 in question (a platform it does not lend), so it stays quiet
//   attach(lease, onEnd, port)    makes the device usable here; resolves its id (an adb
//                                 serial). `port` asks for the local port a restarted daemon
//                                 had. onEnd(reason) fires once if the pool ends the lease.
//   release(lease)                gives it back; resolves once the pool has it (or no longer
//                                 knows the lease), rejects when that is not certain
//   signIn()          (optional)  re-prompts a sign-in; null when signed in, else why not
//   devices()         (optional)  {devices} the pool lists now, or {reason}

import fs from 'node:fs';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import {
  ADB_BIN, BASE_DIR, REMOTE_ACQUIRE_BUDGET_MS, REMOTE_ATTACH_BUDGET_MS, REMOTE_RELEASES_PATH, REMOTE_RELEASE_RETRY_MS,
} from './paths.js';
import { atomicWrite, readConfig } from './state.js';
import { run, matchesRequirements, androidApi } from './devices.js';
import { log } from './log.js';
import * as wsock from './wsock.js';

const instances = new Map(); // module path -> {provider, error, ready}
let onChange = null;

// Called whenever a provider finishes loading, so the snapshot can follow.
export function watch(fn) { onChange = fn; }

function configuredPath() {
  if (process.env.DA_REMOTE_PROVIDER) return process.env.DA_REMOTE_PROVIDER;
  if (process.env.DA_FAKE_DEVICES != null) return null;
  const p = readConfig().deviceRemoteProvider;
  return typeof p === 'string' && p ? p.replace(/^~(?=$|\/)/, os.homedir()) : null;
}

// The provider at `path`, loaded once and shared by concurrent callers. A lease is
// always handled by the provider that granted it, even after the setting has moved on.
async function provider(path) {
  let entry = instances.get(path);
  if (!entry) {
    entry = { provider: null, error: null };
    entry.ready = import(pathToFileURL(path).href)
      .then((mod) => { entry.provider = mod.default({ log, run, adb: ADB_BIN, stateDir: BASE_DIR, atomicWrite, matchesRequirements, androidApi, wsock }); })
      .catch((e) => { entry.error = String(e.message || e); log('remote: provider failed to load', path, entry.error); })
      .finally(() => onChange?.());
    instances.set(path, entry);
  }
  await entry.ready;
  if (entry.error) throw new Error(`the remote pool provider (${path}) failed to load: ${entry.error}`);
  return entry.provider;
}

// Loads the configured provider ahead of the first request, so the snapshot shows it.
export async function load() {
  const path = configuredPath();
  if (path) try { await provider(path); } catch {}
}

export function status() {
  const path = configuredPath();
  if (!path) return { provider: null, state: 'off' };
  const entry = instances.get(path);
  if (!entry) { load(); return { provider: null, state: 'loading' }; }
  if (entry.error) return { provider: null, state: 'error', reason: entry.error };
  if (!entry.provider) return { provider: null, state: 'loading' };
  try { return { provider: entry.provider.name, ...entry.provider.status() }; }
  catch (e) { return { provider: entry.provider.name, state: 'error', reason: String(e.message || e) }; }
}

// Like the provider's acquire, but bounded: a provider stuck past the budget (a sign-in
// nobody finishes, a hung network) costs the agent that long and no more, and a lease
// that lands after the budget is handed straight back.
// A granted lease comes back with `provider`, the module path every later call for it takes.
export async function acquire(req, exclude) {
  const path = configuredPath();
  if (!path) return { applies: false, reason: 'no remote device pool is configured' };
  let p;
  try { p = await provider(path); } catch (e) { return { applies: true, reason: e.message }; }
  const work = Promise.resolve().then(() => p.acquire(req, exclude))
    .catch((e) => ({ applies: true, reason: `${p.name} failed: ${e.message || e}` }));
  let timer;
  const late = new Promise((r) => {
    timer = setTimeout(() => r(null), REMOTE_ACQUIRE_BUDGET_MS);
  });
  const got = await Promise.race([work, late]);
  clearTimeout(timer);
  if (got) return got.lease ? { ...got, provider: path } : got;
  work.then((g) => { if (g.lease) release(path, g.lease); });
  return { applies: true, reason: `${p.name} did not answer within ${Math.round(REMOTE_ACQUIRE_BUDGET_MS / 1000)} s` };
}

// Bounded like acquire: past the budget it fails, and an attach that finishes later is
// released again, since the caller's release may have come before it opened anything.
export async function attach(path, lease, onEnd, port = 0) {
  const p = await provider(path);
  const work = Promise.resolve().then(() => p.attach(lease, onEnd, port));
  let timer;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => {
      work.then(() => release(path, lease), () => {});
      reject(new Error(`not attached within ${Math.round(REMOTE_ATTACH_BUDGET_MS / 1000)} s`));
    }, REMOTE_ATTACH_BUDGET_MS);
  });
  try { return await Promise.race([work, late]); } finally { clearTimeout(timer); }
}

// Releases the pool has not confirmed are kept in REMOTE_RELEASES_PATH and retried, by
// this daemon and the next, until it does; a lease left with its pool stays out of
// everyone's reach until it expires. Past a day one is dropped rather than retried forever.
const RELEASE_GIVE_UP_MS = 24 * 60 * 60 * 1000;
const inFlight = new Map(); // JSON of {path, lease} -> {path, lease}
const idOf = (path, lease) => JSON.stringify({ path, lease });

function unconfirmed() {
  try {
    const v = JSON.parse(fs.readFileSync(REMOTE_RELEASES_PATH, 'utf8'));
    return Array.isArray(v?.releases) ? v.releases.filter((x) => x?.path && x.lease) : [];
  } catch { return []; }
}

function saveUnconfirmed(list) {
  try { atomicWrite(REMOTE_RELEASES_PATH, { version: 1, releases: list }, 0o600); }
  catch (e) { log('remote: could not record unconfirmed releases', String(e)); }
}

// Gives a lease back; resolves whether the pool confirmed it, never throws.
export async function release(path, lease) {
  const id = idOf(path, lease);
  inFlight.set(id, { path, lease });
  let ok = false;
  try { await (await provider(path)).release(lease); ok = true; }
  catch (e) { log('remote: release not confirmed, will retry', String(e.message || e)); }
  finally { inFlight.delete(id); }
  const list = unconfirmed();
  const had = list.find((x) => idOf(x.path, x.lease) === id);
  if (ok && had) saveUnconfirmed(list.filter((x) => x !== had));
  if (!ok && !had) saveUnconfirmed([...list, { path, lease, since: Date.now() }]);
  return ok;
}

// Records the releases still running, for the next daemon: called on the way out.
export function recordInFlight() {
  const list = unconfirmed();
  const ids = new Set(list.map((x) => idOf(x.path, x.lease)));
  const add = [...inFlight.entries()].filter(([id]) => !ids.has(id)).map(([, x]) => ({ ...x, since: Date.now() }));
  if (add.length) saveUnconfirmed([...list, ...add]);
}

let retrying = false;
async function retryUnconfirmed() {
  if (retrying) return;
  retrying = true;
  try {
    for (const x of unconfirmed()) {
      if (inFlight.has(idOf(x.path, x.lease))) continue;
      if (Date.now() - (x.since || 0) > RELEASE_GIVE_UP_MS) {
        log('remote: giving up on an unconfirmed release', x.path, JSON.stringify(x.lease));
        saveUnconfirmed(unconfirmed().filter((y) => idOf(y.path, y.lease) !== idOf(x.path, x.lease)));
        continue;
      }
      await release(x.path, x.lease);
    }
  } finally { retrying = false; }
}

// Retries the unconfirmed releases now and every REMOTE_RELEASE_RETRY_MS.
export function startRetrying() {
  retryUnconfirmed();
  setInterval(retryUnconfirmed, REMOTE_RELEASE_RETRY_MS).unref();
}

export async function signIn() {
  const path = configuredPath();
  const p = path && await provider(path);
  if (!p) return { ok: false, reason: 'no remote device pool is configured' };
  if (!p.signIn) return { ok: false, reason: `${p.name} has no sign-in` };
  const reason = await p.signIn();
  return reason ? { ok: false, reason } : { ok: true };
}

export async function devices() {
  const path = configuredPath();
  const p = path && await provider(path);
  if (!p) return { reason: 'no remote device pool is configured' };
  if (!p.devices) return { reason: `${p.name} does not list its devices` };
  return p.devices();
}
