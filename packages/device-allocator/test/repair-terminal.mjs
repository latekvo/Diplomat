// The repair agent opens in the terminal Diplomat published (`spawnTerminal` in
// ~/.diplomat/config.json), and the command it stages actually runs the agent.
//
// Drives the REAL spawnRepairAgent through dispatchRepair (DA_EXPOSE_TEST seam) with a
// fake `osascript` on PATH that records its argv, then runs the staged launcher with a
// fake `claude` and checks the agent ran and its output reached the repair log.
// Run: node test/repair-terminal.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-repair-term-'));
const BIN = path.join(BASE, 'bin');
const CONFIG = path.join(BASE, 'config.json');
const RECORD = path.join(BASE, 'osascript-argv');
fs.mkdirSync(BIN);
fs.writeFileSync(path.join(BASE, 'fake.json'), '[]');
const fake = (name, body) => fs.writeFileSync(path.join(BIN, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
fake('claude', 'for a; do :; done; printf "claude-ran %s\\n" "$a"');

process.env.DA_EXPOSE_TEST = '1';
process.env.DA_BASE_DIR = BASE;
process.env.DA_FAKE_DEVICES = path.join(BASE, 'fake.json');
process.env.DIPLOMAT_CONFIG = CONFIG;
const REAL_PATH = process.env.PATH;
process.env.PATH = `${BIN}:/usr/bin:/bin`;

const { ensureDirs } = await import('../src/state.js');
ensureDirs();
const { allocations, dispatchRepair } = await import('../src/daemon.js');
const { resolveTerminal, openArgv } = await import('../src/terminal.js');
const { REPAIRS_DIR } = await import('../src/paths.js');

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const setTerminal = (v) => fs.writeFileSync(CONFIG, JSON.stringify(v === undefined ? {} : { spawnTerminal: v }));
async function waitFor(file) {
  for (let i = 0; i < 50 && !fs.existsSync(file); i++) await delay(100);
  return fs.existsSync(file);
}

function repair(key) {
  const rec = {
    key, platform: 'ios', udid: key.slice(4), handle: key.slice(4), name: 'iPhone X',
    status: 'repairing', ownerPid: null, agentName: 'repair', bootedByUs: true,
    brokenReason: 'line one\nline "two"', requirements: { platform: 'ios' },
  };
  allocations.set(key, rec);
  dispatchRepair(rec);
  return rec;
}

// ---- resolution -----------------------------------------------------------
setTerminal(undefined);
assert.equal(resolveTerminal('darwin'), 'terminal', 'nothing published: Terminal.app');
setTerminal('ghostty');
assert.equal(resolveTerminal('darwin'), 'ghostty');
setTerminal('iterm');
assert.equal(resolveTerminal('darwin'), 'iterm');
setTerminal('konsole');
assert.equal(resolveTerminal('darwin'), 'terminal', 'a Linux key is not a Mac terminal');
assert.equal(resolveTerminal('linux'), 'konsole');
fs.writeFileSync(CONFIG, '{not json');
assert.equal(resolveTerminal('darwin'), 'terminal', 'corrupt config degrades to the fallback');
setTerminal(undefined);
fake('gnome-terminal', 'exit 0');
assert.equal(resolveTerminal('linux'), 'gnome-terminal', 'unpublished on Linux: first installed');
assert.deepEqual(openArgv('gnome-terminal', '/x/l.sh'), ['gnome-terminal', ['--', '/bin/sh', '/x/l.sh']]);
assert.deepEqual(openArgv('kitty', '/x/l.sh'), ['kitty', ['/bin/sh', '/x/l.sh']]);
console.log('ok   resolution + Linux argv');

// ---- the published terminal opens the window, end to end -------------------
// macOS drives Ghostty through osascript; Linux runs the terminal's own argv.
const MAC = process.platform === 'darwin';
const OPENER = MAC ? 'osascript' : 'konsole';
fake(OPENER, `printf '%s\\n' "$@" > '${RECORD}'`);
setTerminal(MAC ? 'ghostty' : 'konsole');
const r = repair('ios:FIRST');
assert.ok(await waitFor(RECORD), `${OPENER} was never invoked`);
const argv = fs.readFileSync(RECORD, 'utf8');
let launcher;
if (MAC) {
  assert.match(argv, /tell application "Ghostty" to new window with configuration \{command:"\/bin\/sh '[^']+\.sh'"\}/);
  assert.doesNotMatch(argv, /"Terminal"/);
  launcher = argv.match(/\/bin\/sh '([^']+)'/)[1];
} else {
  const lines = argv.trim().split('\n');
  assert.deepEqual(lines.slice(0, 2), ['-e', '/bin/sh']);
  launcher = lines[2];
}
assert.equal(path.dirname(launcher), REPAIRS_DIR);

// The staged command really runs the agent, with the daemon's PATH and the multi-line
// reason intact, and tees into the log the record points at.
const ran = spawnSync('/bin/sh', [launcher], { input: '\n', encoding: 'utf8', env: { PATH: '/usr/bin:/bin' } });
assert.equal(ran.status, 0, ran.stderr);
assert.match(ran.stdout, /claude-ran A device managed/);
assert.match(ran.stdout, /press Enter to close/);
assert.match(fs.readFileSync(r.repairLog, 'utf8'), /^claude-ran A device managed/);
assert.match(fs.readFileSync(launcher, 'utf8'), /line one\nline "two"/);

// The window holds its output until someone has read it.
const held = spawn('/bin/sh', [launcher], { stdio: ['pipe', 'ignore', 'ignore'], env: { PATH: '/usr/bin:/bin' } });
await delay(500);
assert.equal(held.exitCode, null, 'the launcher exited without waiting for Enter');
held.stdin.end('\n');
await new Promise((res) => held.on('exit', res));
console.log(`ok   ${MAC ? 'Ghostty' : 'konsole'} window runs the agent`);

// ---- Terminal.app and iTerm keep the input settle ---------------------------
if (MAC) {
  for (const [term, app] of [['terminal', 'Terminal'], ['iterm', 'iTerm']]) {
    fs.rmSync(RECORD, { force: true });
    setTerminal(term);
    repair(`ios:${term.toUpperCase()}`);
    assert.ok(await waitFor(RECORD), `${term}: osascript was never invoked`);
    const s = fs.readFileSync(RECORD, 'utf8');
    assert.match(s, new RegExp(`tell application "${app}"`));
    assert.match(s, /delay 5/);
    // Typed into the window the script just opened, not a second one.
    assert.match(s, term === 'terminal' ? /do script "[^"]+" in t$/m : /tell current session of w to write text/);
  }
  console.log('ok   Terminal / iTerm');
}

// ---- a terminal that cannot be launched does not take the daemon down -------
fs.rmSync(path.join(BIN, OPENER));
process.env.PATH = BIN;
repair('ios:MISSING');
await delay(500);
const logText = fs.readFileSync(path.join(BASE, 'daemon.log'), 'utf8');
assert.match(logText, /repair: terminal failed ios:MISSING/);
assert.doesNotMatch(logText, /repair: agent dispatched ios:MISSING/);
assert.match(logText, /repair: agent dispatched ios:FIRST/);
process.env.PATH = REAL_PATH;
console.log(`ok   missing ${OPENER} is logged, daemon survives`);

fs.rmSync(BASE, { recursive: true, force: true });
console.log('PASS repair-terminal');
process.exit(0);
