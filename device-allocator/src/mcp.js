#!/usr/bin/env node
// The per-agent stdio MCP server. Claude Code launches one of these per session;
// it is a thin forwarder to the shared allocator daemon (which it auto-starts).
//
// Coercion levers (so every agent is FORCED to allocate before touching a device):
//   - `instructions` on the server handshake (reliably injected into Claude Code
//     whenever the server is connected, which is always — it's user-global).
//   - `request-device` is marked alwaysLoad so it can never be hidden behind
//     progressive tool-loading; it's permanently in the tool list.
//   - every tool description states the MUST-allocate-first contract.
//
// Liveness: this process lives exactly as long as its agent session, so the
// daemon uses *our* PID as the ownership token. When the agent dies Claude Code
// kills us; the daemon's reaper frees our devices. We also free explicitly when
// stdin closes (clean shutdown).

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { callDaemon } from './ipc.js';
import { daemonAlive } from './state.js';
import { LOG_PATH, BASE_DIR } from './paths.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DAEMON_JS = path.join(__dirname, 'daemon.js');
const OWNER_PID = process.pid;

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const COERCION =
  'Device allocation is MANDATORY. Before you boot, run, install onto, screenshot, ' +
  'or otherwise interact with ANY iOS simulator or Android emulator, you MUST first call ' +
  'request-device to be allocated one, and then use ONLY the device id it returns. Never ' +
  'select or touch a simulator/emulator you were not allocated — another agent may be using ' +
  'it. Call free-device when done, change-device to switch, and report-device-broken if it ' +
  'will not boot. This applies every time, without exception.';

const TOOLS = [
  {
    name: 'request-device',
    description:
      'Reserve an Android emulator or iOS simulator for your EXCLUSIVE use, booting one if ' +
      'needed. You MUST call this before booting, launching apps on, or interacting with ANY ' +
      'device — never touch a device you were not allocated here, or you will collide with ' +
      'another agent. Returns a device id (UDID for iOS, adb serial for Android) that is yours ' +
      'alone until you free it.',
    inputSchema: {
      type: 'object',
      properties: {
        platform: { type: 'string', enum: ['android', 'ios', 'any'], description: "Platform you need; 'any' if you don't care." },
        version: { type: 'string', description: "Optional OS version: '18'/'18.5' for iOS, '14' or API level '34' for Android. Omit for any." },
        agentName: { type: 'string', description: 'Short label for you, shown in the Argent Utils panel (e.g. your task or window title).' },
      },
      required: [],
    },
    _meta: { 'anthropic/alwaysLoad': true },
  },
  {
    name: 'free-device',
    description: 'Release a device you are done with and shut it down, returning it to the pool. Call this as soon as you finish — good hygiene.',
    inputSchema: {
      type: 'object',
      properties: { deviceId: { type: 'string', description: 'The device id you were given (optional if you only hold one).' } },
      required: [],
    },
  },
  {
    name: 'change-device',
    description: 'Release your current device and request a different one in a single step (e.g. you now need a different platform or version).',
    inputSchema: {
      type: 'object',
      properties: {
        deviceId: { type: 'string', description: 'Device id to release (optional if you only hold one).' },
        platform: { type: 'string', enum: ['android', 'ios', 'any'] },
        version: { type: 'string' },
      },
      required: [],
    },
  },
  {
    name: 'report-device-broken',
    description:
      'Report that the device you were allocated cannot be booted or is malfunctioning. It is ' +
      'taken out of the pool, a repair is dispatched automatically, and you are immediately ' +
      'handed a different device. Do not keep fighting a broken device — report it.',
    inputSchema: {
      type: 'object',
      properties: {
        deviceId: { type: 'string', description: 'Device id that is broken (optional if you only hold one).' },
        reason: { type: 'string', description: 'What went wrong (boot timeout, black screen, crash, etc.).' },
      },
      required: [],
    },
  },
];

// ---- daemon lifecycle -----------------------------------------------------

async function ensureDaemon() {
  if (daemonAlive()) {
    try { await callDaemon('GET', '/health', null, { timeout: 2000 }); return; } catch {}
  }
  try { fs.mkdirSync(BASE_DIR, { recursive: true }); } catch {}
  let out = 'ignore';
  try { out = fs.openSync(LOG_PATH, 'a'); } catch {}
  const child = spawn(process.execPath, [DAEMON_JS], {
    detached: true,
    stdio: ['ignore', out, out],
  });
  child.unref();
  for (let i = 0; i < 60; i++) {
    await delay(200);
    try { await callDaemon('GET', '/health', null, { timeout: 1000 }); return; } catch {}
  }
  throw new Error('device-allocator daemon failed to start');
}

function parentTty() {
  try {
    return execFileSync('ps', ['-p', String(process.ppid), '-o', 'tty='], { encoding: 'utf8' }).trim() || null;
  } catch { return null; }
}

// ---- result formatting ----------------------------------------------------

function formatResult(name, r) {
  let human;
  if (name === 'request-device' || name === 'change-device') {
    human = r.deviceId
      ? `Allocated ${r.name} (${r.platform}${r.version ? ` ${r.version}` : ''}). ` +
        `Device id: ${r.deviceId} (status: ${r.status}). It is yours EXCLUSIVELY — use only this id. ` +
        `Call free-device when finished.`
      : 'No device was allocated.';
  } else if (name === 'free-device') {
    human = r.released ? `Released ${r.released} device(s) and shut them down.` : 'You held no device to release.';
  } else if (name === 'report-device-broken') {
    human = r.deviceId
      ? `Reported broken; a repair was dispatched. You have been reallocated ${r.name} ` +
        `(${r.platform}${r.version ? ` ${r.version}` : ''}) — device id: ${r.deviceId}.`
      : 'Reported broken; no replacement device was available.';
  } else {
    human = 'Done.';
  }
  return `${human}\n\n${JSON.stringify(r)}`;
}

// ---- server ---------------------------------------------------------------

const server = new Server(
  { name: 'argent-device-allocator', version: '0.1.0' },
  { capabilities: { tools: {} }, instructions: COERCION },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name;
  const args = req.params.arguments || {};
  try {
    await ensureDaemon();
    const base = { ownerPid: OWNER_PID, tty: parentTty(), agentName: args.agentName };
    // Allocation may cold-boot a device (up to ~180s); keep the client window
    // comfortably above that so the daemon always responds before we give up
    // (a client timeout mid-boot would orphan the allocation).
    const BOOT = { timeout: 300000 };
    let r;
    if (name === 'request-device') {
      r = await callDaemon('POST', '/request', { ...base, platform: args.platform, version: args.version }, BOOT);
    } else if (name === 'free-device') {
      r = await callDaemon('POST', '/release', { ownerPid: OWNER_PID, deviceId: args.deviceId });
    } else if (name === 'change-device') {
      r = await callDaemon('POST', '/change', { ...base, deviceId: args.deviceId, platform: args.platform, version: args.version }, BOOT);
    } else if (name === 'report-device-broken') {
      r = await callDaemon('POST', '/broken', { ...base, deviceId: args.deviceId, reason: args.reason }, BOOT);
    } else {
      throw new Error(`unknown tool ${name}`);
    }
    return { content: [{ type: 'text', text: formatResult(name, r) }] };
  } catch (e) {
    return { content: [{ type: 'text', text: `device-allocator error: ${e.message || e}` }], isError: true };
  }
});

// Free everything we hold when the agent session ends.
let cleaningUp = false;
async function cleanup() {
  if (cleaningUp) return;
  cleaningUp = true;
  try { await callDaemon('POST', '/release', { ownerPid: OWNER_PID }, { timeout: 5000 }); } catch {}
  process.exit(0);
}

const transport = new StdioServerTransport();
transport.onclose = cleanup; // stdin closed ⇒ agent gone
process.on('SIGTERM', cleanup);
process.on('SIGINT', cleanup);

await server.connect(transport);
