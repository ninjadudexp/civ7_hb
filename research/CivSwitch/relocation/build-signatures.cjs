#!/usr/bin/env node
// Generate nn-sdk-signatures.json from ns2jb's verified SDK bytes.
//
// ns2jb stores full verified bodies (engine/shared/sdk-functions.json,
// engine/switch2/app/*-code.json). We keep the full bytes for exact search and
// derive a short, non-prologue ANCHOR for partial search when a firmware build
// has drifted. Anchors are chosen after the common stp prologue so a matching
// anchor localizes the function even when the prologue is shared.
//
// Usage:  node build-signatures.cjs [path-to-ns2jb-engine]
// Output: relocation/signatures/nn-sdk-signatures.json

const fs = require("node:fs");
const path = require("node:path");

const engine = process.argv[2] || path.join(__dirname, "../../ns2jb/engine");
const outDir = path.join(__dirname, "signatures");
const outFile = path.join(outDir, "nn-sdk-signatures.json");

// name -> why it matters for the Civ VII port and which milestone needs it.
const PURPOSE = {
  allocate: ["M1", "heap allocation (malloc replacement); worker buffers and staging pages"],
  free: ["M1", "heap free"],
  memcpy: ["M1", "bulk copy for staging and header installs"],
  memset: ["M1", "buffer init and shared-memory gates"],
  getService: ["M1", "SDK service-manager client (GetService through a stolen SM handle)"],
  send: ["M1", "synchronous IPC send wrapper"],
  close: ["M1", "handle close wrapper"],
  socket: ["M1", "loader / control sockets"],
  connect: ["M1", "loader / control sockets"],
  closeSocket: ["M1", "loader / control sockets"],
  poll: ["M1", "loader / control sockets"],
  fcntl: ["M1", "loader / control sockets"],
  errno: ["M1", "socket error reporting"],
  tick: ["M1", "native tick counter for wave spacing"],
  frequency: ["M1", "tick frequency"],
  priority: ["M1", "thread priority"],
  create: ["M1", "native worker thread creation"],
  join: ["M1", "native worker join"],
  exit: ["M1", "native worker exit"],
  current: ["M1", "current thread identity"],
  sleep: ["M1", "thread sleep"],
  longjmp: ["M1", "thread trampoline / context switch"],
  loadTable: ["M1", "SDK call-table indirection"],
  call4: ["M1", "SDK call with four arguments"],
  tryWait: ["M1", "non-blocking event wait"],
  tryWaitImplementation: ["M1", "non-blocking event wait implementation"],
  saveResult: ["M1", "SDK call result store"],
  saveResult64: ["M1", "SDK call result store"],
  initialize: ["M1", "event initialize"],
  finalize: ["M1", "event finalize"],
  wait: ["M1", "event wait"],
  signal: ["M1", "event signal"],
  clear: ["M1", "event clear"],
  waitAddress: ["M1", "resolved svc wait wrapper"],
  signalAddress: ["M1", "resolved svc signal wrapper"],
  "sdk-proxy": ["M1", "SDK dispatcher used by table-interpreted calls"],
  hash: ["M1", "GenerateSha256Hash, used to verify captured files"],
};

// SVC wrappers. Switch 1 numbers come from libnx; Switch 2 alternates come from
// ns2jb's payload (start.S) and the SmileBASIC SDK. `code` is the ns2jb-observed
// `svc #imm; ret` encoding where available.
const SVC = [
  { name: "queryMemory", s1: 0x06, s2: null },
  { name: "createThread", s1: 0x08, s2: null },
  { name: "sleepThread", s1: 0x0b, s2: null },
  { name: "clearEvent", s1: 0x12, s2: null, code: "410200d4c0035fd6" },
  { name: "signalEvent", s1: 0x11, s2: null, code: "210200d4c0035fd6" },
  { name: "closeHandle", s1: 0x16, s2: 0x2c1, code: "c10200d4c0035fd6" },
  { name: "waitSynchronization", s1: 0x18, s2: null },
  { name: "cancelSynchronization", s1: 0x19, s2: null, code: "210300d4c0035fd6" },
  { name: "getSystemTick", s1: 0x1e, s2: null },
  { name: "connectToNamedPort", s1: 0x1f, s2: null },
  { name: "sendSyncRequest", s1: 0x21, s2: 0x441, code: "410400d4c0035fd6" },
  { name: "sendSyncRequestWithUserBuffer", s1: 0x22, s2: null },
  { name: "sendAsyncRequestWithUserBuffer", s1: 0x23, s2: null },
  { name: "getInfo", s1: 0x29, s2: null },
  { name: "createEvent", s1: 0x45, s2: null },
];

function readJson(file) {
  return JSON.parse(fs.readFileSync(path.join(engine, file), "utf8"));
}

function isHex(value) {
  return typeof value === "string" && value.length >= 2 && value.length % 2 === 0 && /^[0-9a-fA-F]+$/.test(value);
}

// Collect {name, offset, bytes, source} from every ns2jb code table.
function collect() {
  const rows = [];
  const push = (name, value, source) => {
    if (!value || typeof value.bytes !== "string") return;
    const offset = value.offset ?? value.functionOffset ?? null;
    if (offset === null) return;
    rows.push({ name, offset, bytes: value.bytes.toLowerCase(), source });
  };

  const sdk = readJson("shared/sdk-functions.json");
  for (const [name, value] of Object.entries(sdk)) push(name, value, "shared/sdk-functions.json");

  const thread = readJson("switch2/app/thread-code.json");
  for (const [name, value] of Object.entries(thread.functions || {})) push(name, value, "switch2/app/thread-code.json");

  const timing = readJson("switch2/app/timing-code.json");
  for (const [name, value] of Object.entries(timing.functions || {})) push(name, value, "switch2/app/timing-code.json");

  const event = readJson("switch2/app/event-code.json");
  for (const [name, value] of Object.entries(event.functions || {})) push(name, value, "switch2/app/event-code.json");

  const hash = readJson("switch2/app/hash-code.json");
  push("hash", { offset: hash.offset, bytes: hash.bytes }, "switch2/app/hash-code.json");

  const proxy = readJson("switch2/app/sdk-proxy-code.json");
  push("sdk-proxy", { offset: proxy.functionOffset, bytes: proxy.bytes }, "switch2/app/sdk-proxy-code.json");

  return rows;
}

// A common AArch64 prologue that several functions share. Anchors that begin
// with it are weak, so prefer a window after it.
const PROLOGUE = "fd7bbca9f85f01a9fd030091f65702a9f44f03a9fd7bbea9f44f01a9fd030091";

function anchorFor(bytes, length = 20) {
  const total = bytes.length / 2;
  if (total <= length) return { anchorOffset: 0, anchorBytes: bytes, note: "whole body (short)" };
  const candidates = [];
  for (const off of [16, 32, 8, 4, 0]) {
    if (off + length > total) continue;
    const window = bytes.slice(off * 2, (off + length) * 2);
    const weak = PROLOGUE.includes(bytes.slice(off * 2, off * 2 + Math.min(PROLOGUE.length, (total - off) * 2)));
    candidates.push({ anchorOffset: off, anchorBytes: window, weak });
  }
  // Prefer the first non-weak window at offset 16 or beyond, else fall back.
  return candidates.find(c => !c.weak && c.anchorOffset >= 16) || candidates[0];
}

function main() {
  const rows = collect();
  const functions = rows.map(row => {
    const { anchorOffset, anchorBytes, note } = anchorFor(row.bytes);
    const [milestone, purpose] = PURPOSE[row.name] || ["M1", "SDK primitive"];
    const bodyLen = row.bytes.length / 2;
    return {
      name: row.name,
      milestone,
      purpose,
      source: row.source,
      offset: row.offset,
      offsetHex: "0x" + row.offset.toString(16),
      bytes: row.bytes,
      bodyBytes: bodyLen,
      // Exact byte search is only meaningful for non-trampoline bodies.
      exactReliable: bodyLen >= 12,
      anchorOffset,
      anchorBytes,
      ...(note ? { note } : {}),
    };
  });

  functions.sort((a, b) => a.name.localeCompare(b.name));

  const document = {
    generatedFrom: "research/ns2jb @ 95f5525 (engine/)",
    generatedAt: new Date().toISOString(),
    notes: [
      "bytes are the full ns2jb-verified body; scan for these first.",
      "anchorBytes is a shorter window for drifted builds; the candidate base is matchOffset - anchorOffset.",
      "exactReliable is false for branches/trampolines whose encoded target is build-specific.",
      "SVC wrappers are located by svc-immediate scan, not by bytes, because the encoding is stable.",
    ],
    svc: SVC,
    functions,
  };

  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(outFile, JSON.stringify(document, null, 2) + "\n");
  console.log(`wrote ${outFile}: ${functions.length} functions, ${SVC.length} svc wrappers`);
  const weak = functions.filter(f => !f.exactReliable).map(f => f.name);
  if (weak.length) console.log("short/trampoline (exact match unreliable): " + weak.join(", "));
}

main();
