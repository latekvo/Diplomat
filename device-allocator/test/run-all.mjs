// Runs every test in this directory, discovered from disk.
//
// There used to be two hand-written lists of these files — `npm test` and the CI
// job — and they had drifted apart in both directions at once: CI never ran
// paths.mjs, and `npm test` never ran daemon-singleton-race.mjs or
// agent-kill-spares-supervisor.mjs. Nothing was failing, which is the problem: a
// test that exists but is never run looks exactly like a test that passes.
//
// So there is no list any more. A file dropped in test/ runs from that moment,
// and the only way to opt out is to not write it. Run: node test/run-all.mjs

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SELF = path.basename(__filename);

const files = fs.readdirSync(__dirname)
  .filter((f) => f.endsWith('.mjs') && f !== SELF)
  .sort();

// A glob that matches nothing reports "all tests passed", which is the one result
// this runner must never be able to produce by accident.
if (files.length === 0) {
  console.error(`FAILED: no test files found in ${__dirname}`);
  process.exit(1);
}

// Sequential, not parallel: each test drives a daemon over a unix socket, and
// while they sandbox themselves with DA_BASE_DIR, several also assert on process
// counts and reaper timing that a loaded machine perturbs.
const failed = [];
for (const [i, file] of files.entries()) {
  console.log(`\n===== [${i + 1}/${files.length}] ${file} =====`);
  const r = spawnSync(process.execPath, [path.join(__dirname, file)], {
    stdio: 'inherit',
    cwd: path.join(__dirname, '..'),
  });
  // A test killed by a signal exits with a null status — treat anything that is
  // not a clean 0 as a failure rather than only a non-zero number.
  if (r.status !== 0) {
    failed.push(`${file} (${r.signal ? `signal ${r.signal}` : `exit ${r.status}`})`);
  }
}

console.log(`\n===== ${files.length - failed.length}/${files.length} test files passed =====`);
if (failed.length > 0) {
  for (const f of failed) console.error(`  FAILED  ${f}`);
  process.exit(1);
}
