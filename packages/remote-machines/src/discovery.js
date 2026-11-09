// Finding the machines this one might reach over SSH, without connecting to any.
//
// Candidates come from four places: hosts named in Diplomat's settings, the Host
// aliases in ~/.ssh/config, `_ssh._tcp` services advertised over mDNS, and the
// unhashed local-network entries in known_hosts. Each is resolved through `ssh -G`, so
// the user's own config decides the user, port and host name it would connect with.
//
// A candidate is `trusted` only when its host key is already in a known_hosts file.
// Nothing here ever accepts a new key: a host that has not been vetted could accept any
// client key and receive whatever an agent offloads to it, and probing would offer our
// keys to every laptop on an office network.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { SSH_BIN, SSH_KEYGEN_BIN, SSH_CONFIG, USER_SSH_CONFIG, MDNS_BROWSE_MS } from './paths.js';

/// {code, stdout, stderr}; never rejects. A process still running at `timeout` is
/// killed and reported with code null.
export function run(bin, args, { timeout = 5000, input, onSpawn } = {}) {
  return new Promise((resolve) => {
    let child;
    try { child = spawn(bin, args, { stdio: ['pipe', 'pipe', 'pipe'] }); }
    catch (e) { resolve({ code: -1, stdout: '', stderr: String(e) }); return; }
    onSpawn?.(child);
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (c) => { if (stdout.length < 1_000_000) stdout += c; });
    child.stderr.setEncoding('utf8').on('data', (c) => { if (stderr.length < 100_000) stderr += c; });
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch {} }, timeout);
    child.on('error', (e) => { stderr += String(e); });
    child.on('close', (code) => { clearTimeout(timer); resolve({ code, stdout, stderr }); });
    child.stdin.on('error', () => {});
    child.stdin.end(input ?? '');
  });
}

export const sshConfigArgs = () => (SSH_CONFIG ? ['-F', SSH_CONFIG] : []);

/// Concurrency-limited map that keeps input order.
export async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

// ---- local network --------------------------------------------------------

function ipv4(h) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  return m ? m.slice(1).map(Number) : null;
}

/// A name or address on this network: a single label, an mDNS or home/LAN suffix, a
/// tailnet, or a private, link-local or CGNAT (Tailscale) address.
export function isLocalNetwork(hostname) {
  const h = String(hostname).toLowerCase().replace(/\.$/, '');
  if (!h || h === 'localhost') return false;
  const v4 = ipv4(h);
  if (v4) {
    const [a, b] = v4;
    return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)
      || (a === 169 && b === 254) || (a === 100 && b >= 64 && b <= 127);
  }
  if (h.includes(':')) return /^(fe[89ab]|f[cd])/.test(h);
  if (!h.includes('.')) return true;
  return /\.(local|lan|home|internal|home\.arpa|ts\.net)$/.test(h);
}

// ---- sources --------------------------------------------------------------

function expandHome(p) { return p.replace(/^~(?=$|\/)/, os.homedir()); }

// Matches one path segment against an ssh glob (* and ?).
function globSegment(seg) {
  return new RegExp(`^${seg.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`);
}

function includedFiles(pattern, baseDir) {
  const p = expandHome(pattern);
  const abs = path.isAbsolute(p) ? p : path.join(baseDir, p);
  if (!/[*?]/.test(path.basename(abs))) return [abs];
  const dir = path.dirname(abs);
  const re = globSegment(path.basename(abs));
  try { return fs.readdirSync(dir).filter((n) => re.test(n)).sort().map((n) => path.join(dir, n)); } catch { return []; }
}

/// The literal Host aliases of an ssh config and the files it Includes. Patterns
/// (*, ?, !) are not hosts and are skipped. A relative Include is resolved against the
/// config's own directory: ~/.ssh for the user config, as ssh does.
export function sshConfigAliases(file = USER_SSH_CONFIG, seen = new Set()) {
  if (seen.has(file)) return [];
  seen.add(file);
  let text;
  try { text = fs.readFileSync(file, 'utf8'); } catch { return []; }
  const baseDir = SSH_CONFIG ? path.dirname(SSH_CONFIG) : path.join(os.homedir(), '.ssh');
  const out = [];
  for (const raw of text.split('\n')) {
    const line = raw.replace(/#.*/, '').trim();
    const m = /^(\S+)\s*=?\s*(.*)$/.exec(line);
    if (!m) continue;
    const key = m[1].toLowerCase();
    const values = m[2].split(/\s+/).map((v) => v.replace(/^"|"$/g, '')).filter(Boolean);
    if (key === 'host') out.push(...values.filter((v) => !/[*?!]/.test(v)));
    else if (key === 'include') {
      for (const v of values) for (const f of includedFiles(v, baseDir)) out.push(...sshConfigAliases(f, seen));
    }
  }
  return [...new Set(out)];
}

/// What `ssh -G` would connect to for `target`, or null when ssh cannot parse it.
export async function resolveTarget(target) {
  const r = await run(SSH_BIN, [...sshConfigArgs(), '-G', '--', target]);
  if (r.code !== 0) return null;
  const opt = {};
  for (const line of r.stdout.split('\n')) {
    const i = line.indexOf(' ');
    if (i > 0) opt[line.slice(0, i)] = line.slice(i + 1).trim();
  }
  if (!opt.hostname) return null;
  return {
    target,
    hostname: opt.hostname,
    user: opt.user || os.userInfo().username,
    port: Number(opt.port) || 22,
    hostKeyAlias: opt.hostkeyalias && opt.hostkeyalias !== 'none' ? opt.hostkeyalias : null,
    knownHostsFiles: [
      ...(opt.userknownhostsfile || '').split(/\s+/),
      ...(opt.globalknownhostsfile || '').split(/\s+/),
    ].filter(Boolean).map(expandHome),
  };
}

/// The SRV targets in `dns-sd -Z` output: [{hostname, port}].
export function parseDnsSd(text) {
  return [...text.matchAll(/\sSRV\s+\d+\s+\d+\s+(\d+)\s+(\S+)/g)]
    .map((m) => ({ hostname: m[2].replace(/\.$/, ''), port: Number(m[1]) }));
}

/// The resolved services in `avahi-browse -rpt` output: [{hostname, port}].
export function parseAvahi(text) {
  return text.split('\n').map((line) => line.split(';'))
    .filter((f) => f[0] === '=' && f[6])
    .map((f) => ({ hostname: f[6], port: Number(f[8]) || 22 }));
}

/// `_ssh._tcp` services advertised over mDNS: dns-sd on macOS, avahi-browse elsewhere.
/// Neither present, or browsing off, is an empty list.
export async function mdnsHosts() {
  if (!MDNS_BROWSE_MS) return [];
  if (process.platform === 'darwin') {
    // -Z prints each service's SRV record, whose target is the host to connect to.
    return parseDnsSd((await run('dns-sd', ['-Z', '_ssh._tcp', 'local.'], { timeout: MDNS_BROWSE_MS })).stdout);
  }
  return parseAvahi((await run('avahi-browse', ['-rpt', '_ssh._tcp'], { timeout: MDNS_BROWSE_MS + 2000 })).stdout);
}

/// The local-network hosts named in plain-text (unhashed) known_hosts entries.
export function knownHostsLan(files) {
  const out = [];
  for (const file of files) {
    let text;
    try { text = fs.readFileSync(file, 'utf8'); } catch { continue; }
    for (const line of text.split('\n')) {
      const first = line.trim().split(/\s+/)[0];
      // `|1|` is a hashed name, `@` a marker line (@cert-authority, @revoked).
      if (!first || /^[#|@]/.test(first)) continue;
      for (const name of first.split(',')) {
        const m = /^\[(.+)\]:(\d+)$/.exec(name);
        const hostname = m ? m[1] : name;
        if (/[*?!]/.test(hostname) || !isLocalNetwork(hostname)) continue;
        out.push({ hostname, port: m ? Number(m[2]) : 22 });
      }
    }
  }
  return out;
}

/// Whether this resolved candidate's host key is already in one of its known_hosts files.
export async function hostKeyKnown(c) {
  const name = c.hostKeyAlias || c.hostname;
  const key = c.port === 22 ? name : `[${name}]:${c.port}`;
  for (const f of c.knownHostsFiles) {
    if (!fs.existsSync(f)) continue;
    const r = await run(SSH_KEYGEN_BIN, ['-F', key, '-f', f]);
    if (r.code === 0 && r.stdout.trim()) return true;
  }
  return false;
}

// ---- candidates -----------------------------------------------------------

// In order of preference: when two sources name the same host and port, the earlier
// one's target is kept, since a configured alias carries the user and key the user set
// up for it, which a bare mDNS name connecting as the local user lacks.
export const SOURCES = ['config', 'ssh-config', 'known-hosts', 'mdns'];

const portTarget = ({ hostname, port }) => (port === 22 ? hostname : `ssh://${hostname}:${port}`);

// An mDNS name is whatever a peer on the network chose to advertise, so a name that is
// not a plain host token is dropped, on top of the `--` every ssh call passes.
const SAFE_TARGET = /^(ssh:\/\/)?[A-Za-z0-9_][A-Za-z0-9._@%:[\]-]*$/;

/// Every candidate machine, merged by host name and port:
/// [{target, hostname, user, port, sources, names, trusted}].
///
/// `hosts` are listed whatever their network; the other sources only on this network.
/// A target or host name in `exclude` (case-insensitive) is dropped.
export async function candidates({ hosts = [], exclude = [] } = {}) {
  const defaults = await resolveTarget('localhost');
  // Built in SOURCES order, so the first candidate for a host is the one to keep.
  const raw = [
    ...hosts.map((target) => ({ target, source: 'config' })),
    ...sshConfigAliases().map((target) => ({ target, source: 'ssh-config' })),
    ...knownHostsLan(defaults?.knownHostsFiles ?? []).map((h) => ({ target: portTarget(h), source: 'known-hosts' })),
    ...(await mdnsHosts()).map((h) => ({ target: portTarget(h), source: 'mdns' })),
  ];
  const skip = new Set(exclude.map((e) => String(e).toLowerCase()));
  const self = os.hostname().toLowerCase();
  const safe = raw.filter((c) => SAFE_TARGET.test(c.target));
  const resolved = await mapLimit(safe, 8, async (c) => ({ ...c, r: await resolveTarget(c.target) }));
  const byKey = new Map();
  for (const { target, source, r } of resolved) {
    if (!r) continue;
    const hostname = r.hostname.toLowerCase();
    if (skip.has(target.toLowerCase()) || skip.has(hostname)) continue;
    if (hostname === self || hostname === 'localhost' || hostname.startsWith('127.')) continue;
    if (source !== 'config' && !isLocalNetwork(hostname)) continue;
    const key = `${hostname}:${r.port}`;
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, { ...r, key, sources: [source], names: [target] });
      continue;
    }
    if (!prev.sources.includes(source)) prev.sources.push(source);
    if (!prev.names.includes(target)) prev.names.push(target);
  }
  const list = [...byKey.values()];
  await mapLimit(list, 8, async (c) => { c.trusted = await hostKeyKnown(c); });
  return list;
}
