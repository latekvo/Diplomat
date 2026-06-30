// Device enumeration + control for iOS simulators (xcrun simctl) and Android
// emulators (adb + the emulator binary). Also the "motion" signal used to
// detect a device that has sat idle (a screenshot hash that stops changing).
//
// Test mode: when DA_FAKE_DEVICES points at a JSON file, enumeration reads it
// and all boot/shutdown/motion operations become controllable no-ops, so the
// allocation logic can be exercised deterministically without real devices.

import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { ADB_BIN, EMULATOR_BIN } from './paths.js';
import { log } from './log.js';

const pexec = promisify(execFile);

async function run(cmd, args, opts = {}) {
  try {
    const { stdout, stderr } = await pexec(cmd, args, {
      maxBuffer: 64 * 1024 * 1024, timeout: opts.timeout ?? 30000, ...opts,
    });
    return { ok: true, stdout: stdout ?? '', stderr: stderr ?? '' };
  } catch (e) {
    return { ok: false, stdout: e.stdout ?? '', stderr: e.stderr ?? '', error: e };
  }
}

async function runBuf(cmd, args, opts = {}) {
  try {
    const { stdout } = await pexec(cmd, args, {
      encoding: 'buffer', maxBuffer: 256 * 1024 * 1024, timeout: opts.timeout ?? 30000, ...opts,
    });
    return { ok: true, buf: stdout };
  } catch (e) { return { ok: false, error: e }; }
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

// ---- test fixture support -------------------------------------------------

function fakeDevices() {
  const p = process.env.DA_FAKE_DEVICES;
  if (!p) return null;
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; }
}
const FAKE = () => process.env.DA_FAKE_DEVICES != null;

// ---- enumeration ----------------------------------------------------------

export async function listIOS() {
  const fake = fakeDevices();
  if (fake) return fake.filter((d) => d.platform === 'ios');

  const r = await run('xcrun', ['simctl', 'list', 'devices', '--json']);
  if (!r.ok) return [];
  let parsed;
  try { parsed = JSON.parse(r.stdout); } catch { return []; }
  const out = [];
  for (const [runtime, devs] of Object.entries(parsed.devices || {})) {
    const m = /SimRuntime\.iOS-(\d+)-(\d+)/.exec(runtime);
    if (!m) continue; // iOS runtimes only (skip tvOS/watchOS)
    const version = `${m[1]}.${m[2]}`;
    for (const d of devs || []) {
      if (d.isAvailable === false) continue;
      out.push({
        key: `ios:${d.udid}`, platform: 'ios', handle: d.udid, udid: d.udid,
        name: d.name, version, apiVersion: m[1],
        state: d.state === 'Booted' ? 'booted' : 'shutdown',
      });
    }
  }
  return out;
}

export async function listAndroid() {
  const fake = fakeDevices();
  if (fake) return fake.filter((d) => d.platform === 'android');

  const r = await run(EMULATOR_BIN, ['-list-avds']);
  if (!r.ok) return [];
  const avds = r.stdout.split('\n').map((s) => s.trim()).filter(Boolean);
  const running = await androidRunningMap();
  return avds.map((avd) => {
    const api = (/API[_-]?(\d+)/i.exec(avd) || [])[1] || null;
    const serial = running[avd] || null;
    return {
      key: `android:${avd}`, platform: 'android', handle: serial, avd,
      name: avd, version: api ? androidRelease(api) : null, apiVersion: api,
      serial, state: serial ? 'booted' : 'shutdown',
    };
  });
}

async function androidRunningMap() {
  const map = {};
  const r = await run(ADB_BIN, ['devices']);
  if (!r.ok) return map;
  const serials = r.stdout
    .split('\n').slice(1)
    .map((l) => l.split('\t')[0].trim())
    .filter((s) => s.startsWith('emulator-'));
  for (const s of serials) {
    const n = await run(ADB_BIN, ['-s', s, 'emu', 'avd', 'name']);
    if (n.ok) {
      // First line is the AVD name, second line is "OK".
      const name = n.stdout.split('\n')[0].trim();
      if (name && name !== 'OK') map[name] = s;
    }
  }
  return map;
}

const ANDROID_RELEASE = {
  35: '15', 34: '14', 33: '13', 32: '12', 31: '12', 30: '11', 29: '10', 28: '9', 27: '8.1',
};
function androidRelease(api) { return ANDROID_RELEASE[Number(api)] || String(api); }

// ---- requirement matching -------------------------------------------------

export function matchesRequirements(dev, req) {
  const plat = (req.platform || 'any').toLowerCase();
  if (plat !== 'any' && dev.platform !== plat) return false;
  const v = req.version;
  if (v && String(v).toLowerCase() !== 'any') {
    const want = String(v).trim();
    const cands = [dev.version, dev.apiVersion].filter(Boolean).map(String);
    const hit = cands.some(
      (c) => c === want || c.startsWith(`${want}.`) || c.split('.')[0] === want,
    );
    if (!hit) return false;
  }
  return true;
}

// ---- boot / shutdown ------------------------------------------------------

export async function bootIOS(udid) {
  if (FAKE()) return { ok: true, handle: udid };
  await run('xcrun', ['simctl', 'boot', udid], { timeout: 120000 }); // no-op if already booted
  await run('open', ['-ga', 'Simulator']);
  const r = await run('xcrun', ['simctl', 'bootstatus', udid], { timeout: 180000 });
  return { ok: r.ok, handle: udid };
}

export async function shutdownIOS(udid) {
  if (FAKE()) return { ok: true };
  return run('xcrun', ['simctl', 'shutdown', udid], { timeout: 60000 });
}

export async function eraseIOS(udid) {
  if (FAKE()) return { ok: true };
  await run('xcrun', ['simctl', 'shutdown', udid], { timeout: 60000 });
  return run('xcrun', ['simctl', 'erase', udid], { timeout: 120000 });
}

export async function bootAndroid(avd, { wipe = false } = {}) {
  if (FAKE()) return { ok: true, handle: `emulator-fake-${avd}`, serial: `emulator-fake-${avd}` };
  // Already running? Reuse it instead of spawning a doomed second instance that
  // would just abort on the AVD lock. (simctl boot is a no-op when booted, but
  // the emulator binary is not, so we guard here.)
  if (!wipe) {
    const running = (await androidRunningMap())[avd];
    if (running) {
      const b = await run(ADB_BIN, ['-s', running, 'shell', 'getprop', 'sys.boot_completed']);
      if (b.ok && b.stdout.trim() === '1') return { ok: true, handle: running, serial: running };
    }
  }
  const args = ['-avd', avd, '-no-snapshot-save', '-netdelay', 'none', '-netspeed', 'full'];
  if (wipe) args.push('-wipe-data');
  const child = spawn(EMULATOR_BIN, args, { detached: true, stdio: 'ignore' });
  child.unref();
  const serial = await waitForAndroidBoot(avd, 180000);
  return { ok: !!serial, handle: serial, serial };
}

async function waitForAndroidBoot(avd, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let serial = null;
  while (Date.now() < deadline) {
    const map = await androidRunningMap();
    if (map[avd]) {
      serial = map[avd];
      const b = await run(ADB_BIN, ['-s', serial, 'shell', 'getprop', 'sys.boot_completed']);
      if (b.ok && b.stdout.trim() === '1') return serial;
    }
    await delay(3000);
  }
  return serial;
}

export async function shutdownAndroid(serial) {
  if (FAKE() || !serial) return { ok: true };
  return run(ADB_BIN, ['-s', serial, 'emu', 'kill'], { timeout: 30000 });
}

export async function wipeAndroid(avd) {
  if (FAKE()) return { ok: true };
  // Cold-boot with a data wipe, wait for boot, then leave it shut down & clean.
  const r = await bootAndroid(avd, { wipe: true });
  if (r.serial) await shutdownAndroid(r.serial);
  return { ok: r.ok };
}

// ---- motion (idle) signal -------------------------------------------------

// Returns a hash of the current screen, or null if it can't be captured.
// Identical hashes across an interval ⇒ no on-screen motion.
export async function motionHash(dev) {
  if (FAKE()) {
    // In tests, a per-device file lets us simulate "frozen" vs "moving" screens.
    const f = process.env.DA_FAKE_MOTION_DIR
      ? path.join(process.env.DA_FAKE_MOTION_DIR, `${dev.key.replace(/[^\w.-]/g, '_')}.txt`)
      : null;
    if (f && fs.existsSync(f)) { try { return fs.readFileSync(f, 'utf8').trim(); } catch {} }
    return 'frozen'; // constant ⇒ counts as idle once the window elapses
  }
  try {
    if (dev.platform === 'ios') {
      const tmp = path.join(os.tmpdir(), `da-shot-${dev.udid}.png`);
      const r = await run('xcrun', ['simctl', 'io', dev.udid, 'screenshot', tmp], { timeout: 20000 });
      if (!r.ok) return null;
      const buf = fs.readFileSync(tmp);
      try { fs.unlinkSync(tmp); } catch {}
      return sha(buf);
    }
    if (dev.platform === 'android' && dev.serial) {
      const r = await runBuf(ADB_BIN, ['-s', dev.serial, 'exec-out', 'screencap', '-p'], { timeout: 20000 });
      if (!r.ok || !r.buf?.length) return null;
      return sha(r.buf);
    }
  } catch (e) { log('motionHash error', dev.key, String(e)); }
  return null;
}
