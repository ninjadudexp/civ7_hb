---
tags: [civ7, switch2, homebrew, payload, npdm, abi]
status: active
---

# Payload policy and ABI

Deep dive into what gives the ns2jb payload its privileges and which ABI it speaks. Derived from `ro/payload/manifest.json`, `ro/payload/start.S`, `build-payload.cjs`, `ro/files.cjs`, and `ro/launch.cjs`.

## What the manifest pins

```json
{ "platform": 2,
  "programId": "0400000000000037",
  "payloadBytes": 4757,
  "payloadSha256": "d590cbfc5d9152c24d5a36f083f8470a2f3164be6b78130129d44132b939701a",
  "policySha256": "f63138f1e3f747af5e881c2402d30762e338c372c34069b2e20f1f28198865de" }
```

## Where the policy comes from

`donor-prepare` (`ro/files.cjs`) reads two local files and checks them against the manifest:

- `ro/payload/main` — the packaged NSO
- `<session>-code-0400000000000037/main.npdm` — captured earlier from the console's RO CodeFS

The capture path opens the native RO CodeFS for title `0400000000000037` (`root-native-code-open`), then captures `main.npdm` and `main` (`root-native-file-capture`). So **`policySha256` is the hash of the console's own stock NPDM for that system title.**

This answers the [[Open questions]] item "which target process the payload rides on" and the missing builder:

- The payload does not ship its own NPDM. It is written as the donor title's `main`, while the donor's stock `main.npdm` is left in place.
- Its capabilities are therefore exactly those the stock system title `0400000000000037` declares — which is why it can use `svcQueryProcessMemory`, `svcMapProcessCodeMemory`, `svcUnmapProcessCodeMemory`, and `svcSetProcessMemoryPermission`.
- `engine/shared/npdm.cjs` (referenced by `tools/npdm.py`) is not on this pipeline's critical path. Its absence does not block M4.

Accepted launch targets (`ro/launch.cjs`): `0400000000000037` (default) and `0400000000000019`, `…000a`, `…004f`, `…0014`, `…0013`, `…001d`. The donor policy must belong to the selected target.

## Donor file set

Preflight checks 13 names: `main`, `main.npdm`, `rtld`, `sdk`, `subsdk0`…`subsdk9`. Only `main` and `main.npdm` are written; the other names must be absent (`0x202`) or byte-identical to their existing content. Reused files are recorded with `reused: true`, and an existing differing file forces an application restart. The shipped payload is freestanding (`-fno-compiler-rt`, `-fno-unwind-tables`, `-fno-stack-protector`), so it needs none of the dynamic files.

## Image format

`build-payload.cjs` compiles a freestanding AArch64 ELF (Zig 0.15.2, `aarch64-freestanding-none`, `-fPIC`, entry `_start`) and packages `.text`/`.rodata`/`.data` into an NSO (`NSO0`, name `main`, mod0 at `0x0c`, sections at `0x14/0x18`, hashes at `0xa0`). Entry jumps to `main`; `.text.start` carries a `MOD0` module header; `.data` holds the handshake magic `0x4558454f5232534e` (`"NS2ROEXE"`).

## SVC ABI split

`start.S` uses **Switch 1 legacy** SVC numbers. The SmileBASIC app SDK wrappers in `engine/shared/sdk-functions.json` use **Switch 2** numbers.

| Purpose | Payload (`start.S`) | SmileBASIC SDK | libnx (Switch 1) |
| --- | --- | --- | --- |
| `connectToNamedPort` | `0x1f` | — | `0x1f` |
| `sendSyncRequest` | `0x21` | `0x441` | `0x21` |
| `closeHandle` | `0x16` | `0x2c1` | `0x16` |
| `sleepThread` | `0x0b` | — | `0x0b` |
| `waitSynchronization` | `0x18` | — | `0x18` |
| `replyAndReceive` | `0x43` | — | `0x43` |
| `acceptSession` | `0x41` | — | `0x41` |
| `getProcessId` | `0x24` | — | `0x24` |
| `getThreadId` | `0x25` | — | `0x25` |
| `getSystemTick` | `0x1e` | — | `0x1e` |
| `getInfo` | `0x29` | — | `0x29` |
| `queryMemory` | `0x06` | — | `0x06` |
| `queryProcessMemory` | `0x76` | — | `0x76` |
| `mapProcessMemory` | `0x74` | — | `0x74` |
| `unmapProcessMemory` | `0x75` | — | `0x75` |
| `mapProcessCodeMemory` | `0x77` | — | `0x77` |
| `unmapProcessCodeMemory` | `0x78` | — | `0x78` |
| `setProcessMemoryPermission` | `0x73` | — | `0x73` |

The payload matches libnx Switch 1 numbering exactly, so it must be loaded into a process whose kernel SVC table is Switch 1-numbered (a compat/legacy process). The Switch 2-numbered SDK is a different ABI and cannot run the payload as-is. See [[Compat ABI]].

## IPC command surface (`process.c`)

| Command | Function |
| --- | --- |
| `0x524f0001` | handshake: returns magic, pid, tid, tick, registration result |
| `0x524f0010` | query: process info, alias/heap bounds, one free page in target and in RO |
| `0x524f0011` | read up to 128 bytes from a target page |
| `0x524f0012` | map one caller-owned Normal/RW page as code and set RX |
| `0x524f0013` | unmap code |
| `0x524f0014` | 16-byte verified patch with cache publish and readback |
| `0x524f0015` | toggle read bit of native FS execute-only pages (`4`↔`5`, program `0400000000000000` only) |
| `0x524f0016` | bulk read up to 4 MiB through a temporary RO alias |

## Discrepancy to resolve before M5

`HBLauncher/docs/ARCHITECTURE.md` maps code with `0x524f001a` (N pages RX), `0x524f001b` (N pages RW), and `0x524f001c` (unmap N). The shipped payload returns `BAD_INPUT` for all three. The launcher therefore needs an extended payload, or the docs describe a newer payload build than the one on disk. This is a real gap between the two repositories, not a documentation typo.
