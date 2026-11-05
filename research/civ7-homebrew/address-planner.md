---
tags: [civ7, switch2, homebrew, exploit, fs, planner]
status: active
---

# Address planner

Deep dive into the signed cache-address planner, derived from `research/ns2jb/engine/switch2/memory/signed-cache-plan.cjs`, `retained/lower-window.cjs`, and the consumers `memory/probe-reserved-gap.cjs` and `retained/retained-memory.cjs`.

This is the module that turns a chosen native address into a small header the FS content path will parse as a pathname, so that a normal "read content at offset" returns bytes from that native address. It is the arithmetic core of the [[Retained memory]] and [[Filesystem breakout]] work.

## Problem

After the M3 alias, one of the FS cache bucket fields holds an attacker-influenced block descriptor (`offset`, `range`, and a base pointer). The cache indexes buckets with a **signed right shift** of the block offset. To read native memory at `target` through the ordinary content-read command, the planner must find a descriptor such that:

- the `entryOffset` (a signed 64-bit value) is **negative** so the signed shift selects bucket zero;
- `entryOffset >> 14` is a multiple of the bucket count (`(offset >> 14) % bucketCount == 0`);
- `base + readOffset - entryOffset == target` for the chosen read offset;
- the header bytes are valid UTF-8 and contain **no** `0x00`, `/` (`0x2f`), or `\` (`0x5c`) — otherwise the path parser rejects or misparses them.

The header is `0x28` bytes:

| Offset | Field |
| --- | --- |
| `0x00` | `entryOffset` (signed, negative) |
| `0x08` | `range = 0x7e7e7e7e7e7e7e7e` |
| `0x10..0x13` | four `1` bytes (validity flags) |
| `0x20` | `base` (unsigned) |

Constants: `BLOCK = 0x4000`. Supported bucket periods: `0x28000` and `0x500000` (`plan` rejects anything else).

## Encoding the offset (`encodeOffset`)

The offset is not just any negative number: `entryOffset = int64(bytes)` where `bytes` is an 8-byte UTF-8-valid, forbidden-byte-free string. The planner searches for two such byte sequences `L` (offset) and `R` whose **difference equals the bias** `target - readOffset`:

```
right[i] = (left[i] + displacement[i] + carry) & 0xff      # displacement = LE(bias)
carry    = (left[i] + displacement[i] + carry) >> 8
```

so that `uint64(offset + bias) == base` and `base + readOffset - offset == target`.

- `utf8Step` tracks UTF-8 validity as a small state machine (`[remaining, low, high]`), including the surrogate guard (`0xe0`/`0xed`/`0xf0`/`0xf4` edge cases) so `isUtf8` passes without a decode.
- A `mask` derived from `zeroBits = (2**power - 1) * BLOCK`, where `power` is the 2-adic valuation of the odd part of the bucket count, forces the low offset bits to zero so the signed shift lands on bucket zero.
- The search is a DFS over the 8 byte positions with memoization (`misses` keyed by position + UTF-8 states + carry + remainder) and a 5-second deadline (`No signed cache encoding found within five seconds`). Position 6/7 are pinned to `0xc2`/`0x81` to satisfy the UTF-8 and negativity constraints.
- `remainder` must equal `(2**50) % odd` at the end, which is the modular condition tying the high bytes to the bucket count.

Result: `{offset: int64(L), base: uint64(offset + bias)}`.

## Choosing the read offset (`prefixFor`)

The planner slides a candidate block start across the measured storage on `cycle` boundaries, trying four sub-block phases `{0, 0x40, 0x140, 0x200}`, and returns the first `0x28`-byte header whose arithmetic the caller can independently verify. If none fits the measured storage it throws rather than guessing.

## `plan()` and independent checks

`plan(target, storageSize, cycle)` validates the inputs (`target` in the range `2**32 .. 2**39`, `storageSize >= 0x4000`, allowed cycle) and then **re-verifies its own output** before returning:

- length `0x28`, `isUtf8`, no forbidden bytes;
- `offset < 0` and `(offset >> 14) % bucketCount == 0`;
- `readOffset` page-aligned inside the storage and within the mapped range;
- `offset + range < 2**63`;
- `uint64(base + readOffset - offset) == target`.

The returned plan is `encoding: signed-one-shot`, `headerFormat: signed-one-shot`, and 64-bit fields carried as strings.

## `lowerWindow()` — widen below an observed pointer

One leak may expose only the middle of the allocator pool, so the true lowest readable address is unknown. `lowerWindow(target, storageSize, cycle, pointers)`:

```text
maximumPrefix = min(0x4000000, storageSize - 0x8000, target - 2**32)
prefix        = floor(maximumPrefix / cycle) * cycle
proposedStart = target - prefix
result        = plan(target - prefix, storageSize, cycle)
result.readOffset += prefix          # addressBias + readOffset == target
```

The lower start is a **search hypothesis only**; the proof stays at the observed address until a real native anchor is found (see [[Retained memory]] step 2). The result records `lowerWindow.lowestObservedPointer` and an explicit `qualification` string saying the lower mapping has not been read.

## Consumers

- **`memory/probe-reserved-gap.cjs` (M3)** — after the alias qualifies, `prepareAddressControl` calls `lowerWindow` on the observed target. It hex-dumps the plan into `report.cacheAddress`, installs the header via `copyField`, then reads with poison `0xa5` and again with `0x5a`, requiring both reads to succeed, match each other, and differ from ordinary content. It writes a 16-byte marker through the redirected pointer, verifies the surrounding bytes, and restores. It also verifies the planned bucket period qualifies on the candidate (`addressPlan.cycle === 0x500000 || bucketPeriod === addressPlan.cycle`).
- **`retained/retained-memory.cjs` (M4)** — `moveBootstrap` uses `lowerWindow` to propose the next mapping and re-anchors it on a native reciprocal link. After install, the plan becomes `retained-two-bucket`: the steady-state channel no longer re-encodes a header, it only rewrites the `+0x20` pointer inside the canonical data descriptor (`pointerAddress = array + dataEntryOffset + 0x20`).

## Why two encodings

The signed one-shot header is needed exactly once per mapping change, when a whole new `0x28` descriptor must pass the path parser. The retained channel instead keeps a canonical descriptor and pokes one aligned pointer, so the expensive UTF-8/bucket search leaves the hot path entirely. That is the main reliability win of [[Retained memory]] over raw alias reuse.

## Correctness caveats

- `encodeOffset` is a heuristic search with a wall-clock deadline; a miss throws instead of falling back to a wrong header.
- `lowerWindow` does not prove the lower address is mapped — only that a valid encoding exists there. Native byte readback is the proof.
- The bucket period is measured, not assumed; the number in the note (`0x28000`) is the observed planner cycle, and `0x500000` is the alternate.

## Related

- [[Retained memory]] — uses the planner once, then a pointer poke
- [[Filesystem breakout]] — the alias this planner drives
- [[Launch gate]] — downstream consumer
