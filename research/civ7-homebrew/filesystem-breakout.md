---
tags: [civ7, switch2, homebrew, exploit, fs]
status: active
---

# Filesystem breakout

Deep dive into what ns2jb actually does to reach the filesystem process, derived from `engine/switch2/memory/probe-reserved-gap.cjs` and `engine/switch2/memory/paths.cjs`. This is the M3 subject: reproduce the qualification window from the Civ VII process.

## Mechanism in one paragraph

A path request whose receive extent is interleaved across the pipeline is held pending while a second path request carries a `/..\`-prefixed name. The oversized, interleaved extent shifts the FS general-heap allocation order so that one pending request's metadata copy lands on an owned path canary, and a retained content-cache entry is then aliased to an offset outside its allocation. The alias is read back through the ordinary content-read command, giving reads (and, after command `5` invalidation, control) inside the FS process. Every step is gated on measurable timing and allocation geometry before any overwrite is attempted.

## Trigger construction

- Prefix `/..\` (backslash) for the exploit payload; `/../` and `/..\` control profiles.
- `extent = 0x2440`, split into five `0x800` pieces. Piece 0 is staged at `triggerInput`; pieces 1–4 at consecutive `tailInputs`. The trigger's own path descriptor declares only `0x800`, so four trailing pieces are separate path requests (`1002`).
- For `extent-*`, segments are written at `0x800` stride and the basename (`SPAN_MARKER` / `SPAN_ABSENT`) is written at `extent - basename.length`.
- For `positive`, the marker sits at `markerOffset = 0x2340` inside a `B`-filled extent with the selected directory written over it.
- Paths are built with `normalizedPath(prefix, length)`, which pads an already-normalized missing path with `/<up to 32 A's>` chunks (canary padded to `0x5a0`, guard to `0x300`, `page_absent` to `0x7ef`).
- Path descriptors are capped at two per packet and `size <= 0x800` (`paths.cjs`).

## Allocation geometry

| Quantity | Value | Source |
| --- | --- | --- |
| Reservation pages | 59 | `probe-reserved-gap.cjs` |
| Reservation group object cost | `59 * 208` bytes | manager command `1200`, one packet per page |
| Reservation batching | 32 addresses per RPC batch | code |
| Pin storage cost | 13616 bytes | command `200`, then `newPersistentBytes` check |
| Candidate reuse cost | 13616 bytes | reuses the canary slot |
| Storage read size (`probeReadSize`) | `0x180000` | code |
| Cache read size | `0x200000` | code |
| Marker offset | `0x2340` | code |
| Minimum cache held | `0x2810` | field-copy gate |
| Pre-release gate | `0x2600` | `hasWindow` |
| Planner cycle | `0x28000` | address planner |

## Worker plan (13 roles)

Roles, in index order: `commit`, `hole`, `tail0`–`tail3`, `canary`, `filler0`, `filler1`, `release`, `guard0`, `guard1`, `trigger`.

Delay profile (nanoseconds) applied to roles 0–9: `[0, 300000, 350000, 450000, 550000, 650000, 1700000, 2000000, 2150000, 2800000]`. `guard0`/`guard1` wait on `groupReleased` plus `(i-10) * 150000`. `trigger` sleeps `2400000`, then waits `groupReleased` and `guardLaunched`, sleeps `400000`, then samples guard state. Workers are pinned across the available core mask.

## Qualification gates

`hasWindow(row)` requires all of:

- `restored` — general-heap counters returned to baseline
- `!triggerAborted` — no native guard tripped
- `allPending` — every held path started before the trigger and ended after it
- `receiveOrderMarginUs >= 300`
- `preReleaseUsed >= 0x2600`
- for `extent-present`, additionally `selectedMarker`

Profiles run in order: `abort-control`, `abort-hole-control`, `extent-absent`, `extent-present` (retry up to 3), then, if qualified, `negative`, `positive`, a repeated `positive`, `address-encoding-control`, and interleaved `checkExtent`. A miss at the overwrite stage sets `placementUncertain` and throws, retaining all resources for recovery rather than retrying blind.

## Expected per-role results

| Role | Expected canonical result |
| --- | --- |
| `tail*` | `0x320002` (deferred seed) |
| `guard*` | `0x202` |
| `filler*` | `0x202`, or `0x2eea02` in exploit profiles |
| `canary` (control) | `0`, control type `1` |
| `trigger` (`/..\`) | `0x2eec02` |
| `trigger` (`binary-control`) | `0x2eea02` or `0x2eec02` |
| `trigger` (`extent-present`) | `0` with inline marker word `1` |

## Native guards

The native worker samples three decisions from shared memory and, at the trigger, aborts by replacing the trigger buffer with a zero-length copy (`memcpy` at SDK offset `0x586fb0`) when completed, early-hole, or minimum-spacing invariants are violated. `abort-control` must trip `completed`; `abort-hole-control` must trip `earlyHole` and `minimumSpacing`. The verified `memcpy` body is checked before use.

## Why Civ VII is different

ns2jb's qualification constants were measured with SmileBASIC's filesystem traffic and its own native thread. Civ VII writes saves heavily and drives different request patterns, so the general-heap layout, cache window, and receive-order margins must be re-measured from the game process. The abort controls are the intended early signal: they stop before any overwrite. See [[Wave harness]] and [[Probe plan]] P7.
