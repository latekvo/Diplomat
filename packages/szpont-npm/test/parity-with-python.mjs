// The two `szpont` packages read the same machine and plan the same run.
//
// `npm i -g szpont` and `pip install szpont` install the same name from two
// indexes, and the promise attached to that name is that it does the same thing.
// Nothing enforces it structurally — they are two files in two languages — so it
// is enforced here: every machine in scenarios.mjs is probed by both probe()
// implementations, on both platforms, and every scenario goes through both plan()
// implementations, and the two answers must be identical, key for key.
//
// Run: node test/parity-with-python.mjs

import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { plan, probe } from '../src/launcher.js';
import { SCENARIOS, PLATFORMS, machines, onPlatform } from './scenarios.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PY_PKG = path.resolve(__dirname, '..', '..', 'szpont');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'szpont-parity-'));
const MACHINES = machines(root);
// Both sides probe from the fixture's root; the Python twin inherits this cwd.
const startedIn = process.cwd();
process.chdir(root);

// One interpreter run for the whole table: starting python once per question
// would be most of this test's runtime.
const script = `
import json, sys
import szpont_launcher
given = json.load(sys.stdin)
facts = {}
for platform in given["platforms"]:
    sys.platform = platform
    facts[platform] = {k: szpont_launcher.probe(env=v) for k, v in given["machines"].items()}
print(json.dumps({
    "facts": facts,
    "plans": {k: szpont_launcher.plan(v) for k, v in given["scenarios"].items()},
}))
`;

const py = spawnSync('python3', ['-c', script], {
  input: JSON.stringify({ platforms: PLATFORMS, machines: MACHINES, scenarios: SCENARIOS }),
  encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: PY_PKG },
});

// A parity test that cannot reach the other implementation has proved nothing,
// so it fails rather than skips.
assert.equal(py.status, 0,
  `could not run the Python twin (python3 with PYTHONPATH=${PY_PKG}):\n${py.stderr || py.error}`);

const fromPython = JSON.parse(py.stdout);
const machineNames = Object.keys(MACHINES);
const scenarioNames = Object.keys(SCENARIOS);
for (const platform of PLATFORMS) {
  assert.deepEqual(Object.keys(fromPython.facts[platform]).sort(), [...machineNames].sort(),
    `the twin skipped a machine on ${platform}`);
}
assert.deepEqual(Object.keys(fromPython.plans).sort(), [...scenarioNames].sort(), 'the twin skipped a scenario');

try {
  console.log('parity: the JavaScript and Python launchers read a machine identically');
  for (const platform of PLATFORMS) {
    onPlatform(platform, () => {
      for (const name of machineNames) {
        assert.deepEqual(probe([], { env: MACHINES[name] }), fromPython.facts[platform][name],
          `facts differ for ${name} on ${platform}`);
        console.log('  PASS', `${name} on ${platform}`);
      }
    });
  }
  console.log(`${machineNames.length} machines agree on ${PLATFORMS.length} platforms`);
} finally {
  process.chdir(startedIn);
  fs.rmSync(root, { recursive: true, force: true });
}

console.log('parity: the JavaScript and Python launchers plan identically');
for (const name of scenarioNames) {
  assert.deepEqual(plan(SCENARIOS[name]), fromPython.plans[name], `plans differ for ${name}`);
  console.log('  PASS', name);
}
console.log(`${scenarioNames.length} scenarios agree`);
