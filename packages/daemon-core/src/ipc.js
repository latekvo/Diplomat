// HTTP over a unix socket, the client side of every daemon here. The socket is
// secured by filesystem permissions, so there is no port or token to manage; MCP
// servers and installers start a daemon and talk to it through one of these.

import http from 'node:http';
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

/// `label` names the daemon in errors ("device-allocator daemon failed to start").
export function createDaemonClient({ socketPath, daemonJs, baseDir, logPath, label }) {
  function callDaemon(method, route, body, opts = {}) {
    const timeout = opts.timeout ?? 180000;
    return new Promise((resolve, reject) => {
      const data = body ? JSON.stringify(body) : null;
      const req = http.request(
        {
          socketPath: opts.socketPath || socketPath,
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

  // {ok, pid} from the daemon serving the socket, or null when none answers. Clients
  // key liveness on this, not on daemon.json: a full disk can refuse that file to a
  // daemon that is serving, and a crash leaves it naming one that is not.
  async function daemonHealth(timeout = 2000) {
    try { return await callDaemon('GET', '/health', null, { timeout }); } catch { return null; }
  }

  // Starts a daemon and waits for it to serve. Throws when it exits first, with its exit
  // status and the reason it sent over the IPC channel (a full disk loses its log line),
  // or when it is not serving within budgetMs. One that exits 0 deferred to another
  // daemon, which is waited for in its place.
  async function spawnDaemon(budgetMs) {
    try { fs.mkdirSync(baseDir, { recursive: true }); } catch {}
    let out = 'ignore';
    try { out = fs.openSync(logPath, 'a'); } catch {}
    let child;
    try {
      child = spawn(process.execPath, [daemonJs], { detached: true, stdio: ['ignore', out, out, 'ipc'] });
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
        if (status) throw new Error(`${label} daemon failed to start (${status}): ${reason || `see ${logPath}`}`);
        if (await daemonHealth(1000)) return;
      }
    } finally {
      // An open channel would keep this process alive.
      if (child.connected) child.disconnect();
    }
    throw new Error(`${label} daemon failed to start`);
  }

  return { callDaemon, daemonHealth, spawnDaemon };
}
