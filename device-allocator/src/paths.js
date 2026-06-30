// Central filesystem layout + tool locations for the device allocator.
//
// Everything the daemon owns lives under one base dir so it is trivial to
// inspect and to clean up. The base dir defaults to ~/.argent/device-allocator
// but can be redirected with DA_BASE_DIR (used by the test harness so it never
// touches the real allocation state).

import os from 'node:os';
import path from 'node:path';

export const BASE_DIR =
  process.env.DA_BASE_DIR || path.join(os.homedir(), '.argent', 'device-allocator');

export const SOCKET_PATH = path.join(BASE_DIR, 'daemon.sock');
export const DISCOVERY_PATH = path.join(BASE_DIR, 'daemon.json'); // {pid, startedAt, socket}
export const STATE_PATH = path.join(BASE_DIR, 'state.json'); // public snapshot the applet reads
export const LOG_PATH = path.join(BASE_DIR, 'daemon.log');
export const REPAIRS_DIR = path.join(BASE_DIR, 'repairs');

// Android SDK lives outside PATH on this machine, so resolve binaries by absolute
// path from the SDK root (adb itself is usually on PATH via Homebrew).
export const ANDROID_HOME =
  process.env.ANDROID_HOME ||
  process.env.ANDROID_SDK_ROOT ||
  path.join(os.homedir(), 'Library', 'Android', 'sdk');
export const EMULATOR_BIN = path.join(ANDROID_HOME, 'emulator', 'emulator');
export const ADB_BIN = process.env.ADB_PATH || 'adb';

// Tunables (overridable for tests). The 1h idle reclaim is the spec default.
export const IDLE_LIMIT_MS = Number(process.env.DA_IDLE_LIMIT_MS) || 60 * 60 * 1000;
export const REAP_INTERVAL_MS = Number(process.env.DA_REAP_INTERVAL_MS) || 10 * 1000;
export const IDLE_INTERVAL_MS = Number(process.env.DA_IDLE_INTERVAL_MS) || 5 * 60 * 1000;
export const POOL_INTERVAL_MS = Number(process.env.DA_POOL_INTERVAL_MS) || 8 * 1000;
export const ALLOC_GRACE_MS = Number(process.env.DA_ALLOC_GRACE_MS) || 20 * 1000;
