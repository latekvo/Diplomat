// The window a repair agent runs in: the terminal Diplomat spawns its own agents in.
//
// That choice lives in the applet's own settings store, which this process cannot
// read, so each applet publishes the terminal its spawns resolve to as `spawnTerminal`
// in the shared ~/.diplomat/config.json (Store.swift / store.py). Nothing published -
// no applet has run here - falls back to Terminal.app on macOS and the first installed
// terminal on Linux, the same fallbacks the applets end on.
//
// The command is staged in a launcher file rather than handed over inline: Ghostty
// splits `command:` into words, Linux terminals take an argv, and an AppleScript
// literal is one line. It runs the command as the applets' own spawns do
// (AgentSpawner.shellCommand, review.terminal_argv): under the operator's interactive
// $SHELL (bash if unset), whose rc puts `claude` on PATH where neither Ghostty's bare
// login PATH nor launchd's has it, and inside tmux wherever tmux exists, so an rc that
// execs tmux unless $TMUX is set cannot replace that shell first. This daemon's PATH
// is appended as a fallback.

import fs from 'node:fs';
import path from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { readConfig } from './state.js';
import { log } from './log.js';

const MAC_TERMINALS = ['ghostty', 'iterm', 'terminal'];

// Twin of review.TERMINALS (diplomat-runtime): key -> argv placed before the command.
const LINUX_TERMINALS = {
  'x-terminal-emulator': ['x-terminal-emulator', '-e'],
  'xfce4-terminal': ['xfce4-terminal', '-x'],
  'gnome-terminal': ['gnome-terminal', '--'],
  konsole: ['konsole', '-e'],
  kitty: ['kitty'],
  alacritty: ['alacritty', '-e'],
  xterm: ['xterm', '-e'],
};

// Seconds between opening an iTerm/Terminal window and typing into it: input typed
// while the new shell is still starting is dropped. Same as AgentSpawner.inputSettleDelay.
const INPUT_SETTLE_DELAY = 5;

export const shq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
const aq = (s) => `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;

function published() {
  const v = readConfig().spawnTerminal;
  return typeof v === 'string' ? v : '';
}

function which(bin) {
  return (process.env.PATH || '').split(path.delimiter)
    .filter(Boolean).map((d) => path.join(d, bin)).find((p) => fs.existsSync(p)) || null;
}

export function resolveTerminal(platform = process.platform) {
  const v = published();
  if (platform === 'darwin') return MAC_TERMINALS.includes(v) ? v : 'terminal';
  if (LINUX_TERMINALS[v]) return v;
  return Object.keys(LINUX_TERMINALS).find((k) => which(LINUX_TERMINALS[k][0])) || 'xterm';
}

// Twin of TerminalFocus.binary (macOS, DIPLOMAT_TMUX included) and of review.terminal_argv's
// shutil.which (Linux).
export function tmuxBinary(platform = process.platform) {
  const env = process.env.DIPLOMAT_TMUX;
  if (env !== undefined) return fs.existsSync(env) ? env : null;
  return (platform !== 'darwin' && which('tmux'))
    || ['/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux'].find((p) => fs.existsSync(p)) || null;
}

// Closing the window only detaches tmux, and the agent never exits on its own: the
// session ends once unattached. `diplomat-` is TerminalFocus.sessionPrefix.
export function launcherScript(command, launcher) {
  const tmux = tmuxBinary();
  const session = `diplomat-repair-${path.basename(launcher, '.sh')}`;
  return [
    '#!/bin/sh',
    `export PATH="\${PATH:+$PATH:}"${shq(process.env.PATH || '')}`,
    ...(tmux ? [`[ -z "$TMUX" ] && exec ${shq(tmux)} new-session -s ${shq(session)} ${shq(`/bin/sh ${shq(launcher)}`)} \\; set-option destroy-unattached on`] : []),
    `"\${SHELL:-/bin/bash}" -i -c ${shq(command)}`,
    "printf '\\n[repair agent exited - press Enter to close] '",
    'read _',
    '',
  ].join('\n');
}

// [file, args] that open `terminal` on the launcher.
export function openArgv(terminal, launcher) {
  const run = `/bin/sh ${shq(launcher)}`;
  switch (terminal) {
    case 'ghostty':
      return ['osascript', ['-e',
        `tell application "Ghostty" to new window with configuration {command:${aq(run)}}`]];
    case 'iterm':
      return ['osascript', [
        '-e', 'tell application "iTerm"',
        '-e', 'set w to (create window with default profile)',
        '-e', `delay ${INPUT_SETTLE_DELAY}`,
        '-e', `tell current session of w to write text ${aq(run)}`,
        '-e', 'end tell']];
    case 'terminal':
      return ['osascript', [
        '-e', 'tell application "Terminal"',
        '-e', 'set t to do script ""',
        '-e', `delay ${INPUT_SETTLE_DELAY}`,
        '-e', `do script ${aq(run)} in t`,
        '-e', 'end tell']];
    default: {
      const [bin, ...prefix] = LINUX_TERMINALS[terminal];
      return [bin, [...prefix, '/bin/sh', launcher]];
    }
  }
}

// Open a window running `command`, staged at `launcher`. Outcome goes to the log only:
// the caller has already quarantined the device, and the repair TTL frees it if no
// agent ever reports back.
export function openRepairWindow(key, command, launcher) {
  try { fs.writeFileSync(launcher, launcherScript(command, launcher), { mode: 0o755 }); }
  catch (e) {
    // A full disk can leave it half-written; no window opens on it, so drop it.
    try { fs.unlinkSync(launcher); } catch {}
    throw e;
  }
  const terminal = resolveTerminal();
  const [file, args] = openArgv(terminal, launcher);
  if (file === 'osascript') {
    execFile(file, args, { timeout: 60_000 }, (err, _out, stderr) => {
      if (err) log('repair: terminal failed', key, terminal, String(stderr || err).trim());
      else log('repair: agent dispatched', key, terminal, launcher);
    });
  } else {
    const child = spawn(file, args, { detached: true, stdio: 'ignore' });
    child.on('error', (e) => log('repair: terminal failed', key, terminal, String(e)));
    child.on('spawn', () => log('repair: agent dispatched', key, terminal, launcher));
    child.unref();
  }
}
