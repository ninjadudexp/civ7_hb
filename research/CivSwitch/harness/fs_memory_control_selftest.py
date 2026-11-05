"""Offline selftest for fs_memory_control.FsMemoryControl (P1).

Runs the controller core against a fake console + fake DMA channel — no
console, no payload, no waves. The wave-dependent primitives (copyField)
are stubbed with a recording fake. Verifies the cjs-faithful behavior of:

  1. rememberRange interval merging
  2. select() hit arithmetic + retarget refusal without retarget:true
  3. dispatch select -> retarget via copyField writes (plan swap)
  4. read() chunking (step = cycle-bound) + overlap-window prefix slicing
  5. write_at alignment guard (aligned >= 0x8000 refused)
  6. dispatch write happy path (before/verify/restore bookkeeping)
  7. failed-read artifact + nativeResult, and the all-poison DMA detector
  8. qualifySignedHeader full sequence (ordinary control, double-poison
     verify, marker write verify, restore)
  9. qualifySingleBucket (formerly-different bucket, cycle demotion)
 10. dispatch validation errors + rx-page reuse discipline

Usage: python fs_memory_control_selftest.py   (exit 0 = all pass)
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import host                      # real packet/u64 helpers (import-safe)
import signed_cache_plan
from fs_memory_control import FsMemoryControl, CheckError

STORAGE = 0x140000
CYCLE = 0x28000
CYCLE_SMALL = 0x28000            # tests use the ten-bucket period
P1, P2 = 0x123456789, 0x123458000   # known-good planner targets
# RT sits mid-bucket relative to the P1 plan (offset 0x3c000 = block 15 of a
# 10-block cycle), so select() must miss and retarget. Verified planner target.
RT = P1 + 0x3C000


def pattern(n, seed=3):
    """Non-uniform deterministic bytes (uniform bytes would trip the fake's
    poison-write detector, so every DMA payload must be non-uniform)."""
    return bytes(((i * 7 + seed) & 0xFF) for i in range(n))


class FakeConsole:
    """console.js stand-in: flat memory, page allocs, poisoned-DMA emulation.

    A send only "DMAs" when the previous write was a poison fill (the
    controller's discipline) and the DMA queue is non-empty — mirroring how a
    real receive descriptor only lands data when the device actually copies.
    """

    def __init__(self):
        self.mem = bytearray()
        self.mem_base = None
        self.heap_ptr = 0x10000000
        self.queue = []            # pending DMA payloads (FIFO)
        self.armed_rx = None       # address poisoned by the last poison write
        self.result = 0            # reply result for IPC sends
        self.sends = []            # (handle, packet_bytes)
        self.writes = []           # (address, data)
        self.allocs = []           # pages

    def _page(self, address):
        if self.mem_base is None:
            self.mem_base = address
        offset = address - self.mem_base
        if offset + 1 > len(self.mem):
            self.mem.extend(b"\0" * (offset + 1 - len(self.mem)))
        return offset

    def request(self, command, **fields):
        if command == "alloc":
            pages = fields.get("pages", 1)
            address = self.heap_ptr
            self.heap_ptr += pages * 0x1000
            self.allocs.append((pages, address))
            self._page(address + pages * 0x1000 - 1)
            return {"address": address}
        if command == "write":
            data = bytes.fromhex(fields["hex"])
            address = fields["address"]
            offset = self._page(address)
            self.mem[offset:offset + len(data)] = data
            self.writes.append((address, data))
            if len(set(data)) == 1:          # poison fill -> arm the DMA target
                # _write chunks a fill into consecutive 0x1000-byte uniform writes; arm at the
                # first chunk's address, not the last chunk, or the DMA lands on the wrong page.
                if self.armed_rx is None:
                    self.armed_rx = address
            return {"written": len(data)}
        if command == "read":
            address, length = fields["address"], fields["length"]
            offset = self._page(address)
            return {"hex": bytes(self.mem[offset:offset + length]).hex()}
        if command == "send":
            handle, packet_bytes = fields["handle"], bytes.fromhex(fields["packet"])
            self.sends.append((handle, packet_bytes))
            if self.armed_rx is not None and self.queue:
                payload = self.queue.pop(0)
                offset = self._page(self.armed_rx)
                self.mem[offset:offset + len(payload)] = payload
            self.armed_rx = None
            return {"hex": ""}
        raise AssertionError("unexpected console command %r" % command)


def fake_parse_reply(_buf, fc):
    return {"result": fc.result, "canonicalResult": fc.result}


def make_controller(tmp, initial_target=P1, cycle=CYCLE):
    fc = FakeConsole()
    mod = SimpleNamespace(
        packet=host.packet, u64=host.u64, control=host.control,
        parse_reply=lambda buf, _fc=fc: fake_parse_reply(buf, fc),
        RUNS=Path(tmp))
    fc.request("alloc", pages=0x40)               # input buffer region
    input_addr = fc.allocs[-1][1]
    initial = signed_cache_plan.plan(initial_target, STORAGE, cycle)
    copy_calls = []

    def copy_field(payload, name):
        copy_calls.append((bytes(payload), name))
        return 1                                  # one accepted wave window

    ctl = FsMemoryControl(fc, mod, candidate_handle=0xCAFE0001,
                          control_handle=0xCAFE0002, storage_size=STORAGE,
                          initial_plan=initial, input_addr=input_addr,
                          copy_field=copy_field, cycle=cycle, label="t")
    return ctl, fc, copy_calls


def expect_error(fn, needle):
    try:
        fn()
    except CheckError as error:
        assert needle in str(error), "wrong error: %s" % error
        return error
    raise AssertionError("expected CheckError containing %r" % needle)


def main():
    tmp = tempfile.mkdtemp(prefix="memctl-selftest-")
    passes = []

    # ---- 1. rememberRange merging ------------------------------------
    ctl, fc, copy_calls = make_controller(tmp)
    ctl._remember_range(0x20000000, 0x1000)
    ctl._remember_range(0x20000800, 0x800)
    assert [0x20000000, 0x20001000] in ctl.known_ranges, ctl.known_ranges
    assert all(b < e for b, e in ctl.known_ranges)
    passes.append("1 rememberRange merge")

    # ---- 2. select hit arithmetic + retarget refusal ------------------
    offset = ctl.select(P1, 0x4000)               # offset 0, aligned -> hits
    assert offset == 0, offset
    assert copy_calls == [], "select must not retarget on a hit"
    # P1+0x14000 sits mid-bucket (block 5 of a 10-block cycle) -> not a hit.
    expect_error(lambda: ctl.select(P1 + 0x14000, 1),
                 "Address needs a new header")
    passes.append("2 select hits + retarget refusal")

    # ---- 3. dispatch select -> retarget -------------------------------
    entry = ctl.dispatch({"command": "select", "address": RT, "retarget": True})
    assert ctl.active_plan["target"] == RT, ctl.active_plan["target"]
    assert entry["plan"]["target"] == RT
    assert len(copy_calls) == 1 and copy_calls[0][1].startswith("retarget-"), copy_calls
    assert copy_calls[0][0] == ctl.active_plan["bytes"], "copyField must carry the header"
    assert ctl.state.get("pending") is None
    expect_error(lambda: ctl.dispatch({"command": "select", "address": RT}),
                 "Header selection requires retarget:true")
    passes.append("3 dispatch select retarget + refusal")

    # ---- 4. read() chunking + overlap slicing --------------------------
    h1, h2, h3 = pattern(0x28000, 3), pattern(0x18000, 4), pattern(0x1C000, 5)
    fc.queue = [h1, h2]
    read1 = ctl.dispatch({"command": "read", "address": RT, "length": 0x40000})
    assert read1["length"] == 0x40000
    assert Path(read1["outputFile"]).read_bytes() == h1 + h2
    assert read1["sha256"] == hashlib.sha256(h1 + h2).hexdigest()
    fc.queue = [h3]
    read2 = ctl.dispatch({"command": "read", "address": RT + 0x14000, "length": 0x8000})
    assert read2["length"] == 0x8000
    assert Path(read2["outputFile"]).read_bytes() == h3[0x14000:0x14000 + 0x8000], \
        "overlap path must slice the bucket-start read at the prefix"
    expect_error(lambda: ctl.dispatch({"command": "read", "address": RT,
                                       "length": 0x600001}),
                 "Read length must be 1..6 MiB")
    passes.append("4 read chunking + overlap slicing")

    # ---- 5. write alignment guard --------------------------------------
    fc.queue = [pattern(0x8000, 6)]               # the dispatch's before-read DMA
    expect_error(lambda: ctl.dispatch({"command": "write", "address": RT,
                                       "hex": pattern(0x8000, 6).hex(),
                                       "expectedHex": pattern(0x8000, 6).hex()}),
                 "aligned large write")
    passes.append("5 write alignment guard")

    # ---- 6. dispatch write happy path ----------------------------------
    data = pattern(16, 7)
    before = pattern(16, 8)
    fc.queue = [before, data]
    entry = ctl.dispatch({"command": "write", "address": RT + 0x1000,
                          "hex": data.hex(), "expectedHex": before.hex()})
    assert entry["verified"] is True
    assert Path(entry["beforeFile"]).read_bytes() == before
    input_writes = [w for w in fc.writes if w[0] == ctl.input_addr]
    assert data in [w[1] for w in input_writes], "write must go through put(input)"
    assert len(fc.sends) >= 3, "before-read IPC + write IPC + readback IPC"
    passes.append("6 dispatch write happy path")

    # ---- 7. failed read artifact + all-poison detector ------------------
    fc.result = 0x356
    err = expect_error(lambda: ctl.dispatch({"command": "read",
                                             "address": RT, "length": 0x4000}),
                       "FS memory read returned 0x356")
    assert err.nativeResult == 0x356
    failed = ctl.state["failedRead"]
    assert Path(failed["outputFile"]).exists() and failed["changedBytes"] == 0
    last_op = ctl.state["operations"][-1]
    assert last_op["error"] == "FS memory read returned 0x356"
    fc.result = 0
    fc.queue = []
    expect_error(lambda: ctl.dispatch({"command": "read", "address": RT,
                                       "length": 0x4000}),
                 "did not DMA (all poison)")
    passes.append("7 failed-read artifact + all-poison detector")

    # ---- 8. qualifySignedHeader ------------------------------------------
    ctl2, fc2, _ = make_controller(tmp, initial_target=P1)
    ctl2.dispatch({"command": "select", "address": P2, "retarget": True})
    # 0x4100 bytes so the known range also covers the single-bucket address (+0x4000)
    fc2.queue = [pattern(0x4100, 9)]
    ctl2.dispatch({"command": "read", "address": P2, "length": 0x4100})
    ordinary = pattern(0x4000, 10)
    native = pattern(0x4000, 11)
    marker = bytes.fromhex("5349474e45442d525700ffcc1122aa6633")
    native_marker = bytearray(native)
    native_marker[0x108:0x108 + len(marker)] = marker
    fc2.queue = [ordinary, native, native, native, bytes(native_marker), native]
    entry = ctl2.dispatch({"command": "qualify-signed-header", "address": P2})
    assert entry["copyAttempts"] == 1
    assert entry["readVerified"] is True
    assert entry["writeVerified"] is True
    assert entry["restored"] is True
    assert entry["changedSha256"] == hashlib.sha256(bytes(native_marker)).hexdigest()
    assert ctl2.state["signedHeaderQualified"] is True
    assert "pending" not in ctl2.state and "currentOperation" not in ctl2.state
    passes.append("8 qualifySignedHeader full sequence")

    # ---- 9. qualifySingleBucket + cycle demotion -------------------------
    assert ctl2.cycle == CYCLE
    # Negative case first: the plan target's own bucket (offset 64, aligned 0)
    # is NOT a formerly-different bucket, and the cycle must not demote on failure.
    expect_error(lambda: ctl2.dispatch({"command": "qualify-single-bucket",
                                        "address": P2, "expectedHex": "aa" * 16}),
                 "formerly different bucket")
    assert ctl2.cycle == CYCLE, "failed qualification must not demote the cycle"
    expected = pattern(16, 12)
    fc2.queue = [pattern(16, 13), expected, expected]
    proof = ctl2.dispatch({"command": "qualify-single-bucket",
                           "address": P2 + 0x4000, "expectedHex": expected.hex()})
    assert proof["verified"] is True
    assert ctl2.cycle == 0x4000 and ctl2.state["bucketPeriod"] == 0x4000
    # After demotion every block-aligned offset is a hit -> no retarget needed.
    offset = ctl2.select(P2 + 0x5000, 16)          # offset = 64 + 0x5000 = 0x5040
    assert offset == 0x5040, offset
    passes.append("9 qualifySingleBucket + cycle demotion")

    # ---- 10. validation errors + rx reuse --------------------------------
    expect_error(lambda: ctl.dispatch({"command": "bogus"}), "Unknown memory command")
    ctl3, fc3, _ = make_controller(tmp)
    fc3.queue = [pattern(16, 14)]
    expect_error(lambda: ctl3.dispatch({"command": "write", "address": P1,
                                        "hex": pattern(16, 15).hex(),
                                        "expectedHex": "ff" * 16}),
                 "The expected memory bytes differ")
    # rx region must be allocated once and grown only for longer reads
    ctl4, fc4, _ = make_controller(tmp)
    fc4.queue = [pattern(0x4000, 16), pattern(0x8000, 17)]
    ctl4.dispatch({"command": "read", "address": P1, "length": 0x4000})
    first_rx, first_size = ctl4.rx_base, ctl4.rx_size
    # second read at a bucket-aligned address (P1 + cycle) with a longer block:
    # the rx region must GROW (a new alloc) since 0x8000 > 0x4000
    ctl4.dispatch({"command": "read", "address": P1 + CYCLE, "length": 0x8000})
    assert ctl4.rx_size == 0x8000, ctl4.rx_size
    assert len(fc4.allocs) == 3, "input alloc + rx alloc + one rx grow"
    info = ctl4.info()
    assert info["plan"]["target"] == P1 and info["ready"] is True
    passes.append("10 validation errors + rx reuse discipline")

    print("\n== fs_memory_control selftest: %d/%d passed ==" % (len(passes), len(passes)))
    for line in passes:
        print("  ok", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
