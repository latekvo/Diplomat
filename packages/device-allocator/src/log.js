import fs from 'node:fs';
import { LOG_PATH, BASE_DIR } from './paths.js';

try { fs.mkdirSync(BASE_DIR, { recursive: true }); } catch {}

// Append-only line log. Cheap and crash-safe; the daemon is the only heavy writer.
export function log(...args) {
  const parts = args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a)));
  const line = `[${new Date().toISOString()}] ${parts.join(' ')}\n`;
  try { fs.appendFileSync(LOG_PATH, line); } catch {}
}
