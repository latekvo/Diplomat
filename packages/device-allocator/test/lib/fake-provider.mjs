// A remote device pool provider (src/remote.js) for tests, steered through files in
// FAKE_POOL_DIR:
//   devices.json   [{key, platform, name, version, apiVersion, format, free}] the pool lends
//   mode           'ok' | 'throw' | 'slow' (leases after FAKE_POOL_SLOW_MS) | 'attach-fails'
//                  | 'slow-attach' (attaches after FAKE_POOL_SLOW_MS)
//                  | 'slow-attach-strict' (the same, but fails if released meanwhile)
//   end-<lease id> written by a test to end that lease; its content is the reason
//   events.log     what the provider did, one line each: "lease <key>", "release <key>", ...
// An attached device is a local TCP port that echoes, standing in for an adb transport.

import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

const DIR = process.env.FAKE_POOL_DIR;
const read = (f, dflt) => { try { return fs.readFileSync(path.join(DIR, f), 'utf8'); } catch { return dflt; } };
const event = (line) => fs.appendFileSync(path.join(DIR, 'events.log'), `${line}\n`);
const servers = new Map(); // lease id -> server
const released = new Set(); // lease ids

export default function create(ctx) {
  let n = 0;
  return {
    name: 'Test Pool',
    status: () => ({ state: 'on' }),
    async acquire(req, exclude) {
      const mode = read('mode', 'ok').trim();
      if (mode === 'throw') throw new Error('boom');
      if (mode === 'slow') await new Promise((r) => setTimeout(r, Number(process.env.FAKE_POOL_SLOW_MS)));
      if (req.platform !== 'android' && req.platform !== 'any') return { applies: false, reason: 'android only' };
      const devices = JSON.parse(read('devices.json', '[]'));
      const d = devices.find((x) => x.free && !exclude.has(x.key) && ctx.matchesRequirements(x, { ...req, platform: 'android' }));
      if (!d) return { applies: true, reason: 'nothing free in the test pool' };
      d.free = false;
      fs.writeFileSync(path.join(DIR, 'devices.json'), JSON.stringify(devices));
      const lease = { id: `L${process.pid}-${++n}`, key: d.key };
      event(`lease ${d.key}`);
      return { device: d, lease };
    },
    async attach(lease, onEnd, port) {
      const mode = read('mode', 'ok').trim();
      if (mode === 'attach-fails') throw new Error('tunnel refused');
      if (mode.startsWith('slow-attach')) await new Promise((r) => setTimeout(r, Number(process.env.FAKE_POOL_SLOW_MS)));
      if (mode === 'slow-attach-strict' && released.has(lease.id)) throw new Error('released while attaching');
      const server = net.createServer((s) => { s.on('error', () => {}); s.pipe(s); });
      await new Promise((resolve, reject) => {
        server.once('error', (e) => (port ? server.listen(0, '127.0.0.1', resolve) : reject(e)));
        server.listen(port, '127.0.0.1', resolve);
      });
      servers.set(lease.id, server);
      event(`attach ${lease.key}${port ? ` port ${port}` : ''}`);
      const poll = setInterval(() => {
        const why = read(`end-${lease.id}`, null);
        if (why == null || !servers.has(lease.id)) { if (!servers.has(lease.id)) clearInterval(poll); return; }
        clearInterval(poll);
        servers.get(lease.id).close();
        servers.delete(lease.id);
        onEnd(why);
      }, 50);
      return `127.0.0.1:${server.address().port}`;
    },
    async release(lease) {
      released.add(lease.id);
      servers.get(lease.id)?.close();
      servers.delete(lease.id);
      const devices = JSON.parse(read('devices.json', '[]'));
      for (const d of devices) if (d.key === lease.key) d.free = true;
      fs.writeFileSync(path.join(DIR, 'devices.json'), JSON.stringify(devices));
      event(`release ${lease.key}`);
    },
    async devices() { return { devices: JSON.parse(read('devices.json', '[]')) }; },
  };
}
