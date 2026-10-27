// A minimal RFC 6455 WebSocket client, plus the frame codec a test server reuses.
//
// Handed to remote pool providers (src/remote.js) for their tunnels. Not Node's built-in
// WebSocket: a refused handshake surfaces there as a bare 1006 with no status or body,
// which a provider cannot tell apart from "the network is down".

import http from 'node:http';
import https from 'node:https';
import crypto from 'node:crypto';
import { EventEmitter } from 'node:events';

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
export const OP = { TEXT: 1, BINARY: 2, CLOSE: 8, PING: 9, PONG: 10 };

export function encodeFrame(opcode, payload, mask) {
  const len = payload.length;
  const head = len < 126 ? 2 : len < 65536 ? 4 : 10;
  const keyLen = mask ? 4 : 0;
  const buf = Buffer.alloc(head + keyLen + len);
  buf[0] = 0x80 | opcode;
  if (len < 126) buf[1] = len;
  else if (len < 65536) { buf[1] = 126; buf.writeUInt16BE(len, 2); }
  else { buf[1] = 127; buf.writeBigUInt64BE(BigInt(len), 2); }
  if (mask) {
    buf[1] |= 0x80;
    const key = crypto.randomBytes(4);
    key.copy(buf, head);
    for (let i = 0; i < len; i++) buf[head + 4 + i] = payload[i] ^ key[i & 3];
  } else {
    payload.copy(buf, head);
  }
  return buf;
}

// Returns a feed(chunk) that calls onFrame(opcode, payload) once per whole message,
// joining fragments; control frames pass straight through.
export function frameParser(onFrame) {
  let buf = Buffer.alloc(0);
  let frag = null;
  let fragOp = 0;
  return (chunk) => {
    buf = buf.length ? Buffer.concat([buf, chunk]) : chunk;
    for (;;) {
      if (buf.length < 2) return;
      let len = buf[1] & 127;
      let off = 2;
      if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
      else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
      const masked = buf[1] & 128;
      if (buf.length < off + (masked ? 4 : 0) + len) return;
      const key = masked ? buf.subarray(off, off + 4) : null;
      if (masked) off += 4;
      const payload = Buffer.from(buf.subarray(off, off + len));
      if (key) for (let i = 0; i < len; i++) payload[i] ^= key[i & 3];
      const fin = buf[0] & 128;
      const op = buf[0] & 15;
      buf = buf.subarray(off + len);
      if (op >= 8) { onFrame(op, payload); continue; }
      if (op !== 0) { frag = [payload]; fragOp = op; } else if (frag) frag.push(payload); else continue;
      if (fin) { const whole = Buffer.concat(frag); frag = null; onFrame(fragOp, whole); }
    }
  };
}

// An open connection. Emits 'text' (string), 'binary' (Buffer) and, exactly once,
// 'close' (code, reason) — 1006 with no reason when the socket died without a close frame.
class Conn extends EventEmitter {
  constructor(socket, head) {
    super();
    this.socket = socket;
    this.closed = false;
    const feed = frameParser((op, payload) => {
      if (op === OP.TEXT) this.emit('text', payload.toString('utf8'));
      else if (op === OP.BINARY) this.emit('binary', payload);
      else if (op === OP.PING) this.#write(OP.PONG, payload);
      else if (op === OP.CLOSE) {
        const code = payload.length >= 2 ? payload.readUInt16BE(0) : 1005;
        this.#write(OP.CLOSE, payload.subarray(0, 2));
        this.#finish(code, payload.subarray(2).toString('utf8'));
      }
    });
    socket.setTimeout(0);
    socket.setNoDelay(true);
    socket.on('close', () => this.#finish(1006, ''));
    socket.on('error', () => {});
    // Frames that came in with the handshake are held until the caller, resumed by
    // connect()'s promise, has attached its listeners; the socket buffers behind them.
    setImmediate(() => {
      if (head?.length) feed(head);
      socket.on('data', feed);
    });
  }

  #write(op, payload) {
    if (!this.socket.destroyed) this.socket.write(encodeFrame(op, payload, true));
  }

  #finish(code, reason) {
    if (this.closed) return;
    this.closed = true;
    this.socket.end();
    // The server is meant to close the TCP connection next; one that never does would
    // otherwise hold this socket open for good.
    setTimeout(() => this.socket.destroy(), 1000).unref();
    this.emit('close', code, reason);
  }

  send(data) {
    if (this.closed) return;
    if (typeof data === 'string') this.#write(OP.TEXT, Buffer.from(data, 'utf8'));
    else this.#write(OP.BINARY, data);
  }

  close(code = 1000) {
    if (this.closed) return;
    const p = Buffer.alloc(2);
    p.writeUInt16BE(code, 0);
    this.#write(OP.CLOSE, p);
    this.#finish(code, '');
    this.socket.destroy();
  }
}

// Resolves an open Conn on 101. Rejects with `status` and the parsed `body` when the
// server answers anything else, and with `network: true` when it cannot be reached.
export function connect(url, headers, { timeoutMs = 10000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const key = crypto.randomBytes(16).toString('base64');
    const req = (u.protocol === 'https:' ? https : http).request({
      hostname: u.hostname, port: u.port || undefined, path: `${u.pathname}${u.search}`,
      method: 'GET', timeout: timeoutMs,
      headers: {
        Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13',
        'Sec-WebSocket-Key': key, ...headers,
      },
    });
    req.on('upgrade', (res, socket, head) => {
      const want = crypto.createHash('sha1').update(key + GUID).digest('base64');
      if (res.headers['sec-websocket-accept'] !== want) {
        socket.destroy();
        reject(Object.assign(new Error('bad websocket handshake'), { status: 101 }));
        return;
      }
      resolve(new Conn(socket, head));
    });
    req.on('response', (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (c) => (raw += c));
      res.on('end', () => {
        let body = {};
        try { body = JSON.parse(raw); } catch {}
        reject(Object.assign(new Error(body.error || `HTTP ${res.statusCode}`), { status: res.statusCode, body }));
      });
    });
    req.on('timeout', () => req.destroy(new Error('timed out')));
    req.on('error', (e) => reject(Object.assign(new Error(e.code || e.message), { network: true })));
    req.end();
  });
}
