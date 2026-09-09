// The machines this launcher plans for, as fact sets, and the machines it probes,
// as a directory tree. Each is used twice: by plan.mjs for what the JavaScript
// makes of them, and by parity-with-python.mjs for whether the Python twin makes
// exactly the same.
//
// A branch that is not represented here is a branch on which the two published
// `szpont` packages are free to disagree, so a new one in launcher.js belongs in
// this file in the same commit.

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

// Everything present and up to date; every scenario below is this with something
// taken away.
const READY = {
  platform: 'linux',
  path: '/usr/bin:/bin',
  checkout: '/home/u/.diplomat/checkout',
  checkout_state: 'checkout',
  managed: true,
  repo_url: 'https://github.com/latekvo/Diplomat.git',
  update: true,
  git: true,
  swift: true,
  python3: '3.12.3',
  core_bin: true,
  venv: '/home/u/.diplomat/venv',
  venv_python: true,
  venv_current: true,
  args: [],
};

export const SCENARIOS = {
  'darwin-fresh': { ...READY, platform: 'darwin', checkout_state: 'absent', python3: null, core_bin: null },
  'darwin-managed': { ...READY, platform: 'darwin', python3: null, core_bin: null },
  'darwin-someone-elses-checkout': { ...READY, platform: 'darwin', managed: false, python3: null, core_bin: null },
  'darwin-no-update': { ...READY, platform: 'darwin', update: false, python3: null, core_bin: null },
  'darwin-no-swift': { ...READY, platform: 'darwin', swift: false, python3: null, core_bin: null },
  'darwin-no-git-fresh': { ...READY, platform: 'darwin', checkout_state: 'absent', git: false, python3: null, core_bin: null },
  'darwin-with-args': { ...READY, platform: 'darwin', args: ['--prefill', '337'], python3: null, core_bin: null },
  'linux-ready': READY,
  'linux-fresh': {
    ...READY, checkout_state: 'absent', core_bin: false, venv_python: false, venv_current: false,
  },
  'linux-stale-venv': { ...READY, venv_current: false },
  'linux-no-core-bin': { ...READY, core_bin: false },
  'linux-no-core-bin-no-swift': { ...READY, core_bin: false, swift: false },
  'linux-no-git': { ...READY, git: false },
  'linux-no-python3': { ...READY, python3: null, venv_python: false, venv_current: false },
  'linux-old-python3': { ...READY, python3: '3.9.6', venv_python: false, venv_current: false },
  'linux-old-python3-existing-venv': { ...READY, python3: '3.9.6' },
  'linux-unreadable-python3': { ...READY, python3: 'weird', venv_python: false, venv_current: false },
  'linux-with-args': { ...READY, args: ['--dump'] },
  'foreign-directory': { ...READY, checkout_state: 'foreign' },
  'unsupported-platform': { ...READY, platform: 'win32' },
};

// Every machine below is probed on both: the toolchain gate is a darwin question,
// python3 and core_bin linux ones.
export const PLATFORMS = ['darwin', 'linux'];

// Run `fn` with probe() believing it is on `platform`.
export function onPlatform(platform, fn) {
  const real = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: platform });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, 'platform', real);
  }
}

// The machines probe() reads, built under `root`: one environment per machine,
// every path in it absolute. Probe them from `root` as the working directory -
// one machine keeps its tool there, named by an empty PATH entry.
//
// The `usr-bin-*` machines exist only where a /usr/bin/git does: the shim gate
// has nothing to gate without one.
export function machines(root) {
  const dir = (...parts) => {
    const p = path.join(root, ...parts);
    fs.mkdirSync(p, { recursive: true });
    return p;
  };
  const file = (p, body = '') => fs.writeFileSync(p, body);
  const executable = (d, name, body = '#!/bin/sh\n') => {
    file(path.join(d, name), body);
    fs.chmodSync(path.join(d, name), 0o755);
  };

  const home = dir('home');
  const bin = dir('bin');
  executable(bin, 'git');
  executable(bin, 'swift');
  // Anything executable that is not a directory is found, as shutil.which finds it.
  const dirbin = dir('dirbin');
  dir('dirbin', 'git');
  const fifobin = dir('fifobin');
  execFileSync('mkfifo', [path.join(fifobin, 'git')]);
  fs.chmodSync(path.join(fifobin, 'git'), 0o755);
  executable(root, 'git');
  const danglebin = dir('danglebin');
  fs.symlinkSync(path.join(root, 'gone'), path.join(danglebin, 'git'));

  dir('src', 'packages', 'diplomat-platform');
  const checkout = path.join(root, 'src');
  const elsewhere = dir('elsewhere');

  const pipless = dir('home-pipless');
  executable(dir('home-pipless', '.diplomat', 'venv', 'bin'), 'python');
  const withPip = dir('home-pip');
  executable(dir('home-pip', '.diplomat', 'venv', 'bin'), 'python');
  executable(path.join(withPip, '.diplomat', 'venv', 'bin'), 'pip');

  const requirements = 'PySide6>=6.5\n';
  const stamped = (name, stamp) => {
    const h = dir(name);
    file(path.join(dir(name, '.diplomat', 'checkout', 'packages', 'diplomat-platform', 'linux'), 'requirements.txt'), requirements);
    file(path.join(dir(name, '.diplomat', 'venv'), '.szpont-requirements'), stamp);
    return h;
  };
  const current = stamped('home-current', createHash('sha256').update(requirements).digest('hex'));
  const stale = stamped('home-stale', 'not-that-digest');

  const coreBin = path.join(dir('core'), 'diplomat-core');
  executable(dir('core'), 'diplomat-core');
  const xdg = dir('xdg');
  file(path.join(dir('xdg', 'diplomat'), 'diplomat-core'));
  const homeWithCore = dir('home-core');
  file(path.join(dir('home-core', '.local', 'share', 'diplomat'), 'diplomat-core'));

  const found = {
    bare: { HOME: home, PATH: '' },
    'tools-on-path': { HOME: home, PATH: bin },
    'odd-tools-on-path': { HOME: home, PATH: `${dirbin}${path.delimiter}${fifobin}` },
    'directory-as-tool': { HOME: home, PATH: dirbin },
    'tool-in-the-working-directory': { HOME: home, PATH: path.delimiter },
    'dangling-tool': { HOME: home, PATH: danglebin },
    'own-checkout': { HOME: home, PATH: '', DIPLOMAT_SELF_REPO: checkout },
    'own-checkout-with-a-trailing-slash': { HOME: home, PATH: '', DIPLOMAT_SELF_REPO: `${checkout}/` },
    'empty-checkout-variable': { HOME: home, PATH: '', DIPLOMAT_SELF_REPO: '' },
    'foreign-directory': { HOME: home, PATH: '', DIPLOMAT_SELF_REPO: elsewhere },
    fork: { HOME: home, PATH: '', DIPLOMAT_REPO_URL: '/srv/diplomat.git' },
    'venv-without-pip': { HOME: pipless, PATH: '' },
    'venv-with-pip': { HOME: withPip, PATH: '' },
    'venv-current': { HOME: current, PATH: '' },
    'venv-stale': { HOME: stale, PATH: '' },
    'core-bin-override': { HOME: home, PATH: '', DIPLOMAT_CORE_BIN: coreBin },
    'core-bin-on-path': { HOME: home, PATH: path.dirname(coreBin) },
    'core-bin-in-xdg': { HOME: home, PATH: '', XDG_DATA_HOME: xdg },
    'core-bin-in-home': { HOME: homeWithCore, PATH: '' },
  };

  if (fs.existsSync('/usr/bin/git')) {
    // `xcode-select -p` naming a directory, or exiting 2 as the real one does with
    // no developer directory selected.
    const none = dir('no-tools');
    executable(none, 'xcode-select', '#!/bin/sh\nexit 2\n');
    const some = dir('tools');
    executable(some, 'xcode-select', `#!/bin/sh\necho ${dir('developer')}\n`);
    const usrbin = path.join(root, 'usrbin');
    fs.symlinkSync('/usr/bin', usrbin);
    const linkbin = dir('linkbin');
    fs.symlinkSync('/usr/bin/git', path.join(linkbin, 'git'));
    Object.assign(found, {
      'usr-bin-without-toolchain': { HOME: home, PATH: `${none}${path.delimiter}/usr/bin` },
      'usr-bin-doubled': { HOME: home, PATH: `${none}${path.delimiter}//usr/bin` },
      'usr-bin-symlinked': { HOME: home, PATH: `${none}${path.delimiter}${usrbin}` },
      'usr-bin-linked-tool': { HOME: home, PATH: `${none}${path.delimiter}${linkbin}` },
      'usr-bin-with-toolchain': { HOME: home, PATH: `${some}${path.delimiter}/usr/bin` },
    });
  }
  return found;
}
