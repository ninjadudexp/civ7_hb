---
tags: [civ7, switch2, homebrew, status, index]
status: active
updated: 2026-11-05
---

# Project status — where we truly stand

The one-file state of the Civ VII → stock Switch 2 homebrew project. **New sessions:
read [[handoff-v2]] (operations + file map) and this file, then drive.** The historical
record lives in [[gated-canary-plan]] (the living log, boots 13–52), [[tracker]]
(rounds 1–60), and [[handoff]] (v1: pre-round-13 baseline + rounds 13–37) — open those
only when a specific decision needs their detail.

## The current wall — Stage A of the alias chain

The whole remaining research question is **one stochastic gate**: the positive wave's
aliased write must land on **the canary's** server-side path copy during a **windowed
draw**.

What is proven around it:

- **The window is deterministic** — `allPending` (all 10 protected roles spanning the
  trigger) holds on 4–6 of 6 draws every recent boot; `windowOk` repeatedly.
- **The fold fires hot every draw** — trigger `0x2EEC02` (the ns2jb-required form,
  accepted; round-32's `0x2EEA02` rejection is long superseded).
- **The aliased write is live** — ~30% of draws land on *some* held path.
- **The landing law** (round-51 dig, ~90 draws): landings concentrate on
  **non-windowed** draws (~85%) via the freed-hole channel, because a windowed draw is
  a fast trigger resolving *before the queue drains* — **no freed holes can exist at
  fold time on a windowed draw**. Stage A's channel is the **live-copy scatter:
  ~4% of windowed draws** (one confirmed event: r37a2 — the full qualification).
- **Every structural lever has been tried and nullified**: client-slot swap (boot 29),
  staged-page swap (boot 36), canary size-class 0x7f0 (boot 37), sacrifice role at
  delays 1.2 ms / 0 (boots 38–39 — mid-busy sends defer; no early-free exists inside a
  wave), drain-order slot swap (boot 40). All nulls are explained by the no-holes law.

So Stage A is a **lottery at ~17–20% per boot** (4–6 draws × ~4%/windowed-draw), with
every mechanism around it complete and stable. It landed once today (r37a2, 13:25 —
the complete `canaryChanged + markerSelected` qualification) and will land again;
expected cost ≈ 5–6 game relaunches per landing.

## What our last input was

- **Last code change:** `COMMIT_WRITE_SIZE` default **16 MB → 1 MB** (`host.py`) after
  boot 51's `recreate: 0x3C00` (**OutOfSpace**) — Civ VII's save (~62.6 MB free) cannot
  hold a 16 MB file; ns2jb's 16 MB rides a 16+ GB SmileBASIC target. The faithful port
  is *"a file sized to the save's free margin, re-dirtied every wave"* — the journal
  work is the heap-shaping point, not the byte count.
- **Last run:** **boot 52** (2026-11-05 17:32) — the frozen 6-marker chain +
  `CIV_COMMIT_DATA=1`: **6/6 DIRTIED, zero degrades, 5/6 windowed, 1 landing
  (filler1), canary 0/6.** The 1 MB dirty regime is the stable baseline.
- **Last recorded state:** tracker round-60; [[gated-canary-plan]] boot 52 +
  SESSION CLOSE section (the frozen command + dead-end list live there).

## What is remaining on this project

**Near (this phase — the alias chain):**

1. **Stage A** — the canary landing in a windowed draw (the wall; lottery, ~17–20%/boot).
2. **Stage B** — candidate prime: open content storage (cmd 200) on the reserved root,
   16-byte prime read, assert `prime.bytes == control prefix` — i.e. the 0x5a0 cache
   bucket table really allocated **inside the freed canary slot**. Auto-follows the
   first Stage A landing in the same payload (`CIV_ALIAS_UPTO=prime`). *(A prime ran
   live once, round-31 — but pre-landing, without a freed slot; the real B is untested.)*
3. **Stage C** — `installCombinedLeak`: 7 copyField waves write the alias header
   (offset `0x1400000`, range `0x4141...`) into the cache entry. `nul_writes` ported 7/7
   against the reference; the copyField wave path has never completed against the console.
4. **Stage D** — over-read: `readStorage(candidate, 0x1c000, 0x1400000)` — bytes past
   0x4000 are adjacent fsp-srv heap. **The actual leak.**
5. **Stage E** — neighbor cache invalidation → freelist pointers in block heads
   (`changedLink`).

**Medium (the channel):**

6. **Retained memory** ([[retained-memory]]): the corruption-free arbitrary R/W in the
   FS process via the aliased cache — signed one-shot headers
   (`signed_cache_plan.py`, byte-verified port) + the controller
   (`fs_memory_control.py`, 10/10 offline).
7. **Open qualification items:** extent-present `selectedMarker` (tracker todo #2 —
   the type-1 trigger resolution; ns2jb's own discipline gates positive behind it and
   we run on the full r37 acceptance instead), the `negative` profile, the remaining
   probes (`address-encoding-control`, `binary-control`), Civ's bucketPeriod
   (0x28000 vs 0x500000), the ordinary-content control handle, bulk reads > 0x8000.
   Also the commit-packet fidelity item (ours carries a send descriptor; ns2jb's is
   bare `packet(10)` — restore after the dirty write validates further).

**Long (the launch):**

8. **The stolen service-manager handle** ([[porting-roadmap]]): export the FS process's
   own SM handle via the system-data notifier (command 510) → GetService
   `pm:shell` / `pm:dmnt` / `ro:dmnt` are *authorized for the FS process* → the launch
   is a breakout, not a privesc.
9. **M4** ([[launch-gate]]): the session-TOCTOU CodeFS swap — park an FS server
   session, redirect its wait word, exchange the two domain entry object pointers,
   verify both owners.
10. **M5**: map the payload RX **in the FS process** (the promotion our game cannot do
    itself — stock kernel forbids `ControlCodeMemory` on the owning process), launch via
    `pm:shell`, bring up HBLauncherNS2. **Known gap:** the shipped payload implements
    only `0x524f0001`/`0x10–0x16`; HBLauncher's docs use `0x524f001a–1c` — M5 needs an
    extended payload build.
11. **Display** (two owners of the framebuffer in one process — patch the game render,
    suspend it, or a separate process) and **persistence** past the Load Game trigger.

## What is expected when we complete

- **The headline outcome:** a stock, unmodified Switch 2 launches **native homebrew
  (an app package via HBLauncherNS2) from a Civilization VII save file** — no hardware
  mods, entry via the chat-XSS, everything else through the filesystem service.
  "What SmileBASIC 4 users have — for Civilization VII."
- **Per milestone:** B passing means the freed-slot reuse is real (allocator LIFO);
  C/D passing means arbitrary fsp-srv **heap reads** (artifacts in
  `runs/leak-*.bin`, adjacent-heap bytes past 0x4000); E means freelist pointers
  observable; the retained channel means **stable arbitrary R/W in the FS process**;
  the launch means the game process stopped being the boundary.
- **The full chain at completion:** chat-XSS → JS/ROP → FS reservation wave → alias
  chain → retained R/W in fsp-srv → stolen SM → privileged handles → CodeFS swap →
  RX in the FS process → `pm:shell` launch → homebrew.

## Where we truly stand (honest position)

- **Proven and stable:** entry, relocation (M1), path-capable FS (M3), the 13-role wave
  with a **deterministic window**, fold accepted every draw, the per-wave 1 MB dirty
  write (todo #4 closed), the complete positive acceptance **once** (r37a2:
  `canaryChanged` + `markerSelected` + `windowOk`), the landing law, the retry loop,
  the A–D scaffolding (offline-verified), and a live controller read (r31).
- **Stochastic:** Stage A's landing — ~17–20%/boot, ~5–6 relaunches per expected
  landing. This is the wall, and it is *cheap* per boot (~3 min) but not free.
- **The next real research question past the wall:** Stage B's prime assert — does the
  cache table actually land in the freed canary slot? It has never run post-landing.
- **After B:** C/D/E and the retained channel are offline-verified scaffolding —
  expected to be bring-up, not research. The launch path (M4/M5) has documented designs
  and enumerated gaps (payload range commands, display policy).
- **Honest bottom line:** a working exploit chain with one statistical gate in front
  of it, then engineering milestones whose designs exist. Remaining effort is dominated
  by lottery boots + Stage B's allocator question + the M4/M5 port work.

## Proof artifacts (all in `research/CivSwitch/harness/runs/`)

| artifact | what it proves |
| --- | --- |
| `MILESTONE-allPending-true-r16a2.json` | first all-10-span window (round 16) |
| `MILESTONE-allSpan-windowOk-realgeom-r23a1.json` | window on the real (closing-release) geometry |
| `MILESTONE-allSpan-windowOk-resolved-canary-r34a2.json` | window with a **resolved** canary (boot 14) |
| `MILESTONE-positive-wave-ns2jb-faithful-2eec02-r35a1.json` | positive form accepted (`0x2EEC02`), alias live |
| `MILESTONE-positive-alias-lands-guard1-r35b1.json` | aliased write landing on a held path |
| `MILESTONE-stageA-canary-changed-positive-qualified-r37a2.json` | **the Stage A qualification** — `canaryChanged + markerSelected` on a windowed sample |
| `memctl-memory-1.bin` | first live controller read (round 31) |