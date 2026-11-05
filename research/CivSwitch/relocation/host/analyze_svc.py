#!/usr/bin/env python3
"""analyze_svc.py - decode a Switch syscall stub table from a memory dump.

Reads a small binary dump containing `svc #imm` stubs, decodes just enough AArch64
to find each stub's entry point and trailing return, and writes a relocation table.

Usage:
  python analyze_svc.py <dump.bin> <base-address> [out.json]

The stub entry is the first instruction of the stub containing the svc: the region
start, or the instruction right after the previous `ret`/unconditional `b`.
"""

import json
import os
import sys

RET_MASK = 0xFFFFFC1F
RET_VALUE = 0xD65F0000          # ret / ret xN
SVC_MASK = 0xFFE0001F
SVC_VALUE = 0xD4000001
B_MASK = 0xFC000000
B_VALUE = 0x14000000            # b
BC_MASK = 0xFF000010
BC_VALUE = 0x54000000           # b.cond
CB_MASK = 0x7E000000
CB_VALUE = 0x34000000           # cbz/cbnz


def sxt(value, bits):
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def decode(word, address):
    """Return (mnemonic, detail) for the handful of forms needed to read stubs."""
    if (word & RET_MASK) == RET_VALUE:
        rn = (word >> 5) & 0x1F
        return "ret", "" if rn == 30 else "x%d" % rn
    if (word & SVC_MASK) == SVC_VALUE:
        return "svc", "#0x%x" % ((word >> 5) & 0xFFFF)
    if (word & B_MASK) == B_VALUE:
        target = address + (sxt(word & 0x03FFFFFF, 26) << 2)
        return "b", "0x%x" % target
    if (word & BC_MASK) == BC_VALUE:
        cond = word & 0xF
        target = address + (sxt((word >> 5) & 0x7FFFF, 19) << 2)
        return "b.%d" % cond, "0x%x" % target
    if (word & CB_MASK) == CB_VALUE:
        kind = "cbnz" if (word & 0x01000000) else "cbz"
        target = address + (sxt((word >> 5) & 0x7FFFF, 19) << 2)
        return kind, "x%d, 0x%x" % (word & 0x1F, target)
    if word == 0xD503201F:
        return "nop", ""
    if (word & 0x7F800000) == 0x52800000 or (word & 0x7F800000) == 0x72800000:
        hw = (word >> 21) & 0x3
        imm = ((word >> 5) & 0xFFFF) << (hw * 16)
        return ("movz" if (word & 0x7F800000) == 0x52800000 else "movk", "x%d, #0x%x" % (word & 0x1F, imm))
    if (word & 0xFFE00C00) == 0xF81F0C00:            # str xN, [sp, #-16]!
        return "str", "x%d, [sp, #-16]!" % (word & 0x1F)
    if (word & 0xFFC003E0) == 0xA9800000 or (word & 0xFFC003E0) == 0xA9000000:
        rt, rt2, rn = word & 0x1F, (word >> 10) & 0x1F, (word >> 5) & 0x1F
        off = sxt((word >> 15) & 0x7F, 7) << 3
        pre = "!" if (word & 0x02800000) == 0x02800000 else ""
        return "stp", "x%d, x%d, [x%d, #%d]%s" % (rt, rt2, rn, off, pre)
    if (word & 0xFFE00C00) == 0xF8400C00:            # ldr xN, [sp], #imm
        return "ldr", "x%d, [sp], #%d" % (word & 0x1F, ((word >> 12) & 0x1FF) << 3)
    if (word & 0xFFE00C00) == 0xF9400000 and ((word >> 5) & 0x1F) == 31:  # ldr xN, [sp, #imm]
        return "ldr", "x%d, [sp, #%d]" % (word & 0x1F, ((word >> 12) & 0xFFF) << 3)
    if (word & 0xFFE00C00) == 0xB9000000 and ((word >> 5) & 0x1F) != 31:  # str wM, [xN, #imm]
        return "str", "w%d, [x%d]" % (word & 0x1F, (word >> 5) & 0x1F)
    if (word & 0xFFE00C00) == 0xF9000000 and ((word >> 5) & 0x1F) != 31:  # str xM, [xN, #imm]
        return "str", "x%d, [x%d]" % (word & 0x1F, (word >> 5) & 0x1F)
    if (word & 0x7F8003FF) == 0x110003FF:            # add/sub sp, sp, #imm
        imm = (word >> 10) & 0xFFF
        if word & 0x40000000:
            imm = 0x1000 - imm
        return ("add" if not (word & 0x40000000) else "sub"), "sp, sp, #0x%x" % imm
    return "?", "0x%08x" % word


def find_stubs(buf, base, ref=None):
    ref = base if ref is None else ref
    words = {}
    for off in range(0, len(buf) - 3, 4):
        w = int.from_bytes(buf[off:off + 4], "little")
        words[base + off] = w
    addresses = sorted(words)

    # Barriers delimit stubs: the instruction after a ret or unconditional b starts one.
    def is_barrier(w):
        return (w & RET_MASK) == RET_VALUE or (w & B_MASK) == B_VALUE

    stubs = []
    for addr in addresses:
        w = words[addr]
        if (w & SVC_MASK) != SVC_VALUE:
            continue
        imm = (w >> 5) & 0xFFFF
        start = addr
        index = addresses.index(addr)
        while index > 0:
            prev = addresses[index - 1]
            if is_barrier(words[prev]):
                break
            start = prev
            index -= 1
        # Strip alignment padding (nops) so the entry is the first real instruction.
        while start != addr:
            prev_index = addresses.index(start)
            if decode(words[start], start)[0] != "nop":
                break
            start = addresses[prev_index + 1]
        # Walk the stub from start to its first ret.
        body, end = [], None
        j = addresses.index(start)
        while j < len(addresses):
            a = addresses[j]
            mn, detail = decode(words[a], a)
            body.append({"address": a, "word": "0x%08x" % words[a], "mnemonic": mn, "detail": detail})
            if mn == "ret":
                end = a
                break
            j += 1
        stubs.append({"svc": imm, "immHex": "0x%02x" % imm, "svcAddress": addr,
                      "svcOffset": addr - ref, "entry": start, "entryOffset": start - ref,
                      "returnAddress": end,
                      "length": (end - start + 4) if end is not None else None,
                      "form": "svc;ret" if len(body) == 2 else "multi",
                      "returns": end is not None, "body": body})
    return stubs


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    path, base = args[0], int(args[1], 0)
    ref, out = None, None
    rest = args[2:]
    if rest and rest[0] == "--ref":
        ref = int(rest[1], 0)
        rest = rest[2:]
    if rest:
        out = rest[0]
    with open(path, "rb") as fh:
        buf = fh.read()
    stubs = find_stubs(buf, base, ref)

    print("%d svc stubs" % len(stubs))
    for stub in stubs:
        forms = ",".join(m["mnemonic"] for m in stub["body"])
        print("  svc #%-3d (%-5s) entry=+0x%-6x %-8s returns=%s  [%s]" %
              (stub["svc"], stub["immHex"], stub["entryOffset"], stub["form"], stub["returns"], forms))

    table = {"generatedFrom": os.path.basename(path), "base": base,
             "referenceBase": base if ref is None else ref,
             "count": len(stubs), "stubs": stubs}
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=2)
        print("wrote %s" % out)


if __name__ == "__main__":
    main()
