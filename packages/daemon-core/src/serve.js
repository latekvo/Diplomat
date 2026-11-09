// Serving a daemon's unix socket as the one daemon of its state dir.

import http from 'node:http';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { pidAlive } from './fsio.js';

// Is a *live* daemon already answering on the socket? (vs. a stale socket file
// left by a crashed one.) Resolves false on any connection error/timeout.
export function socketHealthy(socketPath) {
  return new Promise((resolve) => {
    const req = http.request(
      // 3s, not 1s: this probe also gates the stale-socket takeover — a live peer
      // whose event loop is briefly blocked must not get its socket unlinked.
      { socketPath, path: '/health', method: 'GET', timeout: 3000 },
      (res) => { res.resume(); resolve(res.statusCode === 200); },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.end();
  });
}

// A full disk loses the log line, so the client that spawned this daemon (spawnDaemon in
// ipc.js) also gets the reason over the IPC channel.
export function startFailed(log, ...why) {
  log(...why);
  if (process.connected) process.send(why.join(' '), () => process.exit(1));
  else process.exit(1);
}

// Startup-takeover lock. The stale-socket reclaim ("unlink the dead socket file, then
// re-listen") is unavoidably two steps, so two daemons that both see the socket as stale
// each unlink + bind — and the second unlinks the first's FRESHLY bound socket, leaving
// two live daemons (split-brain: each runs its own loops against the shared state
// files). Serialize the reclaim behind an exclusive lock so exactly one daemon takes
// over. Held only for the few ms of takeover, then released; a crash while holding it
// leaves a stale lock the next daemon reclaims by pid-liveness.
const LOCK_FRESH_MS = 5000; // far past the few ms a takeover holds it

function acquireTakeoverLock(lockPath) {
  // An open fd (lock held) or null if a LIVE daemon already holds it. Throws when the
  // lock cannot be created at all (a full disk), which is no sign of a holder.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(lockPath, 'wx'); // atomic O_CREAT | O_EXCL
      try { fs.writeSync(fd, String(process.pid)); } catch {}
      return fd;
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      let holder = 0;
      try { holder = Number(fs.readFileSync(lockPath, 'utf8').trim()) || 0; } catch {}
      if (holder && holder !== process.pid && pidAlive(holder)) return null; // a live holder
      // No pid yet: a peer between its create and its write, or one whose write a full
      // disk refused. Either is taking over right now, so a young empty lock is held.
      let age = Infinity;
      try { age = Date.now() - fs.statSync(lockPath).mtimeMs; } catch {}
      if (!holder && age < LOCK_FRESH_MS) return null;
      // Stale lock (holder dead/unreadable): drop it and retry the exclusive create once.
      // If another recoverer wins the re-create, our second attempt sees a live holder and
      // returns null (defers) — so the O_EXCL create, not the unlink, decides the winner.
      try { fs.unlinkSync(lockPath); } catch {}
    }
  }
  return null;
}

function releaseTakeoverLock(lockPath, fd) {
  try { fs.closeSync(fd); } catch {}
  try { fs.unlinkSync(lockPath); } catch {}
}

// Singleton acquisition without a pre-emptive unlink (which would clobber a live peer's
// socket). We let listen() fail with EADDRINUSE and use a /health probe to distinguish "a
// live daemon owns it" (defer + exit 0) from "a stale socket file remains" (take the
// lock, unlink + retry once). A second EADDRINUSE means a fresh daemon bound in the gap —
// defer to it rather than take over again.
export function listenSingleton({ server, socketPath, log, onListening }) {
  const lockPath = `${socketPath}.lock`;
  let retriedListen = false;
  let takeoverLock = null; // held across the takeover's re-listen
  server.on('error', async (e) => {
    // That re-listen failed (a peer bound in the gap, or a full disk): the lock goes too.
    if (takeoverLock != null) { releaseTakeoverLock(lockPath, takeoverLock); takeoverLock = null; }
    if (e.code === 'EADDRINUSE') {
      if (await socketHealthy(socketPath)) { log('a live daemon already owns the socket; exiting'); process.exit(0); }
      if (retriedListen) { log('could not acquire the socket (contended); deferring'); process.exit(0); }
      retriedListen = true;
      let lockFd;
      try { lockFd = acquireTakeoverLock(lockPath); }
      catch (err) { startFailed(log, 'cannot take the startup lock', String(err)); return; }
      if (lockFd == null) {
        // Another daemon is (or just finished) taking over. Give it a beat, then defer to it.
        await delay(300);
        if (await socketHealthy(socketPath)) { log('another daemon took the socket; exiting'); process.exit(0); }
        log('the startup lock is held by a live daemon; deferring'); process.exit(0);
      }
      // Re-probe UNDER the lock: a peer may have bound between our first probe and the lock.
      if (await socketHealthy(socketPath)) {
        releaseTakeoverLock(lockPath, lockFd);
        log('a live daemon already owns the socket; exiting'); process.exit(0);
      }
      try { fs.unlinkSync(socketPath); } catch {}
      takeoverLock = lockFd;
      server.listen(socketPath, () => {
        releaseTakeoverLock(lockPath, takeoverLock);
        takeoverLock = null;
        onListening();
      });
      return;
    }
    startFailed(log, 'server error', String(e));
  });
  server.listen(socketPath, onListening);
}

/// What a jsonServer handler returns for a route it does not serve.
export const NO_ROUTE = Symbol('no route');

/// Reads a JSON request body (capped at 1 MB) and answers with handle(route, body)'s
/// result, or the statusCode/message of what it throws. `route` is "METHOD /path".
export function jsonServer(handle) {
  return http.createServer((req, res) => {
    const send = (code, obj) => {
      res.writeHead(code, { 'content-type': 'application/json' });
      res.end(JSON.stringify(obj));
    };
    let body = '';
    // A StringDecoder, so a multi-byte character split across two 'data' chunks is
    // reassembled rather than turned into U+FFFD.
    req.setEncoding('utf8');
    req.on('data', (c) => {
      body += c;
      if (body.length > 1_000_000) req.destroy();
    });
    req.on('end', async () => {
      let p = {};
      try { p = body ? JSON.parse(body) : {}; } catch { return send(400, { error: 'invalid json' }); }
      try {
        const result = await handle(`${req.method} ${req.url}`, p);
        if (result === NO_ROUTE) return send(404, { error: `unknown route ${req.url}` });
        send(200, result ?? {});
      } catch (e) {
        send(e.statusCode || 500, { error: e.message || String(e) });
      }
    });
  });
}
