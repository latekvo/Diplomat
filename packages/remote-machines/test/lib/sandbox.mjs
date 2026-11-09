// A throwaway world for one test: its own state dir, ssh config, known_hosts, Diplomat
// settings and fake ssh, so nothing it runs reads or reaches the real ones.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as core from '../../../daemon-core/src/test-interrupt.js';

const FAKE_SSH = fileURLToPath(new URL('./fake-ssh.mjs', import.meta.url));
export const DAEMON_JS = fileURLToPath(new URL('../../src/daemon.js', import.meta.url));
export const SRC = fileURLToPath(new URL('../../src/', import.meta.url));

/// `hosts`: the fake-ssh table. `sshConfig`: the ssh config text. `known`: names whose
/// host key known_hosts holds. `settings`: Diplomat's config.json.
export function sandbox({ hosts = {}, sshConfig = '', known = [], settings = {} } = {}) {
  // Short: a unix socket path caps at 104 bytes.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rm-'));
  execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', path.join(dir, 'hostkey')]);
  const pub = fs.readFileSync(path.join(dir, 'hostkey.pub'), 'utf8').trim().split(' ').slice(0, 2).join(' ');
  const file = (name, text) => { fs.writeFileSync(path.join(dir, name), text); return path.join(dir, name); };
  const env = {
    ...process.env,
    RM_BASE_DIR: path.join(dir, 'base'),
    RM_SSH: FAKE_SSH,
    RM_SSH_CONFIG: file('ssh_config', sshConfig),
    RM_MDNS_MS: '',
    DIPLOMAT_CONFIG: file('config.json', JSON.stringify(settings)),
    FAKE_SSH_HOSTS: file('hosts.json', JSON.stringify(hosts)),
    FAKE_KNOWN_HOSTS: file('known_hosts', known.map((h) => `${h} ${pub}\n`).join('')),
    FAKE_SSH_LOG: file('ssh.log', ''),
  };
  const kill = () => core.killDaemonsUnder(DAEMON_JS, 'RM_BASE_DIR', env.RM_BASE_DIR);
  core.onInterrupt(kill);
  return {
    dir,
    env,
    setHosts: (h) => fs.writeFileSync(env.FAKE_SSH_HOSTS, JSON.stringify(h)),
    // Each probe's ssh arguments; its target is the word before the remote command.
    probes: () => fs.readFileSync(env.FAKE_SSH_LOG, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)),
    probed: () => fs.readFileSync(env.FAKE_SSH_LOG, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l).at(-2)),
    daemons: () => core.daemonsUnder(DAEMON_JS, 'RM_BASE_DIR', env.RM_BASE_DIR),
    cleanup: () => { kill(); fs.rmSync(dir, { recursive: true, force: true }); },
  };
}

/// Runs a module's code in a child with the sandbox's env, returning its stdout parsed as JSON.
export function inSandbox(env, code) {
  return JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', code], { env, encoding: 'utf8', cwd: SRC }));
}
