// The installer against a throwaway HOME: --install lays down all four layers and starts
// the daemon, --check reads a clean install as current and an edited copy as drift,
// --uninstall takes everything back off and stops the daemon, and nothing else in the
// user's CLAUDE.md or ~/.claude.json is touched.
// Run: node test/install.mjs

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sandbox } from './lib/sandbox.mjs';

const INSTALL = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'install.js');
const sb = sandbox();
const home = path.join(sb.dir, 'home');
fs.mkdirSync(path.join(home, '.claude'), { recursive: true });
fs.writeFileSync(path.join(home, '.claude', 'CLAUDE.md'), '# Mine\n\nKeep this.\n');
fs.writeFileSync(path.join(home, '.claude.json'), JSON.stringify({ mcpServers: { other: { command: 'x' } }, theme: 'dark' }));
const env = { ...sb.env, HOME: home };
const run = (flag) => JSON.parse(execFileSync(process.execPath, [INSTALL, flag], { env, encoding: 'utf8' }));
const rule = path.join(home, '.claude', 'rules', 'diplomat-remote-machines.md');

let checks = 0;
const check = (name, fn) => { fn(); checks++; console.log(`  PASS ${name}`); };

try {
  const before = run('--check');
  check('a machine that never installed is neither installed nor outdated', () => {
    assert.equal(before.installed, false);
    assert.equal(before.outdated, false);
  });

  const inst = run('--install');
  check('--install lays down every layer and starts the daemon', () => {
    assert.equal(inst.installed, true);
    assert.equal(inst.daemonRunning, true);
    assert.deepEqual(inst.drift, []);
    const j = JSON.parse(fs.readFileSync(path.join(home, '.claude.json'), 'utf8'));
    assert.deepEqual(j.mcpServers['diplomat-remote-machines'], { type: 'stdio', command: process.execPath, args: [path.join(path.dirname(INSTALL), 'mcp.js')] });
    assert.deepEqual(j.mcpServers.other, { command: 'x' });
    assert.equal(j.theme, 'dark');
    const md = fs.readFileSync(path.join(home, '.claude', 'CLAUDE.md'), 'utf8');
    assert.ok(md.startsWith('# Mine\n\nKeep this.\n\n<!-- diplomat-remote-machines'), md);
    assert.match(md, /call the\n`list-machines` tool first/);
    assert.ok(fs.existsSync(path.join(home, '.claude', 'skills', 'diplomat-remote-machines', 'SKILL.md')));
  });

  check('a second install leaves one copy of the CLAUDE.md block', () => {
    run('--install');
    const md = fs.readFileSync(path.join(home, '.claude', 'CLAUDE.md'), 'utf8');
    assert.equal(md.split('<!-- diplomat-remote-machines').length, 2);
  });

  check('an edited copy reads as drift', () => {
    fs.appendFileSync(rule, '\nhand edit\n');
    const s = run('--check');
    assert.equal(s.outdated, true);
    assert.deepEqual(s.drift, ['rule']);
  });

  const un = run('--uninstall');
  check('--uninstall takes everything back off and stops the daemon', () => {
    assert.equal(un.installed, false);
    assert.equal(un.outdated, false);
    assert.equal(un.daemonRunning, false);
    assert.equal(fs.readFileSync(path.join(home, '.claude', 'CLAUDE.md'), 'utf8'), '# Mine\n\nKeep this.\n');
    const j = JSON.parse(fs.readFileSync(path.join(home, '.claude.json'), 'utf8'));
    assert.deepEqual(Object.keys(j.mcpServers), ['other']);
    assert.equal(fs.existsSync(rule), false);
  });

  console.log(`INSTALL OK — ${checks} checks passed`);
} finally {
  sb.cleanup();
}
