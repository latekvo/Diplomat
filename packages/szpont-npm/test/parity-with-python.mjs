// The two `szpont` packages plan the same run.
//
// `npm i -g szpont` and `pip install szpont` install the same name from two
// indexes, and the promise attached to that name is that it does the same thing.
// Nothing enforces it structurally — they are two files in two languages — so it
// is enforced here: every scenario in scenarios.mjs goes through both plan()
// implementations and the two answers must be identical, key for key.
//
// Run: node test/parity-with-python.mjs

import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { plan } from '../src/launcher.js';
import { SCENARIOS } from './scenarios.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PY_PKG = path.resolve(__dirname, '..', '..', 'szpont');

// One interpreter run for the whole table: starting python twenty times to ask
// twenty questions is most of this test's runtime.
const script = `
import json, sys
import szpont_launcher
print(json.dumps({k: szpont_launcher.plan(v) for k, v in json.load(sys.stdin).items()}))
`;

const py = spawnSync('python3', ['-c', script], {
  input: JSON.stringify(SCENARIOS),
  encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: PY_PKG },
});

// A parity test that cannot reach the other implementation has proved nothing,
// so it fails rather than skips.
assert.equal(py.status, 0,
  `could not run the Python twin (python3 with PYTHONPATH=${PY_PKG}):\n${py.stderr || py.error}`);

const fromPython = JSON.parse(py.stdout);
const names = Object.keys(SCENARIOS);
assert.deepEqual(Object.keys(fromPython).sort(), [...names].sort(), 'the twin skipped a scenario');

console.log('parity: the JavaScript and Python launchers plan identically');
for (const name of names) {
  assert.deepEqual(plan(SCENARIOS[name]), fromPython[name], `plans differ for ${name}`);
  console.log('  PASS', name);
}
console.log(`${names.length} scenarios agree`);
