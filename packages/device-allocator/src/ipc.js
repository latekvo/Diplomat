// Tiny HTTP-over-unix-socket client. The daemon listens on a unix socket
// (filesystem-permission secured, no port/token to manage); every MCP server
// instance and the installer talk to it through this helper.

import http from 'node:http';
import { SOCKET_PATH } from './paths.js';

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
