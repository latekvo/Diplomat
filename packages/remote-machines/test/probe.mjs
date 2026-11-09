// The probe: probe.sh run for real on this machine (the Darwin branch on a Mac, the
// Linux one in CI), what describe() makes of each OS's fields, how ssh failures are
// told apart, that a hung probe is cut off at its timeout, what makes a machine a poor
// pick, and the load-since-last-scan estimate against a simulated kernel.
// Run: node test/probe.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { sandbox, inSandbox } from './lib/sandbox.mjs';

const HANG_S = 677; // a sleep this test can find again by its argument
const sb = sandbox({
  hosts: {
    hung: { hostname: 'hung.local', behavior: 'hang', sleep: HANG_S },
    odd: { hostname: 'odd.local', inject: ['__proto__=x', 'constructor=y', 'hasOwnProperty=z'] },
  },
});
Object.assign(process.env, sb.env);
const { probe, describe, classify, meanLoadBetween, warnings, spareCores } = await import('../src/probe.js');
const sleepers = () => {
  try { return execFileSync('pgrep', ['-f', `sleep ${HANG_S}`], { encoding: 'utf8' }).trim(); } catch { return ''; }
};

let checks = 0;
const check = async (name, fn) => { await fn(); checks++; console.log(`  PASS ${name}`); };

try {
  await check('probe.sh reads this machine', async () => {
    const r = await probe(null, 'full');
    assert.equal(r.ok, true, JSON.stringify(r));
    const m = describe(r.fields);
    assert.equal(m.cores, os.cpus().length);
    assert.equal(m.home, os.homedir());
    // A minimal Linux (a container) has no machine id at all.
    if (process.platform === 'darwin' || fs.existsSync('/etc/machine-id')) assert.ok(m.machineId, 'machine id');
    assert.ok(m.memory.totalBytes > 0 && m.memory.availableBytes > 0, JSON.stringify(m.memory));
    assert.ok(m.memory.availableBytes <= m.memory.totalBytes);
    assert.ok(m.disk.freeBytes > 0 && m.disk.freeBytes <= m.disk.totalBytes, JSON.stringify(m.disk));
    for (const k of ['last1m', 'last5m', 'last15m']) assert.equal(typeof m.load[k], 'number', k);
    assert.equal(m.os.arch, { arm64: process.platform === 'linux' ? 'aarch64' : 'arm64', x64: 'x86_64' }[os.arch()]);
    assert.ok(m.os.name, JSON.stringify(m.os));
    // ARM Linux under a hypervisor may name no CPU model anywhere.
    const named = process.platform === 'darwin' || /^model name/m.test(fs.readFileSync('/proc/cpuinfo', 'utf8'));
    if (named) assert.ok(m.cpu.model, JSON.stringify(m.cpu));
    assert.ok(m.tools.git, 'git is on every CI runner and dev machine');
    assert.ok(Math.abs(m.bootTime - (Date.now() / 1000 - os.uptime())) < 120, 'boot time');
  });

  await check('a load probe leaves capabilities out', async () => {
    const r = await probe(null, 'load');
    const m = describe(r.fields);
    assert.equal(m.os, undefined);
    assert.equal(m.tools, undefined);
    assert.equal(typeof m.load.last5m, 'number');
  });

  await check('Darwin fields', () => {
    const m = describe({
      os: ['Darwin'], loadavg: [' 2.30 2.59 2.89 '], ncpu: ['11'], mem_bytes: ['19327352832'],
      mem_free_pct: ['66'], mem_pressure: ['2'], swap: ['total = 6144.00M  used = 4660.44M  free = 1483.56M  (encrypted)'],
      disk_kb: ['482797652 14724464'], arch: ['arm64'], os_version: ['macOS 27.0'], cpu: ['Apple M3 Pro'],
      perf_cores: ['5'], gpu: ['Apple M3 Pro (14-core)'], tool: ['node /opt/homebrew/bin/node'], node_version: ['v26.7.0'],
      xcode: ['Xcode 27.1'],
    });
    assert.deepEqual(m.load, { last1m: 2.3, last5m: 2.59, last15m: 2.89 });
    assert.deepEqual(m.memory, { totalBytes: 19327352832, availableBytes: Math.round(19327352832 * 0.66), pressure: 'warning' });
    assert.deepEqual(m.swap, { totalBytes: 6144 * 2 ** 20, usedBytes: Math.round(4660.44 * 2 ** 20) });
    assert.deepEqual(m.disk, { totalBytes: 482797652 * 1024, freeBytes: 14724464 * 1024 });
    assert.deepEqual(m.gpus, [{ name: 'Apple M3 Pro (14-core)' }]);
    assert.deepEqual(m.tools, { node: '/opt/homebrew/bin/node' });
    assert.deepEqual(m.versions, { node: 'v26.7.0', xcode: 'Xcode 27.1' });
  });

  await check('Linux fields, NVIDIA GPUs included', () => {
    const m = describe({
      os: ['Linux'], loadavg: ['0.50 0.40 0.30'], ncpu: ['32'], meminfo_MemTotal: ['65536000'], meminfo_MemAvailable: ['32768000'],
      meminfo_SwapTotal: ['2048000'], meminfo_SwapFree: ['1024000'], disk_kb: ['1000 400'],
      nvidia: ['NVIDIA GeForce RTX 4090, 24564, 1024, 37'], arch: ['x86_64'], os_version: ['Ubuntu 24.04 LTS'], cpu: ['AMD Ryzen 9'],
    });
    assert.deepEqual(m.memory, { totalBytes: 65536000 * 1024, availableBytes: 32768000 * 1024, pressure: null });
    assert.deepEqual(m.swap, { totalBytes: 2048000 * 1024, usedBytes: 1024000 * 1024 });
    assert.deepEqual(m.gpus, [{ name: 'NVIDIA GeForce RTX 4090', vramBytes: 24564 * 2 ** 20, vramUsedBytes: 1024 * 2 ** 20, utilizationPct: 37 }]);
  });

  await check('ssh failures are told apart', () => {
    assert.equal(classify('me@box: Permission denied (publickey).\n', false).status, 'auth-failed');
    assert.equal(classify('Host key verification failed.\n', false).status, 'untrusted');
    assert.equal(classify('@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n', false).status, 'key-changed');
    assert.equal(classify('ssh: connect to host x port 22: Connection refused\n', false).status, 'unreachable');
    assert.equal(classify('ssh: Could not resolve hostname x: nodename nor servname provided\n', false).status, 'unreachable');
    assert.equal(classify('', true).status, 'unreachable');
    assert.deepEqual(classify('zsh: command not found: sh\n', false), { status: 'error', error: 'zsh: command not found: sh' });
  });

  await check('a hung probe returns at its timeout and leaves nothing running', async () => {
    const r = inSandbox({ ...sb.env, RM_PROBE_TIMEOUT_MS: '1000' }, `
      const { probe } = await import('./probe.js');
      const t = Date.now();
      const r = await probe('hung', 'load');
      console.log(JSON.stringify({ ms: Date.now() - t, status: r.status }));
      process.exit(0);`);
    assert.equal(r.status, 'unreachable');
    assert.ok(r.ms < 4000, `took ${r.ms} ms`);
    assert.equal(sleepers(), '', 'the probe left its process group running');
  });

  await check('probe output keys are data, whatever they are named', async () => {
    const r = await probe('odd', 'load');
    assert.equal(r.ok, true);
    assert.deepEqual(r.fields.__proto__, ['x']);
    assert.deepEqual(r.fields.constructor, ['y']);
    assert.ok(describe(r.fields).cores > 0);
  });

  await check('a machine is a poor pick for load, memory, swap without a pressure reading, and disk', () => {
    const GB = 2 ** 30;
    const base = { cores: 8, memory: { totalBytes: 16 * GB, availableBytes: 8 * GB, pressure: 'normal' }, disk: { freeBytes: 100 * GB } };
    const fullSwap = { totalBytes: 4 * GB, usedBytes: 3.9 * GB };
    const load = { last1m: 2 };
    assert.deepEqual(warnings({ info: base, load }), []);
    assert.deepEqual(warnings({ info: { ...base, swap: fullSwap }, load }), []);
    const linux = { ...base, memory: { ...base.memory, pressure: null }, swap: fullSwap };
    assert.deepEqual(warnings({ info: linux, load }), ['swap 98% used']);
    assert.deepEqual(warnings({ info: base, load: { last1m: 9 } }), ['overloaded: load 9 on 8 cores']);
    assert.deepEqual(warnings({ info: { ...base, memory: { ...base.memory, pressure: 'critical' } }, load }), ['memory pressure critical']);
    assert.deepEqual(warnings({ info: { ...base, disk: { freeBytes: 5 * GB } }, load }), ['low disk: 5 GB free']);
    assert.equal(spareCores({ info: base, load: { last1m: 2.04 } }), 6);
  });

  await check('load since the last scan matches a simulated kernel', () => {
    // The kernel's averages: one update every 5 s, decaying with time constants of
    // 60/300/900 s, read with two decimals. A deterministic run queue that swings.
    let l5 = 0;
    let t = 0;
    const queue = (s) => 6 + 5 * Math.sin(s / 40);
    const tick = () => { l5 = l5 * Math.exp(-5 / 300) + queue(t) * (1 - Math.exp(-5 / 300)); t += 5; };
    while (t < 3000) tick();
    for (let round = 0; round < 20; round++) {
      const before = Math.round(l5 * 100) / 100;
      let sum = 0;
      for (let i = 0; i < 12; i++) { sum += queue(t); tick(); }
      const truth = sum / 12;
      const est = meanLoadBetween(before, Math.round(l5 * 100) / 100, 60);
      assert.ok(Math.abs(est - truth) < 0.5, `round ${round}: estimated ${est}, truly ${truth.toFixed(2)}`);
    }
    assert.equal(meanLoadBetween(10, 0, 60), 0, 'never negative');
  });

  console.log(`PROBE OK — ${checks} checks passed`);
} finally {
  sb.cleanup();
}
