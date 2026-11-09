#!/usr/bin/env node
// Installer / status-checker, with the device allocator's interface and JSON output:
//   node src/install.js --check          -> prints JSON status
//   node src/install.js --install        -> register MCP + skill + rule + CLAUDE.md, start daemon
//   node src/install.js --uninstall      -> undo all of the above, stop daemon
//   node src/install.js --start-daemon   -> just ensure the daemon is running
//   node src/install.js --restart-daemon -> stop the running daemon, start this checkout's

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readJson } from '../../daemon-core/src/fsio.js';
import { claudeCodeInstall } from '../../daemon-core/src/claude-install.js';
import { daemonControl } from '../../daemon-core/src/daemon-control.js';
import * as client from './ipc.js';
import { DISCOVERY_PATH } from './paths.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_DIR = path.resolve(__dirname, '..');
const ASSETS = path.join(PKG_DIR, 'assets');

const CLAUDE_MD_BODY = [
  '<!-- diplomat-remote-machines (managed — remove via its installer, src/install.js --uninstall) -->',
  '## Offload artifact-producing work to remote machines',
  '',
  'This machine is shared by many agents, and other machines on the network are reachable over',
  'SSH. **For any task that produces an artifact** (a build, binary, app bundle, test or lint run,',
  'package, container image, render, video, generated docs, dataset, benchmark), **call the',
  '`list-machines` tool first and prefer running the work on the best remote machine it lists**:',
  'rsync the inputs to `offload/<task>/` there, run it through a login shell',
  '(`ssh <target> \'$SHELL -l -s\' <<\'EOF\'`; a plain `ssh <target> <command>` misses Homebrew), copy',
  'back only the artifacts, and delete the remote directory when done.',
  '',
  'Stay on this machine when no remote machine is usable, when the work needs something only this',
  'machine has (simulators and devices, the GUI, local services, credentials that must not leave',
  'it), or when the remote lacks the toolchain. Never copy secrets to a remote, and leave nothing',
  'running there.',
  '<!-- end diplomat-remote-machines -->',
].join('\n');

const VERSION = (readJson(path.join(PKG_DIR, 'package.json')) || {}).version ?? 'unknown';

const claude = claudeCodeInstall({
  name: 'diplomat-remote-machines',
  mcpJs: path.join(__dirname, 'mcp.js'),
  skillSrc: path.join(ASSETS, 'skill', 'SKILL.md'),
  ruleSrc: path.join(ASSETS, 'rule', 'diplomat-remote-machines.md'),
  claudeMd: CLAUDE_MD_BODY,
  backupSuffix: 'bak-rm',
});
const { installed } = claude;
// The daemon holds nothing worth waiting for: its table is rebuilt by the next scan.
const { startDaemon, stopDaemon, restartDaemon } = daemonControl({ client, discoveryPath: DISCOVERY_PATH, restartExitMs: 10000 });

async function status() {
  const s = { ...claude.status(), daemonRunning: (await client.daemonHealth()) != null, version: VERSION };
  s.drift = claude.drift();
  // Only an install can be out of date; see the device allocator's installer.
  s.outdated = installed(s) && s.drift.length > 0;
  return s;
}

const arg = process.argv[2] || '--check';
let out;
try {
  switch (arg) {
    case '--install': {
      claude.install();
      await startDaemon();
      const s = await status();
      out = { action: 'install', ...s, installed: installed(s) };
      break;
    }
    case '--uninstall': {
      await stopDaemon();
      claude.uninstall();
      const s = await status();
      out = { action: 'uninstall', ...s, installed: installed(s) };
      break;
    }
    case '--start-daemon': {
      const { running, error } = await startDaemon();
      out = { action: 'start-daemon', ...(error ? { error } : {}), ...await status(), daemonRunning: running };
      break;
    }
    case '--restart-daemon': {
      const error = await restartDaemon();
      const s = await status();
      out = { action: 'restart-daemon', ...(error ? { error } : {}), ...s, installed: installed(s) };
      break;
    }
    case '--check':
    default: { const s = await status(); out = { action: 'check', ...s, installed: installed(s) }; break; }
  }
} catch (e) {
  // Always emit parseable JSON (with current status) so a UI degrades cleanly.
  out = { action: arg.replace(/^--/, ''), error: e.message || String(e), ...await status(), installed: false };
}
process.stdout.write(`${JSON.stringify(out, null, 2)}\n`);
