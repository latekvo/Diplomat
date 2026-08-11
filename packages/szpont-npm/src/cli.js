#!/usr/bin/env node
// The `szpont` / `npx szpont` entry point. Everything it does is in launcher.js,
// which the tests import directly — this file exists so that the bin has no logic
// of its own to test.

import { main } from './launcher.js';

process.exitCode = main(process.argv.slice(2));
