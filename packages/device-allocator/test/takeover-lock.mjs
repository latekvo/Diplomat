// The startup lock around a stale-socket takeover, on the paths where the takeover
// fails. A directory at the socket path makes it fail on cue: listen() reports
// EADDRINUSE, /health cannot connect, and the unlink is refused.
//
// 1. The re-listen fails (EADDRINUSE again here; ENOSPC on a full disk): the daemon
//    gives the lock back. Left behind, it names a dead pid, and a later daemon
//    reusing that pid would read it as a live holder and defer forever.
// 2. The lock cannot be created at all (a read-only dir here; ENOSPC on a full disk):
//    the daemon says so and exits 1, rather than defer to a holder that does not exist.
// Run: node test/takeover-lock.mjs

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON = path.join(__dirname, '..', 'src', 'daemon.js');

function blockedBase() {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'da-lock-'));
  fs.writeFileSync(path.join(base, 'fake.json'), '[]');
  fs.writeFileSync(path.join(base, 'daemon.log'), '');
  fs.mkdirSync(path.join(base, 'daemon.sock'));
  return base;
}
function runDaemon(base) {
  const d = spawn(process.execPath, [DAEMON], {
    stdio: 'ignore',
    env: { ...process.env, DA_BASE_DIR: base, DA_BAN_DIR: path.join(base, 'ban'), DA_FAKE_DEVICES: path.join(base, 'fake.json') },
  });
  return new Promise((resolve) => {
    d.once('exit', resolve);
    setTimeout(() => { d.kill('SIGKILL'); resolve('timeout'); }, 10000);
  });
}

const relisten = blockedBase();
try {
  const code = await runDaemon(relisten);
  const logText = fs.readFileSync(path.join(relisten, 'daemon.log'), 'utf8');
  assert.equal(code, 0, logText);
  assert.match(logText, /could not acquire the socket/, 'the takeover never reached its re-listen');
  assert.ok(!fs.existsSync(path.join(relisten, 'daemon.sock.lock')), 'the startup lock was left behind');
  console.log('  PASS a failed takeover re-listen releases the startup lock');
} finally {
  fs.rmSync(relisten, { recursive: true, force: true });
}

if (process.getuid?.() === 0) {
  console.log('  SKIP an uncreatable lock: root ignores the read-only dir');
} else {
  const readOnly = blockedBase();
  fs.chmodSync(readOnly, 0o555);
  try {
    const code = await runDaemon(readOnly);
    const logText = fs.readFileSync(path.join(readOnly, 'daemon.log'), 'utf8');
    assert.match(logText, /cannot take the startup lock.*EACCES/, logText);
    assert.equal(code, 1, logText);
    console.log('  PASS a lock that cannot be created fails the start, not read as a live holder');
  } finally {
    fs.chmodSync(readOnly, 0o755);
    fs.rmSync(readOnly, { recursive: true, force: true });
  }
}
