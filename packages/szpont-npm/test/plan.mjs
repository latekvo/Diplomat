// What the launcher decides, read back off the plan.
//
// The scenarios themselves live in scenarios.mjs, where the parity test also
// takes them from; this file is what the answers have to *be*. Both halves are
// load-bearing: parity alone would be satisfied by two implementations that are
// identically wrong.
//
// Run: node test/plan.mjs

import assert from 'node:assert/strict';
import { plan, probe } from '../src/launcher.js';
import { SCENARIOS } from './scenarios.mjs';

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  PASS', name); passed++; };
const ids = (p) => p.steps.map((s) => s.id);
const step = (p, id) => p.steps.find((s) => s.id === id);

console.log('plan: what each machine gets asked to do');

const MACOS = '/home/u/.diplomat/checkout/packages/diplomat-platform/macos';
const LINUX = '/home/u/.diplomat/checkout/packages/diplomat-platform/linux';

const fresh = plan(SCENARIOS['darwin-fresh']);
ok('a missing checkout is cloned, built and opened',
  JSON.stringify(ids(fresh)) === JSON.stringify(['clone', 'build', 'launch']));
ok('the clone names the repo and where it goes',
  step(fresh, 'clone').cmd.join(' ') ===
    'git clone https://github.com/latekvo/Diplomat.git /home/u/.diplomat/checkout');
ok('the bundle is built by the checkout\'s own script, from its own directory',
  step(fresh, 'build').cmd[0] === `${MACOS}/install/build-app.sh` && step(fresh, 'build').cwd === MACOS);
ok('the app is opened, not run in the foreground',
  step(fresh, 'launch').cmd.join(' ') === `open ${MACOS}/Diplomat.app`);

ok('a checkout this launcher owns is fast-forwarded first, and may fail',
  ids(plan(SCENARIOS['darwin-managed']))[0] === 'update'
  && step(plan(SCENARIOS['darwin-managed']), 'update').optional === true);
ok('a checkout someone named themselves is never pulled',
  !ids(plan(SCENARIOS['darwin-someone-elses-checkout'])).includes('update'));
ok('--no-update leaves even the managed checkout as it stands',
  !ids(plan(SCENARIOS['darwin-no-update'])).includes('update'));

ok('applet arguments ride behind --args on macOS',
  step(plan(SCENARIOS['darwin-with-args']), 'launch').cmd.slice(-3).join(' ') === '--args --prefill 337');
ok('applet arguments go straight to the launcher on Linux',
  step(plan(SCENARIOS['linux-with-args']), 'launch').cmd.join(' ') === `${LINUX}/diplomat --dump`);

ok('the applet is started on the venv\'s interpreter',
  step(plan(SCENARIOS['linux-ready']), 'launch').env.PATH === '/home/u/.diplomat/venv/bin:/usr/bin:/bin');
ok('a ready Linux machine only updates and launches',
  JSON.stringify(ids(plan(SCENARIOS['linux-ready']))) === JSON.stringify(['update', 'launch']));
ok('a first Linux run builds the prompt binary, the venv and its dependencies',
  JSON.stringify(ids(plan(SCENARIOS['linux-fresh'])))
    === JSON.stringify(['clone', 'build-core', 'venv', 'deps', 'launch']));
ok('a venv whose requirements moved is installed into again, not rebuilt',
  JSON.stringify(ids(plan(SCENARIOS['linux-stale-venv']))) === JSON.stringify(['update', 'deps', 'launch']));
ok('the prompt binary is not rebuilt when the applet would find one',
  !ids(plan(SCENARIOS['linux-ready'])).includes('build-core'));

ok('no git and nothing to clone is not a blocker, just no pull',
  plan(SCENARIOS['linux-no-git']).blocked === null
  && !ids(plan(SCENARIOS['linux-no-git'])).includes('update'));
ok('no git and nothing here yet is a blocker',
  plan(SCENARIOS['darwin-no-git-fresh']).blocked.tool === 'git');
ok('a Mac without Swift is pointed at Xcode, not at swift.org',
  plan(SCENARIOS['darwin-no-swift']).blocked.fix.includes('xcode-select'));
ok('a Linux box without Swift is pointed at swift.org',
  plan(SCENARIOS['linux-no-core-bin-no-swift']).blocked.fix.includes('swift.org'));
ok('a python3 too old for the applet never gets a venv built from it',
  plan(SCENARIOS['linux-old-python3']).blocked.tool === 'python3'
  && plan(SCENARIOS['linux-old-python3']).blocked.reason.includes('3.9.6'));
ok('an existing venv is not re-opened over the system python\'s version',
  plan(SCENARIOS['linux-old-python3-existing-venv']).blocked === null);
ok('an unreadable python3 version is not treated as an old one',
  plan(SCENARIOS['linux-unreadable-python3']).blocked === null);

ok('the wrong directory stops everything, with the path in the message',
  plan(SCENARIOS['foreign-directory']).steps.length === 0
  && plan(SCENARIOS['foreign-directory']).blocked.reason.includes('/home/u/.diplomat/checkout'));
ok('an unsupported platform plans nothing at all',
  plan(SCENARIOS['unsupported-platform']).steps.length === 0
  && plan(SCENARIOS['unsupported-platform']).blocked.reason.includes('win32'));

console.log('probe: what this machine says about itself');
const found = probe([], { env: { HOME: '/tmp/nowhere-szpont', PATH: '' } });
ok('the checkout defaults into the state directory Diplomat already owns',
  found.checkout === '/tmp/nowhere-szpont/.diplomat/checkout' && found.managed === true);
ok('a directory that is not there is not a checkout', found.checkout_state === 'absent');
ok('the applet\'s own checkout variable is what points elsewhere',
  probe([], { env: { HOME: '/tmp/nowhere-szpont', PATH: '', DIPLOMAT_SELF_REPO: '/srv/d' } }).checkout === '/srv/d'
  && probe([], { env: { HOME: '/tmp/nowhere-szpont', PATH: '', DIPLOMAT_SELF_REPO: '/srv/d' } }).managed === false);
ok('a fork is taken from the environment',
  probe([], { env: { HOME: '/tmp/x', PATH: '', DIPLOMAT_REPO_URL: '/srv/d.git' } }).repo_url === '/srv/d.git');
ok('tools are looked for on the PATH it is given',
  probe([], { env: { HOME: '/tmp/x', PATH: '/usr/bin:/bin' } }).git === true
  && probe([], { env: { HOME: '/tmp/x', PATH: '/nonexistent' } }).git === false);

console.log(`${passed} assertions passed`);
