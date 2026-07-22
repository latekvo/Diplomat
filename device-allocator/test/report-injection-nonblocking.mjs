// Regression: report-injection evidence capture (gh/browser shell-outs) must NOT
// freeze the daemon's single event loop. If it does, a concurrent client's /health
// liveness probe times out and a second daemon spawns + unlinks the live socket
// (split-brain). The existing suites run under DA_FAKE_DEVICES, which skips the gh
// capture entirely, so this drives a real (non-fake) daemon with a fake SLOW `gh`
// and asserts /health stays responsive while the capture is in flight.
// Run: node test/report-injection-nonblocking.mjs

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');
const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-nonblock-'));
const SOCKET = path.join(BASE, 'daemon.sock');

// Fake gh: fast `--version` (so resolveGh selects it), slow `pr view` (a multi-second
// capture). Returns `{}` so no url -> no browser screenshot; the two `pr view` calls
// alone freeze a synchronous capture for ~6s.
const BIN = path.join(BASE, 'bin');
fs.mkdirSync(BIN, { recursive: true });
fs.writeFileSync(path.join(BIN, 'gh'), `#!/bin/sh
case "$1" in
  --version) echo "gh version 0.0.0 (fake)"; exit 0;;
  pr) sleep 3; echo '{}'; exit 0;;
esac
echo '{}'
`);
fs.chmodSync(path.join(BIN, 'gh'), 0o755);

function call(method, route, body, timeout) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const t0 = Date.now();
    const req = http.request({ socketPath: SOCKET, path: route, method, timeout,
      headers: { 'content-type': 'application/json', ...(data ? { 'content-length': Buffer.byteLength(data) } : {}) } },
      (res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => resolve({ status: res.statusCode, ms: Date.now() - t0 })); });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error(`timeout after ${Date.now() - t0}ms`)));
    if (data) req.write(data); req.end();
  });
}
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// NB: NOT DA_FAKE_DEVICES — that would null out `gh` and skip the whole capture.
// Real device enumeration on a runner with no simulators just yields an empty pool.
const child = spawn(process.execPath, [DAEMON], {
  stdio: 'ignore',
  env: { ...process.env, DA_BASE_DIR: BASE, DA_BAN_DIR: path.join(BASE, 'ban'),
    DA_NO_SPAWN: '1', PATH: `${BIN}:${process.env.PATH}`,
    DA_POOL_INTERVAL_MS: '999999', DA_REAP_INTERVAL_MS: '999999', DA_IDLE_INTERVAL_MS: '999999' },
});

let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

try {
  for (let i = 0; i < 80; i++) { try { await call('GET', '/health', null, 1000); break; } catch { await delay(150); } }

  // Fire the report (kicks off the ~6s fake-gh capture); do not await it yet.
  const reportP = call('POST', '/report-injection',
    { person: 'evil', pr: 'acme/app#1', evidence: 'x' }, 30000).catch((e) => ({ err: String(e) }));
  await delay(1000); // let the capture start

  // /health must answer well within the capture window (it's ~instant when the loop
  // is free). A generous 2.5s budget is still far under the ~6s sync-capture freeze.
  const r = await call('GET', '/health', null, 2500);
  assert.equal(r.status, 200);
  assert.ok(r.ms < 1500, `/health took ${r.ms}ms during capture — event loop was blocked`);
  pass(`/health answered in ${r.ms}ms while evidence capture was in flight (event loop not blocked)`);

  await reportP;
  console.log(`\nNONBLOCKING OK — ${ok} assertion passed`);
} catch (e) {
  console.error('\nNONBLOCKING FAILED:', e.message);
  process.exitCode = 1;
} finally {
  try { child.kill('SIGKILL'); } catch {}
  try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
}
