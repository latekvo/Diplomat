// The installer's out-of-date detection.
//
// Everything `--install` lays down is a copy of something in this checkout: the
// skill, the rule, the CLAUDE.md coercion block, the MCP registration. A `git pull`
// updates the originals and nothing else, so without a drift check an installed
// machine runs an old skill and an old rule against a new server indefinitely, while
// `--check` cheerfully reports `installed: true`.
//
// Everything here runs against a throwaway HOME + DA_BASE_DIR, so it never reads or
// writes the developer's real ~/.claude.json, ~/.claude/CLAUDE.md or daemon state.
// Run: node test/install-drift.mjs

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PKG_DIR = path.join(__dirname, '..');
const INSTALL_JS = path.join(PKG_DIR, 'src', 'install.js');
const MCP_JS = path.join(PKG_DIR, 'src', 'mcp.js');

const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'da-drift-'));
const BASE = path.join(HOME, '.diplomat', 'device-allocator');
const FAKE = path.join(HOME, 'fake.json');
fs.writeFileSync(FAKE, '[]');

const SKILL = path.join(HOME, '.claude', 'skills', 'diplomat-device-allocator', 'SKILL.md');
const RULE = path.join(HOME, '.claude', 'rules', 'diplomat-device-allocator.md');
const CLAUDE_MD = path.join(HOME, '.claude', 'CLAUDE.md');
const CLAUDE_JSON = path.join(HOME, '.claude.json');
const MCP_KEY = 'diplomat-device-allocator';

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };
const eq = (name, a, b) => { assert.deepEqual(a, b, `${name} (got ${JSON.stringify(a)})`); console.log('  PASS', name); passed++; };

function run(arg) {
  const out = execFileSync(process.execPath, [INSTALL_JS, arg], {
    encoding: 'utf8',
    env: {
      ...process.env,
      HOME: HOME,
      // Sandboxes the daemon's state AND makes install.js skip the one-time
      // ~/.argent migration, which would otherwise reach outside this HOME.
      DA_BASE_DIR: BASE,
      // No real simulators/emulators enumerated by the daemon --install starts.
      DA_FAKE_DEVICES: FAKE,
    },
  });
  return JSON.parse(out);
}

function readClaudeJson() { return JSON.parse(fs.readFileSync(CLAUDE_JSON, 'utf8')); }
function writeClaudeJson(j) { fs.writeFileSync(CLAUDE_JSON, JSON.stringify(j, null, 2)); }

function killDaemon() {
  try {
    const disc = JSON.parse(fs.readFileSync(path.join(BASE, 'daemon.json'), 'utf8'));
    if (disc?.pid) process.kill(disc.pid, 'SIGKILL');
  } catch {}
}

try {
  console.log('install: drift detection');

  // ---- a fresh install is, by construction, current -----------------------
  const fresh = run('--install');
  ok('fresh install reports installed', fresh.installed === true);
  eq('fresh install has no drift', fresh.drift, []);
  ok('fresh install is not outdated', fresh.outdated === false);

  // The stamp both applets show. Read from package.json rather than hardcoded
  // here, so bumping the version doesn't break this test — the point is that the
  // installer reports the version of the checkout it ran from, not some constant.
  const pkgVersion = JSON.parse(fs.readFileSync(path.join(PKG_DIR, 'package.json'), 'utf8')).version;
  eq('the check reports this package version', fresh.version, pkgVersion);

  // ---- one stale artifact at a time ---------------------------------------
  // Each is restored by a re-install before the next, so every case is measured
  // against a known-clean machine rather than against the previous mutation.

  const cases = [
    ['skill', () => fs.appendFileSync(SKILL, '\nan edit from an older release\n')],
    ['rule', () => fs.writeFileSync(RULE, '# a rule from an older release\n')],
    ['claudeMd', () => {
      // Keep the markers, change the body: this is exactly what an old install
      // looks like, and `claudeMdInjected` (a marker search) still reports true.
      const text = fs.readFileSync(CLAUDE_MD, 'utf8');
      const begin = text.indexOf('<!-- diplomat-device-allocator');
      const endMark = '<!-- end diplomat-device-allocator -->';
      const end = text.indexOf(endMark, begin);
      const older = `${text.slice(0, begin)}<!-- diplomat-device-allocator (managed — installed by Diplomat; remove via the installer) -->\nold coercion text\n${endMark}${text.slice(end + endMark.length)}`;
      fs.writeFileSync(CLAUDE_MD, older);
    }],
    ['mcp', () => {
      const j = readClaudeJson();
      j.mcpServers[MCP_KEY].args = ['/somewhere/else/device-allocator/src/mcp.js'];
      writeClaudeJson(j);
    }],
  ];

  for (const [name, mutate] of cases) {
    mutate();
    const after = run('--check');
    eq(`a stale ${name} is the reported drift`, after.drift, [name]);
    ok(`a stale ${name} makes the install outdated`, after.outdated === true);
    ok(`a stale ${name} is still an install`, after.installed === true);

    const repaired = run('--install');
    eq(`--install clears the stale ${name}`, repaired.drift, []);
    ok(`--install leaves it current after ${name}`, repaired.outdated === false);
  }

  // ---- an MCP entry whose node is gone -------------------------------------
  // The other half of the registration: the args can be right while the binary
  // named by `command` no longer exists (an nvm version pruned under it), which
  // leaves Claude Code with a server it cannot start.
  const withDeadNode = readClaudeJson();
  withDeadNode.mcpServers[MCP_KEY].command = path.join(HOME, 'no-such-node');
  writeClaudeJson(withDeadNode);
  eq('a registration pointing at a deleted node is drift', run('--check').drift, ['mcp']);
  run('--install');

  // ---- what must NOT count as drift ---------------------------------------
  // The registration is judged by which FILE it names, not by how that file is
  // spelled. This checkout is reachable as ~/dev/diplomat and ~/dev/Diplomat on a
  // case-insensitive volume, so a string compare would report permanent drift and
  // reinstall on every single launch. A symlink is the portable stand-in for that
  // (a case test can't run on a case-sensitive filesystem).
  const link = path.join(HOME, 'mcp-link.js');
  fs.symlinkSync(MCP_JS, link);
  const aliased = readClaudeJson();
  aliased.mcpServers[MCP_KEY].args = [link];
  writeClaudeJson(aliased);
  eq('another path to the same mcp.js is not drift', run('--check').drift, []);

  // A node OTHER than the one that installed is not drift either: the two
  // front-ends resolve node differently, and treating that as stale would have
  // them reinstall over each other forever.
  const otherNode = path.join(HOME, 'another-node');
  fs.writeFileSync(otherNode, '#!/bin/sh\n');
  const otherCmd = readClaudeJson();
  otherCmd.mcpServers[MCP_KEY].command = otherNode;
  writeClaudeJson(otherCmd);
  eq('a different but existing node is not drift', run('--check').drift, []);

  // ---- an uninstalled machine is not an out-of-date one --------------------
  // Every artifact is missing after --uninstall, so a naive "does it match?"
  // reports maximum drift. Calling that outdated would have the applet treat a
  // deliberate uninstall as damage and reinstall it behind the user's back.
  const gone = run('--uninstall');
  ok('uninstall leaves nothing installed', gone.installed === false);
  ok('an uninstalled machine is not outdated', gone.outdated === false);

  console.log(`\ninstall-drift: ${passed} checks passed`);
} finally {
  killDaemon();
  fs.rmSync(HOME, { recursive: true, force: true });
}
