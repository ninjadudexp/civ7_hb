---
tags: [civ7, switch2, homebrew, porting, roadmap]
status: active
---

# Porting roadmap

How to reproduce the ns2jb pipeline from the Civilization VII process, derived from `research/ns2jb/engine/switch2/pipeline.cjs` and the `ro/` + `process/` stages. This note is the single reference for the ultimate goal: run homebrew through the Civ VII exploit the way SmileBASIC 4 does.

## The insight that makes it possible: steal the FS service manager

The old worry — "a retail game cannot open `pm:shell` or `pm:dmnt`" — is bypassed, and the code shows how:

1. The game opens its own `fsp-srv` and opens an **OpenSystemDataUpdateEventNotifier** (command `510`) on it (`ro/control.cjs::notifier-open`). This is an ordinary FS service an app can reach. **Confirmed on hardware (2026-11-03):** `fsp-srv` opens through the SDK `getService`, while `pm:shell`/`pm:dmnt`/`jit:u` are denied with SM result `0x1015` — see [[Probe plan]] P4.
2. The [[Retained memory]] channel lets the game read/write FS-process memory, including the notifier's **handle field**.
3. `exportHandle` temporarily points that field at another handle value and triggers the notifier, so the **FS process itself performs the object copy**; the game receives a real handle to an FS-process object. The field is then restored.
4. `ro/test-sm.cjs` finds the FS process's own **service-manager client** at `FS.sdk + 0x12a0` (an initialized SM handle + guard), exports that handle, and calls GetService through it (`ro/control.cjs::sm-service`). Because the caller is the FS process, the returned `pm:shell` / `pm:dmnt` / `ro:dmnt` handles are authorized even though the game could never open them directly.

So the privilege is not in the game; it is **stolen from the filesystem system process**. The breakout is what makes the theft possible, and the gate/launch stages then run as if the game owned the privileged services.

## End-to-end stage map

`pipeline.cjs` runs four phases. Civ VII must reproduce each, in order.

| Phase | ns2jb stages | Civ VII prerequisite | milestone |
| --- | --- | --- | --- |
| 1. Prepare the app | `app/map`, `app/inspect-mounts`, cleanup, `app/qualify` | SDK relocation + thread/RPC wrappers verified in the game image | M1 |
| 2. Acquire FS memory | `acquire.cjs` → `memory/probe-reserved-gap.cjs` (breakout + [[Address planner]] control) | port [[Wave harness]]; requalify heap geometry from the game's traffic | M2/M3 |
| 3. Native helper and launch | `retained/find-metadata`, `ro/install-retained`, `ro/layout`, `ro/objects root|notifier`, `ro/test-sm`, `ro/copy-loader-root`, `ro/prepare-workers`, donor prep, `ro/bind-sessions`, `ro/prepare-event`, `ro/test-gate`, `ro/launch`, `runtime.activate`, `runtime.probe`, `runtime.handoff` | [[Retained memory]] chain + FS-process layout offsets re-derived for the game's FS build | M4 |
| 4. Run and clean up | `process/bind-notifier`, `process/qualify`, `process/fs-execute`, cleanup | payload + process-memory primitives | M5 |

## What phase 3 actually does

Once the retained channel and the stolen SM handle exist, the launch is a sequence of native operations:

- `ro/layout` indexes the current boot from the `0x4d8000` heap capture and validates live Loader root objects against it (FS SDK base, root vptr, domain manager, 7 contiguous waiter slots). `ro/objects` re-proves the owned root and notifier by **domain-ID locator + clone challenge** rather than by pointer guessing.
- `ro/prepare-workers` builds the 7 API-worker copies and the thread-cancellation primitive; `ro/bind-sessions` finds the two server bindings (own + target domain) whose shapes match; `ro/prepare-event` and `ro/test-gate` qualify the [[Launch gate]].
- Donor prep captures the console's own stock `main.npdm` and `main` for title `0400000000000037` through the stolen `ro:dmnt`/CodeFS path (see [[Payload policy]]).
- `ro/launch` swaps the CodeFS object reference during the queued mount ([[Launch gate]]), `runtime.activate` starts the replacement RO process and terminates the original, and `runtime.probe`/`handoff` verify instruction challenges and return the Loader session.

## Civ VII deltas (what must be re-derived)

1. **SDK relocation (M1).** The game image needs verified bytes for the wrappers ns2jb assumes: `send`, `close`, `getService`, `malloc`/`free`, `memset`, thread create/join, and `CancelSynchronization` (`svc #0x19`). See [[Primitive matrix]] and [[SDK signatures]].
2. **FS-process layout offsets.** `capturedLayout` hardcodes `sdk = range.address + 0x500000`, `rootVptr = sdk - 0xf38be0`, the domain vptr pair, `holderVptr`, and the waiter stride `0x110`. These are the **FS build's** constants and must be re-found for the game's firmware, not copied. **Progress (2026-11-03):** the game's *first* filesystem step is solved — the mounted save `IFileSystem` is obtained by scanning RW regions for the `save` accessor and `CopyObject`-ing the borrowed session; see [[Filesystem access]].
3. **Cache-owner offsets.** The retained install checks `+0x20 storageSize`, `+0x28 0x4000`, `+0x50 0x5a0`, `+0x58 count 10`, `+0x60 shift 14`. Re-verify each on the game's FS build.
4. **Bucket period.** `0x28000` vs `0x500000` is measured; the address control must qualify on the game's candidate.
5. **Breakout geometry.** The trigger allocation sizes, delays, and the `0x2600`/`0x2810` gates were measured on SmileBASIC traffic; Civ VII writes saves heavily. Re-measure via [[Probe plan]] P7.
6. **Payload build.** The shipped payload is a Switch 1 ABI binary ([[Compat ABI]]); M5 also needs the extended range commands `0x524f001a`–`0x524f001c` that `process.c` lacks.

## Why this is a strong path

- The exploit mechanism (FS general-heap breakout) and everything downstream are **privilege-independent once the FS process is reached**; the theft of the SM handle removes the retail-service restriction entirely.
- ns2jb already proves every downstream step on real hardware for SmileBASIC. The port is a **relocation + re-measurement** problem, not a new-exploit problem.
- The riskiest unknowns are the measured ones: whether Civ VII's filesystem traffic reproduces the qualification window ([[Open questions]]), and whether the game's SDK table-interpreter functions expose the same thread primitives.

## Related

- [[Retained memory]] — the channel the helper is built on
- [[Address planner]] — the first mapping
- [[Launch gate]] — the CodeFS swap
- [[Payload policy]] — donor policy and payload commands
- [[Probe plan]] / [[Wave harness]] — the M2/M3 work
- [[Bridge design]] / [[Primitive matrix]] — milestones and relocation status
