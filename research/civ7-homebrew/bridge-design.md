---
tags: [civ7, switch2, homebrew, design]
status: draft
---

# Bridge Design

Goal: Civ VII as a permanent entry point that yields native homebrew on a stock Switch 2.

## Kernel constraints found in the references

1. Stock forbids code memory on your own process. libnx jit.c notes that from 5.0.0 onward svcControlCodeMemory errors for the owning process, and Atmosphere's kern_svc_code_memory.cpp comments out exactly that owner check to help homebrew. So RW pages cannot become RX inside Civ VII without privileges.
2. Process-memory and debug SVCs are npdm-gated. libnx uses svc 0x73 SetProcessMemoryPermission, 0x4B CreateCodeMemory, 0x4C ControlCodeMemory, 0x74 MapProcessMemory, 0x77 and 0x78 Map/UnmapProcessCodeMemory, 0x60 DebugActiveProcess. Games do not declare these; verify with GetInfo type 26 IsSvcPermitted.
3. Retail apps cannot reach pm:shell or pm:dmnt, so a game cannot launch a homebrew process by itself.

## The breakout ns2jb already proved

- The bug lives in the filesystem service general heap. Requests built with a /..\ prefix plus an oversized interleaved path extent let one pending request's metadata copy land on an owned path canary, and a retained cache entry is then aliased to an offset outside its allocation, giving reads and writes inside the FS process.
- Qualification is measurable: receive-order margin at least 300 microseconds, at least 0x2600 bytes held before release, an unchanged canary on disk, and stable allocation counters (59 objects costing 59 * 208 bytes, one storage costing 13616).
- After the breakout, FS opens pm:shell, launches a system target, and swaps the CodeFS filesystem reference with a donor reference, so the launched process runs the payload image. The payload answers commands 0x524f0010 to 0x524f0016 with process-memory queries, page mapping, permission changes, and 16-byte verified patches with cache publish.
- HBLauncher consumes those commands to map launcher code RX at 0x08000000 and per-app RX at 0x09000000, following each code map with one publish per 64-byte cache line.

## Why Civ VII still needs its own work

- The exploit is driven from the calling process, so Civ VII must supply the same primitives: raw filesystem IPC frames, several worker threads with tight timing, allocation counters, and heap shaping. SmileBASIC gets threads from SDK table-interpreter functions; the same signatures should exist in the game's SDK copy.
- Compat ABI: a Switch 1 cartridge running on Switch 2 uses the Switch 1 SVC numbering, where libnx send is svc 0x21. ns2jb's Switch 2 SDK wrappers encode different numbers, send 0x441 and close 0x2c1. Switch 2 native code inside a Switch 1 compat process would call the wrong SVC numbers, so native payloads belong in their own process, or must be built with legacy numbers.
- Display ownership: HBLauncher creates its own vi swapchain while a game host keeps drawing. Either patch the game render path through the payload's verified 16-byte write, or run homebrew in a launched process.

## Preferred architecture

Civ VII payload (JS plus ROP over TCP) drives an ns2jb-style host pipeline, replays the filesystem qualification waves from the game process, obtains the breakout, launches the payload process, then hands that process to a homebrew launcher and app packages. Civ VII stays as the boot trigger via XSTRIG.Civ7Save.

Fallback: an in-process launcher with a patched game render loop and legacy-SVC native builds.

## Milestones

| stage | deliverable | gate |
| --- | --- | --- |
| M0 | entry verified on fw 23.0.1 with logged bases | loader reachable over TCP |
| M1 | SDK signature relocation table | every needed function verified by bytes |
| M2 | threaded RPC harness | timed wave profiles reproducible |
| M3 | filesystem qualification from the game | canary overwrite with stable counters |
| M4 | payload process answering process commands | mapping and patch commands succeed |
| M5 | launcher plus one app package | visible frame, clean exit |
