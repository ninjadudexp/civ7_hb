---
tags: [civ7, switch2, homebrew, abi, compat]
status: active
---

# Compat ABI

The SVC-numbering question from [[Probe plan]] P2, sharpened with repository evidence. This decides whether a native payload can run in the game process and whether the harness probes the right numbers.

## The numbering question

| Source | Evidence | `send` | `close` |
| --- | --- | --- | --- |
| Switch 1 legacy (libnx) | `research/libnx/nx/include/switch/kernel/svc.h`; `CivSwitch/harness/console.js` `SVC_IDS` | `0x21` | `0x16` |
| SmileBASIC SDK wrappers | `ns2jb/engine/shared/sdk-functions.json` decoded as `(word >> 5) & 0xffff` | `0x22` | `0x16` |
| ns2jb payload | `ns2jb/engine/switch2/ro/payload/start.S` | `0x21` | `0x16` |

**Correction.** An earlier note read the SDK bytes as Switch 2 numbering (`0x441`/`0x2c1`). That came from slicing the little-endian hex string, not decoding the instruction. `410400d4` is `svc #0x22` (`SendSyncRequestWithUserBuffer`) and `c10200d4` is `svc #0x16` (`CloseHandle`) — both Switch 1. SmileBASIC 4 is itself a Switch 1 title (compat), so there is no Switch 2-native divergence in these wrappers.

## Consequence

- The ns2jb payload and the SmileBASIC SDK wrappers are both **Switch 1**, differing only in which SVC they use (`0x21` plain send vs `0x22` with-user-buffer). The payload is not a Switch 2-native binary; `platform: 2` refers to the console platform.
- A genuinely Switch 2-native build would need Switch 2 numbers, but nothing in this thread requires one.

## Implication for Civ VII

Civ VII runs from a Switch 1 physical cartridge in compatibility mode. That argues its in-image SDK copy uses Switch 1 numbering, which is exactly what `console.js` probes (ids `0x06, 0x08, 0x0B, 0x12, 0x16, 0x18, 0x1E, 0x1F, 0x21, 0x22, 0x23, 0x29, 0x45`). Two outcomes:

- **Switch 1 numbering** — wrappers relocate, the plan proceeds, and the payload's ABI matches the host process.
- **Switch 2 numbering** — every `svcCandidates` list is empty for the Switch 1 table. The harness reports zero wrappers instead of relocating wrongly, which is the correct failure mode.

Do not "fix" a zero-candidate report by loosening the scan; the id table encodes the ABI decision.

## Confirmed on hardware (2026-11-03)

First telemetry from the Switch 2 (`relocation/runs/…-diag-*.json`) settles P2:

- `main_base` and `sdk_base` both point at real AArch64 code.
- A single-pass `svcindex` over `sdk_base + 0x600000` found **60 `svc #imm` stubs with Switch 1 immediates** (`1–29`, `31`, `33–42`, `44`, `45`, `47`, `50–54`, `64`, `65`, `67–69`, `72`, `73`, `75`, `80`, `81`, `82`, `112`, `114`), contiguous at about `sdk_base + 0x168AD8`.
- The first 1 MB of `main_base` contains no `svc`.

The stub table was then dumped and decoded (`relocation/host/analyze_svc.py`), giving an
authoritative `imm → entryOffset` map (`relocation/signatures/svc-relocation.json`):
`queryMemory 0x06 → +0xb0c`, `connectToNamedPort 0x1f → +0xc34`, `sendSyncRequest 0x21
→ +0xc4c`, `sendSyncRequestWithUserBuffer 0x22 → +0xc54`, `getInfo 0x29 → +0xca4`,
`createEvent 0x45 → +0xddc`, and 54 more.

So the image is **Switch 1-numbered**, the payload ABI matches the host process, and
`console.js`'s id family is the right one. One important caveat: **not every stub is
literally `svc; ret`**. `queryMemory` (`0x06`) and others save an output pointer, store
the returned handle, then return, so relocation must use the decoded **stub entry**, not
`svc + ret` adjacency. That is why the first `env` run reported "wrapper not found" even
though the stubs were present.

## Threading and affinity references

Two captured references in `ns2jb/engine/switch2/app/` document the compat shim that Switch 1 code goes through on this console:

- `compat-affinity-reference.json` — `nn::os::GetThreadAvailableCoreMask` is imported from `nnSdk` and lands on an `nnCompatThin` wrapper (`pacibsp` prologue, `and x0, x0, #7`), i.e. the compat shim masks the core count to 3 bits.
- `thread-scheduling-reference.json` — `pthread_getaffinity_np` in `nnSdk` (`stp x29, x30, [sp, #-0x30]!`, bounds checks at `#8`/`#0x10`), used for worker pinning.

When the harness pins workers, the available core mask should be read through the same path and treated as compat-masked, not as the raw hardware width.

## Open

- Decode the stub table bytes to record each stub's entry and return form (`dump-svc` plus `analyze_svc.py`).
- The syscall stubs are not the SDK library functions; those still need the `scan` signature relocation (`allocate`, `getService`, `socket`, threads, events).
- Confirm the core-mask/thin-wrapper behaviour on hardware before pinning workers.
