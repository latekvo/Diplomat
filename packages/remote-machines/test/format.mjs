// What an agent reads: the commands it is told to run must work as printed. A machine
// on a non-22 port gets explicit `ssh -p` / `rsync -e` forms (rsync cannot parse an
// ssh:// target), a changed host key is not reported as a never-accepted one, and the
// header points at a login shell.
// Run: node test/format.mjs

import assert from 'node:assert/strict';
import { formatSnapshot } from '../src/format.js';

const now = Date.parse('2026-11-09T12:00:00Z');
const machine = (over) => ({
  status: 'ok', machineName: 'm', home: '/home/b', cores: 8, spareCores: 6,
  load: { last1m: 2, last5m: 2, last15m: 2 }, memory: { totalBytes: 2 ** 34, availableBytes: 2 ** 33 },
  os: { name: 'Linux', arch: 'x86_64' }, tools: { node: '/usr/bin/node' }, probedAt: '2026-11-09T11:59:50Z',
  user: 'b', sources: ['mdns'], ...over,
});
const text = formatSnapshot({
  updatedAt: '2026-11-09T11:59:50Z', discoveredAt: '2026-11-09T11:58:00Z', probeIntervalMs: 60000,
  local: machine({ target: null }),
  machines: [
    machine({ target: 'ssh://box.local:2222', hostname: 'box.local', port: 2222 }),
    machine({ target: 'alpha', hostname: 'alpha.local', port: 22, sources: ['ssh-config'] }),
  ],
  unavailable: [
    { target: 'moved', status: 'key-changed', sources: ['ssh-config'] },
    { target: 'stranger', status: 'untrusted', sources: ['mdns'] },
  ],
}, now);

let checks = 0;
const check = (name, fn) => { fn(); checks++; console.log(`  PASS ${name}`); };

check('a non-22 port is spelled out for ssh and rsync', () => {
  assert.ok(text.includes("port 2222: use `ssh -p 2222 box.local` and `rsync -e 'ssh -p 2222' ... box.local:offload/<task>/`"), text);
  // Only for that machine: a plain target needs nothing extra.
  assert.equal(text.match(/^ {3}port \d+: /gm).length, 1, text);
});

check('a changed host key is told apart from a never-accepted one', () => {
  assert.match(text, /host key changed since it was accepted \(ask its owner before trusting it again\): moved/);
  assert.match(text, /host key never accepted: stranger/);
});

check('the header points at a login shell', () => {
  assert.ok(text.includes("`ssh <target> '$SHELL -l -s' <<'EOF'`"), text);
});

console.log(`FORMAT OK — ${checks} checks passed`);
