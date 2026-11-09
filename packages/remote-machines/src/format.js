// The snapshot as an agent reads it: one block per machine, best first.

const GB = 2 ** 30;
const gb = (b) => (b == null ? '?' : `${(b / GB).toFixed(b < 10 * GB ? 1 : 0)} GB`);

function ago(iso, now) {
  if (!iso) return 'never';
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  return s < 120 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

function hardware(m) {
  const cpu = m.cpu?.model ? `${m.cpu.model}, ${m.cores} cores${m.cpu.perfCores ? ` (${m.cpu.perfCores} performance)` : ''}` : `${m.cores} cores`;
  const gpu = m.gpus?.length ? ` · GPU ${m.gpus.map((g) => g.name + (g.vramBytes ? `, ${gb(g.vramBytes)} VRAM` : '')).join('; ')}` : '';
  return `${m.os?.name ?? '?'} ${m.os?.arch ?? ''} · ${cpu}${gpu} · ${gb(m.memory?.totalBytes)} RAM`;
}

function loadLine(m) {
  const l = m.load ?? {};
  const since = l.sinceLastScan != null ? `, ${l.sinceLastScan} since the last scan` : '';
  const perCore = m.cores && l.last5m != null ? ` (${(l.last5m / m.cores).toFixed(2)} per core over 5 min)` : '';
  const mem = m.memory?.availableBytes != null ? `${gb(m.memory.availableBytes)} RAM available` : 'RAM ?';
  const swap = m.swap?.totalBytes ? ` · swap ${gb(m.swap.usedBytes)} of ${gb(m.swap.totalBytes)} used` : '';
  const disk = m.disk ? ` · ${gb(m.disk.freeBytes)} disk free` : '';
  const gpuUse = (m.gpus ?? []).filter((g) => g.utilizationPct != null).map((g) => ` · GPU ${g.utilizationPct}% busy`).join('');
  return `load ${l.last1m} now, ${l.last5m} over 5 min${since}${perCore} · ${m.spareCores} spare cores · ${mem}${swap}${disk}${gpuUse}`;
}

function tools(m) {
  const names = Object.keys(m.tools ?? {}).map((t) => (t === 'node' && m.versions?.node ? `node ${m.versions.node}` : t));
  if (m.versions?.xcode) names.push(m.versions.xcode);
  return names.length ? names.join(', ') : 'none found';
}

function machine(m, now) {
  const lines = [
    `   ${hardware(m)}`,
    `   ${loadLine(m)}`,
  ];
  if (m.target) lines.push(`   tools on the login PATH: ${tools(m)} · home ${m.home}`);
  // rsync reads `ssh://host:port:path` as host "ssh", so spell out both forms.
  if (m.target?.startsWith('ssh://')) {
    lines.push(`   port ${m.port}: use \`ssh -p ${m.port} ${m.hostname}\` and \`rsync -e 'ssh -p ${m.port}' ... ${m.hostname}:offload/<task>/\``);
  }
  if (m.warnings?.length) lines.push(`   WARNING: ${m.warnings.join('; ')}`);
  lines.push(`   load read ${ago(m.probedAt, now)}`);
  return lines.join('\n');
}

const STATUS = {
  'auth-failed': 'SSH refused our keys',
  unreachable: 'not answering',
  untrusted: 'host key never accepted',
  'key-changed': 'host key changed since it was accepted (ask its owner before trusting it again)',
  error: 'probe failed',
};

export function formatSnapshot(s, now = Date.now()) {
  if (!s) return 'The remote-machines daemon has not finished its first scan yet; call list-machines again in a few seconds.';
  const out = [];
  const local = s.local;
  out.push(local?.status === 'ok'
    ? `This machine (${local.machineName}):\n${machine(local, now)}`
    : `This machine: could not be read (${local?.error ?? 'not probed yet'}).`);
  out.push('');
  if (s.machines.length) {
    out.push("Remote machines you can offload to, best first (run commands there through a login shell: `ssh <target> '$SHELL -l -s' <<'EOF'`):");
    s.machines.forEach((m, i) => {
      const aka = m.aliases?.length ? ` (also ${m.aliases.join(', ')})` : '';
      out.push(`${i + 1}. ${m.target}${aka} - ${m.user}@${m.hostname}`);
      out.push(machine(m, now));
    });
  } else {
    out.push('No remote machine is reachable right now, so run the work on this machine.');
  }
  const groups = {};
  let stale = 0;
  for (const u of s.unavailable) {
    // An address known_hosts alone remembers is usually a lease long since handed on.
    if (u.status === 'unreachable' && u.sources.length === 1 && u.sources[0] === 'known-hosts') stale++;
    else (groups[u.status] ??= []).push(u.target);
  }
  const parts = Object.entries(groups).map(([status, targets]) => `${STATUS[status] ?? status}: ${targets.join(', ')}`);
  if (stale) parts.push(`${stale} old known_hosts addresses not answering`);
  if (parts.length) out.push('', `Not usable (${s.unavailable.length}) - ${parts.join('; ')}.`);
  out.push('', `Load is re-read every ${Math.round(s.probeIntervalMs / 1000)} s; machines were last discovered ${ago(s.discoveredAt, now)}. Pass refresh: true for a fresh scan.`);
  return out.join('\n');
}
