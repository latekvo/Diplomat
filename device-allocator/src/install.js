#!/usr/bin/env node
// Installer / status-checker for the device allocator. The applet shells this:
//   node src/install.js --check       -> prints JSON status (for the UI)
//   node src/install.js --install      -> register MCP + skill + rule + CLAUDE.md, start daemon
//   node src/install.js --uninstall    -> undo all of the above, stop daemon
//   node src/install.js --start-daemon -> just ensure the daemon is running
//
// Coercion is installed across every layer the research surfaced, strongest first:
//   1. MCP server registration (so the server — and its `instructions` field — is
//      always connected for every local Claude Code agent).
//   2. a `~/.claude/rules/*.md` rule with alwaysApply: true (argent's convention).
//   3. a fenced, managed block appended to ~/.claude/CLAUDE.md (the most reliable
//      always-on text injection for Claude Code).
//   4. a skill in ~/.claude/skills/ describing the request→use→free protocol.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { daemonAlive } from './state.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_DIR = path.resolve(__dirname, '..');
const MCP_JS = path.join(__dirname, 'mcp.js');
const DAEMON_JS = path.join(__dirname, 'daemon.js');
const ASSETS = path.join(PKG_DIR, 'assets');

const HOME = os.homedir();
const CLAUDE_JSON = path.join(HOME, '.claude.json');
const SKILL_DIR = path.join(HOME, '.claude', 'skills', 'argent-device-allocator');
const RULES_DIR = path.join(HOME, '.claude', 'rules');
const RULE_DEST = path.join(RULES_DIR, 'argent-device-allocator.md');
const CLAUDE_MD = path.join(HOME, '.claude', 'CLAUDE.md');

const MCP_KEY = 'argent-device-allocator';
const CMD_NODE = process.execPath; // capture the node that ran the installer

const CLAUDE_MD_BEGIN = '<!-- argent-device-allocator (managed — installed by Argent Utils; remove via the installer) -->';
const CLAUDE_MD_END = '<!-- end argent-device-allocator -->';
const CLAUDE_MD_BODY = [
  CLAUDE_MD_BEGIN,
  '## Device allocation is mandatory',
  '',
  'Before you boot, run, install onto, screenshot, or otherwise interact with ANY iOS simulator',
  'or Android emulator, you MUST first call the `request-device` tool (argent-device-allocator',
  'MCP server) and then use ONLY the device id it returns. Never select, boot, or touch a',
  'simulator/emulator you were not allocated — another agent may be using it and you will corrupt',
  'their session. Call `free-device` when done, `change-device` to switch, and',
  '`report-device-broken` if it will not boot. This applies every time, without exception.',
  CLAUDE_MD_END,
].join('\n');

// ---- helpers --------------------------------------------------------------

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}
// Atomic write (temp + rename) so a concurrent reader (Claude Code rewrites
// ~/.claude.json frequently) never sees a torn file, and a killed write can't
// truncate the original.
function writeFileAtomic(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, content);
  fs.renameSync(tmp, file);
}
function writeJson(file, obj) {
  writeFileAtomic(file, `${JSON.stringify(obj, null, 2)}\n`);
}
function copyFile(src, dest) {
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
}

// ---- status ---------------------------------------------------------------

function mcpRegistered() {
  const j = readJson(CLAUDE_JSON);
  return !!(j && j.mcpServers && j.mcpServers[MCP_KEY]);
}
function claudeMdInjected() {
  try { return fs.readFileSync(CLAUDE_MD, 'utf8').includes(CLAUDE_MD_BEGIN); } catch { return false; }
}
function status() {
  return {
    mcpRegistered: mcpRegistered(),
    skillInstalled: fs.existsSync(path.join(SKILL_DIR, 'SKILL.md')),
    ruleInstalled: fs.existsSync(RULE_DEST),
    claudeMdInjected: claudeMdInjected(),
    daemonRunning: daemonAlive(),
    nodePath: CMD_NODE,
    mcpJs: MCP_JS,
  };
}
function installed(s = status()) {
  return s.mcpRegistered && s.skillInstalled && s.ruleInstalled && s.claudeMdInjected;
}

// ---- install / uninstall --------------------------------------------------

function registerMcp() {
  let j = {};
  if (fs.existsSync(CLAUDE_JSON)) {
    const raw = fs.readFileSync(CLAUDE_JSON, 'utf8');
    if (raw.trim() !== '') {
      try {
        j = JSON.parse(raw);
      } catch (e) {
        // A parse failure here usually means we read mid-write — NEVER overwrite
        // the user's ~134KB config with a fresh near-empty object. Abort instead.
        throw new Error(`refusing to modify ${CLAUDE_JSON}: it is not valid JSON right now (${e.message})`);
      }
    }
    // Back up only a known-good file (we just parsed it), so a clobber can't
    // overwrite a good backup with a bad one.
    try { fs.copyFileSync(CLAUDE_JSON, `${CLAUDE_JSON}.bak-da`); } catch {}
  }
  j.mcpServers = j.mcpServers || {};
  j.mcpServers[MCP_KEY] = { type: 'stdio', command: CMD_NODE, args: [MCP_JS] };
  writeJson(CLAUDE_JSON, j);
}
function unregisterMcp() {
  const j = readJson(CLAUDE_JSON);
  if (j && j.mcpServers && j.mcpServers[MCP_KEY]) {
    delete j.mcpServers[MCP_KEY];
    writeJson(CLAUDE_JSON, j);
  }
}

function installSkillAndRule() {
  copyFile(path.join(ASSETS, 'skill', 'SKILL.md'), path.join(SKILL_DIR, 'SKILL.md'));
  copyFile(path.join(ASSETS, 'rule', 'argent-device-allocator.md'), RULE_DEST);
}
function uninstallSkillAndRule() {
  try { fs.rmSync(SKILL_DIR, { recursive: true, force: true }); } catch {}
  try { fs.rmSync(RULE_DEST, { force: true }); } catch {}
}

function injectClaudeMd() {
  // Only treat as empty when the file genuinely doesn't exist; a real read error
  // on an existing file must surface, not silently clobber the user's CLAUDE.md.
  let text = '';
  if (fs.existsSync(CLAUDE_MD)) text = fs.readFileSync(CLAUDE_MD, 'utf8');
  text = stripClaudeMd(text);
  const sep = text && !text.endsWith('\n\n') ? (text.endsWith('\n') ? '\n' : '\n\n') : '';
  writeFileAtomic(CLAUDE_MD, `${text}${sep}${CLAUDE_MD_BODY}\n`);
}
// Remove every managed block (loop), so install/uninstall always converge to one.
function stripClaudeMd(text) {
  let out = text;
  for (;;) {
    const i = out.indexOf(CLAUDE_MD_BEGIN);
    if (i === -1) break;
    const j = out.indexOf(CLAUDE_MD_END, i);
    if (j === -1) { out = out.slice(0, i); break; }
    out = out.slice(0, i) + out.slice(j + CLAUDE_MD_END.length);
  }
  return out.replace(/\n{3,}/g, '\n\n').replace(/\s+$/, out.trim() ? '\n' : '');
}
function uninjectClaudeMd() {
  try {
    if (!fs.existsSync(CLAUDE_MD)) return;
    const text = fs.readFileSync(CLAUDE_MD, 'utf8');
    writeFileAtomic(CLAUDE_MD, stripClaudeMd(text));
  } catch {}
}

function startDaemon() {
  if (daemonAlive()) return true;
  const child = spawn(CMD_NODE, [DAEMON_JS], { detached: true, stdio: 'ignore' });
  child.unref();
  // give it a moment to bind
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline) {
    if (daemonAlive()) return true;
    try { execFileSync('sleep', ['0.2']); } catch {}
  }
  return daemonAlive();
}
function stopDaemon() {
  try {
    const disc = readJson(path.join(HOME, '.argent', 'device-allocator', 'daemon.json'));
    if (disc && disc.pid) process.kill(disc.pid, 'SIGTERM');
  } catch {}
}

function doInstall() {
  registerMcp();
  installSkillAndRule();
  injectClaudeMd();
  startDaemon();
  return status();
}
function doUninstall() {
  stopDaemon();
  unregisterMcp();
  uninstallSkillAndRule();
  uninjectClaudeMd();
  return status();
}

// ---- cli ------------------------------------------------------------------

const arg = process.argv[2] || '--check';
let out;
try {
  switch (arg) {
    case '--install': { const s = doInstall(); out = { action: 'install', ...s, installed: installed(s) }; break; }
    case '--uninstall': { const s = doUninstall(); out = { action: 'uninstall', ...s, installed: installed(s) }; break; }
    case '--start-daemon': out = { action: 'start-daemon', ...status(), daemonRunning: startDaemon() }; break;
    case '--check':
    default: out = { action: 'check', ...status(), installed: installed() }; break;
  }
} catch (e) {
  // Always emit parseable JSON (with current status) so the applet degrades cleanly.
  out = { action: arg.replace(/^--/, ''), error: e.message || String(e), ...status(), installed: false };
}
process.stdout.write(`${JSON.stringify(out, null, 2)}\n`);
