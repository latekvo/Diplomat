// Regression: the daemon's HTTP body reader must decode the request as UTF-8, so a
// large multi-byte `evidence` payload (the verbatim forensic capture of an injection)
// is stored byte-exact rather than corrupted into U+FFFD at every socket-chunk
// boundary that splits a multi-byte character. Drives the real daemon over its socket
// with a >100KB Chinese evidence string and asserts the stored evidence.txt round-trips.
// Run: node test/report-injection-utf8.mjs

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');
const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-utf8-'));
const SOCKET = path.join(BASE, 'daemon.sock');
const BAN_DIR = path.join(BASE, 'ban');

function call(method, route, body, timeout) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request({ socketPath: SOCKET, path: route, method, timeout,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } },
      (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve({ status: res.statusCode, body: b })); });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    if (data) req.write(data); req.end();
  });
}
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const child = spawn(process.execPath, [DAEMON], {
  stdio: 'ignore',
  env: { ...process.env, DA_FAKE_DEVICES: '1', DA_BASE_DIR: BASE, DA_BAN_DIR: BAN_DIR,
    DA_NO_SPAWN: '1', DA_POOL_INTERVAL_MS: '999999', DA_REAP_INTERVAL_MS: '999999',
    DA_IDLE_INTERVAL_MS: '999999' },
});

let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

// A >100KB string dense in a 3-byte character (中 = e4 b8 ad); 8KB socket reads are not
// 3-byte aligned, so without setEncoding every chunk boundary splits a character.
const evidence = '中'.repeat(50000);

try {
  for (let i = 0; i < 80; i++) { try { await call('GET', '/health', null, 1000); break; } catch { await delay(150); } }

  const r = await call('POST', '/report-injection',
    { person: 'evil', pr: 'acme/app#1', evidence }, 30000);
  assert.equal(r.status, 200, `report-injection returned ${r.status}`);

  const injRoot = path.join(BAN_DIR, 'injections');
  const slugs = fs.readdirSync(injRoot);
  assert.equal(slugs.length, 1, `expected one injection dir, got ${slugs.length}`);
  const stored = fs.readFileSync(path.join(injRoot, slugs[0], 'evidence.txt'), 'utf8');

  assert.ok(!stored.includes('�'), 'stored evidence contains U+FFFD replacement chars (corrupted)');
  assert.equal(stored.length, evidence.length, `stored length ${stored.length} != sent ${evidence.length}`);
  assert.equal(stored, evidence, 'stored evidence is not byte-exact with the sent payload');
  pass(`multi-byte evidence (${evidence.length} chars) round-tripped byte-exact through the daemon`);

  console.log(`\nREPORT-INJECTION-UTF8 OK — ${ok} assertion passed`);
} catch (e) {
  console.error('\nREPORT-INJECTION-UTF8 FAILED:', e.message);
  process.exitCode = 1;
} finally {
  try { child.kill('SIGKILL'); } catch {}
  try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
}
