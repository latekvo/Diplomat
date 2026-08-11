// A whole first run, for real: a real `git clone` off this disk, the real steps
// in order, into a build script and a launch that are stubs and report being run.
//
// Everything above this file tests what the launcher *decides*. This is the one
// that would catch a plan that is right on paper and unrunnable in practice — a
// step whose cwd is wrong, an argument list that never reaches the script, a venv
// whose interpreter is not the one the applet ends up on.
//
// The stub tree is shaped like Diplomat's, but only the two scripts the launcher
// actually reaches exist. Run: node test/e2e.mjs

import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.join(__dirname, '..', 'src', 'cli.js');

if (!['darwin', 'linux'].includes(process.platform)) {
  console.log(`e2e: skipped, the launcher only plans for macOS and Linux (this is ${process.platform})`);
  process.exit(0);
}

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

// Resolved: macOS hands out /var/folders/… temporary directories that are really
// /private/var/folders/…, and a shell's $PWD reports the second. The assertions
// below compare paths, so they have to be comparing the one the steps will see.
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'szpont-e2e-')));
const marker = path.join(tmp, 'marker.txt');
const origin = path.join(tmp, 'origin');
const home = path.join(tmp, 'home');
fs.mkdirSync(home);

const write = (file, body) => {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, body);
  fs.chmodSync(file, 0o755);
};

const platformDir = path.join(origin, 'packages', 'diplomat-platform', process.platform === 'darwin' ? 'macos' : 'linux');
if (process.platform === 'darwin') {
  write(path.join(platformDir, 'install', 'build-app.sh'), `#!/bin/sh\necho "built in $PWD" >> ${marker}\n`);
} else {
  write(path.join(platformDir, 'diplomat'),
    `#!/bin/sh\necho "launched in $PWD with $* using $(command -v python3)" >> ${marker}\n`);
  fs.writeFileSync(path.join(platformDir, 'requirements.txt'), '');
}

const git = (...args) => {
  const r = spawnSync('git', args, { encoding: 'utf8' });
  assert.equal(r.status, 0, `git ${args.join(' ')}: ${r.stderr}`);
};
git('init', '--quiet', '-b', 'main', origin);
git('-C', origin, 'add', '-A');
git('-C', origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '--quiet', '-m', 'tree');

const env = { ...process.env, HOME: home, DIPLOMAT_REPO_URL: origin, DIPLOMAT_SELF_REPO: '' };
if (process.platform === 'darwin') {
  // `open` would really ask Finder to open a bundle that is not there; the stub in
  // front of it records the argv the launch step ran with instead.
  write(path.join(tmp, 'stub', 'open'), `#!/bin/sh\necho "launched in $PWD with $*" >> ${marker}\n`);
  env.PATH = `${path.join(tmp, 'stub')}${path.delimiter}${process.env.PATH}`;
} else {
  env.DIPLOMAT_CORE_BIN = origin; // exists, so no Swift toolchain is wanted
}

console.log('e2e: clone, build and launch');
const first = spawnSync(process.execPath, [CLI, '--', '--dump'], { encoding: 'utf8', env });
ok('the run succeeds', first.status === 0 || (console.error(first.stderr), false));
ok('the checkout is there afterwards',
  fs.existsSync(path.join(home, '.diplomat', 'checkout', 'packages', 'diplomat-platform')));

const ran = fs.readFileSync(marker, 'utf8');
if (process.platform === 'darwin') {
  ok('the build script ran, in the package directory', ran.includes(`built in ${path.join(home, '.diplomat', 'checkout', 'packages', 'diplomat-platform', 'macos')}`));
  ok('the applet was opened with the arguments it was given', ran.includes('--args --dump'));
} else {
  ok('the applet was launched with the arguments it was given', ran.includes('--dump'));
  ok('the applet runs on the venv\'s interpreter, not the one that started us',
    ran.includes(path.join(home, '.diplomat', 'venv', 'bin', 'python3')));
  ok('the venv records the requirements it was built from',
    fs.existsSync(path.join(home, '.diplomat', 'venv', '.szpont-requirements')));
}

// The second run is the one people actually do: it must not clone again, and on
// Linux it must not reinstall a venv that already matches its requirements.
const second = JSON.parse(spawnSync(process.execPath, [CLI, '--plan'], { encoding: 'utf8', env }).stdout);
const expected = process.platform === 'darwin' ? ['update', 'build', 'launch'] : ['update', 'launch'];
ok(`a second run is exactly [${expected}]`,
  JSON.stringify(second.plan.steps.map((s) => s.id)) === JSON.stringify(expected));

fs.rmSync(tmp, { recursive: true, force: true });
console.log(`${passed} assertions passed`);
