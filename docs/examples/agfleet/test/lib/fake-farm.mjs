// A stand-in for the Argent Fleet farm: the REST API, the lease-watch and tunnel
// sockets (the tunnel echoes what it is sent, in place of a phone's adb), and the
// browser sign-in, which redirects straight back with a token as a signed-in user would.

import http from 'node:http';
import crypto from 'node:crypto';
import { ALLOCATOR } from './allocator.mjs';

const { encodeFrame, frameParser, OP } = await import(`${ALLOCATOR}/src/wsock.js`);

export const API_TOKEN = 'a'.repeat(64);

export function startFarm(devices) {
  const farm = {
    devices: devices.map((d) => ({ host_id: 'h1', lease_id: null, ...d })),
    leases: new Map(), // id -> {device, token, watches: Set<socket>, tunnels: Set<socket>}
    requests: [], // "METHOD /path" per REST call
    refuseLease: new Set(), // device ids whose next lease is refused with 409, as if taken meanwhile
    token: API_TOKEN,
  };

  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      farm.requests.push(`${req.method} ${req.url.split('?')[0]}`);
      const send = (code, body) => {
        res.writeHead(code, { 'content-type': 'application/json' });
        res.end(body == null ? '' : JSON.stringify(body));
      };
      const u = new URL(req.url, 'http://x');
      if (u.pathname === '/auth/login') {
        res.writeHead(302, { location: `http://127.0.0.1:${u.searchParams.get('port')}/?token=${farm.token}` });
        res.end();
        return;
      }
      if (req.headers.authorization !== `Bearer ${farm.token}`) { send(401, { error: 'bad api token' }); return; }
      if (req.method === 'GET' && u.pathname === '/v1/devices') { send(200, { devices: farm.devices }); return; }
      if (req.method === 'POST' && u.pathname === '/v1/leases') {
        const { device, ttl_s: ttl } = JSON.parse(raw || '{}');
        const d = farm.devices.find((x) => x.id === device);
        if (!d) { send(404, { error: 'unknown device' }); return; }
        if (farm.refuseLease.delete(device) || d.state !== 'ready') { send(409, { error: 'device is leased' }); return; }
        const id = crypto.randomUUID();
        const token = crypto.randomBytes(16).toString('hex');
        d.state = 'leased';
        d.lease_id = id;
        farm.leases.set(id, { device: d, token, ttl, watches: new Set(), tunnels: new Set() });
        const now = Math.floor(Date.now() / 1000);
        send(200, { lease: { id, token, device: d.id, platform: d.platform, created_at: now, expires_at: now + ttl } });
        return;
      }
      const m = /^\/v1\/leases\/([^/]+)$/.exec(u.pathname);
      if (req.method === 'DELETE' && m) {
        if (!farm.leases.has(m[1])) { send(404, { error: 'unknown lease' }); return; }
        farm.end(m[1], 'released');
        send(204, null);
        return;
      }
      send(404, { error: 'not found' });
    });
  });

  server.on('upgrade', (req, socket) => {
    const lease = [...farm.leases.values()].find((l) => req.headers.authorization === `Bearer ${l.token}`);
    if (!lease) {
      const body = JSON.stringify({ error: 'no such lease' });
      socket.end(`HTTP/1.1 404 Not Found\r\ncontent-type: application/json\r\ncontent-length: ${body.length}\r\n\r\n${body}`);
      return;
    }
    const accept = crypto.createHash('sha1')
      .update(`${req.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    socket.on('error', () => {});
    const set = req.url === '/v1/lease/watch' ? lease.watches : lease.tunnels;
    set.add(socket);
    socket.on('close', () => set.delete(socket));
    socket.write(encodeFrame(OP.TEXT, Buffer.from('OK android-adb')));
    socket.on('data', frameParser((op, payload) => {
      if (op === OP.TEXT && payload.toString() === 'ping') {
        if (!farm.mute) socket.write(encodeFrame(OP.TEXT, Buffer.from('pong')));
      } else if (op === OP.BINARY) socket.write(encodeFrame(OP.BINARY, payload));
      else if (op === OP.CLOSE) socket.end();
    }));
  });

  // Ends a lease the way the farm does: a 1000 close carrying the reason on every socket.
  farm.end = (id, reason) => {
    const l = farm.leases.get(id);
    if (!l) return;
    farm.leases.delete(id);
    l.device.state = 'ready';
    l.device.lease_id = null;
    const close = encodeFrame(OP.CLOSE, Buffer.concat([Buffer.from([0x03, 0xe8]), Buffer.from(reason)]));
    for (const s of [...l.watches, ...l.tunnels]) { s.write(close); s.end(); }
  };
  // Cuts the watch sockets without a close frame, as a network drop would.
  farm.dropWatches = () => { for (const l of farm.leases.values()) for (const s of l.watches) s.destroy(); };
  farm.leaseFor = (deviceId) => [...farm.leases.entries()].find(([, l]) => l.device.id === deviceId)?.[0];

  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => {
    farm.url = `http://127.0.0.1:${server.address().port}`;
    farm.close = () => { server.closeAllConnections(); server.close(); };
    resolve(farm);
  }));
}
