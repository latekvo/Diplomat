// The idle signal must not see the status bar: its clock changes every minute on an
// otherwise static screen, so a hash over the whole frame never stays equal long
// enough for any idle limit to fire. A change in the top band keeps the hash; a change
// anywhere below it moves it. Covers both frame layouts motionHash reads: Android's
// raw `screencap` (12- and 16-byte headers) and simctl's BMP (bottom-up and top-down).
// Run: node test/motion-hash-skips-status-bar.mjs

import assert from 'node:assert/strict';
import { rawFrameHash, bmpFrameHash } from '../src/devices.js';

const W = 8;
const H = 100;
const STATUS_ROW = 2; // inside the top band
const CONTENT_ROW = 50;

function raw(hdr) {
  const b = Buffer.alloc(hdr + W * H * 4);
  b.writeUInt32LE(W, 0);
  b.writeUInt32LE(H, 4);
  b.writeUInt32LE(1, 8);
  return { buf: b, at: (y) => hdr + y * W * 4 };
}

function bmp(topDown) {
  const off = 54;
  const row = W * 3 + ((4 - ((W * 3) % 4)) % 4); // 24-bit rows, padded to 4 bytes
  const b = Buffer.alloc(off + row * H);
  b.write('BM', 0, 'latin1');
  b.writeUInt32LE(b.length, 2);
  b.writeUInt32LE(off, 10);
  b.writeUInt32LE(40, 14);
  b.writeInt32LE(W, 18);
  b.writeInt32LE(topDown ? -H : H, 22);
  b.writeUInt16LE(1, 26);
  b.writeUInt16LE(24, 28);
  return { buf: b, at: (y) => off + (topDown ? y : H - 1 - y) * row };
}

function check(name, make, hash) {
  const base = make();
  const h0 = hash(base.buf);
  const status = make();
  status.buf[status.at(STATUS_ROW)] = 0xff;
  assert.equal(hash(status.buf), h0, `${name}: a status-bar change moved the hash`);
  const content = make();
  content.buf[content.at(CONTENT_ROW)] = 0xff;
  assert.notEqual(hash(content.buf), h0, `${name}: a content change left the hash`);
  console.log(`ok  ${name}`);
}

check('raw screencap, 16-byte header', () => raw(16), rawFrameHash);
check('raw screencap, 12-byte header', () => raw(12), rawFrameHash);
check('bmp, bottom-up', () => bmp(false), bmpFrameHash);
check('bmp, top-down', () => bmp(true), bmpFrameHash);

// A frame that doesn't parse is hashed whole rather than dropped.
const odd = Buffer.from('not a frame at all, just bytes');
assert.notEqual(bmpFrameHash(odd), bmpFrameHash(Buffer.from('not a frame at all, just bytez')));
console.log('ok  an unparseable frame is hashed whole');
