# CivSwitch tracker

Living status for the Civ VII → homebrew pipeline port (ns2jb's filesystem-breakout, re-pointed at
the game process). Update this file as phases complete; `Work log.md` keeps the chronological detail
and this file keeps the state.

**Goal:** launch homebrew on a stock Switch 2 via the Civilization VII chat-XSS entry point, matching
what SmileBASIC 4 achieved, by porting ns2jb's filesystem-breakout pipeline into the Civ VII game
process.

**Console:** `192.168.103.215`. Loader `:9026`, relocation `:9028`, harness `:9029`
(`CIV_HARNESS_PORT`). Server IP and ports are the only fixed infrastructure.

---

## 1. Completed and working (verified live)

### Toolchain and relocation
- [x] SVC stub decoder — 60 stubs decoded (`relocation/svc-relocation.json`).
- [x] SDK signature resolution (`build-signatures.cjs`, `sdk-resolved.json`).
- [x] `getService` via the SDK (`+0x14cc90`), `fsp-srv` obtained; privileged services denied `0x1015`.
- [x] `sdk_base = read64(main + 0x4DC6088) − 0x5224E0`; `memcpy = sdk_base + 0x5224E0`
      (confirmed live by `guard-test`).

### Filesystem access (M3)
- [x] Path-capable save `IFileSystem` in the game process (mount chain + `CopyObject`).
- [x] `GetFreeSpace` verified (~62.6 MB).
- [x] Reservation group of 59 objects; measured cost 12160–12176 (firmware difference from ns2jb's
      `59×208 = 12272`; ±1 object crashes, so 59 is hypersensitive but stable).
- [x] Mount accessor rediscovered per boot (`relocate_host.py mounts` → `mount-accessor.txt`);
      `discover_mount` itself faults, so the harness reads the file.

### Harness
- [x] `console.js` payload with `readFrame` (frame cap `0x8000` JSON ≈ `0x3f00` raw → write frames in
      `~0x3800` chunks; a 23-page frame needs `0x17000`).
- [x] `collect` polls instead of `waitSynchronization` (that stub writes `*(x0)`, so passing an event
      handle corrupted memory).
- [x] `postRaw` args in libnx order `(out, session, buffer, size)` (was wrong → `0x10801`).
- [x] `setup` / `mounted_save` reusable within a boot; only a stale accessor is fatal.
- [x] `accept` out-params allocated once (a single failure previously poisoned every later accept).
- [x] `flow.py` for the relocate load → mounts → stop → harness load sequence.

### Native worker port (solved)
- [x] Direct-SVC thread route: `svcCreateThread(out, entry, arg, stack_top, prio, cpuid)` at
      `+0x168b2c` **plus** `startThread` (`svc #0x09`, `+0x168b44`) — `CreateThread` alone leaves the
      thread created-but-not-running.
- [x] cpuid pinning at creation (`cpuid = i % 4`) tightens timing ~10×; runtime affinity SVCs
      (`0x10/0x11/0x12`) are a dead end (`0xE401`).
- [x] `worker-minimal`, `worker-marker`, `worker-direct` all verified.
- [x] Frame fix: Civ VII `call4` is `ldr x8,[x8,#0x70]` (ns2jb uses `#0x68`) → callee at `table+0x80`;
      `frames.py CALL4_FN_OFFSET=0x70`; dynamic-arg guard fixed.
- [x] `guard-test` (abort guard), `gate-test`/`gate-worker` (event cluster: initialize `+0x1324E0`,
      finalize `+0x132510`, signal `+0x132520`, clear `+0x132640`, wait `+0x132680`,
      tryWait `+0x1327E0`), `worker-send` (worker FS IPC → valid reply).
- [x] `fold-workers` — **proves the fold is a concurrency requirement**: racing a `1002` seed (root
      session) with a `7` lookup (save FS) from native threads selects the marker; sequential is
      refused `0xE401`. Root cause of earlier failures: `allPending` was half-checked.

### Wave (`worker-wave <profile>`)
- [x] 13-role faithful port (control page with master/holeDone/released/guard gates + flags; ns2jb
      role delays; ns2jb client list).
- [x] `abort-control` and `abort-hole-control` **fully qualified**: commit `0`, hole `0`, tails
      `0x320002`, canary `0`, fillers/guards `0x202`, trigger `0x2EEA02`, `nativeGuard {1,1,1}`,
      `triggerAborted true`, full packet fidelity.
- [x] Packet fidelity: `commit` = `packet(10)` no descriptors; `storage` = plain `packet(200)` on the
      root session; guard packet = two guard-path descriptors of size `0x301`; ctx3 = {commit,
      tail0..3, release}.
- [x] `_stage_extent_ns2jb`: one `extent+1` buffer split into five `0x800` pieces (trigger cmd `7`
      piece 0, tails cmd `1002` pieces 1–4) with a save-FS fallback for the trigger.
- [x] Timing extraction via `frames.py proof["functions"]` (locates the IPC send by address).
- [x] Marker basename at ns2jb's documented `MARKER_OFFSET = 0x2340`.

### Gate definitions (all verified against ns2jb source)
- [x] `allPending` matches ns2jb exactly — the five held paths (`canary`, `filler0`, `filler1`,
      `guard0`, `guard1`) plus tails and `commit`, each requiring
      `r.startUs < trigger.startUs && trigger.endUs < r.endUs`. Same tick-pair timing extraction.
- [x] `!triggerAborted` (guard 0/0/0), `receiveOrderMarginUs >= 300`,
      `preReleaseUsed >= 0x2600` (9728), `restored` (churn-tolerant: `max(4·|churn|, 0x10000)`),
      `selectedMarker` (trigger canonical `0` + inline u32 `1`).

### Gates that now pass reliably
- [x] `restored` — passes in every recent run.
- [x] `receiveOrderMarginUs` — ~58,000–62,000 µs (needs ≥300).
- [x] `preReleaseUsed` — **40,528** in the current config (needs ≥9728); was 7472 before the
      release-read was decoupled from the `released` signal.
- [x] `nativeGuard` — 0/0/0 in good samples (`triggerAborted false`).
- [x] Trigger window — **fast: 35–37 µs** (was 857 µs before the constant-handle change) whenever its
      send slips through undeferred.

---

## 2. Current task — P7 `extent-present` qualification

The remaining work is one race: **get all ten protected roles' windows to contain the trigger's
window in the same sample.**

### Round-3 update (2026-11-04, 21:15)

The `released`-signal ordering bug is **fixed in `host.py`** (signal now fires before the release
role's sleep), which restores the trigger's start as a `CIV_TRIGGER_SLEEP` function: 1.0 ms →
1406 µs, 2.0 ms → 2404 µs, fully decoupled from `CIV_RELEASE_DELAY`. Two live boots after the fix:

- **Boot B (21:10, `CIV_TRIGGER_SLEEP=2000000`): 7/10 spanning in both attempts** (trigger fast,
  252/277 µs at 2404–2406 µs start; tails 0–2, canary @ depth 5, fillers, commit all span).
  `preReleaseUsed` 8128 (closest clean reading; still < 9728). Boot A (sleep=1.0 ms) fired the
  trigger too early — proof of decoupling, not a qualifying config.
- The two remaining misses split cleanly: **guards** (they now start at `GUARD_DELAY` alone →
  ~0.5 ms; set `CIV_GUARD_DELAY≈1950000` to restore their historical 9/9-deferral send time) and
  **tail3** (fastest non-deferral yet: 124–169 µs windows; `CIV_SLOT_SWAP=5,4` is the next test).
- Console hygiene confirmed again: two waves per payload, wedge on the third reserve, loader
  refuses further loads — **plan experiments in twos**.

Next boot's exact config is queued at the end of [[handoff]] §14. After that, the only gate with
no live confirmation is `preReleaseUsed` (8128 vs 9728; game churn can invert it, see §13.7).

### Round-4 update (2026-11-04, 21:21)

Executed live with `CIV_GUARD_DELAY=1950000` + `CIV_SLOT_SWAP=5,4`:

- **Guards fixed** — they span both attempts, including one with a deferred trigger.
- **9/10 best sample** (attempt 2, 21:20): fast trigger 2405→2951; only filler0 (−145 µs) and
  tail3 (−471 µs) miss, both end-side. A 66 µs trigger at the same start would make this a 10/10.
- **`preReleaseUsed` 12816 on attempt 1 — first reading above the 9728 gate** with valid timing.
- **tail3's handle hypothesis refuted** (swap didn't flip deferral). Also: §13.7's marker-move
  "refutation" was invalid — 0x2340→0x2435 both live inside piece 4 (tail3's input), so
  **"tail3 defers iff its input resolves" is still the live hypothesis**; `CIV_TAIL3_PAD=1`
  diagnostic is implemented and queued.
- Boot hygiene held: two attempts, payload died, loader wedge. Fishing config (fast-trigger
  hunting with tail3 +90 µs) queued at the end of [[handoff]] §15.

### Round-5 update (2026-11-04, 21:27)

Fishing boot: attempt 2 hit **9/10 again** (trigger 2404→2822, mid-class 418 µs service; misses
filler0 −154 µs, tail3 −379 µs). The accumulated samples now pin the requirement: **10/10 needs a
fast-class trigger (service ≤ ~39 µs)** — tail3's non-deferring service never exceeds 545 µs and
its start is capped by the trigger's 2404 µs start, so mid-class triggers always miss it by
~380–470 µs. filler0's gap is tunable (`CIV_FILLER_DELAYS=2500000,2300000`, queued). Boot
throughput (2 samples/payload, fast class ≈ 2/10) makes the **payload-side `retryWave` port the
highest-value next engineering item**. Config queued at the end of [[handoff]] §16.

### Round-6 update (2026-11-04, 21:45)

**Session cache landed** (`cached_session`): the retry loop had been opening six fresh fsp-srv
sessions per attempt vs ns2jb's one shared session; retries now run in ~60–90 s with zero new
service opens. **But attempt 3 still froze the console** — the freeze is not the session count.
New suspects: worker-thread accumulation across attempts (13 frames each, do they exit?),
FS-process heap degradation, payload backlog. Next boot captures the full log + reserve trace to
pin the death point. Sample quality held: another 9/10 (missing tail3 −319, filler1 −138 µs);
`preReleaseUsed` 24272 on the deferred-trigger sample (gate passes when slow).

**New fold-preserving tail3 lead, implemented and queued:** tail3's request descriptor is the only
non-0x800 one (0x441, the extent tail) — small IPC payloads ride a different buffer path than
0x800 user-buffer requests, which would explain "only tail3 never defers" exactly.
`CIV_TAIL3_SIZE=0x800` pads its input with trailing `'/'`s (marker position unchanged,
byte-identical fold in the marker region). If padded tail3 defers, tail3 joins the fixable roles.
Full config at the end of [[handoff]] §17.

### Round-7 update (2026-11-04, 21:47)

- **`CIV_TAIL3_SIZE` refuted** — padded tail3 still never deferred (338 µs / 781 µs windows).
  Seven eliminations now stand; the last unexplored axis is **tail3's core pin** (index 5 → core 1,
  shared with the hole). `CIV_PIN_OFFSET=<n>` implemented to test it.
- **Attempt-3 death pinned by the trace**: the reservation completes, then wave()'s client-clone
  step (`CloneSession`) blocks — and ns2jb builds its 13 clients ONCE outside `wave()` (lines
  271–275) while we cloned per attempt. **Client cache implemented** (build once per payload,
  owner-page content refreshed per attempt, list copied so in-place swaps stay per-attempt).
- Next boot tests both: attempt 3 survival + core rotation. Config at the end of [[handoff]] §18.

### Round-8 update (2026-11-04, 21:57) — both fixes confirmed live

- **3 attempts per payload, zero deaths** — the client cache matches ns2jb's retryWave throughput
  (the 4th attempt wedged at re-reserve: the ~3-reservation-cycle payload lifetime is the same
  ceiling the reference has).
- **tail3 DEFERS under `CIV_PIN_OFFSET=1`** (767–1330 µs windows in all three attempts) — deferral
  follows the core pin, the last standing axis. Tail3 is a fixable role now.
- Miss structure collapsed to delay-tuning: best sample 7/10 with tail0 missing by **20 µs**,
  filler0 by 139, tail1 by 169 (all start-side, all delay-fixable); only filler1's 297 µs
  end-side miss needs deferral. Tuned config queued at the end of [[handoff]] §19.
- `preReleaseUsed` still churn-noisy (240–7456 this boot) — needs the bracketing-read fix
  regardless of allPending.

### Round-9 update (2026-11-04, 22:00)

Tuned boot regressed (6/8/6). Corrected reading: **all misses are END-side non-deferral** — starts
transferred fine from round-8 (tail1's start answered its delay cut 574 µs for a 295 µs cut). What
wanders per boot is the **deferral assignment across cores** (canary deferred in round-8, not in
round-9; filler1's 89% deferral didn't fire on core 1). The stable derivations: **the trigger's
start knob is `CIV_GUARD_DELAY`** (start ≈ guard-signal + 400 µs; `CIV_TRIGGER_SLEEP` is absorbed
by the gate wait), and the queued next measurement is the **pin-offset sweep** (`CIV_PIN_OFFSET=
0..3`, one boot each) to find the assignment whose deferral-prone cores cover all ten protected
roles. **`CIV_PRE_BRACKET=1` landed** (min-of-bracket-reads gate) for the churn-noisy
`preReleaseUsed`. Config at the end of [[handoff]] §20.

### Round-10 update (2026-11-04, 22:05) — sweep point offset=2: 4/6/3

Offset 2's map fails differently: tail0/canary starts stuck late on core 0 (unresponsive to
delays), tail1 non-deferring on its second core tested. **No core assignment defers everyone**, and
the deferral map wanders boot-to-boot even at a fixed offset — the per-core deferral function is
real but noisy. Even a 117 µs fast trigger only scored 6/10 under offset 2. Offset 1 remains the
best map (8/10 twice). `CIV_PRE_BRACKET` did not tame `preReleaseUsed` — the gate likely needs a
Civ-re-derived threshold rather than ns2jb's 0x2600. Sweep table and next-boot priorities in
[[handoff]] §21. Console note: game relaunch (not full reboot) suffices for a fresh payload.

### Round-11 update (2026-11-04, 22:09) — sweep point offset=3: 4/5/5

Tail1 non-deferring on its third core (c2); filler1's end missed by only **15–17 µs** twice. The
offset-0 column (last untested cores for both laggards) needs its own boot — a same-payload 4th
attempt wedged at re-reserve. Sweep verdict: **offset 1 stays the best map (8/10 twice)**; no map
defers all ten; the closest misses are now 15–82 µs end-side, i.e. fishing distance once the map
is frozen at offset 1 and samples are harvested at 3/payload. Remaining engineering item: a
Civ-re-derived `preReleaseUsed` threshold. Details in [[handoff]] §22.

### Round-12 update (2026-11-04, 22:13) — sweep point offset=0: 2/4/3, but the endgame config fell out

Offset 0 is the worst map (2/4/3) — yet the **fast trigger appeared twice (48/77 µs)** (the
trigger sits on uncontended core 0 there) and **preReleaseUsed hit 40816** (largest clean reading;
the 272 readings were the bracket min() poisoned by a failed pre2 reply — fixed). The geometry
from these samples yields the first all-roles-demonstrated config: `CIV_GUARD_DELAY=2100000`
(slides the trigger start +150 µs past every start-side miss, all ≤87 µs),
`CIV_TAIL_DELAYS=2480000,2250000,2350000,2450000` (tail1/tail3 ends clear a fast trigger's end).
Fast-trigger rate under offset 0: 2/3 samples. Queued at the end of [[handoff]] §23 — the next
boot is a conjunction test, not a new mechanism.

### Round-13 update (2026-11-04, 22:17) — 5/3/7; corrections + two new instruments

- §23's GUARD_DELAY claim was wrong for offset 0: the trigger's start is **`TRIGGER_SLEEP + 400 µs`**
  (the sleep floor binds; gates arrive earlier). `CIV_TRIGGER_SLEEP` is the trigger-start knob on
  this map.
- **`preReleaseUsed` is churn-dominated on Civ VII** (40816/5168/208 within one boot). Landed
  **`preReleaseHeldFreed`** (counter drop across the release — churn-independent; ≈12160 if the
  group was held) in every report; `CIV_HELD_GATE=1` switches the gate to it.
- Landed **`CIV_PIN_MAP`** (per-role core list): the trigger is fast only on core 0 and tail1
  defers only on core 0's arrangement — no single rotation gives both.
- Misses collapsed to tail1 (end −500, needs its deferral), tail2 (start −26, trivial), filler0
  (end −189). Tuned round-14 config queued at the end of [[handoff]] §24.

### Round-14 update (2026-11-04, 22:23) — 7/7/8; the held-gate metric works; only the lottery remains

The round-14 config delivered: trigger slid to 2455 exactly per the floor model (no start-side
misses in any sample); **`preReleaseHeldFreed` = 6400/6752/6752** — stable within and across
attempts, unlike `preReleaseUsed` — a workable Civ-re-derived gate (`CIV_HELD_GATE=1` once its
floor is established over more boots). Attempt 3: **8/10**, missing only commit and canary — both
non-deferral end-side lottery misses with ample start room. Every mechanical ingredient has now
been demonstrated live; the remaining work is the joint commit+canary deferral in one sample,
harvested at 3 samples per boot of this exact config. No new mechanism queued — the next boots are
repeats. Full detail in [[handoff]] §25.

### Round-15 update (2026-11-04, 22:26) — 5/8/6; config frozen

Attempt 2 hit 8/10 again (fillers, end-side); the stragglers rotated between samples and boots —
confirming the per-sample deferral assignment is a genuine lottery. **`preReleaseHeldFreed`
stable across two boots (6720–6800)** — the gate metric is real; ~6000 threshold separates held
from gone. Decision: **config frozen, harvest mode** — 3 samples per boot, no per-boot retuning.
When a sample lands allPending=True, selectedMarker resolves in the same run. [[handoff]] §26.

### ★ Round-16 (2026-11-04, 22:30) — ALLPENDING = TRUE (first valid sample)

Attempt 2: **all ten protected roles span a fast trigger** (2455→2872 µs, 417 µs service), valid
rotation (restored ✓, margin 37227 µs, guards 0/0/0). Preserved as
`runs/MILESTONE-allPending-true-r16a2.json`. The opening ask of [[handoff]] is answered **yes**.

Fixed same boot: the `CIV_HELD_GATE` ordering bug (report now shows the gated value) and the
threshold (`CIV_HELD_MIN`, default 6000, fitting heldFreed's stable 6400–6800 scale). With that,
**`selectedMarker` is the only failing gate** — the trigger resolved 0x202 (fold cold) with the
window perfect. The fold needs the trigger issued in the right *phase* of the seeds' deferred
state, not merely while they're outstanding. Harvest boots continue with the same config; per
sample, correlate the trigger result against the span count and the trigger's service class.
[[handoff]] §27.

### ★★ Round-17 (2026-11-04, 22:35) — TWO all-span samples; the fold RESOLVED

Attempts 2 and 3 both **allPending=True**. Attempt 3's trigger: **result 0, canonical 0** — the
fold fired and resolved a real object — with inline **type 0 (directory)**: the fold reaches the
directory and stops short of the marker basename. Preserved as
`runs/MILESTONE-allSpan-fold-result0-r17a3.json`. Fixed: `windowOk`'s held threshold now honors
`CIV_HELD_MIN` under `CIV_HELD_GATE` — recomputed, attempt 3 is a **hasWindow-qualified sample
failing only selectedMarker** (ns2jb's own `checkExtent` bar). One geometry fix from the
negative/positive stage. Marker-geometry candidates (one variable per boot): `CIV_EXTENT_MARKER=
end`, `CIV_NORMALIZED_PATHS=1`, canary-depth sweep. [[handoff]] §28. (Pass-4 correction: the
type-0 result means the resolution terminated at an existing directory before the marker — see
[[handoff]] §29's pass-4 note.)

### ★★★ Round-18b (2026-11-04, 22:45) — external byte-level review: four real bugs found and fixed

An external review of the milestone artifacts verified (against the bytes and the reference)
that:

1. **The release role NEVER closed group.outer in any wave** — `context_bytes` wrapped
   close_object and clobbered the object id (release hex proves CloseObject(1)). The 59-object
   group leaked every attempt; the reservation was never released mid-wave, so the trigger
   exploited a still-held heap, not the freed gap the exploit intends. **Fixed** — expect the
   geometry to shift on the next boots.
2. **selectedMarker was impossible at the 0x2340 default**: ns2jb's extent profiles place the
   basename FLUSH (extent − len); 0x2340 is the isPositive/directory placement, and trailing
   slashes after a FILE make fsp-srv return 0x202 (exactly r16a2's trigger result). **Fixed** —
   extent profiles default to flush placement.
3. Release counter reads queued behind tail3's session (the 240-byte readings explained);
   dedicated clone now used.
4. commit/guards lost their CMIF context on packet rebuild; `freeAddresses` freed the aligned
   interior pointer (heap corruption between attempts). Both fixed (console.js maps aligned→raw).

Queued: the commit_data 16 MB dirty write (ns2jb does it per wave; commit finishing early is the
symptom). **The next boots re-baseline everything** — the exploit's intended state (released
group) is now real for the first time. [[handoff]] §28b.

**Pass-4 adds finding 3:** the trigger's result 0 / type 0 means the resolution terminated at an
EXISTING directory (a NUL boundary before the marker piece) — "the fold fired" claims for type-0
samples are downgraded; the fold crossing pieces is proven by the 0x202 samples instead.

### Round-18 update (2026-11-04, 22:38) — marker=end refuted; marker sweep landed

`CIV_EXTENT_MARKER=end` collapsed the trigger to **0x2EEA02 (abort) in all three samples** —
the extent-tail basename is off the fold's reach. The marker stays at 0x2340. Landed
**`CIV_MARKER_OFFSET=<hex>`** to sweep the basename across pieces (candidates 0x2140/0x1b40/0x1340)
— round-17's all-span fold resolved type 0 (directory), so the reach boundary is between the
directory and the abort. **Remaining: selectedMarker only** — window proven in three all-span
samples. [[handoff]] §29.


### ★ Round-19 update (2026-11-04, 22:42) — windowOk = TRUE (first hasWindow-qualified sample)

`CIV_MARKER_OFFSET=0x2140`: attempt 2 recorded **allPending=True AND windowOk=True** (trigger
2455→2727, result 0, canonical 0). **The hasWindow bar is formally met by a live sample.** The
marker-offset hypothesis is disconfirmed though — 0x2140 resolves identically to 0x2340 (type 0,
a directory): the fold's resolved object doesn't track the basename's position. The type-1 route
is the §7 finding: **the normalized form resolves result 0 + inline 1** —
`CIV_NORMALIZED_PATHS=1` is the queued single-variable experiment (the changedPaths expectation
bug that earlier made it look like overwrites was fixed in §13.7). [[handoff]] §30.


### Round-20 update (2026-11-04, 22:50) — release fix was masked; now really fixed

The §28b build_wave fix was necessary but not sufficient: the wave body's generic context
re-apply loop re-clobbered the object id (release hex still showed object 1; 4/6/4 samples).
**Fixed**: the loop passes "release" through untouched; offline byte-check verifies the full
pipeline (object id + context intact). Silver linings: the payload survived 3 attempts on the
§28b stability fixes. **The next boot is the first with a genuinely closing release** — expect
heldFreed to jump to ≈12160 when the group actually closes, and re-baseline the window/fold on
the exploit's real geometry. [[handoff]] §31.


### ★ Round-21 update (2026-11-04, 22:55) — the release WORKS; real geometry live

First boot with a genuinely closing release: **heldFreed −8400/−21952** (sign was inverted;
fixed — positive = bytes freed) proves the group actually leaves the heap at release. **8/10
protected in all three attempts** — only guard0/guard1 miss (end-side 252–387 µs, 687 µs of start
room unused). The fold gave result 0 twice (still type 0). One-knob fix queued:
`CIV_GUARD_DELAY=2400000`. [[handoff]] §32.

### Round-22 update (2026-11-04, 23:02) — heldFreed +41 k/11 k; the guard gap is structural

The release closure at scale confirmed (heldFreed +41184/+10784 — positive after the sign fix).
But the guard bump exposed the structural relation: **guards' end − trigger end = w_guards −
TAIL − w_trigger**, independent of GUARD_DELAY — ns2jb's hardcoded 400 µs post-gate tail makes
the guards fail whenever the trigger's service exceeds ~1 ms. **Landed `CIV_TRIGGER_TAIL`**;
cutting it to 100 µs flips the margin positive across the whole mid class. Coordinated-shift
config queued at the end of [[handoff]] §33.

### Round-23 prep (2026-11-04, 23:05) — external pass-3 review adopted

Pass-3 review verified the §33 derivation to the microsecond and made three calls, all adopted:
(1) `CIV_CANARY_DEPTH=5` was accidentally dropped from the §33 command — restored; (2) the 200 µs
shift was wrong — round-21's delays went 8/8 non-guard in ALL three attempts, so keep them and
instead set `CIV_GUARD_DELAY=2550000` + `CIV_TRIGGER_TAIL=100000` (guards start ~2355, trigger
restored to the proven 2455 start, guard ends clear the mid-class trigger end); (3) CloseObject
(outer) actually executes ~740–785 µs AFTER the trigger's start (the report's release timing was
the first counter send — **fixed to report the last send = the real close**). If round-23 lands
all-span with type 0/0x202, lowering `CIV_RELEASE_DELAY` (1500000–1800000) so the close precedes
the trigger's resolution is the top `selectedMarker` lever. Config in [[handoff]] §34.

### ★ Round-23 update (2026-11-04, 23:17) — all-span + windowOk on the REAL geometry

The §34 config delivered: attempt 1 = **ALL-SPAN + windowOk=True + fold-fired (result 0) with
heldFreed +40944** (the real closure at full scale); attempt 2 = all-span again (result 0x202).
The window is **solved and repeatable on the exploit's real geometry** (four all-span samples in
three boots). Remaining: `selectedMarker` only — the fold resolves a directory (type 0) or 0x202.
Round-24 per the decision tree: **`CIV_RELEASE_DELAY=1500000`** so CloseObject(outer) lands
before/inside the trigger's resolution (the close currently executes ~3.4 ms+, after the
trigger's start). Config at the end of [[handoff]] §35. Milestone:
`runs/MILESTONE-allSpan-windowOk-realgeom-r23a1.json`.

### ★★★ Round-24 update (2026-11-04, 23:23) — ALL-SPAN ×3/3: the window is now deterministic

`CIV_RELEASE_DELAY=1500000` (close before/inside the trigger's resolution): **allPending=True in
ALL THREE attempts** — the window is a property of this config, not a lottery. CloseObject(outer)
sends at ~2036 (before the trigger's 2469 start). Trigger results 0/0/0x202 — the fold fires, but
still resolves type 0 (directory): the close timing alone didn't flip the terminal component.
heldFreed stable 5344/5376 (tracks the close's phase; narrowly missed the 6000 threshold —
round-25 re-derives it to 5000, §13.7's option 3). **Round-25 adds `CIV_NORMALIZED_PATHS=1`** (§7:
the normalized form resolves result 0 + inline 1) with `CIV_HELD_MIN=5000`. Config at the end of
[[handoff]] §36.

### Round-25 update (2026-11-04, 23:29) — normalized form: null; windowOk=TRUE preserved

Attempt 2: **ALL-SPAN + windowOk=True** with `CIV_NORMALIZED_PATHS=1` + `CIV_HELD_MIN=5000`
(heldFreed 5376) — preserved as `runs/MILESTONE-windowOk-normalized-r25a2.json`. But the
normalized descriptor did NOT flip the trigger's type (0x202/type 0, identical to short form).
The trigger's terminal component is now insensitive to marker position (r19) and descriptor form
(r25). Surviving selectedMarker hypotheses: canary depth (queued r26), canary size, close-phase
fine-tuning (RELEASE_DELAY inside the trigger's resolution). Trigger-descriptor-size theory also
eliminated (ns2jb line 379 is byte-identical). Config at the end of [[handoff]] §37.

### Round-25 prep (2026-11-04, 23:35) — external pass-4: three code fixes + the type-0 reframe

Pass-4 verified rounds 23/24 to the byte (heldFreed 5344/5376 = 12160 − 6880 retention, exact)
and caught three would-be-wasted-boot issues, all fixed: (1) the retry loop broke on windowOk
alone (why round-23's boot ran one attempt) — now also requires selectedMarker for
extent-present; (2) NORMALIZED+DEPTH staged the depth canary while expecting 0x202 — phantom
canaryChanged averted; root cause: the normalized canary's 32-'A' parent chain was never created
(ns2jb creates + verifies it) — the normalized branch now creates the chain and verifies 0/1;
(3) guards/fillers now staged ns2jb-faithful always (guard0 + same-path-'C' variant, 0x300
missing; filler descriptors [marker16, page_absent@0x7ef]). **Reframe:** type-0 trigger results
mean "terminated at an existing directory before the marker" — the fold crossing into the marker
piece is proven by the 0x202 samples. Docs (handoff §29/§30/§13.5, tracker rounds) reordered
chronologically. Round-25 command updated in [[handoff]] §38 (normalized canary as ns2jb stages
it; no CIV_CANARY_DEPTH).
### Round-25b update (2026-11-04, 23:38) — normalized canary uncreatable on Civ VII

The chain creation failed silently (0x202s swallowed by an over-tolerant filter; per-step
diagnostics added). Likely law: **MaxPathLength = 0x300** — Civ's fsp-srv won't create 1440-char
paths (ns2jb's SmileBASIC did). **Normalized-canary route closed**; the canary stays deep-form
(result 0/type 1). Boot still delivered the **sixth all-span + windowOk=True sample** — the
window is deterministic across ALL staging variants. The missing canary still spans (missing-path
lookups defer too). selectedMarker hunt narrows to **close-phase fine-tuning**
(`CIV_RELEASE_DELAY=1900000` queued — close completes inside the trigger's resolution). Command
at the end of [[handoff]] §39.

### Round-26 update (2026-11-04, 23:43) — close-phase swept; next: the ALIAS OFFSET (offline)

Attempts 1–2: ALL-SPAN + windowOk=True (eight all-span samples total). Close-phase response
confirmed: close-before-start → result 0/type 0; close-inside/after (1.9 ms) → 0x202; **neither
yields the marker**. Third null knob in a row (marker offset, descriptor form, close phase) —
the remaining variable is the **heap-alias geometry**: Civ's 12160-byte reservation vs ns2jb's
12272 shifts fsp-srv's internal piece adjacency, so the folded resolution hits a NUL before the
basename. **Next work item is offline**: apply ns2jb's address-planner (`cache-address-plan.cjs`,
`address-encoding-control`) to Civ's measured geometry; the wave knobs are done. [[handoff]] §40.

### Round-27 update (2026-11-04, 23:55) — the native cache-read stage is PORTED and LIVE

`leak_read` (+ `leak-read` CLI) works on Civ VII: reserve → open the reserved gap's content
storage (200) → GetSize (4.975 GB) → **IStorage::Read is command 0** (806 echoed the request —
that was the bug) → six 0x4000 blocks captured + inline pointer scan. The planner dig found the
address machinery needs only a measured target (`native-links.cjs` scans leak reads for aligned
39-bit pointers; `signed-cache-plan.cjs` is pure arithmetic, portable as-is). **Queued:
`harness/leak-cycle.py`** — one process, one setup (three setups this session wedged the
console): wave ×2 → leak-read → grep the dumps for the wave's own strings (`CIV_GAP`,
`SPAN_MARKER`…) — if they appear, the alias is CONFIRMED and the pieces' layout/NUL seams are
directly readable. Console needs a relaunch. [[handoff]] §41.

### Round-28 update (2026-11-04, 00:00) — leak-cycle executed: gap storage does NOT mirror the heap

The decisive test ran clean in one process: wave 1 all-span (AP=True, windowOk=True), wave 2
lottery (AP=False); post-wave leak-reads (8 blocks × 2 waves) contained **zero wave strings and
zero pointer candidates, byte-identical to pre-wave controls**. Verdict: the reserved gap's
content storage does not mirror the wave's heap on Civ VII — the aliasing law differs from
ns2jb's. Next discriminating measurement: read the storage DURING the wave's busy window (the
alias may populate only while fsp-srv actively serves). If still clean, port ns2jb's priming
sequence (`fs-memory-control.cjs`: memset-poison + requalification + signed-header install)
before reads can see the heap — all primitives already exist on Civ. [[handoff]] §42.

### Round-29 update (2026-11-05, 00:05) — §42's verdict WITHDRAWN; the leak machinery itself was wrong

Pass-5 review of the leak code (byte-verified): **`leak_read` saved 224 bytes of TLS residue, not
the DMA** — IStorage::Read lands in rx_page (the Type-B receive buffer); the proof is in the
artifacts (every `leak-*.bin` exactly 224 bytes; block01's bytes 16..32 carried our own outbound
u64 argument). Fixed: reusable poisoned rx_page + read-back via the console read command +
all-poison detector; scan is now findNativeLinks-faithful (block heads + zeroTail/zeroAfter32);
storage closes via CMIF Close. **Conceptual model corrected**: command 200 = Civ's RomFS
(4.98 GB); reading it is clean RomFS, not heap — ns2jb's heap leak needs the full chain (positive
wave frees the canary slot → candidateHandle prime allocates the cache table INSIDE that slot →
copyField waves overwrite the cache entry → over-read 0x1c000 from one 0x4000 buffer → neighbor
invalidation writes freelist pointers). **Round-28's "does not mirror" conclusion retracted** —
the test never primed the alias. `leak-cycle.py` v2 is stage-gated; the fs-memory-control.cjs
priming port is the next offline item after the wave prerequisites verify. [[handoff]] §43.

### Round-30 update (2026-11-05) — fs-memory-control port landed + pass-6 review fixes

The §43 priming item is done. Ported and offline-verified: `harness/signed_cache_plan.py`
(verbatim .cjs port, 6/6 triples + 3 bad-args byte-identical), `harness/fs_memory_control.py`
(controller core: select/readBlock/read/writeAt/qualifySignedHeader/qualifySingleBucket/dispatch;
10/10 selftest), and `harness/leak-cycle.py` v3 stage `memctl` (reachable; `h.console` + a real
candidate handle opened via command 200 → prime read). `host.py` grew `worker_wave` payload/
read-size pass-through, `_stage_extent_ns2jb`, and `copy_field_wave`. Plan/state: [[fs-memory-control-plan]],
[[handoff]] §44.

Pass-6 review fixes applied: (1) `memctl` stage guard + ctor/candidate handle; (2) `copy_field_wave`
now `rereserve()`s + frees `wave_allocs` before every wave, breaks out at `confirmed ==
required_copies`, uses the `CIV_HELD_GATE`/`CIV_HELD_MIN` metric, and the `tries>1` recursion
forwards `payload`/`read_size`; (3) all writes >= 0x4000 bytes chunk at 0x1000 (`host.write_chunked`
+ `fs_memory_control._write`) to stay under console.js's 0x8000 JSON-frame cap, and `leak_read`
re-poisons the rx page before each block; (4) the normalized canary chain is rooted at `self.scratch`;
(5) new opt-in `CIV_RELEASE_CLOSE_GATE` makes the trigger wait for the release's actual
`close_object(outer)` (leak-cycle sets it). **Still needs a console boot (P3)** — notably whether
`windowOk` and release-before-trigger can hold together under `CIV_RELEASE_EARLY=1`, and Civ's
bucketPeriod + the ordinary-content control handle. [[handoff]] §44.

**Live P3 attempts (192.168.103.215):** two code bugs surfaced. (1) The first `leak-cycle.py
memctl` took the romfs branch — `sys.argv = ["host.py"]` was set BEFORE `stage = sys.argv[1]`, so
`stage` was always `romfs` (line 68 was not sufficient). Fixed. (2) The wave then died mid-poll on
three fresh boots; the decisive run crashed on the **baseline wave with `CIV_RELEASE_CLOSE_GATE`
off** and one setup, which exonerated duplicate-setup and the gate wait. Root cause: the new
`closed` event was placed at `ctrl+0x1c`, but SDK event structs are 8 bytes and the gates are
8-byte spaced, so `initialize`/`clear` clobbered `guard` (0x18) and `CF` (0x20) every wave. Moved
to `ctrl+0x40` (ctrl zeroed to 0x80). Romfs control reads were clean throughout. Next boot: one
`memctl` run; the baseline should now complete and the close-gated variants answer the
windowOk/close_gap question. [[handoff]] §44.

### Pass-7 review (external) + live geometry result (2026-11-05)

Four code fixes: gate-event 8-byte alignment (confirmed: `closed` moved 0x1c→0x40); the pacing
knobs (`CIV_RELEASE_DELAY` etc.) were import-time constants so the sweep's env overrides were
no-ops — `worker_wave` now reads them at wave time; the diagnostic baseline is opt-in
(`CIV_PREREQ_BASELINE=1`) and the extent allocations are now in `wave_allocs`; `stage full` now
exits clearly and `_ipc` uses `canonical` not `result`.

**Live result (one setup, 4 variants):** the close gate makes `close_gap` negative (-107/-2661/
-2659 µs) but `allPending` goes False in every close-gated variant; the gate-OFF baseline had
`allPending=True` with `close_gap=+6906`. **The two prerequisites do not co-hold with the current
wave structure** — the trigger waits for the close after the protected windows have ended. This
is a geometry problem needing console tuning (send protected roles later, or close earlier), not
a code fix. Per-variant reports now saved as `runs/prereq-variant-N.json`.

**Payload lifetime:** a second `leak-cycle.py` run on the same payload wedged the console
(open-but-silent at the wave's storage open) — one run per payload, relaunch per sweep. Added
`CIV_PREREQ_SKIP=1` to validate the controller on clean RomFS without the wave. [[handoff]] §44.

**Second geometry run (3 samples, capped):** `RELEASE_DELAY=600000` + no `PRE_BRACKET` moved
`release.end` 8874 → 4002 µs (trigger 4109), but the miss is END-side (canary/fillers/guards end
3.1–3.8 ms before the post-close trigger). `PROTECT_SHIFT` moves the protected roles later but
excludes the guards, so `CIV_GUARD_DELAY` must rise too. Next sweep (likely winner first):
600k/no-PRE_BRACKET/GUARD_DELAY=4.0 ms/PROTECT_SHIFT=1.2 ms. Samples per boot now capped at 3
(`CIV_PREREQ_MAX`). [[handoff]] §44.

**Pass-8 review fixes:** the `release.endUs` drop is partly a **cold-vs-warm hole** effect (wave 1
hole ~60 ms → variant 0 is a warm-up; wave 2+ hole ~39 ms → variant 1 is the expected hitter);
and a real `timing_valid` bug was fixed — `origin` used `roles[0]` (commit), which `CIV_PROTECT_SHIFT`
moves later than hole/release, making their `startUs` negative and forcing `allPending=False`. `origin`
is now the minimum start tick across all roles. [[handoff]] §44.

**Third geometry run (3 samples):** `timingValid=True` for all (fix confirmed). The trigger's
START is set by guard1's `guard` signal (`wait(released)+GUARD_DELAY`) + tail, NOT by the close.
Raising `GUARD_DELAY`/`PROTECT_SHIFT` moved the trigger to ~3.8 ms, past the protected windows,
so the miss was END-side again. Corrected: keep the harvest protected/guard timing and only make
the close early (`RELEASE_DELAY=300000`, no `PRE_BRACKET` → `release.end` ~1.0 ms warm), so the
trigger stays in the proven ~2.4 ms window with `release.end` far before it. Next sweep: ambient
warm-up (wave 1), 300k (wave 2, likely), 600k (wave 3). [[handoff]] §44.

**★ Round-31 (2026-11-05) — prerequisite PASSED, controller read LIVE.** Pass-9 identified that a
commit request in flight at t=0 makes the release's `CloseObject(outer)` defer 3.5–4.5 ms; added
`CIV_COMMIT_DELAY`. Winning sample: `RELEASE_DELAY=300000` + `PRE_BRACKET` off + `COMMIT_DELAY=1800000`
→ release close 311→935 µs, commit 1697 µs, trigger 2367→4076 µs, **allPending=True,
windowOk=True, close_gap=-1432 µs**. In the same process `memctl` then opened the candidate handle,
primed it (result 0), dispatched `info`, and the smoke `read` returned 4096 bytes of real RomFS
(`runs/memctl-memory-1.bin`) — the first live controller read. `selectedMarker` still False, so the
alias chain is not yet exercised. [[handoff]] §44.

**Round-32 — A–D alias chain implemented; blocked on `selectedMarker`.** Ported
`cache_address_plan.nul_writes` (7/7 against the .cjs) and added an `alias`/`full` stage:
A positive → B reserve/candidate/prime → C `installCombinedLeak` (7 copyField writes) → D
over-read, gated by `CIV_ALIAS_UPTO`. Live: prerequisite passed (`COMMIT_DELAY=2000000`), then
**Stage A failed** — positive qualified (`AP=True`, `win=True`) but `canaryChanged=False`,
`canaryType=1`, trigger `0x2EEA02` (normalizer abort), `nativeGuard=0`. Same unresolved
`selectedMarker`/`markerReproduced` blocker. The `positive` branch now honours `CIV_MARKER_OFFSET`;
the live marker sweep (`0x2140`, `0x1b40`) still gave `canaryChanged=False` and trigger `0x2EEA02`
for both. §30's `0x2140`/result-0 was *extent-present* and resolved to type 0 (a directory), which
is not `selectedMarker` (type 1) — the fold carries a directory regardless of marker placement, so
this is not a marker-offset problem. The §30/§25 lead is the normalized one-descriptor form
(`CIV_NORMALIZED_PATHS=1`), which round-25b found uncreatable on Civ VII. Stage A needs a new
idea; the A–D scaffolding is ready once the positive overwrite lands. [[handoff]] §44/§45.
**The hunt's action plan now lives in [[selected-marker-plan]]** (SM1–SM4: canary depth, canary
size, address-planner on the 12160-byte geometry, §30 cross-check).


### ★ Round-33 (2026-11-05) — external vault review: gated-canary sign fix; Stage-A probes queued

1. **[[gated-canary-plan]] boots 10–12 had `allPending`'s signs inverted.** The gate
   (`host.py`, ns2jb-faithful) needs **start delta < 0, end delta > 0** (the canary
   starts before the trigger, ends after it). Boots 10/11 measured start +55/+52 ✗ with
   end +709/+84 ✓ — the queued size cut (0x400) and the boot-12 TAIL plan attacked the
   passing end side (its own prediction, end ≈ −68, was called "closing the window" —
   it is the failure condition). Origin: the boot-8 shift model in [[selected-marker-plan]]
   ("need start delta > 0 AND end delta < 0"), corrected there. **Corrected boot 12:**
   `CIV_TRIGGER_TAIL=200000` + `CIV_CANARY_SIZE` back to **0x5a0** + offset 150000 →
   predicted start ≈ −48, end ≈ −48 + 654 ≈ **+606** → the normalized canary spans for
   the first time (equivalent alternative: offset 40000, size 0x5a0). Non-deferred-canary
   regimes (boots 5–8 class) stay unwinnable at any offset — lottery losses; record
   `canary_span − trigger_span` per sample.
2. **Stage A is form-blocked, not marker-blocked.** Round-32's positive trigger
   `0x2EEA02` with `nativeGuard=0` implicates the `/\..\` prefix piece itself; marker
   offsets cannot touch it (the `host.py` staging comment's round-19 citation conflates
   extent-present with positive). SM5 queued: sequential form probe + `/../` variant.
3. **SM6 queued: downward marker sweep** (`0x240/0x640/0xa40/0x1400`) to measure the
   fold's carry limit — every swept offset so far sits in piece 4, and the direct form
   resolves 1440-char paths (round-25b's MaxPathLength closure refuted; the stale
   verdict was still live in [[handoff]] §39/§44), so any cap is fold-specific. Feeds SM3.
4. **Docs:** index links added (gated-canary plan, save-I/O note, Questions folder);
   date typos fixed (2025/2024 → 2026); `Questions/` duplicates of the path-limit and
   controller-read queries verified byte-identical to the `civ7-homebrew/` copies (left
   in place).

### Geometry established

**(Superseded — pre-round-3 analysis; the live state is the round updates above and [[handoff]]
§13 onward. The client-order effect below IS still valid and was the basis of the slot swaps.)**



- The protected roles span comfortably when they defer (windows ~1.8 → 7.9 ms).
- The tails' windows close early when they *don't* defer (~1.4 → 2.2 ms) and defer out to ~3 ms when
  they do.
- **The trigger's `startUs` is fixed at ~2.4 ms and is not tunable** — verified across four runs where
  `CIV_TRIGGER_SLEEP` swept `1000000 → 1800000` and the start stayed `2370–2398`. Only its *speed*
  varies (fast 35 µs vs deferred ~5.4 ms).
- **A deferred trigger is structurally unqualifiable**: queued last, it drains last, so its end is
  behind every protected end. A fast trigger is required.
- **Drain order follows queue order**, so the protected role sent *earliest* closes *earliest* — and
  its close must be after the trigger's end. Sending filler0 last fixed its 29 µs miss; that simply
  promoted filler1 into the early slot (28 µs miss).

### Best samples so far

- Fast trigger + **eight of ten roles spanning** (tails `1353–1793 → 2961–3175`, guards
  `1963/1966 → 2493/2436`, commit `0 → 8725`; canary/fillers failed only by not deferring).
- Latest: canary, filler0, guards, commit span a fast trigger at `2369 → 7604`; **filler1 misses by
  28 µs** (early slot).

### Current configuration

**(Superseded — see the Round-3 update above; the live current config is the one queued at the end
of [[handoff]] §14.)**

```
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=1000000 CIV_RELEASE_DELAY=1500000
CIV_CANARY_DELAY=2100000 CIV_GUARD_DELAY=700000 CIV_GUARD_SPREAD=0
CIV_FILLER_DELAYS=2200000,2100000      # next: both fillers late
CIV_TAIL_DELAYS=1600000,1700000,1800000,1900000
```

### Immediate next step (superseded — see the round updates above)

### Update — the client-order effect (2026-11-04)

The protected roles' windows close **in client-slot order, not send order**. Verified by moving the
canary's send from 1890 → 2075 us and watching it still close first, while filler0/filler1/guards/
commit closed in ascending slot order.

- The canary sat at slot 6 — the first protected slot — so it always closed ~30 us before the
  trigger's end. Swapping it to the last protected slot (`clients[6] ↔ clients[11]`;
  `CIV_CANARY_LAST=0` restores ns2jb's order) **fixed it**: canary `1863 → 7847` now spans.
- The failure then rotated to filler0 (slot 7, the new earliest). Expect one more rotation to fix
  it the same way.
- Slots 6-8 and 10-11 are all identical `saved` clones, so the swap is behaviour-neutral apart from
  drain order.

**Current best sample (5/10 spanning, fast trigger):** canary, filler1, guard0, guard1 and commit
all contain the trigger's window. The two remaining groups:

1. **filler0** — a ~33 us miss from the slot ordering (one more rotation).
2. **the four tails** — they did not defer in this sample, ending at ~2.1–2.3 ms instead of out to
   ~3 ms. Sending them into the busy period (`CIV_TAIL_DELAYS=2000000,2050000,2100000,2150000`,
   still ahead of the trigger's fixed 2377 start) is the queued fix.

Note the trigger's end is **not** strictly last: it came back at 7811 while the canary closed at
7847, so the freeing order is racy — which is why the target is "all protected ends after the
trigger's end" rather than a fixed slot layout.

Run the wave with both fillers late, one wave per payload load (fresh port per attempt):

```
cd research/CivSwitch/harness
python ../relocation/host/relocate_host.py 192.168.103.215 load &&
python ../relocation/host/relocate_host.py 192.168.103.215 mounts &&
python ../relocation/host/relocate_host.py 192.168.103.215 stop &&
CIV_HARNESS_PORT=9155 python host.py --host 192.168.103.215 load &&
CIV_HARNESS_PORT=9155 CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 \
CIV_PRE_CLONE=1 CIV_TRIGGER_SLEEP=1000000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2100000 CIV_GUARD_DELAY=700000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2200000,2100000 CIV_TAIL_DELAYS=1600000,1700000,1800000,1900000 \
  python host.py --host 192.168.103.215 worker-wave extent-present
```

Watch: the count of roles whose `endUs` exceeds the trigger's (it has been 6–8), and whether the
tails defer in the same sample.

---

## 3. Still to do

### Short term (this phase)
1. [x] `allPending` true — **achieved (round-16, 22:30; repeatable)** — see rounds 16–17 and the
       milestone artifacts.
2. [ ] `selectedMarker` true — trigger canonical `0` **with inline u32 `1`**. NOTE: the `0x2340`
       placement note below is superseded by [[handoff]] §28b — ns2jb's extent profiles place the
       basename FLUSH (extent − len); 0x2340 is the isPositive/directory placement, and trailing
       slashes after a file return 0x202. The code now defaults to flush placement.
3. [ ] Preserve the qualified run before touching overwrite profiles.
4. [ ] commit_data dirty write (16 MB, per wave) — queued; see [[handoff]] §28b.

### Medium term (`negative` → `positive`)
4. [ ] `negative`, then `positive` behind `CIV_ALLOW_OVERWRITE=1`, for the canary overwrite
       qualification (`canaryChanged`, `changedPaths`, and the `0x2eea02`/`0x2eec02` expectations).
       **POSITIVE HALF DONE (round-37, 2026-11-05):** `canaryChanged + markerSelected` TRUE on a
       windowed sample (`MILESTONE-stageA-canary-changed-positive-qualified-r37a2.json` — the
       full `:532-536` acceptance, trigger `0x2EEC02` ×4). `negative` remains, and the A–D alias
       chain (`leak-cycle.py full`) is unblocked on a fresh payload.
5. [ ] Reconcile reservation accounting (measured 12160–12176 vs ns2jb's asserted 12272) before
       trusting any automated `hasWindow`.

### Longer term
6. [x] ~~Payload-side in-process `retryWave`~~ **Achievable host-side (round-8):** the session +
       client caches give 3 waves per payload (ns2jb's own ceiling). The retry loop breaks early
       once a sample has `windowOk` (and, for extent-present, `selectedMarker` — fixed after
       round-23's boot stopped one attempt early).
7. [ ] `probeReadSize` sweep and the remaining profiles (`address-encoding-control`, `binary-control`,
       backslash control `0x2eec02`).
8. [ ] The actual homebrew launch path (retained memory / cache breakout → RO → launch), per
       `porting-roadmap.md` and `payload-guide.md`.

---

## 4. Dead ends and hazards (do not revisit)

- **Host-side re-reserve** (manager RPC driven from the host) — **freezes the console.**
- **Async IPC** (`svcSendAsyncRequestWithUserBuffer`, #0x23) — returns `0xCC01` for initialized
  fsp-srv at every size; no event. Post-driven concurrency is dead.
- **Runtime affinity SVCs** (`0x10`/`0x11`/`0x12`) — `0xE401`.
- **Never call `sdk+priority` (`+0x1370c0`) bare** — it takes a thread; doing so crashed the game.
- **Never read below a region's base** (reading `save_accessor − 0x2000` under base `0x35306e000`
  killed the game).
- **~~One wave per payload load, tries>1 hangs~~ SUPERSEDED (round-8):** the client/session caches
  give **3 attempts per payload** (ns2jb's own retryWave ceiling); the ~3-cycle limit comes from
  reservation-cycle lifetime, not the retry mechanism. A fresh `console.js` load on a fresh port is
  still cheap — but the loader stops evaluating after one payload, so it is effectively **one
  payload (3 waves) per console state**. (The original claim was itself a symptom of the
  close_object bug fixed in [[handoff]] §28b.)
- **±1 reservation object (58/60) crashes**; 59 is the only stable value.
- **"Ports open but silent" = console freeze** (needs a console restart; relaunching the game will not
  clear it). Distinguish from a game crash, where the console restarts normally.
### ★★ Round-34 (2026-11-05, 13:01) — FIRST RESOLVED-CANARY WINDOW: allPending + windowOk TRUE

Boot 14 (fresh session, one flow, 3 × extent-present) on the round-33-corrected config:
`RELEASE_DELAY=1500000` (round-24's frozen value — the boots-10–12 chase had inherited
900000), normalized resolved canary (0/type 1), gated at `OFFSET_NS=40000`, size 0x5a0,
`TRIGGER_TAIL=100000`, harvest delays. **Attempt 2: `allPending=True` AND
`windowOk=True` with `canaryType=1`** — the first all-span sample in the campaign where
the canary holds a **resolved real file** rather than the deep-form 0x202 missing path.
Preserved: `runs/MILESTONE-allSpan-windowOk-resolved-canary-r34a2.json`.

- The sign fix is confirmed live: gate start deltas −59/−60/−54 (predicted ≈ −55) across
  a deferred trigger (a1), mid (a2: 1175 µs, the milestone), mid (a3). The canary SPANS
  in all three attempts (endD +639/+802/+125) — the resolved walk (~2 ms) covers every
  trigger class; the "stretched regime is unwinnable" worry from boots 10–12 did not
  materialize at RELEASE_DELAY=1500000 (the 900000 release delay is the stretch's
  likely cause). Residual lottery: fillers/guards end-side only (a3), the round-14/15
  shape, secondary.
- **Stage A unblocked (SM0's queued next test):** `worker-wave positive 1` FIRST on a
  fresh payload (boot-14's payload spent: 1 setup + 3 waves = 4 reserve cycles; the
  4th+ wedge is the §45 open-but-silent freeze), same normalized env, via `flow.py`.
  Watch `canaryChanged` + the positive trigger's result/type at the resolved-canary
  geometry — a still-aborting `0x2EEA02`/`nativeGuard=0` is SM5's form-block verdict.
- Boot 13 (this session, pre-relaunch): never sampled — `relocate stop` reset mid-send,
  console.js refused on :9029 and a fresh :9210 (loader wedge; loader :9026 still
  answering = wedge, not freeze). New operational facts: a dead payload's port is a
  **black hole** (connects time out rather than refused); one full flow per game
  session is the safe payload budget. Recorded in [[gated-canary-plan]] boot 13.### ★★★ Round-35 (2026-11-05, 13:10) — Stage A's FORM ANSWERED: 0x2EEC02 accepted ×2, ALIAS LIVE, landed on guard1

Fresh payload (post-relaunch), positive wave on the boot-14 staging. Two waves before
the payload froze the console (2nd invocation's setup `open fsp-srv` timed out — the
documented freeze; **power cycle required**).

- **Wave 1 (13:10)**: `allPending=True` + `windowOk=True` **on positive** — first
  destructive-profile window qualification (fast 259 µs trigger). Trigger returned
  **`0x2EEC02`** — the ns2jb-REQUIRED result for non-extent waves (`:488`). Every row
  faithful (guards/fillers 0x202, canary 0/type 1, nativeGuard 0). `changedPaths=[]`.
  Preserved `MILESTONE-positive-wave-ns2jb-faithful-2eec02-r35a1.json`.
- **Round-32's `0x2EEA02` form-block is superseded**: per the reference's own encoding
  model (`:856-858`), 0x2EEA02 = form REJECTED, 0x2EEC02 = ACCEPTED. The boot-14
  resolved-canary staging flipped Civ's normalizer from rejecting to accepting the
  `/\..\` form. SM5's sequential form probe is answered by stronger evidence — closed.
- **Wave 2 (13:13): `changedPaths=['guard1']` — the alias LANDED** (guard1 result
  `0xFCEEEA02` ≈ 0x2EEA02-with-tick-residue, vs expected 0x202) — an aliased metadata
  copy hit a held path one slot off the canary. Exactly ns2jb's `metadataCanaryMiss`
  retry shape (`:480-483`). Canary intact. Preserved
  `MILESTONE-positive-alias-lands-guard1-r35b1.json`.
- **Stage A is now a landing-slot lottery, not a form problem.** The window held (r34a2,
  r35a1), the form is accepted (2/2), the alias is live — the remaining variable is
  which held-path slot the aliased copy lands on.

**Queued next boot** (per [[gated-canary-plan]] boot 15): (1) positive-wave lottery
harvest, `worker-wave positive 3` fresh flow; (2) if the alias stably lands on guard1,
slot-swap the canary into the preferred slot (`CIV_CANARY_LAST=0` / `CIV_SLOT_SWAP`,
verify vs abort-control first); (3) if landings scatter, SM3's alias-offset analysis
becomes live with a directly observable landing signal.### Round-36 (2026-11-05, 13:20) — positive windowOk ×2; retry-loop break bug fixed (host.py)

Boot 16 (fresh flow, `worker-wave positive 3`): wave 1 landed `allPending=True` +
`windowOk=True` **on positive for the second time** (fast 197 µs trigger), trigger
`0x2EEC02` (3rd consecutive accepted-form sample), canary resolved 0/type 1,
`changedPaths=[]`. **Landing tally after three positive draws: 1 landing (guard1,
13:13), 2 empty (13:10, 13:20).** No console damage — stopped after one wave by choice.

**Port bug fixed (`host.py:1609-1623`):** the retry loop broke on `windowOk` alone for
every profile except extent-present — ns2jb's `retryWave` for the overwrite profiles
keeps fishing until **`canaryChanged`** (the Stage A acceptance, `:532-536`). The bug
stopped boot-16's harvest after ONE of 3 draws (windowOk=True, canaryChanged still
open) and round-23's extent-present boot the same way. Now profile-faithful:
extent-present → `windowOk && selectedMarker`; positive/overwrite →
`windowOk && canaryChanged`; others → `windowOk`. Compile-verified.

**Next boot (queued):** relaunch (console healthy — no power cycle), fresh flow,
`worker-wave positive 3` with the fixed loop — three fresh lottery draws at the
boot-14-proven budget, stopping early only on Stage A success. Decision tree per
[[gated-canary-plan]] boot 15: stable guard1 landing → slot-swap the canary into the
preferred slot (verify vs abort-control); scattered landings → SM3 alias-offset dig with
the landing signal as the observable.### ★★★★ Round-37 (2026-11-05, 13:25) — STAGE A QUALIFIED: canaryChanged + markerSelected, complete positive acceptance

Boot 17 (fresh flow, `worker-wave positive 3`, **fixed retry loop**): wave 1 windowed
with no landing (`windowOk=True`, `changedPaths=[]` — the old break condition would
have stopped here); **wave 2 landed the alias ON THE CANARY** — preserved
`runs/MILESTONE-stageA-canary-changed-positive-qualified-r37a2.json`:

- `changedPaths=['canary']` (only), canary result `0xE5AEEA02`, `canaryType=0` — the
  held path overwritten, **the canary slot FREED in fsp-srv's heap**.
- `allPending=True`, `windowOk=True`, `timingValid=True`, margin 36,655 µs (≥300),
  nativeGuard 0/0/0, trigger `0x2EEC02` (4th consecutive accepted-form sample).
- **Verified against `probe-reserved-gap.cjs:532-536`: `canaryChanged=TRUE` AND
  `markerSelected=TRUE` — the complete positive-wave qualification.** Todo #4's
  positive half (tracker §3) is done; the destructive stage the campaign has hunted
  since round 16 is qualified by a live windowed sample.

**The retry-loop port fix (host.py:1609–1623, this session) is the direct enabler:**
wave 1's `windowOk=True` would have broken the old loop (same shape as round-23's
extent-present stop and boot-16's harvest stop); wave 2 is the draw that landed.
Landing tally across the session's 5 positive draws: canary 1, guard1 1, empty 3 —
a real per-draw lottery, harvestable at 3 draws/payload by the fixed loop.

**Consequence — the A–D alias chain is fully unblocked** (§43/§44):
Stage B (candidate prime allocating the cache table inside the freed slot), Stage C
(`installCombinedLeak`, 7 copyField writes), Stage D (over-read 0x1c000 → heap bytes
past 0x4000) — all scaffolded in `leak-cycle.py full`, whose Stage A rides the fixed
`worker_wave`. **One leak-cycle run per payload** (§45): this payload has spent setup +
2 waves — relaunch required. Queued: `leak-cycle.py full` (`CIV_ALIAS_UPTO=read`) on
the fresh payload; if Stage A misses all 3 draws, relaunch and re-fish.

**Docs:** the milestone, the gate verification table, and the landing tally are
recorded in [[gated-canary-plan]] boot 17. selectedMarker for extent-present
(tracker todo #2, type-1 trigger resolution) remains the other open gate — the
positive qualification here does not answer it (different profile, different fold).### Round-38 (2026-11-05, 13:29–13:33) — leak-cycle Stage A live in-chain; boot 19 CRASH validates the destructive-stage discipline

- **Boot 18** (`leak-cycle.py full`, `upto=positive`, 2 markers, `CIV_PREREQ_SKIP=1`,
  empty `CIV_RELEASE_CLOSE_GATE`): stage 0 clean (no strings/candidates), Stage A's
  in-chain integration works — two fully windowed draws (`AP/win True`), no landing
  (`canaryChanged=False` ×2). Clean exit. **Landing tally: canary 1 (r37), guard1 1,
  nowhere 7 → ~1/9 per draw.**
- **Boot 19** (`upto=prime`, 3 markers): **crashed mid-wave-1 readback** — the payload
  went silent during row extraction (`host.py:1963` read timeout; console pinged, loader
  answered, `:9029` open-but-silent = the freeze). No JSON, no `positive[marker=...]`
  line. **Reading: the landing hit payload memory** (the wave's request/reply pages are
  payload heap) — a landing class `changedPaths` cannot see by construction. Tally:
  canary 1, guard1 1, nowhere 7, payload-fatal 1. Power cycle required.
- **Discipline note:** ns2jb gates `positive` behind extent-present's full
  `selectedMarker && hasWindow` (`:616`) and treats failed placements as
  stop-and-recover — we have been running positive on `windowOk` alone (todo #2
  open). The crash is that exact case. Decision recorded in
  [[gated-canary-plan]] boot 19: (1) re-drive once more — a second mid-readback
  crash makes the payload-page landing systematic and needs a mitigation design;
  (2) or close todo #2 first (SM3/SM6: downward marker sweep + address-planner on
  the measured geometry) so the destructive stage is properly gated.### Round-39 (2026-11-05, 13:44–13:52) — option (c) chosen and CONFIRMED: stage-0 chain survives; lottery dry 2 boots

- **Boot 22** (`upto=prime` WITH stage 0): **second mid-readback payload-fatal** (draw 1,
  same death point as boot 19) — the pre-agreed trigger fired; stopped per the
  decision tree. Tally: canary 1, guard1 1, filler0 1, payload-fatal 2, nowhere 7.
- **User decision: option (c)** — one chain boot without stage 0 (the confound test),
  with the follow-up direction "extend to more samples per payload" (the ns2jb-method
  angle). `CIV_STAGE0_SKIP=1` implemented (`leak-cycle.py`; stage B GetSize inline via
  `packet(4)` when stage 0 is skipped).
- **Boot 23** (stage-0-free): **3/3 draws survived, zero crashes** — all windowed
  (`AP/win True`), all landed nowhere. **Confound SUPPORTED: the stage-0 leak-read
  preamble's allocations are what shifted the landing into payload memory** (with
  stage 0: 2 fatals/2 boots; without: 0 fatals/3 draws). **The chain is now drivable
  without stage 0 — drive it that way until Stage B lands.**
- **Landing tally (15 draws): canary 1, guard1 1, filler0 1, payload-fatal 2
  (both with stage 0), nowhere 10.** Canary ≈ 7%/draw, ~19%/boot; two dry boots.
- **Samples-per-payload analysis (offline, recorded in [[gated-canary-plan]] boot 23):**
  the 4th reserve cycle dies at `rereserve` (open-but-silent). Three candidate
  mechanisms in diagnosis order: fsp-srv session accumulation (wedge point identifies
  the table), payload heap fragmentation (dies at `59 manager objects created`), manager
  RPC object-cap (dies at the CreateObject batch). Lever follows the diagnosis — but
  do not burn a boot on it while 3 draws/boot keeps fishing Stage B's prime gate.
- **Next drive (unchanged, stage-0-free):** relaunch → `leak-cycle.py full`
  (`CIV_STAGE0_SKIP=1`, `upto=prime`, 3 markers). If a draw lands, Stage B's prime
  assert answers "is the cache table inside the freed canary slot" in the same payload.### Round-40 (2026-11-05, 13:57) — 3rd dry boot; lottery cost now exceeds the samples-per-payload diagnosis

- **Boot 24** (stage-0-free chain, `upto=prime`, 3 markers): 3/3 draws survived, all
  windowed, all landed nowhere. Stage-0-free chain now **9/9 crash-free** (b23/24) vs
  2 fatals in 5 chain-with-stage-0 draws — the confound verdict holds.
- **17 draws total: canary 1, guard1 1, filler0 1, payload-fatal 2, nowhere 12 →
  ~6%/draw, ~17%/boot.** Three consecutive dry boots (b21/23/24). P(0-in-9) ≈ 58% —
  not anomalous, but each Stage B miss now costs a game relaunch, and the expected
  lottery cost (~1 canary landing per ~6 boots) exceeds one diagnosis boot.
- **Decision: the next boot is the 4th-rereserve wedge diagnosis** (the user's
  samples-per-payload / ns2jb-method direction): `worker-wave positive 6` with
  `CIV_RESERVE_TRACE=1` — it dies at cycle 4 by §44, and the trace line identifies the
  mechanism: `open counters session`/`CloneSession` = fsp-srv session accumulation
  (lever: close-on-release), `59 manager objects created`/`wrapped` = payload heap
  fragmentation (lever: alloc reuse), `open owner domain` = manager RPC state (lever:
  interleaved cleanup). Whichever line dies is the lever to build — doubling
  draws/boot halves the lottery's expected cost.
- **Do not re-drive the plain lottery while the diagnosis is pending.** The chain
  command (stage-0-free, `upto=prime`) stays queued for after the lever lands.### ★ Round-41 (2026-11-05, 14:04) — 4th-rereserve wedge REFUTED: 6/6 cycles survived; `tries` IS the samples lever

Boot 25 (the recorded wedge diagnosis, `worker-wave positive 6`, `CIV_RESERVE_TRACE=1`,
fresh flow): **all six reserve cycles survived** — setup + 6 waves = 7 cycles, ~21 s/draw,
zero freeze, clean exit. The §44 "~3-cycle payload lifetime" did not reproduce; the
recorded wedges were context-specific (round-19's tuned 4th, §17 pre-client-cache
freezes, second-SETUP invocations — not a 4th rereserve). **The user's
samples-per-payload direction needs no engineering: `tries=6` works.**

- 6 draws: 5 windowed (fast class 138–254 µs), 1 mid-class miss (a5, 1223 µs) that
  landed on **filler1** — the 4th distinct landing target (canary/guard1/filler0/filler1
  all hit once each: pure scatter, SM3's random-landing model confirmed).
- **Tally (23 draws): canary 1, guard1 1, filler0 1, filler1 1, payload-fatal 2
  (stage-0 chain only), nowhere 17 → canary ≈ 4.3%/draw → ~23%/boot at 6 draws.**
- **Next drives: 6-marker chain boots** (`CIV_ALIAS_MARKERS=0x2340×6`,
  `CIV_ALIAS_MARKERS_MAX=6`, `CIV_STAGE0_SKIP=1`, `upto=prime`) — one relaunch per
  boot, ~2.5 min, until a draw lands, then Stage B's prime assert in the same payload.
- Console healthy after 7 cycles; do not re-invoke on this payload (the §43
  second-setup wedge remains the documented freeze class).### Round-42 (2026-11-05, 14:06) — 6-marker chain boot: wedge is VARIABLE (cycles 3–7); policy = relaunch-per-wedge

Boot 26 (first 6-marker chain harvest, stage-0-free, `upto=prime`): draw 1 windowed dry,
draw 2 dry, **3rd rereserve wedged** (`open counters session` RPC timeout — game alive,
payload dead). Boot 25 ran 6/6 clean on the same family — **the §44 "~3-cycle" ceiling
was a small-sample artifact; the real wedge point varies, cycles 3–7, no repeat point
across boots**.

- **Tally (25 draws): canary 1, guard1 1, filler0 1, filler1 1, payload-fatal 2
  (stage-0 chain), nowhere 19 → canary ≈ 4%/draw.** Four distinct landing targets
  (scatter confirmed at volume).
- **Harvest policy (recorded in [[gated-canary-plan]] boot 26):** expected draws/boot
  2–6; a wedge kills the process, not the boot — relaunch and re-drive the same
  6-marker command; the Stage A acceptance breaks the marker loop early on a landing.
  **Do not spend boots on the wedge mechanism until a canary landing + Stage B
  prime pass/fail is in hand** — the wedge throttles throughput only; the r37
  acceptance proves the full landing→release path works.
- Queued: relaunch → same 6-marker chain (`CIV_ALIAS_MARKERS=0x2340×6`, `MAX=6`,
  `CIV_STAGE0_SKIP=1`, `upto=prime`) until a canary draw lands → Stage B prime assert
  (`prime.bytes == control prefix`) in the same payload.### Round-43 (2026-11-05, 14:16) — 6/6 clean, 0 canary (31 draws); landing bias → fillers 4 : canary 1

Boot 27 (second 6-marker chain harvest): all six draws ran clean (no wedge — the
variable-ceiling model holds: b25 6/6, b26 wedge@3, b27 6/6), draws 1/4 landed on
**filler1/filler0**, rest dry. Console healthy.

**Tally (31 draws): canary 1, guard1 1, filler0 2, filler1 2, payload-fatal 2, nowhere
23 → canary ≈ 3.2%/draw.** Landings-on-somewhere 8/31, but **fillers take 4 of 8** —
the aliased copy's landing region favors the filler packets' pages ~4× over the
canary's. Not yet anomalous for the chain's 0-canary streak (P ≈ 46% at 3%/draw), but
the bias is measurable.

**Queued (per [[gated-canary-plan]] boot 27):** continue 6-marker boots; if two more
go canary-dry (≥43 draws at 0), run one `CIV_SLOT_SWAP=6,7` boot (canary↔filler0, the
round-13 swap machinery) — a canary landing under the swap = landing-follows-client
(odds lever found); a filler landing = landing-follows-staged-page (SM3 staging
geometry). One boot, both outcomes progress. Stage B prime stays gated on a canary
landing in the landing payload.### Round-44 (2026-11-05, 14:21) — landing-hot boot (5 events/6 draws, first DOUBLE); swap experiment due

Boot 28 (third 6-marker chain harvest): 6/6 clean, 0 canary, **first double-landing**
(draw 6: `changed=['filler1','guard0']` — the aliased write scatters ≥2 metadata copies
per wave). **Tally (37 draws): canary 1, guard1 1, filler0 2, filler1 3, guard0 3,
payload-fatal 2, nowhere ~25 → landing events 12, canary 1/12 (8%), fillers+guards
10/12 (83%).** Chain context: ~25 draws, 0 canary.

**The boot-27 policy trigger fired** (two canary-dry boots: b27, b28): the queued
one-variable experiment is the `CIV_SLOT_SWAP=6,7` boot (canary↔filler0 client slots,
round-13 machinery) on the same 6-marker chain command. Canary-lands ⇒ 
landing-follows-client (odds lever: keep the swap, ~10–15%/draw). Filler-lands ⇒
landing-follows-staged-page (lever: staging geometry, SM3 territory). Either outcome
converts the lottery structural. Recorded in [[gated-canary-plan]] boot 28.

**Relaunch + drive** when ready: the swap boot is the next command (one variable:
`CIV_SLOT_SWAP=6,7` on the frozen 6-marker chain config).### Round-45 (2026-11-05, 14:29) — swap inconclusive by DESIGN: scatter is UNIFORM; keep rolling, stopping rule set

Boot 29 (`CIV_SLOT_SWAP=6,7`, 6-marker chain): 6/6 clean, 0 canary, 1 landing on
**guard1** (untouched by the swap). Aggregate re-analysis across all 13 landing
events: **χ² ≈ 2.2 (p≈0.5–0.7) — uniform over the 5 held paths.** The boot-27/28
"filler bias" is downgraded (dissolved at n=13); the swap's premise (a disfavored
slot) was wrong, so its inconclusive result was expected. **Swap REVERTED.**

- Landing-somewhere ≈ 30%/draw; canary ≈ 6%/draw under uniform (observed 1/43 draws —
  Poisson noise). The chain's 0-canary streak (~31 draws) is P ≈ 15% — unlucky, not
  anomalous. **Statistically honest answer: keep rolling the frozen 6-marker harvest.**
- **Stopping rule:** two more canary-dry boots (→ 43 chain draws at 0, P≈7%) kills
  "unlucky" — then the lever is SM3 staging geometry (steer the ~30%/draw scatter by
  where staged paths sit in the heap), not client slots.
- Recorded in [[gated-canary-plan]] boot 29. Next: relaunch → unchanged 6-marker chain,
  6 draws/boot; first canary landing → Stage B prime in the same payload.### Round-46 (2026-11-05, 14:41) — STOPPING RULE FIRED: chain 0-canary streak (~37 draws); pivot to context control + SM3

Boot 31 (dry boot 2 of 2): 6/6 clean, 0 canary, landings filler0/guard0×2. **The
boot-29 stopping rule fired: no more unchanged chain boots.**

**The decisive cut — landing by context:**

| context | draws | canary | somewhere |
| --- | --- | --- | --- |
| standalone `worker-wave positive` | ~13 | **1** (r37a2) | 2 |
| chain (`leak-cycle full`) | ~37 | **0** | 11 |

The chain has hit every held path except the canary (11 events: filler0×2, filler1×3,
guard0×5, guard1×1). P(0-in-37 | 5.6%) ≈ 5–7%; the standalone/chain asymmetry (1/13 vs
0/37) suggests the chain's own per-draw allocations shift the landing geometry away
from the canary's slot. **Pivot (recorded in [[gated-canary-plan]] boot 31):**
1. **Context control first (one cheap boot):** standalone `worker-wave positive 6`
   (the b25 shape) — a canary landing there (~40%/boot at observed rates) proves the
   context effect and re-points the chain's Stage A at the standalone draw shape
   (client/session reuse, no rereserve-per-draw).
2. If confirmed or ambiguous → **SM3 staging-geometry analysis** (alias-offset
   planner on the measured 12160 geometry) to bias the ~30%/draw scatter structurally.

Chain scaffolding (B/C/D) unchanged and ready; only Stage A's draw shape is in
question. **No more unchanged chain boots** until the context control runs.### Round-47 (2026-11-05, 14:49) — context control NEGATIVE (standalone 0/6); verdict: uniform-unlucky; rolling resumes

Boot 32 (standalone `worker-wave positive 6`, the b25 shape): 6/6 clean, 0 canary,
guard1 landing on d6. **The context asymmetry hypothesis died** — standalone also
misses. Fisher exact 1/19 vs 0/37: p≈0.33 (not significant). Combined streak: 56 draws,
1 canary vs λ≈3.1 expected — **P≈13%, unlucky-not-anomalous** (the round-46 stopping
math overcalled it; b32's data regressed the streak to noise).

**Revised policy (recorded in [[gated-canary-plan]] boot 32):**
1. **Resume rolling chain boots** — ~3.4 expected boots per Stage A landing at the
   uniform 5.6%/draw, ~3 min/boot, Stage B auto-follows in the landing payload.
   The chain is the only context Stage B can run in.
2. **Hard stop rule:** THREE more canary-dry chain boots (~55 chain draws at 0,
   P≈4%) → stop rolling, go fully structural (SM3 + re-staging).
3. **Offline stream:** SM3's landing-offset analysis of the 14 landing events
   (result-word clobbers in `runs/worker-wave-positive-attempt*.json` + milestones) —
   no console required; feeds any future re-staging decision.

Console healthy after b32's 7 cycles. Relaunch → unchanged 6-marker chain command.### Round-48 (2026-11-05, 15:04) — HARD STOP FIRED (dry 3/3); fold HOT every draw; pivot to staged-address swap

- **Boot 35** (frozen 6-marker chain): 6/6 clean, 0 canary, filler0 landing d3 — **dry
  chain boot 3/3: the boot-32 hard stop rule FIRED. No more unchanged chain boots.**
- **Fold-hot/cold check:** boot 32 standalone 6/6 HOT + boot 35 d6 HOT — **the fold
  fires every observed draw; the landing-rate-collapse theory is dead.** The aliased
  write is live each draw; it is the landing SLOT that misses the canary.
- **Final tally (~50 draws): 17 held-path landing events — canary 1 (6%) vs
  fillers+guards 16 (94%).** Uniform-over-5 borderline-rejected (p≈0.08–0.12): the
  landing region is biased away from the canary's staged page. Client slots (b29 swap)
  and timing (window holds every draw) are both excluded — **the lever is WHERE the
  canary's path text is staged in the payload heap.**
- **Diagnostic footgun recorded:** attempt JSON copies are CLI-path-only;
  `leak-cycle.py` never writes them — chain boots leave the last standalone run's
  attempt files on disk. Check mtimes against the boot window before attributing.
- **Pivot (designed, recorded in [[gated-canary-plan]] boot 35): staged-address swap** —
  canary path text at a filler's packet-tail page, filler text at the canary's page,
  clients/delays/gates identical; env-gated in `host.py`, offline-verifiable. If canary
  landings rise to the filler rate (~60%/boot), Stage A is structural and the chain
  resumes B/C/D; if not, the dig goes to SM3's planner proper (FS-side allocation
  order). Implementation is offline — next session item, no console required until the
  validation boot.### Round-49 (2026-11-05, ~15:15) — `CIV_STAGE_SWAP` implemented + offline-verified; validation boot queued

The round-48 pivot is implemented: `CIV_STAGE_SWAP=x,y` in `host.py` —
`stage()` records payloads, `swap_staged_pages()` rewrites each entry's text into the
other's page (padded to its own descriptor size) and exchanges the entry addresses;
invoked in `setup()` after staging, before verification. **Offline-verified
byte-faithful** (truthful-page fake console): every byte through each path's NUL
terminator identical pre/post-swap; only alloc-residue past the terminator differs
(ignored by resolution); addresses exchanged, sizes preserved; wire selftests pass
unchanged in default mode.

**Validation boot (queued, recorded in [[gated-canary-plan]]):** frozen 6-marker chain
+ `CIV_STAGE_SWAP=canary,filler0`. Canary landings rise ⇒ landing follows the staged
page's heap address — **Stage A structural, swap stays, chain resumes, Stage B
auto-follows**. Landings stay on fillers/guards ⇒ landing follows FS-side allocation
order — **SM3's planner owns it**, swap recorded as a dead end. One boot decides.### Round-50 (2026-11-05, 15:11) — stage-swap validated: NO effect — and the boot was underpowered to show one; every single-boot discriminator on this question is stuck at the same floor

- **Boot 36** (`CIV_STAGE_SWAP=canary,filler0`, frozen 6-marker chain, swap active):
  6/6 clean, landings filler0/filler1/guard0, **canary 0/6**. No effect observed —
  consistent with the role-following/FS-side model (the aliased write clobbers the FS's
  server-side path copy, which the payload-heap swap cannot move).
- **Power analysis (recorded in [[gated-canary-plan]] boot 36):** both models predict
  0–1 canary hits in 6 draws (P(0) = 57% page vs 74% role). Boot 29's client-slot test
  is stuck at the same floor. **Distinguishing 5%→15% needs ~30 draws (~5 boots).**
  No single boot can decide this question — recorded as the standing caveat on all
  landing-geometry experiments.
- **Swap REVERTED** (no benefit shown; no config drift into rate measurements).
- **The fork (recorded):** (1) volume — a matched 5-boot × 6-draw WITH-swap campaign
  against the existing ~44-draw WITHOUT-swap control (≥2 canary landings ⇒
  page-following; 0–1 ⇒ FS-side confirmed, SM3 planner owns it); or (2) mechanism —
  the SM3 offline dig: model the FS-side path-copy layout from the reservation shape +
  dispatch order (the wave fixes both; no boots needed to model, one boot to validate
  a re-staging prediction). **Recommendation: mechanism first** — zero boots, and a
  confirmed layout model yields a structural re-staging design.
- Console healthy; payload spent (setup + 6 waves). Stage A remains the sole gate;
  Stage B/C/D scaffolding unchanged and waiting.### Round-51 (2026-11-05, ~15:20) — SM3 DIG COMPLETE: the landing law (freed holes, window anti-correlation); size-class lever queued

The offline dig compiled every positive-wave draw (~64, fatals excluded) and found the
mechanism the landing lottery was hiding:

- **P(landing | non-windowed) ≈ 85%; P(landing | windowed) ≈ 4%.** Non-windowed draws
  (slow trigger overlapping the queue drain) free held-path copies mid-fold; the
  aliased write lands in/near the freed holes and clobbers NEIGHBORS (b32a6: guard0
  freed, guard1 hit). Windowed draws have no holes → the write misses.
- **Stage A's rate is now exact:** windowed ∧ canary-hit ≈ 2%/draw (observed once in
  ~45 windowed draws: r37a2). The "6%-uniform lottery" model is superseded.
- The b29/b36 swap nulls are explained: the landing rides the server-side copy's
  DECLARED SIZE CLASS + freed-hole adjacency, not the client handle or payload page.
- **H1 lever queued:** `CIV_CANARY_SIZE=0x7f0` (declared 0x7f1 = the fillers'/trigger's
  2032/2048 class) → predicted windowed canary-hit rise toward filler rate; Stage B
  unaffected (bigger freed slot fits the 0x5a0 bucket). Fallback if the 2032-char
  chain fails at setup: deep-form canary at declared 0x7f0. Validation = one boot,
  frozen 6-marker chain, single variable. If null at n=6 (P(0)≈53% — weak), the next
  structural idea is the deliberate-hole wave design (a sacrificial early-completing
  14th role adjacent to the canary's class).
- Full write-up: [[gated-canary-plan]] SM3 DIG section. recorded 2026-11-05.### Round-52 (2026-11-05, 15:21) — H1 null-observed (underpowered, n=2 windowed draws); law 21/23; deliberate-hole design is next (offline)

Boot 37 (`CIV_CANARY_SIZE=0x7f0`, only variable): 2032-char chain created 62/62, canary
resolved/gated every draw, 6/6 clean, **canary 0/6** — but only 2 windowed draws occurred
(both dry): P(0 | filler-rate, n=2) ≈ 81%, a weak null. **The rate-measurement route to
H1 is closed** (~15+ boots for power). Landing law re-confirmed: 4/4 events this boot on
non-windowed draws, **~21/23 campaign-wide**.

**Next (recorded in [[gated-canary-plan]] boot 37): the deliberate-hole wave design —
OFFLINE first.** A 14th sacrificial role (not in `held_paths`, so `allPending` ignores
it) completing early frees its server-side copy mid-fold WHILE the window holds; staged
at the canary's 0x7f1 class, allocated adjacent → the freed-hole landing channel (~85%)
becomes aimed at the canary inside windowed draws. Stage A converts from a 2%/draw
lottery to an engineered event. Implementation: host.py (14th role staging/slot/delay/
gates), offline-verified, then one validation boot. **No more lottery/H1 boots.**### Round-53 (2026-11-05, ~15:35) — deliberate-hole role IMPLEMENTED + offline-verified; validation boot queued

The round-52 design is code: `CIV_SACRIFICE=1` adds role 13 `sacrifice0` — a missing
normalized path at the canary's exact declared class (0x7f1, staged 2032 chars,
terminator covered), own saved clone at slot 13, plan = sleep(1.2 ms default) + send
only, excluded from `held_paths`/`protected_roles`/nativeGuard — the window is
untouched by construction. Mechanism: its fast 0x202 reply frees the 0x7f1-class
server-side copy BEFORE the canary's gated dispatch (LIFO reuse puts the canary's copy
IN the hole), aiming the ~21/23 freed-hole landing channel at the canary inside
windowed draws. **Offline-verified:** compile, staging geometry (2032 chars, 0x7f1
declared), packet build, default-mode wire selftests identical. Design + validation
command + decision tree in [[gated-canary-plan]] (Deliberate-hole design section).

**Next boot:** the frozen 6-marker chain + `CIV_SACRIFICE=1` (canary stays 0x7f0 —
classes must match). ≥1 windowed `canaryChanged` ⇒ Stage A structural, chain resumes
B/C/D. 0/6 ⇒ weak null → one more boot at `CIV_SACRIFICE_DELAY=800000` before judging.
No more lottery/H1 boots — the aim design is the only open Stage A question.### Round-54 (2026-11-05, 15:32) — sacrifice validation: premise failed (mid-busy sends defer; hole frees after fold); DELAY=0 variant queued

Boot 38 (`CIV_SACRIFICE=1`, canary 0x7f0): 6/6 clean, 5/6 windowed, landings filler1
(d1, non-windowed), **canary 0/6** (weak null, P≈17%). **The sacrifice row's own
timing refuted the design's premise:** sent 1017 µs → serviced 3579 µs — the
mid-busy send **deferred** (the wave's own mechanism), its 0x202 reply marking
service ~1 ms AFTER the trigger's fold (2470→2600). No hole existed to aim. The
queued 800000-delay variant is invalid by the same evidence (any post-0.3 ms send
defers).

- **Revised validation (queued): `CIV_SACRIFICE_DELAY=0`** — sent at t=0 racing the
  hole's read arrival, the miss reply services ~0.2–0.4 ms: a stale 0x7f1 hole before
  the tails dispatch. Outcome A: fillers' 0x7f0 copies take it (same bin) → aim
  degraded; Outcome B: bins differ → the hole survives to the canary's live copy →
  windowed `canaryChanged` possible. One boot, both outcomes informative.
- **Deeper (recorded): the b32a6 fresh-hole channel (freed DURING the trigger's
  service, hits the neighbor) is the observed ~21/23 channel; DELAY=0 tests the
  stale-hole form. If stale holes die to same-bin reuse, the full aim design needs the
  fresh-hole form (sacrifice on an early client slot + deferral — a bigger redesign).**
- Boot 37/38 config note: canary 0x7f0 stayed (classes matched); 2032-chain creation
  worked (62/62); gate/window healthy at the new size.

Console healthy after 7 cycles; payload spent. Full timing table + reasoning in
[[gated-canary-plan]] boot 38.### Round-55 (2026-11-05, 15:38) — delay route DEAD (t=0 defers too); drain-order design queued (b32a6 evidence)

Boot 39 (`CIV_SACRIFICE_DELAY=0`): 6/6 clean, canary 0/6, landings guard0×2/filler1
(non-windowed). **Decisive: sacrifice sent at 0.0 µs serviced at 3408 µs** — after the
trigger (2823). Even t=0 sends queue behind the hole's read: **no pathPacket completes
early inside a wave at any delay** (b38: 1.2 ms→3579; b39: 0→3408). The delay knob is
closed.

**What remains is drain-order** — b32a6's channel: guard0@slot-10 serviced DURING the
trigger's window (3439 < 3507) → fresh hole during the fold → hit on the drain-adjacent
guard1. **Queued design (one boot): sacrifice takes slot 10 (swap with guard0's
identical saved clone), guard0 takes 13** — post-CANARY_LAST drain order
guard1(6) → **sacrifice(10)** → **canary(11)** → trigger(12): the sacrifice frees its
0x7f1-class copy immediately before the canary's dispatch, the canary's live copy
allocates into the freed hole (LIFO), and the fold's freed-hole channel aims at the
canary's region inside windowed draws. Honest caveat recorded: one b32a6 event,
adjacency model ambiguous (client-slot vs allocation order) — the boot tests the
strongest form and refines the model either way. Full design + caveats in
[[gated-canary-plan]] boot 39.

Console healthy after 7 cycles. Next boot: the slot-swap sacrifice validation.### Round-56 (2026-11-05, 15:43) — SLOT_SWAP null; STRUCTURAL INSIGHT: windowed draws have no holes at fold time — Stage A rides the live-copy channel

Boot 40 (`CIV_SACRIFICE=1` + `CIV_SLOT_SWAP=10,13`): 6/6 windowed (highest rate yet),
0 landings, 0 canary; sacrifice serviced 5105 µs (post-fold — the handle swap does not
move service position; round-13's law covers drain order, not this). Also: a
CIV_SACRIFICE_DRAIN roles-swap was implemented and reverted pre-boot (round-13 evidence
makes it equivalent to the existing SLOT_SWAP).

**The insight that closes the design line:** a windowed draw IS a fast trigger
resolving before the queue drains — **no freed hole can exist at fold time on a
windowed draw**. The ~85% freed-hole channel (non-windowed) is structurally unavailable
to Stage A; **Stage A's channel is the live-copy scatter** (~4%/windowed-draw; the two
windowed hits: r37a2 canary, b36d3 filler1). Deliberate-hole designs (delay/drain/slot)
all aim the wrong channel — 3 boots, 3 nulls, one law explains them.

**Fork (recorded in [[gated-canary-plan]] boot 40):** (1) resume the plain 6-marker
lottery (~15–20%/boot, ~6 boots expected per landing, Stage B follows each landing) —
r37a2 proves the channel reaches the canary; (2) **SM3 offline as primary** (model the
live-copy layout from the 2 windowed hits + 12 freed-hole residues → re-staging
prediction or volume-only verdict); (3) ns2jb-diff archaeology (their wave landed
reliably; the staging diff is offline). **Recommendation: pause boots, run 2+3
offline** — the lottery's cost is high against two unmined threads. Console healthy;
payload spent. Stage A is the sole remaining gate; window, Stage B, C, D scaffolding
all proven.### Round-57 (2026-11-05, ~16:00) — SM3 + ns2jb-diff dig COMPLETE: the missing per-wave 16 MB dirty write found; todo #4 implementation queued

The requested double-back paid off decisively. Full findings in
[[gated-canary-plan]] (SM3 + ns2jb-diff dig section):

- **ns2jb runs a per-wave heap-shaping step our port never implemented** (tracker
  todo #4, queued since round-18b): setup creates `scratch/commit_data` at 16 MB
  (CreateFile cmd 0 + size arg); **every wave** opens it (cmd 8, mode 3), **writes 16
  MB dirty** (cmd 1 + send descriptor), closes — THEN runs the 13-role wave. Their
  stated purpose: the commit role's `packet(10)` has flush work. The heap
  consequence: the FS journal/allocator processes 16 MB of dirty data before every
  wave — **the regime the fold's aliased write lands in differs from ours.**
- **Other diffs:** their commit packet is bare `packet(10)` (ours adds a send
  descriptor — likely compensation for the empty journal); their staging uses fixed
  offsets in one pre-mapped region (ours: fresh alloc pages).
- **SM3 status:** the live-copy channel (~4%/windowed-draw, 2 events: r37a2 canary @
  874 µs mid-trigger, b36d3 filler1) cannot be modeled further **until the regime
  confound is removed** — all our landing-rate data was measured without the
  reference's heap shaping. The dirty write is plausibly WHY SmileBASIC's waves land
  in the planner's expected region while ours scatter.
- **Queued:** `CIV_COMMIT_DATA=1` implementation (setup CreateFile + per-wave
  open/write/close, offline-verified, then one validation boot on the frozen 6-marker
  chain). This closes todo #4 and unblocks SM3's rate model.

Console idle since boot 40 (healthy, payload spent). Next session item: the dirty
write implementation + validation boot.### Round-58 (2026-11-05, ~16:56) — ★ DIRTY WRITE WORKS (todo #4 CLOSED): 16MB file confirmed, 1MB mode-1 writes, 16×1MB chunks

Boot 47 (final ladder, fresh boot): **all six waves DIRTIED** — the per-wave
commit_data write works. Full archaeology closed ([[gated-canary-plan]] boot 47
table): the file is **16 MB** (never empty — the cmd-2 Flush "fileSize 0" was
garbage); the 16 MB single-shot is **rejected and session-poisoning** (boot 42's 1 MB
failed only after it); **the working form is OpenFile(mode 3) → 16 × 1 MB mode-1
writes → raw svc closeHandle** (ns2jb's own s.close). SetSize unneeded.

- Boot-47 loop-count bug found in-run (probe success skipped 15 chunks — each
  "16MB" wave dirtied ONE 1MB): fixed + offline-verified (16 contiguous 1MB writes
  = exactly 16MB). Boot 47's waves ran at 1/16 regime strength.
- **Todo #4 (tracker §3, "commit_data dirty write 16 MB per wave") CLOSED** — the
  machinery is live; the next boot runs the waves under the reference's full heap
  regime for the first time.
- Waves at 1 MB strength: 5/6 windowed, guard0 ×1, canary 0 — regime read pending
  full strength.

**Next:** relaunch → same command → the TRUE 16 MB-regime harvest (the landing
distribution is the round-57 question: does canary's ~4%/windowed-draw share shift
under the reference's heap regime?). Then Stage A odds are honest — lottery resumed
informed, or SM3 layout model with data.### Round-59 (2026-11-05, ~17:05) — ★ MACHINERY COMPLETE: full 16 MB dirty regime live ×6; Stage A is pure statistics now

Boot 48 (fixed loop): **6/6 waves DIRTIED at full strength** — 16 contiguous 1 MB
mode-1 writes per wave, verified `fileSize 16MB` via the real GetSize(4), raw
svc closeHandle (ns2jb's form). First full-regime harvest: 4/6 windowed, landings
guard1/filler0, **canary 0/6** — indistinguishable from the old regime at n=1 boot.

**The implementation arc (rounds 57–59) is complete:** the port now performs every
ns2jb per-wave step — CreateFile 16 MB, per-wave OpenFile → 16 MB dirty Write →
Close, the 13-role wave, gates, chains, caches, the per-profile retry loop, and the
B/C/D scaffolding. Full table in [[gated-canary-plan]] boot 48.

- **Stage A is now pure statistics:** ~4%/windowed-draw live-copy channel, 4–6
  draws/boot; every future lottery boot doubles as the dirty-regime rate measurement.
  A canary landing runs Stage B's prime assert in the same payload; B/C/D otherwise
  complete.
- Boot 47/48's archaeology summary: 16 MB single-shot = rejected + session-poisoning
  (never re-send it); working form = 16 × 1 MB mode-1; GetSize(4)/SetSize(3)/Flush(2)
  per libnx ground truth; close = raw svcCloseHandle (close_object closes domain
  OBJECT ids — never raw session handles).
- Console healthy; payload spent (setup + 6 waves). **Relaunch → same command resumes
  the informed lottery under the reference's regime.**### Round-60 (2026-11-05, ~17:35) — LOTTERY SESSION (boots 48–52): 1 MB dirty regime stable; session close

- **Boot 48** (16 MB loop fixed): 6/6 DIRTIED, 4/6 windowed, landings guard1/filler0.
- **Boot 49**: 6/6 DIRTIED, 4/6 windowed, landings guard0/filler1.
- **Boot 50** (accidental control — setup CreateFile failed silently): 6/6 windowed,
  0 landings, UNDIRTIED.
- **Boot 51** (self-heal found the cause): **`recreate: 0x3C00` = OutOfSpace — the
  16 MB file cannot exist on Civ's ~62 MB save.** ns2jb's 16 GB target vs our 62 MB:
  the faithful port is "sized to the free margin." `COMMIT_WRITE_SIZE` → **1 MB**.
- **Boot 52** (1 MB): **6/6 DIRTIED, zero degrades** — 5/6 windowed, filler1 landing.
  **The space-correct regime is the stable baseline.**
- **Session: 24 draws, 0 canary** (campaign ~90 draws, canary 1: r37a2). Dirty vs
  undirtied landing rate shows no dramatic shift; Stage A stays ~4%/windowed-draw.

**Machinery arc COMPLETE (rounds 57–60):** every ns2jb per-wave step is ported and
stable — CreateFile sized to the margin, per-wave 1 MB dirty write, raw svc close,
13-role wave, gates, chains, caches, retry loop, B/C/D scaffolding. **Stage A is the
sole remaining gate — pure statistics, ~17–20%/boot.** First canary landing → Stage B
prime assert in the same payload. Full session-close state, the frozen command, and
the dead-end list are in [[gated-canary-plan]] (SESSION CLOSE section, boot 52).
Next session: relaunch → the frozen command → lottery harvest.