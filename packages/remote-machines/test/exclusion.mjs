// The operator's exclusions against a daemon whose own scans are far apart, so nothing
// here can pass on the back of a periodic scan: a machine let back in is read at once,
// and a probe already out when its machine is excluded for good does not bring it back.
// Run: node test/exclusion.mjs

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { sandbox } from './lib/sandbox.mjs';

const sb = sandbox({
  hosts: {
    quick: { hostname: 'quick.local', overrides: { machine_id: 'M-QUICK' } },
    slow: { hostname: 'slow.local', overrides: { machine_id: 'M-SLOW' } },
  },
  sshConfig: 'Host quick slow\n',
  known: ['quick.local', 'slow.local'],
});
Object.assign(process.env, sb.env, {
  RM_PROBE_INTERVAL_MS: '600000', RM_DISCOVERY_INTERVAL_MS: '600000', RM_CONFIG_POLL_MS: '100',
});
const { callDaemon, spawnDaemon } = await import('../src/ipc.js');
const settings = (obj) => fs.writeFileSync(sb.env.DIPLOMAT_CONFIG, JSON.stringify(obj));
const targets = (list) => list.map((m) => m.target).sort();
async function until(ok, what, ms = 3000) {
  const deadline = Date.now() + ms;
  let st;
  while (Date.now() < deadline) {
    st = await callDaemon('GET', '/state');
    if (ok(st)) return st;
    await delay(50);
  }
  assert.fail(`${what}: machines ${targets(st.machines)}, excluded ${targets(st.excluded)}`);
}

let checks = 0;
const check = async (name, fn) => { await fn(); checks++; console.log(`  PASS ${name}`); };
const session = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });

try {
  settings({ remoteMachineExclude: ['quick'] });
  await spawnDaemon(12000);
  await callDaemon('POST', '/attach', { ownerPid: session.pid });
  const s = await callDaemon('GET', '/state');

  await check('a machine excluded from the start is listed but never read', () => {
    assert.deepEqual(targets(s.machines), ['slow']);
    assert.deepEqual(s.excluded.map((m) => [m.target, m.status, m.excluded]), [['quick', 'excluded', { until: null }]]);
    assert.ok(!sb.probed().includes('quick'));
  });

  await check('a machine let back in is read at once, not at the next scan', async () => {
    settings({});
    const st = await until((x) => x.machines.some((m) => m.target === 'quick'), 'quick back');
    assert.deepEqual(st.excluded, []);
  });

  await check('a probe out when its machine is excluded for good does not bring it back', async () => {
    const h = JSON.parse(fs.readFileSync(sb.env.FAKE_SSH_HOSTS, 'utf8'));
    h.slow.delayMs = 1500;
    sb.setHosts(h);
    const scanning = callDaemon('POST', '/scan', {});
    await delay(500); // the slow probe is out
    settings({ remoteMachineExclude: ['slow'] });
    await until((x) => x.excluded.some((m) => m.target === 'slow'), 'slow excluded');
    const st = await scanning;
    assert.deepEqual(st.excluded.map((m) => [m.target, m.status]), [['slow', 'excluded']]);
    assert.deepEqual(targets(st.machines), ['quick']);
  });

  console.log(`EXCLUSION OK — ${checks} checks passed`);
} finally {
  session.kill('SIGKILL');
  sb.cleanup();
}
