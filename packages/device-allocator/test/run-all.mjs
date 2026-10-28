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

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
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

// Concurrent: every test sandboxes itself (DA_BASE_DIR, HOME) and mostly waits on a
// daemon. DA_TEST_JOBS=1 runs them one at a time. Output is held per file, so the log
// never interleaves.
const jobs = Math.max(1, Number(process.env.DA_TEST_JOBS) || Math.min(6, os.availableParallelism()));
// Largest first (size tracks run time), so the longest never starts last.
const queue = [...files].sort((a, b) => fs.statSync(path.join(__dirname, b)).size - fs.statSync(path.join(__dirname, a)).size);

const failed = [];
let done = 0;
const running = new Map(); // file -> { child, out }
let stopped = false;
function run(file) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(__dirname, file)], {
      stdio: ['ignore', 'pipe', 'pipe'],
      cwd: path.join(__dirname, '..'),
    });
    const out = [];
    running.set(file, { child, out });
    child.stdout.on('data', (b) => out.push(b));
    child.stderr.on('data', (b) => out.push(b));
    child.on('close', (status, signal) => {
      running.delete(file);
      console.log(`\n===== [${++done}/${files.length}] ${file} =====`);
      process.stdout.write(Buffer.concat(out));
      // A test killed by a signal exits with a null status — treat anything that is
      // not a clean 0 as a failure rather than only a non-zero number.
      if (status !== 0) failed.push(`${file} (${signal ? `signal ${signal}` : `exit ${status}`})`);
      resolve();
    });
  });
}
// A runner stopped from outside (a CI timeout, ^C) starts nothing more, passes the
// signal on as a process-group signal would, and waits for each test's output: one
// left writing into a closed pipe would die without cleaning up, and its held output
// is all that says how far it got.
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, async () => {
    stopped = true;
    queue.length = 0;
    console.log(`\n===== ${sig}: stopping ${[...running.keys()].join(', ') || 'nothing'} =====`);
    const closed = [...running.values()].map(({ child }) => new Promise((r) => child.once('close', r)));
    for (const { child } of running.values()) child.kill(sig);
    await Promise.race([Promise.all(closed), new Promise((r) => setTimeout(r, 10000).unref())]);
    for (const [file, { out }] of running) {
      console.log(`\n===== ${file} did not stop =====`);
      process.stdout.write(Buffer.concat(out));
    }
    process.exit(1);
  });
}
await Promise.all(Array.from({ length: Math.min(jobs, queue.length) }, async () => {
  while (queue.length > 0) await run(queue.shift());
}));
if (stopped) await new Promise(() => {}); // the signal handler reports and exits

console.log(`\n===== ${files.length - failed.length}/${files.length} test files passed =====`);
if (failed.length > 0) {
  for (const f of failed) console.error(`  FAILED  ${f}`);
  process.exit(1);
}
