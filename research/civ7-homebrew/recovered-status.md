---
tags: [civ7, switch2, research, status]
status: recovered
---

# Recovered Status

Recovered by filesystem inspection; Obsidian retrieval is not wired up yet.

## Repositories under research/

| repo | role | latest commit |
| --- | --- | --- |
| CivSwitch | Civ VII chat-XSS entry, ROP payload, boot installer | 2465a68, 2026-11-02 |
| ns2jb | filesystem-service breakout, privileged payload, SDK session | 95f5525, 2026-10-22 |
| HBLauncher | native launcher inside a host process, Vulkan UI, app packages | 18b4748, 2026-10-29 |
| HBLauncherNS2 | GPU-only launcher variant, separate native verification gate | 9d00834, 2026-10-23 |
| libnx | homebrew SDK reference, SVC wrappers, jit fallback | b156b47, 2026-10-19 |
| Atmosphere | kernel and capability model reference, stock module docs | 6b0c077, 2026-10-16 |

## Entry point (CivSwitch)

- Bootstrap: USB HID macro types the Remote JS Loader payload into the multiplayer chat box; system and USB keyboard language must be English US.
- core.js: unstable addrof, read and write, fake objects and fake functions, heap manipulation, ROP epilogue plus gadgets, window.v for user payloads, TCP transport for the loader.
- Base derivation: main_base = exe_addr - 0x1AF7FA0, sdk_base = read64(main_base + 0x4DC6088) - 0x5224E0. Offsets are pinned to the game image, so they follow the game build, not the firmware.
- Persistence: installer-creator/install.py writes save:/saves/Single/XSTRIG.Civ7Save; Load Game runs the stager every boot.
- Alternate entry: misc/ patches romfs:/Base/modules/core/ui/mp-chat/screen-mp-chat.js and delivers the payload over network chat.

## Where the frozen session stopped

- workspace.json last-opened files were libnx SF headers (sessionmgr.h, hipc.h, cmif.h, tipc.h) and Atmosphere module docs (ro.md, fs.md, spl.md), i.e. studying service IPC wire formats and stock sysmodule behaviour.
- Atmosphere README.md carries only a line-ending diff.
- No prior notes exist anywhere in the vault.

## Clone gaps

- ns2jb has no docs/ directory, so boundary and recovery documents are missing.
- ns2jb npdm builder is absent; tools/npdm.py expects engine/shared/npdm.cjs. Payload policy is only described by engine/switch2/ro/payload/manifest.json.
