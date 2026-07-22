// Regression (Round 18): the Android automated-repair `-wipe-data` must actually wipe
// even when the broken device is still RUNNING. bootAndroid's running-instance handling
// used to sit behind `if (!wipe)`, so the wipe path never killed the original; the
// `-wipe-data` emulator then aborted on the AVD lock the original still held (detached +
// stdio:'ignore' => unobserved), and waitForAndroidBoot returned that still-running
// UN-WIPED original as booted:true — repair reported ok:true without wiping, returning a
// still-broken device to the pool. eraseIOS shuts a running sim down before erasing; the
// Android wipe path must do the same.
//
// This exercises the REAL (non-FAKE) bootAndroid/wipeAndroid via fake adb/emulator
// binaries (ADB_PATH/EMULATOR_PATH) that model the AVD multi-instance lock: a second
// same-AVD `-wipe-data` boot aborts while an instance is running; a cold one wipes.
// Run: node test/android-wipe-shuts-down-running-instance.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-android-wipe-'));
const STATE = path.join(BASE, 'state');
fs.mkdirSync(STATE, { recursive: true });

// State the fakes coordinate through:
//   running     -> exists iff an emulator holds the AVD lock; contains the AVD name
//   data_marker -> DIRTY (unwiped) or CLEAN (wiped)
const FAKE_ADB = `#!/bin/bash
S="${STATE}"; SERIAL="emulator-5554"
if [ "$1" = "devices" ]; then
  echo "List of devices attached"
  [ -f "$S/running" ] && printf '%s\\tdevice\\n' "$SERIAL"
  exit 0
fi
if [ "$1" = "-s" ]; then
  shift 2
  if [ "$1" = "emu" ] && [ "$2" = "avd" ] && [ "$3" = "name" ]; then
    [ -f "$S/running" ] && printf '%s\\nOK\\n' "$(cat "$S/running")"; exit 0
  fi
  if [ "$1" = "shell" ] && [ "$2" = "getprop" ] && [ "$3" = "sys.boot_completed" ]; then
    [ -f "$S/running" ] && echo "1"; exit 0
  fi
  if [ "$1" = "emu" ] && [ "$2" = "kill" ]; then rm -f "$S/running"; echo "OK"; exit 0; fi
fi
exit 0
`;
const FAKE_EMULATOR = `#!/bin/bash
S="${STATE}"; avd=""; wipe=0
while [ $# -gt 0 ]; do
  case "$1" in
    -avd) avd="$2"; shift 2;;
    -wipe-data) wipe=1; shift;;
    *) shift;;
  esac
done
if [ -f "$S/running" ]; then echo "PANIC: Could not lock AVD" >&2; exit 1; fi
echo "$avd" > "$S/running"
[ "$wipe" = "1" ] && echo "CLEAN" > "$S/data_marker"
exit 0
`;
const adbPath = path.join(BASE, 'fake-adb.sh');
const emuPath = path.join(BASE, 'fake-emulator.sh');
fs.writeFileSync(adbPath, FAKE_ADB, { mode: 0o755 });
fs.writeFileSync(emuPath, FAKE_EMULATOR, { mode: 0o755 });

process.env.ADB_PATH = adbPath;
process.env.EMULATOR_PATH = emuPath;
delete process.env.DA_FAKE_DEVICES; // exercise the REAL bootAndroid/wipeAndroid path

// Broken-but-booted original: still running (holds the lock), data DIRTY.
fs.writeFileSync(path.join(STATE, 'running'), 'testavd\n');
fs.writeFileSync(path.join(STATE, 'data_marker'), 'DIRTY');

const dev = await import('../src/devices.js');
const r = await dev.wipeAndroid('testavd');
const marker = fs.readFileSync(path.join(STATE, 'data_marker'), 'utf8').trim();

fs.rmSync(BASE, { recursive: true, force: true });

assert.equal(r.ok, true, 'wipeAndroid should report ok');
assert.equal(marker, 'CLEAN',
  'the data was NOT wiped — the wipe emulator aborted on the AVD lock held by the ' +
  'still-running original (bootAndroid must shut a running instance down before wiping)');

console.log('ok - android wipe shuts a running instance down and actually wipes');
