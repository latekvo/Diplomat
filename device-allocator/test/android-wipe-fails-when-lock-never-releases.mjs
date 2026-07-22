// Regression (round-3 sweep): the Android wipe repair must NOT report success when the
// shut-down instance never releases its AVD lock. bootAndroid(wipe) issues `emu kill` then
// waitForAndroidGone; if that TIMES OUT (a wedged device — exactly the class being repaired —
// whose kill does not free the lock), the old code ignored the timeout, fell through to spawn
// `emulator -wipe-data` (which aborts on the still-held lock, detached + unobserved), and then
// waitForAndroidBoot read the STILL-RUNNING UN-WIPED original as booted:true — so wipeAndroid
// returned ok:true without ever wiping, and dispatchRepair handed the still-broken device back
// to the pool. bootAndroid must instead surface the failure as ok:false so the device stays
// quarantined. This is the shutdown-TIMEOUT companion to android-wipe-shuts-down-running-instance.
//
// Run: node test/android-wipe-fails-when-lock-never-releases.mjs

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const BASE = fs.mkdtempSync(path.join(os.tmpdir(), 'da-wipe-nolock-'));
const STATE = path.join(BASE, 'state');
fs.mkdirSync(STATE, { recursive: true });

// `emu kill` is a NO-OP here: the wedged instance never exits, so the AVD lock is never freed.
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
  if [ "$1" = "emu" ] && [ "$2" = "kill" ]; then echo "OK"; exit 0; fi
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
delete process.env.DA_FAKE_DEVICES;
process.env.DA_ANDROID_GONE_TIMEOUT_MS = '1500'; // keep the timeout path fast

// Wedged-but-booted original: still running (holds the lock), data DIRTY.
fs.writeFileSync(path.join(STATE, 'running'), 'testavd\n');
fs.writeFileSync(path.join(STATE, 'data_marker'), 'DIRTY');

const dev = await import('../src/devices.js');
const r = await dev.wipeAndroid('testavd');
const marker = fs.readFileSync(path.join(STATE, 'data_marker'), 'utf8').trim();

fs.rmSync(BASE, { recursive: true, force: true });

assert.equal(marker, 'DIRTY', 'sanity: the wipe could not run (lock held), so data stays DIRTY');
assert.equal(r.ok, false,
  'wipeAndroid falsely reported ok:true: the lock never released, the wipe aborted, and the ' +
  'still-running UN-WIPED original was read as booted — a false repair success returns a ' +
  'broken device to the pool (bootAndroid must fail when waitForAndroidGone times out)');

console.log('ok - android wipe fails honestly when the AVD lock never releases');
