// The npm twin of `packages/szpont/szpont_launcher.py`, line for line where it
// matters: `probe()` reads the machine, `plan()` turns those facts into the steps
// that will run, and `run()` runs them. Splitting it that way is what lets
// `test/parity-with-python.mjs` hand both implementations one set of synthetic
// facts and demand the same plan back — the two are published under the same name
// to the two indexes, and a `szpont` that meant different things depending on
// which one you installed from would be the whole failure mode.
//
// Why a bootstrapper at all, rather than a package holding Diplomat: see the
// Python module's header. Short version — Diplomat is built out of the checkout it
// runs from, and updates itself as one.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

export const VERSION = '0.14.0';
export const DEFAULT_REPO_URL = 'https://github.com/latekvo/Diplomat.git';
export const SUPPORTED = ['darwin', 'linux'];
export const MIN_PYTHON = [3, 10];

// The venv and the managed checkout live under the state directory Diplomat
// already owns, so uninstalling is one `rm -rf ~/.diplomat` rather than a hunt.
const STATE_DIR = '.diplomat';

function which(name, env) {
  const dirs = (env.PATH || '').split(path.delimiter).filter(Boolean);
  return dirs.some((dir) => {
    try {
      fs.accessSync(path.join(dir, name), fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}

// `python3 --version`, or null when there is no python3 to ask. Only ever called
// on Linux: a stock macOS has a /usr/bin/python3 that is a Command Line Tools
// stub, and *running* it pops the graphical installer at whoever typed `szpont`.
function python3Version(env) {
  if (!which('python3', env)) return null;
  const r = spawnSync('python3', ['--version'], { encoding: 'utf8', timeout: 30_000 });
  if (r.status !== 0 || !r.stdout) return null;
  const parts = r.stdout.trim().split(/\s+/);
  return parts.length ? parts[parts.length - 1] : null;
}

function digestOf(file) {
  try {
    return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch {
    return null;
  }
}

// A directory, not merely something with that name — the Python twin asks
// `is_dir()` here, and the two have to answer alike about the same disk.
function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch {
    return null;
  }
}

// Whether the applet would find a `diplomat-core` to shell out to. The three
// candidates and their order are `promptcore.core_bin`'s; asking a different
// question here would rebuild a binary the applet can already see, or skip
// building one it cannot.
function coreBinPresent(home, env) {
  if (env.DIPLOMAT_CORE_BIN && fs.existsSync(env.DIPLOMAT_CORE_BIN)) return true;
  if (which('diplomat-core', env)) return true;
  const data = env.XDG_DATA_HOME || path.join(home, '.local', 'share');
  return fs.existsSync(path.join(data, 'diplomat', 'diplomat-core'));
}

export function probe(appArgs = [], { update = true, env = process.env } = {}) {
  const home = env.HOME || os.homedir();
  const platform = process.platform;
  // DIPLOMAT_SELF_REPO is what the applet itself calls the checkout it lives in
  // (selfupdate.repo_root), so pointing the launcher at a working copy and
  // pointing the running applet at one are the same act.
  const explicit = env.DIPLOMAT_SELF_REPO;
  const checkout = explicit || path.join(home, STATE_DIR, 'checkout');

  let state;
  if (!fs.existsSync(checkout)) state = 'absent';
  else if (isDir(path.join(checkout, 'packages', 'diplomat-platform'))) state = 'checkout';
  else state = 'foreign';

  const venv = path.join(home, STATE_DIR, 'venv');
  const digest = digestOf(path.join(checkout, 'packages', 'diplomat-platform', 'linux', 'requirements.txt'));

  return {
    platform,
    path: env.PATH || '',
    checkout,
    checkout_state: state,
    // Only a checkout this launcher created is one it may move: a working copy
    // someone named themselves is theirs, and a launcher that fast-forwarded it
    // would be editing a tree with work in it.
    managed: !explicit,
    repo_url: env.DIPLOMAT_REPO_URL || DEFAULT_REPO_URL,
    update: Boolean(update),
    git: which('git', env),
    swift: which('swift', env),
    // Linux-only questions, and asked only there — see python3Version.
    python3: platform === 'linux' ? python3Version(env) : null,
    core_bin: platform === 'linux' ? coreBinPresent(home, env) : null,
    venv,
    venv_python: fs.existsSync(path.join(venv, 'bin', 'python')),
    venv_current: digest !== null && readText(path.join(venv, '.szpont-requirements')) === digest,
    args: [...appArgs],
  };
}

function step(id, cmd, { cwd = null, env = {}, needs = null, optional = false } = {}) {
  return { id, cmd, cwd, env, needs, optional };
}

// An optional step whose tool is missing is skipped, never a blocker: a checkout
// that cannot be fast-forwarded is still a checkout that runs.
function dropped(s, facts) {
  return Boolean(s.optional && s.needs && !facts[s.needs]);
}

const MISSING = {
  git: 'git is needed to fetch Diplomat',
  swift: 'a Swift toolchain is needed to build Diplomat',
  python3: 'python3 is needed to run the Linux applet',
};
const FIX = {
  git: { darwin: 'xcode-select --install', linux: 'install git from your package manager' },
  swift: {
    darwin: 'install Xcode, or the Command Line Tools (xcode-select --install)',
    linux: 'https://swift.org/install - swiftly is the easy path',
  },
  python3: { darwin: 'install python3', linux: 'install python3 from your package manager' },
};

function tooOld(version) {
  const parts = String(version || '').split('.').slice(0, 2).map((p) => Number.parseInt(p, 10));
  if (parts.length !== 2 || parts.some(Number.isNaN)) return false; // unreadable is not old
  return parts[0] < MIN_PYTHON[0] || (parts[0] === MIN_PYTHON[0] && parts[1] < MIN_PYTHON[1]);
}

// What no step could fix: the wrong OS, or the wrong directory.
function unrunnable(facts) {
  if (!SUPPORTED.includes(facts.platform)) {
    return {
      tool: null,
      reason: `Diplomat runs on macOS and Linux, not ${facts.platform}`,
      fix: 'run it on one of those',
    };
  }
  if (facts.checkout_state === 'foreign') {
    return {
      tool: null,
      reason: `${facts.checkout} exists but is not a Diplomat checkout`,
      fix: 'remove it, or point DIPLOMAT_SELF_REPO at a real one',
    };
  }
  return null;
}

// The first tool this plan needs and this machine has not got, or null. Reported
// rather than thrown, so `--plan` on a machine missing a toolchain still prints
// the whole plan and names what it is short of.
function blockedBy(facts, steps) {
  for (const s of steps) {
    if (!s.needs || dropped(s, facts) || facts[s.needs]) continue;
    return { tool: s.needs, reason: MISSING[s.needs], fix: FIX[s.needs][facts.platform] };
  }
  // Only the interpreter a venv is about to be *made* from: an existing venv is
  // whatever it was built with, and this launcher does not get to re-open that.
  if (steps.some((s) => s.id === 'venv') && tooOld(facts.python3)) {
    return {
      tool: 'python3',
      reason: `the applet needs Python ${MIN_PYTHON[0]}.${MIN_PYTHON[1]}+, and python3 is ${facts.python3}`,
      fix: 'install a newer python3',
    };
  }
  return null;
}

// What running probe()'s machine would do, as data. Pure: same facts in, same plan
// out, on either implementation and either OS. That is what makes `--plan` worth
// printing before anything is cloned or built, and what the parity test compares.
export function plan(facts) {
  // Nothing is worth planning past these two: a machine that cannot run Diplomat
  // and a directory that is not it are both answers, and a plan that offered to
  // clone anyway would be describing a download that ends in the same message.
  const stop = unrunnable(facts);
  if (stop) return { platform: facts.platform, checkout: facts.checkout, steps: [], blocked: stop };

  const macos = path.join(facts.checkout, 'packages', 'diplomat-platform', 'macos');
  const linux = path.join(facts.checkout, 'packages', 'diplomat-platform', 'linux');
  const args = [...facts.args];
  const steps = [];

  if (facts.checkout_state === 'absent') {
    steps.push(step('clone', ['git', 'clone', facts.repo_url, facts.checkout], { needs: 'git' }));
  } else if (facts.managed && facts.update) {
    // Best-effort and fast-forward-only. The applet updates itself daily and on a
    // button, so this is not the update path — it is what keeps a one-shot
    // `npx szpont` from launching last month's commit.
    steps.push(step('update', ['git', '-C', facts.checkout, 'pull', '--ff-only', '--quiet'], {
      needs: 'git',
      optional: true,
    }));
  }

  if (facts.platform === 'darwin') {
    steps.push(step('build', [path.join(macos, 'install', 'build-app.sh')], { cwd: macos, needs: 'swift' }));
    // build-app.sh rebuilds unconditionally, so the bundle always matches the
    // source it was just cloned or pulled from; `open` then detaches it from this
    // terminal, which is where a menu-bar app belongs.
    const launch = ['open', path.join(macos, 'Diplomat.app')];
    if (args.length) launch.push('--args', ...args);
    steps.push(step('launch', launch, { cwd: macos }));
  } else if (facts.platform === 'linux') {
    if (!facts.core_bin) {
      steps.push(step('build-core', [path.join(linux, 'install', 'build-core.sh')], { cwd: linux, needs: 'swift' }));
    }
    // A system python3 is wanted for exactly one thing, creating the venv;
    // everything after it runs on the venv's own interpreter.
    if (!facts.venv_python) {
      steps.push(step('venv', ['python3', '-m', 'venv', facts.venv], { needs: 'python3' }));
    }
    if (!facts.venv_current) {
      steps.push(step('deps', [
        path.join(facts.venv, 'bin', 'python'), '-m', 'pip', 'install', '--upgrade', '--quiet',
        '-r', path.join(linux, 'requirements.txt'),
      ]));
    }
    // The checkout's own launcher, with the venv's interpreter in front of it — so
    // PySide6 resolves without the user's python having heard of Qt, and the two
    // spellings of "start the applet" stay one script.
    steps.push(step('launch', [path.join(linux, 'diplomat'), ...args], {
      cwd: linux,
      env: { PATH: path.join(facts.venv, 'bin') + path.delimiter + facts.path },
    }));
  }

  return {
    platform: facts.platform,
    checkout: facts.checkout,
    steps: steps.filter((s) => !dropped(s, facts)),
    blocked: blockedBy(facts, steps),
  };
}

// Run a plan's steps in order, announcing each. Node cannot exec over itself the
// way the Python twin does, so the applet is a child here and this process stays
// up as its parent — inheriting stdio, and returning its exit code as ours.
export function run(steps, { checkout, venv }) {
  for (const s of steps) {
    process.stderr.write(`→ ${s.cmd.join(' ')}\n`);
    const r = spawnSync(s.cmd[0], s.cmd.slice(1), {
      cwd: s.cwd || undefined,
      env: { ...process.env, ...s.env },
      stdio: 'inherit',
    });
    if (r.error || r.status !== 0) {
      const why = r.error ? r.error.message : `exit ${r.status}`;
      if (s.optional) {
        process.stderr.write(`  (skipped: ${why})\n`);
        continue;
      }
      process.stderr.write(`szpont: ${s.id} failed (${why})\n`);
      return r.status === null || r.status === undefined ? 1 : r.status;
    }
    // The venv now matches the requirements it was just given, and recording that
    // is what makes every later launch skip the install. Hashed here rather than
    // at probe time: on a first run the file arrives with the clone, two steps
    // after the only chance probe had to read it.
    if (s.id === 'deps') {
      const digest = digestOf(path.join(checkout, 'packages', 'diplomat-platform', 'linux', 'requirements.txt'));
      try {
        fs.writeFileSync(path.join(venv, '.szpont-requirements'), digest || '');
      } catch {
        // a re-install next time is the whole cost of not recording it
      }
    }
  }
  return 0;
}

const USAGE = `usage: szpont [--plan] [--no-update] [--version] [-- APP_ARGS...]

Fetch Diplomat if it isn't here, build it, and start it.

  --plan       print what would be run, as JSON, and do none of it
  --no-update  launch the checkout as it stands, without fast-forwarding it
  --version    print the launcher version

Everything after -- is passed to the applet. DIPLOMAT_SELF_REPO points at your own
checkout (never updated); DIPLOMAT_REPO_URL clones from somewhere other than GitHub.
`;

export function main(argv) {
  const flags = { plan: false, update: true };
  const appArgs = [];
  let passthrough = false;
  for (const arg of argv) {
    if (passthrough) appArgs.push(arg);
    else if (arg === '--') passthrough = true;
    else if (arg === '--plan') flags.plan = true;
    else if (arg === '--no-update') flags.update = false;
    else if (arg === '--version') { process.stdout.write(`szpont ${VERSION}\n`); return 0; }
    else if (arg === '--help' || arg === '-h') { process.stdout.write(USAGE); return 0; }
    else if (arg.startsWith('-')) { process.stderr.write(`szpont: unknown option ${arg}\n${USAGE}`); return 2; }
    else appArgs.push(arg);
  }

  const facts = probe(appArgs, { update: flags.update });
  const steps = plan(facts);

  if (flags.plan) {
    process.stdout.write(`${JSON.stringify({ version: VERSION, facts, plan: steps }, null, 2)}\n`);
    return 0;
  }
  if (steps.blocked) {
    process.stderr.write(`szpont: ${steps.blocked.reason}\n  → ${steps.blocked.fix}\n`);
    return 2;
  }
  return run(steps.steps, { checkout: facts.checkout, venv: facts.venv });
}
