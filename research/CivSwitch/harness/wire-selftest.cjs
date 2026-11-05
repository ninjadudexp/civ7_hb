// Byte-parity selftest for the Civ VII wire codecs.
// Builds the same frames with ns2jb's own codecs and with harness/host.py's Python
// port, then the two JSON dumps are diffed. No console, no network.
const crypto = require("crypto");
const path = require("path");
const fs = require("node:fs");

const ipc = require(path.join(__dirname, "../../ns2jb/engine/shared/ipc.cjs"));
const domain = require(path.join(__dirname, "../../ns2jb/engine/shared/domain.cjs"));
const paths = require(path.join(__dirname, "../../ns2jb/engine/switch2/memory/paths.cjs"));

const buf32 = v => {const b = Buffer.alloc(4); b.writeUInt32LE(v); return b;};
const buf64 = v => {const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(v)); return b;};
const hex = b => b.toString("hex");

const A = 0x12340000, B = 0x10000000, C = 0x24000000;
const cases = [];
const add = (name, bytes) => cases.push({name, hex: hex(bytes), sha: crypto.createHash("sha256").update(bytes).digest("hex")});

const wire = domain;
add("commit-request", ipc.packet(10, buf64(0), {send: [{address: A, size: 0x800}]}));
add("hole-request", ipc.packet(0, Buffer.concat([buf64(0), buf64(0x180000)]),
  {receive: [{address: B, size: 0x180000, mode: 1}]}));
add("initialize", ipc.packet(1, buf64(0), {pid: true}));
add("clone", ipc.packet(2));
add("copy-handle", ipc.packet(11, buf32(7), {copies: [7], send: [{address: A, size: 0x800}]}));
// The context marker is local to ns2jb's gap probe (probe-reserved-gap.cjs line 74),
// not part of shared/domain.cjs, so it is replicated here for parity checking.
const applyContext = (bytes, value = 9) => {
  const at = domain.dataStart(bytes);
  bytes.writeUInt32LE((bytes.readUInt32LE() & 0xffff0000) | 6);
  bytes.writeUInt32LE(1, at + 4);
  bytes.writeUInt32LE(value, at + 12);
  return bytes;
};

add("domain-counters", wire.domain(1, 1009));
add("domain-create", wire.domain(1, 1200));
add("domain-control", wire.control(0));
add("domain-copy-object", wire.control(1, buf32(3)));
add("domain-close-object", wire.closeObject(5));
add("domain-close-context", wire.closeObject(5, 3));
add("sequential-counters", applyContext(wire.domain(1, 1009), 9));
add("concurrent-counters", applyContext(wire.domain(1, 1009), 3));
add("path-query", paths.pathPacket(7, Buffer.alloc(0), A, 0x800));
add("path-append", paths.pathPacket(1002, Buffer.alloc(0), A, 16));
add("path-two-descriptors", paths.pathPackets(5, Buffer.alloc(0), [{address: A, size: 16}, {address: C, size: 0x7f0}]));
add("path-with-input", paths.pathPacket(0, Buffer.concat([buf64(0), buf64(1)]), A, 0x300));

// Crafted replies, identical bytes in both languages, to compare parser behaviour.
function craftReply({objects = 0, dataSize = 0, copies = [], domainReply = false} = {}) {
  const bytes = Buffer.alloc(256);
  bytes.writeUInt32LE(4, 0);
  let at = 8;
  if (copies.length) {
    bytes.writeUInt32LE(0x80000000, 4);
    bytes.writeUInt32LE(copies.length << 1, 8);
    at += 4;
    for (const handle of copies) {bytes.writeUInt32LE(handle, at); at += 4;}
  }
  at = (at + 15) & ~15;
  if (domainReply) {
    bytes.writeUInt32LE(objects, at);
    bytes.writeUInt32LE(0, at + 8);
    bytes.write("SFCO", at + 16);
    bytes.writeUInt32LE(0, at + 24);
    at += 32 + dataSize;
    for (let i = 0; i < objects; i++) bytes.writeUInt32LE(10 + i, at + 4 * i);
    return bytes;
  }
  bytes.write("SFCO", at);
  bytes.writeUInt32LE(0, at + 8);
  return bytes;
}

const norm = parsed => ({result: parsed.result, canonical: parsed.canonicalResult,
  copies: parsed.copies, moves: parsed.moves, objects: parsed.objects ?? [],
  dataHex: parsed.data.subarray(0, 16).toString("hex")});

const replies = [];
for (const spec of [
  {name: "plain-reply", objects: 0, dataSize: 0},
  {name: "reply-with-copies", objects: 0, dataSize: 0, copies: [7, 8]},
  {name: "domain-reply", objects: 2, dataSize: 16, domainReply: true},
  {name: "domain-reply-context", objects: 1, dataSize: 128, copies: [9], domainReply: true}
]) {
  const bytes = craftReply(spec);
  const parsed = spec.domainReply
    ? domain.response(bytes, {domain: true, dataSize: spec.dataSize})
    : ipc.response(bytes, {});
  replies.push({name: spec.name, hex: hex(bytes), sha: crypto.createHash("sha256").update(bytes).digest("hex"),
    parsed: norm(parsed)});
}

fs.writeFileSync(path.join(__dirname, "selftest-js.json"),
  JSON.stringify({cases, replies}, (_, v) => typeof v === "bigint" ? v.toString() : v, 2) + "\n");
console.log(JSON.stringify({frames: cases.length, replies: replies.length}));
