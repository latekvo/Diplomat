// The daemon over its real unix socket, against fake hosts: one machine reached under two
// names is listed once, failures are classified, an untrusted host is never contacted,
// machines are ranked by spare cores with warned ones last, a refresh re-reads the load,
// refreshes arriving together share a scan, each reading reaches history.json, the
// operator's exclusions take a machine out of the pool, the snapshot reaches state.json,
// and the daemon exits once its last session is gone.
// Run: node test/daemon.mjs

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { sandbox } from './lib/sandbox.mjs';

// Telling this machine apart takes a machine id, which a minimal Linux (a container) lacks.
const HAS_ID = process.platform === 'darwin' || fs.existsSync('/etc/machine-id');
const TiB = 1024 ** 3; // disk_kb counts KiB
// probe.sh runs on this machine under the fake ssh, so everything a warning reads is
// pinned rather than inherited from whatever this machine is going through. Only the
// keys of this machine's OS appear in its output; the others are never matched.
const healthy = {
  ncpu: '8', disk_kb: `${TiB} ${TiB / 2}`,
  mem_pressure: '1', mem_free_pct: '50', swap: 'total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)',
  meminfo_MemTotal: '16000000', meminfo_MemAvailable: '8000000', meminfo_SwapTotal: '0', meminfo_SwapFree: '0',
};
const hosts = (busyLoad) => ({
  busy: { hostname: 'busy.local', overrides: { ...healthy, machine_id: 'M-BUSY', loadavg: `${busyLoad} ${busyLoad} 1` } },
  idle: { hostname: 'idle.local', overrides: { ...healthy, machine_id: 'M-IDLE', loadavg: '0.50 0.50 0.50', ssh_sessions: '3' } },
  'idle-ip': { hostname: '192.168.1.7', overrides: { ...healthy, machine_id: 'M-IDLE', loadavg: '0.50 0.50 0.50' } },
  // The most spare cores, but nearly out of disk.
  full: { hostname: 'full.local', overrides: { ...healthy, machine_id: 'M-FULL', ncpu: '64', loadavg: '0 0 0', disk_kb: `${TiB} 1000` } },
  // No machine_id override: it reports this machine's own, as this machine's mDNS name would.
  ...(HAS_ID ? { mirror: { hostname: 'mirror.local', overrides: healthy } } : {}),
  locked: { hostname: 'locked.local', behavior: 'auth' },
  down: { hostname: 'down.local', behavior: 'refused' },
  stranger: { hostname: 'stranger.local' },
});

const sb = sandbox({
  hosts: hosts('6.00'),
  sshConfig: `Host ${Object.keys(hosts('6.00')).join(' ')}\n`,
  known: ['busy.local', 'idle.local', '192.168.1.7', 'full.local', ...(HAS_ID ? ['mirror.local'] : []), 'locked.local', 'down.local'],
});
Object.assign(process.env, sb.env, {
  RM_IDLE_EXIT_MS: '1500', RM_CLIENT_CHECK_MS: '200', RM_PROBE_INTERVAL_MS: '700', RM_LOAD_GAP_MIN_MS: '500',
  RM_CONFIG_POLL_MS: '100',
});
const settings = (obj) => fs.writeFileSync(sb.env.DIPLOMAT_CONFIG, JSON.stringify(obj));
const stateWhen = async (ok, what) => {
  const deadline = Date.now() + 8000;
  let st;
  while (Date.now() < deadline) {
    st = await callDaemon('GET', '/state');
    if (ok(st)) return st;
    await delay(50);
  }
  assert.fail(`${what}: ${JSON.stringify({ machines: st.machines.map((m) => m.target), excluded: st.excluded.map((m) => [m.target, m.excluded]) })}`);
};
const historyFile = () => JSON.parse(fs.readFileSync(path.join(sb.env.RM_BASE_DIR, 'history.json'), 'utf8'));
const { callDaemon, spawnDaemon, daemonHealth } = await import('../src/ipc.js');

let checks = 0;
const check = async (name, fn) => { await fn(); checks++; console.log(`  PASS ${name}`); };
// A stand-in agent session: the daemon serves it while this process lives.
const session = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });

// What an earlier daemon left: one row past the history's reach, one within it.
const earlier = Date.now() - 5000;
fs.mkdirSync(sb.env.RM_BASE_DIR, { recursive: true });
fs.writeFileSync(path.join(sb.env.RM_BASE_DIR, 'history.json'), JSON.stringify({
  version: 1, machines: { 'M-GONE': [[Date.now() - 25 * 3600e3, 0.5, 0.5, 0]], 'M-IDLE': [[earlier, 0.9, 0.9, 9]] },
}));

try {
  await spawnDaemon(12000);
  await callDaemon('POST', '/attach', { ownerPid: session.pid });
  const s = await callDaemon('GET', '/state');

  await check('this machine is read locally and listed apart', () => {
    assert.equal(s.local.status, 'ok');
    assert.equal(s.local.target, null);
    assert.ok(s.local.cores > 0 && s.local.os?.name);
  });

  if (HAS_ID) {
    await check('this machine reached over ssh is not listed as a remote one', () => {
      assert.ok(sb.probed().includes('mirror'), 'it is probed');
      assert.equal([...s.machines, ...s.unavailable].find((m) => m.target === 'mirror'), undefined);
    });
  }

  await check('one machine under two names is listed once, the rest by spare cores, warned last', () => {
    assert.deepEqual(s.machines.map((m) => m.target), ['idle', 'busy', 'full']);
    assert.deepEqual(s.machines[0].aliases, ['idle.local', 'idle-ip', '192.168.1.7']);
    assert.deepEqual(s.machines.map((m) => m.spareCores), [7.5, 2, 64]);
    assert.deepEqual(s.machines[2].warnings, ['low disk: 0 GB free']);
    assert.deepEqual(s.machines[0].warnings, []);
  });

  await check('a machine carries its capabilities and load', () => {
    const m = s.machines[0];
    assert.equal(m.machineId, 'M-IDLE');
    assert.equal(m.id, 'M-IDLE');
    assert.equal(m.cores, 8);
    assert.equal(m.sshSessions, 3);
    assert.equal(m.excluded, null);
    assert.deepEqual(m.load, { last1m: 0.5, last5m: 0.5, last15m: 0.5, sinceLastScan: null });
    assert.ok(m.os?.name && m.tools && m.capabilitiesAt && m.probedAt);
    assert.deepEqual(m.sources, ['ssh-config', 'known-hosts']);
  });

  await check('usage is the load per core and the share of RAM not available, as the ranking reads them', () => {
    assert.deepEqual(s.machines.map((m) => m.usage), [
      { cpu: 0.063, memory: 0.5 }, { cpu: 0.75, memory: 0.5 }, { cpu: 0, memory: 0.5 },
    ]);
    // 8 cores at load 6 leaves 2: spareCores is cores × (1 − cpu).
    assert.equal(s.machines[1].spareCores, 2);
  });

  await check('the snapshot names the daemon and the sessions it serves', () => {
    assert.ok(s.daemon.pid > 0 && Date.parse(s.daemon.startedAt));
    assert.equal(s.daemon.sessions, 1);
  });

  await check('failures are classified and an untrusted host is never contacted', () => {
    const why = Object.fromEntries(s.unavailable.map((u) => [u.target, u.status]));
    assert.deepEqual(why, { locked: 'auth-failed', down: 'unreachable', stranger: 'untrusted' });
    assert.ok(!sb.probed().includes('stranger'), sb.probed().join(' '));
  });

  await check('every probe is unattended and refuses a new or changed host key', () => {
    for (const args of sb.probes()) {
      for (const opt of ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'ControlMaster=no', 'ClearAllForwardings=yes']) {
        assert.ok(args.includes(opt), `${opt} missing from ${JSON.stringify(args)}`);
      }
      assert.equal(args.at(-3), '--', `the target can be read as an option: ${JSON.stringify(args)}`);
    }
  });

  await check('load since the last scan is filled in by the next probe and kept across a quick refresh', async () => {
    // Every fake idle machine reads a steady 0.50, so the run queue between two readings is 0.50.
    let m;
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      m = (await callDaemon('GET', '/state')).machines.find((x) => x.target === 'idle');
      if (m.load.sinceLastScan != null) break;
      await delay(100);
    }
    assert.equal(m.load.sinceLastScan, 0.5);
    // Inside the minimum gap: the measured value stays rather than going null.
    const r = await callDaemon('POST', '/scan', { ownerPid: session.pid });
    assert.equal(r.machines.find((x) => x.target === 'idle').load.sinceLastScan, 0.5);
  });

  await check('the snapshot reaches state.json', () => {
    const disk = JSON.parse(fs.readFileSync(path.join(sb.env.RM_BASE_DIR, 'state.json'), 'utf8'));
    assert.deepEqual(disk.machines.map((m) => m.target), ['idle', 'busy', 'full']);
  });

  await check('refreshes that arrive together share one scan rather than queueing', async () => {
    const before = sb.probed().filter((t) => t === 'idle').length;
    await Promise.all(Array.from({ length: 6 }, () => callDaemon('POST', '/scan', { ownerPid: session.pid, full: true })));
    // The one in flight plus the one they all joined; a periodic scan may slip in too.
    const scans = sb.probed().filter((t) => t === 'idle').length - before;
    assert.ok(scans >= 1 && scans <= 3, `${scans} scans for 6 refreshes`);
  });

  await check('each new reading goes into history.json once, keyed by machine id', async () => {
    const before = historyFile().machines['M-IDLE'];
    assert.deepEqual(before[0], [earlier, 0.9, 0.9, 9], 'an earlier daemon\'s rows are kept');
    assert.ok(before.length >= 2);
    for (const row of before.slice(1)) {
      assert.equal(row.length, 4);
      assert.deepEqual(row.slice(1), [0.063, 0.5, 3]);
    }
    const at = before.map((r) => r[0]);
    assert.deepEqual(at, [...new Set(at)].sort((a, b) => a - b), 'one row per reading, oldest first');
    await callDaemon('POST', '/scan', { full: true });
    const after = historyFile();
    assert.ok(after.machines['M-IDLE'].length > before.length);
    assert.equal(after.intervalMs, 700);
    // Unreachable and untrusted machines have no readings to keep, and a row older than a
    // day is dropped.
    assert.deepEqual(Object.keys(after.machines).sort(), ['M-BUSY', 'M-FULL', 'M-IDLE', s.local.id].sort());
  });

  await check('a scan asked for without a session attaches nothing', async () => {
    const r = await callDaemon('POST', '/scan', {});
    assert.equal(r.daemon.sessions, 1);
  });

  await check('a machine excluded for a while leaves the pool but is still read, under all its names', async () => {
    const until = new Date(Date.now() + 3600e3).toISOString();
    // Excluded under one of its names only.
    settings({ remoteMachineExcludeUntil: { 'idle-ip': until, busy: new Date(Date.now() - 1000).toISOString() } });
    const st = await stateWhen((x) => x.excluded.length === 1, 'idle excluded');
    assert.deepEqual(st.machines.map((m) => m.target), ['busy', 'full'], 'an exclusion that has run out counts for nothing');
    assert.equal(st.excluded[0].target, 'idle');
    assert.deepEqual(st.excluded[0].excluded, { until });
    const probes = sb.probed().filter((t) => t === 'idle').length;
    const again = await callDaemon('POST', '/scan', { full: true });
    assert.ok(sb.probed().filter((t) => t === 'idle').length > probes, 'still read');
    assert.equal(again.excluded[0].usage.cpu, 0.063);
  });

  await check('a machine excluded for good is not contacted until it is let back in', async () => {
    settings({ remoteMachineExclude: ['IDLE.local'] });
    const st = await stateWhen((x) => x.excluded.length === 1 && x.excluded[0].status === 'excluded', 'idle excluded for good');
    assert.deepEqual(st.excluded.map((m) => [m.target, m.excluded]), [['idle', { until: null }]]);
    assert.deepEqual(st.machines.map((m) => m.target), ['busy', 'full']);
    // Its other name reaches the same machine id: it is not listed in its place.
    assert.ok(!st.unavailable.some((m) => m.target === 'idle-ip'));
    const probes = sb.probed().filter((t) => t === 'idle' || t === 'idle-ip').length;
    await callDaemon('POST', '/scan', { full: true });
    await callDaemon('POST', '/scan', { full: true });
    assert.equal(sb.probed().filter((t) => t === 'idle' || t === 'idle-ip').length, probes);
    settings({});
    const back = await stateWhen((x) => x.machines.some((m) => m.target === 'idle'), 'idle back');
    assert.deepEqual(back.excluded, []);
    assert.equal(back.machines.find((m) => m.target === 'idle').status, 'ok');
  });

  await check('a refresh re-reads the load and re-ranks', async () => {
    sb.setHosts(hosts('0.10'));
    const r = await callDaemon('POST', '/scan', { ownerPid: session.pid, full: true });
    assert.deepEqual(r.machines.map((m) => m.target), ['busy', 'idle', 'full']);
    assert.equal(r.machines[0].load.last1m, 0.1);
  });

  await check('the daemon exits once its last session is gone, taking its socket along', async () => {
    await delay(2000);
    assert.ok(await daemonHealth(), 'it must stay up while a session lives');
    session.kill('SIGKILL');
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline && sb.daemons().length) await delay(100);
    assert.deepEqual(sb.daemons(), []);
    assert.equal(fs.existsSync(path.join(sb.env.RM_BASE_DIR, 'daemon.sock')), false);
  });

  console.log(`DAEMON OK — ${checks} checks passed`);
} finally {
  session.kill('SIGKILL');
  sb.cleanup();
}
