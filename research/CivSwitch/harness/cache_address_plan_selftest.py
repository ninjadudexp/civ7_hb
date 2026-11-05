"""Byte-for-byte check of cache_address_plan.nul_writes against ns2jb's cache-address-plan.cjs.

The expected rows are the ones node produced for the installCombinedLeak prefix; if the .cjs
changes, regenerate them with:
  node -e "..." (see handoff §44 / the alias-chain notes).
"""

from cache_address_plan import nul_writes, PlanError

ALIAS_OFFSET = 0x1400000


def _prefix(alias_offset=ALIAS_OFFSET):
    desired = bytearray(0x12)
    desired[0:8] = alias_offset.to_bytes(8, "little")
    desired[8:16] = (0x4141414141414141).to_bytes(8, "little")
    desired[0x10] = 1
    return bytes(desired)


EXPECTED = [
    {"end": 17, "bytes": "4242400142424242414141414141414101"},
    {"end": 7, "bytes": "42424001424242"},
    {"end": 6, "bytes": "424240014242"},
    {"end": 5, "bytes": "4242400142"},
    {"end": 4, "bytes": "42424001"},
    {"end": 1, "bytes": "42"},
    {"end": 0, "bytes": ""},
]


def main():
    desired = _prefix()
    steps = nul_writes(desired)
    assert len(steps) == 7, "combined prefix must need exactly 7 copies, got %d" % len(steps)
    for index, (step, expected) in enumerate(zip(steps, EXPECTED)):
        assert step["end"] == expected["end"], (index, step["end"], expected["end"])
        assert step["bytes"].hex() == expected["bytes"], (index, step["bytes"].hex())
    # cjs throw case: no trailing zero.
    try:
        nul_writes(b"\x01\x02")
    except PlanError:
        pass
    else:
        raise AssertionError("a prefix without a trailing zero must be refused")
    # non-UTF-8 placeholder run must be refused (0xc3 -> 0x42 replacement keeps it valid, so use
    # a genuinely invalid sequence already present).
    try:
        nul_writes(bytes([0xC3, 0x28, 0x00]))
    except PlanError:
        pass
    else:
        raise AssertionError("an invalid UTF-8 prefix must be refused")
    print("cache_address_plan: combined prefix = 7/7 copies, throws verified")


if __name__ == "__main__":
    main()
