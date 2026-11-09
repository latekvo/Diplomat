// The daemon over its real unix socket, against fake hosts: one machine reached under two
// names is listed once, failures are classified, an untrusted host is never contacted,
// machines are ranked by spare cores with warned ones last, a refresh re-reads the load,
// refreshes arriving together share a scan,
// the snapshot reaches state.json, and the daemon exits once its last session is gone.
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
  idle: { hostname: 'idle.local', overrides: { ...healthy, machine_id: 'M-IDLE', loadavg: '0.50 0.50 0.50' } },
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
});
const { callDaemon, spawnDaemon, daemonHealth } = await import('../src/ipc.js');

let checks = 0;
const check = async (name, fn) => { await fn(); checks++; console.log(`  PASS ${name}`); };
// A stand-in agent session: the daemon serves it while this process lives.
const session = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });

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
    assert.equal(m.cores, 8);
    assert.deepEqual(m.load, { last1m: 0.5, last5m: 0.5, last15m: 0.5, sinceLastScan: null });
    assert.ok(m.os?.name && m.tools && m.capabilitiesAt && m.probedAt);
    assert.deepEqual(m.sources, ['ssh-config', 'known-hosts']);
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
