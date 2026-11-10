// The operator's exclusions against a daemon whose own scans are far apart, so nothing
// here can pass on the back of a periodic scan: a machine let back in is read at once, a
// probe already out when its machine is excluded for good does not bring it back, a pause
// that runs out ends on time, a paused machine that stops answering stays listed as
// paused, a pause holds every name of its machine, and an exclusion for good holds every
// name across a restart until known_hosts accepts a different machine at one of them.
// Run: node test/exclusion.mjs

import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { sandbox } from './lib/sandbox.mjs';

const sb = sandbox({
  hosts: {
    quick: { hostname: 'quick.local', overrides: { machine_id: 'M-QUICK' } },
    'quick-ip': { hostname: '192.168.1.9', overrides: { machine_id: 'M-QUICK' } },
    slow: { hostname: 'slow.local', overrides: { machine_id: 'M-SLOW' } },
    flaky: { hostname: 'flaky.local', overrides: { machine_id: 'M-FLAKY' } },
  },
  sshConfig: 'Host quick quick-ip slow flaky\n',
  known: ['quick.local', '192.168.1.9', 'slow.local', 'flaky.local'],
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
    assert.deepEqual(targets(s.machines), ['flaky', 'quick-ip', 'slow']);
    assert.deepEqual(s.excluded.map((m) => [m.target, m.status, m.excluded]), [['quick', 'excluded', { until: null, by: ['quick'] }]]);
    assert.ok(!sb.probed().includes('quick'));
  });

  await check('a machine let back in is read at once, not at the next scan', async () => {
    settings({});
    const st = await until((x) => x.machines.some((m) => m.target === 'quick'), 'quick back');
    assert.deepEqual(st.excluded, []);
    assert.ok(st.machines.find((m) => m.target === 'quick').aliases.includes('quick-ip'), 'one machine, two names');
  });

  await check('a pause that runs out ends on time, not at the next scan', async () => {
    settings({ remoteMachineExcludeUntil: { flaky: new Date(Date.now() + 1200).toISOString() } });
    await until((x) => x.excluded.some((m) => m.target === 'flaky'), 'flaky paused');
    await until((x) => x.machines.some((m) => m.target === 'flaky'), 'flaky back', 3000);
  });

  await check('a paused machine that stops answering is still listed as paused', async () => {
    const until1h = new Date(Date.now() + 3600e3).toISOString();
    settings({ remoteMachineExcludeUntil: { flaky: until1h } });
    const h = JSON.parse(fs.readFileSync(sb.env.FAKE_SSH_HOSTS, 'utf8'));
    h.flaky.behavior = 'refused';
    sb.setHosts(h);
    const st = await callDaemon('POST', '/scan', { full: true });
    assert.deepEqual(st.excluded.map((m) => [m.target, m.status, m.excluded]), [['flaky', 'unreachable', { until: until1h, by: ['flaky'] }]]);
    assert.ok(!st.unavailable.some((m) => m.target === 'flaky'));
    delete h.flaky.behavior;
    sb.setHosts(h);
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

  await check('a pause under one name holds the machine under its others when that one stops answering', async () => {
    const until1h = new Date(Date.now() + 3600e3).toISOString();
    settings({ remoteMachineExcludeUntil: { quick: until1h } });
    const h = JSON.parse(fs.readFileSync(sb.env.FAKE_SSH_HOSTS, 'utf8'));
    h.quick.behavior = 'refused';
    sb.setHosts(h);
    await until((x) => x.excluded.some((m) => m.id === 'M-QUICK'), 'quick paused');
    const st = await callDaemon('POST', '/scan', { full: true });
    assert.ok(!st.machines.some((m) => m.id === 'M-QUICK'), JSON.stringify(st.machines.map((m) => m.target)));
    const paused = st.excluded.filter((m) => m.id === 'M-QUICK');
    assert.deepEqual(paused.map((m) => [m.target, m.status, m.excluded.by]), [['quick-ip', 'ok', ['quick']]]);
    delete h.quick.behavior;
    sb.setHosts(h);
    settings({});
    await until((x) => x.machines.some((m) => m.id === 'M-QUICK'), 'quick back');
  });

  await check('an exclusion for good holds every name of its machine across a restart', async () => {
    settings({ remoteMachineExclude: ['quick'] });
    await until((x) => x.excluded.some((m) => m.target === 'quick'), 'quick excluded');
    const { pid } = JSON.parse(fs.readFileSync(`${sb.env.RM_BASE_DIR}/daemon.json`, 'utf8'));
    process.kill(pid, 'SIGTERM');
    while (sb.daemons().length) await delay(50);
    const before = sb.probed().length;
    await spawnDaemon(12000);
    await callDaemon('POST', '/attach', { ownerPid: session.pid });
    const st = await callDaemon('GET', '/state');
    const since = sb.probed().slice(before);
    assert.ok(!since.includes('quick') && !since.includes('quick-ip'), since.join(' '));
    assert.deepEqual(st.excluded.map((m) => [m.id, m.target, m.excluded.by]), [['M-QUICK', 'quick', ['quick']]]);
    assert.ok(st.excluded[0].aliases.includes('quick-ip'));
    assert.ok(!st.machines.some((m) => m.id === 'M-QUICK'));
  });

  await check('a name known_hosts now holds another key for is no longer counted as the excluded machine', async () => {
    // The address went to another machine, and its new key was accepted.
    execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', `${sb.dir}/otherkey`]);
    const pub = fs.readFileSync(`${sb.dir}/otherkey.pub`, 'utf8').trim().split(' ').slice(0, 2).join(' ');
    const known = fs.readFileSync(sb.env.FAKE_KNOWN_HOSTS, 'utf8').split('\n')
      .map((l) => (l.startsWith('192.168.1.9 ') ? `192.168.1.9 ${pub}` : l)).join('\n');
    fs.writeFileSync(sb.env.FAKE_KNOWN_HOSTS, known);
    const h = JSON.parse(fs.readFileSync(sb.env.FAKE_SSH_HOSTS, 'utf8'));
    h['quick-ip'].overrides.machine_id = 'M-OTHER';
    sb.setHosts(h);
    const { pid } = JSON.parse(fs.readFileSync(`${sb.env.RM_BASE_DIR}/daemon.json`, 'utf8'));
    process.kill(pid, 'SIGTERM');
    while (sb.daemons().length) await delay(50);
    await spawnDaemon(12000);
    await callDaemon('POST', '/attach', { ownerPid: session.pid });
    const st = await callDaemon('GET', '/state');
    assert.deepEqual(st.machines.filter((m) => m.target === 'quick-ip').map((m) => m.id), ['M-OTHER']);
    assert.deepEqual(st.excluded.map((m) => [m.id, m.target]), [['M-QUICK', 'quick']]);
  });

  console.log(`EXCLUSION OK — ${checks} checks passed`);
} finally {
  session.kill('SIGKILL');
  sb.cleanup();
}
