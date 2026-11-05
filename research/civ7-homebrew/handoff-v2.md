---
tags: [civ7, switch2, homebrew, handoff, index]
status: active
updated: 2026-11-05
---

# Handoff v2 — START HERE

**New session entry point.** Read this + [[project-status]], then drive. **Do not
re-walk the history**: [[handoff]] is v1 (pre-round-13 baseline + rounds 13–37),
[[gated-canary-plan]] is the living log (boots 13–52), [[tracker]] holds rounds 1–60.
Open those only when a specific decision needs their detail.

## The project in one paragraph

Launch native homebrew on a **stock Switch 2** from a **Civilization VII** save file:
the in-game chat XSS gives JS + ROP inside the game process; the stock kernel forbids
code-memory promotion in-process, so the path is ns2jb's filesystem-service breakout —
reserve a heap shape in fsp-srv, race the FS's own request queue so a folded path lookup
aliases, free the canary slot, prime a cache table into it, over-read into fsp-srv's
heap, build a corruption-free R/W channel, steal the FS process's own service-manager
handle, and launch a payload through `pm:shell` with RX mapped in the FS process.

## Stages PASSED (chronological — how)

| # | Stage | How it passed |
| --- | --- | --- |
| 1 | **Entry** (chat-XSS → JS/ROP, boot-persistent) | USB HID typed payload, English US; persistence via `save:/saves/Single/XSTRIG.Civ7Save` (recovered from the frozen session) |
| 2 | **M1 relocation** | SDK signature scan (36 fns), 60 SVC stubs; `sdk_base = read64(main+0x4DC6088) − 0x5224E0`; Switch-1 numbering confirmed on hardware (P2) |
| 3 | **Kernel denial + service reach** (P3/P4) | `CreateCodeMemory` aborts → in-process RX closed; fsp-rrv opens via SDK getService, `pm:*`/`jit:u` denied `0x1015` → the FS route is the only path |
| 4 | **M3 path-capable FS** | Mount-chain walk (RW mapping → accessor → impl → service → manager → `CopyObject`) → real `IFileSystem`: 62.6 MB free, create/verify; counters (1009); 59-object reservation (12160 vs ns2jb 12272) |
| 5 | **Wave substrate** | Raw-SVC threads (create+start, cpuid pin at creation), event gates, faithful 13-role wave; abort profiles byte-qualified (tails `0x320002`, guards `0x202`, nativeGuard 1/1/1) |
| 6 | **The window** (`allPending`) | Rounds 3–24: release-close bug fixed (r18b), guard/trigger geometry, `CIV_PIN_OFFSET=0` endgame; **round 24 = 3/3 deterministic**; `windowOk` + Civ-re-derived `heldFreed` gate (`CIV_HELD_MIN=5000`) |
| 7 | **Fold + alias** | Round 35: trigger `0x2EEC02` (form **accepted** — the resolved-canary staging flipped Civ's normalizer); alias live (guard1 landing) |
| 8 | **Stage A qualified** | **Round 37 (13:25): `canaryChanged + markerSelected` on a windowed positive sample** — the complete ns2jb `:532-536` acceptance. Artifact: `MILESTONE-stageA-canary-changed-positive-qualified-r37a2.json` |
| 9 | **The landing law** | Round-51 dig (~90 draws): landings ≈ 85% on **non-windowed** draws (freed holes); a windowed draw resolves before the queue drains → **no holes at fold time** → Stage A rides the ~4%/windowed-draw live-copy channel. Explains every structural null (b29/36/37/38/39/40) |
| 10 | **Leak/controller machinery** | `leak_read` fixed (DMA vs TLS residue), `signed_cache_plan`/`fs_memory_control` ports (offline selftests), **live controller read** (r31, `memctl-memory-1.bin`), per-profile retry loop (r36), A–D chain scaffolded |
| 11 | **Per-wave dirty write** (todo #4) | Rounds 57–60: ns2jb-diff found the missing step; boot-41→47 archaeology (IFile GetSize=**4**/SetSize=**3**/Flush=2; 16 MB single-shot = rejected + session-poisoning; close = raw `svcCloseHandle`); boot 51 `0x3C00` OutOfSpace → **1 MB margin-sized file**; boot 52: **6/6 DIRTIED, stable** |

## CURRENT STAGE — the Stage A lottery (the wall)

**The one remaining research gate:** the positive wave's aliased write must land on
the **canary's** server-side path copy during a **windowed** draw. Window deterministic
(4–6/6), fold hot every draw, write live — but the landing is a lottery at
**~17–20% per boot**. It landed once (r37a2 = the full qualification); expected cost
≈ 5–6 game relaunches per landing. **A landing auto-runs Stage B in the same payload**
(`CIV_ALIAS_UPTO=prime`). B/C/D/E scaffolding is otherwise complete and offline-verified.

### The last 5 boots (2026-11-05)

| boot | regime | windowed | landings | canary |
| --- | --- | --- | --- | --- |
| 48 | DIRTIED 16 MB ×6 | 4/6 | guard1, filler0 | 0 |
| 49 | DIRTIED 16 MB ×6 | 4/6 | guard0, filler1 | 0 |
| 50 | UNDIRTIED (control — silent CreateFile fail) | 6/6 | none | 0 |
| 51 | UNDIRTIED — **`recreate 0x3C00` OutOfSpace found** | 4/6 | guard0 ×2 | 0 |
| 52 | **DIRTIED 1 MB ×6 — the stable baseline** | 5/6 | filler1 | 0 |
| 53 | DIRTIED 1 MB ×5 (fatal on d6) | 3–4/5 | guard1 | 0 |

Session: 29 draws, 0 canary. Dirty vs undirtied shows no dramatic landing-rate shift;
canary stays ~4%/windowed-draw wherever landings land.

## The frozen command (drive this)

```
cd research/CivSwitch/harness
# boot: power on → Civ VII → Load Game (XSTRIG save) → loader :9026 answers → then:
CIV_ALLOW_OVERWRITE=1 CIV_PREREQ_SKIP=1 CIV_STAGE0_SKIP=1 CIV_ALIAS_UPTO=prime \
CIV_RELEASE_CLOSE_GATE= CIV_ALIAS_MARKERS=0x2340,0x2340,0x2340,0x2340,0x2340,0x2340 \
CIV_ALIAS_MARKERS_MAX=6 CIV_COMMIT_DATA=1 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 \
CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 CIV_HELD_MIN=5000 CIV_NORMALIZED_PATHS=1 \
CIV_CANARY_GATED=1 CIV_CANARY_OFFSET_NS=40000 CIV_CANARY_SIZE=0x5a0 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 CIV_GUARD_DELAY=2550000 \
CIV_GUARD_SPREAD=0 CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 CIV_TRIGGER_TAIL=100000 \
  python leak-cycle.py full
```

Watch per draw: `wave regime: DIRTIED` (expected — self-heals if not), `AP/win`,
`changed` (landing slot), and **`candidate primed`** — that line means Stage A landed
and Stage B passed; preserve the payload's JSON immediately. Wedge/fatal → relaunch
game, re-drive.

## Console protocol (hygiene — learned the hard way)

- **Console `192.168.103.215`** — ports `9026` loader / `9028` relocation / `9029` harness.
- **Boot order:** power on → launch game → **Load Game** (runs the stager) → loader
  answers on `:9026` → drive `flow.py` (relocate → mounts → stop → load → command).
- **One flow per game session.** A spent payload (setup + ~6 waves) needs a game
  relaunch; the loader refuses a second payload otherwise.
- **Failure discriminators:** all ports dead = **game crash** (relaunch). Ports accept
  but silent = **wedge/freeze** (fsp-srv worker pool; **power cycle**). Loader answers
  but loads refuse = loader wedge (relaunch).
- ~21 s/draw, 6 draws/payload, ~3 min/boot. `CIV_RESERVE_TRACE=1` prints the wedge
  point if a reserve hangs.
- **Record every boot** in [[gated-canary-plan]] (the living log) — that file is the
  session memory.

## File map (lookups)

```
research/civ7-homebrew/            ← the notes (Obsidian)
  handoff-v2.md                    ← START HERE (this file)
  project-status.md                ← state: wall / last input / remaining / honest position
  gated-canary-plan.md            ← THE LIVING LOG (boots 13–52 + frozen command + dead ends)
  tracker.md                      ← rounds 1–60
  handoff.md                      ← v1 (historical; §13–37 = rounds 13–37)
  selected-marker-plan.md         ← SM0–SM6 marker/landing hunt (+ landing-law stats)
  fs-memory-control-plan.md       ← controller port P0–P3
  retained-memory.md, address-planner.md, launch-gate.md, porting-roadmap.md,
  payload-guide.md, filesystem-breakout.md, filesystem-access.md, compat-abi.md,
  native-worker-port.md, probe-plan.md, primitive-matrix.md, ...

research/CivSwitch/harness/        ← THE CODE
  host.py                          ← wave engine (worker_wave, dirty_commit_data, gates)
  console.js                       ← the payload (server loop, frames, svc wrappers)
  leak-cycle.py                    ← A–D chain driver (`full`/`memctl`; CIV_ALIAS_UPTO)
  signed_cache_plan.py             ← signed one-shot planner (byte-verified vs .cjs)
  fs_memory_control.py            ← controller core (select/read/writeAt/qualify*)
  cache_address_plan.py            ← nul_writes port (7/7)
  flow.py                          ← boot orchestrator
  frames.py                        ← ARM64 frame builder (CALL4_FN_OFFSET=0x70 — Civ-specific!)
  mount-accessor.txt               ← per-boot save accessor (mounts scan rewrites it)
  runs/                            ← artifacts; MILESTONE-*.json = the proof points

research/CivSwitch/relocation/     ← M1 (relocate payload, signatures, mounts scan)
research/ns2jb/engine/switch2/memory/probe-reserved-gap.cjs   ← THE REFERENCE
research/Atmosphere, research/libnx ← kernel/SDK ground truth (IFile cmds, SVC numbers)
```

## Dead ends — do NOT revisit

- **16 MB commit file** — `0x3C00` OutOfSpace on Civ's ~62 MB save (1 MB is the margin-sized form).
- **16 MB single-shot writes** — rejected *and* session-poisoning (all later writes on the handle fail).
- **`close_object` on raw session handles** — it closes domain *object ids*; on fh it corrupts the session. Close = raw `svcCloseHandle` (console `svc` command) or `close_object` on a *clone*.
- **IFile cmd layout** — Read=0/Write=1/**Flush=2**/SetSize=3/**GetSize=4** (same trailing layout as IStorage).
- **The freed-hole aim family** — windowed draws have no holes at fold time (delays, drain-order, slot swaps all null by law).
- **Client-slot / staged-page swaps** — landing scatter is uniform over roles.
- **Async IPC** (`0x23` → `0xCC01`), **runtime affinity** (`0xE401`), **host-side re-reserve** (freezes), **58/60 reservation objects** (crash; 59 only), **reading below a region base** (kills the game), **stale mount accessors** (fault — rescan mounts every boot).

## Next session, in order

1. Relaunch game → drive the frozen command (above) → lottery harvest, ~3 min/boot.
2. First `candidate primed` → **Stage B passed** — preserve JSON, record, then C/D
   bring-up (the scaffolding is ready; see [[fs-memory-control-plan]] P3).
3. Dry streaks are expected (P ≈ 80%/boot dry at ~4%/draw × 4–6 draws) — keep rolling
   or stop per budget; every boot also refines the dirty-regime rate estimate.
4. Stage B fail (prime assert) → the allocation-order dig is the next research
   question; everything needed is already in the notes.