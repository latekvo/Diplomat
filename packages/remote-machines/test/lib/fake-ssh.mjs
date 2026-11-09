#!/usr/bin/env node
// Stands in for `ssh` (RM_SSH), so no test reaches a real host. FAKE_SSH_HOSTS is a
// JSON file of {target: {hostname, user?, port?, behavior?, overrides?}}:
//   - `ssh -G <target>` prints what that entry resolves to (an unknown target resolves
//     to itself), with FAKE_KNOWN_HOSTS as the known_hosts file;
//   - a probe of a target with behavior 'auth' or 'refused' fails the way ssh does;
//     otherwise probe.sh runs on this machine, with each `key=value` line whose key is
//     in `overrides` replaced (machine_id, loadavg, disk_kb, ...).
// Every probe appends its arguments, as one JSON line, to FAKE_SSH_LOG.

import fs from 'node:fs';
import os from 'node:os';
import { spawnSync } from 'node:child_process';

const hosts = JSON.parse(fs.readFileSync(process.env.FAKE_SSH_HOSTS, 'utf8'));
const args = process.argv.slice(2);
const g = args.indexOf('-G');
if (g !== -1) {
  const target = args[g + 1] === '--' ? args[g + 2] : args[g + 1];
  const h = hosts[target] ?? {};
  const uri = /^ssh:\/\/(.+):(\d+)$/.exec(target); // how real ssh reads an ssh:// target
  process.stdout.write([
    `hostname ${h.hostname ?? uri?.[1] ?? target}`,
    `user ${h.user ?? os.userInfo().username}`,
    `port ${h.port ?? uri?.[2] ?? 22}`,
    `userknownhostsfile ${process.env.FAKE_KNOWN_HOSTS}`,
    'globalknownhostsfile /nonexistent/ssh_known_hosts',
    '',
  ].join('\n'));
  process.exit(0);
}

// The target is the word before the remote command, which is the last argument.
const target = args[args.length - 2];
const mode = args[args.length - 1].split(' ').pop();
fs.appendFileSync(process.env.FAKE_SSH_LOG, `${JSON.stringify(args)}\n`);
const h = hosts[target] ?? {};
if (h.behavior === 'auth') {
  process.stderr.write(`${h.user ?? 'me'}@${h.hostname ?? target}: Permission denied (publickey).\n`);
  process.exit(255);
}
if (h.behavior === 'refused') {
  process.stderr.write(`ssh: connect to host ${h.hostname ?? target} port 22: Connection refused\n`);
  process.exit(255);
}
const r = spawnSync('sh', ['-s', '--', mode], { input: fs.readFileSync(0), encoding: 'utf8' });
const overrides = h.overrides ?? {};
process.stdout.write(r.stdout.split('\n').map((line) => {
  const k = line.slice(0, line.indexOf('='));
  return k in overrides ? `${k}=${overrides[k]}` : line;
}).join('\n'));
process.exit(r.status ?? 1);
