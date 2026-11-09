// The client every MCP server instance and the installer use to start the daemon and
// talk to it over its unix socket.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createDaemonClient } from '../../daemon-core/src/ipc.js';
import { SOCKET_PATH, BASE_DIR, LOG_PATH } from './paths.js';

export const { callDaemon, daemonHealth, spawnDaemon } = createDaemonClient({
  socketPath: SOCKET_PATH,
  daemonJs: path.join(path.dirname(fileURLToPath(import.meta.url)), 'daemon.js'),
  baseDir: BASE_DIR,
  logPath: LOG_PATH,
  label: 'remote-machines',
});
