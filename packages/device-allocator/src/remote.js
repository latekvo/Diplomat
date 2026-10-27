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
//   release(lease)                gives it back; never throws
//   signIn()          (optional)  re-prompts a sign-in; null when signed in, else why not
//   devices()         (optional)  {devices} the pool lists now, or {reason}

import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { ADB_BIN, BASE_DIR, REMOTE_ACQUIRE_BUDGET_MS } from './paths.js';
import { atomicWrite, readConfig } from './state.js';
import { run, matchesRequirements, androidApi } from './devices.js';
import { log } from './log.js';
import * as wsock from './wsock.js';

let loaded = { path: null, provider: null, error: null };

function providerPath() {
  if (process.env.DA_REMOTE_PROVIDER) return process.env.DA_REMOTE_PROVIDER;
  if (process.env.DA_FAKE_DEVICES != null) return null;
  const p = readConfig().deviceRemoteProvider;
  return typeof p === 'string' && p ? p.replace(/^~(?=$|\/)/, os.homedir()) : null;
}

// The configured provider, loaded once per path; null when none is configured.
async function provider() {
  const p = providerPath();
  if (!p) return null;
  if (loaded.path !== p) {
    loaded = { path: p, provider: null, error: null };
    try {
      const mod = await import(pathToFileURL(p).href);
      loaded.provider = mod.default({ log, run, adb: ADB_BIN, stateDir: BASE_DIR, atomicWrite, matchesRequirements, androidApi, wsock });
    } catch (e) {
      loaded.error = String(e.message || e);
      log('remote: provider failed to load', p, loaded.error);
    }
  }
  if (loaded.error) throw new Error(`the remote pool provider (${p}) failed to load: ${loaded.error}`);
  return loaded.provider;
}

// Loads the configured provider ahead of the first request, so the snapshot shows it.
export async function load() {
  try { await provider(); } catch {}
}

export function status() {
  if (!providerPath()) return { provider: null, state: 'off' };
  if (loaded.error) return { provider: null, state: 'error', reason: loaded.error };
  if (!loaded.provider) return { provider: null, state: 'loading' };
  try { return { provider: loaded.provider.name, ...loaded.provider.status() }; }
  catch (e) { return { provider: loaded.provider.name, state: 'error', reason: String(e.message || e) }; }
}

// Like the provider's acquire, but bounded: a provider stuck past the budget (a sign-in
// nobody finishes, a hung network) costs the agent that long and no more, and a lease
// that lands after the budget is handed straight back.
export async function acquire(req, exclude) {
  let p;
  try { p = await provider(); } catch (e) { return { applies: true, reason: e.message }; }
  if (!p) return { applies: false, reason: 'no remote device pool is configured' };
  const work = Promise.resolve().then(() => p.acquire(req, exclude))
    .catch((e) => ({ applies: true, reason: `${p.name} failed: ${e.message || e}` }));
  let timer;
  const late = new Promise((r) => {
    timer = setTimeout(() => r(null), REMOTE_ACQUIRE_BUDGET_MS);
  });
  const got = await Promise.race([work, late]);
  clearTimeout(timer);
  if (got) return got;
  work.then((g) => { if (g.lease) p.release(g.lease); });
  return { applies: true, reason: `${p.name} did not answer within ${Math.round(REMOTE_ACQUIRE_BUDGET_MS / 1000)} s` };
}

export async function attach(lease, onEnd, port = 0) {
  const p = await provider();
  if (!p) throw new Error('no remote device pool is configured any more');
  return p.attach(lease, onEnd, port);
}

export async function release(lease) {
  try { await (await provider())?.release(lease); } catch (e) { log('remote: release failed', String(e)); }
}

export async function signIn() {
  const p = await provider();
  if (!p) return { ok: false, reason: 'no remote device pool is configured' };
  if (!p.signIn) return { ok: false, reason: `${p.name} has no sign-in` };
  const reason = await p.signIn();
  return reason ? { ok: false, reason } : { ok: true };
}

export async function devices() {
  const p = await provider();
  if (!p) return { reason: 'no remote device pool is configured' };
  if (!p.devices) return { reason: `${p.name} does not list its devices` };
  return p.devices();
}
