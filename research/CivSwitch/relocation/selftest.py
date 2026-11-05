#!/usr/bin/env python3
"""Offline self-test for the relocation scanner. No console needed.

Builds a synthetic image with known signatures at known offsets, runs the same
offline scan the host tool uses, and asserts exact matches, an anchor match on a
drifted body, and SVC-immediate scanning for both Switch 1 and Switch 2 ids.

Usage: python selftest.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_host():
    spec = importlib.util.spec_from_file_location("rh", os.path.join(HERE, "host", "relocate_host.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    rh = load_host()
    table = rh.load_signatures()
    failures = []

    buf = bytearray(b"\x00" * 0x400000)

    # 1. exact matches at known offsets
    exact_plants = {"allocate": 0x123400, "memset": 0x2b0000, "getService": 0x180000}
    for name, off in exact_plants.items():
        f = next(f for f in table["functions"] if f["name"] == name)
        body = bytes.fromhex(f["bytes"])
        buf[off:off + len(body)] = body

    # 2. an anchor-only match: plant only the anchor window, not the full body
    anchor_name = "call4"  # stable, non-trampoline body
    af = next(f for f in table["functions"] if f["name"] == anchor_name)
    asig = bytes.fromhex(af["anchorBytes"])
    anchor_base = 0x300000
    buf[anchor_base + af["anchorOffset"]:anchor_base + af["anchorOffset"] + len(asig)] = asig

    # 3. svc wrappers: switch1 send (#0x21) and switch2 send (#0x441)
    def svc(imm):
        return (0xD4000001 | (imm << 5)).to_bytes(4, "little") + (0xD65F03C0).to_bytes(4, "little")
    buf[0x15845c:0x15845c + 8] = svc(0x21)
    buf[0x158480:0x158480 + 8] = svc(0x441)

    base = 0x70000000
    report = rh.offline_scan(bytes(buf), table, base=base)
    by = {r["name"]: r for r in report["functions"]}

    for name, off in exact_plants.items():
        want = base + off
        got = by[name]["exact"]
        if want not in got:
            failures.append("exact %s expected 0x%x, got %s" % (name, want, [hex(a) for a in got]))

    row = by[anchor_name]
    if row["exact"]:
        failures.append("anchor test expected no exact match for %s" % anchor_name)
    if not any(a["base"] == base + anchor_base for a in row["anchor"]):
        failures.append("anchor %s expected base 0x%x, got %s" % (anchor_name, base + anchor_base, row["anchor"]))

    svc_by = {s["name"]: s for s in report["svc"]}
    if svc_by["sendSyncRequest"]["foundSwitch1"] != [base + 0x15845c]:
        failures.append("switch1 send scan wrong: %s" % svc_by["sendSyncRequest"]["foundSwitch1"])
    if svc_by["sendSyncRequest"]["foundSwitch2"] != [base + 0x158480]:
        failures.append("switch2 send scan wrong: %s" % svc_by["sendSyncRequest"]["foundSwitch2"])

    print("functions in table : %d" % len(table["functions"]))
    print("localised          : %d" % sum(1 for r in report["functions"] if r.get("best")))
    print("switch1 send       : %s" % [hex(a) for a in svc_by["sendSyncRequest"]["foundSwitch1"]])
    print("switch2 send       : %s" % [hex(a) for a in svc_by["sendSyncRequest"]["foundSwitch2"]])
    if failures:
        print("SELFTEST FAIL")
        for line in failures:
            print("  - " + line)
        sys.exit(1)
    print("SELFTEST PASS")


if __name__ == "__main__":
    main()
