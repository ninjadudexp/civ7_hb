---
tags: [civ7, switch2, research, review]
status: active
---

# Verification pass (2026-11-02)

Re-derived the existing notes from repository code and re-ran the offline checks, to separate what was verified from what was inferred, and to record the first deep-dive findings. See [[Index]] and [[Work log]].

## Offline checks re-run

| Check | Command | Result |
| --- | --- | --- |
| Wire byte parity | `cd research/CivSwitch/harness && node wire-selftest.cjs && python wire-selftest.py && python wire-diff.py` | `{"frames": 17, "replies": 4, "identical": true, "issues": []}` |

Repository state matches [[Recovered status]]: `CivSwitch` at `2465a68` with untracked `harness/` and a line-ending-only `README.md` diff; `ns2jb` at `95f5525` with a line-ending-only `README.md` diff; `Atmosphere` also only line endings.

## Claims that check out

| Claim | Evidence |
| --- | --- |
| Entry: USB HID chat-XSS, English US, `XSTRIG.Civ7Save`, Load Game runs the stager | `CivSwitch/README.md`, `installer-creator/install.py`, `misc/README.md` |
| `main_base = exe_addr - 0x1AF7FA0`, `sdk_base = read64(main_base + 0x4DC6088) - 0x5224E0` | `core.js` ~L766–780, walking JSFunction → SharedFunctionInfo → FunctionTemplateInfo → CallHandlerInfo → Foreign |
| `calloc`/`free` at `main_base + 0x515b50` / `main_base + 0x515a60` | `core.js` |
| ROP epilogue `main_base + 0x2450084`, full gadget set, `call_rop` | `core.js` |
| FS qualification constants (see [[Filesystem breakout]]) | `engine/switch2/memory/probe-reserved-gap.cjs` |
| Payload command range `0x524f0010`–`0x524f0016` | `engine/switch2/ro/payload/process.c` |
| Manifest `payloadBytes 4757`, `payloadSha256 d590cbfc…` | `ro/payload/manifest.json` |
| `send`/`close` SDK wrappers are single `svc`+`ret` | `engine/shared/sdk-functions.json` |

## Corrections and precision

- **Held-bytes gate has two numbers.** The prose said `0x2600`; the code gates `preReleaseUsed >= 0x2600` in `hasWindow` but measures against `minimumCacheHeld = 0x2810` for the cache field copies. Both are real; `0x2600` is the window gate, `0x2810` the stricter cache gate.
- **`0x2800` vs `0x800` is structural.** The trigger is staged as five `0x800` pieces (one trigger page plus four tail pages) = `0x2800`, while its path descriptor declares only `0x800`. The four tail pieces are separate path requests. This is the probe subject, confirmed in code.
- **The reservation group is 59 pages of one domain command** (command `1200`) sent in batches of 32, expected to allocate exactly `59 * 208` bytes. On mismatch the group is released and retried up to 3 times — the check is a hard gate, not advisory.
- **The pin/candidate persistent cost is `13616` bytes**, verified twice: once for the pin storage and once for the candidate that reuses the canary slot.
- **`InfoType_IsSvcPermitted` caveat stands**, and the probe plan's direct `svcCreateCodeMemory` fallback is the safer primary signal on this firmware.

## New findings

1. **The native payload is a Switch 1 ABI binary.** `ro/payload/start.S` encodes legacy SVC numbers (`send #0x21`, `close #0x16`, `connectToNamedPort #0x1f`, `replyAndReceive #0x43`), and the SmileBASIC SDK wrappers in `sdk-functions.json` decode to the same family (`send` = `svc #0x22`, `close` = `svc #0x16`). **Correction:** an earlier version of this note read the SDK bytes as Switch 2 (`0x441`/`0x2c1`); that was a little-endian hex-slicing mistake, since `410400d4` decodes to `svc #0x22`. Details in [[Payload policy]] and [[Compat ABI]].
2. **The payload's privileges come from the console's own stock NPDM**, not from a builder. `policySha256` is the SHA-256 of the stock `main.npdm` captured from the RO CodeFS for title `0400000000000037`. The missing `engine/shared/npdm.cjs` is therefore not on the critical path. See [[Payload policy]].
3. **The shipped payload does not implement the launcher's range commands.** `process.c` answers only `0x524f0001` and `0x524f0010`–`0x524f0016`; `HBLauncher/docs/ARCHITECTURE.md` uses `0x524f001a`–`0x524f001c` for bulk range map/unmap. M5 needs an extended payload, not this one.
4. **The CodeFS swap is a session TOCTOU**, not a data copy: a parked FS server session's wait word is redirected, then two domain entry object pointers are exchanged and both owners verified. See [[Launch gate]].
5. **The `ns2jb/docs/` gap is real but not blocking.** `README.md` carries the operator procedure; the wiki pages referenced were never cloned, so boundary/recovery prose is absent. The pipeline code and `run-control`/recovery paths still document the operational contract.

## Stale or confirmed gaps

- Obsidian integration is working as of 2026-11-03. The earlier blocked reads came from a stale PATH: the running Pi process predated the PATH edit and needed a restart. Existing-note validation now succeeds; the vault path is remembered and proposed-content validation is unaffected. One residual `pi-obsidian-vault` 0.2.3 quirk: a note's own text can trip the setup classifier and surface as `NOTE_NOT_FOUND`.
- `research/sq.json` is an empty search result file (`total_count: 0`) and carries no research value.

## Still open

Unchanged from [[Open questions]]: whether the game process reproduces the FS qualification window. The ABI question is answered: the game image's stub table is Switch 1-numbered (see [[Compat ABI]]), so `console.js`'s SVC family is correct.
