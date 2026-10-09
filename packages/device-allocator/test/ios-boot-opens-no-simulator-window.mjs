// Regression: bootIOS boots the simulator headless and never launches Simulator.app.
// Agents drive their device through argent, which streams the screen itself. A
// Simulator.app that crashes on launch (an older Xcode's app over a newer system
// CoreSimulator) put a "quit unexpectedly" popup on every boot, and `open -ga`
// still exited 0, so bootIOS never noticed.
//
// Exercises the REAL (non-FAKE) bootIOS through fake `xcrun` and `open` executables
// first on PATH that log their argv.
// Run: node test/ios-boot-opens-no-simulator-window.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-ios-boot-'));
const BIN = path.join(BASE, 'bin');
const LOG = path.join(BASE, 'calls.log');
fs.mkdirSync(BIN);

for (const name of ['xcrun', 'open']) {
  fs.writeFileSync(path.join(BIN, name),
    `#!/bin/bash\necho "${name} $*" >> "${LOG}"\nexit 0\n`, { mode: 0o755 });
}

process.env.PATH = `${BIN}${path.delimiter}${process.env.PATH}`;
delete process.env.DA_FAKE_DEVICES; // exercise the REAL bootIOS

const UDID = '00000000-0000-0000-0000-00000000B007';
const dev = await import('../src/devices.js');
const r = await dev.bootIOS(UDID);
const calls = fs.readFileSync(LOG, 'utf8').trim().split('\n');

fs.rmSync(BASE, { recursive: true, force: true });

assert.deepEqual(r, { ok: true, handle: UDID });
assert.deepEqual(calls, [
  `xcrun simctl boot ${UDID}`,
  `xcrun simctl bootstatus ${UDID}`,
], 'bootIOS must run exactly simctl boot + bootstatus, and never `open` Simulator.app');

console.log('ok - iOS boot runs simctl boot + bootstatus and opens no Simulator window');
