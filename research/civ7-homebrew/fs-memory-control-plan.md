---
tags: [civ7, switch2, homebrew, exploit, fs, memctl, plan]
status: active
---

# FS memory control port — action plan

Written **before** implementation (session-crash protection). The port of
`research/ns2jb/engine/switch2/memory/fs-memory-control.cjs` (376 lines) into the
CivSwitch Python harness. Source of truth for state: [[handoff]] §41–§43 and
[[tracker]] round-29.

## Current state (as of the crash)

- **UPDATE (pass-6 review, offline):** P0 ✅, P1 ✅ (10/10 offline selftest), P2 wired ✅
  (custom-payload wave path + copyField port + `memctl` stage). Pass-6 review fixes applied:
  `copy_field_wave` re-reserves + frees pages per wave, breaks at `required_copies`, and accepts on
  the `CIV_HELD_GATE`/`CIV_HELD_MIN` metric; large writes are chunked (console 0x8000 frame cap);
  `leak_read` re-poisons per block; the normalized canary chain is scratch-rooted; and
  `CIV_RELEASE_CLOSE_GATE` makes the trigger wait for the release's close. Everything is
  offline-verified only; no console boot since the pass-5 leak fixes. Next action =
  P3 (first console validation run), see the P3 section below.
- `harness/leak-cycle.py` v2 exists (80 lines): stage 0 romfs control reads with the
  corrected `leak_read` (reusable 0xa5-poisoned rx_page, console read-back,
  all-poison detector), findNativeLinks-faithful `scan()`, CMIF Close, one setup per
  payload. Stage `full` is gated behind wave prerequisites and only reaches the
  extent-present wave — **no controller machinery yet**.
- `harness/host.py` has `leak_read` (fixed) and `worker_wave`; the wave transport
  (copyField-equivalent) and memset/IPC primitives exist on Civ per handoff §42/§43.
- Reference controller (`fs-memory-control.cjs`) provides: retarget via
  `signed-cache-plan.cjs` (pure arithmetic — already declared portable, handoff §41),
  `select`/`read`/`writeAt` with knownRanges bookkeeping, `qualifySignedHeader`
  (control-handle diff + double-poison read verify + marker write verify + restore),
  `qualify-single-bucket` (cycle 0x28000), and a socket dispatch layer we do NOT need
  (harness is one process, one setup).

## Action plan

### P0 — port `signed-cache-plan.cjs` → `harness/signed_cache_plan.py`
### P0 — port `signed-cache-plan.cjs` → `harness/signed_cache_plan.py` ✅ DONE (verified)

1. ✅ Ported `plan(address, storageSize, cycle)` verbatim: 40-byte UTF-8 signed-one-shot
   header, `writes` list (copyField byte payloads), `readOffset`, `addressBias`,
   `entryOffset`. File: `CivSwitch/harness/signed_cache_plan.py` (memoized backtracking
   search, 5 s deadline, UTF-8/forbidden-byte machinery preserved exactly).
2. ✅ **Byte-for-byte cross-checked against the .cjs via node**: 6 (target, storage,
   cycle) triples (incl. both cycles and Civ's 4.975 GB storage size) + 3 bad-arg
   error cases — all identical (header hex, readOffset, base, entryOffset,
   addressBias). Selftest: `CivSwitch/harness/signed_cache_plan_selftest.py`.
3. ⬜ Constraint to preserve (deferred to P1): `next.readOffset + length <= storageSize`
   check — belongs to the controller's select/read path, not the planner.
### P1 — port the controller core → `harness/fs_memory_control.py` ✅ DONE (verified)

1. ✅ Ported the full controller core (cjs-faithful arithmetic throughout): state
   (`activePlan`/`knownRanges` merged intervals/`cycle` ctor arg default 0x500000),
   `select` (bias arithmetic + aligned-bucket hit test + retarget branch guarded by
   `allowRetarget`/`forceRetarget`), `readBlock` (reusable poisoned rx region, IPC
   Read cmd 0, chunked read-back, all-poison detector, failed-read artifact), `read`
   (1..0x600000 with cycle-bound step + overlap-window prefix slicing), `writeAt`
   (aligned ≥0x8000 guard, command 1 send-descriptor path), `qualifySignedHeader`
   (control-handle diff, 0xa5/0x5a double-poison verify, SIGNED-RW marker write
   verify, full-block restore), `qualifySingleBucket` (formerly-different bucket,
   0x28000→0x4000 demotion).
2. ✅ **10/10 offline selftest passed** (`fs_memory_control_selftest.py`, fake console
   + fake DMA channel): rememberRange merge, select hits/retarget refusal, dispatch
   retarget, read chunking + overlap slicing, write guard, write happy path, failed
   read + all-poison detector, qualifySignedHeader sequence, qualifySingleBucket
   demotion, validation + rx reuse/grow discipline. Two fake-DMA off-by-N bugs found
   and fixed during the run (payload + readback offsets).
3. ⬜ Deferred to P2: `copy_field` is injected but stubbed (refuses until the wave
   wiring lands); `control_handle` (retained.storages[0] equivalent) still an open
   item.

Original port order (all steps now implemented):

1. **State**: `activePlan`, `knownRanges` (sorted merged interval list,
   `rememberRange`), `cycle = 0x500000` default (bucketPeriod measured quantity —
   Civ's value NOT yet verified; 0x28000/0x500000 are the two candidates).
2. **`select(address, length)`**: bias arithmetic, alignment check, retarget branch
   (writes via copyField waves), refuse unless `allowRetarget` (mirror the cjs guard:
   `retarget: true` required, `forceRetarget` implies it).
3. **`readBlock(address, length)`**: memset poison → IPC Read (command 0 on the
   candidateHandle) with receive descriptor at the same output buffer → read back via
   the console read command → all-poison detector → failed-read artifact dump on
   nonzero result. Reuse `leak_read`'s corrected rx_page discipline.
4. **`read(address, length)`** (1..0x600000): chunking below the large-read failure
   bound (`step = cycle <= 0x40000 ? 0x40000-cycles : 0x40000`), overlap-window logic
   when `direct` is false (`prefixKnown` via knownRanges), local slice instead of
   header rebuild. Partial-read artifact on exception.
5. **`writeAt(address, bytes)`**: alignment guard (aligned large writes may bypass the
   controlled cache — `offset % 0x4000 != 0 or bytes.length < 0x8000`), send descriptor
   path, command 1.
6. **`qualifySignedHeader(address)`**: needs an ordinary content control handle
   (retained.storages[0] equivalent on Civ — NOT yet identified; see Open items), the
   before/after diff against ordinary content, double-poison read verify (0xa5 / 0x5a),
   marker write (`SIGNED-RW` 16-byte marker at +0x108) with verify and restore.
7. **`qualifySingleBucket(address, expectedHex)`**: requires cycle 0x28000 and a
   formerly-different bucket; keep the `bucketPeriod = 0x4000` demotion.

### P2 — wire into `leak-cycle.py` as stage `memctl` ✅ wired (offline, NOT yet console-verified)

1. ✅ `host.py` `_stage_extent_ns2jb(profile, payload=None)`: custom payload path with
   ns2jb's wave() checks (`/..\\` prefix, no embedded NUL, 5-piece split, full
   0x800 trigger piece); profile branches skipped when a payload is supplied.
2. ✅ `host.py` `worker_wave(profile, tries=1, payload=None, read_size=None)`: payload
   pass-through + hole read-size override (grows the hole buffer if the override
   exceeds the staged PROBE_READ_SIZE).
3. ✅ `host.py` `Harness.copy_field_wave(tail, name, required_copies, read_size)`:
   ns2jb copyField port — builds `/..\\` + 'B' fill + tail at MARKER_OFFSET (0x2340)
   + NUL payload, runs waves in batches, acceptance = windowOk && preReleaseUsed >=
   MINIMUM_CACHE_HELD (0x2810) && changedPaths empty; FIELD_COPIES = 2.
4. ✅ `leak-cycle.py` stage `memctl`: gated on the wave prerequisites (windowOk AND
   release-before-trigger), then instantiates FsMemoryControl with the correct
   keyword-only ctor args (candidate_handle / storage_size / initial_plan /
   input_addr / copy_field), initial plan via `signed_cache_plan.plan(0x123456789,
   storage_size, 0x500000)` (test target — a real run derives the target from the
   leak-read pointer candidates), dispatches 'info'.
5. ✅ Syntax verified on all four files (ast + py_compile).
- ⬜ NOT yet done: a console boot. The copyField wave path (`worker_wave` with a
  payload) has never run against the console — expect first-boot friction (see P3).
- Keep **one setup per payload** (console hygiene rule, handoff §43) — memctl
  currently reuses the setup the romfs stage performed in the same process.

### Pass-6 review fixes (offline, this session)

1. `copy_field_wave`: `self.rereserve()` + free the previous wave's `wave_allocs` before every
   field-copy wave (each wave's `release` role closes `outer`; `worker_wave(tries=1)` never
   re-reserves). Inner loop breaks at `confirmed == required_copies`. Acceptance threshold is
   `CIV_HELD_MIN` under `CIV_HELD_GATE=1`, else `MINIMUM_CACHE_HELD` (0x2810). The `tries>1`
   recursion forwards `payload`/`read_size`.
2. All writes that can reach 0x4000 bytes are chunked at 0x1000 bytes: `host.write_chunked`
   (leak_read poison) and `fs_memory_control._write` (poison fills, `write_at` input).
   `leak_read` re-poisons `rx_page` before each block's IPC.
3. Normalized canary chain rooted at `self.scratch` (`walked = "/" + parts[0]`).
4. `CIV_RELEASE_CLOSE_GATE=1`: release signals a `closed` gate after `close_object(outer)` and the
   trigger waits on it, so `release.endUs < trigger.startUs` no longer depends on the early
   `released` handshake. leak-cycle sets it for its prerequisite wave.
   **Console caveat:** under the frozen harvest config (`CIV_RELEASE_EARLY=1`) the close is
   ~4.4 ms while protected windows end ~3.7-4.2 ms, so `windowOk` (requires `allPending`) may not
   co-hold; P3 must confirm or run the memctl wave with `CIV_RELEASE_EARLY` off.

### P3 — console validation run (first boot after the port)

**Progress (live, 192.168.103.215):** two bugs surfaced. (1) The first `python leak-cycle.py
memctl` silently took the romfs branch — `stage` was parsed AFTER `sys.argv` was clobbered to
`["host.py"]`. Fixed (parse stage first). (2) The wave then died mid-poll on three fresh boots,
including a **baseline wave with `CIV_RELEASE_CLOSE_GATE` off** and one setup — which ruled out
duplicate-setup and the gate wait. Root cause: the new `closed` event sat at `ctrl+0x1c`; the SDK
event struct is 8 bytes and the gates are 8-byte spaced, so `initialize`/`clear` clobbered
`guard` (0x18) and `CF` (0x20) on every wave. Fixed by moving `closed` to `ctrl+0x40` and zeroing
the ctrl page to 0x80. Romfs control reads were clean. (The gate fix was validated live: the
next run survived all four waves instead of dying mid-poll.)

**Pass-7 review fixes:** pacing knobs (`CIV_RELEASE_DELAY`, `CIV_TRIGGER_SLEEP`,
`CIV_CANARY_DELAY`, `CIV_GUARD_DELAY`, `CIV_GUARD_SPREAD`, `CIV_FILLER_DELAYS`) are now read at
wave time, so the sweep's overrides take effect; the diagnostic baseline is opt-in
(`CIV_PREREQ_BASELINE=1`); `stage_extent`/`_stage_extent_ns2jb` allocations are recorded in
`wave_allocs`; `stage full` exits clearly; `FsMemoryControl._ipc` uses CMIF `canonical`.

**Live geometry result (one setup, 4 variants):** close gate ON gives negative `close_gap`
(-107/-2661/-2659 µs) but `allPending=False`; gate OFF gives `allPending=True` with
`close_gap=+6906` µs. **windowOk and release-before-trigger do not co-hold with the current wave
structure** — the trigger waits for the close after the protected windows have ended. Needs
console tuning (send protected roles later, or close earlier), not a code fix. Per-variant
reports are saved as `runs/prereq-variant-N.json`.

**Payload lifetime:** a second `memctl` run on the same payload wedged fsp-srv (open-but-silent).
One run per payload. `CIV_PREREQ_SKIP=1` runs the controller against clean RomFS without the
wave, to validate the controller independently.

**★ P3 step 1 DONE (round-31): prerequisite passed and the controller read live.** The missing
knob was `CIV_COMMIT_DELAY`: with commit at t=0 the release's `CloseObject(outer)` defers behind it
to 3.5–4.5 ms; delaying commit past the close keeps `release.end` at ~1.0 ms while the trigger
stays in the harvest ~2.4 ms window. Winning sample:

```
CIV_RELEASE_DELAY=300000  CIV_PRE_BRACKET off  CIV_COMMIT_DELAY=1800000
  allPending True, windowOk True, closeEnd-vs-triggerStart -1432 us
```

Then `memctl` opened the candidate handle, primed it (result 0), dispatched `info`, and the smoke
`read` returned 4096 bytes of real RomFS (`runs/memctl-memory-1.bin`). Next: run the actual alias
chain (positive Stage A + installCombinedLeak/copyField Stage C) and the over-read, and resolve
`selectedMarker` (still False).

**A–D alias chain implemented (`alias`/`full` stage).** `cache_address_plan.nul_writes` is ported
(7/7 vs the .cjs); the stage runs A positive → B reserve/candidate/prime → C `installCombinedLeak`
→ D over-read, gated by `CIV_ALIAS_UPTO` (positive|prime|install|read). Live, Stage A **failed**:
the positive wave qualified (AP/win True) but the canary was unchanged (`canaryType=1`) and the
trigger returned `0x2EEA02` (normalizer abort) at the default marker offset `0x2340`. This is the
campaign's `selectedMarker` blocker — the alias chain cannot proceed until the positive trigger
resolves to the marker. The `positive` branch now honours `CIV_MARKER_OFFSET`; a live sweep
(`0x2140`, `0x1b40`) still gave `canaryChanged=False` / trigger `0x2EEA02` for both. §30's
`0x2140` result was for *extent-present* and resolved to type 0 (a directory), not
`selectedMarker` (type 1) — the fold carries a directory regardless of marker offset, so this is
not a marker-placement problem. The lead remains the normalized one-descriptor form
(`CIV_NORMALIZED_PATHS=1`, round-25b: uncreatable on Civ VII). Stage A needs a new approach. The
A–D scaffolding is complete and ready once the positive overwrite lands.

1. romfs control stage: identical hashes across calls (regression check on the §43 fix).
2. Wave prerequisites: log AP / selectedMarker / windowOk / closeEnd-vs-triggerStart.
3. If prerequisites pass: full A–E chain + controller select → read of the aliased
   heap window → `scan()` for pointer candidates and wave strings.
4. Update [[tracker]] + [[handoff]] §44 with the outcome either way (nulls included).

## Open items blocking the port

- **Civ's bucketPeriod**: measured quantity, must qualify on Civ's fsp-srv
  (0x28000 vs 0x500000). Port must default to 0x500000 and expose the sweep.
- **Ordinary content control handle** (cjs `retained.storages[0]`): which Civ handle
  plays that role; needed by qualifySignedHeader and qualifySingleBucket only.
- **bulk transfer** for reads > 0x8000 (cjs `bulk.read`): check whether the Civ
  harness path (`probe_async.py` / console read command) covers 0x1c000 over-reads
  or needs the bulk path.
- **aliasOffset 0x1400000**: leak-cycle.py's stage C pins this for the over-read;
  confirm it matches the planner geometry after the [[address-planner]] lands on
  Civ's 12160-byte reservation.

## Related

- [[retained-memory]] — the steady-state consumer this feeds (`retained-inject-holder`)
- [[address-planner]] — alias-offset geometry for Civ's 12160 reservation
- [[filesystem-breakout]] — the M3 alias
- [[tracker]] round-29 / [[handoff]] §43 — pass-5 corrections
- [[tracker]] round-30 / [[handoff]] §44 — the port, the pass-6/pass-7 fixes, and the live P3 result
