// Runs every test in this directory, discovered from disk (see daemon-core's
// test-runner.js for why there is no list). Run: node test/run-all.mjs

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { runTests } from '../../daemon-core/src/test-runner.js';

const file = fileURLToPath(import.meta.url);
await runTests(path.dirname(file), path.basename(file), 'DA_TEST_JOBS');
