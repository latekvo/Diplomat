// Central filesystem layout + tool locations for the device allocator.
//
// Everything the daemon owns lives under one base dir so it is trivial to
// inspect and to clean up. The base dir defaults to ~/.argent/device-allocator
// but can be redirected with DA_BASE_DIR (used by the test harness so it never
// touches the real allocation state).

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const BASE_DIR =
  process.env.DA_BASE_DIR || path.join(os.homedir(), '.argent', 'device-allocator');

export const SOCKET_PATH = path.join(BASE_DIR, 'daemon.sock');
export const DISCOVERY_PATH = path.join(BASE_DIR, 'daemon.json'); // {pid, startedAt, socket}
export const STATE_PATH = path.join(BASE_DIR, 'state.json'); // public snapshot the applet reads
export const LEASES_PATH = path.join(BASE_DIR, 'leases.json'); // full allocation records, for restart rehydration
export const LOG_PATH = path.join(BASE_DIR, 'daemon.log');
export const REPAIRS_DIR = path.join(BASE_DIR, 'repairs');

// Prompt-injection ban list + captured evidence. Lives under the pr-monitor dir
// (a sibling of device-allocator) because the applet's PR-review monitor reads it
// to skip banned authors. Overridable with DA_BAN_DIR for tests.
export const BAN_DIR =
  process.env.DA_BAN_DIR || path.join(os.homedir(), '.argent', 'pr-monitor');
export const BANNED_PATH = path.join(BAN_DIR, 'banned.json'); // the applet reads this
export const INJECTIONS_DIR = path.join(BAN_DIR, 'injections'); // per-incident evidence
export const AUDIT_PATH = path.join(BAN_DIR, 'audit.jsonl'); // shared action log the applet shows

// Android SDK lives outside PATH on this machine, so resolve binaries by absolute
// path from the SDK root. adb too: the daemon may be launched from launchd or the
// applet, whose PATH lacks Homebrew/SDK dirs — a bare 'adb' would silently make
// every Android device invisible (run() maps ENOENT to {ok:false} -> empty list).
//
// The SDK default location is OS-specific: macOS puts it under ~/Library, Linux
// (the Android Studio default) under ~/Android/Sdk. Defaulting to the macOS path
// on Linux points EMULATOR_BIN at a nonexistent file, so `emulator -list-avds`
// fails and every Android device is invisible — the daemon then reports
// needs-create for emulators that are booted and adb-visible.
const DEFAULT_ANDROID_HOME =
  process.platform === 'darwin'
    ? path.join(os.homedir(), 'Library', 'Android', 'sdk')
    : path.join(os.homedir(), 'Android', 'Sdk');
export const ANDROID_HOME =
  process.env.ANDROID_HOME ||
  process.env.ANDROID_SDK_ROOT ||
  DEFAULT_ANDROID_HOME;
const SDK_EMULATOR = path.join(ANDROID_HOME, 'emulator', 'emulator');
// Mirror the adb fallback: if the SDK isn't at the resolved root, fall back to a
// bare 'emulator' so a PATH-provided binary still works instead of failing hard.
export const EMULATOR_BIN =
  process.env.EMULATOR_PATH || (fs.existsSync(SDK_EMULATOR) ? SDK_EMULATOR : 'emulator');
const SDK_ADB = path.join(ANDROID_HOME, 'platform-tools', 'adb');
export const ADB_BIN = process.env.ADB_PATH || (fs.existsSync(SDK_ADB) ? SDK_ADB : 'adb');

// Tunables (overridable for tests). A device with zero screen motion for 15 min is
// reclaimed; the sweep runs every 2 min so reclamation lands close to the threshold.
export const IDLE_LIMIT_MS = Number(process.env.DA_IDLE_LIMIT_MS) || 15 * 60 * 1000;
export const REAP_INTERVAL_MS = Number(process.env.DA_REAP_INTERVAL_MS) || 10 * 1000;
export const IDLE_INTERVAL_MS = Number(process.env.DA_IDLE_INTERVAL_MS) || 2 * 60 * 1000;
export const POOL_INTERVAL_MS = Number(process.env.DA_POOL_INTERVAL_MS) || 8 * 1000;
export const ALLOC_GRACE_MS = Number(process.env.DA_ALLOC_GRACE_MS) || 20 * 1000;

// Max concurrent devices held across all agents. The device *pool* is unbounded
// (agents create devices on demand), so this caps concurrency, not inventory.
export const QUOTA = Number(process.env.DA_QUOTA) || 5;
export const AWAIT_TIMEOUT_MS = Number(process.env.DA_AWAIT_TIMEOUT_MS) || 15 * 60 * 1000;

// How long a quarantined ('repairing') device may sit before the reaper returns it
// to the pool anyway. The repair agent is told to notify /repaired when done, but
// it can die or forget — without a TTL a broken report shrinks the pool forever.
export const REPAIR_TTL_MS = Number(process.env.DA_REPAIR_TTL_MS) || 2 * 60 * 60 * 1000;
