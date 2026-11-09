// The shared interrupt helpers, bound to this package's daemon and DA_BASE_DIR.

import { fileURLToPath } from 'node:url';
import * as core from '../../../daemon-core/src/test-interrupt.js';

const DAEMON_JS = fileURLToPath(new URL('../../src/daemon.js', import.meta.url));

export const { onInterrupt } = core;
export const daemonsUnder = (base) => core.daemonsUnder(DAEMON_JS, 'DA_BASE_DIR', base);
export const killDaemonsUnder = (base) => core.killDaemonsUnder(DAEMON_JS, 'DA_BASE_DIR', base);
