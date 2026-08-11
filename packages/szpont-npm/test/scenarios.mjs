// The machines this launcher plans for, as fact sets. One table, used twice: once
// by plan.mjs for what the JavaScript makes of them, and once by
// parity-with-python.mjs for whether the Python twin makes exactly the same.
//
// A branch that is not represented here is a branch on which the two published
// `szpont` packages are free to disagree, so a new one in launcher.js belongs in
// this list in the same commit.

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
