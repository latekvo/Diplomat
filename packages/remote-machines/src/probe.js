// Runs probe.sh on a machine (over ssh, or locally for this one) and turns its
// key=value lines into a machine description.

import fs from 'node:fs';
import { run, sshConfigArgs } from './discovery.js';
import { SSH_BIN, PROBE_TIMEOUT_MS } from './paths.js';

const SCRIPT = fs.readFileSync(new URL('./probe.sh', import.meta.url), 'utf8');

// Unattended, and never trusting a new or changed host key whatever the user's config
// says (see discovery.js). A 3 s connect is ample on a LAN and keeps a stale address
// from holding up a scan. No ControlMaster: a master this daemon started would outlive
// it by ControlPersist.
const SSH_OPTS = [
  '-T',
  '-o', 'BatchMode=yes',
  '-o', 'StrictHostKeyChecking=yes',
  '-o', 'ConnectTimeout=3',
  '-o', 'ServerAliveInterval=5',
  '-o', 'ServerAliveCountMax=2',
  '-o', 'ControlMaster=no',
  // Nor riding a master the user's own ssh left open, whose session the probe's would be.
  '-o', 'ControlPath=none',
  '-o', 'ClearAllForwardings=yes',
];

/// Why ssh failed, from its stderr: unreachable, auth-failed, untrusted, key-changed or error.
export function classify(stderr, timedOut) {
  if (timedOut) return { status: 'unreachable', error: `no answer within ${PROBE_TIMEOUT_MS / 1000} s` };
  const last = stderr.trim().split('\n').filter(Boolean).pop() || 'ssh failed';
  if (/Permission denied|Too many authentication failures/.test(stderr)) return { status: 'auth-failed', error: last };
  if (/IDENTIFICATION HAS CHANGED/i.test(stderr)) return { status: 'key-changed', error: last };
  if (/Host key verification failed|host key is known/i.test(stderr)) {
    return { status: 'untrusted', error: last };
  }
  if (/Could not resolve|Connection refused|timed out|No route to host|Network is unreachable|Connection closed|Connection reset/i.test(stderr)) {
    return { status: 'unreachable', error: last };
  }
  return { status: 'error', error: last };
}

/// {ok: true, fields, latencyMs} or {ok: false, status, error, timedOut}. `target` null probes
/// this machine. `mode` is 'full' (capabilities and load) or 'load'.
export async function probe(target, mode) {
  const started = Date.now();
  const [bin, args] = target == null
    ? ['sh', ['-s', '--', mode]]
    : [SSH_BIN, [...sshConfigArgs(), ...SSH_OPTS, '--', target, `sh -s -- ${mode} ssh`]];
  const r = await run(bin, args, { timeout: PROBE_TIMEOUT_MS, input: SCRIPT });
  const i = r.stdout.indexOf('__RM_BEGIN__\n');
  const j = r.stdout.indexOf('__RM_END__', i);
  if (i === -1 || j === -1) return { ok: false, timedOut: r.code === null, ...classify(r.stderr, r.code === null) };
  const fields = Object.create(null); // keys come from the remote: `__proto__=` is just a key
  for (const line of r.stdout.slice(i + 13, j).split('\n')) {
    const k = line.indexOf('=');
    if (k > 0) (fields[line.slice(0, k)] ??= []).push(line.slice(k + 1).trim());
  }
  return { ok: true, fields, latencyMs: Date.now() - started };
}

const num = (v) => (v == null || v === '' || Number.isNaN(Number(v)) ? null : Number(v));
const one = (f, k) => f[k]?.[0] || null; // a probe prints a key with nothing after it when it could not tell

// "total = 6144.00M  used = 4660.44M  free = 1483.56M  (encrypted)"
function macSwap(s) {
  const mb = (k) => num(new RegExp(`${k} = ([\\d.]+)M`).exec(s || '')?.[1]);
  const total = mb('total');
  const used = mb('used');
  return total == null ? null : { totalBytes: Math.round(total * 2 ** 20), usedBytes: Math.round((used ?? 0) * 2 ** 20) };
}

/// The probe's fields as {load, memory, swap, disk, gpus, bootTime, machineId, ...}.
/// Capability keys (os, cpu, tools, ...) are present only after a `full` probe.
export function describe(f) {
  const darwin = one(f, 'os') === 'Darwin';
  const [l1, l5, l15] = (one(f, 'loadavg') || '').trim().split(/\s+/).map(num);
  const [diskTotalKb, diskFreeKb] = (one(f, 'disk_kb') || '').split(' ').map(num);
  let memory;
  let swap;
  if (darwin) {
    const total = num(one(f, 'mem_bytes'));
    const freePct = num(one(f, 'mem_free_pct'));
    memory = {
      totalBytes: total,
      availableBytes: total != null && freePct != null ? Math.round((total * freePct) / 100) : null,
      pressure: { 1: 'normal', 2: 'warning', 4: 'critical' }[one(f, 'mem_pressure')] ?? null,
    };
    swap = macSwap(one(f, 'swap'));
  } else {
    const kb = (k) => { const v = num(one(f, `meminfo_${k}`)); return v == null ? null : v * 1024; };
    memory = { totalBytes: kb('MemTotal'), availableBytes: kb('MemAvailable'), pressure: null };
    const swapTotal = kb('SwapTotal');
    swap = swapTotal == null ? null : { totalBytes: swapTotal, usedBytes: swapTotal - (kb('SwapFree') ?? 0) };
  }
  const nvidia = (f.nvidia || []).map((line) => {
    const [name, totalMb, usedMb, util] = line.split(',').map((s) => s.trim());
    return { name, vramBytes: num(totalMb) * 2 ** 20, vramUsedBytes: num(usedMb) * 2 ** 20, utilizationPct: num(util) };
  });
  const out = {
    machineName: one(f, 'hostname'),
    home: one(f, 'home'),
    machineId: one(f, 'machine_id'),
    bootTime: num(one(f, 'boottime')),
    cores: num(one(f, 'ncpu')),
    sshSessions: num(one(f, 'ssh_sessions')),
    load: { last1m: l1 ?? null, last5m: l5 ?? null, last15m: l15 ?? null },
    memory,
    swap,
    disk: diskFreeKb == null ? null : { totalBytes: diskTotalKb * 1024, freeBytes: diskFreeKb * 1024 },
    ...(nvidia.length ? { gpus: nvidia } : {}),
  };
  if (!f.arch) return out;
  return {
    ...out,
    os: { name: one(f, 'os_version') || one(f, 'os'), kernel: one(f, 'kernel'), arch: one(f, 'arch') },
    cpu: { model: one(f, 'cpu'), perfCores: num(one(f, 'perf_cores')) },
    gpus: nvidia.length ? nvidia : (f.gpu || []).map((name) => ({ name })),
    tools: Object.fromEntries((f.tool || []).map((t) => { const s = t.indexOf(' '); return [t.slice(0, s), t.slice(s + 1)]; })),
    versions: {
      ...(one(f, 'node_version') ? { node: one(f, 'node_version') } : {}),
      ...(one(f, 'xcode') ? { xcode: one(f, 'xcode') } : {}),
    },
  };
}

/// The mean run queue between two readings of the 5-minute load average `dt` seconds
/// apart. That average is an exponential moving average of the run queue (time
/// constant 300 s on macOS and Linux alike), so L5(t) = L5(t0)·e^(-dt/300) +
/// (1 - e^(-dt/300))·n, where n is the run queue over [t0, t] weighted near-uniformly
/// for dt of a few minutes or less. Solving for n needs nothing on the machine but
/// the two readings.
export function meanLoadBetween(prevL5, l5, dt) {
  const decay = Math.exp(-dt / 300);
  return Math.max(0, Math.round(((l5 - prevL5 * decay) / (1 - decay)) * 100) / 100);
}

// ---- ranking ----------------------------------------------------------------

const GB = 2 ** 30;

/// What makes this machine a poor place to run something right now.
export function warnings({ info, load }) {
  const out = [];
  const now = load?.last1m;
  if (info.cores && now != null && now / info.cores > 1) out.push(`overloaded: load ${now} on ${info.cores} cores`);
  const mem = info.memory;
  if (mem?.pressure === 'warning' || mem?.pressure === 'critical') out.push(`memory pressure ${mem.pressure}`);
  else if (mem?.totalBytes && mem.availableBytes != null && mem.availableBytes / mem.totalBytes < 0.1) {
    out.push(`low memory: ${(mem.availableBytes / GB).toFixed(1)} GB available`);
  }
  // Only without a pressure reading (Linux): macOS grows swap a file at a time and keeps
  // it, so its used/total stays high on a machine whose pressure is normal.
  const swap = info.swap;
  if (mem?.pressure == null && swap?.totalBytes && swap.usedBytes / swap.totalBytes > 0.75) {
    out.push(`swap ${Math.round((100 * swap.usedBytes) / swap.totalBytes)}% used`);
  }
  if (info.disk && info.disk.freeBytes < 20 * GB) out.push(`low disk: ${(info.disk.freeBytes / GB).toFixed(0)} GB free`);
  return out;
}

/// Cores left over once the current load (the freshest reading, the 1-minute one) is served.
/// Machines are ranked by this.
export function spareCores({ info, load }) {
  const used = load?.last1m;
  return info.cores == null || used == null ? null : Math.round(Math.max(0, info.cores - used) * 10) / 10;
}

const frac = (v) => Math.round(v * 1000) / 1000;

/// The readings the ranking and the warnings judge a machine by, as fractions: `cpu` is
/// the 1-minute load per core (above 1 when overloaded), `memory` the share of RAM not
/// available.
export function usage({ info, load }) {
  const mem = info.memory;
  return {
    cpu: info.cores && load?.last1m != null ? frac(load.last1m / info.cores) : null,
    memory: mem?.totalBytes && mem.availableBytes != null ? frac(1 - mem.availableBytes / mem.totalBytes) : null,
  };
}
