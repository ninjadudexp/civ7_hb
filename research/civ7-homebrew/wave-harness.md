---
tags: [civ7, switch2, homebrew, research, m2]
status: active
---

# Wave harness

Milestone M2: reproduce ns2jb's threaded RPC timing inside Civ VII without needing executable pages, so filesystem qualification can be measured from the game process.

## What exists now

- `research/CivSwitch/harness/console.js` — console side, loaded after `core.js`. Relocates SVC wrappers by scanning for `svc #imm; ret`, stages page-aligned `0x1000` buffers, sends synchronous requests, posts asynchronous requests, collects replies, timestamps with the native tick, and runs a wave plan. Control port `9027`.
- `research/CivSwitch/harness/host.py` — workstation side. Holds the CMIF packet codec, domain and control framing, close-object, path packets, context marking, reply parsing, the filesystem bootstrap, the reservation group, path staging, wave profiles, and report files in `harness/runs/`.
- `research/CivSwitch/harness/wire-selftest.cjs` + `wire-selftest.py` + `wire-diff.py` — byte parity check against ns2jb's own codecs.

## Verification actually performed

```sh
node wire-selftest.cjs && python wire-selftest.py && python wire-diff.py
```

Result: 17 frames, 4 crafted replies, `identical: true`. The Python port produces byte-identical frames to `engine/shared/ipc.cjs`, `engine/shared/domain.cjs`, and `engine/switch2/memory/paths.cjs`, and parses crafted replies to the same values.

## Things copied from ns2jb, kept verbatim

| Item | Value | Source |
| --- | --- | --- |
| Reservation pages | 59 | `probe-reserved-gap.cjs` |
| Expected allocation | `59 * 208` | `probe-reserved-gap.cjs` |
| Allocation counters | command 1009, word 5 | `probe-reserved-gap.cjs` |
| Group object creation | command 1200 | `probe-reserved-gap.cjs` |
| Trigger extent | 0x2440, marker offset 0x2340 | `probe-reserved-gap.cjs` |
| Marker / absent names | `SPAN_MARKER`, `SPAN_ABSENT` | `probe-reserved-gap.cjs` |
| Context values | 9 sequential, 3 concurrent | `probe-reserved-gap.cjs` local `context()` |
| FS bootstrap | named port `fsp-srv`, command 1 with pid, command 0 ConvertToDomain | `app/domain.cjs:openDomain` |

## Current state (2026-11-03)

Working on hardware:

- `console.js` loads; the host pushes the decoded SVC table + relocated SDK functions; tick ≈ 31 MHz; `ready: true`.
- `setup`: mounted save `IFileSystem` (via the mount chain, see [[Filesystem access]]), scratch tree created (all canonical `0`), free space ~62.6 MB, counters read.
- `reserve`: 59 `CreateObject` (`1200`) + wrapped `outer`; measured cost **~12160–12176**, not `59 * 208 = 12272`.
- Rules learned: the loader is one payload per boot; `setup` copies the save filesystem and is **single-use per boot**; counters `1009` are a plain command on an initialised session.

**The wave is not safe yet.** The first `abort-control` run aborted the game process (hard reboot). Root-cause hypothesis: the concurrency model is wrong. ns2jb drives the interleaving from **13 native worker threads pinned across cores** with native sleeps (`app/native-worker.cjs`, `thread-code.json`, `timing-code.json`); this harness posts every request from the single JS thread with JS `sleep`. Without those workers the receive-order geometry and spacing differ, so the heap shaping can abort the FS process.

Also still needed: the native `memcpy` guard body (`0x586fb0`) that the abort profiles trip, and the `measure`/`counters` gating on the native side.

## Next

1. Port the thread primitives (`create`/`join`/`exit`/`loadTable`/`call4`/`tryWait`/`longjmp`, `sdk-proxy`) and build a native worker that posts and sleeps independently — relocated names are in `signatures/nn-sdk-signatures.json`.
2. Then re-run `abort-control` with workers and native timing, gating on the **measured** reservation cost.
3. Only then `extent-absent`, `extent-present`, `positive`.
