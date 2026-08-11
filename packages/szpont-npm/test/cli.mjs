// The command itself: what `szpont` prints, what it exits with, and — for the two
// flags whose whole point is that they change nothing — that it left the disk
// alone. Run: node test/cli.mjs

import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { VERSION } from '../src/launcher.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(__dirname, '..', 'src', 'cli.js');

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'szpont-cli-'));
const szpont = (args, extraEnv = {}) => spawnSync(process.execPath, [CLI, ...args], {
  encoding: 'utf8',
  env: { ...process.env, HOME: home, DIPLOMAT_SELF_REPO: '', ...extraEnv },
});

console.log('cli: szpont');

const version = szpont(['--version']);
ok('--version names the launcher and its version',
  version.status === 0 && version.stdout.trim() === `szpont ${VERSION}`);

const help = szpont(['--help']);
ok('--help documents the flags and the passthrough',
  help.status === 0 && help.stdout.includes('--plan') && help.stdout.includes('DIPLOMAT_SELF_REPO'));

const bogus = szpont(['--bogus']);
ok('an unknown option is refused rather than passed to the applet',
  bogus.status === 2 && bogus.stderr.includes('--bogus'));

const planned = szpont(['--plan']);
const report = JSON.parse(planned.stdout);
ok('--plan prints the version, the facts and the plan',
  planned.status === 0 && report.version === VERSION && report.facts && report.plan);
ok('--plan on a bare machine plans a clone',
  report.plan.steps[0].id === 'clone');
ok('--plan does none of it',
  !fs.existsSync(path.join(home, '.diplomat', 'checkout')));

const foreign = path.join(home, 'not-a-checkout');
fs.mkdirSync(foreign);
const blocked = szpont([], { DIPLOMAT_SELF_REPO: foreign });
ok('a directory that is not a checkout exits 2 and says which directory',
  blocked.status === 2 && blocked.stderr.includes(foreign));
ok('nothing was cloned into it',
  fs.readdirSync(foreign).length === 0);

fs.rmSync(home, { recursive: true, force: true });
console.log(`${passed} assertions passed`);
