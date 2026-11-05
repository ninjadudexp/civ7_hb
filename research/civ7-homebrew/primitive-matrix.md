---
tags: [civ7, switch2, homebrew, primitives]
status: draft
---

# Primitive Matrix

What ns2jb and HBLauncher use in the SmileBASIC host, and the Civ VII equivalent.

| capability | SmileBASIC host | Civ VII equivalent | status |
| --- | --- | --- | --- |
| arbitrary read and write | COPY NSR fake pointers | unstable addrof, fake object, read and write | available |
| arbitrary native call | CALL# with sdk_base plus offset | call_rop through game-image gadgets | available |
| allocator | SDK allocate and free | game calloc and free at main_base + 0x515b50 and main_base + 0x515a60 | available |
| IPC send and close | SDK wrappers ending in svc then ret | same SDK functions exist in the game image, find by signature | needs relocation |
| service open | SDK getService | same | needs relocation |
| sleep, tick, affinity | SDK sleep, tick, core mask | same SDK family | needs relocation |
| worker threads | SDK thread create, join, loadTable, call4, tryWait, longjmp from engine/switch2/app/thread-code.json | same signatures should exist in the game SDK copy | port first |
| RPC batching | host RPC over TCP with page-packed request buffers | core.js socket transport plus multi-page writes | reusable |
| filesystem object handles | domain handles, Clone, CopyObject, CloseObject frames | same frames sent from the game | port wire format from engine/shared/domain.cjs |
| heap accounting | allocation counters via command 1009 | same request | portable |
| RX mapping | payload commands 0x524f0012 and 0x524f0013 | same, once the breakout exists | blocked on M3 |
| verified RX patch | payload command 0x524f0014 with cache publish | same | blocked on M3 |

## Steady-state native memory

The rows above are mostly *in-process* primitives. The pipeline that turns the M3 alias into a usable native channel is a separate, privesc-independent capability:

- **Address planner** (`memory/signed-cache-plan.cjs`) encodes a `0x28`-byte UTF-8, NUL/`/`/`\`-free header whose `entryOffset` selects bucket zero after the FS cache's signed shift and whose `base` reproduces the target. One-shot, per mapping change.
- **Retained memory** (`switch2/retained/`) pins one owned cache array and rewrites a single pointer inside it, giving a steady-state read/write channel with **no further path corruption**. It also provides the `retained-inject-holder` primitive the [[Launch gate]] depends on.

Both are privilege-independent but firmware-specific: they need the FS server-session layout, the cache-owner field offsets, and the native-root worker (`CancelSynchronization` + 7 API-worker copies). Port them together with the worker relocation, not as part of the filesystem wave work. See [[Retained memory]] and [[Address planner]].

## M1 relocation status (2026-11-03)

Measured on the Switch 2 through the relocation payload (`research/CivSwitch/relocation/`):

- SVC numbering is Switch 1; 60 syscall stubs decoded to `imm → sdk offset` in `signatures/svc-relocation.json`.
- 18/36 SDK signatures localised: `getService` (`+0x14cc90`), timing/thread `tick`/`frequency`/`priority`/`current` (`+0x137xxx`), event `initialize`/`finalize`/`clear`/`tryWait` (`+0x132xxx`), `loadTable`, `saveResult`, `saveResult64`.
- `core.js` already supplies working `calloc`/`free`/socket/`memcpy`, so those capabilities are effectively available without relocation.
- Still to relocate: thread `create`/`join`/`exit`, event `wait`/`signal`, `memset`, `hash`.

## Relocation rule

ns2jb never trusts an offset alone; it compares bytes at the offset through verifyCode. Offsets belong to the SmileBASIC image, byte signatures do not. Scan the Civ VII SDK region for the same byte patterns, then record a relocation table with the verified offset, the signature, and surrounding context in every session report.

The game image holds one static SDK copy, reached through sdk_base derived from a memcpy pointer. That is the same libnx or libnn family the homebrew SDK models, including SVC wrappers, service clients, filesystem calls, socket calls, and thread helpers.

## Things Civ VII does not give you

- process launch, because apps only get pm:info
- RX permission transitions, because the owning-process check blocks code memory
- debug reads of another process, because debug privileges come from npdm
