"""Port of ns2jb memory/cache-address-plan.cjs (pure planning, no console calls).

`nul_writes(desired)` plans the copyField write sequence that reconstructs a desired byte
prefix through fsp-srv's path normalizer: each write copies `desired[:end]` with NUL bytes
replaced by 0x42 ('B') placeholders, and the sequence ends just before each real zero, so
sequential copies concatenate into the target prefix. Every produced write must decode as
strict UTF-8 (the normalizer rejects anything else).

The alias chain's combined prefix is 0x12 bytes:
    u64(aliasOffset)  +  u64(0x4141414141414141)  +  byte 1  +  NUL(trailer)
which yields exactly 7 writes (the last of which is empty). Verified byte-for-byte against the
.cjs (`cache_address_plan_selftest.py`).
"""


class PlanError(ValueError):
    """The cjs throw cases."""


def nul_writes(desired):
    desired = bytes(desired)
    last = -1
    for index in range(len(desired) - 1, -1, -1):
        if desired[index] != 0:
            last = index
            break
    if last < 0 or last + 1 >= len(desired):
        raise PlanError("The requested prefix needs a trailing zero")
    ends = [last + 1]
    for end in range(last, -1, -1):
        if desired[end] == 0:
            ends.append(end)
    steps = []
    for end in ends:
        chunk = bytes(0x42 if byte == 0 else byte for byte in desired[:end])
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError as exc:                     # noqa: PERF203
            raise PlanError("The requested prefix does not decode as strict UTF-8") from exc
        steps.append({"end": end, "bytes": chunk})
    return steps
