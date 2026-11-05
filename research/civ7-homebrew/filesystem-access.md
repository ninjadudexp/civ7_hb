---
tags: [civ7, switch2, homebrew, filesystem, m3]
status: active
---

# Filesystem access

How the game process obtains a **path-capable `IFileSystem`** — the M3 breakthrough (2026-11-03). Derived from ns2jb `app/mounted-save.cjs`, `app/inspect-mounts.cjs`, and `switch2/memory/probe-reserved-gap.cjs`, reproduced live.

## The problem

`fsp-srv` opens and `Initialize` (`1`) / `ConvertToDomain` (`0`) / `Clone` (`2`) work, but **path methods are refused with kernel error `0xf601`** — on the raw session, on the converted domain, and on a content storage:

- content storage (`200`) is an `IStorage`, not an `IFileSystem`; `GetSize` (`4`) works (`4.98 GB`) but `GetFreeSpace` (`11`) / `GetEntryType` (`7`) / `CreateDirectory` (`2`) do not.

ns2jb avoids this by copying the **already-mounted save filesystem** out of the process, via a borrowed session and `CopyObject`.

## The mount chain (measured)

The SDK mount table lives in a **separate RW mapping** from the SDK RX region (`sdk_base` is RX). The `save` accessor (this build) is found by scanning readable+writable regions for `"save\0"`.

`accessor` layout (name at `+24`, matches ns2jb):

| Offset | Field | Value (measured) |
| --- | --- | --- |
| `+0x00` | tag | `0x232ad8e308` |
| `+0x10` | next (list) | `0x65b5c6e3f8` |
| `+0x18` | **name** (16 bytes) | `"save"` |
| `+0x28` | **implementation** pointer | `0x662adff920` |

Then, walking pointers:

| Step | Read | Value |
| --- | --- | --- |
| implementation | `[impl+0x10]` | `service = 0x66285fdf00` |
| service | `[service+0x10]` | `manager = 0x232adef920` |
| service | `[service+0x18]` (u32) | `objectId = 124` |
| manager | `[manager+0x08]` (u32) | `0x800` (sanity) |
| manager | `[manager+0x5c]` (u32) | `3` (sanity) |
| manager | `[manager+0x10]` | `slots = 0x232adef9a0` |
| slots | `[slots]` (u32) | `borrowed = 0x981ed` |

## Getting the handle

Send **`CopyObject`** (`control(1, u32(objectId))`) on the **borrowed** handle:

```
send(handle=borrowed, packet=control(1, u32(objectId)))
  -> canonical 0, one moved handle = the save IFileSystem
```

Verified on the returned handle:

| Path command | Call | Result |
| --- | --- | --- |
| `11` | GetFreeSpace `/` | `0`, `65,699,840` bytes (~62.6 MB) |
| `7` | GetEntryType `/` | `0` (directory) |
| `2` | CreateDirectory | `0` |
| `0` | CreateFile (`u64 off + u64 size`) | `0` |
| `7` | GetEntryType file | `1` (file) |

So the game process has **path-capable read/write on its save filesystem**.

## Mount name registry

The scan also found the nnSdk mount-name table (entries look like `{u32 tag=0x2251; u32 hash; u32 len; char name[len]}`), names in this build: `save`, `tmp`, `SdCard`, `GameCard`, `SaveData`, `Content`, `Host`, `Nand`, `System`, `User`, `Prod`, `Safe`, `Aoc`, `Rom`. This is where the accessor address is discovered each boot.

## Counters and reservation

- Allocation counters (`1009`) are a **plain command on an initialised (non-domain) session**, not a domain request. Returns 16 `u64`. The harness's first attempt framed it as a domain request and got `0xf601`.
- Creating the 59-object reservation group (`domain(root, 1200)`) **aborted the process** when run standalone. ns2jb runs it inside the wave with workers/pinning active, so it is opt-in (`setup --reserve`) until those preconditions are understood.

## Tooling

- Relocation payload: `regions` (queryMemory walk), `scanRegions` (host-filtered readable+writable name scan). **Never read a region without `perm & 1`, and keep 8 bytes of slack from a region end** — `read32`/`read8` are 8-byte reads and cross boundaries otherwise (this faulted twice).
- Relocation host: `regions`, `mounts` (writes `runs/*-mounts-*.json`).
- Harness host: `Harness.mounted_save(accessor)`, `setup(..., reserve=False)`, `reserve_group`, `counters`; accessor persisted in `harness/mount-accessor.txt`.

## Related

- [[Porting roadmap]] — phase 3 needs this handle
- [[Probe plan]] P6
- [[Filesystem breakout]] — the next stage (qualification waves) uses this filesystem
- [[Work log]] — the run that found it
