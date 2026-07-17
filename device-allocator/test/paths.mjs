// Guards the OS-specific Android SDK default resolution in src/paths.js.
//
// Regression: the default ANDROID_HOME was hardcoded to the macOS location
// (~/Library/Android/sdk). On Linux that path does not exist, so EMULATOR_BIN
// pointed at a missing binary, `emulator -list-avds` failed, listAndroid()
// returned [], and the daemon reported needs-create for booted, adb-visible
// emulators. Run: node test/paths.mjs

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PATHS = path.join(__dirname, '..', 'src', 'paths.js');

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };

// Resolve paths.js in a clean child process with a controlled environment.
function resolvePaths(env) {
  const script =
    `import("${PATHS}").then(p => process.stdout.write(JSON.stringify(` +
    `{ ANDROID_HOME: p.ANDROID_HOME })))`;
  const base = { ...process.env };
  delete base.ANDROID_HOME;
  delete base.ANDROID_SDK_ROOT;
  const out = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
    env: { ...base, ...env }, encoding: 'utf8',
  });
  return JSON.parse(out);
}

const home = os.homedir();
const macDefault = path.join(home, 'Library', 'Android', 'sdk');
const linuxDefault = path.join(home, 'Android', 'Sdk');
const expectedDefault = process.platform === 'darwin' ? macDefault : linuxDefault;

console.log('paths: OS-specific Android SDK default');

// With no env override, the default must match the host OS — never the macOS
// path on a non-macOS host.
const bare = resolvePaths({});
ok(`default ANDROID_HOME matches ${process.platform} (${expectedDefault})`,
  bare.ANDROID_HOME === expectedDefault);
if (process.platform !== 'darwin') {
  ok('non-macOS default is NOT the ~/Library macOS path',
    bare.ANDROID_HOME !== macDefault);
}

// Explicit env still wins over the OS default.
const override = resolvePaths({ ANDROID_HOME: '/custom/sdk' });
ok('ANDROID_HOME env overrides the default', override.ANDROID_HOME === '/custom/sdk');

const rootOverride = resolvePaths({ ANDROID_SDK_ROOT: '/root/sdk' });
ok('ANDROID_SDK_ROOT is used when ANDROID_HOME is unset',
  rootOverride.ANDROID_HOME === '/root/sdk');

console.log(`\nPATHS OK — ${passed} assertions passed`);
