---
tags: [civ7, switch2, homebrew, fs, canary, timing]
status: active
created: 2026-11-04
---

# Gated canary plan

Action plan written **before** implementation (crash-safe ordering, per the §session-lessons
rule). Related: [[selected-marker-plan]] (phase-tuning history), [[wave-harness]],
[[probe-plan]], Q&A digest in [[selected-marker-plan]] (worker-pool model, generalized —
treat as hypotheses).

## Goal

Replace the canary's fixed absolute delay (`CIV_CANARY_DELAY`) with a **signal-gated start**
so its alignment to the trigger is structural. Phase tuning is dead: boot 5/7/8/9 showed the
canary *start* tracks the delay 1:1 but the canary-vs-trigger *duration* flips regime
boot-to-boot (canary −394 µs, −200, −236, then **+755 µs longer**), so no constant closes
the window. A gate removes start-alignment from the equation; the residual end-delta becomes
a pure duration race we can measure per-run.

## Design

New gate `canaryGate` on the same wave control page (must be 8-byte spaced like the others;
`closed` is at +0x40, so the new gate goes at **+0x48**).

1. **guard1 (role 11)** — currently ends with `signal(gate["guard"])`, which is exactly what
   releases the trigger's `wait(gate["guard"])`. When gated mode is on, guard1 signals
   `canaryGate` **after** `guard` (one extra event signal, ~µs). This is a broadcast-safe
   design: two roles never wait on the same gate, so a single-wake signal cannot steal.
2. **canary (role 6)** — when `CIV_CANARY_GATED=1`, replace `sleep_ns(delays[6])` with
   `wait(gate["canaryGate"])` followed by `sleep_ns(CIV_CANARY_OFFSET_NS)` (default 150000).
   Wait on `master` first, as every role does.
3. **Report** — add `canaryGate` to the `gates` dict in the wave report so the boot log shows
   it was actually wired.

When `CIV_CANARY_GATED` is unset, behavior is byte-identical to today (default-off env flag).

## Expected geometry

Trigger start ≈ guard-signal + TAIL (100 µs) + copies/guards (~20–50 µs). Canary start ≈
guard-signal + offset. So **start delta ≈ offset − 120–150 µs**; offset 150 µs gives ≈ +0 to
+50 µs, and larger offsets shift the canary later linearly. The end delta is then
`canary_dur − trigger_dur` plus that offset — still regime-dependent, but now measurable with
a *known* start alignment, and the offset knob moves it in a predictable direction.

## Steps

1. [x] Confirm gate layout and role plumbing in `host.py` (roles list line ~1644, gate alloc
   line ~1650, steps built line ~1834-1930).
2. [x] Edit `host.py`: add `canaryGate` (+0x48), env reads (`CIV_CANARY_GATED`,
   `CIV_CANARY_OFFSET_NS`), guard1 signal, canary steps, report gate entry.
   Done + live-verified (boot 10's report carried `canaryGate`; code confirmed at the
   gate dict + role-6/11 branches).
3. [ ] Run the offline selftest (`wire-selftest.py` + `wire-diff.py`) — wave frames should be
   unchanged in default mode.
4. [ ] Boot: `flow.py worker-wave extent-present 1` with `CIV_CANARY_GATED=1`,
   `CIV_CANARY_OFFSET_NS=150000`, all other knobs as in the boot-9 baseline.
5. [ ] Record observed trigger/canary spans; decide next offset from the measured start delta
   (formula above), not from assumptions.

## Success criteria

- Start delta (canary start − trigger start) is **positive and near the predicted value**
  (±30 µs) in the gated run — this is the regression test that the gate works.
- `allPending` / window qualification is a bonus; a measured span set is the deliverable.

## Safety rails (from the Q&A digest)

- One payload per console session; 3 samples max per payload; workers blocked by a stalled
  request are unreclaimable — if a sample stalls abnormally, stop the payload (do not spend
  remaining samples against a shrinking pool).
- Reviewer answers are generalized models, not SDK ground truth — log contradictions and
  downgrade digest items when a boot contradicts them.

## Follow-ups (not this boot)

- Per-payload bookkeeping: samples used, session access path per boot (digest T3).
- Worker-stall abort heuristic in the harness/report.
- The final payload (user's stated end goal): the gated canary + measured geometry feed the
  retained-memory chain ([[retained-memory]] §portability) — gate the trigger–canary window
  in the payload rather than tuning constants at runtime.

## Boot 10 — gated canary, offset 150 µs (first gated run)

`worker-wave-extent-present.json`, 1 of 3 samples. `AP False / win False / sel False /
canaryChanged False`, canary 0/type 1, trigger 0/type 0, guard1 514/type 0.

- Gate wiring confirmed: report shows `canaryGate: 170261381192` = `closed (+0x40) + 8`
  — the +0x48 slot is live.
- Timing: trigger 2486 → 9897 (span 7411 µs), canary 2541 → 10606 (span 8065 µs).
- **Start delta +55 µs — gate regression test PASSED.** Predicted +0 to +50 µs from
  offset 150 (offset − TAIL − copy overhead); observed 55, just outside the ±30 µs band
  but structurally positive and reproducible-by-construction. No boot-9-style drift:
  start alignment is no longer phase-luck.
- **End delta +709 µs — stretched regime again.** Canary is 654 µs longer than the
  trigger (8065 vs 7411). With a duration deficit of −654 µs, end delta < 0 is
  unreachable at any positive start delta: the canary cannot both start after the
  trigger and end before it while being longer. Same trap as boot 9, but now with a
  *known* start, so the residual is purely the duration race.
- Stretched regime has now appeared in boots 7/8/10 with spans ~5.6–7.4 ms; the
  unstretched regime (boot 5, trigger ~2.0 ms) has not reproduced since. Regime may be
  tied to console/heap state at boot rather than wave knobs.

### Decision: the offset knob is now confirmed linear and safe — but it cannot fix a
duration deficit. Two levers remain:
1. **Offset sweep (cheap, sample-hungry):** confirms linearity but cannot produce a
   window while canary_dur > trigger_dur.
2. **Canary duration lever (the real one):** make the canary shorter — `CIV_CANARY_SIZE`
   (SM2's size sweep: 0x5a0 → 0x400) shortens the path work; or lengthen the trigger
   (`CIV_TRIGGER_SLEEP`/depth) to widen its span. With start gating fixed, a size sweep
   becomes interpretable: each size's end delta is now measured against a known start.

### Next sample (sample 2 of 3, this payload)
`CIV_CANARY_SIZE=400` (0x400) with the same gated offset 150 µs. Hypothesis: shorter
canary path shrinks its span by more than 654 µs, flipping end delta negative and
closing the window. If it does not flip, the duration race is not size-driven and
sample 3 goes to the trigger-length lever instead.

### Boot 11 — gated canary, offset 150 µs, size 0x400 (sample 2 of 3)

`AP False / win False / sel False`, canary 0/type 1, trigger 514/type 0.

- Timing: trigger 2454 → 9836 (span 7382), canary 2506 → 9920 (span 7414).
- Start delta **+52** — the gate held (predicted ~+30–50; consistent with boot 10's +55).
- End delta **+84** — down from +709 in boot 10. The size cut (0x5a0 → 0x400) shortened
  the canary span by ~651 µs, almost exactly the previous deficit. Trigger span was
  already ~7.4 ms in boot 10's stretched regime, unchanged here.
- **The window is nearly closed by size alone:** end delta +84 with start delta +52. The
  constraint is now arithmetic: end delta = start delta + (canary_span − trigger_span);
  canary_span − trigger_span = +32. Reducing the offset further would break the positive
  start delta (offset 150 − 52 ≈ 98 µs would zero it), so offset is spent.
- **Remaining lever: extend the trigger's end.** `CIV_TRIGGER_TAIL` 100000 → 200000 ns
  adds ~100 µs to the trigger's span without touching start gating; predicted end delta
  ≈ +32 − 100 = **−68**, start delta unchanged at +52. That closes the window under the
  stretched regime.

### Boot 12 — offset 150 µs, size 400 (decimal!), TAIL 200 µs (sample 3 of 3)

`AP False / win False / sel False / canaryChanged False`, canary 0/type 1, trigger 514/type 0.

- Timing: trigger 2573 → 9929 (span 7356), canary 2525 → 9651 (span 7126).
- Start delta **−48 — NEGATIVE, and the model now explains it.** TAIL sits *after*
  `wait(gate["guard"])` in the trigger's step list (host.py ~1885), so raising TAIL
  100→200 µs shifted the trigger's whole send window later (start 2454→2573, +119;
  end 9836→9929, +93; span 7382→7356 ≈ unchanged). **TAIL is a phase knob for the
  trigger, not a span lever** — the "adds ~100 µs to the span without touching start
  gating" hypothesis is withdrawn.
- **Corrected model, validated 3/3:** start delta = offset − TAIL + ~3 µs. Predicted
  +55/+52/−50 vs observed +55/+52/−48 across boots 10/11/12. The gate geometry is
  deterministic and linear; the regression test passed every boot.
- End delta −278 = start (−48) + span diff (−230). The canary's entire span sat inside
  the trigger's request window, yet canaryChanged False — expected, since per
  leak-cycle.py's model canaryChanged belongs to the *positive-wave stage* (the `/\.`
  underflow piece), a separate stage-gated chain. This wave's criterion is geometric.
- Span diff at size 400 across boots: **+32** (boot 11, shared payload) vs **−230**
  (boot 12, fresh payload) — 262 µs of spread, larger than the +84 margin boot 11
  needed. Payload freshness may correlate (boots 10/11 shared one payload; boot 12
  was fresh). Label correction: the plan's earlier "0x400" was wrong — `int("400", 0)`
  is decimal 400; the real cut was 1440 → 400 bytes (~654 µs, ~0.63 µs/byte).

**Samples exhausted: boots 10/11/12 = 3/3. Fresh payload required.**

## Boot 13 plan (written BEFORE execution, per the §session-lessons rule)

Fresh payload. Two goals: restore the validated +52 start geometry, and collect a
third diff point at size 400 to test the payload-freshness correlation.

1. Recover console: `relocate_host.py 192.168.103.215 stop` then `host.py ... load`
   (fresh payload — samples reset).
2. Knobs: `CIV_TRIGGER_TAIL=100000` (restore), `CIV_CANARY_OFFSET_NS=150000`,
   `CIV_CANARY_SIZE=400`, all else as the boot-12 baseline (gated, hole_early,
   release_early, pre_clone, pin_early, CIV_ALLOW_OVERWRITE=1, CIV_NORMALIZED_PATHS=1).
3. Predictions: start delta ≈ **+52** (model, validated); end delta = 52 + diff with
   diff in the observed −230..+32 band — unknown sign, that IS the measurement.
4. Record the diff; decide sample 2 from the measured value, not assumptions.
5. Log: `harness/runs/gated-canary-boot13-fresh.log`.

### Boot 13 — fresh payload, TAIL restored, size 400, offset 150 µs (sample 1 of 3)

`AP False / win False / sel False / canaryChanged False`, canary 0/type 1, trigger
0/type 0.

- Timing: trigger 2475 → 8767 (span **6292**), canary 2530 → 9334 (span **6804**).
- Start delta **+55** (model predicted +52) — **gate regression test 4/4.** The gate is
  structurally sound; start alignment is fully deterministic.
- End delta **+567** — span diff +512. A **new regime**: both spans ~1 ms shorter than
  the stretched 7.3–7.4 ms regime of boots 10–12, and the diff flipped from −230 (boot
  12, fresh payload) to +512 (boot 13, fresh payload). **The payload-freshness
  correlation is dead** — same size, same knobs, fresh payloads both boots, 742 µs
  apart in diff.
- **Conclusion of the geometric campaign:** the duration race (canary span − trigger
  span) swings +654 / +32 / −230 / +512 across four boots. It is regime-dominated
  noise, not a client-tunable quantity. No fixed offset/TAIL/size config closes the
  window across regimes — the same conclusion that motivated the gate, now confirmed
  for the *duration* axis too. The gate fixed the start axis; the duration axis needs
  a different approach (see sample-2 decision).

## Sample 2 decision (written before execution)

The regime question is now the only one that matters: **is the span regime per-payload
(consistent within a payload) or per-wave (varies between waves in one payload)?**

- Round-28's leak-cycle run already hinted within-payload lottery (wave 1 all-span,
  wave 2 AP=False), but that was the leak-cycle wave, not worker-wave.
- Experiment: **one boot, 3 waves** (`flow.py ... worker-wave extent-present 3`) on
  the current payload with identical knobs. This spends the payload's full 3-sample
  budget in one boot and yields 3 span pairs.
- If the 3 waves agree within ~50 µs: the regime is per-payload (heap/console state at
  load) — the fix belongs in payload preparation, not wave knobs.
- If they scatter by hundreds of µs: the regime is per-wave randomness and geometric
  window closure from the host is structurally impossible — the plan's follow-up
  ("gate the trigger–canary window in the payload") becomes the only viable route.
- Log: `harness/runs/gated-canary-boot14-3waves.log`.

### Boot 14 status — CONSOLE WEDGED, no data collected

The 3-wave boot never reached the wave: `relocate_host.py` timed out mid-receive
(`_recv_exact` on the relocation channel), then the auto-reload sent console.js but
:9029 still refused. Recovery attempt per the printed procedure:

- stop: **ConnectionResetError** (remote forcibly closed — console not answering the
  relocation channel at all)
- load: console.js sent, "harness control should be live on :9029"
- port probe: **REFUSED** on :9029

State: the §43 "open-but-silent" wedge, third occurrence this campaign. The boot's
payload/sample budget must be treated as burned — after a console relaunch, reload a
fresh payload and re-run boot 14 (3 waves) from scratch. **User action required**:
relaunch the game/payload on the console; nothing on the host side can recover this.

After relaunch checklist:
1. Port probe 192.168.103.215:9029 → expect OPEN.
2. `relocate_host.py stop` → expect clean exit (no reset error).
3. `host.py load` → fresh payload, samples reset.
4. Boot 14: `flow.py ... worker-wave extent-present 3` with the boot-13 knob set,
   logging to `gated-canary-boot14-3waves.log`.
### ★ CORRECTION (external review, 2026-11-05): boots 10–12 have the window signs inverted

The `allPending` gate (`host.py`, ns2jb-faithful) requires for the canary:
`canary.startUs < trigger.startUs` **AND** `trigger.endUs < canary.endUs` — start delta
**negative**, end delta **positive**. Re-read against the gate:

| boot | start delta | end delta | failing side |
| --- | --- | --- | --- |
| 10 | +55 ✗ | +709 ✓ | start only |
| 11 | +52 ✗ | +84 ✓ | start only |
| (5) | −10 ✓ | −404 ✗ | end only |

The decision above ("cannot fix a duration deficit", "make the canary shorter") and the
boot-12 prediction ("end delta ≈ −68 … that closes the window") both target a **negative
end delta — which is the failure condition**. The boot-11 size cut attacked the one side
that was already passing (end margin +654 → +32). The origin is the boot-8 shift model
in [[selected-marker-plan]] ("need start delta > 0 AND end delta < 0") — corrected there.

The arithmetic (boot-10/11 anchors): **start delta ≈ `CIV_CANARY_OFFSET_NS` −
`CIV_TRIGGER_TAIL`** (the two roles' wake/copy overheads measured ≈ equal: 150 − 100 →
+50, observed +55/+52). End delta = start delta + (canary_span − trigger_span); the
deferred-canary regime measures **+654** (size 0x5a0, boots 9–10) vs **+32** (size
0x400, boot 11).

**Corrected boot 12 (final sample of this payload):** keep `CIV_TRIGGER_TAIL=200000`
(trigger starts ≈ +100 µs later → start delta ≈ −48 ✓, and every other protected role
gains the same start margin), **revert `CIV_CANARY_SIZE` to 0x5a0** (restores the +654
end margin), keep offset 150000. Predicted: start ≈ −48, end ≈ −48 + 654 ≈ **+606** →
the canary spans → the first real `allPending` shot with the normalized (resolved,
0/type 1) canary. Equivalent alternative without touching TAIL:
`CIV_CANARY_OFFSET_NS=40000` with size 0x5a0 (start ≈ −55, end ≈ +599).

- If a boot lands in a non-deferred-canary regime (canary_span < trigger_span — boots
  5–8 class), **no offset closes the window** (end ≤ dur_gap < 0 for every start < 0);
  that sample is a lottery loss. Record `canary_span − trigger_span` per sample and
  interpret offset/size changes only on deferred-canary samples.
- The gate itself is validated and worth keeping: start placement is structural
  (boots 10/11 within prediction), which is exactly what it was built for.### Boot 13 attempt (2026-11-05, ~13:00) — LOADER WEDGE before any sample; corrected config queued

Ran the corrected window test via `flow.py` (full sequence) with the round-24 frozen
config + normalized canary + gate:

```
CIV_NORMALIZED_PATHS=1 CIV_CANARY_GATED=1 CIV_CANARY_OFFSET_NS=40000 CIV_CANARY_SIZE=0x5a0
CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 CIV_TRIGGER_TAIL=100000
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0
CIV_FILLER_DELAYS=2480000,2300000 CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000
  python flow.py 192.168.103.215 worker-wave extent-present 3
```

**No sample ran.** The flow itself failed at the payload boundary:

1. `relocate load` → evaluated, `:9028` up; `mounts` → **worked** (fresh accessor
   `0x570026e2a0` @ 12:57:47 — valid only for that game session).
2. `relocate stop` → **ConnectionReset mid-send** (vs the clean `{"ok": true}` in the
   12:47 retry log) — the relocation payload wedged between serving `mounts` and the
   stop, and `:9028` kept accepting connects while resetting on data.
3. `host.py load` → "console.js sent" but `:9029` **refused** (nothing listening).
4. Retried the load on a **fresh port 9210** (the "never reuse a port" rule) → same
   refusal. **It is the loader wedge, not port reuse**: the loader accepts the bytes but
   refuses to evaluate further payloads this game session (5+ evaluations since 12:45).

**New operational facts (refining §hygiene):**

- **A dead payload's port is a black hole, not a closed port**: connects to `:9029`
  **time out** (SYN dropped by the backlog of a dead-but-bound socket) rather than being
  refused — so "port probe" alone cannot distinguish dead-payload from frozen-console.
  The loader (`:9026`) answering loads is the discriminator: loader answers → wedge
  (game relaunch clears); loader silent → freeze (power-cycle required).
- The `relocate stop` reset means the stop did NOT unload the payload — the
  "unload the relocation payload, then load this one" hint in host.py's load error is
  unreachable when the relocation payload is wedged; only a relaunch clears it.
- Budget lesson: this game session had already run two full flows (12:45, 12:47) with
    4+ payload evaluations; the third flow hit the wedge. **Treat one full flow
  (relocation + console.js + waves) per game session as the safe payload budget**, and
  relaunch before each new sweep — cheaper than diagnosing mid-sweep.

**Queued for the fresh game session (unchanged corrected config, default port):** the
command block above via `flow.py` (the flow rescans `mounts` for the new accessor —
never load `console.js` directly). Single variable vs the round-24 deterministic
allPending config: the canary form (deep 0x202 → normalized resolved 0/type 1, gated,
offset 40 µs → start ≈ −55). Prediction: if the regime lands unstretched (trigger
fast/mid as in round-24 3/3), the canary's ~1574 µs span clears a ~540–2400 µs trigger
window and **allPending=True lands with the canary RESOLVED — the first such sample —
directly unblocking Stage A** (`worker-wave positive 1` on the NEXT fresh payload).
If the regime lands stretched (trigger ~7.4 ms as in boots 10–12), expect guards/fillers
end-side misses ~+200–960 µs and treat the sample as regime data: the gate arithmetic
(held) plus the regime map is the deliverable, same as boots 10–12.### ★★ Boot 14 (2026-11-05, 13:01–13:02) — CORRECTED CONFIG: allPending + windowOk TRUE with a RESOLVED canary

Ran on the fresh (relaunched) game session, one full flow, 3 × extent-present, the
round-33-corrected config: `CIV_NORMALIZED_PATHS=1`, `CIV_CANARY_GATED=1`,
`CIV_CANARY_OFFSET_NS=40000`, `CIV_CANARY_SIZE=0x5a0`, `CIV_RELEASE_DELAY=1500000`
(the round-24 frozen value, not the 900000 the boots-10–12 chase inherited),
`CIV_TRIGGER_TAIL=100000`, harvest delays.

**Attempt 2 is the campaign's key sample — preserved as
`runs/MILESTONE-allSpan-windowOk-resolved-canary-r34a2.json`:**

```
trigger 2479.9 -> 3655.1 (1175 us, mid class)   allPending=True  windowOk=True
canary  2420.3 -> 4457.3 (2037 us)   startD -60   endD +802   result 0 / type 1
commit 0->4477  tails 2.5-2.7 ms  fillers 1.7-2.0 ms  guards 1.5-1.8 ms — ALL SPAN
heldFreed 5344 >= 5000  changedPaths=[]  canaryChanged=False  timingValid=True
```

**This is the first all-span sample where the canary is RESOLVED (0/type 1, a real
file holding a real heap slot) instead of the deep-form 0x202 missing path.** The
round-33 sign model is confirmed live in all three attempts — the gate delivered
start deltas −59/−60/−54 (predicted ≈ −55) across three different trigger regimes:

| attempt | trigger class | canary startD / endD | other misses |
| --- | --- | --- | --- |
| 1 | deferred (6910 µs) | −59 / **+639 ✓** | guard1 only (+354) |
| 2 | mid (1175 µs) | −60 / **+802 ✓** | **NONE — 10/10 span** |
| 3 | mid (2046 µs) | −54 / **+125 ✓** | fillers/guards (end-side +330…+1000) |

The canary spans **in every attempt** — the gated start (offset 40 µs < TAIL+overhead)
plus the resolved walk's ~2 ms duration cover every trigger class seen (the boot-13
"stretched regime is unwinnable" worry did not materialize at RELEASE_DELAY=1500000;
the stretch itself appears tied to the 900000 release delay). The residual lottery is
fillers/guards end-side only (a3), the same shape round-14/15 harvested through.

**Stage A consequence (SM0's queue, now live):** the positive wave's canary is
resolved in this staging — the slot the `/\..\` trigger must free is a real file. The
queued next test is `worker-wave positive 1` **FIRST on a fresh payload** (this
payload has spent its budget: 1 setup + 3 waves = 4 reserve cycles; §45's lesson is
the 4th+ wedge is open-but-silent = power cycle). Same normalized env, via `flow.py`
(fresh accessor per boot). Watch: `canaryChanged`, the trigger's result/type at the
resolved-canary geometry, `selectedMarker`. If the positive trigger still aborts
`0x2EEA02` with `nativeGuard=0`, that is SM5's form-block verdict, independent of
window quality.

**Boot 13 addendum (this session, pre-14):** the boot-13 attempt never sampled —
`relocate stop` reset mid-send (wedge), console.js refused on :9029 AND a fresh
:9210. Operational refinements recorded there: a dead payload's port is a **black
hole** (connects time out, not refused), and the loader answering loads
(`:9026`) discriminates wedge (relaunch clears) from freeze (power cycle). One full
flow per game session is the safe payload budget.### ★★★ Boot 15 (2026-11-05, 13:10 + 13:13) — Stage A's form question is ANSWERED: 0x2EEC02 accepted, ALIAS LIVE

Fresh payload after a relaunch; ran the positive wave (`CIV_ALLOW_OVERWRITE=1`) on the
boot-14 staging (normalized resolved canary, gate offset 40 µs, RELEASE_DELAY=1500000).

**Wave 1 (13:10, preserved `MILESTONE-positive-wave-ns2jb-faithful-2eec02-r35a1.json`):**

- `allPending=True` + `windowOk=True` **on the positive profile** — first time the
  destructive wave itself qualified the window (fast 259 µs trigger).
- **The trigger returned `0x2EEC02`** — the ns2jb-REQUIRED result for every non-extent
  wave (`probe-reserved-gap.cjs:488`: `check(trigger.result === 0x2eec02, 'Backslash
  control result differs')`). Every row faithful: guards 0x202, fillers 0x202, canary
  0/type 1 (resolved), nativeGuard 0/0/0, tails 0x??0002 (tick residue).
- **The round-32 `0x2EEA02` abort did NOT reproduce.** Per the reference's own encoding
  model (`:856-858`): `0x2EEA02` = normalizer REJECTED the `/\..\` encoded form,
  `0x2EEC02` = normalizer ACCEPTED it. The boot-14 staging (resolved canary holding a
  real heap slot vs the old 0x202 missing-path deep form) flipped the form from
  rejected to accepted. **Round-32's form-block is superseded; SM5's sequential form
  probe is answered by stronger evidence — do not spend a boot on it.**
- `changedPaths=[]`, `canaryChanged=False` — the alias did not land this sample.

**Wave 2 (13:13, preserved `MILESTONE-positive-alias-lands-guard1-r35b1.json`, same
payload's second invocation):**

- Trigger `0x2EEC02` **again** (2/2 — the acceptance is stable at this staging).
- **`changedPaths=['guard1']` — an aliased copy LANDED on guard1's held path**: its
  result came back `0xFCEEEA02` (the 0x2EEA02 accepted-encoding class with tick
  residue in the high byte, the same tick-residue pattern the tails' 0x??0002 rows
  show) instead of the expected `0x202`. This is exactly ns2jb's
  `metadataCanaryMiss` shape (`:480-483`: "a metadata copy can land on the still-owned
  path canary — let the caller retry this measured miss") — one held-path slot off
  the target.
- `canaryChanged=False`, canary intact (0/type 1), `changedPaths` has ONLY guard1.
- Window lost this sample (allPending=False — the trigger ran 5.7 ms deferred-class;
  the canary still spanned: startD −55 / endD +618).

**The attempt-2 setup then froze the console** (`open fsp-srv` timed out open-but-silent
on the second invocation's reserve — the documented freeze; this payload had spent
setup+wave ×2). Power cycle required, relaunch alone will not clear it.

### Campaign state after boot 15

**The `/\..\` fold aliases into the held-path region on Civ VII — the alias mechanism is
real, live, and instrumented.** Stage A (`canaryChanged`) is now a **landing-slot
lottery**: 13:10 landed nowhere, 13:13 landed on guard1 (one held-path slot off the
canary). The two per-sample variables are where the alias lands (heap-slot geometry) and
the window (trigger class). Both were held at least once.

### Queued for the next boot (after power cycle)

1. **Positive-wave lottery harvest** (primary): fresh flow, `worker-wave positive 3`
   (one setup, three reserve cycles — the boot-14-proven budget). Watch per sample:
   `changedPaths` (where the alias lands), the trigger's result (expect 0x2EEC02),
   and the window. Two landing samples are enough to see whether guard1 is a stable
   landing or one draw of a wider lottery.
2. **If the alias keeps landing on guard1** (stable slot): the slot-swap machinery
   (`CIV_CANARY_LAST=0` restores ns2jb's order, or `CIV_SLOT_SWAP` on the canary/guard
   pair) puts the canary INTO the slot the alias prefers — Stage A becomes structural.
   Verify once against `abort-control` semantics first (per §13.4's caveat).
3. If landings scatter (no stable slot): the per-sample landing is heap-geometry noise —
   SM3's alias-offset analysis (address-planner on the 12160-byte reservation) becomes
   the live dig, now with a directly observable landing signal to fit against.### Boot 16 (2026-11-05, 13:20) — windowOk on positive ×2; retry-loop port bug found and fixed

Fresh flow after the power cycle, `worker-wave positive 3` on the boot-14 staging.

**Wave 1 (13:20): `0x2EEC02` acceptance ×3, `windowOk=True` on positive ×2, no landing.**
Trigger 197 µs (fast class), `allPending=True`, `windowOk=True`, trigger `0x2EEC02`
(3rd consecutive accepted-form sample: 13:10, 13:13, 13:20), canary resolved 0/type 1,
`changedPaths=[]` — the alias landed nowhere this draw. **Landing tally: 1/3 draws,
guard1 once** (13:13).

**Port bug found (this boot's harvest ran ONE attempt of 3):** the retry loop broke on
`windowOk` alone for every profile except extent-present (`host.py:1612`) — but ns2jb's
`retryWave` for the overwrite profiles keeps fishing until **`canaryChanged`**
(`probe-reserved-gap.cjs:532-536`, the Stage A acceptance: changedPaths includes
'canary' AND allPending AND margin ≥ 300). Boot 16's attempt 1 hit `windowOk=True`
after one draw and stopped; the same shape stopped round-23's extent-present boot
(selectedMarker open). **Fixed (`host.py`, ns2jb-faithful):**

```python
if profile == "extent-present":
    done = bool(last.get("windowOk") and last.get("selectedMarker"))
elif profile in self.OVERWRITE_PROFILES:
    done = bool(last.get("windowOk") and last.get("canaryChanged"))
else:
    done = bool(last.get("windowOk"))
```

Compile-verified. **Consequence: `worker-wave positive 3` on a fresh flow now fishes
all three draws (one setup + three reserve cycles — boot-14's exact proven budget) and
stops early only on Stage A success (`windowOk && canaryChanged`), which IS the win.**

Budget note: this payload has spent setup + 1 wave (reserve #1 released by the wave's
own release role). A further invocation on this payload would burn attempt 0 on the
released group (stale — §28b: a retry needs a fresh reservation) and pushes reserve #2/#3
into the zone where boot 15 froze (its #2 open timed out). Stopped here with the console
healthy — **relaunch the game** (no power cycle needed) and run the fixed `positive 3`
fresh.### ★★★★ Boot 17 (2026-11-05, 13:24–13:25) — STAGE A QUALIFIED: canaryChanged + markerSelected on a windowed positive wave

Fresh flow after relaunch, `worker-wave positive 3` with the **fixed retry loop**
(host.py:1609–1623, landed this session: the loop now breaks per-profile — positive
fishes until `canaryChanged`, not `windowOk`-alone).

- **Wave 1 (13:24:43):** `0x2EEC02` (4th consecutive accepted-form sample), fast 169 µs
  trigger, `allPending=True`, `windowOk=True`, `changedPaths=[]` — no landing. **The
  fixed loop did NOT break here** (canaryChanged still open) — the bug's own fix proven
  live on its first run.
- **Wave 2 (13:25:05) — ★ STAGE A LANDED. Preserved
  `MILESTONE-stageA-canary-changed-positive-qualified-r37a2.json`:**

```
trigger 2477.5 -> 3351.7 (874 us)   result 0x2EEC02
canary  2418.9 -> 4855.5            result 0xE5AEEA02  canaryType=0  (path OVERWRITTEN)
allPending=True  windowOk=True  timingValid=True  changedPaths=['canary']  only
nativeGuard 0/0/0  heldFreed 5344  restored=True  placementUncertain=False
```

**Full ns2jb positive acceptance verified against `probe-reserved-gap.cjs:532-536`:**

| gate | value | need | verdict |
| --- | --- | --- | --- |
| `changedPaths` | `['canary']` | includes 'canary', only canary | ✓ |
| `allPending` | True | true | ✓ |
| `receiveOrderMarginUs` | 36,655 | ≥ 300 | ✓ |
| `canaryType` | 0 | 0 (the file no longer resolves) | ✓ |
| `windowOk` (hasWindow) | True | true | ✓ |

**`canaryChanged = TRUE` and `markerSelected = TRUE` — the complete positive-wave
qualification.** The aliased copy landed on the canary's held path inside a windowed
wave: the canary slot is **freed in fsp-srv's heap**, ready for Stage B's candidate
prime (the 0x5a0 cache bucket table allocated INSIDE the freed slot, §43's chain).

**The retry-loop fix is the direct enabler:** wave 1 had `windowOk=True` with no
landing — the old break condition (`:1612`, windowOk-alone) would have stopped the
harvest after ONE draw, exactly as it stopped boot 16 and round 23. Wave 2 is the draw
that landed. **Todo #4's positive half (tracker §3) is DONE** — the destructive-stage
qualification the campaign has been building toward since round 16.

Landing tally across all positive draws this session: 13:10 empty, 13:13 guard1,
13:20 empty, 13:24 empty, **13:25 canary** → canary-landing 1/5, guard1-landing 1/5,
nowhere 3/5. The landing slot IS a lottery, but the fixed loop harvests it at
3 draws/payload.

### Queued: the A–D chain, fresh payload (`leak-cycle.py full`)

Stage A is reproducible (fixed loop, ~1-in-3 draws per payload at the observed rate) —
but **one `leak-cycle.py` run per payload** (§45: a second setup+wave on a live payload
wedges the console). The current payload has spent setup + 2 waves. **Relaunch, then:**

```
python leak-cycle.py full
  (CIV_ALLOW_OVERWRITE=1; CIV_ALIAS_UPTO=read; its own setup + positive lottery
   [fixed worker_wave] → B candidate prime → C installCombinedLeak → D over-read)
```

Watch: Stage A re-landing (canaryChanged), then B's prime `result 0`, C's copyField
writes (`confirmed == 7`), D's over-read artifacts (`runs/leak-*.bin` > 0x4000 →
heap bytes past 0x4000 + the changedLink scan). If Stage A misses all 3 draws on a
payload, relaunch and re-run — the chain is scaffolded, the lottery is the only
variable left.### Boot 18 (2026-11-05, 13:29–13:31) — leak-cycle Stage A validated in-chain; 2 lottery draws, 0 landings

First `leak-cycle.py full` run (`CIV_PREREQ_SKIP=1` — the prereq sweep would have burned
1–3 extent waves re-validating what r37 already qualified; `CIV_RELEASE_CLOSE_GATE=` empty
to beat the script's `setdefault` — the §44 geometry breaker stays off; `CIV_ALIAS_UPTO=positive`,
`CIV_ALIAS_MARKERS=0x2340,0x2340` = 2 single-draw waves).

- **Stage 0 ran clean**: romfs control reads 4 blocks, `strings=[] headCandidates=[]` (clean
  RomFS — the §43 regression check holds), storageSize captured for the chain.
- **The chain's Stage A integration works**: its positive wave (own rereserve + `worker_wave`
  per marker) rode the r37 staging and produced two **fully windowed draws**
  (`AP True / win True / trigger form accepted` ×2) — but `canaryChanged=False` both.
  Exit 1 is the script's own "positive wave did not free the canary" raise — clean, no wedge.
- **Landing tally (all positive draws this campaign): canary 1 (r37a2), guard1 1 (r35b1),
  nowhere 7.** Per-draw landing rate ≈ 1/9 → 2 draws/payload ≈ 22%.

### Strategy update (accepted): chain A→B in one payload

`upto=positive` wastes the payload when the draw lands early: if Stage A lands on draw 1,
the payload has spent setup + 1 wave (2 cycles) and Stage B (`rereserve` + candidate open +
prime reads — no waves) fits comfortably (cycle 3). **Next boot runs `CIV_ALIAS_UPTO=prime`
with `CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340` + `CIV_ALIAS_MARKERS_MAX=3`** (3 draws ≈ 33%/payload,
stops early on landing, then B validates immediately): watch the prime assert
(`prime.bytes == control prefix` — the cache bucket table really is inside the freed canary slot)
and the alias-control discriminator. Stage C (7 copyField waves = 7+ reserve cycles) still
cannot fit one payload — its strategy is a separate decision after B lands.### Boot 19 (2026-11-05, 13:33) — CRASH mid-destructive-wave-1: the lottery can land on payload memory

`leak-cycle.py full` (`CIV_ALIAS_UPTO=prime`, 3 markers, `CIV_PREREQ_SKIP=1`, empty
`CIV_RELEASE_CLOSE_GATE`, r37 staging) on a fresh flow:

- Stage 0 clean (4 control reads, no strings/candidates), reserve clean
  (`allocated 12048, outer 266`), all 13 frames launched.
- **Died mid-wave-1 readback**: `console.request("read", address=requests[role])` timed
  out at `host.py:1963` — the payload went silent DURING the row extraction, before any
  `positive[marker=...]` line or JSON. The console pinged and the loader answered
  (`:9026` OPEN, `:9029` accepts-but-silent) — the documented **freeze**; power cycle
  required.
- **Reading: the wave LANDED somewhere outside the watched held paths** (canary/fillers/
  guards all answered nothing — no JSON exists). A landing on the *payload's own pages*
  (the wave's request/reply pages ARE payload heap) corrupts payload control state and
  kills it mid-readback. `changedPaths` cannot see such a landing by construction —
  only the 5 held paths are instrumented. Tally so far: canary 1, guard1 1, nowhere 7,
  **payload-fatal 1**.

### Discipline note (validated by this crash)

ns2jb gates `positive` behind extent-present's full qualification
(`selectedMarker && hasWindow`, `:616`) — we have been running it on `windowOk` alone
(todo #2's extent-present `selectedMarker` — the type-1 trigger resolution — is still
open). The reference also treats failed placements as **stop-and-recover**
(`placementUncertain`: save, retain, never retry blind) — this crash is exactly that
case: a landing we cannot locate, on a payload we cannot interrogate. The chain's
lottery is survivable (r37 landed clean), but each crash costs a power cycle, and the
landing surface includes the payload's own wave pages.

**Next boot options (decision for the user):**
1. **Re-drive the same A→B command** (power cycle first): the crash is a documented
   expected loss of the destructive profile; the lottery continues; ~1/9 per draw.
   If it crashes mid-readback AGAIN, the payload-page landing is systematic (2/10+)
   and needs a mitigation design, not more dice.
2. **Close todo #2 first** (extent-present `selectedMarker`): the SM3/SM6 offline dig —
   marker-offset sweep downward (fold carry limit) + the address-planner on the
   measured 12160 geometry — makes the destructive stage properly gated per ns2jb's
   own discipline before more positive draws. Costs engineering time, not boots.### Boot 20 (2026-11-05, ~13:40) — never sampled; console.js died on load → full freeze (T1 validated)

The re-drive of the `upto=prime` chain never reached a wave: the relocation chain printed
no mounts output, the console.js load sent the payload and then **died mid-handshake**
(`console closed the control connection`, `EOFError`), and the console went fully silent
— all three ports down **including the loader** (`:9026` timeout = the freeze
discriminator from boot 13).

**Reading: a game relaunch on top of boot 19's freeze, not a clean power cycle.** fsp-srv
worker-pool exhaustion persists system-wide until reboot (digest T1); console.js's own
setup opens fsp-srv, so a fresh payload on an un-rebooted console inherits the wedged
pool and dies on init — exactly the observed mid-handshake EOF followed by total
silence. A true power cycle brings the console up with a fresh pool; the loader only
answers once the game is relaunched AND the save trigger (`XSTRIG.Civ7Save`, Load Game)
has run the stager.

**Operational rule (now validated twice): after a freeze, POWER CYCLE — a game relaunch
does not clear it, and a payload loaded onto a frozen console dies on init and wedges
the loader state further.** After the power cycle: launch the game, Load Game (the save
trigger), THEN drive — the loader `:9026` answering is the readiness signal.

The A→B command is unchanged and queued: fresh flow → `leak-cycle.py full`
(`CIV_ALIAS_UPTO=prime`, `CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340`,
`CIV_ALIAS_MARKERS_MAX=3`, `CIV_PREREQ_SKIP=1`, empty `CIV_RELEASE_CLOSE_GATE`, r37
staging). Landing tally: canary 1, guard1 1, nowhere 7, payload-fatal 1. A second
mid-readback crash ⇒ the payload-page landing is systematic ⇒ mitigation design before
more draws (see boot 19's decision tree).### Boot 21 (2026-11-05, 13:41) — 3 draws, 0 landings, console HEALTHY; landing scatter confirmed

`leak-cycle.py full` (`upto=prime`, 3 markers, `CIV_PREREQ_SKIP=1`, empty close gate, r37
staging) on the fresh post-crash session:

| draw | window | landing |
| --- | --- | --- |
| 1 | AP False / win False | none |
| 2 | AP False / win False | **filler0** (`changed=['filler0']`) |
| 3 | AP True / win True | none |

Clean exit (the script's own raise), payload survived setup + 3 waves (4 reserve
cycles), console healthy after (`:9026`/`:9029` both answer). **The boot-19
payload-fatal did NOT repeat (1/11 draws) — not systematic; the lottery stays
winnable per the boot-19 decision tree.**

**Landing tally (all positive draws): 11 draws — canary 1 (r37a2), guard1 1 (r35b1),
filler0 1 (b21d2), payload-fatal 1 (b19d1), nowhere 7.** Four landings on four different
targets: textbook heap-geometry noise, not a stable preferred slot — SM3's alias-offset
model, now with the landing signal as a directly observed random variable.

**Operational model correction (user-confirmed):** boot 20's diagnosis was wrong —
that was a **game crash**, not a console wedge. The refined discriminator:

- **Game crash**: all ports dead (refused/timeout — the listeners die with the game
  process). Relaunch the game; everything comes back fresh.
- **Console wedge/freeze**: ports ACCEPT but stay silent (worker pool exhausted
  system-wide). Power cycle required.

### Budget & next-drive strategy (unchanged command)

This payload is spent (setup + 3 waves = 4 reserve cycles — the documented ceiling).
The next chain run needs a **game relaunch** (not a power cycle — the console is
healthy). Expected cost to reach Stage B: ~9%/draw canary-specific × 3 draws/boot ≈
**25%/boot**; the reference's own `retryWave` is exactly this dice-roll
(`:531-547`, retries until `markerSelected`). Continue the lottery with the same
`upto=prime` command; if two more boots land nowhere on canary, the marker-offset
landing-preference sweep (`CIV_ALIAS_MARKERS=0x2140,0x1b40,0x1340` — landing slot vs
marker offset was never cross-measured) becomes the cheap next experiment.### Boot 22 (2026-11-05, 13:44) — SECOND payload-fatal: the boot-19 decision-tree trigger has FIRED

Same `upto=prime` chain on a fresh flow: stage 0 clean (4 control reads, no strings),
reserve clean (`allocated 12160, outer 266`), all 13 frames launched — then the payload
**died mid-wave-1 readback** (`host.py:1963`, `console.request("read")` timeout), before
any JSON or `positive[marker=...]` line. Game alive after (ports accepting; loader
`:9026` answers) — payload dead, console healthy.

**That is the second mid-readback payload-fatal (b19 draw 1, b22 draw 1) — the
pre-agreed trigger ("a second mid-readback crash ⇒ the payload-page landing is
systematic ⇒ mitigation design before more dice") has FIRED. No more lottery draws
without an explicit strategy decision.**

**Landing tally (12 draws): canary 1 (r37a2), guard1 1 (r35b1), filler0 1 (b21d2),
payload-fatal 2 (b19d1, b22d1), nowhere 7.** Context split worth noting: all 6
standalone `worker-wave positive` draws (boots 15–17) were crash-free (one canary
landing); both chain-context crashes came on the chain's **draw 1** (leak-read stage 0
+ rereserve + wave). Possible confound: the chain's stage-0 rx_page/allocations shift
the payload heap the wave lands against; the standalone context may be safer. Sample
too small to call it.

### The decision (presented; not driven past)

- **(a) Continue the lottery** (relaunch → `upto=prime`): ~25%/boot to reach Stage B,
  ~25%/boot to crash (power cycle). The crash is an *expected loss* of the destructive
  stage per the handoff's own hazards ("crashes are expected; a mis-placed reservation
  corrupts memory"), and ns2jb's `retryWave` is the same dice — the reference has no
  mitigation either.
- **(b) Stop boots; offline work.** Two items, no console risk:
  1. **Extend the held-path watchers** to every wave page (owner, ctrl, tails' request
     pages, the rx_page): landings become `changedPaths`-visible instead of
     fatal-or-invisible — the payload-fatal class currently cannot be located by
     construction. Host-side change, offline-testable.
  2. **SM3 address-planner on the measured geometry** (12160-byte reservation, 4-slot
     landing scatter): if the landing offset distribution is predictable from the heap
     shape, marker placement (`CIV_MARKER_OFFSET`) or the canary's slot could be moved
     INTO the landing-prone region — converting the lottery structural.
- **(c) Middle path (my recommendation):** one more chain boot WITHOUT stage 0 —
  the two chain crashes both followed the leak-read stage; if a stage-0-free chain
  draw lands Stage B, the confound is real and the chain is safe without mitigation.
  If it crashes again (3/13), stop entirely and do (b).### Boot 23 (2026-11-05, 13:52) — (c) CONFIRMED: stage-0-free chain survives 3/3; the lottery went dry

**User decision (boot 22 → 23): option (c)** — test the stage-0 confound before more
lottery dice; follow-up direction: extend samples per payload (the ns2jb-method angle).

Implemented `CIV_STAGE0_SKIP=1` (`leak-cycle.py`): skips the romfs control reads (the
rx_page/block allocations that preceded both payload-fatals); stage B now derives the
storage size inline (`GetSize` = `packet(4)` on the opened control storage) when stage 0
is skipped. Compile-verified.

**Result: 3/3 draws SURVIVED, zero crashes.** All three windowed
(`AP True / win True / form accepted`), all three landed nowhere
(`canaryChanged=False`, `changed=[]`). Clean script exit; console healthy after
(`:9026`/`:9029` both answer); payload spent its full budget (setup + 3 waves = 4
reserve cycles).

**Confread verdict: SUPPORTED.** Crash record with stage 0: 2 fatals in 2 boots
(b19d1, b22d1, both mid-readback after stage 0). Without stage 0: 0 fatals in 3 draws
across 1 boot. The leak-read preamble's allocations shift the wave's landing into
payload memory — **the chain is drivable without stage 0, and should be driven that
way until Stage B lands** (re-enable stage 0 only when the C/D over-read needs its
control-prefix artifacts — and by then the chain is already past the crash window).

**Landing tally (15 draws): canary 1 (r37a2), guard1 1 (r35b1), filler0 1 (b21d2),
payload-fatal 2 (b19d1, b22d1 — both with stage 0), nowhere 10.** Canary rate 1/15 ≈
7%/draw → ~19%/boot at 3 draws. Two dry boots in a row now (b21: filler0, b23: none).

### Extending samples per payload (the follow-up direction, offline analysis)

The ~3-wave payload ceiling was matched to ns2jb's own throughput at round-8
("matching ns2jb's own 3-waves-per-payload"). The 4th cycle dies at **rereserve**
(open-but-silent — §44), after 3 successful reserve→wave→release cycles. Candidate
mechanisms, in diagnosis order (one disposable boot each, `CIV_RESERVE_TRACE=1`):

1. **fsp-srv-side session accumulation** — each `rereserve` opens fresh
   counters/owner-domain/root sessions (cached_session reuses by label, but the
   release/close of prior cycles' clones may leak handles server-side); the wedge
   point (`open counters session` vs `open owner domain` vs `CloneSession`) identifies
   which table fills.
2. **Payload heap fragmentation** — `freeAddresses` reclaims the recorded
   `wave_allocs`, but the rereserve path itself allocates (owner page content rewrite,
   clone list copy); a fragmentation-driven reserve failure would wedge at
   `59 manager objects created`.
3. **Manager RPC state** — the reservation is 59 `CreateObject` on a domain; if the
   FS manager tracks per-process object totals, the 4th cycle's 59-object batch could
   trip a cap the first three didn't (release may not fully deregister).

The lever depends on the diagnosis: (1) → close-on-release discipline (session close
order per the Q&A digest's teardown question); (2) → alloc reuse inside the payload;
(3) → interleave `negative`/cleanup waves. **Do not burn a boot on this until the
lottery question resolves** — 3 draws/boot is enough to keep fishing while Stage B's
prime assert is the actual gate.

**Next drive (unchanged command, stage-0-free):** relaunch → `leak-cycle.py full`
(`CIV_STAGE0_SKIP=1`, `upto=prime`, 3 markers). If a draw lands, Stage B runs in the
same payload and the prime assert answers "is the cache table inside the freed slot".### Boot 24 (2026-11-05, 13:57) — THIRD dry boot: 9 draws, 0 canary; throughput is the bottleneck

Stage-0-free chain (`CIV_STAGE0_SKIP=1`, `upto=prime`, 3 markers), fresh flow: **3/3 draws
survived** (zero crashes — the stage-0-free chain is now 9/9 draws crash-free across
boots 23/24), all three windowed (`AP/win True`), all three landed nowhere. Clean exit,
console healthy.

**Corrected tally context (boot 21 WAS chain-with-stage-0 — its `leak-control-x-block`
lines prove it):**

| context | draws | outcomes |
| --- | --- | --- |
| standalone `worker-wave positive` (b15–17) | 6 | canary 1, guard1 1, nowhere 4 |
| chain WITH stage 0 (b19, b21, b22) | 5 | payload-fatal 2, filler0 1, nowhere 2 |
| chain WITHOUT stage 0 (b23, b24) | 6 | **nowhere 6** |

**17 draws total: canary 1, guard1 1, filler0 1, payload-fatal 2, nowhere 12 → canary
≈ 6%/draw, ~17%/boot at 3 draws.** Three consecutive dry boots (b21/23/24: filler0,
none, none) — P(0 canary in 9 draws) ≈ 58% at 6%/draw, so not yet anomalous, but the
fishing cost is now 1 canary-landing per ~6 boots ≈ ~6 power-safe minutes… per *game
relaunch*, and each Stage B miss costs a relaunch.

**Consequence — the user's samples-per-payload direction is now the right next boot**:
ns2jb's `retryWave` is *payload-side JS* (`:531-547` retries until `markerSelected`
with no host round-trips); ours is host-driven and capped at ~3 reserve cycles by the
documented **4th-rereserve wedge** (§44). If the 4th cycle can survive, draws/boot
doubles-to-triples and the lottery's expected cost halves. Diagnosis plan (one
disposable boot, `CIV_RESERVE_TRACE=1`, `worker-wave positive 6` — dies at cycle 4 by
§44, and the trace line identifies the mechanism):

| wedge point | mechanism | lever |
| --- | --- | --- |
| `open counters session` / `CloneSession` | fsp-srv session-table accumulation (the wave's 13 clones + per-rereserve sessions leak server-side) | close-on-release discipline (CMIF Close per clone, the §43 fix's pattern) |
| `59 manager objects created` / `wrapped` | payload heap fragmentation (rereserve's own allocations) | alloc reuse / freeAddresses ordering |
| `open owner domain` | manager RPC state (release does not fully deregister) | interleaved cleanup (`negative` waves) |

Whichever line the 4th cycle dies at is the lever to build. **Do not re-drive the plain
lottery while this diagnosis is pending** — three dry boots say the lottery's expected
cost is now worse than the diagnosis boot's cost.### ★ Boot 25 (2026-11-05, 14:01–14:04) — WEDGE REFUTED: 6/6 cycles survived; the samples lever already exists

Wedge-diagnosis boot (the recorded plan): standalone `worker-wave positive 6` with
`CIV_RESERVE_TRACE=1`, fresh flow, r37 staging.

**Result: ALL SIX reserve cycles survived** (setup reserve + 6 waves = 7 cycles, ~21 s
per draw), zero wedge, zero crash, clean exit. The §44 "~3-cycle payload lifetime /
4th-rereserve wedge" **did not reproduce** — the ceiling is context- or boot-dependent
(the recorded wedges came from round-19's tuned 4th attempt, §17's pre-client-cache
freezes, and leak-cycle second-invocation setups — boot 15's 13:13 freeze was a second
SETUP, not a 4th rereserve).

Per-draw data (all six):

| draw | trigger | window | landing |
| --- | --- | --- | --- |
| 1 | 164 µs fast | AP✓ win✓ | none |
| 2 | 172 µs fast | AP✓ win✓ | none |
| 3 | 254 µs fast | AP✓ win✓ | none |
| 4 | 138 µs fast | AP✓ win✓ | none |
| 5 | 1223 µs mid | AP✗ | **filler1** (`changed=['filler1']`) |
| 6 | 230 µs fast | AP✓ win✓ | none |

Canary gate start deltas −54…−60 all six draws (the r37 staging holds at volume).

**Landing tally (23 draws): canary 1 (r37a2), guard1 1 (r35b1), filler0 1 (b21d2),
filler1 1 (b25d5), payload-fatal 2 (stage-0 chain), nowhere 17 → canary ≈ 4.3%/draw.**
Four distinct landing targets + fatals = confirmed scatter.

### Consequence: the throughput lever is `tries`, no engineering needed

At 6 draws/payload: **P(≥1 canary) ≈ 23%/boot** (vs 13% at 3 draws) — and the chain's
marker loop can carry the same budget (`CIV_ALIAS_MARKERS=0x2340 ×6`,
`CIV_ALIAS_MARKERS_MAX=6`, stage-0-free). One game relaunch per boot, ~2.5 min per
boot, no power cycles while crash-free. **Next drives: 6-marker chain boots until a
draw lands, then Stage B prime in the same payload.**

Console state: healthy after 7 cycles (`:9026`/`:9029` both answer). This payload is
now at its observed high-water mark — do NOT invoke again on it (the §43 second-setup
wedge is the remaining documented freeze class); relaunch for the next harvest.### Boot 26 (2026-11-05, 14:06) — 6-marker chain: 2 draws dry, then the 3rd rereserve WEDGED — the ceiling is variable, cycles 3–7

First 6-marker chain harvest (`CIV_ALIAS_MARKERS=0x2340×6`, `CIV_ALIAS_MARKERS_MAX=6`,
stage-0-free, `upto=prime`), fresh flow.

- Draw 1: AP✓ win✓, dry. Draw 2: AP✗ win✗, dry (`changed=[]`).
- **The 3rd rereserve then WEDGED**: `reserve[gap] open counters session` — the
  fsp-srv `open` timed out mid-rereserve (`host.py:1423` → `_receive` timeout). The
  payload was silent after (ports accepting, game alive — the wedge/aftermath shape).

**Refined wedge model: the ceiling is BOOT-VARIABLE, cycles 3–7.** Boot 25 ran 6/6
cycles clean on the same command family; boot 26 wedged at cycle 3 (its 2nd rereserve —
cycle count: setup(1) + wave1(2) + wave2(3) → the 3rd reserve hung). The §44 "~3-cycle"
constant was an artifact of small samples: the real behavior is a variable wedge point.
The wedge points so far: b13 (setup 2), b15 (setup 2), b19/b22 (mid-wave readback, after
stage 0 — fixed by `CIV_STAGE0_SKIP`), b26 (`open counters session`, cycle 3), r19
(re-reserve 4, tuned config). **No wedge has repeated at the same point twice with the
same config** — chasing the mechanism needs the trace line captured at the wedge, and
the current trace prints the step BEFORE the hang's RPC, so the hang is always one step
past the last trace line (`open counters session` = the `open` RPC that never returned).

**Landing tally (25 draws): canary 1, guard1 1, filler0 1, filler1 1, payload-fatal 2,
nowhere 19 → canary ≈ 4%/draw.**

### Practical harvest policy (replaces the "6 draws/payload" assumption)

The chain's marker loop dies with the payload at the wedge — expected draws/boot is
**2–6, geometric-ish** (P(wedge at cycle k) spread over 3–7). Two mitigating facts:

1. `worker_wave`'s retry loop and the chain's marker loop both rereserve per draw, so
   a wedge kills the process, not the boot — relaunch and continue (the game is alive).
2. The Stage A acceptance (`windowOk && canaryChanged`) breaks the loop early — a
   canary landing on draw 1–2 (before the wedge risk rises) is the common win path.

**Next drives: same 6-marker command, relaunch per wedge, until a canary draw lands —
then Stage B prime in the same payload. Do not spend effort on the wedge mechanism
until a canary landing + Stage B pass/fail is in hand** — the wedge only throttles
throughput, it does not block the chain (the r37 acceptance proves the landing path
works end-to-end through `worker_wave`).**### Boot 27 (2026-11-05, 14:16) — 6/6 draws clean, 0 canary; the landing DISTRIBUTION is now the signal

Second 6-marker chain harvest (same command): **all six draws ran, no wedge** (boot 25
clean, boot 26 wedged at 3 — the variable-ceiling model holds), clean script exit, console
healthy after.

| draw | window | landing |
| --- | --- | --- |
| 1 | AP✗ win✗ | **filler1** |
| 2 | AP✓ win✓ | none |
| 3 | AP✓ win✓ | none |
| 4 | AP✗ win✗ | **filler0** |
| 5 | AP✓ win✓ | none |
| 6 | AP✗ win✗ | none |

**Tally (31 draws): canary 1 (r37a2), guard1 1, filler0 2, filler1 2, payload-fatal 2
(stage-0 chain), nowhere 23 → canary ≈ 3.2%/draw.**

**The distribution is the new signal:** landings-on-somewhere = 8/31 (26%), but the
canary takes only 1 of the 8 — **fillers 4, guard 1, canary 1, fatal 2**. The fillers
(each expect 0x202, staged at the packet-tail pages) take 4× the canary's share. Two
readings, both actionable:

1. **Not yet anomalous:** at 3%/draw, P(0 canary in the chain's 25 draws) ≈ 46% — the
   chain's 0-canary streak is consistent with the r37 rate. No context switch justified
   by the streak alone.
2. **But the geometry is biased:** the aliased copy's server-side landing region
   favors the filler packets' pages over the canary's. If the bias is structural
   (staging-geometry), then **moving the canary's staged path INTO the favored region**
   — `CIV_SLOT_SWAP=6,7` (canary↔filler0, the round-13 machinery; slots 6–8/10–11 are
   identical saved clones so the swap is behavior-neutral) — would 4× the landing odds.
   CAVEAT: the swap moves the *client handle/drain order*, not the staged page; whether
   the landing follows the client or the staged path is exactly what one boot of
   `CIV_SLOT_SWAP=6,7` measures.

### Standing policy + the one-variable experiment queue

Continue 6-marker chain boots (relaunch per wedge) until a canary draw lands → Stage B
prime in the same payload. **If two more boots go canary-dry** (≥43 draws at 0), the
bias reading upgrades and the queued experiment is: one boot with
`CIV_SLOT_SWAP=6,7` + 6 markers — a canary landing under the swap proves the
landing-follows-client model and the odds lever; a filler landing under the swap proves
landing-follows-staged-page and the lever is staging geometry instead (SM3's territory).
Both outcomes are progress; the cost is one boot.### Boot 28 (2026-11-05, 14:21) — 6/6 clean, 0 canary, FIRST DOUBLE-LANDING; the two-dry-boot trigger FIRED

Third 6-marker chain harvest (same command): all six draws ran, no wedge, console
healthy. **Landing-hot boot — 5 landing events on 6 draws:**

| draw | window | landing |
| --- | --- | --- |
| 1 | AP✓ win✓ | none |
| 2 | AP✗ | **filler1** |
| 3 | AP✗ | **guard0** |
| 4 | AP✗ | **guard0** |
| 5 | AP✓ win✓ | none |
| 6 | AP✗ | **filler1 + guard0** (DOUBLE) |

**Draw 6 is the first observed DOUBLE-LANDING** (`changed=['filler1', 'guard0']`) —
the aliased write scatters MULTIPLE metadata copies per wave, not one. Model
consequence: the canary needs one scatter point to land on it; per-wave landing
"count" is ≥ the changed-path count.

**Tally (37 draws): canary 1 (r37a2, standalone), guard1 1, filler0 2, filler1 3,
guard0 3, payload-fatal 2 (stage-0 chain), nowhere ~25. Landing events: 12, of which
canary 1 (8%) vs fillers+guards 10 (83%).** The chain context specifically: ~25 draws,
0 canary landings. P(0-in-25 at the 4% overall rate) ≈ 36% — borderline; at the
filler/guard-heavy observed mix it is worse.

### The recorded policy trigger has FIRED: the `CIV_SLOT_SWAP=6,7` boot is due

Boot 27 + boot 28 were the "two more canary-dry boots" the boot-27 policy named. The
queued one-variable experiment:

```
CIV_SLOT_SWAP=6,7   (canary ↔ filler0 client slots; identical saved clones,
                     behavior-neutral per the round-13 swap machinery)
+ the same 6-marker chain command
```

- **Canary lands under the swap** ⇒ landing-follows-CLIENT (drain/slot geometry) —
  the odds lever is real: keep the swap, harvest 6 draws/payload at the filler's
  observed rate (~10–15%/draw), Stage B follows.
- **Filler0 lands under the swap** (or filler1/guard0 keep landing) ⇒
  landing-follows-STAGED-PAGE (the aliased copy targets the staged path bytes, not
  the client) — the lever is staging geometry: move the canary's staged path to the
  favored page (SM3's alias-offset territory), not its client slot.

Either outcome converts the lottery structural. One boot, both answers progress.
The double-landing makes the swap MORE worth running: if one wave scatters 2+
copies, a canary moved into the scatter-favored region takes multi-hits per wave.### Boot 29 (2026-11-05, 14:29) — SLOT SWAP: inconclusive, and the aggregate says it HAD to be

`CIV_SLOT_SWAP=6,7` (canary↔filler0 client slots, round-13 machinery) on the same
6-marker chain command: 6/6 draws clean (no wedge), 4/6 windowed, 0 canary, **1 landing
— on guard1, a role the swap does not touch**. Neither swapped role was hit.

**Aggregate re-analysis (13 landing events / ~43 draws) — the distribution is
UNIFORM over the 5 held paths:**

| path | events | uniform expectation |
| --- | --- | --- |
| filler1 | 4 | 2.4 |
| guard0 | 3 | 2.4 |
| filler0 | 2 | 2.4 |
| guard1 | 2 | 2.4 |
| canary | 1 | 2.4 |

χ² ≈ 2.2, df=4, p ≈ 0.5–0.7 — **no significant bias.** The boot-27/28 "fillers take
4× the canary" reading was premature pattern-matching (it dissolved at n=13 events).
**Landing-somewhere ≈ 30%/draw; canary ≈ 6%/draw under uniform (1/5 of scatters)** —
observed 1/43 draws (2.3%): Poisson noise (P(≤1 | λ=2.6) ≈ 0.27).

**Consequences:**

1. **The swap experiment's premise was wrong** — there is no disfavored/favored slot
   to move the canary into. Its inconclusive result (guard1, an untouched role, at
   1/5 of a uniform draw) is exactly what uniform predicts. **Swap REVERTED** — no
   benefit shown, and it adds config drift to the frozen harvest.
2. **The chain's 0-canary streak (~31 draws) is P ≈ 15% at the uniform 6%/draw** —
   unlucky, not anomalous. **Keep rolling the frozen 6-marker harvest is the
   statistically honest answer.**
3. **Stopping rule (defined):** if the next TWO boots go canary-dry (~12 more draws →
   43 chain draws at 0, P(0-in-43 | 6%) ≈ 7%), "unlucky" dies — then the lever is
   SM3's staging-geometry analysis (per-draw landing rate ~30% is real and could be
   steered by where the staged paths sit in the heap), not client slots.

Standing: relaunch → the unchanged 6-marker chain command (`CIV_STAGE0_SKIP=1`,
`upto=prime`, markers ×6), 6 draws/boot, wedge-relaunch as needed. First canary
landing → Stage B prime in the same payload.### Boot 30 (2026-11-05, 14:34) — 3 dry draws + payload-fatal at draw 3 (stage-0-FREE): dry boot 1/2 of the stopping rule

Frozen 6-marker harvest (swap reverted, command unchanged): draws 1–2 windowed/dry
(d1 AP✓, d2 AP✗), then **draw 3 died mid-wave-readback** (`host.py:1963`, the b19/b22
death point) — **the payload-fatal class hit WITHOUT stage 0**. Game alive (ports
accepting), payload dead.

**Refined fatal model:** with stage 0 → 2 fatals/5 draws; without → **1/27 draws**
(b30d3). `CIV_STAGE0_SKIP` cuts the fatal rate ~10× but does not eliminate it — the
landing lottery's scatter includes payload memory, and stage-0-free draws just make
that outcome rarer. Expected loss, same as any landing-miss: relaunch and continue.

**Tally (~46 draws): canary 1, guard1 2, filler0 2, filler1 4, guard0 3, payload-fatal
3, nowhere ~31.** Landing-somewhere ≈ 28%/draw; canary ≈ 2%/draw observed vs ~5.6%
uniform expectation (1/5 of scatters) — still Poisson-compatible (P(≤1 | λ=2.6)
≈ 0.27).

**Stopping-rule state: boot 30 = dry boot 1 of 2.** If the next boot (6 draws) also
goes canary-dry, the chain's streak hits ~37 draws at 0 (P≈5–7% at uniform) — "unlucky"
dies, and the lever becomes SM3's staging-geometry analysis: steer the ~28%/draw
scatter by where the staged paths sit in the heap (alias-offset territory), not
client slots. If a canary lands first, Stage B prime runs in the same payload.### Boot 31 (2026-11-05, 14:41) — DRY BOOT 2/2: the stopping rule FIRED — chain context never lands on the canary

Frozen 6-marker harvest: 6/6 draws clean (no wedge, no fatal), **0 canary landings**.
Landings: filler0 (d1), guard0 (d3, d6). Dry boot 2 of 2 per the boot-29/30 stopping
rule — **the rule has fired.**

**Final tally split by context (the decisive cut):**

| context | draws | canary landings | somewhere landings |
| --- | --- | --- | --- |
| standalone `worker-wave positive` | ~13 | **1** (r37a2) | 2 (guard1, filler1) |
| **chain context** (`leak-cycle full`, stage-0-free) | ~37 | **0** | 11 |

Chain streak: **~37 draws, 0 canary** — P(0 | 5.6%/draw uniform) ≈ 5–7%, at/below the
pre-agreed anomaly threshold. But the sharper signal is the **context asymmetry**: the
one canary landing of the campaign came STANDALONE (r37a2), and the chain context —
which alone has run Stage A 31+ times since — has hit every held path except the canary
(filler0 ×2, filler1 ×3, guard0 ×5, guard1 ×1). χ² on chain-context landings
(11 events, canary expected 2.2): **canary 0 vs ≥2.2 expected — p ≈ 0.10** on its own;
combined with the standalone/chain asymmetry (1/13 vs 0/37), the reading is
**"unlucky" plus a possible context effect** — the chain's own allocations
(rereserve-per-draw vs standalone's client-reuse) shift the landing geometry away
from the canary's slot.

### The pivot (per the fired rule): SM3 staging-geometry, with a context control first

**Do not drive more chain boots unchanged.** Two recorded next steps, in order:

1. **Context control (one boot, cheap):** standalone `worker-wave positive 6` (the
   b25 command that produced 6/6 clean draws) — NOT the chain. If a canary landing
   appears in standalone's 6 draws (observed rate there: 1/13 → ~40%/boot at 6 draws),
   the context effect is real and **the chain's Stage A should be re-pointed at the
   standalone draw shape** (drop the chain's rereserve-per-draw; reuse the b25
   client/session reuse pattern) before any geometry work.
2. **If the context control confirms the asymmetry (or is ambiguous):** SM3's
   staging-geometry analysis — the landing offset distribution vs the staged paths'
   heap positions (alias-offset planner on the measured 12160-byte geometry) — to
   bias the ~30%/draw scatter toward the canary's page structurally.

The chain scaffolding (B/C/D) stays ready; only Stage A's draw shape is in question.### Boot 32 (2026-11-05, 14:46) — context control NEGATIVE: standalone also 0/6; the lottery is uniform-unlucky, not context-blocked

Standalone `worker-wave positive 6` (the b25 shape, no chain): **6/6 draws clean,
0 canary** — landings: guard1 (d6). Fast triggers (95–357 µs) on 5/6, all gates healthy.

**The context hypothesis is NOT supported.** Standalone also missed 6 in a row —
which at the uniform model's ~5.6%/draw is P ≈ 71% per 6-draw boot. Updated stats:

| context | draws | canary | somewhere | canary rate |
| --- | --- | --- | --- | --- |
| standalone | 19 | 1 | 3 | 5.3% |
| chain | ~37 | 0 | 11 | 0% |

- Fisher exact on the asymmetry (1/19 vs 0/37): **p ≈ 0.33 — not significant.** The
  context effect cannot be claimed; the uniform model stands.
- Combined: 56 draws, 1 canary vs expected ~3.1 (λ = 0.056×56) — **P(≤1 | λ=3.1) ≈
  13%. Unlucky, not anomalous.** The earlier stopping-rule math (P≈5–7% at 43 draws)
  was for the streak including standalone draws; with b32's data the streak regressed
  toward noise.

### Revised verdict: the lottery is honest; rolling is cheap; SM3 is the parallel stream

1. **Keep rolling chain boots** — expected **~3.4 boots per Stage A landing**
   (1/(1−0.944⁶) at 6 draws/boot), each boot ~3 min. Stage B auto-follows in the
   landing payload. This is the ONLY context Stage B can run in, and its cost is
   trivial; the "chain never lands" alternative (p≈0.33) cannot be ruled out but also
   has no mechanism behind it.
2. **Re-trigger rule:** if the next THREE chain boots go canary-dry (~18 more draws →
   55 chain draws at 0, P ≈ 4% at uniform), stop rolling for good and go fully
   structural (SM3 + re-pointed staging).
3. **Parallel offline stream (no console):** the 14 landing events in the attempt
   artifacts ARE the landing-offset measurements (result-word clobbers with distinct
   residues) — SM3's analysis of where the aliased write lands vs the staged paths
   proceeds from `runs/worker-wave-positive-attempt*.json` + the r37 milestone, no
   boot required, and feeds any future re-staging decision.### Boot 33 (2026-11-05, 14:52) — 6/6 clean, ZERO landings anywhere; dry chain boot 1/3 of the hard stop rule

Frozen 6-marker chain: all six draws clean (no wedge, no fatal), **zero landings on any
held path** (`changed=[]` ×6) — the driest boot of the campaign. Five of six windowed
(AP✓). Console healthy.

**Stop-rule state: chain-dry boot 1/3.** Streaks: chain ~43 draws / 0 canary; combined
~62 draws / 1 canary vs λ≈3.5 expected → P(≤1)≈14% (still noise). Landings-somewhere
rate may be drifting down (last three chain boots: 5 events/6, 1/6, 0/6 — small n,
watch it); if the *somewhere* rate itself collapsed (30% → ~10%), the alias firing rate
is what moved, and that is a **wave-content question** (the aliased write only lands
when the fold carries a path — recent draws may be folding cold), worth checking the
trigger's `result` word per draw in the next boot's attempt JSONs (`0x2EEC02` = form
accepted/fold hot vs `0x2EEA02` = rejected) before trusting pure luck as the model.

Next: two more chain boots (unchanged) — a canary landing → Stage B; two more dry →
the hard stop fires and SM3 re-staging owns the question.### Boot 34 (2026-11-05, 14:57) — payload-fatal at draw 1 (3rd stage-0-free); dry boot 2/3 by the crash rule

Frozen 6-marker chain: **draw 1 died mid-readback** (`host.py:1963`, the b19/b22/b30
death point) before any marker line or JSON. Game alive, payload dead — relaunch clears
it. Stage-0-free fatals now 3/30 draws (10%) vs 2/5 with stage 0.

**Classification: dry boot 2/3** — a payload-fatal burns the boot exactly like a dry
boot does (no Stage A landing, relaunch required), so the hard stop rule counts it.
Boot 33 (0 landings) = dry 1/3; **boot 34 = dry 2/3.** One more dry-or-fatal chain boot
and the rule fires → SM3 re-staging owns the Stage A question.

Combined streaks after b34: chain ~44 draws / 0 canary (P≈6% at uniform); combined ~63
draws / 1 canary vs λ≈3.5 (P≈14%). Landings-somewhere over the last 4 chain boots:
5/6, 1/6, 0/6, 0/6-with-fatal — the fold-hot/cold check (trigger result words per draw,
`0x2EEC02` hot vs `0x2EEA02` cold) is queued for the next boot's attempt JSONs; if the
recent boots are folding cold, the landing-rate collapse is wave-content, not luck.### Boot 35 (2026-11-05, 15:01) — DRY BOOT 3/3: THE HARD STOP FIRED; fold is HOT every draw — geometry owns Stage A

Frozen 6-marker chain: **6/6 draws clean, 0 canary, 1 landing (filler0, d3)**. Dry boot
3 of 3 per the boot-32 rule. **The hard stop rule has FIRED — no more unchanged chain
boots.**

**Fold-hot/cold check (the boot-33-queued diagnostic) — ran on the available
artifacts:**

- Boot 32 (standalone, the only boot whose per-attempt JSONs survive): **6/6 HOT**
  (`0x2EEC02` — form accepted, fold carrying a path — every draw).
- Boot 35 draw 6 (`worker-wave-positive.json`, last write): **HOT**.
- **Verdict: the landing-rate-collapse theory is DEAD.** The fold fires hot on every
  observed draw; the aliased write is live each time. What misses is the LANDING SLOT —
  the write lands on filler/guard request pages and not the canary's. **Geometry, not
  wave content.**

**Diagnostic footgun discovered and recorded:** the per-attempt JSON copies
(`worker-wave-positive-attemptN.json`) are written only by the CLI retry-loop path
(`worker-wave positive 6`); `leak-cycle.py`'s single-try marker loop overwrites
`worker-wave-positive.json` but never writes attempt copies — **chain boots leave the
last standalone run's attempt files on disk.** My first hot/cold parse read boot 32's
files thinking they were boot 35's. Always check mtimes against the boot window before
attributing attempt artifacts.

**Final lottery tally (~50 draws): canary 1 (r37a2), guard1 3, guard0 5, filler0 4,
filler1 4, payload-fatal 3, nowhere ~30.** Landing events on held paths: 17, of which
canary 1 (6%) vs fillers+guards 16 (94%) — **χ² on held-path landings: canary expected
3.4, observed 1; the fillers/guards region takes ~94% of landings.** Uniform-over-5 is
borderline-rejected (p ≈ 0.08–0.12); the landing region is biased AWAY from the canary's
staged page.

### THE PIVOT (rule fired): Stage A is a staging-geometry question — the re-staging design

The aliased write lands on held-path **request pages** (the staged path bytes the FS
copies during resolution — the clobbered result words prove the path text itself was
overwritten). The fillers/guards' staged pages sit in the landing-prone region; the
canary's does not. **The lever is not the client (the boot-29 swap showed nothing) and
not the timing (the window holds every draw) — it is WHERE the canary's path text is
staged in the payload heap.**

**Designed experiment (offline-implementable, one boot to validate):** a
staged-address swap — stage the canary's path text at a landing-prone page (a filler's
packet-tail offset) and the filler's text at the canary's old page, keeping every
client, delay, and gate identical. If canary landings rise to the filler/guard rate
(~10–15%/draw → ~60%/boot at 6 draws), Stage A becomes structural and the chain
resumes with B/C/D. If landings stay on fillers/guards, the landing follows something
other than the staged page's address (allocation order inside the FS, not the payload
heap) and the dig goes to SM3's planner proper.

Implementation: env-gated (`CIV_STAGE_SWAP=canary,filler0` or similar) in `host.py`'s
staging; offline-verifiable by byte-diffing the staged packets; no console needed until
the validation boot.### Stage-swap IMPLEMENTED and offline-verified (2026-11-05, ~15:15) — validation command queued

`CIV_STAGE_SWAP=x,y` landed in `host.py`:

1. `stage()` now records `entry["payload"]` (the padded bytes) — swap rewrites need it.
2. `Harness.swap_staged_pages(x, y)`: rewrites each entry's payload into the OTHER's
   page (padded to its own descriptor size, asserted < 0x1000), then exchanges the
   entry `address` fields. Every packet still describes byte-identical data; only the
   payload-heap placement of the path text moves.
3. `setup()` invokes it after all staging, before any verification (the verified paths
   — `root_dir`/`scratch_dir`/`canary_file` — are separate entries; a
   `canary,filler0` swap touches none of them).

**Offline verification (fake console, truthful page model):** all bytes through each
path's NUL terminator identical pre/post-swap (the only diffs are alloc-residue past
the terminator, which path resolution ignores); addresses exchanged; sizes preserved.
Default mode byte-identical: `wire-selftest.cjs` + `wire-selftest.py` + `wire-diff.py`
all pass unchanged (`frames: 17, replies: 4, identical: true`).

**Validation boot (queued, the round-48 experiment):** the frozen 6-marker chain
command + `CIV_STAGE_SWAP=canary,filler0` —

```
CIV_ALLOW_OVERWRITE=1 CIV_PREREQ_SKIP=1 CIV_STAGE0_SKIP=1 CIV_ALIAS_UPTO=prime
CIV_RELEASE_CLOSE_GATE= CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340,0x2340,0x2340,0x2340
CIV_ALIAS_MARKERS_MAX=6 CIV_STAGE_SWAP=canary,filler0
CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000
CIV_NORMALIZED_PATHS=1 CIV_CANARY_GATED=1 CIV_CANARY_OFFSET_NS=40000 CIV_CANARY_SIZE=0x5a0
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 CIV_GUARD_DELAY=2550000
CIV_GUARD_SPREAD=0 CIV_FILLER_DELAYS=2480000,2300000
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 CIV_TRIGGER_TAIL=100000
  python leak-cycle.py full
```

**Decision tree:** canary landings rise toward the filler/guard rate (≥1 in 6 draws,
~60%/boot at parity) ⇒ **landing follows the staged page's payload-heap address —
Stage A structural: keep the swap permanently**, resume the chain, Stage B auto-follows
the first landing. Landings stay on fillers/guards (canary 0 in 6) ⇒ landing follows
**FS-side allocation order**, not the payload heap ⇒ SM3's planner proper owns the
question, and the swap stays as a recorded dead end.### Boot 36 (2026-11-05, 15:11) — stage-swap VALIDATION: no effect observed — but the boot was UNDERPOWERED to detect one

`CIV_STAGE_SWAP=canary,filler0` on the frozen 6-marker chain (swap confirmed active,
log line 12): 6/6 draws clean, landings filler0 (d2), filler1 (d3), guard0 (d6) —
**canary 0/6**. Console healthy.

**Statistical honesty (the important part):** the boot cannot separate the two models.

| model | canary prediction | P(0 in 6) | observed |
| --- | --- | --- | --- |
| page-following (swap works) | canary text at filler0's page → ~9%/draw | 57% | 0/6 |
| role-following (FS-side object) | canary keeps ~5%/draw | 74% | 0/6 |

Both predict 0–1 canary hits at 6 draws; distinguishing a 5%→15% rate shift reliably
needs **~30 draws (~5 boots per condition)**. Boot 29's client-slot test
(`CIV_SLOT_SWAP=6,7`, canary→slot 7) is stuck at the same floor (0/6). **Every
single-boot discriminator on this question is statistically incapable of deciding.**

**Verdict (provisional):** no effect observed → consistent with the role-following /
FS-side model (the aliased write clobbers the FS's server-side copy of a role's path —
an object the payload-heap swap cannot move); the pre-recorded decision tree points
there. **But "consistent with" is all 6 draws buys.** Swap REVERTED from the frozen
command (no benefit shown; do not carry config drift into rate measurement).

### The two ways out (the real decision)

1. **Volume (a matched rate campaign):** ~44 chain draws exist WITHOUT the swap
   (canary 0, landings-somewhere 16). A matched WITH-swap campaign of **5 boots × 6
   draws** would measure the canary rate at n=30: ≥2 canary landings ⇒ page-following
   (keep the swap permanently); 0–1 ⇒ FS-side model confirmed, SM3's planner owns it.
   Cost: ~25 min + 5 relaunches.
2. **Mechanism (offline, no console):** the SM3 dig — derive from the artifacts what
   the landing actually clobbers. Evidence in hand: every landing produces the SAME
   result class (0x2EEA02-with-residue) = the normalizer-underflow error on that role's
   path text — i.e. the aliased write corrupts a role's PATH TEXT as the FS resolves
   it, and the clobbered text is whatever the fold carried (path-derived bytes). The
   question "which role's text" is a question about **the FS-side copy layout**:
   derivable from the reservation shape + dispatch order (allocation sequence),
   which the wave structure fixes — no boots needed to model it, only to validate a
   re-staging prediction.

**Recommendation:** mechanism first — the offline dig costs zero boots and, if the
FS-side layout model predicts which slot the fold's scatter reaches, it produces a
re-staging design (move the canary's FS-side object into the scatter region) that one
validation boot then confirms. Rolling more lottery boots in the meantime stays
available (~17%/boot) but buys no information beyond the rate itself.### SM3 DIG (2026-11-05, ~15:20) — the landing law found: freed holes, window anti-correlation; the size-class lever

Compiled the full draw-level dataset from every positive-wave log (~64 draws, fatals
excluded). **The landing is governed by the window state, not slot luck:**

| draw class | count | landing draws | rate |
| --- | --- | --- | --- |
| non-windowed (AP=False) | ~20 | 17 | **~85%** |
| windowed (AP=True) | ~45 | 2 | **~4%** |

- **Non-windowed draws land almost always** — and non-windowed means some held request
  COMPLETED before the trigger's return (a slow/deferred trigger overlaps the queue's
  drain): its server-side path copy FREES mid-fold, the heap churns, and the aliased
  write lands in/near the holes. b32a6 is the microscope: guard0 ended early (freed),
  the hit landed on **guard1** — the freed hole's *neighbor*, not the freed role.
  The write clobbers copies ADJACENT to freed holes.
- **Windowed draws (all held, fast trigger) almost never land** — no holes, the write
  misses (or hits non-role heap, unobservable). The two windowed hits: b36d3 (filler1)
  and **r37a2 (the canary — the Stage A qualification)**.
- **Stage A's rarity is now exact:** acceptance = windowed ∧ canary-hit ≈ 4%/draw ×
  (canary's share of windowed hits ≈ 1/2 observed) ≈ **2%/draw** — one event in ~45
  windowed draws (r37a2) — the campaign's own data confirms the rate we've been
  fishing at. The lottery was never 6%-uniform; it is **4%-conditional × slot-share**.
- The client-slot (b29) and staged-page (b36) swaps both null because neither moves
  what matters: the landing follows the **server-side copy layout + freed holes**,
  which ride the request's DECLARED SIZE CLASS, not the client handle or the payload
  page address.

**H1 — the size-class lever (the actionable model):** the trigger's server-side copy
is 0x800-piece class (2048); the fillers' declared 0x7f0 (2032) shares that class →
their copies are heap-adjacent to the trigger's → they dominate hits (b36d3's windowed
hit = filler1). The canary's declared 0x5a1 (1441) sits in another class region → its
windowed-hit share is the 1-of-2 observed. **Prediction: `CIV_CANARY_SIZE=0x7f0`
(declared 0x7f1, same class as fillers/trigger) raises windowed canary hits toward the
fillers' rate** — Stage A becomes ~structural. Stage B compatibility: the freed canary
slot grows to 0x7f1 (the 0x5a0 cache bucket table still fits — a bigger hole). Risks:
the 2032-char normalized chain (63 levels; 1440/44-levels worked at boot 2) and
2033-char direct resolution (1441-char proved; 2033 untested) — if chain-create or
verify fails at setup, the fallback is the deep-form canary with declared 0x7f0
(short text, same class, no chain dependency).

**Validation command (one boot):** the frozen 6-marker chain with
`CIV_CANARY_SIZE=0x7f0` as the ONLY change. Decision tree: ≥1 windowed canary landing
(≈ filler-rate) ⇒ H1 confirmed, Stage A structural, chain resumes with B/C/D; 0
canary landings in 6 ⇒ H1 dead at n=6 power (P(0|filler-rate)≈53% — weak), then the
deliberate-hole wave design (a 14th sacrificial early-completing role adjacent to the
canary's class) is the next structural idea, before any more volume.

**The dig's other output:** the ~85%-on-non-windowed landing law means the destructive
profile's *observable* landing channel (changedPaths) is dominated by freed-hole noise
that Stage A does not need — the qualification event is the rare windowed live-copy
hit, and H1 is the first lever aimed at it.### Boot 37 (2026-11-05, 15:21) — H1 validation: observed-null but UNDERPOWERED (2 windowed draws); law re-confirmed 21/23

`CIV_CANARY_SIZE=0x7f0` (declared 0x7f1, the fillers'/trigger's 2032-class) as the only
variable on the frozen 6-marker chain. **The 2032-char normalized chain created
fully** (62/62 steps `0x0` — the fallback risk did not materialize), canary resolved
0/type 1 every draw, gate held, 6/6 clean draws, console healthy.

| draw | class | landing |
| --- | --- | --- |
| 1 | non-windowed | **filler1** |
| 2 | windowed | none |
| 3 | non-windowed | none |
| 4 | non-windowed | **filler1 + guard0** (double) |
| 5 | windowed | none |
| 6 | non-windowed | **guard0** |

- **Canary 0/6 — H1 observed-null.** But the test was underpowered AGAIN: only **2
  windowed draws** occurred this boot (the window rate itself dropped, 2/6 vs the
  historical 4–6/6), and both were dry. P(0 canary | filler-rate ~10%/windowed-draw,
  n=2) ≈ 81% — a weak null. H1 lives but cannot be validated at this power: doing so
  by rate measurement needs ~20–30 windowed draws ≈ **15+ boots**. Not worth it.
- **The landing law re-confirmed harder:** 4/4 landing events this boot on non-windowed
  draws → **~21/23 campaign-wide.** Freed-hole landings are the dominant channel;
  windowed live-copy hits (the Stage A channel) remain ~3–6%/windowed-draw.
- **The rate-measurement route to H1 is closed** (the boot-36 power analysis + this
  boot's 2-windowed-draw yield). What remains actionable is the recorded next
  structural idea: **the deliberate-hole wave design** — a 14th sacrificial role
  (not in `held_paths`, so `allPending` ignores it) whose request completes early,
  freeing its server-side copy mid-fold WHILE the window holds; if its copy is
  heap-adjacent to the canary's (same 0x7f1 class, staged adjacent), the aliased
  write's freed-hole channel — the ~85% one — becomes aimed AT the canary inside
  windowed draws. That converts Stage A from a 2%/draw lottery into an engineered
  ~85%-of-windowed-draws event.
- **That design is an offline host.py task first** (14th role: staging, client slot,
  delay class, gates; the wave structure is 13-role-fixed today), then one validation
  boot. No more lottery/H1 boots in the meantime.

Console healthy after 7 cycles; payload spent.### Deliberate-hole design IMPLEMENTED (2026-11-05, ~15:35) — `CIV_SACRIFICE` role 13, offline-verified

The round-52 design landed in `host.py`, env-gated (`CIV_SACRIFICE=1`), default mode
byte-identical (wire selftests: `identical: true`):

1. **setup()**: stages `sacrifice0` — a MISSING normalized path padded to 0x7f0
   (2032) chars, declared **0x7f1** (text+NUL exactly — the round-25b one-byte lesson
   applied), the SAME FS allocator class as the gated canary at
   `CIV_CANARY_SIZE=0x7f0` (declared 0x7f1). Both server-side copies share the bin.
2. **worker_wave()**: role 13 appended to `roles`; its own saved clone at client slot
   13 (drains after the trigger; its fast miss reply returns long before); packet =
   `path_packet(7, sacrifice0)` context 3; the plan branch is **sleep + send only**
   (`CIV_SACRIFICE_DELAY`, default 1.2 ms) — no gates, no CF byte, NOT in
   `held_paths`/`protected_roles` (allPending, changedPaths, and nativeGuard all
   ignore it — the window is untouched by construction).
3. **Mechanism intent:** the 0x202 miss reply frees the 0x7f1-class server-side copy
   at ~1.5 ms — BEFORE the canary's ~2.41 ms gated dispatch (LIFO reuse: the canary's
   copy can land IN that hole) and before the trigger's fold. The freed-hole landing
   channel (~21/23 of all observed landings) becomes AIMED AT the canary's region
   inside windowed draws — Stage A converts from a ~2%/draw lottery to an engineered
   event.

**Offline verification:** compile ✓; normalized path = 2032 chars exactly; declared
size 0x7f1 = 2033 = text+NUL (terminator covered) ✓; packet builds ✓; default-mode
`wire-selftest.cjs`/`.py`/`wire-diff.py` all identical ✓.

### Validation boot (queued — one variable set vs boot 37)

```
CIV_ALLOW_OVERWRITE=1 CIV_PREREQ_SKIP=1 CIV_STAGE0_SKIP=1 CIV_ALIAS_UPTO=prime
CIV_RELEASE_CLOSE_GATE= CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340,0x2340,0x2340,0x2340
CIV_ALIAS_MARKERS_MAX=6 CIV_SACRIFICE=1
CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000
CIV_NORMALIZED_PATHS=1 CIV_CANARY_GATED=1 CIV_CANARY_OFFSET_NS=40000 CIV_CANARY_SIZE=0x7f0
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 CIV_GUARD_DELAY=2550000
CIV_GUARD_SPREAD=0 CIV_FILLER_DELAYS=2480000,2300000
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 CIV_TRIGGER_TAIL=100000
  python leak-cycle.py full
```

(vs boot 37: `CIV_SACRIFICE=1` added — canary stays at 0x7f0 so the classes match.)

**Decision tree:** ≥1 `canaryChanged=True` windowed draw in 6 ⇒ the freed-hole aim
WORKS — Stage A structural, chain resumes with B/C/D immediately. 0/6 windowed-draw
canary hits ⇒ weak null (P(0 | 30%/windowed-draw, ~4 windowed draws) ≈ 24%) — one
more validation boot at `CIV_SACRIFICE_DELAY=800000` (earlier free, more reuse window)
before the design is judged. A canaryChanged with `allPending=False` still proves the
aim (log it; the window and the aim are independent).### Boot 38 (2026-11-05, 15:29) — sacrifice ran but its PREMISE FAILED: mid-busy sends defer; the hole frees after the fold

`CIV_SACRIFICE=1` validation (canary 0x7f0, classes matched): 6/6 clean, **5/6
windowed** (high window rate — good validation power), landings: filler1 (d1,
non-windowed). **Canary 0/6 — weak null as the decision tree predicted**
(P(0 | 5 windowed draws) ≈ 17%).

**The decisive new evidence is the sacrifice row's own timing (draw 6):**

```
sacrifice0  1016.9 -> 3579.3  (2562 us — DEFERRED, serviced in the queue drain)
trigger     2470.6 -> 2600.7  ( 130 us — fold ran ~2.5-2.6 ms)
canary      2414.4 -> 3878.6  (resolved 0/type 1, gated -57 us)
```

**The design's premise was wrong: a pathPacket sent at 1.2 ms (mid-busy) DEFERS** —
the wave's own mechanism (every send during the hole's read blocks) applies to the
sacrifice too. Its 0x202 reply marks its SERVICE at 3.58 ms, i.e. its copy freed
**~1 ms after the trigger's fold had already run**. There was no hole to aim. The
"fast miss reply" class (300–650 µs) belongs to sends OUTSIDE the busy window, which
the 1.2 ms delay avoided by design (it wanted "before the canary" but got "inside the
hole's shadow"). The queued `CIV_SACRIFICE_DELAY=800000` variant is **invalid by the
same evidence** — any send after ~0.1–0.3 ms defers to drain.

**Revised variant (the only one the timing law permits): `CIV_SACRIFICE_DELAY=0`.**
Sent at t=0 (racing the hole's read arrival, before the queue blocks), the miss
reply can service at ~0.2–0.4 ms — a 0x7f1-class hole freed BEFORE the tails dispatch
(0.35–0.65) and 2 ms before the fold. Two possible outcomes, both informative:

- **Fillers' copies (declared 0x7f0) take the hole** (same bin) → the canary (0x7f1)
  gets the fillers' vacated slots or fresh memory — aim degraded to the old lottery.
  Detection: landings stay filler-heavy.
- **The 2033/2032 bins differ** → the hole survives to the canary's dispatch → its
  live copy lands in the freed 0x7f1 hole → the fold's freed-hole channel (the
  ~21/23 channel) hits the canary's region inside windowed draws. Detection:
  `canaryChanged` on windowed draws.

**The deeper microscope finding (b32a6) still stands:** the observed freed-hole hits
were holes freed DURING the trigger's service (guard0 3439 vs trigger end 3507, hit on
neighbor guard1) — a fresh-hole-during-fold channel. The DELAY=0 variant instead tests
the stale-hole channel (free at 0.3 ms, fold at 2.5). If stale holes get reused by
earlier same-bin allocations (likely), the aim design needs the fresh-hole form: the
sacrifice completing DURING the trigger's service — which, per the drain-order law
(round-13: drain follows client slot), means an EARLY client slot + deferral, a
bigger redesign. Run DELAY=0 first (one boot), then judge the whole route.### Boot 39 (2026-11-05, 15:38) — DELAY=0 invalid TOO: t=0 sends still defer; the delay knob is dead; drain-order design queued

`CIV_SACRIFICE_DELAY=0` variant: 6/6 clean, landings guard0×2 + filler1 (all non-windowed),
**canary 0/6**. The decisive row — **sacrifice0 sent at 0.0 µs serviced at 3408 µs**
(after the trigger's 2823): even a t=0 send queues behind the hole's read. The wave's
service is monopolized from its first moments; **the "fast miss reply" class does not
exist inside a wave for a pathPacket, at any delay.** B38 (1.2 ms → 3579) + B39 (0 →
3408) close the delay route entirely.

**What remains is the drain-order route — and the evidence for it is b32a6:** guard0
(slot 10) SERVICED during the trigger's window (end 3439 < trigger end 3507) → its copy
freed DURING the fold → the hit landed on guard1 — the freed role's *drain-adjacent
neighbor*. The fresh-hole channel is real but it is a **drain-order event**, not a
timing event: whichever role's slot services just before/while the trigger resolves
produces the hole, and the hit lands near it.

**The drain-order design (queued, one boot):**

```
Sacrifice takes slot 10 (guard0's handle — identical saved clones, round-13-proven
swap); guard0 takes slot 13. Post-CANARY_LAST drain order at 6/10/11/12:
guard1(6) → sacrifice(10) → canary(11) → trigger(12).
```

The sacrifice services **immediately before the canary's own dispatch** → its
0x7f1-class copy frees → the canary's live copy allocates **into the just-freed hole**
(LIFO) → the fold's freed-hole channel hits **the canary's own region inside windowed
draws**. This is the b32a6 geometry with the fresh hole deliberately placed adjacent to
the canary instead of landing wherever the drain lottery puts it.

**Honest caveat:** b32a6's hit followed guard0@10 → guard1 whose handle sat at slot *6*
post-swap — one event, ambiguous adjacency (client-slot vs allocation-order). The
design tests the strongest form (drain-adjacent); if the hit pattern under the design
still lands elsewhere, the adjacency model refines from the new events. One boot, and
the failure mode is informative either way.

**Also recorded: the sacrifice defaults need updating if this works** —
`CIV_SACRIFICE_DELAY` becomes moot (the slot does the work), and the sacrifice's send
should ride the guard delay class (2.55 ms) so it defers into the drain batch.### Boot 40 (2026-11-05, 15:43) — SLOT_SWAP null + the STRUCTURAL INSIGHT: windowed draws have no holes at fold time; Stage A rides the live-copy channel

`CIV_SACRIFICE=1` + `CIV_SLOT_SWAP=10,13` (sacrifice rides the slot-10 handle): **6/6
windowed (highest window rate of the campaign), 0 landings anywhere, 0 canary.** The
sacrifice's service: sent 1015 → **serviced 5105 µs** — still AFTER the trigger's fold
(2470→2634). The handle swap did not move its service position (the round-13
drain-follows-handle law does not hold for service-end order in this configuration —
that law was measured on drain-order of the deferred batch, and the sacrifice stays in
the batch). Also corrected this session: a CIV_SACRIFICE_DRAIN roles-list swap was
implemented, then **reverted before any boot** — the round-13 evidence (handle swap
moved the canary's drain) shows handle-swap and roles-swap are equivalent, and the
existing SLOT_SWAP already does what the new knob would.

**The structural insight (the real output of boots 38–40):** on a windowed draw the
trigger is FAST (130–870 µs) and resolves **before the queue drains** — the deferred
batch services at 3400–6000+, all after the fold. **A freed hole cannot exist at fold
time on a windowed draw.** Therefore:

- The ~85% freed-hole landing channel (non-windowed draws) is **structurally
  unavailable to Stage A** — it needs a deferred trigger, which breaks the window.
- **Stage A's channel is the live-copy scatter**: the ~4%/windowed-draw rate of the
  aliased write hitting a LIVE protected copy adjacent to its target. The two windowed
  hits of the campaign (r37a2 canary, b36d3 filler1) came from this channel.
- The deliberate-hole design family (delay, drain-order, slot swaps) **cannot raise
  Stage A's rate** — all of them aim the freed-hole channel, which only fires when the
  window is already lost. The design line is closed, honestly: 3 boots, 3 nulls, all
  explained by one law.

**Stage A's true options (the fork):**

1. **Resume the plain lottery** (r37 staging, 6-marker chain, NO sacrifice — the extra
   role adds nothing now): the live-copy channel hit canary once in ~50 windowed draws
   (r37a2 — the qualification exists and is reproducible machinery). Expected ~15–20%
   per boot; each landing → Stage B in the same payload. The honest cost: hours of
   relaunches at ~17% odds.
2. **SM3 offline, resumed as primary:** the live-copy scatter's geometry (which live
   copy the fold's write lands on) is a planner question — model the FS-side copy
   layout from the two observed windowed hits + the 12 freed-hole events' residues.
   No boots; produces either a re-staging prediction (one validation boot) or the
   conclusion that only volume wins.
3. (Diminishing) **ns2jb-diff archaeology:** SmileBASIC's wave landed reliably —
   diffing their staging geometry vs ours on the live-copy channel may expose the
   structural difference (heap era, session age, their tmp-mount trigger). Offline
   only.

**Recommendation:** record, then **pause boots** — options 2+3 are offline and the
lottery's expected cost (~6 boots/landing) is high against two unmined offline
threads. The console rests; the vault carries the full state.### SM3 + ns2jb-diff dig (2026-11-05, ~16:00) — THE MISSING STEP FOUND: the per-wave 16 MB dirty write

**ns2jb-diff findings (double-back, probe-reserved-gap.cjs read in full):**

1. **The commit_data dirty write — per-wave heap shaping our port NEVER runs** (tracker
   todo #4, queued since round-18b):
   - Setup (once): `normal(saved, 0, scratch + '/commit_data', u64(0) + u64(0x1000000))`
     — **CreateFile (cmd 0) with a 16 MB size argument**.
   - **EVERY wave** (`:333-338`): OpenFile (cmd 8, mode `{3,0,0,0}`) → **Write 16 MB
     dirty** (cmd 1 on the file handle, `u64(0)+u64(0)+u64(writeSize)`, send descriptor
     over a 16 MB source buffer) → Close — *then* the 13-role wave.
   - Their stated purpose: "Dirty gap commit data" — the commit role's `packet(10)`
     has real flush work. Heap consequence: the FS's journal/allocator handles 16 MB of
     dirty data before every wave — **a heap-shaping step that changes the regime the
     fold's aliased write lands in.**
   - Their source: `writeData = b + 0x1000000` (pre-mapped), memset 0x66 once. Ours
     would be a console `alloc pages=4096` (content irrelevant — journal dirtied
     regardless; `packet()` already supports send descriptors).
2. **Commit packet deviation:** theirs is bare `packet(10)`; ours adds
   `u64(0), send=[commit path text]` — a likely compensation for the empty journal.
   With the dirty write ported, the faithful bare form restores.
3. **Staging layout:** theirs = fixed offsets in one pre-mapped region (canary
   b+0x40000, guards b+0x42000+i·4096, marker16 b+0x45000, page_absent b+0x46000);
   ours = fresh console-alloc pages per path. Payload-side difference; FS-side copies
   are what the landing targets, but the descriptor reads differ too.

**SM3 status (live-copy channel model):** Stage A's channel is the live-copy scatter
(~4%/windowed-draw, 2 events: r37a2 canary, b36d3 filler1; r37a2 was a mid-class 874 µs
trigger vs r35a1's fast 259 µs miss — a possible trigger-class correlation at n=2, too
weak to use). **The modeling is BLOCKED on a regime confound: every landing
observation this campaign made in a heap regime missing the reference's per-wave
16 MB dirty write.** Further rate modeling before the port is premature — the dirty
write may shift the entire distribution (that is plausibly WHY SmileBASIC's waves
land where ns2jb's planner expects and ours scatter).

### Queued implementation (the dig's actionable output — closes todo #4)

`CIV_COMMIT_DATA=1` (default off) in `host.py`, ns2jb-faithful:
- setup(): CreateFile `scratch/commit_data` at 16 MB (`path_send(fh, 0, …,
  u64(0)+u64(0x1000000))` — staged path entry, one-time per payload).
- worker_wave(): per wave before the 13-role launch — OpenFile (cmd 8, mode 3) →
  Write (cmd 1, offset 0, size 16 MB, send=[16 MB console-alloc buffer, cached per
  payload]) → Close (control 2).
- Offline verification: compile + wire selftests unchanged (default mode) + a fake-
  console packet-geometry check (mode bytes, descriptor layout vs the .cjs bytes).
- **Validation boot: frozen 6-marker chain + `CIV_COMMIT_DATA=1`** — watch the
  landing distribution AND the commit role's window (does commit still span?);
  ≥1 windowed canary landing ⇒ the regime was the missing ingredient; a shifted
  landing pattern (new slots) ⇒ the model refines; no shift ⇒ the dirty write is
  window-only machinery and SM3's layout model proceeds on the old data.### CIV_COMMIT_DATA implemented + offline-verified (2026-11-05, ~16:15) — todo #4 closed, pre-boot

The round-57 queued implementation landed in `host.py` (env-gated `CIV_COMMIT_DATA=1`,
default off — wire selftests identical):

1. **Constant** `COMMIT_WRITE_SIZE` (default `0x1000000`, `CIV_COMMIT_DATA_SIZE` overrides)
   — ns2jb `writeSize` (`:46`).
2. **setup()**: under the flag, stages `commit_data` (`scratch/commit_data`) and
   CreateFiles it once per payload at 16 MB — `path_send(fh, 0, …, u64(0) +
   u64(0x1000000))`, faithful to `normal(saved, 0, filename, u64(0)+u64(writeSize))` (`:208`).
3. **`dirty_commit_data()`**: per-wave — OpenFile (cmd 8, mode `[3,0,0,0]`), Write
   (cmd 1, input `u64(0)+u64(0)+u64(16MB)`, send descriptor over a 16 MB console-alloc
   buffer cached per payload), CMIF Close (`control(2)`, the leak_read pattern).
   Called from `worker_wave` BEFORE the 13-role launch (`:333-338` order).
4. **Commit packet NOT changed** (one-variable discipline): our `packet(10, u64(0),
   send=[commit])` stays for this validation; ns2jb's bare `packet(10)` restores only
   after the dirty write validates (the diff record's consequence).

**Offline verification (all pass):** compile ✓; wire selftests unchanged ✓; fake-console
packet geometry — OpenFile cmd 8 input `03 00 00 00` ✓; Write packet byte-identical to
`packet(1, u64(0)+u64(0)+u64(0x1000000), send=[{16MB, mode 1}])` ✓; fields cmd 1 /
offset 0 / size 0x1000000 ✓; Close == `control(2)` ✓; buffer alloc pages=4096 cached in
state ✓.

**Validation boot (driving now):** the longest-running frozen 6-marker chain (r37
staging — `CIV_CANARY_SIZE=0x5a0`, NO sacrifice/stage/slot swap) + `CIV_COMMIT_DATA=1`
as the ONLY variable. Watch: (a) the 16 MB alloc survives (a refusal is the clean
first signal), (b) the dirty-write asserts pass per draw, (c) the window still holds
(commit's flush work changes — watch its span), (d) **the landing distribution** —
≥1 windowed canary landing ⇒ the regime was the missing ingredient (chain resumes
B/C/D); landings shifting slots ⇒ the model refines; no shift ⇒ the dirty write is
window-only machinery and SM3 proceeds on the old data.### Boot 41 attempt (2026-11-05, ~16:20) — NEVER SAMPLED: loader wedge on drive (game session not fresh)

The validation drive never reached a wave: the relocation payload load was **refused**
(`:9028` never bound), `mounts` printed nothing, console.js "sent" but never bound
`:9029` (the Harness `Console.__init__` connect refused, then went black-hole).
Ports after: `:9026` OPEN (loader answers — **wedge, not freeze**, per the boot-13
discriminator), `:9028`/`:9029` timeout (dead sockets).

**Reading: the game session still carried boot-40's evaluated console.js payload** —
the documented "loader evaluates one payload per game session" rule; it refuses every
further load (relocation included) until a game relaunch. Either the relaunch did not
happen before this drive, or it did not re-run the stager (Load Game on the
`XSTRIG.Civ7Save` save). No code failure: the CIV_COMMIT_DATA implementation passed all
offline checks minutes before, and the validation command is unchanged and queued.

**Required before the next drive:** launch the game → **Load Game** on the save (runs
the stager, brings up a fresh loader) → confirm the loader answers → re-drive the same
command. If the loader still refuses after a confirmed relaunch, power-cycle per the
boot-13 discriminator (loader silent = freeze).### Boot 41b + the fallback fix (2026-11-05, ~16:25) — first live dirty-write signal: 16 MB single-shot REJECTED; chunked fallback implemented

The retry validation boot ran the full flow on the fresh session (mounts OK, canary
chain 62/62, setup CreateFile OK) and gave the **first live signal on the dirty write**:

- **OpenFile (cmd 8, mode 3) succeeded** (moves returned, assert passed).
- **The single 16 MB Write returned `0xD2346402`** (canonical `0x346402` — no known
  family; residue-pattern high byte like the wave rows). The assert aborted the run
  before any wave (payload spent: setup reserve only).

**Offline archaeology before the fix:** `parse_reply` == ns2jb `response()` byte-for-byte
(result at SFCO+8); our Write packet == `packet(1, u64(0)+u64(0)+u64(16MB), send=[16MB
descriptor])` == their `s.ipc(...)` byte-for-byte; their transport
(`call('send', base, 4096, handle)` = sendSyncRequestWithUserBuffer) == our `sendRaw`.
**The only live variables left: the 16 MB send-descriptor SIZE (Civ's fsp-ssrv may cap
per-request send buffers — our proven transfers are ≤0x4000 receive) and the
file-handle plumbing.**

**Fallback implemented + offline-verified (`host.py` `dirty_commit_data`):**
- single 16 MB shot first (ns2jb-faithful);
- on failure: **print result + reply hex[0:64]** (the diagnostic the next boot needs),
  then **auto-fallback to `CIV_COMMIT_DATA_CHUNK` (default 1 MB) × 16 chunks** — the
  journal dirtying (the heap-shaping point) survives chunking;
- CMIF Close as before. Offline test: single-shot fails → 16 × 1 MB at offsets
  0x0..0xF00000 all pass → 16 MB dirtied → close. Compile ✓, wire selftests
  unchanged ✓.

**Validation decision tree (next boot):**
- 16 MB single-shot PASSES → it was transient; done.
- Chunks pass (expected) → the cap is per-request size; 1 MB chunks are the permanent
  form; the wave proceeds and the landing distribution under the NEW REGIME is the
  read.
- Chunks ALSO fail → the handle/plumbing dig (reply hex + GetSize-on-file controls),
  no wave.
- Console: 41b's payload spent (setup + aborted wave 0); **relaunch required** for the
  fixed validation boot.### Boot 42 (2026-11-05, ~16:10) — chunked write ALSO rejected (0x346402, FS module 2): NOT a size cap; diagnostic ladder + graceful degradation implemented

The fixed validation boot (fallback armed) ran on a fresh session: **the 1 MB chunk
failed with the SAME result** — `0xD7746402` (canonical `0x346402`, FS module 2, unknown
description), reply hex well-formed (`SFCO` at +16, result at +24 — a real CMIF error
from a live session). The chunk loop raised and burned the boot (payload spent:
setup + 1 aborted wave).

**Ruled out by the hex:** a size cap (1 MB and 16 MB rejected identically), transport
plumbing (a dead handle would not produce a parsed CMIF result), input encoding (our
Write packet is byte-identical to `s.ipc(...)` on `probe-reserved-gap.cjs:334-336`, and
`parse_reply` == their `response()`). **Remaining suspects:** the file handle's object
identity (moves-vs-copies for OpenFile on a plain IFileSystem), the write-option
semantics of Civ's IFile, or a file-attribute problem with the CreateFile'd file.

**The fix (implemented + offline-verified, `dirty_commit_data` rewritten as a ladder):**

1. GetEntryType (cmd 7) → print; OpenFile (cmd 8) → print moves AND copies counts;
2. **IFile::GetSize (cmd 0) on the handle → print result + size** — identifies the
   object (16 MB = our file ✓, garbage = handle misread → plumbing dig);
3. **Write size descent** (16 MB → 1 MB → 64 K → 4 K → 256 B) — the first working size
   dirties the full 16 MB in working-size chunks;
4. **Graceful degradation:** total failure prints the ladder, CMIF-closes, returns
   False — and `worker_wave` prints `wave regime: DIRTIED/UNDIRTIED` and **the wave
   runs anyway**. No more burned boots: the next boot yields the ladder diagnosis AND
   wave data regardless of the write's fate.

Offline: scenario 1 (64 K works) → 16 MB dirtied in 64 K chunks, returns True;
scenario 2 (all fail) → returns False, wave proceeds. Compile ✓, wire selftests
unchanged ✓. **Relaunch → re-drive: the ladder diagnoses in-run and the wave harvests
either way.**### Boot 43 (2026-11-05, ~16:14) — MY ladder bug: packet(0) on IFile is a malformed Read (GetSize is cmd 2); every ladder step made tolerant

The ladder boot crashed at ITS OWN step 2: `packet(0)` on the file handle is IFile
**Read** (IFile layout: Read=0, Write=1, GetSize=2 — NOT IStorage's GetSize=cmd 4 that
`leak_read` uses), so the probe sent a malformed Read and the reply was unparseable →
`Missing SFCO reply` assert → boot burned (reserve had completed; payload spent its
setup cycle — relaunch required).

**The fix (implemented + offline-verified):**
- GetSize probe now `packet(2)` (IFile GetSize);
- **every ladder step wrapped in try/except** — an unparseable reply records
  `"no-SFCO (AssertionError)"` in the diag dict instead of raising; the ladder
  continues to the next size and the wave runs regardless. Boot-43's crash class is
  now structurally impossible.
- Offline scenarios: unparseable GetSize tolerated ✓ (cmd 2 attempted, no raise);
  Write descent finds the working size (4K in the test) → full 16 MB dirtied in
  working-size chunks → returns True ✓; all-fail → returns False + wave proceeds ✓.
  Compile ✓, wire selftests unchanged ✓.

**Boot-44 read (queued):** the ladder now yields, per draw, the full diagnosis
(GetEntryType / open moves+copies / GetSize cmd-2 / write size descent) AND the wave —
either DIRTIED (some size works → the new heap regime) or UNDIRTIED (graceful False →
regime unchanged, plain lottery draws). Both outcomes are informative; neither burns
the boot.### Boot 44 (2026-11-05, ~16:29) — LADDER DELIVERED: both bugs found and fixed (SetSize + domain Close)

The tolerant ladder ran end-to-end without a crash: 6 draws, all degraded-undirtied,
one filler1 landing (d2). The diagnosis (draw 1's full ladder + draws 2-6):

**Bug 1 — the file is EMPTY (`fileSize: 0`):** the CreateFiled file measures 0 bytes,
so **every Write is out-of-range at every size** (0xE9FB6402-family ≈ canonical
0x346402 — the write-descent confirmed: 16 MB, 1 MB, 64 K, 4 K, 256 B all rejected).
ns2jb's identical CreateFile bytes evidently land differently on Civ (their Write
succeeded on the same form) — the form question is real but sidesteppable.

**Bug 2 — my close was a CLONE:** `control(2)` on the file handle is **CloneSession**
in the domain protocol (that is what `clone()` uses), NOT Close. Draw 1 "closed" via
control(2) → cloned+leaked the file → draws 2-6's OpenFile failed **`3586 (0xE02)`**
(already-opened family) → moves 0 → graceful False. Six leaked clones on the payload.

**Fixes (implemented + offline-verified):**
1. **`SetSize` (IFile cmd 4, `u64(COMMIT_WRITE_SIZE)`) after the GetSize probe** when
   the file is short — extends the file to 16 MB so the Writes fit in range; sidesteps
   the CreateFile-form question entirely. Ladder prints `setSize` in the diag.
2. **Both close sites → `close_object(file_handle, 3)` sent on `fh`** (the owner
   domain session) — the release role's own domain-Close form; control(2) removed
   entirely.
3. Offline: the ladder flow verifies end-to-end (`getSize 0 → setSize 0 →
   write0x1000000: 0` single-shot, close_object exercised, returns True); compile ✓;
   wire selftests unchanged ✓.

**Boot-45 read (queued):** the fixes ride the same ladder — expect DIRTIED
(setSize + single-shot write) and then **the first wave under the reference's heap
regime**; watch the landing distribution vs the old regime's filler/guard scatter,
and the window (commit's flush work now real). If SetSize fails (result ≠ 0), the
ladder prints it and the wave runs undirtied — still no burned boot.### Boot 45 (2026-11-05, ~16:35) — SetSize was FLUSH (cmd 4): SetSize is cmd 3; close-on-fh broke fh — both fixed

Boot 45 ran the tolerant ladder and printed the decisive diagnostic before its wave
crashed:

- **`setSize: 0` was a LIE — IFile cmd 4 is FLUSH, not SetSize.** The IFile layout is
  Read=0/Write=1/GetSize=2/**SetSize=3**/Flush=4. My cmd-4 probe successfully
  *flushed* a 0-byte file (a no-op returning 0), the file stayed 0 bytes, and every
  Write in the descent still returned `0xFA746402` (canonical 0x346402 — the same
  out-of-range family as boots 41/42, now fully explained: empty file).
- **Draw 1's wave crashed at `_clone_handle(fh, "saved-trigger")`** — my boot-44
  "close fix" sent `close_object(file_handle, 3)` on **fh ITSELF**, which corrupted
  the save-FS session (the next CloneSession on fh returned an unparseable reply).
  The release role's identical form works because it rides a **CLONE** of fh
  (`clients[9]`) — never fh directly. Payload spent (fh corrupted).

**Fixes (implemented + offline-verified):**
1. **SetSize now cmd 3** (`packet(3, u64(16MB))`) — the flush no-op is gone; the file
   extends to 16 MB so the Writes fit range.
2. **Both close sites → a cached per-payload `commit-close` CLONE of fh**
   (`state["commit_close_session"]`), `close_object(file_handle, 3)` sent on the
   clone with `domain_reply=True` — the release role's exact form-on-clone. fh is
   never touched by the close.
3. Offline: full ladder flow verified — `getSize 0 → setSize(cmd 3) 0 → write
   single-shot 16MB 0` → close_object on the clone (fh untouched) → returns True.
   Compile ✓, wire selftests unchanged ✓.

**Boot 46 read (queued):** with the file actually 16 MB, expect **DIRTIED** on draw 1
(single-shot write), the file persists per payload (draw 2+ reads 16 MB from GetSize,
no SetSize needed), and **the first waves of the campaign under the reference's heap
regime** — the landing distribution vs the old filler/guard scatter is the actual
round-57 question. The regime print labels every draw either way.### Final-form dirty write (2026-11-05, ~17:00) — libnx ground truth applied; boot 41–46 archaeology closed

**The definitive corrections (libnx `fs.c` + console.js read live):**

1. **IFile layout (libnx ground truth): Read=0, Write=1, Flush=2, SetSize=3, GetSize=4**
   — trailing layout identical to IStorage (where leak_read's live GetSize = cmd 4).
   My cmd-2 GetSize probe was a Flush; "fileSize 0" in boots 44/46 was a Flush reply's
   zero tail — **the empty-file theory was built on garbage**. Boot 46's SetSize(3)
   actually succeeded and the writes still failed → **the write rejection is
   invariant to file size**.
2. **The close is ns2jb's raw svcCloseHandle**: their `s.close(handle)` =
   `call('close', handle, 0, 0)` — the SDK stub — reachable host-side as
   `console.request("svc", name="closeHandle", args=[handle])` (the stub is already
   relocated and used live by console.js for events). My `close_object(file_handle)`
   closes DOMAIN OBJECT IDS — a raw OpenFile session handle is not one — which is
   why it corrupted fh (b45: the next CloneSession on fh died) and leaked the file
   (b44/46: draws 2–6 OpenFile failed 0xE02).

**Final ladder (`dirty_commit_data` v4):** GetEntryType → OpenFile(mode 3) →
**GetSize(4) real** → SetSize(3) + **GetSize(4) re-verify** → **Write descent across
descriptor modes (m1/m3/m0 × 1MB/64K/4K/256B)** → **open-mode probe (mode 2)** → raw
close on every path → graceful False. Offline: success path (64K mode-1 works → full
16 MB dirtied → True) ✓; total failure (all modes/sizes + mode-2 open → False, both
handles closed) ✓. Compile ✓, wire selftests unchanged ✓.

**What boot 47 decides:** with the real GetSize(4), we learn the CreateFile'd file's
actual size (was it 0 or 16MB all along?); the mode descent tests the last untested
packet variable; if some (mode, size) dirties, the regime lands and **the waves run
under the reference's heap regime** — the actual round-57 question. If everything
fails, the printed ladder (11 combos + mode-2 open) is the complete diagnostic for
the fs-team question file. Either way: no burned boot, six draws, full data.### Boot 47 (2026-11-05, ~16:56) — ★ DIRTIED ×6: the write WORKS (1MB mode-1); full archaeology closed; loop-count bug found and fixed

The final ladder ran on a fresh boot and **dirtied every wave** — six × `wave regime:
commit_data DIRTIED (ns2jb-faithful)`, 6/6 draws clean, 5/6 windowed, one guard0
landing (d5), canary 0/6.

**The complete boot-41→47 archaeology (all questions now answered):**

| question | answer |
| --- | --- |
| The file's real size | **16 MB** (`fileSize: 16777216` via the real GetSize(4)) — **never empty**; my cmd-2 Flush probe was garbage and the empty-file theory was wrong |
| The 16 MB single-shot | **Rejected AND session-poisoning**: boot 42's 1 MB write failed ONLY after the 16 MB descriptor failed on the same handle; boot 47 (descent without the 16 MB form) → **1 MB mode-1 PASSES** |
| Working form | **OpenFile(mode 3) → 1 MB mode-1 writes × 16** (offset 0..0xF00000) → raw `svc closeHandle` (ns2jb's own s.close form) |
| SetSize | Unneeded (file is 16 MB from CreateFile) — the ladder's conditional probe confirms per-draw |

**Boot-47 loop-count bug (found in my own print, fixed pre-next-boot):** the 1 MB
probe's success set `dirtied = COMMIT_WRITE_SIZE` without writing the remaining
15 chunks — **every "DIRTIED 16MB" wave this boot actually dirtied ONE 1 MB.**
The waves ran under a 1/16-strength regime, not the reference's. Fixed (chunk loop
from the probe's write onward: 16 contiguous 1 MB writes = exactly 16 MB,
offline-verified; compile ✓, wire selftests unchanged ✓).

**Waves data (partially-dirtied regime, 1 MB/wave):** 5/6 windowed, landings: guard0
×1, canary 0 — indistinguishable from the old regime so far; the landing question
re-reads at full strength next boot.

### Next boot (queued): the TRUE 16 MB regime

The fixed loop + same command. Each wave: GetSize(4)=16MB → 16×1MB contiguous
writes → svc close → 13 roles. **This is the reference's actual per-wave heap
regime** — the round-57 question finally answered: does the landing distribution
(canary ~4%/windowed-draw) shift under the dirty regime? Then Stage A's odds are
known honestly, and either the lottery resumes informed or the SM3 layout model
gets its data.### Boot 48 (2026-11-05, ~17:02) — ★ FULL-STRENGTH DIRTY REGIME LIVE: 6/6 DIRTIED, first honest-regime harvest

The fixed loop ran the full 16 MB per wave (`dirtied 0x1000000 (mode 1, 0x100000
chunks)` = 16 contiguous 1 MB writes), 6/6 waves DIRTIED, console healthy, 6/6 draws
clean.

**First full-regime data:** 4/6 windowed, landings guard1 (d2) + filler0 (d6), **canary
0/6** — distributionally indistinguishable from the old regime so far (fillers/guards
take the landings; the canary's windowed-hit channel unchanged at n=1 boot).

### THE MACHINERY ARC IS COMPLETE (rounds 57–58)

Everything ns2jb does per-wave, the port now does:

| ns2jb step | port status |
| --- | --- |
| CreateFile commit_data 16 MB | ✓ (setup; `fileSize` = 16 MB verified via real GetSize(4)) |
| Per-wave OpenFile(mode 3) → 16 MB dirty Write → Close | ✓ (16 × 1 MB mode-1 chunks — the 16 MB single-shot poisons the session; raw svc closeHandle — ns2jb's own form) |
| 13-role wave, gates, canary chain, client/session caches | ✓ (proven rounds 8–47) |
| Retry loop per-profile | ✓ (round 36) |
| Stage B/C/D scaffolding | ✓ (offline-verified, waiting on Stage A) |

**The remaining Stage A question is pure statistics**: ~4%/windowed-draw for the
canary's live-copy channel, harvestable at 4–6 draws/boot under the reference's own
regime. Every future lottery boot is also a regime-rate measurement — the two
questions (Stage A landing, dirty-regime rate shift) are the same data.

**Standing plan:** resume the informed lottery on demand (user relaunch per boot,
~3 min each, ~17–20%/boot at the old-regime rate; if the dirty regime shifts the rate
upward, the boot history itself reveals it within a few boots). The first canary
landing under the dirty regime runs Stage B's prime assert in the same payload —
and Stage B/C/D are otherwise complete. No further implementation work is pending.### Boot 50 (2026-11-05, ~17:20) — accidental UNDIRTIED control: setup CreateFile failed silently; ladder self-heal added

Boot 50 ran 6/6 windowed (best window rate), **zero landings**, and — the finding —
**UNDIRTIED ×6**: the ladder showed `getEntryType: 0x202` on every draw — the
commit_data FILE did not exist. The setup-level CreateFile (`results
["commitDataCreate"]`) failed silently (recorded, never checked); boots 48/49 created
it fine, so the failure is intermittent (FS state at setup time — unchecked result,
exact value unknown this boot).

**Boot 50's accidental value:** an undirtied control run under the same command
shape — 6 windowed draws, 0 landings, consistent with the old regime's ~70%/draw
nowhere rate. Boots 48/49 (DIRTIED) landed on held paths 4/12 draws. Weak signal,
right direction: the dirty regime may land MORE.

**Self-heal added (offline-verified):** `dirty_commit_data` step 0 now CreateFiles
the file (cmd 0 + 16 MB size arg) and re-probes when `getEntryType != 0` — prints
`ladder recreate=<result>`. The setup CreateFile failure no longer degrades a whole
boot; each draw heals independently. Ladder print from the offline run:
`was missing → recreate=0 → fileSize 16777216 → write0x100000_m1: 0 → dirtied 16MB`.

**Boot 51 (queued):** same command — expect DIRTIED ×6 (healed or setup-created),
window 4–6/6, and the landing tally continues. Session so far (48–50):
DIRTIED draws 12 (48/49) + UNDIRTIED 6 (50); landings: 48 → guard1, filler0;
49 → guard0, filler1; 50 → none. Canary 0/18.### Boot 51 (2026-11-05, ~17:26) — ROOT CAUSE: 0x3C00 OutOfSpace — the 16 MB file CANNOT fit Civ's save; size scaled to the free margin

The self-heal did its job on the first run: **`recreate: 15360 = 0x3C00` —
OutOfSpace.** The commit_data file cannot be created: **Civ VII's save filesystem
(~62.6 MB free, verified rounds ago) cannot hold a 16 MB file.** Every boot-51 draw
degraded (6 × UNDIRTIED, graceful as designed); landings guard0 ×2.

**Design-correct reading of the reference:** ns2jb runs on a **16+ GB save-data
target** (SmileBASIC 4) — their `writeSize = 0x1000000` is ~0.1% of that volume. The
faithful port is not the absolute 16 MB: it is **"a file sized to the save's free
margin, re-dirtied every wave"** — the journal/dirtying *work* is the heap-shaping
point, not the byte count. (Also: boots 48/49 DIRTIED 6 × 16 MB = ~100 MB each against
62 MB free — the first two DIRTIED boots only worked because the file already existed
and the writes dirtied pages in place; creation is what needed the space. The
repeated SetSize(16MB) verified file, writes passing at 16 MB, and the FS absorbing
100+ MB of journaling strongly suggests Civ's save FS is a ~62 MB pool with heavy
dedup/CoW — the exact regime question the dig flagged.)

**Fix (implemented):** `COMMIT_WRITE_SIZE` default **16 MB → 1 MB** (`0x100000`,
~1.6% of the 62 MB free margin — 6 waves × 1 MB = 6 MB per boot, safe). The self-heal
+ size descent remain: if 1 MB also OutOfSpaces on some boot, the descent tries 64 K/
4 K/256 B. Compile ✓. **A full 1 MB-per-wave DIRTIED regime is the new baseline; the
boot-48/49 16 MB DIRTIED draws (landings 4/12) remain valid data at the 16 MB point.**

**Boot 52 (queued):** same command, 1 MB regime — expect DIRTIED ×6 (setup CreateFile
fits comfortably), window, landings; the lottery continues under the space-correct
regime. If landing rates at 1 MB match 16 MB (≈4%/draw somewhere), the dirty-regime
question closes honestly at both points.### Boot 52 (2026-11-05, ~17:32) — ★ 1 MB regime STABLE: 6/6 DIRTIED; the lottery baseline is final

The space-correct 1 MB `COMMIT_WRITE_SIZE`: **DIRTIED ×6, zero degrades, zero
OutOfSpace.** 5/6 windowed (best sustained rate), landings: filler1 (d4), **canary
0/6**. Console healthy.

**The lottery session (boots 48–52, 24 draws under the dirty-write machinery):**

| boot | regime | windowed | landings | canary |
| --- | --- | --- | --- | --- |
| 48 | DIRTIED 16 MB ×6 | 4/6 | guard1, filler0 | 0 |
| 49 | DIRTIED 16 MB ×6 | 4/6 | guard0, filler1 | 0 |
| 50 | UNDIRTIED (control) | 6/6 | none | 0 |
| 51 | UNDIRTIED (control) | 4/6 | guard0 ×2 | 0 |
| 52 | **DIRTIED 1 MB ×6** | 5/6 | filler1 | 0 |

- **The dirty regime's landing rate (16 MB: 4/12 draws; 1 MB: 2/6) vs undirtied
  control (2/12)** — no detectable shift at this sample size; the regime question is
  answered "no dramatic rate change", and Stage A remains ~4%/windowed-draw wherever
  the landings land.
- **Session verdict: 24 draws, 0 canary.** Combined campaign: ~90 draws, canary 1
  (r37a2). The lottery is honest and slow; the machinery is complete and stable.

### SESSION CLOSE (2026-11-05, 17:35) — state for the next session

**The machinery arc is done.** Every ns2jb per-wave step is ported, verified, and
stable: CreateFile (sized to the free margin), per-wave 1 MB dirty write, raw svc
close, 13-role wave, gates, chains, caches, per-profile retry loop, B/C/D scaffolding.
The dirty regime runs 6/6 with zero degradation. **Stage A is the sole remaining gate**
and it is pure statistics: ~4%/windowed-draw, 4–6 draws/boot, ~17–20%/boot.

**Next session starts with:** relaunch → the frozen command (below) → lottery
harvest. First canary landing runs Stage B's prime assert in the same payload
(`prime.bytes == control prefix` — the cache bucket table inside the freed slot), and
B/C/D are otherwise complete.

```
CIV_ALLOW_OVERWRITE=1 CIV_PREREQ_SKIP=1 CIV_STAGE0_SKIP=1 CIV_ALIAS_UPTO=prime
CIV_RELEASE_CLOSE_GATE= CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340,0x2340,0x2340,0x2340
CIV_ALIAS_MARKERS_MAX=6 CIV_COMMIT_DATA=1 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0
CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000 CIV_NORMALIZED_PATHS=1
CIV_CANARY_GATED=1 CIV_CANARY_OFFSET_NS=40000 CIV_CANARY_SIZE=0x5a0
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 CIV_GUARD_DELAY=2550000
CIV_GUARD_SPREAD=0 CIV_FILLER_DELAYS=2480000,2300000
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 CIV_TRIGGER_TAIL=100000
  python leak-cycle.py full
```

**Dead ends recorded (do not revisit):** 16 MB commit file (0x3C00 on Civ's save);
16 MB single-shot writes (rejected + session-poisoning); close_object on raw session
handles (corrupts fh); IFile GetSize≠cmd 2 (that's Flush — GetSize=4, SetSize=3);
the freed-hole aim family (windowed draws have no holes at fold time — rounds 52–56);
client-slot/staged-page swaps (uniform scatter); SM3 rate modeling before more
DIRTIED draws exist.### Boot 53 (2026-11-05, ~17:52) — 5 DIRTIED draws (guard1 landing), canary 0/5; payload-fatal on draw 6

The last roll of the session (the frozen command): **draws 1–5 DIRTIED, 3–4 windowed,
one landing (guard1, d2), canary 0/5**; draw 6 died mid-readback (`host.py:1963`
family — the payload-fatal class, ~4/31 stage-0-free draws ≈ 13%). Game alive after
(ports accepting); relaunch clears.

**Lottery session final (boots 48–53): 29 draws, 0 canary.** Campaign total ~95
draws, canary 1 (r37a2). All machinery stable at the 1 MB regime (11/11 DIRTIED where
the file existed; 12/12 with boot 52). The wall stands: ~17–20%/boot, ~5–6 relaunches
per expected Stage A landing; a landing auto-runs Stage B (`candidate primed`).
State docs: [[handoff-v2]] (entry) + [[project-status]] — created this session and
current through boot 53.