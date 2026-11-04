// Tiny HTTP-over-unix-socket client. The daemon listens on a unix socket
// (filesystem-permission secured, no port/token to manage); every MCP server
// instance and the installer start it and talk to it through these helpers.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { SOCKET_PATH, BASE_DIR, LOG_PATH } from './paths.js';

const DAEMON_JS = path.join(path.dirname(fileURLToPath(import.meta.url)), 'daemon.js');

// {ok, pid} from the daemon serving the socket, or null when none answers. Clients
// key liveness on this, not on daemon.json: a full disk can refuse that file to a
// daemon that is serving, and a crash leaves it naming one that is not.
export async function daemonHealth(timeout = 2000) {
  try { return await callDaemon('GET', '/health', null, { timeout }); } catch { return null; }
}

export function callDaemon(method, route, body, opts = {}) {
  const socketPath = opts.socketPath || SOCKET_PATH;
  const timeout = opts.timeout ?? 180000;
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request(
      {
        socketPath,
        path: route,
        method,
        headers: {
          'content-type': 'application/json',
          ...(data ? { 'content-length': Buffer.byteLength(data) } : {}),
        },
        timeout,
      },
      (res) => {
        let buf = '';
        res.setEncoding('utf8');
        res.on('data', (c) => (buf += c));
        res.on('end', () => {
          let parsed;
          try { parsed = buf ? JSON.parse(buf) : {}; } catch { parsed = { raw: buf }; }
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(parsed);
          else reject(Object.assign(new Error(parsed?.error || `daemon ${res.statusCode}`), {
            statusCode: res.statusCode, body: parsed,
          }));
        });
      },
    );
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('daemon request timeout')));
    if (data) req.write(data);
    req.end();
  });
}

// Starts a daemon and waits for it to serve. Throws when it exits first, with its exit
// status and the reason it sent over the IPC channel (a full disk loses its log line),
// or when it is not serving within budgetMs. One that exits 0 deferred to another
// daemon, which is waited for in its place.
export async function spawnDaemon(budgetMs) {
  try { fs.mkdirSync(BASE_DIR, { recursive: true }); } catch {}
  let out = 'ignore';
  try { out = fs.openSync(LOG_PATH, 'a'); } catch {}
  let child;
  try {
    child = spawn(process.execPath, [DAEMON_JS], { detached: true, stdio: ['ignore', out, out, 'ipc'] });
  } finally {
    // The child holds its own copy; a client outlives many spawns.
    if (out !== 'ignore') fs.closeSync(out);
  }
  child.unref();
  let reason = '';
  child.on('message', (m) => { reason = String(m); });
  const failed = new Promise((resolve) => {
    child.once('error', (e) => resolve(e.message));
    child.once('exit', (code, signal) => { if (code !== 0) resolve(signal ? `signal ${signal}` : `exit ${code}`); });
  });
  try {
    const deadline = Date.now() + budgetMs;
    while (Date.now() < deadline) {
      const status = await Promise.race([delay(200), failed]);
      if (status) throw new Error(`device-allocator daemon failed to start (${status}): ${reason || `see ${LOG_PATH}`}`);
      if (await daemonHealth(1000)) return;
    }
  } finally {
    // An open channel would keep this process alive.
    if (child.connected) child.disconnect();
  }
  throw new Error('device-allocator daemon failed to start');
}
