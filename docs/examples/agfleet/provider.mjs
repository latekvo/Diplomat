// Argent Fleet as a remote device pool for the Diplomat device allocator, which loads this
// file from its `deviceRemoteProvider` setting (packages/device-allocator/src/remote.js has
// the contract). It calls the farm API the `agfleet` CLI uses
// (https://agfleet.swmtest.xyz/llms.txt) directly:
//
//   GET    /v1/devices                 {devices: [{id, platform, model, os, state, ...}]}
//   POST   /v1/leases {device, ttl_s}  {lease: {id, token, expires_at}}; 404/409 when it can't be had
//   DELETE /v1/leases/<id>             204, or 404 once it is gone
//   WS     /v1/lease/watch             "OK <transport>", then "ping"/"pong"; closes with the end reason
//   WS     /v1/tunnel                  "OK <transport>", then the phone's raw adb transport;
//                                      one socket per local TCP connection
//   GET    /auth/login?port=N          browser sign-in, ending at http://127.0.0.1:N/?token=<api token>
//
// The REST calls carry the api token as a bearer, the two sockets the lease token.
// Only Android is leased: a farm iPhone speaks usbmuxd, which argent can drive only once
// usbfluxd (root) has replaced the system usbmuxd.

import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const FLEET_DEFAULT_URL = 'https://agfleet.swmtest.xyz';
// The CLI's own session file, so one sign-in serves both.
const FLEET_SESSION_PATH = path.join(os.homedir(), '.config', 'agfleet', 'session.json');
const FLEET_HTTP_TIMEOUT_MS = Number(process.env.DA_FLEET_HTTP_TIMEOUT_MS) || 8000;
// How long a request waits on a browser sign-in (below the allocator's 11 min budget), and
// how long an unanswered one keeps the prompt from reopening: the user is taken to be away.
const FLEET_LOGIN_TIMEOUT_MS = Number(process.env.DA_FLEET_LOGIN_TIMEOUT_MS) || 10 * 60 * 1000;
const FLEET_AFK_MS = Number(process.env.DA_FLEET_AFK_MS) || 2 * 60 * 60 * 1000;
const FLEET_LEASE_TTL_S = 4 * 60 * 60; // the farm's maximum
// How long a lease whose watch socket dropped is retried before it is given up as lost.
const FLEET_RECONNECT_MS = Number(process.env.DA_FLEET_RECONNECT_MS) || 2 * 60 * 1000;
const FLEET_PING_MS = Number(process.env.DA_FLEET_PING_MS) || 15 * 1000;

let log, run, ADB_BIN, atomicWrite, matchesRequirements, androidApi, wsock, FLEET_STATE_PATH;

const TRANSPORT = 'OK android-adb';
const END_REASONS = {
  expired: 'the lease reached its 4 h limit',
  device_disconnected: 'the phone dropped off its host',
  host_disconnected: "the phone's host went offline",
};

const attached = new Map(); // lease id -> {watch, server, sockets, serial}
let signIn = null; // the browser sign-in in flight: {loginUrl, done}
let afkUntil = 0;

export default function create(ctx) {
  ({ log, run, adb: ADB_BIN, atomicWrite, matchesRequirements, androidApi, wsock } = ctx);
  FLEET_STATE_PATH = path.join(ctx.stateDir, 'agfleet.json');
  try { afkUntil = Number(JSON.parse(fs.readFileSync(FLEET_STATE_PATH, 'utf8')).afkUntil) || 0; } catch {}
  return { name: 'Argent Fleet', status, acquire, attach, release, signIn: retrySignIn, devices };
}

function setAfk(until) {
  afkUntil = until;
  try { atomicWrite(FLEET_STATE_PATH, { afkUntil }, 0o600); } catch (e) { log('fleet: state write failed', String(e)); }
}

function session() {
  let file = {};
  try { file = JSON.parse(fs.readFileSync(FLEET_SESSION_PATH, 'utf8')) || {}; } catch {}
  return {
    url: (process.env.AGFLEET_URL || file.farm || FLEET_DEFAULT_URL).replace(/\/+$/, ''),
    token: process.env.AGFLEET_TOKEN || file.token || null,
    fromEnv: Boolean(process.env.AGFLEET_TOKEN),
  };
}

function status() {
  const s = session();
  if (signIn) return { state: 'signing-in', url: s.url, loginUrl: signIn.loginUrl };
  if (afkUntil > Date.now()) return { state: 'sign-in-unanswered', url: s.url, retryAt: new Date(afkUntil).toISOString() };
  return { state: s.token ? 'on' : 'signed-out', url: s.url };
}

const clock = (ms) => new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

// Never throws: an unreachable farm comes back as {offline, reason}.
async function call(s, method, route, body) {
  try {
    const res = await fetch(`${s.url}${route}`, {
      method,
      signal: AbortSignal.timeout(FLEET_HTTP_TIMEOUT_MS),
      headers: { authorization: `Bearer ${s.token}`, ...(body ? { 'content-type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = {};
    try { data = JSON.parse(await res.text()) || {}; } catch {}
    return { status: res.status, data };
  } catch (e) {
    return { offline: true, reason: `Argent Fleet (${s.url}) is unreachable: ${e.cause?.code || e.cause?.message || e.name}` };
  }
}

const farmError = (r) => r.reason || `Argent Fleet answered ${r.status}${r.data.error ? ` (${r.data.error})` : ''}`;

function toDevice(d) {
  const version = String(d.os || '').replace(/^\D+/, '') || null; // "Android 16" -> "16"
  return {
    key: `fleet:${d.id}`, fleetId: d.id, platform: d.platform, state: d.state,
    name: `${d.model || d.id} (Argent Fleet)`, version, apiVersion: androidApi(version), format: 'phone',
  };
}

async function acquire(req, exclude) {
  let s = session();
  if (!['android', 'any'].includes(req.platform) || req.format === 'tablet') {
    return { applies: false, reason: 'Argent Fleet lends Android phones only' };
  }
  const why = (reason) => ({ applies: true, reason });
  if (!(await run(ADB_BIN, ['version'])).ok) return why(`adb (${ADB_BIN}) is not available`);

  let list = s.token ? await call(s, 'GET', '/v1/devices') : { status: 401, data: {} };
  if (list.status === 401) {
    if (s.fromEnv) return why('the farm rejected AGFLEET_TOKEN');
    const failed = await ensureSignedIn(s.url);
    if (failed) return why(failed);
    s = session();
    list = await call(s, 'GET', '/v1/devices');
  }
  if (list.status !== 200) return why(farmError(list));

  const want = { ...req, platform: 'android' };
  const free = (list.data.devices || []).map(toDevice).filter((d) => d.state === 'ready'
    && d.platform === 'android' && !exclude.has(d.key) && matchesRequirements(d, want));
  if (!free.length) {
    return why(`no ${req.version !== 'any' ? `Android ${req.version} ` : 'Android '}phone is free on Argent Fleet`);
  }
  for (const d of free) {
    const r = await call(s, 'POST', '/v1/leases', { device: d.fleetId, ttl_s: FLEET_LEASE_TTL_S });
    if (r.offline) return why(r.reason);
    const l = r.data.lease;
    if (r.status === 200 && l?.token) {
      return { device: d, lease: { id: l.id, token: l.token, url: s.url, expiresAt: l.expires_at * 1000 } };
    }
    // Taken since the listing, gone offline, its host down: the next one may still do.
    log('fleet: lease refused', d.fleetId, farmError(r));
  }
  return why('every matching Argent Fleet phone was taken before it could be leased');
}

// The panel's "sign in": prompts now, even while the user is marked away.
function retrySignIn() {
  setAfk(0);
  return ensureSignedIn(session().url);
}

// What the farm lists, for the panel and the snapshot's free listing; never prompts a
// sign-in. A ready phone of a kind acquire does not lease is 'not-lent', so it is never
// shown free.
async function devices() {
  const s = session();
  if (!s.token) return { reason: 'signed out of Argent Fleet' };
  const r = await call(s, 'GET', '/v1/devices');
  if (r.status !== 200) return { reason: farmError(r) };
  return { devices: (r.data.devices || []).map(toDevice)
    .map((d) => (d.state === 'ready' && d.platform !== 'android' ? { ...d, state: 'not-lent' } : d)) };
}

// null once signed in, else why not. Concurrent requests share one browser prompt.
function ensureSignedIn(url) {
  if (afkUntil > Date.now()) {
    return Promise.resolve(`signed out of Argent Fleet, and the last sign-in prompt went unanswered; `
      + `it is not reopened before ${clock(afkUntil)} (\`agfleet login ${url}\` signs in sooner)`);
  }
  if (!signIn) {
    signIn = { loginUrl: null };
    signIn.done = browserSignIn(url, signIn).finally(() => { signIn = null; });
  }
  return signIn.done;
}

async function browserSignIn(url, pending) {
  let gotToken;
  const token = new Promise((r) => { gotToken = r; });
  const server = http.createServer((req, res) => {
    const t = new URL(req.url, 'http://127.0.0.1').searchParams.get('token');
    if (!t || !/^[\w-]{32,}$/.test(t)) { res.writeHead(400).end(); return; }
    res.writeHead(200, { 'content-type': 'text/html', connection: 'close' });
    res.end('<p style="font:16px system-ui;margin:3rem">Signed in to Argent Fleet. You can close this tab.</p>');
    gotToken(t);
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  pending.loginUrl = `${url}/auth/login?port=${server.address().port}`;
  const away = (what) => {
    setAfk(Date.now() + FLEET_AFK_MS);
    log('fleet: sign-in abandoned', what);
    return `${what}; taken as away, so the prompt is not reopened before ${clock(afkUntil)}`;
  };
  const timer = new AbortController();
  try {
    const opener = process.env.DA_FLEET_OPEN_CMD || (process.platform === 'darwin' ? 'open' : 'xdg-open');
    if (!(await run(opener, [pending.loginUrl], { timeout: 15000 })).ok) {
      return away('no browser could be opened for the Argent Fleet sign-in');
    }
    log('fleet: sign-in opened', pending.loginUrl);
    const t = await Promise.race([
      token, delay(FLEET_LOGIN_TIMEOUT_MS, null, { signal: timer.signal }).catch(() => null)]);
    if (!t) {
      const wait = FLEET_LOGIN_TIMEOUT_MS >= 60000 ? `${Math.round(FLEET_LOGIN_TIMEOUT_MS / 60000)} min` : `${Math.round(FLEET_LOGIN_TIMEOUT_MS / 1000)} s`;
      return away(`nobody finished the Argent Fleet sign-in within ${wait}`);
    }
    atomicWrite(FLEET_SESSION_PATH, { farm: url, token: t }, 0o600);
    setAfk(0);
    log('fleet: signed in', url);
    return null;
  } finally {
    timer.abort();
    server.close();
    server.closeAllConnections();
  }
}

// ---- the lease's local end ------------------------------------------------

const auth = (lease) => ({ authorization: `Bearer ${lease.token}` });

function firstText(ws, ms = 10000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => { ws.close(); reject(new Error('the farm never confirmed the phone')); }, ms);
    ws.once('text', (s) => { clearTimeout(t); resolve(s); });
    ws.once('close', () => { clearTimeout(t); reject(new Error('the farm closed the lease socket')); });
  });
}

// Listens on `port` (a rehydrated lease keeps its adb serial) or, when that is taken, any port.
function listen(server, port) {
  return new Promise((resolve, reject) => {
    const retry = (e) => (port ? listen(server, 0).then(resolve, reject) : reject(e));
    server.once('error', retry);
    server.listen(port, '127.0.0.1', () => { server.off('error', retry); resolve(); });
  });
}

// Opens the lease's tunnel on a local port and connects adb to it; resolves the adb
// serial. onEnd(reason) fires once if the farm ends the lease (expiry, phone or host lost).
async function attach(lease, onEnd, port = 0) {
  const watch = await wsock.connect(`${lease.url}/v1/lease/watch`, auth(lease));
  const hello = await firstText(watch);
  if (hello !== TRANSPORT) { watch.close(); throw new Error(`unsupported phone transport "${hello}"`); }
  const att = { watch, sockets: new Set(), serial: null };
  att.server = net.createServer((sock) => tunnel(lease, att, sock));
  await listen(att.server, port);
  att.serial = `127.0.0.1:${att.server.address().port}`;
  attached.set(lease.id, att);
  if (lease.released) { teardown(lease.id); throw new Error('released while attaching'); }
  watchLease(lease, att, onEnd);
  await run(ADB_BIN, ['connect', att.serial], { timeout: 30000 });
  const up = await run(ADB_BIN, ['-s', att.serial, 'wait-for-device'], { timeout: 60000 });
  if (lease.released) throw new Error('released while attaching');
  if (!up.ok) { teardown(lease.id); throw new Error(`adb never saw ${att.serial} come up`); }
  return att.serial;
}

function tunnel(lease, att, sock) {
  att.sockets.add(sock);
  sock.pause(); // until the farm confirms the phone, so no adb bytes are sent into the void
  sock.on('error', () => {});
  sock.on('close', () => att.sockets.delete(sock));
  wsock.connect(`${lease.url}/v1/tunnel`, auth(lease)).then((ws) => {
    if (sock.destroyed) { ws.close(); return; }
    ws.once('text', (t) => (t === TRANSPORT ? sock.resume() : ws.close()));
    ws.on('binary', (b) => sock.write(b));
    ws.on('close', () => sock.destroy());
    sock.on('data', (b) => ws.send(b));
    sock.on('close', () => ws.close());
  }, () => sock.destroy());
}

function watchLease(lease, att, onEnd) {
  let lastPong = Date.now();
  const ping = setInterval(() => {
    // A farm that stops answering is a dead socket, whatever TCP thinks.
    if (Date.now() - lastPong > 3 * FLEET_PING_MS) att.watch.close(4000);
    else att.watch.send('ping');
  }, FLEET_PING_MS);
  att.watch.on('text', (t) => { if (t === 'pong') lastPong = Date.now(); });
  att.watch.once('close', async (code, reason) => {
    clearInterval(ping);
    if (lease.released) return;
    const ended = (why) => {
      log('fleet: lease ended', lease.id, why);
      teardown(lease.id);
      onEnd(END_REASONS[why] || why);
    };
    if (code === 1000 && reason) { ended(reason); return; }
    // The socket dropped, not necessarily the lease: reconnect until the farm says it is
    // over, or give it up once the budget is spent.
    const deadline = Date.now() + FLEET_RECONNECT_MS;
    while (Date.now() < deadline) {
      await delay(Math.min(5000, FLEET_RECONNECT_MS / 4));
      if (lease.released) return;
      try {
        const w = await wsock.connect(`${lease.url}/v1/lease/watch`, auth(lease));
        if (await firstText(w) === TRANSPORT && !lease.released) {
          att.watch = w;
          watchLease(lease, att, onEnd);
          return;
        }
        w.close();
      } catch (e) {
        if (e.status) { ended(e.message); return; }
      }
    }
    if (!lease.released) ended('lost contact with the farm');
  });
}

function teardown(id) {
  const att = attached.get(id);
  if (!att) return;
  attached.delete(id);
  att.watch.close();
  att.server.close();
  for (const s of att.sockets) s.destroy();
  if (att.serial) run(ADB_BIN, ['disconnect', att.serial]);
}

// Gives the phone back (the farm resets it for the next user) and drops the tunnel.
async function release(lease) {
  lease.released = true;
  teardown(lease.id);
  const r = await call({ ...session(), url: lease.url }, 'DELETE', `/v1/leases/${lease.id}`);
  // 404: the farm no longer knows the lease, which is as good as given back.
  if (r.status !== 204 && r.status !== 404) throw new Error(farmError(r));
}
