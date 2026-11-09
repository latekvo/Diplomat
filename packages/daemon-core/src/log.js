import fs from 'node:fs';
import path from 'node:path';

// Append-only line log. Cheap and crash-safe; the daemon is the only heavy writer.
export function createLog(logPath) {
  try { fs.mkdirSync(path.dirname(logPath), { recursive: true }); } catch {}
  return function log(...args) {
    const parts = args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a)));
    const line = `[${new Date().toISOString()}] ${parts.join(' ')}\n`;
    try { fs.appendFileSync(logPath, line); } catch {}
  };
}
