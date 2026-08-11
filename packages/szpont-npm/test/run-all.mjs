// Runs every test in this directory, discovered from disk — the same runner the
// device-allocator package uses, and for the same reason: a hand-written list is
// a place for a test to quietly stop being run, which looks exactly like a test
// that passes. A file dropped in test/ runs from that moment.
//
// scenarios.mjs is data, not a test, and is excluded by name; everything else
// here is expected to exit 0 on its own. Run: node test/run-all.mjs

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const NOT_TESTS = new Set([path.basename(__filename), 'scenarios.mjs']);

const files = fs.readdirSync(__dirname).filter((f) => f.endsWith('.mjs') && !NOT_TESTS.has(f)).sort();

// A glob that matches nothing reports "all tests passed", which is the one result
// this runner must never be able to produce by accident.
if (files.length === 0) {
  console.error(`FAILED: no test files found in ${__dirname}`);
  process.exit(1);
}

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
