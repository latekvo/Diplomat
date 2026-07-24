// Proves concurrent daemons racing to reclaim a STALE socket never split-brain. Several
// daemons starting at the same instant against a dead daemon's leftover socket file must
// resolve to EXACTLY ONE live daemon — one /health responder and one pid that logged
// "daemon listening". Without the takeover lock, two+ daemons each unlink the other's
// freshly bound socket and both run their own reap/idle/reclaim loops (split-brain).
// Run: node test/daemon-singleton-race.mjs

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const SOCK = (BASE) => path.join(BASE, 'daemon.sock');
function daemonEnv(BASE) {
  return {
    ...process.env,
    DA_BASE_DIR: BASE,
    DA_BAN_DIR: path.join(BASE, 'ban'),
    DA_FAKE_DEVICES: path.join(BASE, 'fake.json'),
  };
}
function health(BASE) {
  return new Promise((resolve) => {
    const req = http.request(
      { socketPath: SOCK(BASE), path: '/health', method: 'GET', timeout: 1500 },
      (res) => { res.resume(); resolve(res.statusCode === 200); },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

async function bringUp(BASE) {
  const c = spawn(process.execPath, [DAEMON], { stdio: 'ignore', env: daemonEnv(BASE), detached: true });
  for (let i = 0; i < 60; i++) { await delay(100); if (await health(BASE)) return c; }
  throw new Error('daemon never came up');
}

const N = 12;      // racers per round — high enough concurrency to exercise the takeover
const ROUNDS = 6;  // race; the pre-lock bug reproduced ~1/16 rounds at this width
let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

try {
  for (let round = 0; round < ROUNDS; round++) {
    const BASE = fs.mkdtempSync(path.join(os.tmpdir(), `da-race-${round}-`));
    fs.writeFileSync(path.join(BASE, 'fake.json'), '[]');

    // Plant a STALE socket: bring one daemon up, then SIGKILL it (no clean unlink) so the
    // socket file survives with no live owner — the exact stale-reclaim trigger.
    const seed = await bringUp(BASE);
    const seedPid = seed.pid;
    process.kill(seedPid, 'SIGKILL');
    for (let i = 0; i < 40 && alive(seedPid); i++) await delay(50);
    assert.ok(!alive(seedPid), `round ${round}: seed daemon should be dead`);
    assert.ok(fs.existsSync(SOCK(BASE)), `round ${round}: a stale socket file should remain`);
    // Ignore the seed's own "daemon listening" line — only the racers matter below.
    fs.writeFileSync(path.join(BASE, 'daemon.log'), '');

    // Race N daemons against the stale socket, all launched back-to-back.
    const racers = [];
    for (let i = 0; i < N; i++) {
      racers.push(spawn(process.execPath, [DAEMON], { stdio: 'ignore', env: daemonEnv(BASE), detached: true }));
    }
    await delay(3500); // let the fight settle (defers/takeover + any stale-lock recovery)

    assert.ok(await health(BASE), `round ${round}: no daemon came up after the race`);
    const logTxt = fs.existsSync(path.join(BASE, 'daemon.log'))
      ? fs.readFileSync(path.join(BASE, 'daemon.log'), 'utf8') : '';
    const listeners = new Set([...logTxt.matchAll(/daemon listening \S+ pid (\d+)/g)].map((m) => m[1]));
    assert.equal(
      listeners.size, 1,
      `round ${round}: expected exactly ONE listener, got ${listeners.size} — ${[...listeners].join(',')} (split-brain)`,
    );

    // Clean up the survivor + any lingering racers.
    let livePid = 0;
    try { livePid = JSON.parse(fs.readFileSync(path.join(BASE, 'daemon.json'), 'utf8')).pid; } catch {}
    for (const r of racers) { try { r.kill('SIGKILL'); } catch {} }
    if (livePid) { try { process.kill(livePid, 'SIGTERM'); } catch {} }
    await delay(200);
    try { fs.rmSync(BASE, { recursive: true, force: true }); } catch {}
    pass(`round ${round}: exactly one daemon survived the ${N}-way stale-socket race`);
  }
  console.log(`\nDAEMON-SINGLETON-RACE OK — ${ok} rounds clean`);
} catch (e) {
  console.error('\nDAEMON-SINGLETON-RACE FAILED:', e.message);
  process.exitCode = 1;
}
