#!/usr/bin/env python3
"""find_missing.py - locate drifted SDK functions in a dumped SDK image.

For each requested function it takes short anchors from the ns2jb body
(nn-sdk-signatures.json) and slides them over the dump, then reports the
candidate with the longest matching prefix of the full body.

Usage: python find_missing.py <dump.bin> <sdk-base> [name ...]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNATURES = os.path.join(HERE, "signatures", "nn-sdk-signatures.json")

WANT = ["create", "join", "exit", "longjmp", "call4", "tryWait", "tryWaitImplementation",
        "sleep", "memset", "allocate", "free", "hash", "sdk-proxy", "saveResult"]


def load_table():
    with open(SIGNATURES, encoding="utf-8") as fh:
        return {f["name"]: bytes.fromhex(f["bytes"]) for f in json.load(fh)["functions"]}


def match_len(buf, at, sig):
    n = 0
    while n < len(sig) and at + n < len(buf) and buf[at + n] == sig[n]:
        n += 1
    return n


def find(buf, sig, anchor_len=16):
    best = None
    seen = set()
    for offset in range(0, max(1, len(sig) - anchor_len + 1), 4):
        anchor = sig[offset:offset + anchor_len]
        if len(anchor) < anchor_len:
            break
        start = 0
        while True:
            at = buf.find(anchor, start)
            if at < 0:
                break
            start = at + 1
            base = at - offset
            if base < 0 or base in seen:
                continue
            seen.add(base)
            length = match_len(buf, base, sig)
            if best is None or length > best[1]:
                best = (base, length, offset)
    return best


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    path, base = sys.argv[1], int(sys.argv[2], 0)
    want = sys.argv[3:] or WANT
    buf = open(path, "rb").read()
    table = load_table()
    print("%d-byte dump @ 0x%x" % (len(buf), base))
    for name in want:
        sig = table.get(name)
        if not sig:
            print("  %-22s (no signature)" % name)
            continue
        best = find(buf, sig)
        if not best:
            print("  %-22s not found (no anchor hit)" % name)
            continue
        candidate, length, offset = best
        exact = "EXACT" if length == len(sig) else "%d/%d bytes" % (length, len(sig))
        print("  %-22s sdk+0x%-8x %s (anchor@%d)" % (name, candidate, exact, offset))


if __name__ == "__main__":
    main()
