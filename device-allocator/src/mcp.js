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
  'IF YOU CAN SEE THIS MCP SERVER, THERE ARE (OR MAY AT ANY MOMENT BE) MULTIPLE AGENTS ' +
  'RUNNING ON THIS MACHINE — you are NEVER the only one, so never assume a device is yours ' +
  'or free. Device allocation is MANDATORY: before you boot, run, install onto, screenshot, ' +
  'create, or otherwise interact with ANY simulator or emulator, you MUST first call ' +
  'request-device and then use ONLY the device id it returns. Never select, boot, or squat a ' +
  'device you were not allocated. If request-device says the pool is exhausted (quota reached), ' +
  'call await-device and wait — do NOT spin up your own device to get around it. If it says no ' +
  'matching device exists, create one to spec and then call request-device again to claim it. ' +
  'Call free-device when done, change-device to switch, report-device-broken if it will not ' +
  'boot. This applies every time, without exception.';

// Platforms the allocator understands. Format (phone/tablet) applies only to
// ios/android; TV platforms have no format.
const PLATFORMS = ['ios', 'android', 'apple-tv', 'android-tv', 'vega', 'any'];

const TOOLS = [
  {
    name: 'request-device',
    description:
      'Reserve a device for your EXCLUSIVE use, booting one if needed. You MUST call this before ' +
      'booting, launching apps on, creating, or interacting with ANY simulator/emulator — never ' +
      'touch a device you were not allocated here, or you will collide with another agent (there ' +
      'are always potentially several running). Returns a device id (UDID for Apple platforms, ' +
      'adb serial for Android). If the pool is exhausted it tells you to call await-device; if no ' +
      'matching device exists it tells you to create one and call again with its id in deviceId.',
    inputSchema: {
      type: 'object',
      properties: {
        platform: { type: 'string', enum: PLATFORMS, description: "What you need: ios, android, apple-tv, android-tv, vega, or 'any'." },
        format: { type: 'string', enum: ['phone', 'tablet'], description: 'Form factor — only for ios/android. Omit unless you specifically need a phone vs a tablet.' },
        version: { type: 'string', description: 'OPTIONAL and DISCOURAGED — only set an OS version (e.g. "18.5", or "34" API level) when a specific version is genuinely required; otherwise omit and take whatever is available.' },
        deviceId: { type: 'string', description: 'Claim a specific device by UDID/serial — use this to claim a device you just created after a needs-create response.' },
        agentName: { type: 'string', description: 'Short label for you, shown in the Argent Utils panel (e.g. your task or window title).' },
      },
      required: [],
    },
    _meta: { 'anthropic/alwaysLoad': true },
  },
  {
    name: 'await-device',
    description:
      'Wait for a device slot to free when request-device reported the pool EXHAUSTED (the ' +
      'concurrency quota is reached and other agents hold every slot). Blocks until a slot opens, ' +
      'then tells you to call request-device again. Do NOT create your own device to bypass the ' +
      'quota — call this and wait.',
    inputSchema: {
      type: 'object',
      properties: {
        agentName: { type: 'string', description: 'Short label for you (optional).' },
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
    description: 'Release your current device and request a different one in a single step (e.g. you now need a different platform, format, or version).',
    inputSchema: {
      type: 'object',
      properties: {
        deviceId: { type: 'string', description: 'Device id to release (optional if you only hold one).' },
        platform: { type: 'string', enum: PLATFORMS },
        format: { type: 'string', enum: ['phone', 'tablet'] },
        version: { type: 'string', description: 'Optional and discouraged — omit unless a specific version is required.' },
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

function labelReq(req) {
  if (!req) return 'your requirements';
  const parts = [req.platform, req.format, req.version && req.version !== 'any' ? req.version : null].filter(Boolean);
  return parts.join(' / ') || 'your requirements';
}
function deviceDesc(r) {
  return [r.platform, r.format, r.version].filter(Boolean).join(' ');
}

function formatResult(name, r) {
  let human;
  // Outcome-driven responses take priority over the tool name (request / change /
  // report-broken all share the allocate outcomes).
  if (r && r.outcome === 'exhausted') {
    human = `The device pool is EXHAUSTED: all ${r.quota} concurrent slots are held by OTHER agents `
      + `(${r.active}/${r.quota} in use) — you are NOT the only agent on this machine. Do NOT create or `
      + `squat your own device to get around this. Call await-device to wait for a slot to free, then call `
      + `request-device again.`;
  } else if (r && r.outcome === 'needs-create') {
    human = `No available device matches ${labelReq(r.requirements)}, and there is no fixed pool. `
      + `Create a device to that spec yourself (e.g. \`xcrun simctl create\` for Apple platforms, `
      + `\`avdmanager create avd\` for Android, or the relevant argent setup skill), THEN call request-device `
      + `again with deviceId set to the new device's id to claim it. Never use a device without allocating it here.`;
  } else if (r && r.outcome === 'slot-available') {
    human = `A device slot has freed up (${r.active}/${r.quota} now in use). Call request-device again to claim one.`;
  } else if (r && r.outcome === 'await-timeout') {
    human = `Still exhausted after waiting (${r.active}/${r.quota} in use). Call await-device again to keep waiting, `
      + `or open the Argent Utils panel to see who holds the devices.`;
  } else if (name === 'request-device' || name === 'change-device') {
    human = r.deviceId
      ? `Allocated ${r.name} (${deviceDesc(r)}). Device id: ${r.deviceId} (status: ${r.status}). `
        + `It is yours EXCLUSIVELY — use only this id. Call free-device when finished.`
      : 'No device was allocated.';
  } else if (name === 'free-device') {
    human = r.released ? `Released ${r.released} device(s) and shut them down.` : 'You held no device to release.';
  } else if (name === 'report-device-broken') {
    human = r.deviceId
      ? `Reported broken; a repair was dispatched. You have been reallocated ${r.name} (${deviceDesc(r)}) — device id: ${r.deviceId}.`
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
      r = await callDaemon('POST', '/request',
        { ...base, platform: args.platform, format: args.format, version: args.version, deviceId: args.deviceId }, BOOT);
    } else if (name === 'await-device') {
      // Long-poll: block until a slot frees (the daemon caps the wait ~15min).
      r = await callDaemon('POST', '/await', { ...base }, { timeout: 16 * 60 * 1000 });
    } else if (name === 'free-device') {
      r = await callDaemon('POST', '/release', { ownerPid: OWNER_PID, deviceId: args.deviceId });
    } else if (name === 'change-device') {
      r = await callDaemon('POST', '/change',
        { ...base, deviceId: args.deviceId, platform: args.platform, format: args.format, version: args.version }, BOOT);
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
