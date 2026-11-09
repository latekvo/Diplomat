// Installs an MCP server into Claude Code at every layer an agent reads, strongest first:
//   1. the MCP registration in ~/.claude.json (so the server, and the `instructions` on
//      its handshake, are connected in every local session);
//   2. a ~/.claude/rules/*.md rule with alwaysApply: true;
//   3. a fenced, managed block in ~/.claude/CLAUDE.md;
//   4. a skill in ~/.claude/skills/.
//
// Every one is a COPY of something in the checkout, and a `git pull` moves the
// originals alone, so drift() compares them by content rather than against a recorded
// version: a stamp only catches an update someone remembered to bump, and it cannot
// see a hand-edited copy at all.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { readJson, writeAtomic } from './fsio.js';

// Every write is atomic (temp + rename): a concurrent reader (Claude Code rewrites
// ~/.claude.json frequently) never sees a torn file, and neither a killed write nor a
// full disk can truncate the original.
export function writeJson(file, obj) {
  writeAtomic(file, `${JSON.stringify(obj, null, 2)}\n`);
}

function sameBytes(a, b) {
  try { return fs.readFileSync(a).equals(fs.readFileSync(b)); } catch { return false; }
}

/// Whether two paths name the same file on disk, by (device, inode) rather than by
/// string. A path is not an identity here: a checkout can be reached as both
/// ~/dev/diplomat and ~/dev/Diplomat on a case-insensitive volume, and a registration
/// written through one spelling would read as pointing at a different checkout from
/// the other — a permanent, self-reinstalling "out of date" on every launch.
function sameFile(a, b) {
  try {
    const x = fs.statSync(a);
    const y = fs.statSync(b);
    return x.dev === y.dev && x.ino === y.ino;
  } catch { return false; }
}

// Remove every block delimited by (begin, end) — loop, so install/uninstall and
// a legacy-marker cleanup always converge to zero copies. A begin marker without its
// own end (none follows before the next begin) loses only its own line: where the block
// ended is unknown, and what follows may be the user's, so its old body stays as text.
export function stripBlock(text, begin, end) {
  let out = text;
  for (;;) {
    const i = out.indexOf(begin);
    if (i === -1) break;
    const j = out.indexOf(end, i);
    const next = out.indexOf(begin, i + begin.length);
    if (j === -1 || (next !== -1 && next < j)) {
      const eol = out.indexOf('\n', i);
      out = out.slice(0, i) + (eol === -1 ? '' : out.slice(eol + 1));
      continue;
    }
    out = out.slice(0, i) + out.slice(j + end.length);
  }
  return out.replace(/\n{3,}/g, '\n\n').replace(/\s+$/, out.trim() ? '\n' : '');
}

/// `name` keys the MCP registration, the skill dir and the rule file alike.
/// `claudeMd` is the managed block's body, its first and last lines being the markers.
/// `backupSuffix` names the copy of ~/.claude.json kept before each registration.
export function claudeCodeInstall({ name, mcpJs, skillSrc, ruleSrc, claudeMd, backupSuffix, home = os.homedir() }) {
  const claudeJson = path.join(home, '.claude.json');
  const skillDir = path.join(home, '.claude', 'skills', name);
  const ruleDest = path.join(home, '.claude', 'rules', `${name}.md`);
  const claudeMdPath = path.join(home, '.claude', 'CLAUDE.md');
  const lines = claudeMd.split('\n');
  const begin = lines[0];
  const end = lines[lines.length - 1];
  const node = process.execPath;

  function claudeMdText() {
    try { return fs.readFileSync(claudeMdPath, 'utf8'); } catch { return null; }
  }

  function installedBlock() {
    const text = claudeMdText();
    const i = text?.indexOf(begin) ?? -1;
    const j = i === -1 ? -1 : text.indexOf(end, i);
    return j === -1 ? null : text.slice(i, j + end.length);
  }

  function status() {
    return {
      mcpRegistered: !!readJson(claudeJson)?.mcpServers?.[name],
      skillInstalled: fs.existsSync(path.join(skillDir, 'SKILL.md')),
      ruleInstalled: fs.existsSync(ruleDest),
      // The begin marker alone: a block that lost its end marker is a damaged install
      // (drift, reinstalled on launch), not a deliberate uninstall (left alone).
      claudeMdInjected: claudeMdText()?.includes(begin) ?? false,
      nodePath: node,
      mcpJs,
    };
  }

  function installed(s) {
    return s.mcpRegistered && s.skillInstalled && s.ruleInstalled && s.claudeMdInjected;
  }

  /// What no longer matches this checkout, as a list of names for the UI and the log.
  ///
  /// The MCP entry is judged on its `args` and on whether its `command` still exists,
  /// deliberately NOT on `command` matching the node running right now: Diplomat's two
  /// front-ends, which both install the allocator, resolve node slightly differently, and
  /// an equality check there would have each of them see the other's registration as
  /// drift and reinstall on every
  /// launch, forever. A registration pointing at a deleted node, or at another
  /// checkout's mcp.js, is the real breakage — and both of those this catches.
  function drift() {
    const stale = [];
    if (!sameBytes(skillSrc, path.join(skillDir, 'SKILL.md'))) stale.push('skill');
    if (!sameBytes(ruleSrc, ruleDest)) stale.push('rule');
    if (installedBlock() !== claudeMd) stale.push('claudeMd');
    const entry = readJson(claudeJson)?.mcpServers?.[name];
    if (entry && (!sameFile(entry.args?.[0] ?? '', mcpJs) || !fs.existsSync(entry.command ?? ''))) {
      stale.push('mcp');
    }
    return stale;
  }

  function registerMcp() {
    let j = {};
    if (fs.existsSync(claudeJson)) {
      const raw = fs.readFileSync(claudeJson, 'utf8');
      if (raw.trim() !== '') {
        try {
          j = JSON.parse(raw);
        } catch (e) {
          // A parse failure here usually means we read mid-write — NEVER overwrite
          // the user's config with a fresh near-empty object. Abort instead.
          throw new Error(`refusing to modify ${claudeJson}: it is not valid JSON right now (${e.message})`);
        }
      }
      // Back up only a known-good file (we just parsed it), so a clobber can't
      // overwrite a good backup with a bad one.
      try { writeAtomic(`${claudeJson}.${backupSuffix}`, raw, fs.statSync(claudeJson).mode & 0o777); } catch {}
    }
    j.mcpServers = j.mcpServers || {};
    j.mcpServers[name] = { type: 'stdio', command: node, args: [mcpJs] };
    writeJson(claudeJson, j);
  }

  function injectClaudeMd() {
    // Only treat as empty when the file genuinely doesn't exist; a real read error
    // on an existing file must surface, not silently clobber the user's CLAUDE.md.
    let text = '';
    if (fs.existsSync(claudeMdPath)) text = fs.readFileSync(claudeMdPath, 'utf8');
    text = stripBlock(text, begin, end);
    const sep = text && !text.endsWith('\n\n') ? (text.endsWith('\n') ? '\n' : '\n\n') : '';
    writeAtomic(claudeMdPath, `${text}${sep}${claudeMd}\n`);
  }

  function install() {
    registerMcp();
    writeAtomic(path.join(skillDir, 'SKILL.md'), fs.readFileSync(skillSrc));
    writeAtomic(ruleDest, fs.readFileSync(ruleSrc));
    injectClaudeMd();
  }

  function uninstall() {
    const j = readJson(claudeJson);
    if (j?.mcpServers?.[name]) {
      delete j.mcpServers[name];
      writeJson(claudeJson, j);
    }
    try { fs.rmSync(skillDir, { recursive: true, force: true }); } catch {}
    try { fs.rmSync(ruleDest, { force: true }); } catch {}
    try {
      if (fs.existsSync(claudeMdPath)) {
        writeAtomic(claudeMdPath, stripBlock(fs.readFileSync(claudeMdPath, 'utf8'), begin, end));
      }
    } catch {}
  }

  return { status, installed, drift, install, uninstall, paths: { claudeJson, claudeMdPath } };
}
