// src/wsock.js, the WebSocket client handed to remote pool providers, against a raw
// server: a frame sent in the same packet as the handshake still reaches the caller,
// a refused handshake keeps its status and body, an unreachable server is `network`,
// and messages, pings, fragments and closes come through as the RFC has them.
// Run: node test/wsock.mjs

import http from 'node:http';
import net from 'node:net';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { connect, encodeFrame, frameParser, OP } from '../src/wsock.js';

const server = http.createServer((req, res) => {
  res.writeHead(404, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ error: 'no such lease' }));
});
let onSocket;
server.on('upgrade', (req, socket) => {
  if (req.headers.authorization !== 'Bearer good') {
    const body = req.headers.authorization === 'Bearer null' ? 'null' : JSON.stringify({ error: 'no such lease' });
    socket.end(`HTTP/1.1 410 Gone\r\ncontent-length: ${body.length}\r\n\r\n${body}`);
    return;
  }
  const accept = crypto.createHash('sha1')
    .update(`${req.headers['sec-websocket-key']}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest('base64');
  // The greeting goes out in the same write as the 101, so it arrives with the handshake.
  socket.write(Buffer.concat([
    Buffer.from(`HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`),
    encodeFrame(OP.TEXT, Buffer.from('hello')),
  ]));
  onSocket(socket);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/ws`;
let ok = 0;
const pass = (m) => { ok++; console.log(`  PASS ${m}`); };

// Server side of each test: the frames the client sent, unmasked.
function serve() {
  const got = [];
  let socket;
  const ready = new Promise((r) => { onSocket = (s) => { socket = s; s.on('data', frameParser((op, p) => got.push([op, p]))); r(); }; });
  return { got, ready, send: (op, p) => socket.write(encodeFrame(op, p)), raw: (b) => socket.write(b), socket: () => socket };
}

{
  const srv = serve();
  const ws = await connect(url, { authorization: 'Bearer good' });
  const first = await new Promise((r) => ws.once('text', r));
  assert.equal(first, 'hello');
  pass('a frame that arrives with the handshake is delivered to a listener attached after connect');

  await srv.ready;
  ws.send('ping');
  ws.send(Buffer.from([1, 2, 3]));
  const big = crypto.randomBytes(70000);
  ws.send(big);
  await new Promise((r) => setTimeout(r, 100));
  assert.deepEqual(srv.got.map(([op, p]) => [op, p.length]), [[OP.TEXT, 4], [OP.BINARY, 3], [OP.BINARY, 70000]]);
  assert.ok(srv.got[2][1].equals(big));
  pass('text, binary and 64-bit-length frames go out masked and arrive intact');

  const bins = [];
  ws.on('binary', (b) => bins.push(b));
  // A fragmented message (text start + continuation) with a ping between the pieces.
  const startFrag = encodeFrame(OP.TEXT, Buffer.from('par'));
  startFrag[0] &= 0x7f;
  const texts = [];
  ws.on('text', (t) => texts.push(t));
  srv.raw(Buffer.concat([startFrag, encodeFrame(OP.PING, Buffer.from('p')), encodeFrame(0, Buffer.from('ted')),
    encodeFrame(OP.BINARY, Buffer.from('xyz'))]));
  await new Promise((r) => setTimeout(r, 100));
  assert.deepEqual(texts, ['parted']);
  assert.deepEqual(bins.map(String), ['xyz']);
  assert.deepEqual(srv.got.slice(3).map(([op, p]) => [op, String(p)]), [[OP.PONG, 'p']]);
  pass('fragments are joined, and a ping between them is answered with a pong');

  const closed = new Promise((r) => ws.once('close', (code, reason) => r([code, reason])));
  srv.send(OP.CLOSE, Buffer.concat([Buffer.from([0x03, 0xe8]), Buffer.from('expired')]));
  assert.deepEqual(await closed, [1000, 'expired']);
  pass('a close frame surfaces its code and reason');
}

{
  const srv = serve();
  const ws = await connect(url, { authorization: 'Bearer good' });
  await srv.ready;
  const closed = new Promise((r) => ws.once('close', (code, reason) => r([code, reason])));
  srv.socket().destroy();
  assert.deepEqual(await closed, [1006, '']);
  pass('a socket that dies without a close frame is a 1006');
}

await assert.rejects(connect(url, { authorization: 'Bearer stale' }),
  (e) => e.status === 410 && e.body.error === 'no such lease' && e.message === 'no such lease' && !e.network);
pass('a refused handshake keeps its status and body');
await assert.rejects(connect(url, { authorization: 'Bearer null' }), (e) => e.status === 410 && e.message === 'HTTP 410');
pass('a refused handshake whose body is JSON null is still a plain rejection');

const closedPort = await new Promise((r) => { const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
await assert.rejects(connect(`http://127.0.0.1:${closedPort}/ws`, {}), (e) => e.network === true && e.message === 'ECONNREFUSED');
pass('an unreachable server is a network error');

// Nothing may be left holding the process open: the test server never closes the
// sockets of closed connections itself, so their client ends must have let go.
server.close();
server.closeIdleConnections();
console.log(`\nwsock: ${ok} checks passed`);
