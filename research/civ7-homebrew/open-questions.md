---
tags: [civ7, switch2, homebrew, risks]
status: draft
---

# Open Questions

## Does the game process qualify as an exploit caller

ns2jb's qualification constants were measured with SmileBASIC traffic. The game issues different filesystem patterns, so the reservation group, cache window, and receive-order margins may not reproduce. The abort controls give a clean early signal before any overwrite is attempted.

## Which target process the payload rides on

Answered in [[Payload policy]]. `policySha256` is the SHA-256 of the console's own stock `main.npdm` for title `0400000000000037`, captured from the RO CodeFS. The payload is written as that title's `main`; its privileges come from the donor policy, not from a builder. The missing `engine/shared/npdm.cjs` is not blocking. Remaining: confirm the donor title's declared capabilities directly.

## Service privilege for the launch

The launch needs `pm:shell`, `pm:dmnt`, and `ro:dmnt`, which a retail game cannot open directly. The ns2jb code resolves this: the game exports the **filesystem process's own service-manager handle** (via the system-data notifier and the retained channel) and calls GetService through it, so the privileged handles are authorized for the FS process. This makes the port a breakout problem, not a privesc problem. Still to verify for Civ VII: that the game can open `fsp-srv`, the system-data notifier command `510`, and the code-memory-free FS operations the theft depends on. See [[Porting roadmap]].

## Switch 2 kernel differences

Atmosphere describes Switch 1 semantics. SVC numbering, page table regions, and filesystem heap layout differ on Switch 2, and ns2jb's SDK wrappers show the Switch 2 numbering.

## Compat-mode ABI

Narrowed in [[Compat ABI]]. The ns2jb payload is a Switch 1 ABI binary (`start.S` uses libnx numbers), while the SmileBASIC SDK uses Switch 2 numbers (`send 0x441`, `close 0x2c1`). Civ VII runs as a Switch 1 cartridge, so its image is expected to use Switch 1 numbers — the set `console.js` probes. Still must run P2: a zero-candidate relocation report means the image is Switch 2-numbered, and both the SVC table and the payload build then change together.

## Display ownership

Two owners of the display in one process is fragile. HBLauncher never tears down its swapchain, while a game keeps presenting. Options are patching the game render path, suspending it, or running homebrew in a separate process.

## Persistence limits

The save trigger fires on Load Game, so every run needs a manual menu action. Anything earlier in boot needs a different trigger.

## Safety

Every qualification wave writes into filesystem structures. ns2jb keeps explicit recovery paths, restore checks, and retained resources on placement uncertainty. Port those guards before attempting repeated writes.
