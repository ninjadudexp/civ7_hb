#!/usr/bin/env python3
"""a64.py - small AArch64 helper for reverse-engineering the SDK dump.

Not a full disassembler: it decodes the instructions needed to find function
boundaries, call targets, and ADRP-relative global references, so the missing
thread functions can be located by cross-reference.

Usage:
  python a64.py functions  <dump.bin> <base>
  python a64.py callers    <dump.bin> <base> <target-offset>
  python a64.py disasm     <dump.bin> <base> <offset> <count>
  python a64.py globals    <dump.bin> <base> <offset> <count>
"""
import sys


def u32(buf, off):
    return int.from_bytes(buf[off:off + 4], "little")


def sx(value, bits):
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def decode(word, address):
    """Return (mnemonic, detail-dict) for the subset needed here."""
    if word == 0xD65F03C0:
        return "ret", {}
    if (word & 0x7C000000) == 0x14000000:                     # b / bl
        link = bool(word & 0x80000000)
        target = address + (sx(word & 0x03FFFFFF, 26) << 2)
        return ("bl" if link else "b"), {"target": target}
    if (word & 0xFF000010) == 0x54000000:                     # b.cond
        return "b.cond", {"target": address + (sx((word >> 5) & 0x7FFFF, 19) << 2)}
    if (word & 0x7E000000) == 0x34000000:                     # cbz/cbnz
        return ("cbnz" if word & 0x01000000 else "cbz"), {"target": address + (sx((word >> 5) & 0x7FFFF, 19) << 2)}
    if (word & 0x1F000000) == 0x10000000:                     # adr/adrp
        page = bool(word & 0x80000000)
        immlo = (word >> 29) & 3
        immhi = (word >> 5) & 0x7FFFF
        imm = (sx((immhi << 2) | immlo, 21) << (12 if page else 0))
        return ("adrp" if page else "adr"), {"rd": word & 0x1F, "imm": imm}
    if (word & 0x7F800000) == 0x52800000 or (word & 0x7F800000) == 0x72800000:   # movz/movk
        hw = (word >> 21) & 3
        return ("movz" if (word & 0x7F800000) == 0x52800000 else "movk"), {
            "rd": word & 0x1F, "imm": ((word >> 5) & 0xFFFF) << (hw * 16)}
    if (word & 0x7F800000) == 0x12800000:                     # movn
        hw = (word >> 21) & 3
        return "movn", {"rd": word & 0x1F, "imm": ((word >> 5) & 0xFFFF) << (hw * 16)}
    if (word & 0x7F800000) == 0x11000000:                     # add/sub imm
        sub = bool(word & 0x40000000)
        return ("sub" if sub else "add"), {"rd": word & 0x1F, "rn": (word >> 5) & 0x1F,
                                           "imm": (word >> 10) & 0xFFF}
    if (word & 0x7FE00000) == 0x2A000000 or (word & 0x7FE00000) == 0x2A000000:
        return "orr", {"rd": word & 0x1F, "rn": (word >> 5) & 0x1F, "rm": (word >> 16) & 0x1F}
    if (word & 0xFFE00C00) == 0xF9400000:                     # ldr xN, [xM, #imm]
        return "ldr", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": ((word >> 10) & 0xFFF) << 3}
    if (word & 0xFFE00C00) == 0xF9000000:                     # str xN, [xM, #imm]
        return "str", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": ((word >> 10) & 0xFFF) << 3}
    if (word & 0xFFE00C00) == 0xB9400000:
        return "ldr", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": ((word >> 10) & 0xFFF) << 2}
    if (word & 0xFFE00C00) == 0xB9000000:
        return "str", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": ((word >> 10) & 0xFFF) << 2}
    if (word & 0xFFE00C00) == 0xF8400000:
        return "ldr", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": sx((word >> 12) & 0x1FF, 9)}
    if (word & 0xFFE00C00) == 0xF8000000:
        return "str", {"rt": word & 0x1F, "rn": (word >> 5) & 0x1F, "imm": sx((word >> 12) & 0x1FF, 9)}
    if (word & 0xFFC003E0) == 0xA9000000 or (word & 0xFFC003E0) == 0xA9800000:   # stp
        return "stp", {"rt": word & 0x1F, "rt2": (word >> 10) & 0x1F, "rn": (word >> 5) & 0x1F,
                       "imm": sx((word >> 15) & 0x7F, 7) << 3}
    if (word & 0xFFC003E0) == 0xA9400000 or (word & 0xFFC003E0) == 0xA8C00000:   # ldp
        return "ldp", {"rt": word & 0x1F, "rt2": (word >> 10) & 0x1F, "rn": (word >> 5) & 0x1F,
                       "imm": sx((word >> 15) & 0x7F, 7) << 3}
    if (word & 0x7F800000) == 0x53000000:
        return "lsl", {"rd": word & 0x1F}
    return "?", {"word": word}


def load(path):
    return open(path, "rb").read()


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    if cmd == "disasm":
        buf = load(args[1]); base = int(args[2], 0); off = int(args[3], 0); count = int(args[4], 0)
        for i in range(count):
            at = base + off + i * 4
            mn, d = decode(u32(buf, off + i * 4), at)
            extra = " ".join("%s=0x%x" % (k, v) for k, v in d.items())
            print("  0x%x  %-7s %s" % (at, mn, extra))
    elif cmd == "callers":
        buf = load(args[1]); base = int(args[2], 0); target = base + int(args[3], 0)
        hits = []
        for off in range(0, len(buf) - 3, 4):
            mn, d = decode(u32(buf, off), base + off)
            if mn in ("bl", "b") and d.get("target") == target:
                hits.append(base + off)
        print("callers of sdk+0x%x: %s" % (target - base, [hex(h) for h in hits[:40]]))
    elif cmd == "globals":
        buf = load(args[1]); base = int(args[2], 0); off = int(args[3], 0); count = int(args[4], 0)
        regs = {}
        for i in range(count):
            at = base + off + i * 4
            mn, d = decode(u32(buf, off + i * 4), at)
            if mn == "adrp":
                regs[d["rd"]] = base + ((at & ~0xFFF) - base) + d["imm"]
            elif mn in ("ldr", "str", "add"):
                rn = d.get("rn")
                if rn in regs:
                    print("  0x%x  %s -> 0x%x" % (at, mn, regs[rn] + d.get("imm", 0)))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
