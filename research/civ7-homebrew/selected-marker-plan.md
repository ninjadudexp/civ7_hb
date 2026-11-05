---
tags: [civ7, switch2, homebrew, exploit, selectedMarker, wave, plan]
status: active
---

# selectedMarker hunt — action plan

Written **before** the next console session (crash protection, per session policy). Source of
truth for state: [[handoff]] §35–§45, [[tracker]] rounds 24–32, [[probe-plan]] P7. The A–D
alias chain ([[fs-memory-control-plan]]) is fully scaffolded and its Stage A is gated on this
campaign: **without the trigger resolving to the marker (result 0 + inline type 1), the canary
slot is never freed and Stage B can never reuse it.**

## Current state (as of §45 / round-32)

- **The window is solved and deterministic** on the frozen harvest config
  (`CIV_RELEASE_DELAY=1500000`, deep canary, 10/10 all-span samples in recent boots:
  `allPending=True`, `windowOk=True`, `timingValid=True` after the origin fix).
- **selectedMarker is the only failing gate.** The trigger resolves a real object (result 0)
  whose inline type is 0 (a directory), or terminates at 0x202 before the basename completes.
  Live marker sweeps at `0x2140` and `0x1b40` (round-32) changed nothing: `canaryChanged=False`,
  trigger `0x2EEA02` for both.
- The trigger's terminal component is **insensitive to**: marker position (round-19),
  descriptor form (round-25), close phase (round-26: before-start → type 0, inside-window →
  0x202 — both terminate before the basename).
- **The normalized one-descriptor form is CONDITIONALLY closed on Civ VII** (round-25b): the 1440-char canary
  path cannot be created; the law is likely `0x300` (768) as FS MaxPathLength. The canary must
  stay deep-form (`CIV_CANARY_DEPTH`), where it resolves result 0 / type 1 reliably.
- The controller port ([[fs-memory-control-plan]]) is live through Stage A: candidate handle
  opens, GetSize 4.975 GB, 16-byte prime result 0, controller read returns real RomFS. The wall
  is entirely Stage A / selectedMarker.

## Eliminated hypotheses (do not re-sweep without new evidence)

| Hypothesis | Round | Verdict |
| --- | --- | --- |
| Marker offset (`CIV_MARKER_OFFSET` 0x2140/0x1b40) | 19, 32 | Null — the fold carries a directory regardless |
| Descriptor form (normalized one-descriptor) | 25, 25b | Null in round-25b — but that run predates the scratch-root fix (walked from `""` at the FS root); reopened, see SM0 |
| Close phase (`CIV_RELEASE_DELAY` 1.5–2.5 ms) | 24, 26 | Real response, but neither phase yields type 1 |
| Trigger-descriptor size | 25 | Byte-identical to ns2jb — server-side, not descriptor-side |

## Surviving hypotheses (queue order)

1. **Canary depth** (`CIV_CANARY_DEPTH=3`) — the heap layout the fold resolves against is the
   one thing not yet swept (5 constant since round-9). Attempt 3 in round-25 wedged at
   re-reserve; the test is still pending, not failed.
2. **`CIV_CANARY_SIZE`** — the canary's file size (0x5a0 default) sets the extent-alias
   boundary in the heap; ns2jb's canary geometry differs and this knob has never been touched.
3. **Heap-alias geometry (§40's offline analysis)** — Civ's reservation measures **12160**
   vs ns2jb's 12272 (`59×208`): a 112-byte shift in the per-object geometry. fsp-srv copies
   the pieces into its own heap during the seeds' processing; the trigger's resolution flows
   through them only if the copies are ADJACENT. A shifted alias makes the resolution cross
   into fsp-srv's copies at the wrong offset, picking up a NUL/heap byte before the marker's
   basename completes — exactly the observed 202-before-marker / 0-at-boundary split (the
   phase response follows which side of the misalignment the resolution stopped on).
   Requires applying the [[address-planner]] to Civ's measured geometry to derive the expected
   alias offset, then placing the marker where the shifted fold reaches it (or an encoded
   offset like ns2jb's `address-encoding-control` profile).

## Action plan

### SM0 — normalized-path attempt — **REOPENED by a one-byte descriptor fix** (was wrongly CLOSED)

**Boot 1 (bare ambient config):** all 3 prerequisite variants failed AP=False — the harvest knob
set (PIN_EARLY/RELEASE_EARLY/HOLE_EARLY/PRE_CLONE/TRIGGER_TAIL/CANARY_DELAY/GUARD_DELAY and
FILLER/TAIL_DELAYS, the config behind rounds 24–26's deterministic all-span window) was never
applied. The canary returned 0x202 / type 0 in every variant (unresolved). Older winning
attempt2 config used `RELEASE_DELAY=900000`; the round-31 winner (`RELEASE_DELAY=300000` +
`COMMIT_DELAY`) is a different variant line — both should be tried on future boots.

**Boot 2 (full harvest env + `CIV_NORMALIZED_PATHS=1`):** the 1440-char normalized chain
**CREATES on Civ VII** — all 44 chain steps returned `0x0` (26→1412 chars), the file create at
len=1440 returned `0x0`, and GetEntryType verified type 1 (no WARNING printed, so the failure
path never fired). **Round-25b's MaxPathLength verdict is REFUTED** — the long path creates
fine on Civ's filesystem. BUT the wave's canary lookup still returned **`0x202 / type 0`** in
all variants, even though the file exists and verifies via the setup's direct `path_send`.

**Mechanism (CORRECTED after the dev's FS answer):** the earlier "query-time limit" hypothesis
is **wrong**. The real cause is a one-byte descriptor bug in the wave's canary staging:
`stage("canary", canary_text, CANARY_SIZE)` declared **1440** bytes, but the staged payload is
text + NUL = **1441** bytes — the terminator fell OUTSIDE the declared window, so the server
read 1440 bytes of unterminated path → `0x202`. The setup's verify always used `len(text) + 1`
(1441, NUL included) and resolved fine — that is why creation and setup-verify succeeded while
the wave lookup failed. The deep/short forms are unaffected (their NULs fall well inside the
1440-byte window). **Fixed** (`host.py`, normalized branch: `CANARY_SIZE + 1`).

**Consequence for Stage A:** the normalized form is REOPENED as the Stage-A staging — a wave
with a RESOLVED canary (0/type 1) holding a real heap slot is exactly what the positive wave
needs to free. **Retest results (boot 3, post-fix, 2 × extent-present):** canary row =
**0/type 1 in BOTH samples** — the one-byte descriptor fix is CONFIRMED live. But the window
does not span: the canary starts at 2306 (before the trigger's 2466) and ends at 3880 —
~400 µs SHORT of the trigger's end (4278) → `allPending=False`. The pre-fix 0x202 walks were
SLOW (4943–8771 µs); the post-fix resolved walk is FAST (~1574 µs — the 44-dir chain's dentry
cache is warm right after creation). The normalized form now has the OPPOSITE problem: it
resolves but doesn't span. It needs ~2000 µs of duration to span the ~1.8 ms trigger window —
a longer canary (`CIV_CANARY_SIZE` up to ~0x7c0 ≈ 60 levels, ~37 µs/level) is exactly SM2's
sweep, now with a concrete rationale. Next queued: 1 × positive (Stage A) on the current
normalized staging to collect `canaryChanged`/trigger data even without the window.

**Positive-wave attempt (same payload, wave 3):** the direct-`host.py` call reached the console
(chain re-created 44/44 → 0x0, reservation wrapped outer=206) but the clone inside
`worker_wave` **timed out** — the payload's wave budget was spent (2 extent-present + this
setup = the documented ~3-cycle lifetime). No positive JSON was written. Console sockets still
open (9029 up) but the payload is spent per the hygiene rule. **SM0's canary-resolution fix
stands confirmed; the positive-wave fold test still needs a fresh payload.** Next boot:
relaunch → flow load → `worker-wave positive 1` FIRST (fresh budget), with the same normalized
env — that single wave answers Stage A (`canaryChanged`, trigger type, `selectedMarker`),
window or not.

**Boot 4 (fresh payload, positive wave executed FIRST as queued):** the wave ran clean —
canary resolved **0/type 1** (the fix holds on positive too), but the trigger returned
**0x2EEA02** (the normalizer abort, `result 3075074`), `canaryChanged=False`,
`selectedMarker=False`, `windowOk=False`, `AP=False`. Also new: `changedPaths=['filler0']`
(an unexpected filler change; note but do not treat as a signal without a qualified window).
**The timing is the headline:** trigger 2436→8687 µs, canary 2273→8590 µs — the canary ends
only **97 µs short** of the trigger's end, with ~163 µs of start headroom. Compared with the
post-fix ~400 µs gap, the positive payload's extra work (the `/\..\` piece) slows the canary's
walk toward the trigger's duration. **A +150 µs shift (`CIV_CANARY_DELAY=2650000`) should make
the canary span the trigger and qualify the window with a RESOLVED canary — the first time
both conditions hold together** (all previous all-span samples had the deep-form 0x202 canary
or a short-form canary whose geometry does not match the positive piece layout).

**Shift-test attempts (same payload, after the positive wave):** attempt 1 failed at the flow's
harness-load step (connection refused on `:9029` — the harness died during load; the flow ran
relocation load → mounts scan → stop → harness load, then the command could not connect).
Recovery worked: `relocate_host.py stop` → `host.py load` → `:9029` OPEN again. Attempt 2 got
through setup (canary chain 44/44 → 0x0, reservation wrapped) then **timed out mid-wave**
(open-but-silent) — the payload was spent (1 positive + 2 shifted setup cycles). Console needs
a game relaunch before the next run.

**QUEUED for the next payload (the concrete next test):** game relaunch → `flow.py load` →
2 × `worker-wave extent-present 2` with the full harvest env + `CIV_NORMALIZED_PATHS=1` +
`CIV_CANARY_DELAY=2650000` (+150 µs). Success criterion: `allPending=True` with the canary row
still 0/type 1 → the window qualifies WITH a resolved canary → immediately queue 1 × positive
on the same staging (fresh payload) for the real Stage-A fold test. If the shift overshoots
(canary start passes the trigger's start), back it down to +100 µs (2600000).

**Reporting gap (minor):** the wave JSONs carry no `setup` key, so `canaryChainVerify` was only
observable in the boot log — worth fixing in `host.py` later but not a blocker.

### SM1 — canary depth sweep — **STRUCTURALLY CONFOUNDED with timing (SM1 closed — superseded for staging by SM0's fix)**

**Depth-3 boot (3 samples, frozen harvest env, `RELEASE_DELAY=900000`):** all three samples
resolved cleanly (`canary 0/type 1`, `changedPaths=[]`, `heldFreed` stable at 5344) but
**`allPending=False` in every sample** — and the mechanism is structural, not a lottery:

| sample | trigger start/end (µs) | canary start/end (µs) | canary end vs trigger end |
| --- | --- | --- | --- |
| 1 | 2438 / 9608 | 2276 / 8391 | ends 1217 µs short |
| 2 | 2458 / 4429 | 2302 / 3604 | ends 825 µs short |
| 3 | 2478 / 3954 | 2323 / 3284 | ends 670 µs short |

**The canary's lookup cost scales with its path depth** — depth 5 (the constant since round-9)
makes the canary slow enough to stay in flight past the trigger's end and hold `allPending`;
depth 3 shortens the walk by ~700–1200 µs and the role ends before the trigger does. The
canary's start must stay before the trigger's start (~2.45 ms) while its end must clear the
trigger's end (~3.9–4.4 ms), and `CIV_CANARY_DELAY` shifts both — there is no delay value
that compensates a depth change: the window needs the canary's **duration**, not its phase.
**Depth is a timing knob, not a separable geometry knob** — SM1 cannot test heap geometry
through depth on this wave structure. Depth 2 would end even earlier; depth 4 only partially
compensates (~300–500 µs short) — not worth boot budget unless SM2 also nulls.

Original SM1 command (depth 3, run live):

```bash
cd research/CivSwitch/harness
CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000 CIV_TRIGGER_TAIL=100000 CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=900000 CIV_CANARY_DEPTH=3 CIV_CANARY_DELAY=2500000 CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 CIV_FILLER_DELAYS=2280000,2100000 CIV_TAIL_DELAYS=2280000,2050000,2080000,2250000 python harness/flow.py 192.168.103.215 worker-wave extent-present 3 2>&1 | tee runs/sm1-depth3.log
```

(`flow.py` sequence: relocation load → mounts scan (fresh accessor per boot) → stop → harness
load → command. Never load `console.js` directly — that bypasses the accessor refresh and
faults the payload, the boot-1 lesson.)

### SM2 — canary size sweep (needs one relaunch)

`CIV_CANARY_SIZE` at 0x5a0 (control), then 0x800 and 0x400 (one sample each). Watch the same
signals as SM1 plus `heldFreed`'s scale (the canary's extent contributes to the reservation).
Record even nulls: a size-invariant terminal component strengthens SM3.

### SM3 — address-planner on Civ's measured geometry (offline, the deep dig)

Apply `cache-address-plan.cjs` / `signed-cache-plan.cjs` to the **measured** Civ geometry:

1. Measure Civ's actual per-object piece adjacency (from the round-28/31 artifacts or a fresh
   controlled leak-read, if Stage A's blockage allows one via the aliased route).
2. Compute the alias offset Civ's 12160-byte reservation implies vs ns2jb's 12272 — the
   112-byte delta is the hypothesis; the planner turns it into a concrete expected offset.
3. Derive the marker placement that the shifted fold reaches (or an encoded offset like the
   `address-encoding-control` profile), then test it in a boot with `CIV_MARKER_OFFSET` set to
   the computed value.

Abort criteria: if the planner's derived offset lands inside an already-swept range
(`0x2140`–`0x1b40` covered both nearby candidates), the geometry hypothesis is wrong too and
the next dig is [[retained-memory]]'s lower-window machinery (§41's fallback).

### SM4 — reconcile with the §30 type-0 result

§30's `0x2140` sample resolved result 0 / type 0 for *extent-present* — the fold fired and
carried a directory. That is consistent with the geometry hypothesis (the fold reached a
directory boundary before the marker) but also with the canary-form hypotheses. Cross-check
SM1–SM3 results against §30 before declaring any of them dead: a sample that resolves type 0
under one variant and 0x202 under another is real information about where the fold stops.

### SM5 — Stage-A trigger-form isolation probe (cheap; decisive for the whole `..` chain)

Round-32's Stage-A failure is the positive trigger returning **`0x2EEA02` with
`nativeGuard=0`** — the path normalizer aborting — while every Stage-A retry swept
`CIV_MARKER_OFFSET`, which cannot touch the prefix piece. Also note `host.py`'s staging
comment cites round-19's "0x2140 resolves" — that measurement was **extent-present**
(type 0), not positive; the conflation sent the hunt down the marker-offset path.
Isolate the form from the fold (one boot, no wave budget):

1. Sequential `path_send` (GetEntryType, save FS) of the positive form: `/\..\` + 'B'
   fill + `selected` at 0x2340 + '/' fill. `0x2EEA02` ⇒ Civ's normalizer rejects the
   form outright and **no window or marker tuning can ever fix Stage A** — the positive
   profile needs a re-derived prefix.
2. Same with `/../` (the `isPositive=false` form) as a custom payload: if the backslash
   is the discriminator, re-point the alias chain at the forward-slash form.
3. Marker inside piece 0 (`CIV_MARKER_OFFSET=0x240`): the trigger's own 0x800 input
   carries piece 0 verbatim, so a marker completing there resolves without the fold —
   a per-offset fold-necessity test (overlaps SM6).

### SM6 — downward marker sweep (fold carry limit)

All swept offsets (0x1b40/0x2140/0x2340) sit in piece 4, ≥0x1300 bytes into the
assembled path. The direct form resolves 1440-char paths (SM0 boot 2), so any reach cap
is fold-specific. Sweep **down**: `CIV_MARKER_OFFSET=0x240 / 0x640 / 0xa40 / 0x1400`
(piece 0 → 1 → 2 → 3), extent-present, one boot, watching the trigger's result/type at
all-span. The first offset where resolution stops marks the fold's carry limit — the
missing input to SM3's alias-offset analysis. If piece-0 markers resolve type 1,
`selectedMarker` is reachable inside the carry range and the remaining work is extent
geometry, not windows.

## Console hygiene

- One `leak-cycle.py` run per payload; relaunch the game for each sweep (§43/§45 lesson).
- The controller ([[fs-memory-control-plan]]) must not run in the same process as a sweep that
  wedges — keep `memctl`'s smoke test out of the SM boots until Stage A lands.

## Related

- [[address-planner]] — the arithmetic core; SM3's tool
- [[fs-memory-control-plan]] — the A–D chain this plan unblocks (Stage A gate)
- [[filesystem-breakout]] — the M3 alias the fold drives
- [[handoff]] §35–§45 / [[tracker]] rounds 24–32 — chronological evidence
- [[probe-plan]] P7 — the wave-qualification phase this hunt belongs to

## Boot log — 2026-11-04/05 shift chase

- Boot 5 (`CIV_CANARY_DELAY=2650000`, +150 µs): two attempts recorded
  (`worker-wave-extent-present.json` + `-attempt2.json`, 2 of 3 samples). Both resolved
  `AP False / win False / sel False / canaryChanged False`; canary row stayed 0/type 1.
  Timing: trigger 2468 → 4447 µs, canary 2458 → 4043 µs — canary still ends **404 ms short**
  of the trigger's end, so the +150 µs phase shift alone does not cover the window.
- Boot 6 (`CIV_CANARY_DELAY=3150000`, +500 µs): timed out waiting for reply at the wave
  boundary (payload exhausted). Console must be rebooted, not just relaunched.

**Next action (queued):** relaunch game → reload payload → run ONE extent-present sample at
`CIV_CANARY_DELAY=3150000` (+500 µs). Success = `allPending=True` with the canary row still
`0/type 1` — that is the first live confirmation that the near-span exists with the canary
resolved. If the canary overshoots (start after the trigger's end), back the shift down to
+400 µs (`3050000`) on the following boot.

### Boot 7 — +500 µs single sample (console rebooted after boot-6 wedge)

Result (`worker-wave-extent-present.json`, 1 of 3 samples): `AP False / win False / sel
False / canaryChanged False`, canary row 0/type 1, trigger row **0/type 0**.

- Timing: trigger 2479 → 8549 µs, canary 2966 → 8836 µs. The canary now **starts 487 µs
  after** the trigger's start and **ends 287 µs after** the trigger's end — the +500 µs
  shift **overshoots**. The queued success criterion (`allPending=True` with canary 0/type 1)
  is still unmet; the sweet spot is between +150 µs (ends 404 µs early) and +500 µs (ends
  287 µs late).
- **New wrinkle — durations are not boot-stable.** The trigger's span ballooned from
  ~2.0 ms (boot 5: 2468→4447) to ~6.1 ms (boot 7: 2479→8549), and the canary's own duration
  ballooned from ~1.6 ms to ~5.9 ms. Durations scale with overlap/contention, so a constant
  `CIV_CANARY_DELAY` shift does not compose across boots — phase-only tuning is unreliable
  and the window is being chased against a moving trigger end.
- Trigger row resolved `0/type 0` (boot 5 had 514/type 0) — the trigger's own resolution
  changed between boots, another sign the interleaving is not reproducing.

**Next action:** 1 more sample this payload at `CIV_CANARY_DELAY=3050000` (+400 µs, the
planned overshoot fallback), keeping the third sample in reserve. If that also misses the
window, stop tuning the constant and switch to measuring the trigger's end in-run
(`CIV_TRIGGER_TAIL` / a canary delay derived from an observed trigger start) rather than
tuning a fixed phase offset across boots.

### Boot 8 — +400 µs single sample (2nd of 3 on this payload)

Result: `AP False / win False / sel False / canaryChanged False`, canary 0/type 1,
trigger 514/type 0 (back to boot-5's trigger shape).

- Timing: trigger 2454 → 9678 µs, canary 2845 → 9833 µs. Start delta **+391**, end delta
  **+155** — still overshooting, but 132 µs less than boot 7's +287.
- **Clean model found across boots 5/7/8:** canary start delta ≈ `shift − 160 µs`, almost
  exactly 1:1 (boot 5: shift +150 → −10; boot 7: +650 → +487; boot 8: +550 → +391). Phase
  tuning is predictable; it is the trigger's *duration* that moves between regimes.
- **Two regimes:** boot 5 ran unstretched (trigger ~1.98 ms, canary ~394 µs shorter);
  boots 7/8 ran stretched (trigger ~6.1–7.2 ms, canary ~200–236 µs shorter). The stretch
  is not explained by shift or sample count alone — possibly post-wedge console state.
- **Window condition — CORRECTED (round-33 review):** the `allPending` gate (`host.py`,
  ns2jb-faithful) needs **start delta < 0 AND end delta > 0** — the canary starts BEFORE
  the trigger and ends AFTER it. The original model below inverted both signs and aimed
  boots 8–12 at the wrong target: boot 5 (start −10 / end −404) failed only the end side,
  while boots 7–11 (start +36…+487 / end +84…+791) failed only the start side. Correct
  target: shift < ~160 µs in the sleep era, or gated `CIV_CANARY_OFFSET_NS` < ~100 µs
  (below the trigger's TAIL + overhead) with the deferred (longer) canary.
- Window condition (ORIGINAL, superseded — signs inverted): need start delta > 0 AND end delta < 0, i.e. `shift > 160` and
  `shift − 160 < duration gap`. Boot 5's regime gap ~394 µs; stretched regime gap ~236 µs.
  **Shift +200 µs satisfies both regimes** (start delta ≈ +40, end delta ≈ −196…−354).

**Next action (final sample this payload):** `CIV_CANARY_DELAY=2700000` (+200 µs). This is
the first shift value that closes the window under *both* duration regimes. Boot 5's −10 µs
start delta says +150 was one nudge short; +200 adds margin without entering the overshoot
zone. Log the result before anything else.

### Boot 9 recovery — harness reloaded manually (pre-sample log)

- Boot 9 (+200 µs, `CIV_CANARY_DELAY=2700000`) failed **before sampling**: connection
  refused on :9029 — the harness payload had exited. `relocate_host.py stop` also refused
  (relocation payload likely not loaded this boot), then `host.py load` succeeded:
  "console.js sent; harness control should be live on :9029". Ports 9026 + 9029 confirmed
  OPEN after reload.
- **Risk flag:** the boot-1 lesson says loading `console.js` directly bypasses the accessor
  refresh and faults the payload. `flow.py`'s own error text recommends exactly this
  stop→load path, so the two notes conflict. This sample doubles as a test of whether the
  documented recovery path is safe. If the wave faults, the recovery path is guilty and
  must never be used again; log that outcome either way.
- Sample budget: this is the 3rd and final sample of the current payload; console reboot
  expected afterwards regardless of outcome.

### Boot 9 — +200 µs result: the model broke (phase tuning is dead)

Result: `AP False / win False / sel False / canaryChanged False`, canary 0/type 1,
trigger 0/type 0. Trigger 2481 → 8119, canary 2517 → 8910.

- Start delta **+36** (predicted +40 — the start model held perfectly).
- End delta **+791** — massively overshot, because the canary is now **755 µs LONGER**
  than the trigger (6393 vs 5638). This is a **third duration regime**: boots 5/7/8 had the
  canary shorter by +394/+200/+236 µs; boot 9 flipped the sign entirely, at a near-zero
  start delta that resembles boot 5's unstretched regime.
- The duration gap is therefore **not a function of the shift** — it drifts boot-to-boot
  (possibly console/wedge state, heap age, or sampler ordering). No fixed
  `CIV_CANARY_DELAY` can close the window across regimes: with the canary longer than the
  trigger, end delta > 0 for every start delta > 0.
- **Recovery-path test: PASSED.** Boot 9 ran to completion after the manual stop→load
  (no fault, no wedge), so the loader's documented recovery path is safe this time; the
  boot-1 lesson's "never load console.js directly" did not reproduce. Still treat it as
  recovery-only, not a substitute for the full `flow.py` sequence.

**Decision — fall back to the pre-planned alternative:** stop tuning the phase constant.
Next experiment: derive the canary delay **in-run** from the observed trigger start
(`CIV_TRIGGER_TAIL` machinery) instead of a fixed offset, so the canary chases the
trigger's actual end each run rather than a guess across boots.
- Session hygiene: three payloads have now run this session (boots 7/8/9) without a wedge —
  but the standing rule is **one payload per session**. Reboot the console before the next
  experiment rather than pushing a fourth.

## External review digest — `Questions/fs-concurrency-timing-question.md` response

(Recorded before the next run; four takeaways that reshape the plan.)

### T1 — the 3-sample limit is worker-pool exhaustion, not a quota
The reviewer: `fsp-srv` runs a **small, fixed-size IPC worker thread pool**. Once all workers
are blocked, the service is dead system-wide until reboot. Our observed "3 samples per
payload, then wedge, reboot required" is consistent with each sample's wave leaving a worker
blocked/unreturned — samples 1–3 exhaust the pool. Implication: the budget is structural.
Never leave a request pending across a payload boundary; every unreturned request eats a
worker permanently. Also explains why restarting the tool never recovers it.

### T2 — duration regimes are dispatch + lower-layer serialization (expected, not a bug)
Request durations vary because IPC requests dispatch across that small worker pool while
journal locks and storage queues serialize at lower layers — nondeterministic **by design**.
Our boot-to-boot duration regimes (canary shorter/longer than the trigger with no pattern)
are consistent with pool scheduling drift. Confirms the decision to abandon fixed
`CIV_CANARY_DELAY` tuning; also warns that even in-run measurement will fight variance —
measure **both** the trigger span and the canary span in-run, and qualify on observed values,
not assumptions.

### T3 — concurrent sessions to the same save data are flagged as non-standard
The reviewer flagged our "multiple workers, each with its own session and storage handle
against the same save data" as implying direct IPC session/domain manipulation rather than
standard `nn::fs` behavior (exclusive mount/open locks on save containers). For the harness
this is a marker of exactly the region we're probing — but it also means results may depend
on how the harness's sessions were opened, not just on timing. Worth recording which access
path each boot used alongside timings.

### T4 — snapshot semantics explain the "zero wave strings" result
Journaling/commit semantics: a read sees pre-commit state and cannot observe another
context's mid-transaction writes unless synchronized at transaction boundaries by the caller.
Our post-wave leak-reads containing zero wave strings is consistent with reads being served
from a **pre-transaction snapshot** — the wave's data was never committed into the view our
reads use. This weakens the earlier "either we raced or the requests never read the storage"
binary; there is a third explanation (snapshot isolation) that is *expected* behavior.

## Updated next actions

1. **Console reboot first** (boot 9's payload is spent; standing one-payload-per-session rule).
2. **In-run measurement change**: derive the canary delay from the observed trigger start each
   run (`CIV_TRIGGER_TAIL` machinery), and record trigger + canary spans per sample. Qualify
   on observed end-delta, never on assumed durations.
3. **Add `# samples-per-payload` bookkeeping to the harness log** so future boots show how
   close to the worker-pool limit a run got (T1) — catches near-exhaustion before the wedge.
4. **Keep phase-constant experiments dead** unless a boot reproduces the unstretched regime
   three times consecutively.

## External review digest #2 — read cancellation question (answered: no)

Framed question: "is there a supported way to cancel or abort an in-flight read so the
service worker returns to the pool?" Answer: **no**.

1. `IStorage::Read` is a synchronous HIPC command — once the server worker dequeues it
   (`svcReplyAndReceive`), that worker runs the handler **to completion**. No async read or
   abort/cancel command exists on `nn::fs` / `IStorage`.
2. Client-side cancellation (`svcCancelSynchronization`), session close, or process
   termination does **not** preempt the server worker: a healthy worker finishes the read
   then notices the closed session on reply; a blocked/deadlocked worker stays wedged until
   reboot.
3. Design implication stated by the reviewer: because blocked workers cannot be reclaimed
   from userland, the caller must **prevent** the blocking condition — serialize access over
   a single session/handle rather than racing independent sessions.

### Consequences for the plan

- The 3-sample limit is now fully explained (worker pool exhaustion via unreclaimable
  blocked workers) **and** confirmed unfixable from our side. Budget stays hard.
- **The harness's CancelSynchronization / worker-cancel machinery cannot reclaim server
  workers either** — it only unblocks our own waiting threads. Any wave design that leaves a
  server worker blocked is burning pool capacity permanently, per sample.
- The reviewer's "serialize over a single session" recommendation is the standard-conforming
  shape; the harness's multi-session approach is the non-standard probe shape. Both facts
  were already flagged in digest T3 — this confirms them from a second angle.
- Sample bookkeeping becomes even more important: treat every sample as a permanent
  deduction from a fixed-size pool, and stop a payload early if a sample shows any sign of
  leaving workers blocked (e.g. an unexpectedly long or never-returning read).

## Updated next actions (unchanged from digest #1, plus one)

1. Console reboot first (payload spent).
2. In-run measurement change with observed-span qualification.
3. Per-boot logging of samples used + session access path.
4. Phase-constant experiments stay dead unless the unstretched regime reproduces 3×.
5. **New:** when drafting the in-run change, add a per-sample "workers blocked" heuristic —
   if a sample's read behaves like a blocked worker (long stall, no reply), abort the payload
   immediately and log it, rather than spending the remaining samples against a shrinking pool.

### Caveat (2025-11-04): review responses are generalized models, not SDK ground truth

The Q&A reviewer's answers (worker pool size, synchronous read semantics, journal commit
rules, exclusive save mounts) may describe generic service IPC design rather than the
Switch 2 SDK specifically. Treat T1–T4 as **testable hypotheses**, not verified facts:
- T1 (fixed worker pool) predicts sample-budget behavior — test by logging per-sample
  success/stall patterns across payloads.
- T2 (dispatch nondeterminism) predicts duration-regime drift — test by whether observed
  spans qualify better than constants.
- T4 (snapshot semantics) predicts zero wave strings even in "clean" races — test by
  whether committed/flushed control samples ever show wave strings.
If a boot contradicts a prediction, downgrade that digest item and note it here.
