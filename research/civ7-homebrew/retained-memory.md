---
tags: [civ7, switch2, homebrew, exploit, fs, retained]
status: active
---

# Retained memory

Deep dive into the `retained-memory` primitive chain, derived from `research/ns2jb/engine/switch2/retained/retained-memory.cjs`, `ro/install-retained.cjs`, `retained/cache-shape.cjs`, `retained/lower-window.cjs`, `retained/find-metadata.cjs`, `retained/links.cjs`, and the consumer `ro/gate.cjs`.

This is the bridge between the one-shot M3 filesystem alias and the M4 [[Launch gate]]: it converts a single leaked cache view into a **steady-state, corruption-free arbitrary read/write channel inside the FS process**.

## Why it exists

The M3 breakout ([[Filesystem breakout]]) gives a transient alias: each native access needs another interleaved path request and another measured corruption. That is too fragile to drive a multi-step launch, where the gate must read and write several native words per step, dozens of times. The retained endpoint pins one owned cache array and rewrites a single pointer inside it, so subsequent reads/writes cost **no further corruption** (`requiresCorruption: false`, fixed `corruptionCount`).

The post-install consumer is `ro/gate.cjs`, which needs one raw primitive: `retained-inject-holder`.

## Phases

`report.retainedMemory.phase` moves through `bootstrap` → `bootstrap-metadata` → `installing` → `retained` → `retired`. Install is the irreversible step: once `promoted` is set, the legacy one-entry path overwrite is disabled and any incomplete install must stay retained for recovery rather than fall back to the alias.

## 1. Bootstrap: one native block via the existing alias

The acquisition (`memory/probe-reserved-gap.cjs`) already installed a **signed** [[Address planner]] header, mapping one `0x4000` block of native memory as FS "content". `bootstrapRead` walks that mapping with `readStorage(candidateHandle, size, offset, poison)` in `cycle`-sized chunks, requiring `offset % cycle === 0` at the start (the fixed **bootstrap bucket**).

`lowerWindow` proposes a *wider, lower* mapping than the smallest observed pointer, because one leak may expose only the middle of the pool: `prefix = floor(min(0x4000000, storageSize - 0x8000, target - 2**32) / cycle) * cycle`, and the plan is built for `target - prefix` with `readOffset += prefix` so `addressBias + readOffset == target`. The lower address is explicitly a **search hypothesis**, not a verified boundary.

## 2. Metadata move: find a real native anchor

`find-metadata.cjs` locates the allocator's external link so the mapping can be re-anchored onto genuine native metadata instead of a guessed pointer:

1. Read the bootstrap target for `0x500000` bytes; collect **native links** — 8-byte values at `0x4000` page starts that are page-aligned, in the range `2**32 .. 2**39`, and within `0x2000000` of the seed (`links.cjs`). Links whose following 56 bytes are zero are flagged `zeroTail`.
2. `resolvePool` narrows the shared native buffer pool: the allocator's pool is `0x2000000` bytes at `0x200000` alignment. Each hypothesis keeps only candidate starts whose in-range `source` links all point inside the same span. It reads further buckets (preferring uncaptured link targets, else filling a gap) until `first == last`; at most 128 rounds.
3. The allocator node sits at `pool + 0x3a00000`. It must show `next == previous`, `|next - node| > 0x8000000`, and an `extent` of `0x10000..0x10000000`, page-aligned. That is a self-consistent external allocator link, not an inferred mapping.
4. Build `anchor = [node, node]` (the reciprocal link) and call `retained-bootstrap-move` on `page = next & ~0x3fff`.

`moveBootstrap` (in `retained-memory.cjs`) requires a captured source covering the fallback, reads the fallback through the **old** mapping first, and checks the anchor is *not* already there (otherwise the proof is meaningless). It installs the next header via `copyField`, reads with poison `0xa5` then `0x5a`, and only promotes `bootstrapPlan` when the anchor appears in both reads. The promoted plan is `headerFormat: signed-one-shot` with `phase: bootstrap-metadata`.

## 3. Install: pin the owned cache array

`ro/install-retained.cjs` first reconstructs the owned array from the `0x4d8000` heap capture: it finds the `0x5a0`-byte entry whose header matches the active bootstrap header, then finds the owning storage node whose array pointer is in that set (checking `+0x20 == storageSize`, `+0x28 == 0x4000`, `+0x50 == 0x5a0`, `+0x58 == 10`, `+0x60 == 14`). Exactly one match is required. `retained-bootstrap-move` maps the bootstrap **directly onto that array**, verified against the capture, before `retained-install` runs.

`install` then builds the two-bucket interface:

| Field | Value | Meaning |
| --- | --- | --- |
| `array` | `bootstrapPlan.target` | owned cache array base |
| `owner` | matched storage node | native owner record |
| `arrayLength` | `0x5a0` | owned array size |
| `dataBucket` | `1` | second bucket used as the data channel |
| `dataEntryOffset` | `0x90` | `1 * 0x90` entry inside the control bucket |
| `dataOffset` | `0x4000` | offset that the data descriptor points at |
| `scratchOffset` | `0x588` | `arrayLength - 0x18`, a scratch slot inside the array |

Install checks that the control read exposes its **own borrowed descriptor** (`[0]==header`, `[0x10]==0`, zeros at `0x18..0x30`) and that the rest of the array is either empty or the known `0x41`/`/` path fill (`cache-shape.cjs::unusedTail`); anything else could own real cached buffers and is rejected. Recognized path fill is cleared first.

### The cross-channel trick

The elegant part is making the control descriptor canonical without a direct write into itself:

1. Write a scratch marker at `array + scratchOffset` (`0x588`).
2. Install a data descriptor `cacheEntry(dataOffset, scratch)` at `array + dataEntryOffset`; verify through the data channel that it reads the marker, write a second marker through the data channel, and observe it through the control channel — an independent read/write/restore round trip.
3. Write `u64(array)` into the data descriptor's `+0x20` pointer, then read the control descriptor *through the data channel*. The other bucket's in-flight release restores only its own bucket, so the control change survives.
4. Write `cacheEntry(0, array)` through the data channel. Now the canonical control descriptor points at the array itself and the data descriptor points at `array + 0x4000`.

### Disabling the cache loops

The owner record holds a native bucket **count** at `+0x58` (observed `10`) and a **shift** at `+0x60` (observed `14`). Zeroing the count makes lookup/release retain the synthetic block indices while flush/finalize loops skip the synthetic buffers — no vtable, function pointer, or backing storage is changed. Readback must confirm `+0x58 == 0` and `verifyOwner` must still pass.

Steady state is `retainedInfo`: `ready`, `steadyStateReliable`, `requiresCorruption: false`, a fixed `corruptionCount`, `controlAddress == array`, `currentTarget == pointer`, and `plan.pointerAddress == array + dataEntryOffset + 0x20`.

## 4. The channel and the one raw primitive

- `logicalRead/logicalWrite` move up to `0x4000` through the data descriptor (command `1` write, `readStorage` read).
- `readMemory/writeMemory` loop at `0x4000`. `setPointer` first verifies the data descriptor is unchanged except for `+0x20`, then writes the new pointer and reads it back. `writeMemory` reads back every part; the `access` write path also captures a preimage and restores it if the write throws and the channel is still alive.
- `retained-inject-holder` is the only operation that intentionally performs an **unverified store**: it checks the waiter is idle (`parent + 0x10 == 0`), re-verifies the parked session identity and that its parent/state have not moved, confirms the request header is still `0xffffffff`, then writes the server pointer into `parent + 0x10`. It must cancel workers (`CancelSynchronization`, decoded `svc #0x19`, code bytes `210300d4c0035fd6` at SDK offset `0x158414`) **immediately** after, because a normal verified FS read would consume the wake. Cancellations must all return `0`.

## 5. Retirement

`retire` refuses unless `canonical && zeroCountVerified`; it restores the pointer to the owner, confirms the count is still `0` and no extra path corruption occurred, and marks `retired`. `acquire.cjs::retire` additionally refuses if a worker is still pending or any install/pending flag is set, then waits for the probe process to exit cleanly.

## Portability to Civ VII

Everything here is privilege-independent, so it is not gated by the kernel constraints in [[Bridge design]]. It does depend on:

- the **native-root worker** (the 7 API-worker copies and `CancelSynchronization`) — the same relocation work as the [[Primitive matrix]] `needs relocation` rows;
- the FS **server-session layout** and cache-owner field offsets, which are Switch-2-firmware-specific and must be re-derived from the game's FS build;
- the `0x28000`/`0x500000` bucket period, which is a *measured* quantity (`bucketPeriod`) and must qualify on the game's candidate.

## Related

- [[Address planner]] — the signed header that maps the first native block
- [[Launch gate]] — the consumer of `retained-inject-holder`
- [[Filesystem breakout]] — the M3 alias this replaces
- [[Primitive matrix]] — relocation status
