// Filesystem layout, tools and tunables. Everything the daemon owns lives under
// ~/.diplomat/remote-machines; RM_BASE_DIR redirects it, which is how the tests never
// touch the real table.

import os from 'node:os';
import path from 'node:path';

export const BASE_DIR = process.env.RM_BASE_DIR
  ? process.env.RM_BASE_DIR.replace(/^~(?=$|\/)/, os.homedir())
  : path.join(os.homedir(), '.diplomat', 'remote-machines');

export const SOCKET_PATH = path.join(BASE_DIR, 'daemon.sock');
export const DISCOVERY_PATH = path.join(BASE_DIR, 'daemon.json'); // {pid, identity, startedAt, socket}
export const STATE_PATH = path.join(BASE_DIR, 'state.json'); // the public snapshot
export const HISTORY_PATH = path.join(BASE_DIR, 'history.json'); // each machine's usage over time
export const IDS_PATH = path.join(BASE_DIR, 'machine-ids.json'); // {candidate key: machine id}
export const LOG_PATH = path.join(BASE_DIR, 'daemon.log');

// Diplomat's cross-process settings, relocated by the same DIPLOMAT_CONFIG the
// applets honour: remoteMachineHosts, remoteMachineExclude, remoteMachineExcludeUntil and
// remoteMachineArmBias.
export const CONFIG_PATH = process.env.DIPLOMAT_CONFIG
  ? process.env.DIPLOMAT_CONFIG.replace(/^~(?=$|\/)/, os.homedir())
  : path.join(os.homedir(), '.diplomat', 'config.json');

// ARM's share, in percent, of the ranking's weight when remoteMachineArmBias is unset.
export const ARM_BIAS_DEFAULT = 70;

export const SSH_BIN = process.env.RM_SSH || 'ssh';
export const SSH_KEYGEN_BIN = process.env.RM_SSH_KEYGEN || 'ssh-keygen';
// The ssh client config: its Host aliases are discovery candidates, and every ssh
// call reads it with -F when this is set, so a test can never reach a real host.
export const SSH_CONFIG = process.env.RM_SSH_CONFIG || null;
export const USER_SSH_CONFIG = SSH_CONFIG || path.join(os.homedir(), '.ssh', 'config');
// '' turns mDNS browsing off; otherwise it is the browse time.
export const MDNS_BROWSE_MS = process.env.RM_MDNS_MS === '' ? 0 : Number(process.env.RM_MDNS_MS) || 3000;

// The load of every machine that has answered is re-read this often.
export const PROBE_INTERVAL_MS = Number(process.env.RM_PROBE_INTERVAL_MS) || 60 * 1000;
// Candidates are re-discovered, capabilities re-read and never-answering ones retried, this often.
export const DISCOVERY_INTERVAL_MS = Number(process.env.RM_DISCOVERY_INTERVAL_MS) || 5 * 60 * 1000;
// The pause before a machine that answered is probed again after a quick failure.
export const RETRY_DELAY_MS = Number(process.env.RM_RETRY_DELAY_MS) || 1000;
// One probe's ssh connection, script and all, is killed past this.
export const PROBE_TIMEOUT_MS = Number(process.env.RM_PROBE_TIMEOUT_MS) || 20 * 1000;
// Two load readings closer than this are dominated by which 5-second kernel tick each
// fell after, so "load since the last scan" keeps its previous value instead.
export const LOAD_GAP_MIN_MS = Number(process.env.RM_LOAD_GAP_MIN_MS) || 45 * 1000;
// The daemon exits once no agent session has been connected for this long; the next
// session's MCP server starts it again.
export const IDLE_EXIT_MS = Number(process.env.RM_IDLE_EXIT_MS) || 10 * 60 * 1000;
// How far back history.json reaches.
export const HISTORY_MS = Number(process.env.RM_HISTORY_MS) || 24 * 60 * 60 * 1000;
// How often the settings file is checked for a changed exclusion.
export const CONFIG_POLL_MS = Number(process.env.RM_CONFIG_POLL_MS) || 1000;
export const CLIENT_CHECK_MS = Number(process.env.RM_CLIENT_CHECK_MS) || 30 * 1000;
