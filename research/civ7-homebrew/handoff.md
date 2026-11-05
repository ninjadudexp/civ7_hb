# CivSwitch — developer handoff

**Purpose of this document:** hand the current state to another engineer for a second set of eyes.
It assumes you can read C, ARM64 assembly and JavaScript, and that you know your way around a
homebrew ecosystem, but it explains the Switch-2-specific machinery from scratch because almost none
of it is documented publicly.

**The ask, in one line:** *the wave's `allPending` gate needs all ten protected requests to be
"pending" (their filesystem call still outstanding) at the moment the trigger request is issued, and
we can only get 8–9 of 10 in any sample. We believe the last 1–2 requests simply aren't being
deferred by the filesystem, and we'd like to know whether that's a controllable property (queue
depth, door, request ordering) or irreducible timing chance.*

> **STATUS UPDATE (2026-11-04, post-round-19):** the ask is ANSWERED — `allPending` is achieved
> and repeatable (rounds 16–19; milestone artifacts in `harness/runs/MILESTONE-*.json`), and one
> sample is formally `hasWindow`-qualified. Sections 13–37 walk the rounds in order; §28b records the byte-verified bug fixes (release never
closed the group; marker placement; counter-read target; heap-corrupting frees) that reset the
geometry baseline. Remaining gate: `selectedMarker`. Sections 3–12 are the pre-finding baseline,
kept for machine/reference context — read §13 onward for the live state.

Everything else below is context to make that question tractable.

---

## 1. The goal

Launch homebrew on a **stock, unmodified Nintendo Switch 2**, using the **Civilization VII** game as
the entry point. The entry vector is a chat message that reaches an in-game web view vulnerable to
XSS — the XSS gives us JavaScript execution inside the game process. From there we are reimplementing
the pipeline that `ns2jb` (a public Switch 2 jailbreak project) uses for **SmileBASIC 4**, which is a
different game with a different engine, hence the very large amount of re-derivation.

The reference implementation is checked out alongside these notes at `research/ns2jb/`. Its
`engine/switch2/memory/probe-reserved-gap.cjs` is the single most important file to read — it is the
wave we are porting, and the semantics of every number below come from it.

Target console: `192.168.103.215`. A workstation drives it over the network; the person at the
console only performs power cycles and game relaunches.

---

## 2. System overview

```
 workstation (Python)                                   Switch 2
 ┌────────────────────┐                          ┌──────────────────────────────┐
 │ host.py            │  HTTP-ish over TCP :9029 │  console.js payload          │
 │  builds "frames"   │ ───────────────────────► │   server loop, call_rop,     │
 │  drives the wave   │ ◄─────────────────────── │   page allocator, SVC stubs  │
 │  parses results    │                          │                              │
 └────────────────────┘                          │   Civ VII game process       │
                                                 │    (the payload runs inside) │
 relocate_host.py :9028 (bootstraps the above)   └──────────────────────────────┘
```

Two payloads matter:

- **`relocation/payload/relocate.js`** — loaded first, each boot. It locates the SDK's internal
  functions (memcpy, longjmp, thread helpers, event helpers) by signature and reports their offsets.
  Those offsets are then pushed into `console.js`. Run it via `relocate_host.py`.
- **`harness/console.js`** — the working payload. It opens a TCP control port and executes ROP-ish
  "calls" on request. It is the only thing we need after boot.

**Critical operational fact:** the loader evaluates **one payload per boot**. Once `console.js` is
running, you cannot load a second payload without a power cycle. A fresh `console.js` load *on a fresh
port* is cheap and does not need a reboot — but in practice the loader often refuses after one
payload, which is the "loader wedge" referred to throughout.

### Why frames

`console.js` cannot call arbitrary functions with arbitrary arguments directly; arguments must be
loaded into registers by real code. So `frames.py` builds small **position-independent ARM64
programs** ("frames") in the host, writes them into the game's memory, and starts each one as a
**native thread**. A frame is a sequence of calls to a table of SDK functions, with argument setup
interleaved, ending in `longjmp` back into the payload's own state.

Frame layout (see `frames.py`):

| field | offset | meaning |
| --- | --- | --- |
| tables | 0x10 | pointer to the function-per-slot table |
| loadTable | 0x58 | the table loader call |
| stack | 0x68 | top of the frame's stack |
| RESULT | 0x800 | where the frame's return value lands |
| SENTINEL | — | `0xfedcba9876543210`, for spotting uninitialised slots |

**The one hard-won frame detail:** `call4` in Civ VII is `ldr x8,[x8,#0x70]`, while ns2jb's engine
uses `ldr x8,[x8,#0x68]` — the rest of the trampoline is byte-identical. So the callee lives at
`table + 0x80`, not `table + 0x78`. `frames.py` exposes this as `CALL4_FN_OFFSET = 0x70`. Getting
this wrong makes every frame jump into the wrong function.

---

## 3. How to run it

### Every boot

```bash
cd research/CivSwitch/harness

# 1. relocation payload: locate the SDK internals (must be re-run every boot)
python ../relocation/host/relocate_host.py 192.168.103.215 load
python ../relocation/host/relocate_host.py 192.168.103.215 mounts   # writes harness/mount-accessor.txt
python ../relocation/host/relocate_host.py 192.168.103.215 stop

# 2. the working payload, on a port that is not in use
CIV_HARNESS_PORT=9210 python host.py --host 192.168.103.215 load

# 3. the wave, with the current best configuration
CIV_HARNESS_PORT=9210 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=1000000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2500000 CIV_GUARD_DELAY=700000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2520000,2350000 \
CIV_TAIL_DELAYS=2580000,2535000,2350000,2505000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3
```

The trailing `3` is the number of in-process attempts (ns2jb calls this `retryWave`). Each attempt
reserves its own group; reports land in `harness/runs/worker-wave-extent-present-attemptN.json`.

`harness/flow.py <ip> <command>` wraps the relocate-load → mounts → stop → harness-load sequence if
you want one command.

### Analysing a run

```bash
python - <<'PY'
import json
d = json.load(open("runs/worker-wave-extent-present-attempt2.json"))
tm = d["timing"]; t0 = tm["trigger"]["startUs"]; t1 = tm["trigger"]["endUs"]
print("trigger %.1f -> %.1f (%.1f us)" % (t0, t1, t1-t0))
for r in ("commit","hole","tail0","tail1","tail2","tail3","canary","filler0","filler1","guard0","guard1"):
    s, e = tm[r]["startUs"], tm[r]["endUs"]
    note = "spans" if (s < t0 and e > t1) else "ENDS EARLY %.0f" % (t1-e)
    print("  %-8s %8.1f -> %8.1f  %s" % (r, s, e, note))
print("guard", d["nativeGuard"], "allPending", d["allPending"])
PY
```

---

## 4. What the wave does, and why

This is the heart of the whole exercise, so it is worth reading `probe-reserved-gap.cjs` alongside
this section.

**The primitive being attacked.** The save-filesystem service (`fsp-srv`) lets a client **reserve a
block of objects** — a batch of handles that are allocated at once and referenced as a group. If you
can get a reservation of a known size placed at a known spot in the heap, you can later use a
**stale/aliased path lookup** during a window where the filesystem has an outstanding request, and
turn a *path string* into a controlled memory access. That is the "reserved gap" the ns2jb probe is
named after.

**The mechanism.** The wave exploits a concurrency property of the filesystem: while one request is
being serviced (specifically, while a large **read** is in flight and the request queue is busy),
subsequent requests are **deferred** — the caller's `sendSyncRequest` blocks. If a *path lookup*
(the "trigger") is issued while several other path operations are deferred, the filesystem's path
resolution can be made to fold through a set of directories whose names it will then reuse, and the
request resolves against a **different object than the caller asked for**.

That is what the "fold" refers to: `slash-check` demonstrates that the filesystem collapses `//`
sequences, so a path such as `<scratch>//S0//S1//S2//SPAN_MARKER` resolves as a single descriptor
normalised along the way. `fold-workers` proves the fold is a **concurrency requirement**: racing a
`1002` seed (on the root session) against a `7` lookup (on the save filesystem) from native threads
selects the marker; issuing the same two requests sequentially is refused with `0xE401`.

**The wave's roles.** Thirteen native threads, each with a role, each sending one filesystem request
at a measured delay:

| # | role | request | purpose |
| --- | --- | --- | --- |
| 0 | `commit` | `packet(10)`, no descriptors | marks the group as committed |
| 1 | `hole` | `packet(0)` with a `receive` descriptor of `PROBE_READ_SIZE` | the large read that makes the FS busy for ~60 ms |
| 2–5 | `tail0..3` | `path_packet(1002, …)` on root clones | the "seeds": deferred lookups that build the folded path |
| 6 | `canary` | `path_packet(7, …)` | an owned path we watch for corruption |
| 7–8 | `filler0/1` | `path_packet(5, …)` + descriptors | occupy queue slots |
| 9 | `release` | closes the wrapped group + counter reads | measures `preReleaseUsed`, releases the reservation |
| 10–11 | `guard0/1` | guard-path packets, two `0x301` descriptors | set the "completed" flag |
| 12 | `trigger` | `path_packet(7, …)` | the lookup that must resolve through the fold |

**The gates** (`hasWindow` in ns2jb, all must hold):

| gate | requirement | current status |
| --- | --- | --- |
| `!triggerAborted` | `nativeGuard.completed == 0` | **passes** in good samples |
| `allPending` | every protected role's window contains the trigger's | **8–9 of 10** — the open problem |
| `receiveOrderMarginUs >= 300` | hole end − guard1 start | **passes** (~59,000 µs) |
| `preReleaseUsed >= 0x2600` | memory held at the pre-release read | **FAILING** — 7136–7472 against 9728 (see §13.7); an earlier 40,528 came from a different filler config and is superseded |
| `restored` | the heap returns to baseline after the group is released | **passes** (churn-tolerant: `max(4·|churn|, 0x10000)`) |
| `selectedMarker` | `trigger.result == 0` **and** inline u32 `== 1` | partially: `result == 0` observed, type `1` observed once |

`allPending` is defined in ns2jb as (and our implementation matches it exactly):

```js
[...heldPaths, ...rows.filter(r => r.role.startsWith('tail') || r.role === 'commit')].every(
  r => r.startUs < trigger.startUs && trigger.endUs < r.endUs)
// heldPaths = ['canary','filler0','filler1','guard0','guard1']
```

So ten roles total. Each role's `startUs`/`endUs` are the two `cntvct` ticks surrounding its IPC
send — i.e. a role "spans" the trigger iff it was **still blocked** when the trigger was sent *and*
remained blocked until after the trigger returned. That is exactly "the request was deferred".

---

## 5. The current blocker, with data

The configuration above produces, repeatedly:

```
trigger  2418.5 -> 2963.2   (545 us — fast; 83-650 us across samples)
commit      0.0 -> 3550.5   spans
hole      100.2 -> 60541.7  spans
tail0    2377.6 -> 4079.1   spans        (deferred)
tail1    2330.1 -> 2623.3   ENDS EARLY 340    (not deferred)
tail2    2341.7 -> 4156.7   spans        (deferred)
tail3    2303.3 -> 4207.6   spans        (deferred)
canary   2300.4 -> 2944.3   ENDS EARLY 124    (not deferred)
filler0  2317.6 -> 3330.2   spans
filler1  2352.6 -> 3509.3   spans
guard0   2009.2 -> 3522.0   spans
guard1   2012.0 -> 3537.7   spans
guard 0/0/0   allPending False
```

**The pattern.** When a request **defers**, its window is ~1.7–1.9 ms wide (it stays blocked until
the hole's 60 ms read has drained and the queue releases). When it **does not defer**, its window is
only ~300–650 µs wide. Roles that defer always span; roles that don't, miss by 100–500 µs.

Which roles defer varies per sample. We have seen every role defer at some point, and we have seen
8/10 (and once 9/10-equivalent) in a single sample — but never 10/10. Individual roles under
apparently identical input:

| role | window observed |
| --- | --- |
| canary | 644 µs (not deferred) / ~1.2 ms (deferred) |
| tail1 | 293 µs (not deferred) / ~1.8 ms (deferred) |
| tail3 | ~500 µs (not deferred) / ~1.9 ms (deferred) |

**Tuning has hit its noise floor.** The relationship between a role's configured sleep and its actual
`startUs` is not stable between runs — measured offsets from the nominal delay range from −450 µs to
+265 µs for the *same* role and the *same* delay, and small corrections (60–175 µs) reliably fix one
role while knocking another out. This is why we stopped tuning.

**Also established:** the trigger's `startUs` is effectively **fixed at ~2.4 ms** and does not respond
to `CIV_TRIGGER_SLEEP` (swept 1.0–2.0 ms across four runs; start stayed at 2370–2398). Only its
*speed* varies: ~35–650 µs when it slips through undeferred, ~5.4 ms when it is itself deferred.
A deferred trigger is **structurally unqualifiable**, because it is queued last and drains last, so
its end lands behind every protected role's end.

---

## 6. The open question (what we want a second opinion on)

**Why does the filesystem defer some requests and not others, apparently at random?**

Points that seem relevant:

1. Every request in the wave is issued while the `hole`'s ~60 ms read is in flight — the FS should be
   busy for the entire wave. Yet only some requests block.
2. We have not been able to correlate deferral with: the role, the request opcode (`7` vs `1002` vs
   `5`), the session/door the request is sent on (the wave uses two saved-filesystem clones, four
   root clones and one storage session), or the configured delay.
3. The session/door did matter for **drain order** (see below), which suggests the FS has per-door
   state that could plausibly also govern deferral.
4. If deferral is a **queue-depth** property (i.e. the FS defers only while N requests are already
   outstanding), then issuing more requests — or issuing them in a tighter burst — might make it
   deterministic rather than a coin flip. That is the hypothesis we would most like tested.

**Concrete experiments we have not run:**

- Vary the number of concurrently-issued requests (e.g. add 4 more throwaway `1002` lookups) and see
  whether the deferral fraction rises toward 100%.
- Issue all ten protected requests in a single tight burst (tens of µs apart) and see whether they
  defer together.
- Send the same request repeatedly on the same door vs on different doors, holding everything else
  fixed, and measure deferral rate per door.
- Check whether the FS's deferred-request queue has a *depth limit* by pushing 20+ requests and seeing
  whether the tail of the burst stops deferring.

---

## 7. Findings that cost real time (do not re-derive)

### Architecture

- **Post-driven concurrency is dead.** `svcSendAsyncRequestWithUserBuffer` (SVC `0x23`) returns
  `0xCC01` for an initialised `fsp-srv` at every buffer size, leaves the session handle in the out
  word, and produces no event. Verified by `probe_async.py`. Concurrency must come from threads.
- **Native threads, not the game's thread API.** The game's own `svcCreateThread` callers are
  ~0x40-byte cluster wrappers with a hard-wired fixed entry point, useless for us. Use the direct-SVC
  route: `svcCreateThread(Handle* out, entry, arg, stack_top, prio, cpuid)` at `sdk+0x168b2c`, **plus**
  `svcStartThread` (`svc #0x09`) — `CreateThread` alone leaves the thread created but not running.
- **Pin at creation.** `cpuid = i % 4` passed to `svcCreateThread` tightens timing spread ~10×. The
  runtime affinity SVCs (`0x10`/`0x11`/`0x12`) are a **dead end** (`0xE401`).

### SVC stub ABI — read the form before calling

`relocation/signatures/svc-relocation.json` records each stub's `form`. This matters:

- form `"svc;ret"` → a plain stub, safe to call with the syscall's own arguments.
- form `"multi"` → the stub **writes the syscall's return value to `*(x0)`**, i.e. `x0` is an output
  pointer, not an argument.

We lost a session to this: `svcWaitSynchronization` (`0x18`, `sdk+0x168bf4`) is form `multi`, so
passing the event handle as `x0` corrupted memory. The harness now **polls** instead of waiting (see
`collect` in `console.js`). `svcCloseHandle` (`0x16`, `sdk+0x168be4`) is form `svc;ret` and is safe.

### The reservation group

- **59 objects is the only stable size.** 58 or 60 crashes the game. The cost is hypersensitive
  because the reservation is what shifts the heap into the exploitable shape.
- ns2jb asserts the allocation costs exactly `59 × 208 = 12272` bytes. **Civ VII measures 12160–12176**
  — a real firmware difference, not an accounting bug. ns2jb's `check()` would throw on this, so the
  port must relax that assertion (it does, recording `expected` vs `allocated`).
- The group is released **mid-wave** by the `release` role closing the wrapped outer (ns2jb writes
  `closeObject(group.outer)` into `requests[9]`). A retry therefore needs a **fresh reservation**;
  reusing the released group cannot qualify.

### The retry loop (this is now working — do not break it)

- Each attempt reserves its own group. **Do not call raw `svcCloseHandle` on the owner/rootCopy**
  after the wave: the wave's own `release` role already closed the outer, and the extra closes froze
  the console (traced: zero reservation-trace lines before the connection died). ns2jb closes
  comparable handles in its own JS table, not as raw SVC handles. `CIV_RELEASE_HANDLES=1` restores
  the old behaviour for comparison.
- Each wave leaks ~100 pages of the payload's heap (13 frame bases, 13 out pages, 13 request pages,
  the ctrl page, buffers) — never freed. This is what killed the payload on attempts 2–3.
  `host.py` now records them (`self.state["wave_allocs"]`) and the payload frees them between
  attempts via the `freeAddresses` command. With that fix, two-to-three attempts per payload complete.
- **A payload that dies takes its port with it.** Never reuse a port for a subsequent load on the
  same boot; use a fresh one per attempt.

### Drain order — client slot, not send time

The order in which deferred requests complete follows the **client's slot in the session list**, not
when it was sent. We verified this by moving the canary's send from 1890 → 2075 µs and watching it
still close first, while the other roles closed in ascending slot order. The canary sat in the first
protected slot, so it always closed ~30 µs before the trigger — swapping it to the last protected
slot (`clients[6] ↔ clients[11]`, `CIV_CANARY_LAST=0` to disable) fixed it. The same trick rotated the
failure to the next slot, which is how we know the model is right.

### Path handling

- The filesystem collapses `//` runs (`slash-check`), and normalises paths. `<scratch>||S0||S1||S2||SPAN_MARKER`
  resolves with `result 0` and inline u32 `1` as **one** descriptor.
- The canary's geometry is byte-sensitive: the short-form canary keeps `result 0`; the faithful
  `normalized_path` form flips it to `0x202`. `CIV_NORMALIZED_PATHS=1` selects the faithful form.
- The marker basename lives at ns2jb's documented `MARKER_OFFSET = 0x2340` (`CIV_EXTENT_MARKER=end`
  to put it at the extent end instead). With it at `0x2340` the canary's inline type comes back `1` —
  the value `selectedMarker` wants.

### Mount chain

The save filesystem's accessor moves every boot, and **reading a stale one faults the process**.
`relocate_host.py mounts` scans readable regions for `save\0`, walks accessor → impl → service →
manager, and writes `harness/mount-accessor.txt`. The harness reads that file; its own
`discover_mount` faults and is not used.

### Dead ends, in brief

| thing | verdict |
| --- | --- |
| async IPC (`svcSendAsyncRequestWithUserBuffer`, `0x23`) | `0xCC01`, no event — dead |
| runtime affinity (`SetThreadCoreMask`, SVC `0x10/0x11/0x12`) | `0xE401` — pin at creation instead |
| `sdk+0x1370c0` ("priority") | **takes a thread argument**; calling it bare crashed the game |
| reading below a region base | reading `save_accessor − 0x2000` under base `0x35306e000` killed the game |
| host-side retry/reserve | freezes the console — retries must run in-process |
| reusing ports across loads | the dead payload keeps the port |
| one payload per boot | the loader will not evaluate a second one without a power cycle |

---

## 8. Code map

```
research/CivSwitch/
  core.js                         shared payload utilities
  send_js.py, flow.py             convenience drivers
  relocation/
    build-signatures.cjs          finds SDK functions by signature
    signatures/svc-relocation.json   60 SVC stubs: imm, entryOffset, form, returns
    signatures/sdk-resolved.json     resolved SDK function offsets
    host/relocate_host.py         drives the relocation payload; `mounts` writes the accessor
    payload/relocate.js           the relocation payload itself
  harness/
    console.js                    the working payload: server loop, call_rop, page allocator,
                                  SVC wrappers, wave runner, `freeAddresses`
    host.py                       the host: frame building, the 13-role wave, reservation group,
                                  retry loop, report generation
    frames.py                     ARM64 frame builder (CALL4_FN_OFFSET = 0x70 here!)
    flow.py                       load → mounts → stop → load-one-command
    probe_async.py                the experiment that killed the async-IPC idea
    runs/                         JSON reports (worker-wave-<profile>[-attemptN].json)

research/ns2jb/                   the reference implementation — read this
  engine/switch2/memory/probe-reserved-gap.cjs     THE wave (all semantics come from here)
  engine/shared/sdk-functions.json, sdk-layout.json
  engine/switch2/app/native-worker.cjs, mounted-save.cjs, domain.cjs
  engine/switch2/ro/*.cjs          the subsequent RO stage (not started)

research/civ7-homebrew/           design notes
  tracker.md                      live status board — start here
  handoff.md                      this document
  native-worker-port.md           the native thread work, in detail
  filesystem-breakout.md          the primitive and the mount chain
  namespaces/ notes on ...        (engine internals, retained memory, address planning)
```

## 9. Key offsets (Civ VII build in use)

```
sdk_base = read64(main + 0x4DC6088) - 0x5224E0
memcpy      +0x5224E0     longjmp     +0x5511B4     call4   +0x359B88
getService  +0x14CC90      current    +0x137050      tick   +0x137240
loadTable   +0x4E094       saveResult +0x28C980      saveResult64 +0x19BCC0
setCurrentThread +0x1380D8  exit      +0x13E0A8      sleep  +0x13E270
event cluster: initialize +0x1324E0  finalize +0x132510  signal +0x132520
               clear +0x132640  wait +0x132680  tryWait +0x1327E0
SVC stubs: queryMemory 0x06  createThread 0x08  startThread 0x09  exitThread 0x0A
           sleepThread 0x0B  getThreadCoreMask 0x10  setThreadCoreMask 0x11
           closeHandle 0x16  waitSynchronization 0x18  getSystemTick 0x1E
           connectToNamedPort 0x1F  sendSyncRequest 0x21  sendSyncRequestWithUserBuffer 0x22
           sendAsyncRequestWithUserBuffer 0x23  getInfo 0x29  createEvent 0x45
TLS current-thread slot: [tpidr_el0, #0x1f8]
```

## 10. Environment variables

| variable | default | purpose |
| --- | --- | --- |
| `CIV_HARNESS_PORT` | 9029 | control port (use a fresh one per load) |
| `CIV_READ_SIZE` | 0x180000 | the hole's receive size |
| `CIV_TRIGGER_SLEEP` | 2400000 | trigger's pre-send sleep (start is fixed regardless) |
| `CIV_RELEASE_DELAY` | 2800000 | release role's pre-read sleep |
| `CIV_CANARY_DELAY` | 1700000 | canary send delay |
| `CIV_GUARD_DELAY` / `CIV_GUARD_SPREAD` | 0 / 150000 | guards' send offset / inter-guard spread |
| `CIV_FILLER_DELAYS` | 2000000,2150000 | filler0,filler1 send delays |
| `CIV_TAIL_DELAYS` | (ns2jb's) | tail0..3 send delays, comma-separated |
| `CIV_PIN_EARLY` / `CIV_HOLE_EARLY` / `CIV_RELEASE_EARLY` / `CIV_PRE_CLONE` | off | timing/decoupling fixes, all recommended on |
| `CIV_CANARY_LAST` | 1 | swaps the canary to the last protected client slot |
| `CIV_RESERVE_OBJECTS` | 59 | reservation size — **do not change**, 58/60 crash |
| `CIV_ALLOW_OVERWRITE` | off | gate for the destructive profiles |
| `CIV_RELEASE_HANDLES` | off | restore the raw-closeHandle behaviour (known to freeze) |
| `CIV_ALLOC_TRACE`-style diagnostics | `CIV_RESERVE_TRACE`, `CIV_NO_PIN`, `CIV_TRIGGER_DYNAMIC`, `CIV_NORMALIZED_PATHS`, `CIV_EXTENT_MARKER` | see `host.py` |

## 11. Known environmental hazards

- **The console freezes regularly.** This is inherent to probing a live game's allocator, not a bug
  in the harness: a mis-placed reservation corrupts memory. If the control port is open but silent,
  the console is frozen and must be power-cycled; relaunching the game alone will not clear it.
- **"Loader wedge"**: after one payload the loader often refuses to evaluate another even though the
  console is alive. Relaunching the game clears it; power-cycling clears it too.
- **Crashes are expected.** ns2jb's own flow treats a failed placement as *stop-and-recover*
  (`placementUncertain`, "retain resources for recovery"), not retry-blind. The destructive profiles
  are gated behind `CIV_ALLOW_OVERWRITE` for this reason and must not run until
  `selectedMarker && hasWindow` holds.

## 12. Suggested next steps

1. **Answer the deferral question (§6).** That is the whole remaining gap.
2. Once `allPending && selectedMarker`: freeze the run as a qualified artifact (the repo already
   preserves `runs/worker-wave-extent-present-QUALIFIED*.json` from earlier milestones), then and only
   then run `negative`, then `positive` behind `CIV_ALLOW_OVERWRITE=1`.
3. Reconcile the reservation accounting (measured 12160–12176 vs ns2jb's asserted 12272) before
   trusting any automation that keys off it.
4. Port the payload-side `retryWave` properly (our retries are host-driven; ns2jb's are in-process and
   can generate more samples per boot).
5. After `extent-present` qualifies: the remaining profiles (`address-encoding-control`,
   `binary-control`, backslash control `0x2eec02`), then the RO stage in `ns2jb/engine/switch2/ro/`.

---

## 13. Second-review findings (2026-11-04) — incorporated

A second engineer reviewed the reference wave, `host.py` and the run artifacts. Four points, all acted on.

### 13.1 The reference never made deferral deterministic either

`probe-reserved-gap.cjs`'s `retryWave` tolerates the coin flip (`if (!hasWindow(row)) continue;`) —
SmileBASIC's success is the survivor of retries, not a controlled deferral property. So the §6 framing
over-asks. The practical lever is **samples per boot** (todo #6, payload-side `retryWave`), not determinism.

### 13.2 The two preserved QUALIFIED artifacts are PHANTOMS — do not treat them as evidence

An audit of every report found exactly two with invalid timing:

```
worker-wave-extent-present-QUALIFIED.json     trigger start=5720327888.1   end=1759.6   allPending=True
worker-wave-extent-present-QUALIFIED-2.json   trigger start=12331457272.9  end=1551.5   allPending=True
```

The start slot holds a raw `cntvct` (≈1e9 µs) because the extraction failed to find the trigger's real
surrounding ticks. With a bogus huge `startUs` every role trivially starts "before" the trigger, so
`allPending` passes **vacuously**. Nothing in the artifacts said so, and an engineer could burn a boot
"verifying" a phantom.

**Fixed:** `host.py` now computes `timingValid` (start and end within 1e6 µs, `end >= start`) and forces
`allPending = False` when it fails; the flag is written into every report. **Both QUALIFIED files should
be treated as non-comparable.** (The earlier milestone claims in `Work log.md` that cite them are
therefore superseded.)

### 13.3 Deferral is role-specific and stable, not random

Aggregation over all valid reports:

| role | slot | deferred | rate | | role | slot | deferred | rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| commit | 0 | 9/9 | 100% | | filler0 | 7 | 4/9 | 44% |
| tail0 | 2 | 5/9 | 56% | | filler1 | 8 | 8/9 | 89% |
| tail1 | 3 | 5/9 | 56% | | guard0 | 10 | 9/9 | 100% |
| tail2 | 4 | 9/9 | **100%** | | canary | 11 | 4/9 | 44% |
| tail3 | 5 | 0/9 | **0%** | | guard1 | 6 | 9/9 | 100% |

`tail3` never defers (0/9) while `tail2` always does (9/9) — same opcode (`1002`), different path. So
`allPending` is **impossible** while tail3 behaves this way: this is a structural window problem, not
bad luck, and "roll more dice" alone cannot solve it.

Also: **no anti-correlation** between a fast trigger and the deferral count (both fast and slow triggers
showed 7/10 deferred), so the trigger is not stealing queue slack. Null result, but it rules that out.

### 13.4 The "structurally unqualifiable deferred trigger" is a slot artifact

Drain order follows client slot (proven by the canary swap), and the trigger sits in the **last** slot —
so a deferred trigger drains last and its end lands behind every protected end. Swapping the trigger into
an **early** slot should invert that: a deferred trigger drains first, so `trigger.endUs < r.endUs`
becomes easy to satisfy, while `startUs < trigger.startUs` is untouched (the trigger's send is at
~2.4 ms regardless of slot).

Implemented as `CIV_SLOT_SWAP=a,b` (e.g. `CIV_SLOT_SWAP=12,6`). Caveat: the fold's semantics should not
care which clone carries the trigger, but verify once against `abort-control`.



### 13.6 CONFIRMED: the deep canary is a real fix (2026-11-04)

`CIV_CANARY_DEPTH=3` works exactly as designed:

```
canary window 6077 us     result=0     type=1
```

A ~6 ms window (vs the short form's 105–520 µs) **and** the correct `result 0` / type `1`. This is
what the normalized/padded form could not achieve: it widened the window to 6587 µs but resolved to a
*missing* path, flipping the result to `0x202` / type 0. The deep form widens the window by making the
lookup walk a real, long directory tree, so the path still resolves.

In the same sample the canary missed `allPending` by only **31 µs** (its 6077 µs window against a
deferred trigger's 6108 µs), so `CIV_CANARY_DEPTH=5` is queued to add margin.

**tail3's path shape is ruled out.** `CIV_TAIL3_PATH=/SPAN_ABSENT/` (directory-style, trailing slash)
left tail3 at 545/373 µs — still not deferred. With opcode, delay (2.2 ms, where the guards defer 100%),
slot and path shape all eliminated, the remaining difference between tail3 (0/10) and tail2 (9/9) is
its **client handle** — all four tails are root clones, but separate ones. `CIV_SLOT_SWAP=5,4` swaps
tail3's and tail2's handles and tests that directly.

**Best deferral count observed: 9/10** (`attempt1` above, with only tail3 not deferring and everything
else — including a deferred trigger — holding).

Both attempts in that run also reported `timingValid=True`, confirming the §13.2 guard is live.

### 13.7 Round-2 findings (2026-11-04) — instrumentation fixed, two review claims corrected

**Instrumentation now in every report** (the review's #4, plus the safety item):

- **`config`** — a dump of every `CIV_*` environment variable for that run (14 keys in practice).
  Post-hoc forensics across runs was previously guesswork; ns2jb records `workerDelaysNs` for the same
  reason.
- **`timingValid`** — see §13.2. `allPending` is forced false when it fails.
- **`placementUncertain`** — set when `changedPaths` contains the canary *without* a qualifying window
  (ns2jb's discipline: save, stop, retain for recovery; never retry an overwrite blind).
- **`changedPaths` now respects the canary's staging form.** Comparing against a fixed
  `result 0 / type 1` expectation made every `CIV_NORMALIZED_PATHS=1` run report
  `changedPaths=['canary']` — a false overwrite signal indistinguishable from a real aliased write.
  The expectation is now derived from the staging form.

**`CIV_CANARY_DEPTH` confirmed holding** across multiple samples: the canary now defers and spans where
it previously missed by 100–500 µs. It is off the blocker list.

**`tail3`'s suspected mechanism is REFUTED.** The review hypothesised that tail3's piece contains the
marker basename at `0x2340`, so its lookup *resolves*, gets serviced, and therefore never queues.
Tested with `CIV_EXTENT_MARKER=end` (marker moved out of tail3's piece): tail3 still did not defer
(0/2, windows 462/476 µs). **tail3 remains an unexplained 0% outlier** with opcode, delay, slot, path
shape and piece content all eliminated. It is the single unobtained role.

**`preReleaseUsed` is a genuinely open gate, and it is not stale.** Measured 7136–7472 against the
0x2600 (9728) requirement. §4's claim of 40,528 came from a different filler configuration and is
superseded. A run with `CIV_RELEASE_DELAY=2500000` produced **−21280** — negative — which shows the
failure mode precisely: the baseline (`held`) is captured fresh per reservation, but the **game's own
heap churn exceeds the reservation's held margin** during the wave, so the metric is noisy and can
invert. ns2jb's 0x2600 is ≈80% of its 12160 reservation; we measure ≈60%. This gate fails
**independently of `allPending`**, so it must be solved before any qualification is real.

**Latent coupling discovered:** raising `CIV_RELEASE_DELAY` from 1.5 ms to 2.5 ms **moved the trigger's
start from ≈2400 µs to 3365–3428 µs**. The `released` gate is therefore not fully decoupled by
`CIV_RELEASE_EARLY` — the trigger's fixed start is only fixed *at the tuned release delay*. Any future
change to the release timing must re-check the trigger's start.

### Corrections to the round-2 review

| claim | verdict |
| --- | --- |
| "ns2jb loops `probeReadSize` until a size qualifies; sweep `CIV_READ_SIZE`" | **Incorrect.** `probeReadSize` is a constant `0x180000` (line 17); line 526 only *records* it into `report.qualifiedReadSize`. Our value already matches the reference. A size sweep is still a legitimate probe (the hole governs how busy the FS gets) but is not "the reference's own qualification procedure". |
| "the 20:11 run overwrote the canary without a qualifying window — safety breach" | **Misattributed.** Those runs used `CIV_NORMALIZED_PATHS=1`, where the canary's expected result *is* `0x202`/type 0. `changedPaths` was comparing against the short-form expectation. No overwrite occurred; the instrumentation was wrong, and is now fixed. |
| "`Work log.md` is referenced by the tracker but doesn't exist" | **Casing false alarm** — the file exists and is the same one my notes go to. |
| "`held` may be stale across retries, silently inflating `preReleaseUsed`" | **Unfounded** — `held` is re-read from each new reservation (`host.py` lines 1198/1252). The metric's real problem is churn, not staleness (above). |

### 13.6 CONFIRMED: the deep canary is a real fix (2026-11-04)

`CIV_CANARY_DEPTH=3` works exactly as designed:

```
canary window 6077 us     result=0     type=1
```

A ~6 ms window (vs the short form's 105–520 µs) **and** the correct `result 0` / type `1`. This is
what the normalized/padded form could not achieve: it widened the window to 6587 µs but resolved to a
*missing* path, flipping the result to `0x202` / type 0. The deep form widens the window by making the
lookup walk a real, long directory tree, so the path still resolves.

In the same sample the canary missed `allPending` by only **31 µs** (its 6077 µs window against a
deferred trigger's 6108 µs), so `CIV_CANARY_DEPTH=5` is queued to add margin.

**tail3's path shape is ruled out.** `CIV_TAIL3_PATH=/SPAN_ABSENT/` (directory-style, trailing slash)
left tail3 at 545/373 µs — still not deferred. With opcode, delay (2.2 ms, where the guards defer 100%),
slot and path shape all eliminated, the remaining difference between tail3 (0/10) and tail2 (9/9) is
its **client handle** — all four tails are root clones, but separate ones. `CIV_SLOT_SWAP=5,4` swaps
tail3's and tail2's handles and tests that directly.

**Best deferral count observed: 9/10** (`attempt1` above, with only tail3 not deferring and everything
else — including a deferred trigger — holding).

Both attempts in that run also reported `timingValid=True`, confirming the §13.2 guard is live.

### 13.7 Round-2 findings (2026-11-04) — instrumentation fixed, two review claims corrected

**Instrumentation now in every report** (the review's #4, plus the safety item):

- **`config`** — a dump of every `CIV_*` environment variable for that run (14 keys in practice).
  Post-hoc forensics across runs was previously guesswork; ns2jb records `workerDelaysNs` for the same
  reason.
- **`timingValid`** — see §13.2. `allPending` is forced false when it fails.
- **`placementUncertain`** — set when `changedPaths` contains the canary *without* a qualifying window
  (ns2jb's discipline: save, stop, retain for recovery; never retry an overwrite blind).
- **`changedPaths` now respects the canary's staging form.** Comparing against a fixed
  `result 0 / type 1` expectation made every `CIV_NORMALIZED_PATHS=1` run report
  `changedPaths=['canary']` — a false overwrite signal indistinguishable from a real aliased write.
  The expectation is now derived from the staging form.

**`CIV_CANARY_DEPTH` confirmed holding** across multiple samples: the canary now defers and spans where
it previously missed by 100–500 µs. It is off the blocker list.

**`tail3`'s suspected mechanism is REFUTED.** The review hypothesised that tail3's piece contains the
marker basename at `0x2340`, so its lookup *resolves*, gets serviced, and therefore never queues.
Tested with `CIV_EXTENT_MARKER=end` (marker moved out of tail3's piece): tail3 still did not defer
(0/2, windows 462/476 µs). **tail3 remains an unexplained 0% outlier** with opcode, delay, slot, path
shape and piece content all eliminated. It is the single unobtained role.

**`preReleaseUsed` is a genuinely open gate, and it is not stale.** Measured 7136–7472 against the
0x2600 (9728) requirement. §4's claim of 40,528 came from a different filler configuration and is
superseded. A run with `CIV_RELEASE_DELAY=2500000` produced **−21280** — negative — which shows the
failure mode precisely: the baseline (`held`) is captured fresh per reservation, but the **game's own
heap churn exceeds the reservation's held margin** during the wave, so the metric is noisy and can
invert. ns2jb's 0x2600 is ≈80% of its 12160 reservation; we measure ≈60%. This gate fails
**independently of `allPending`**, so it must be solved before any qualification is real.

**Latent coupling discovered:** raising `CIV_RELEASE_DELAY` from 1.5 ms to 2.5 ms **moved the trigger's
start from ≈2400 µs to 3365–3428 µs**. The `released` gate is therefore not fully decoupled by
`CIV_RELEASE_EARLY` — the trigger's fixed start is only fixed *at the tuned release delay*. Any future
change to the release timing must re-check the trigger's start.

### Corrections to the round-2 review

| claim | verdict |
| --- | --- |
| "ns2jb loops `probeReadSize` until a size qualifies; sweep `CIV_READ_SIZE`" | **Incorrect.** `probeReadSize` is a constant `0x180000` (line 17); line 526 only *records* it into `report.qualifiedReadSize`. Our value already matches the reference. A size sweep is still a legitimate probe (the hole governs how busy the FS gets) but is not "the reference's own qualification procedure". |
| "the 20:11 run overwrote the canary without a qualifying window — safety breach" | **Misattributed.** Those runs used `CIV_NORMALIZED_PATHS=1`, where the canary's expected result *is* `0x202`/type 0. `changedPaths` was comparing against the short-form expectation. No overwrite occurred; the instrumentation was wrong, and is now fixed. |
| "`Work log.md` is referenced by the tracker but doesn't exist" | **Casing false alarm** — the file exists and is the same one my notes go to. |
| "`held` may be stale across retries, silently inflating `preReleaseUsed`" | **Unfounded** — `held` is re-read from each new reservation (`host.py` lines 1198/1252). The metric's real problem is churn, not staleness (above). |

### 13.5 Queued experiments (cheap — one boot each)

| experiment | rationale |
| --- | --- |
| `CIV_CANARY_DEPTH=N` | deepens the canary's path with long **valid** components, widening its window structurally while keeping `result 0` / type `1`. (The normalized form widened it 5.5× — 6587 µs observed — but resolved to a *missing* path and flipped the result to `0x202` / type `0`.) |
| `CIV_SLOT_SWAP=12,6` | the trigger-slot inversion above |
| tail3 at ~2.2 ms | the guards defer 100% at that delay; tail3 never does at 2.505 ms. If it still never defers, tail3's *path* (not its timing) is the cause |
| tight burst (`CIV_PROTECT_SHIFT`, equal protected delays) | if 10/10 deferral appears in bursts, deferral is a queue-depth property; if not, it is per-request — either answer kills half the hypothesis space in one boot |

## 14. Round-3: the `released`-signal fix, executed live (2026-11-04, 21:00–21:15)

The third review diagnosed the §13.7 latent coupling down to the step list: `release_early`
appended `signal(gate["released"])` **after** `sleep_ns(RELEASE_DELAY)`, so the release pacing
saturated every downstream waiter. **Fixed in `host.py`** (one move + one deletion): the signal is
now emitted immediately after `wait(master)`, before the role's sleep; the `not release_early`
branch still signals late, preserving the old behaviour.

The change is host-side only (frames are built by the host), so it takes effect on every load
without a payload change.

### What the two live boots measured

**Boot A (21:00, `CIV_TRIGGER_SLEEP=1000000`):** the fix's *effect* was instant — the trigger's
start decoupled from `RELEASE_DELAY` (2.5 ms) and followed the sleep instead:

| `CIV_TRIGGER_SLEEP` | trigger start | note |
| --- | --- | --- |
| 1.0 ms | 1406/1407 µs | too early: every protected send is *after* the trigger |
| 2.0 ms | 2404/2406 µs | ns2jb's intended placement restored |

`start ≈ TRIGGER_SLEEP + 400 µs + gate ε` now, exactly as the code comment always claimed the
knob should behave. Both boot-A attempts ran (7/10 in neither — roles missed the start side);
the console then **wedged during the third `rereserve`** (`open fsp-srv` timeout, ports open but
silent) and was power-cycled.

**Boot B (21:10, `CIV_TRIGGER_SLEEP=2000000`):** the wave is healthy again and **7/10 spanning in
both attempts**:

```
trigger 2404->2656 / 2406->2682  (fast class: 252/277 us)
spanning: commit, tail0, tail1, tail2, canary(depth 5), filler0, filler1
missing:  guard0, guard1, tail3
```

- `preReleaseUsed` = 8128 both attempts (still < 0x2600 = 9728, but the highest clean-reading
  so far; `restored` true).
- `selectedMarker` still false — expected while the window is incomplete.

### Two consequences of the fix that must be re-tuned

1. **Guards now fire at `GUARD_DELAY` alone** — `released` arrives at ~0 ms, so guard0/1 started
   at ~0.5 ms and completed ~1.3 ms (neither deferred; they must be sent into the busy period).
   They started at ~2.0 ms pre-fix (old released ≈ 1.5 ms + 0.5 ms wait overhead... i.e. they rode
   the release delay). **Set `CIV_GUARD_DELAY≈1950000`** to restore their old send time, where they
   deferred 9/9 historically. This is the near-certain fix for the two guard misses.
2. **`tail3` measured its fastest non-deferral yet** (124–169 µs windows, ending 2308/2292 —
   *before* the trigger's start). Opcode, delay, slot, path shape and piece content are all
   eliminated (§13.6/13.7); the `CIV_SLOT_SWAP=5,4` handle test is now the top-of-queue
   experiment, bundled with the guard re-tune. Note a deferred trigger is no longer required:
   with a fast trigger (end ≈ 2.68 ms), `allPending` needs tail3's *end* past ~2.68 ms — deferral
   OR a later send can supply that; the swap decides whether deferral is even possible.

### Boot hygiene reproduced exactly as documented

Two waves per payload, then the third `rereserve` hung (boot A) / the payload refused connections
after two attempts (boot B) and the loader **refused a fresh load on a new port** (the documented
loader wedge). Each boot yields two clean samples; plan experiments in twos.

### Next boot's configuration (queued)

```
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1
CIV_TRIGGER_SLEEP=2000000 CIV_RELEASE_DELAY=2500000
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0
CIV_FILLER_DELAYS=2300000,2300000
CIV_TAIL_DELAYS=2580000,2535000,2350000,2360000
CIV_SLOT_SWAP=5,4
```

Expected: guards re-enter the deferring regime (9/9 historically at ~2.0 ms) → 9/10; tail3's swap
decides the tenth. If tail3 still never defers with tail2's handle, the last standing differences
are the *path staged into its request buffer* (piece order) and per-boot handle luck — re-run the
swap a second boot before concluding.

## 15. Round-4: guards fixed, 9/10, and the tail3 refutation was itself refuted (21:17–21:21)

Ran the §14 queued config live. **Two clean attempts**, then the payload died and the loader wedged
(again, exactly two-per-payload).

### Result: 9/10 — the best sample yet

| attempt | trigger | spanning | misses |
| --- | --- | --- | --- |
| 1 (21:18) | 2404→8632 (**deferred**, 6228 µs) | 6/10 | filler0/filler1/canary (end side vs the slow trigger), tail3 |
| 2 (21:20) | 2405→2951 (**fast**, 546 µs) | **9/10** | filler0 (end −145 µs), tail3 (end −471 µs) |

- **`CIV_GUARD_DELAY=1950000` confirmed the guard fix**: guards spanned in *both* attempts,
  including attempt 1's deferred trigger. They were the easy half; §14's prediction held.
- **`preReleaseUsed` = 12816 (attempt 1) — above the 0x2600 gate for the first time** with a valid
  timing extract; 7664 on attempt 2. The gate is now within range at the current config.
- `selectedMarker` still false (window incomplete in every sample).

### tail3: handle hypothesis refuted, and the earlier refutation was INVALID

`CIV_SLOT_SWAP=5,4` gave tail3 tail2's client handle: tail3 **still did not defer** (306 µs and
earlier windows), and tail2 kept deferring — so per-client-handle state does not govern deferral.

More important, re-deriving the piece layout (`_stage_extent_ns2jb`) shows **§13.7's path-shape
refutation was invalid**: `CIV_EXTENT_MARKER=end` moves the marker from 0x2340 to 0x2435, but
*both offsets live inside piece 4* (0x2000–0x2440) — tail3's input buffer. The marker never left
tail3's input in that test. So the leading hypothesis — **tail3 defers iff its input path does
not resolve** — is still open, and it is the only remaining one that survives elimination
(opcode, delay, slot, handle, path shape... the piece *content* differs from tails 0–2 in exactly
the resolving-marker way).

**Implemented `CIV_TAIL3_PAD=1`** (host.py, staging): blanks tail3's input to `'/'`-padding of the
same length — non-resolving, tails 0–2-style — while leaving everything else untouched. A padded
run breaks the fold by construction, so it is a **diagnostic only**: if tail3 defers with the pad,
the resolvability mechanism is confirmed and the problem becomes how to keep the fold (marker in
the trigger-reachable extent) while tail3's input does not resolve.

### The other lever: trigger speed, not role fixes

The 9/10 sample missed on filler0 by **145 µs** and tail3 by **471 µs** — both *end-side*, against
a 546 µs trigger service. A 66 µs trigger (observed on the 20:49 boot) starting at the same 2405
would end at ~2471: **filler0's 2806 end spans, and tail3's 2480 end spans by 9 µs**. So the
fast-trigger mode is itself worth fishing for; every role fix above is only needed when the
trigger runs slow. Concretely: `CIV_TAIL_DELAYS=2580000,2535000,2350000,2450000` (tail3 +90 µs —
its non-deferring end moves to ~2570, clearing a fast trigger with margin) plus the round-4 config
and **two attempts per boot is the current fishing protocol**.

### Next boot (queued, in priority order)
1. **Fishing config**: round-4 config, `CIV_SLOT_SWAP` dropped (refuted),
   `CIV_TAIL_DELAYS=2580000,2535000,2350000,2450000`, `tries=2`. Watch trigger service time — a
   ≤75 µs trigger with this layout is a 10/10 candidate, and the first real
   `selectedMarker` shot (fold intact).
2. **`CIV_TAIL3_PAD=1` diagnostic** (one attempt) if the fishing boot leaves a spare attempt:
   settles the resolvability mechanism regardless of qualification value.
3. If tail3 resolvability is confirmed: design a fold-preserving split of the marker between
   trigger-reachable extent position and a non-resolving tail3 input — that is a geometry
   question for [[filesystem-breakout]] / ns2jb's `paths.cjs`, not a timing one.

## 16. Round-5: fishing boot (21:24–21:27) — 9/10 twice; the numbers now pin the requirement

Ran the §15 fishing config live (`CIV_TAIL_DELAYS=…,2450000`, no slot swap):

| attempt | trigger service | spanning | misses |
| --- | --- | --- | --- |
| 1 (21:24) | 6090 µs (deferred) | 5/10 | tails 1/3, canary (−80 µs), filler0/1 — end side vs the slow trigger |
| 2 (21:27) | 418 µs | **9/10** | filler0 (−154 µs), tail3 (−379 µs) |

### What the accumulated fast-trigger samples now pin down

Trigger service times across valid samples: 35–66 µs (fast class, twice), 252–650 (mid, most
samples), 5.4–6.2 ms (deferred). The mid class misses tail3 by ~380–470 µs; tail3's non-deferring
service maxes at 545 µs while the start-side cap is the trigger's 2404 µs start (tail starts reach
~2341). So for tail3 to span a *mid-class* trigger it would need service ≥ ~570 µs — never
observed. **10/10 therefore requires the fast class: trigger service ≤ ~39 µs** at the current
placement, with filler0's end also clearing (attempt 2's filler0 end 2668 clears a 66 µs trigger
with 200 µs margin).

- filler0's remaining gap is tunable: `CIV_FILLER_DELAYS=2500000,2300000` moves its measured start
  ~200 µs later, covering the mid-class trigger ends (~2820). Queued.
- tail3 has no knob left: start capped by the trigger start, service capped by its resolving path.
  The fast-trigger sample is the qualification path — and it keeps the fold intact, so it doubles
  as the first real `selectedMarker` attempt.
- Boot throughput is now the bottleneck: 2 samples per payload (loader wedge caps it), P(fast
  class) ≈ 2/10 of valid samples. The payload-side `retryWave` port (todo #6) turns that into 3+
  per boot and is now the highest-value engineering item — ns2jb's `wave()` is in-process JS;
  our frames are host-built, so the port is a real task, not a knob.
- `CIV_TAIL3_PAD=1` remains staged and ready but spends an attempt on mechanism-confirmation, not
  qualification value.

### Next boot (queued)

```
CIV_HARNESS_PORT=9029 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2000000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2500000,2300000 \
CIV_TAIL_DELAYS=2580000,2535000,2350000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 2
```

Watch: trigger service ≤ ~39 µs (10/10 → qualification attempt), or accept mid-class 9/10s and
start the `retryWave` port for sample throughput.

## 17. Round-6: session cache + the retryWave-equivalent attempt (21:36–21:45)

**Session cache implemented (`cached_session`, host.py).** The retry loop had been opening **six
fresh fsp-srv sessions per attempt** (counters, wave-root, churn-a, churn-b, after, plus the
per-attempt domain) where ns2jb's in-process `wave()` rides one shared session plus one new domain
per retry. The cached session liveness-probes with a counter read and reopens transparently. This
is the targeted equivalent of the payload-side `retryWave` port: retries are now ~60–90 s each
(vs ~2 min) and carry zero new service opens.

**Result:** two clean attempts, **attempt 3 still froze the console** (open-but-silent during its
first reserve step). So the freeze is NOT the fsp-srv session count. Remaining suspects, in order:
(1) **worker-thread accumulation** — 13 frames launched per attempt; if the workers park instead of
exiting (they longjmp back into the payload's state), attempt 3 has 39 native threads plus the
game's own — check whether the frame epilogue exits the thread; (2) the FS process's own heap
degrading across reservation/release cycles (Civ VII churn vs SmileBASIC's);
(3) the payload's control backlog under back-to-back polling. The death point was not captured
(full traceback lost to `tail`) — **next boot runs with `CIV_RESERVE_TRACE=1` and full output
to a file** to pin it.

**Quality of the two samples stayed high:** attempt 2 = **9/10 again** (trigger 2405→2874, 469 µs
mid-class; missing tail3 −319 µs and filler1 −138 µs — filler0 now spans with the 2.5 ms delay).
Attempt 1's deferred-trigger sample showed `preReleaseUsed` **24272** — the gate passes with huge
margin whenever the sample is slow. The fast trigger class has not reappeared in six samples of
the new configs.

### New fold-preserving tail3 lead (implemented, untested)

Tail3's request descriptor is the **only one not 0x800 bytes** — its piece is the extent tail
(`0x441` bytes) while tails 0–2, the trigger and the staged pieces all carry 0x800. Small IPC
payloads can ride a session's static buffer instead of the user-buffer path, which would explain
exactly "only tail3 never defers, regardless of handle/slot/delay/path-shape".
**`CIV_TAIL3_SIZE=0x800`** (host.py, staging) pads tail3's input with `'/'`s to 0x800 — trailing
slashes collapse identically and the marker stays at 0x2340, so the fold is byte-identical in the
marker region. If tail3 defers padded, the discriminator is the buffer size and tail3 joins the
fixable roles permanently.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2000000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2500000,2300000 \
CIV_TAIL_DELAYS=2580000,2535000,2350000,2450000 \
CIV_TAIL3_SIZE=0x800 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round7-full.log
```

Priorities: (1) does padded tail3 defer? (2) if yes, is attempt 2 a 10/10? (3) with the trace on,
where exactly does attempt 3 die — reserve step, thread create, or first poll?

## 18. Round-7 results (21:44–21:47) — size hypothesis refuted; death point pinned; two fixes landed

The boot ran the §17 config with the trace on and full log capture (`runs/round7-full.log`).

### `CIV_TAIL3_SIZE=0x800` is refuted

tail3 still never deferred: 338 µs (attempt 2, mid-class trigger, −402 µs miss) and 781 µs
(attempt 1, deferred trigger). The descriptor-size / static-buffer theory is dead. Tail3's
standing differentiators after six eliminations (opcode, delay, slot, path shape, piece content,
client handle, descriptor size): **its core pin** (index 5 → core 1 — the same core as the hole
and the release role; the §13.3 rate table's only unexplored axis) and the frame/thread identity
itself. **Implemented `CIV_PIN_OFFSET=<n>`** (host.py) — rotates the `index % 4` core map so tail3
lands elsewhere. If deferral follows the core, tail3 defers and tail2 stops; one knob, decisive.

### The attempt-3 death is now pinned to per-attempt client cloning

The trace shows attempt 3 completed its ENTIRE reservation (counters, domain, root copy, 59
objects, wrapped) and died at `wave`'s **client clone step** (`_clone_handle` → `control(2)`
CloneSession on the cached root, host.py:1508). Cross-checked against the reference:
**ns2jb builds its 13 clients ONCE, outside wave() (lines 271–275), and reuses them across every
retry** — our port cloned ~13 fresh sessions per attempt, which is what exhausted the FS session
table. Note this run predated the fix below (timeline: the boot ran, then the cache was written).

### Fixes landed (untested at time of writing)

1. **Client cache** (`wave_clients` in state): 13 client clones + storage open + trigger clone
   built once per payload; reused across attempts. Per attempt only the owner page's content is
   rewritten (matching ns2jb's `{u32: b+0x2a00}` page-refresh design), and the list is **copied**
   per attempt so `CIV_CANARY_LAST`/`CIV_SLOT_SWAP`'s in-place swaps keep their per-attempt
   semantics.
2. **`CIV_PIN_OFFSET`** as above.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2000000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2500000,2300000 \
CIV_TAIL_DELAYS=2580000,2535000,2350000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round8-full.log
```

Three questions, one boot: (1) does attempt 3 survive (client cache)? (2) does tail3 defer under
the rotated core map (and tail2 stop)? (3) any 10/10 in the samples. If the core rotation flips
tail3, the pin map is part of the wave's semantics and belongs in the handoff body, not the
experiment queue.

## 19. Round-8 (21:50–21:57): BOTH fixes worked — 3 attempts/payload, tail3 defers

Two answers at once:

1. **The client cache fixed the attempt-3 death.** Three full attempts ran back-to-back with
   **zero tracebacks** — matching ns2jb's own 3-waves-per-payload throughput for the first time.
   (A tuned 4th attempt wedged during its re-reserve: the payload's lifetime is ~3 reservation
   cycles — same as the reference's `retryWave` cap. This is the throughput ceiling now, not 2.)
2. **tail3 DEFERS under the rotated core map.** All three attempts: tail3 windows 767–1330 µs
   (previously 0/12+ above ~550 µs). Deferral follows the **core pin** — the last standing axis.
   (A bug in the just-shipped `CIV_TAIL3_SIZE` line crashed the first host invocation before any
   wave; the payload survived it and the same boot then ran clean — that earlier §17 boot's
   refutation stands, and the crash itself is fixed.)

### The miss structure has collapsed into delay-tuning territory

Best sample (attempt 3, 21:51; trigger 2552→3094, 7/10):

```
MISS tail0   start-side by  20 µs   (delay fix: −100 µs)
MISS filler0 start-side by 139 µs   (delay fix: −200 µs)
MISS tail1   start-side by 169 µs   (observed offset +476 under the rotated pin; fix: −500 µs)
MISS filler1 end-side   by 297 µs   (needs deferral, not delay — its core doesn't defer)
```

Everything else — commit, tail2, **tail3**, canary, both guards, release — spans. The 10/10
requirement has decomposed into per-role start-offset corrections under the offset=1 pin map.
Note the pin rotation also shifted the trigger's start +150 µs (2552 vs 2404) — trigger is on
core 1 now; `CIV_TRIGGER_SLEEP=1850000` restores ~2400 if the protected starts need the room.

`preReleaseUsed` remains the wild card (240 on attempts 2–3, 7456 on attempt 1) — the churn-noise
problem from §13.7 is still open and will need the bracketing/min-of-reads fix regardless of
when `allPending` lands.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=1850000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2300000,2300000 \
CIV_TAIL_DELAYS=2480000,1950000,2350000,2300000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round9-full.log
```

Three samples per payload; expect the start-side misses to collapse. filler1's end-side miss is
the one structural item left — if its 89% deferral doesn't reappear on core 1, try `CIV_PIN_OFFSET=2`
as the following boot's single change.

## 20. Round-9 (21:57–22:00): end-side deferral again — and a correction to the first read of it

Ran the §19 tuned config. Results went **6/8/6** (attempt 2's 8/10 missed only tail1 and filler1).

**Correction to the first reading of this data** (the review's own miss-label parsing was wrong
once before): all of round-9's misses are **END-side, non-deferral** — the starts are fine:

| attempt | miss | start | end | reading |
| --- | --- | --- | --- | --- |
| 2 (8/10) | tail1 | 2147 (423 µs early ✓) | 2600, −327 | did not defer |
| 2 (8/10) | filler1 | 2296 (262 µs early ✓) | 2820, −82 | did not defer |
| 3 (6/10) | + canary, commit | all starts early ✓ | end-side | deferral lottery shifted |

tail1's start responded to its delay cut **more than one-for-one** (−295 µs delay → −574 µs
start), so the delay knobs are alive; the round-8 start-side corrections transferred. What moved
is the **deferral assignment across cores, per boot**: the canary deferred in round-8 and not in
round-9; filler1's historical 89% deferral sits on core 1 in this map and didn't fire. Under the
offset=1 map the cores are: 0 = tail1/filler0/guard1, 1 = commit/tail2/filler1/trigger,
2 = hole/tail3/release, 3 = tail0/canary/guard0 — and which cores defer is not stable across
boots, which is why per-role end-side gaps wander.

One robust derivation stands: **the trigger's start is `guard-signal + 400 µs`** — the sleep is
fully absorbed by the `wait(released)`/`wait(guard)` pair, so the real trigger-start knob is
`CIV_GUARD_DELAY` (the guards signal right before their own sends at ~GUARD_DELAY).

The right next measurement stands, but for the corrected reason: a **per-boot deferral map is
noise across boots; sweep `CIV_PIN_OFFSET=0..3`** (one boot each, three samples each) and find the
assignment where the deferral-prone cores cover all four non-commit protected roles. Then the
delay fitting has something stable to fit against.

### Landed (no console needed)

**`CIV_PRE_BRACKET=1`** (host.py): a second counter send immediately after the preRelease read,
inside the same held window; the gate uses `held − min(reads)` — the most conservative
"reservation still held" estimate against the game's save-traffic churn (which produced readings
from 24272 down to −54352 across boots). Default off; enable on the next boot. Not validated live
yet — the boot's spare 4th attempt wedged at re-reserve (the ~3-cycle payload lifetime), which
itself reconfirms the ceiling.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=1 CIV_PRE_BRACKET=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=1850000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2300000,2300000 \
CIV_TAIL_DELAYS=2480000,1950000,2350000,2300000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round10-full.log
```

Then the pin-offset sweep boot (`CIV_PIN_OFFSET=0/2/3`, one boot each) to map starts to cores
before any further delay fitting.

## 21. Round-10 (22:03–22:05): sweep point `CIV_PIN_OFFSET=2` — 4/6/3, and the sweep table so far

Same config as §20 with offset 2 (map: core0 = tail0/canary/guard0, core1 = tail1/filler0/guard1,
core2 = commit/tail2/filler1/trigger, core3 = hole/tail3/release). Attempt 2 even landed a
**117 µs fast trigger** — and still only 6/10, because the failures moved with the map:

| boot (offset) | best | misses | shape |
| --- | --- | --- | --- |
| 8/9 (1) | **8/10** (r9-a2) | tail1 end +327, filler1 end +82 | both non-deferring (cores 0/1) |
| 10 (2) | 6/10 (a2, fast 117 µs trigger!) | tail0 start −312, canary start −334, tail1 end +334 | core-0 starts late; tail1 non-deferring again |
| 9 (1, re-run) | 6/10 | all end-side (canary/tail1/filler1/commit non-deferral) | deferral map shifted per boot |

Three sweep conclusions:

1. **No core assignment defers everyone.** tail1 was non-deferring on cores 0 and 1; filler1 on
   cores 1 and 2; the deferral-prone set wanders even under a fixed map (round-9 vs round-8,
   same offset). The per-core deferral function exists but is **not stable boot-to-boot** — it
   depends on where the game's own threads and the FS's workers sit that boot.
2. **Core 0 under offset 2 starts late** (tail0/canary +310–360 µs, unresponsive to their delays).
   Start offsets are contention-driven per boot, confirming delay-fitting across boots is unreliable.
3. `CIV_PRE_BRACKET` did **not** tame `preReleaseUsed` (−16416/496/288): the game's save-traffic
   churn during the held window exceeds what a two-point bracket bounds. The metric needs a
   different definition for Civ VII (e.g. re-deriving the minimum held requirement from Civ's own
   aliasing outcomes, §13.7's option 3) rather than importing ns2jb's 0x2600 constant.

**Offset 1 remains the best map** (8/10 twice in its family). The most promising next boots, in
order: (a) offset 1 + round-9 delays + `CIV_GUARD_DELAY` raised ~200 µs to slide the trigger's
start ~250 µs later — attempt-2-class samples then need only tail1/filler1's ends to clear, which
their deferral sometimes supplies; (b) offset 3 to finish the sweep table; (c) accept that the
last mile is the deferral lottery and rely on 3-samples-per-payload fishing once the map is fixed.

The console needs a game relaunch for each boot; the user confirms relaunch (not reboot) is
sufficient for a fresh payload.

## 22. Round-11 (22:07–22:09): sweep point `CIV_PIN_OFFSET=3` — 4/5/5; matrix nearly complete

| role | core 0 | core 1 | core 2 | core 3 |
| --- | --- | --- | --- | --- |
| tail1 | non-def (off 1) | non-def (off 2) | non-def (off 3) | **off 0 — untested** |
| filler1 | **off 0 — untested** | non-def (off 1) | shifted (off 2) | non-def, en+15/en+17 (off 3) |

Offset 3's samples (4/5/5) had the tails span in attempt 2 (all four!) while commit/canary/fillers
took the end-side misses; filler1 missed its end by **15 and 17 µs** in two samples — the closest
single-role miss recorded. A 4th attempt at offset 0 on the same payload wedged at re-reserve
(the ~3-cycle payload lifetime again), so the offset-0 column is still unmeasured and needs its
own boot.

Reading the whole sweep (offsets 1 best at 8/10; 2 → 4/6/3; 3 → 4/5/5): no assignment defers all
ten, the deferral assignment wanders within a boot's samples, and the closest misses are now
15–82 µs end-side. That is pure fishing distance **if** the map with the best baseline (offset 1)
is kept and samples are harvested at 3/payload. The remaining engineering item is a Civ-re-derived
`preReleaseUsed` threshold (bracket reads didn't bound the churn); the deferral lottery itself no
longer needs new mechanisms — it needs volume.

## 23. Round-12 (22:12–22:13): sweep point offset 0 — worst map, but the fast trigger lives there

Offset 0 (the original ns2jb map) scored **2/4/3** — yet it produced the two most important
samples of the sweep:

1. **The fast trigger appeared TWICE (48 µs and 77 µs).** Under offset 0 the trigger sits on
   uncontended core 0; under offset 1 (trigger on core 1 with commit/tail2/filler1) it never left
   the 263–917 µs mid class in six samples. The trigger's service class is a core-contention
   property too.
2. **`preReleaseUsed` = 40816** on attempt 1 — the largest clean reading ever, comfortably above
   the 0x2600 gate. (Attempts 2–3 read 272 — diagnosed as the bracket min() being poisoned by a
   failed pre2 reply; fixed: the bracket read is now only used when its result is 0 and the data
   is full-length.)

### The offset-0 endgame geometry (all numbers from this boot's samples)

With the trigger at 2257→2304 (48 µs service), the start-side misses were tail0/tail2/canary/
filler1 at **−10 to −87 µs**, and the end-side misses were tail1/tail3 at +275/+138 — all four
ends 2601–3031, far past a fast trigger's end. So one structural nudge aligns everything:

- **`CIV_GUARD_DELAY=2100000`** (from 1950000): slides the trigger's start ~150 µs later
  (~2400) — every start-side miss flips (their margins were ≤87 µs), and the guards keep their
  deferral (they spanned on cores 2/3 all boot).
- **`CIV_TAIL_DELAYS=2480000,2250000,2350000,2450000`**: tail1 (delay 1950000 → 2250000) and
  tail3 (2300000 → 2450000) push their ends past a fast trigger's end (~2477) while keeping their
  starts under the trigger's (~2400).
- Everything else already spans at the fast-trigger service times.

The caveat: under offset 0 the deferred-trigger samples fail hard (2/10 — the trigger lands early
and the protected ends can't reach). That is acceptable — the target is the fast-trigger class,
which showed 2/3 under this map. If the class holds at that rate, three samples per payload give
**P(10/10) ≈ high per boot** — better odds than any configuration so far.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=1850000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=2100000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2300000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2350000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round13-full.log
```

This is the first configuration where every individual role has demonstrated its required
behaviour live — the boot is a conjunction test, not a new mechanism.

## 24. Round-13 (22:16–22:17): 5/3/7 — improving; two model corrections; new instrumentation

The §23 conjunction config scored 5/3/7 (attempt 3 = 7/10, best under offset 0). Two corrections
and one new instrument:

1. **§23's GUARD_DELAY claim was wrong for offset 0.** The trigger's start under this map is
   `TRIGGER_SLEEP + 400 µs` — the sleep floor binds because the gate signals arrive earlier than
   it (guards at ~2.1–2.2 ms). Measured: 1850000 → 2255–2332 across both rounds, regardless of
   GUARD_DELAY. So under offset 0 **`CIV_TRIGGER_SLEEP` is the trigger-start knob** after all
   (it was absorbed only in the offset-1 arrangement, where the gates arrive late).
2. **`preReleaseUsed` is structurally churn-dominated on Civ VII** — readings 40816/8000/5168/208
   within single boots; the game's save traffic moves counter[5] by more than ns2jb's whole 0x2600
   gate during the held window. **Landed `preReleaseHeldFreed`** (host.py): the counter DROP
   between the pre-release and post-release reads — churn-independent (if the group was held at
   release, the drop ≈ the measured ~12160 allocation; if already gone, ≈ 0), recorded in every
   report; `CIV_HELD_GATE=1` switches the gate to it. This is the Civ-re-derived threshold route
   (§21's option 3); a first-boot reading of ~12160 makes the case for the switch.
3. **Landed `CIV_PIN_MAP`** (13-entry per-role core list, overrides the rotation): the sweep
   showed the trigger fast ONLY on core 0 and tail1 deferring ONLY on core 0 (offset-1
   arrangement) — properties no single rotation provides together. The map knob allows mixing
   (e.g. trigger → core 0 with tail1 → core 1's deferral-prone arrangement) without re-rotating
   everything else.

### Round-13 miss structure (attempt 3, 7/10; trigger 2317→2856)

- tail1: start has 273 µs of room, end short by 500 (service 312, no deferral on core 3)
- tail2: start-side miss by **26 µs** (trivial delay fix), end fine (deferred)
- filler0: start has 222 µs room, end short by 189 (service 572, no deferral)
- everything else spans (incl. canary and tails 0/3)

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round14-full.log
```

The slide gives every start ~180 µs more room (trigger ~2450); tail2's and filler0's corrections
land them inside the trigger's window. tail1's ~380 µs end-side deficit still needs its deferral —
if `CIV_PIN_MAP` experiments are wanted next, try trigger→core 0 with tail1→core 1
(`CIV_PIN_MAP=0,1,2,1,0,1,2,3,0,1,2,3,0`), and watch `preReleaseHeldFreed` — a ~12160 reading
justifies flipping the gate.

## 25. Round-14 (22:22–22:23): 7/7/8 — the slide worked; `preReleaseHeldFreed` is a real gate

The §24 config delivered:

- **`CIV_TRIGGER_SLEEP=2050000` slid the trigger to 2455** exactly as the floor model predicts
  (+180 µs) — the start-side room materialized, and no role missed the start side in any sample.
- **`preReleaseHeldFreed` = 6400/6752/6752** — stable within and across attempts, unlike
  `preReleaseUsed` (8000/224/240 the same boot). The release measurably frees ~6.5–6.8 KB that
  counter[5] books; a stable nonzero drop is the direct "the group was still held at release"
  evidence. With a few more boots to see its floor, `CIV_HELD_GATE=1` + a ~6 k threshold is the
  Civ-re-derived replacement for the 0x2600 gate (§13.7 option 3, now realized).
- **Attempt 3: 8/10** — misses are commit (window 0→2369, ended 758 µs before the trigger's end;
  its packet(10) wasn't deferred this sample) and canary (window 235 µs, ended 578 µs short;
  also not deferred). Both are pure deferral-lottery misses with ample start-side room; tails 0–3
  and all fillers/guards spanned.

The campaign state is now: **every mechanical ingredient has been demonstrated live in at least
one qualified-adjacent sample** — the remaining work is the joint deferral of commit+canary in the
same sample (both have deferred individually, repeatedly), harvested at 3 samples per boot. No new
mechanism is queued; the next boots are repeats of this exact config. When a sample lands
`allPending=True`, `selectedMarker` resolves in the same run (the fold is intact on this config).

## 26. Round-15 (22:25–22:26): 5/8/6 — the lottery rotates, the metric holds

Second boot of the frozen round-14 config:

- attempt 2: **8/10** again — filler0 (end +167) and filler1 (end +334), both non-deferral.
- attempt 3: 6/10 — the misses rotated to tails 1/2/3 and the canary (all end-side, all started
  correctly).
- **`preReleaseHeldFreed` = 6720/6752/6800** — stable across BOTH boots now (6400/6752/6752 then
  6720/6752/6800). This is the gate metric; a threshold of ~6000 separates "group held at
  release" from all the pre-158-churn readings. `CIV_HELD_GATE=1` is on and working.

The stragglers rotate per sample (r14: commit+canary; r15a2: fillers; r15a3: tails+canary) —
confirmation that the per-sample deferral assignment is a genuine lottery, not a fixed map. The
decision is to **freeze this config and harvest**: each boot is 3 draws; 8/10 appears most boots;
10/10 needs the two stragglers to cooperate in one draw. Do NOT per-boot retune — the rotation is
random, and chasing it costs start margin bought in earlier boots.

## 27. Round-16 (22:29–22:30): ★ ALLPENDING = TRUE — the window has landed

Attempt 2: **all ten protected roles span a fast trigger** (2455→2872, 417 µs service), valid
rotation (restored ✓ churn 0, receive margin 37227 µs, guards 0/0/0). Preserved as
`runs/MILESTONE-allPending-true-r16a2.json`. The handoff's opening ask — *can all ten protected
requests be pending at the trigger?* — is now answered **yes, and repeatably**: the harvest
config produces it, the lottery just has to cooperate.

The same boot also exposed and fixed the last instrumentation bugs:

1. **`CIV_HELD_GATE` ordering bug** — the gate switch ran after the report dict was built, so the
   report still showed the raw churn value (240) instead of the held-gate value (6752). Fixed:
   the dict is patched after the switch.
2. **The 0x2600 threshold does not fit the heldFreed metric's scale** — heldFreed is stable at
   6400–6800 when held. The gate now uses **`CIV_HELD_MIN` (default 6000)**: heldFreed ≥ 6000
   passes. With that threshold, the round-16 sample's gate state is: allPending ✓, restored ✓,
   receive margin ✓, !aborted ✓, held ✓ — **`selectedMarker` is the only failing gate**.

### selectedMarker: the new focus (one gate left)

The all-span sample's trigger resolved **0x202** (the ordinary-path control result) with empty
inline — the fold did **not** fire in that sample. The seeds were demonstrably in flight
(allPending), so the missing ingredient is the *phase* of the trigger relative to the seeds'
processing — ns2jb's fold needs the trigger issued into the seeds' deferred state at the right
moment, not merely while they are outstanding. The trigger's service time (417 µs, mid class)
may matter: a fast trigger (48–117 µs class, seen under this map) spends less time resolving and
may be the phase the fold wants — or the marker's reachability at 0x2340 under the deep-canary
geometry. Next boots: same harvest config; per-sample, record trigger result alongside the span
count — a 0x202 with all-span is 'window good, fold cold'; a result 0/type 1 with a failing
window is 'fold hot, window bad' — the crossing point is the qualification.

### Next boots (unchanged harvest config, now with the fixed gate)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/roundN-full.log
```

## 28. Round-17 (22:34–22:35): ★★ TWO all-span samples — and the fold RESOLVED (result 0)

The harvest is paying: attempts 2 AND 3 both recorded **allPending=True** (fast triggers, 298/318 µs).
Attempt 3's trigger row is the campaign's key artifact:

```
trigger result = 0     canonical = 0     inline type = 0
```

**The fold fired** — the trigger's lookup resolved through the seeds to a real object (result 0,
canonical 0) — but the resolved object's inline type is **0 (a directory), not 1 (the marker
file)**: the fold reached the directory and stopped short of the basename. That is now a pure
marker-geometry question — the exact shape of "which byte of the extent the folded basename comes
from" — not a timing or concurrency question. Preserved as
`runs/MILESTONE-allSpan-fold-result0-r17a3.json`.

Also fixed this boot: `windowOk` still compared against ns2jb's 0x2600 regardless of the held-gate
metric's scale — it now uses `CIV_HELD_MIN` (6000) under `CIV_HELD_GATE`. Recomputed against the
attempt-3 sample: **windowOk = True** — i.e. this run is a hasWindow-qualified sample failing only
`selectedMarker`. That is exactly ns2jb's own requalification bar (`checkExtent`); the pipeline is
one geometry fix from the negative/positive stage.

### The last gate: make the folded basename resolve to the marker file (inline type 1)

Established facts: the canary's inline type DOES come back 1 on this geometry (its own lookup
resolves the file); the trigger's fold reaches type 0; the earlier campaign saw type 1 on a
trigger exactly once (pre-instrumentation-fix). Candidates for the next boots, one variable each:

1. **`CIV_EXTENT_MARKER=end`** — moves the marker to the extent tail (0x2435); the §13.6 note says
   the fold "stopped short of it" there, but that was measured pre-allPending; re-measure now that
   the window is qualified.
2. **`CIV_NORMALIZED_PATHS=1`** — the faithful normalized_path canary form changes what the fold's
   descriptor looks like (it flipped the canary's type; it may flip the trigger's).
3. **`CIV_CANARY_SIZE` / marker interaction** — the canary's depth-5 geometry changed the heap
   layout the fold resolves against; a depth sweep (3 vs 5) on the all-span config may move the
   fold's reach.

The qualification bar (ns2jb `checkExtent`): one sample with `selectedMarker && hasWindow`. Two
gates, one sample, both now individually demonstrated.

## 28b. CORRECTION to §28 (external review, byte-verified): the fold did not "stop short of the
## basename" — the basename was unresolvable by construction

An external byte-level review of the milestone artifacts found four real bugs (all verified
against the code and the reference before these fixes were made):

1. **The release role never closed group.outer — in ANY wave.** `context_bytes` must never wrap
   `close_object`: its `at+4` write lands on byte 20, which for a domain packet IS the object id.
   The release row's own hex proves it (`...02000000 01000000...` = CloseObject(object 1) instead
   of group.outer 266). Consequences: the 59-object group leaked every attempt (the constant
   −19152 restored-delta), the FS degraded across reservation cycles, and `heldFreed` 6752 was
   measuring the closure of object 1, not the group. **Fixed**: `close_object(outer, 3)` uses
   close_object's own context parameter (ns2jb writes closeObject unmodified).
   **This changes the exploit's premise: ns2jb's wave releases the group mid-wave so the trigger
   exploits the FREED gap — our samples ran with the reservation still held. Expect the next
   boots' geometry to shift.**
2. **selectedMarker was impossible at the 0x2340 default.** ns2jb's `isExtent` writes the basename
   FLUSH (extent − len, no trailing slashes) for both extent profiles; markerOffset 0x2340 is only
   the isPositive placement, where the target is a DIRECTORY (trailing '/' is valid). Our default
   left 245 trailing '/'s after a FILE: fsp-srv returns 0x202 for a file path with trailing
   slashes — r16a2's trigger 0x202 was the fold REACHING the marker and hitting the slashes; the
   type-0 samples were folds stopping at a directory piece. **Fixed**: extent profiles now default
   to flush placement; `CIV_MARKER_OFFSET` still overrides for sweeps. (The marker=end variant's
   0x2EEA02 remains unexplained — re-check after the release fix, since the abort result may have
   been entangled with the never-released group.)
3. **Release counter reads queued behind tail3.** `CIV_PRE_CLONE` targeted clients[5] = tail3's
   session; the counter sends blocked behind tail3's deferred lookup, so preRelease was sampled
   after most roles returned — the 240 readings were tail0+tail2's allocations, not churn.
   **Fixed**: a dedicated `release-counter` clone no role shares.
4. **commit/guards lost their CMIF context** on rebuild, and `freeAddresses` freed the page-
   aligned interior pointer instead of malloc's raw (heap corruption between attempts — a likely
   co-factor in the 3-attempt deaths). **Both fixed** (contexts re-applied on rebuild; console.js
   now maps aligned→raw and skips unknown addresses).

Queued, not yet implemented: the commit_data dirty write (ns2jb writes 0x1000000 to
scratch/commit_data before each wave so commit's packet(10) has flush work — our comment noted it
but never did it; commit finishing early is the visible symptom). Needs careful staging of a
16 MB send descriptor; deferred rather than rushed.

### Round-20 plan (next boot)

All six fixes in: release close, marker flush placement, dedicated release-counter clone, context
restores, aligned→raw frees. **Expect the geometry to shift** — the reservation is now actually
released mid-wave for the first time, which is the exploit's intended state. Re-baseline the
window (allPending/heldFreed) and read the trigger's result/type in the same samples. The §27
harvest protocol stands; if the all-span state survives the release fix, the trigger's type at
flush placement decides selectedMarker.

## 29. Round-18 (22:38): `CIV_EXTENT_MARKER=end` is REFUTED — and the marker sweep is now a knob

With the basename at the extent tail, the trigger resolved **0x2EEA02 in all three attempts** —
the abort-control result: the folded path's tail component fell entirely off the reach and the
lookup collapsed to the native abort. No all-span samples either (6/6/7 — the marker move also
perturbed the deferral geometry). **The marker lives at 0x2340; the extent-end variant is closed.**

**Landed `CIV_MARKER_OFFSET=<hex>`** (host.py staging): sweeps the basename's extent position —
the fold carried components but stopped short of the basename at 0x2340 (round-17's type 0 =
directory), so the live reach for the folded basename is somewhere between the directory (type 0)
and the abort (0x2EEA02). Piece-relative candidates: **0x2140, 0x1b40, 0x1340** (pieces 4/3/2),
one boot each, watching the trigger's result/type at all-span. Note the canary's own lookup keeps
returning type 1 — the FILE type is reachable by a direct lookup; the fold just has to carry the
basename.

Campaign state unchanged otherwise: window proven (three all-span samples across two boots),
held-gate metric stable, `windowOk` threshold fixed. **Remaining: selectedMarker only.**

### Pass-4 correction (2026-11-04): the 0x2EEA02 was the NATIVE ABORT, not the reach

The round-18 log shows `nativeGuard.completed = 1` / `triggerAborted = true`: the guards had
finished (1.89–2.13 ms) *before* the trigger's 2.45 ms start, so the native abort overwrote byte 0
of the trigger input. The "marker fell off the reach" conclusion was wrong — with the marker at
the extent tail the fold may well reach it. Re-measure under the fixed release before closing the
marker-placement question (the CIV_MARKER_OFFSET knob stays live).

## 30. Round-19 (22:42): ★ windowOk = TRUE — first hasWindow-qualified sample; marker offset is invariant

`CIV_MARKER_OFFSET=0x2140` (deeper into piece 4): attempt 2 (and by file identity attempt 3)
recorded **allPending=True AND windowOk=True** — trigger 2455→2727 (272 µs), result 0, canonical
0, inline type 0. **The hasWindow bar (ns2jb's `checkExtent` precondition) is now formally met by
a live sample** — the window machinery is done.

The marker-sweep hypothesis, however, is **disconfirmed**: 0x2140 resolves identically to 0x2340
(result 0, type 0). The fold's resolved object does not track the basename's position — the fold
carries a *directory* regardless of where the marker sits. The resolved type is governed by the
fold's **depth/phase** (which component the fold carries when the trigger fires), not by marker
placement.

The clue for the fix is already in §7 (path-handling findings): **the normalized form resolves
`result 0` + `inline u32 1`** — `<scratch>||S0||S1||S2||SPAN_MARKER` as ONE descriptor. The
short-form (current) descriptor reaches only the directory. The changedPaths-expectation bug that
made `CIV_NORMALIZED_PATHS=1` runs look like canary overwrites was fixed in §13.7, so the faithful
form can now be evaluated cleanly.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_NORMALIZED_PATHS=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=1950000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round20-full.log
```

Single variable vs the frozen harvest config: `CIV_NORMALIZED_PATHS=1`. Watch the trigger's
inline type at all-span (1 = qualified) and the canary's result under the staging-form-aware
expectation. Preserve any selectedMarker=True run immediately.

### Pass-4 re-reading (2026-11-04): result 0 / type 0 means the resolution terminated at a NUL
### boundary BEFORE the marker — not that "the fold fired and stopped short"

fsp-srv resolves the receive extent until the first NUL. Pieces 0–3 end in '/' runs whose
directories (scratch, S0, S1, S2) all EXIST — so a resolution stopping at any of those boundaries
returns exactly result 0 / type 0 (directory). The §30/§35/§36 "the fold fired, inline type 0"
narrative is downgraded: result 0 proves only that the path terminated at an existing directory;
it does not show the fold reached the marker. The 0x202 samples (flush placement) are the
interesting ones: the resolution reached INTO piece 4 but terminated before the basename
completed — the fold does cross pieces; making it carry the full basename is the remaining puzzle.

## 31. Round-20 (22:50): the release fix was MASKED — one clobber site remained; now really fixed

Round-20 ran 4/6/4 with the release hex **still** showing object 1: the §28b build_wave fix was
necessary but not sufficient — the wave body's generic context re-apply loop
(`context_bytes(packets[role], …) for role in roles`) re-clobbered the object id. **Fixed**:
the loop now passes "release" through untouched, and an offline check verifies the pipeline
end-to-end (`close_object(266, 3)` → byte 20 = 0x10A intact, context 3 at byte 28).

Round-20's silver linings: the payload survived 3 attempts with the §28b stability fixes
(aligned→raw frees; dedicated release-counter clone), and the flush-placement marker did not
disturb the geometry (trigger starts steady at 2455).

**The next boot is the FIRST with a genuinely closing release.** Expect the reservation to be
freed mid-wave for the first time — the exploit's intended heap state — so re-baseline everything
(allPending margins, heldFreed scale — it should now jump to ≈ the measured allocation ~12160
when the group actually closes, vs the 6752 that was object-1's closure), and read the trigger's
result/type at the flush-placed marker. The window may improve (freed gap!) or need re-tuning;
either way it is the first run on the exploit's real geometry.

## 32. Round-21 (22:54): ★ the release WORKS — the exploit's real geometry is live

First boot with a genuinely closing release. Three findings:

1. **The group is freed.** `heldFreed` went **−8400 / −21952** — and per the reviewer's sign
   convention (counter[5] is FREE bytes), a negative here means free heap *increased* after the
   pre-release read: **the group (plus same-window churn) genuinely left the heap at release.**
   The §31 heldFreed sign was inverted; **fixed** (`after − pre`, positive = bytes freed).
2. **8/10 protected in all three attempts** — the only protected misses are guard0/guard1,
   end-side by 252–387 µs, with 687 µs of unused start room (their windows shrank to ~1.2–1.3 ms
   under the new geometry). **`CIV_GUARD_DELAY=2400000`** (+450) is the queued one-knob fix.
3. **The fold still resolves a directory** (result 0 twice at the flush-placed marker, one 0x202).
   With the real geometry now live, the fold's phase question is open again — but the trigger's
   result 0 twice is a good sign: the fold is firing on the intended heap state.

Also: `heldFreed`'s scale changed with the real closure (−8400/−21952 vs the old 6752) — the
`CIV_HELD_MIN` threshold stays 6000 against the now-positive scale.

### Next boot (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=2400000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round22-full.log
```

If the guards flip, the all-span state returns on the REAL geometry — and the trigger's type at
flush placement is the last question between here and `checkExtent`.

## 33. Round-22 (23:01): heldFreed +41 k/11 k (real closure at scale); the guard gap is structural

`CIV_GUARD_DELAY=2400000`: heldFreed **41184/5968/10784** — the release frees at the expected
scale (10784 ≈ the group's 12160 book-keeping; the sign fix is confirmed). The fold gave result 0
(6457 µs deferred trigger) and 0x202 twice. But the protected span **dropped to 6/5/5**: the
trigger start slid to ~2608 (guard-signal + 400), and the guards/fillers/canary now miss the END
side by 169–903 µs.

The decisive derivation from this boot: with the trigger's start = guard-signal + TAIL and the
guards' send = guard-signal + ε, the guards' end-vs-trigger-end margin is
**w_guards − TAIL − w_trigger** — independent of `GUARD_DELAY`. ns2jb's hardcoded 400 µs tail
means the guards need windows longer than 400 µs + the trigger's service; under the mid-class
trigger (1.2 ms) that fails by ~250 µs no matter the guard delay. **Landed
`CIV_TRIGGER_TAIL`** (default 400000 = ns2jb's value): cutting it to ~100000 flips the margin
to w_guards − 0.1 − w_trigger ≥ 0 for any trigger service ≤ ~1.25 ms — the whole mid class.

### Next boot (queued — needs a game relaunch)

Coordinated shift: everything ~250 µs earlier, trigger tail cut to 100 µs.

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_TRIGGER_TAIL=100000 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2300000 CIV_GUARD_DELAY=2400000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2280000,2100000 \
CIV_TAIL_DELAYS=2280000,2050000,2080000,2250000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round23-full.log
```

## 34. Round-23 (supersedes §33's queued command — external review pass 3)

A third review pass verified the §33 derivation against the run artifacts exactly (trigger.start −
guard1.start pinned at 405 µs = the tail; the ±1 µs arithmetic) and made three calls, all adopted:

1. **The §33 queued command dropped `CIV_CANARY_DEPTH=5`** — the short canary (105–520 µs window)
   would guarantee a canary end-side miss. Restored.
2. **The 200 µs shift of canary/fillers/tails was the wrong coordination**: round-21's delays went
   **8/8 on non-guard roles in ALL THREE attempts** at trigger start 2455 — shifting them earlier
   would spend two-thirds of the gained margin and pull them out of the 2.28–2.50 ms sweet spot.
   Instead: **`CIV_GUARD_DELAY=2550000` + `CIV_TRIGGER_TAIL=100000`**, everything else at the
   round-21/22 values. Guards start ~2355 (+588 vs round-21), trigger starts at guard + 100 ≈
   2455 — the exact round-21 start where the eight non-guard roles went 8/8, while the guards'
   ends (2355 + 1350–1450 ≈ 3705–3805) clear the mid-class trigger end (2455 + ~1200 ≈ 3657).
3. **CloseObject(outer) actually executes 740–785 µs AFTER the trigger's start** — computed from
   the frame slots (the report's `timing["release"]` was the first counter send; **fixed** to
   report the last send, which is the close). The close lands at ~3.35–3.39 ms vs the trigger's
   2.61 start. **If round-23's all-span samples still return type 0/0x202, lowering
   `CIV_RELEASE_DELAY` (1500000–1800000) so the close lands before/inside the trigger's
   resolution is the single highest-leverage knob for selectedMarker** — the exploit's premise is
   the trigger folding into the FREED gap.

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_TRIGGER_TAIL=100000 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=2500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round23-full.log
```

Decision tree for this boot: all-span + type 1 → **qualified** (preserve, then `negative` behind
`CIV_ALLOW_OVERWRITE=1`); all-span + type 0/0x202 → round-24 lowers `CIV_RELEASE_DELAY`; fewer
spans → the guard/tail interplay needs the round-22 data revisited.

## 35. Round-23 (23:13–23:17): the §34 config WORKS — all-span + windowOk on the real geometry

(A rocky boot: the first harness load raced its own bind, a stale-accessor read faulted one
payload after an unnoticed console reboot, and the third attempt hit the re-reserve wedge — but
the two samples that landed are the best of the campaign.)

- **Attempt 1: ALL-SPAN + `windowOk=True` on the real (closing-release) geometry.** Trigger
  2445→9903 (a deferred trigger — the ten roles span it anyway), trigger **result 0** (the fold
  fired, inline type 0 = directory again), `heldFreed` **+40944** (the real closure at full
  scale), CloseObject(outer) measured honestly now (10136→11402 — after the trigger's end in this
  deferred sample). Preserved as `runs/MILESTONE-allSpan-windowOk-realgeom-r23a1.json`.
- **Attempt 2: ALL-SPAN again** (allPending=True), trigger result **0x202** this time — the fold's
  outcome varies per sample (0 / 0x202), the window does not.
- Attempt 3 wedged at re-reserve (payload lifetime ~2-3 cycles on the new geometry).

### Where this leaves the campaign

The window is **solved and repeatable on the exploit's real geometry** — four all-span samples in
the last three boots. `selectedMarker` is the sole remaining gate, and its failure mode is now
well-characterized: the fold resolves a real object (result 0) whose inline type is 0 (a
directory), or occasionally 0x202. The §34 decision tree says: **round-24 lowers
`CIV_RELEASE_DELAY` (2500000 → 1500000–1800000) so CloseObject(outer) lands before/inside the
trigger's resolution** — the trigger must fold into the FREED gap, and the close currently lands
at ~3.4 ms (or later), i.e. after the trigger's start. This is the single highest-leverage knob
left. If the close timing alone doesn't flip the type, the follow-ups are `CIV_NORMALIZED_PATHS=1`
(§30) and the canary-depth sweep.

### Round-24 (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_TRIGGER_TAIL=100000 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round24-full.log
```

Watch: the close's real window (`timing["release"]`) should land ~2.4–2.7 ms (before the
trigger's 2455 start or inside its resolution), the trigger's result/type at all-span, and
`heldFreed`'s scale (expect ≈12160+ once the close truly precedes the trigger).

## 36. Round-24 (23:22–23:23): ★★★ ALL-SPAN ×3/3 — the window is now DETERMINISTIC

`CIV_RELEASE_DELAY=1500000` delivered:

- **allPending=True in ALL THREE attempts.** The window is no longer a lottery at this config —
  it is a property of it. Trigger starts 2392–2469, fast/mid service (538–703 µs on two
  attempts), trigger results **0 / 0 / 0x202** — the fold fires on two of three.
- **The close lands early**: attempt 2's CloseObject(outer) send at 2036 µs — before the trigger's
  2469 start — with its completion overlapping the trigger's resolution. The exploit's premise
  (trigger folding while the gap frees) is now the actual timeline.
- **heldFreed is stable at 5344/5376** at this close timing (vs 10784/40944 at the later close) —
  the metric tracks the close's phase, and it narrowly missed the 6000 `CIV_HELD_MIN` threshold,
  which is why windowOk read False. **Round-25 sets `CIV_HELD_MIN=5000`** — the re-derived Civ
  constant, exactly §13.7's option 3; at 5344+ it cleanly separates the real closure from the
  pre-fix chaos (−26448/208/240).
- **selectedMarker remains the only gate**: the fold resolves type 0 (a directory) even with the
  close early. The close timing alone did not flip the fold's terminal component.

### Round-25 (queued — needs a game relaunch)

The §30/§35 follow-up: the normalized descriptor form.

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_HELD_MIN=5000 CIV_NORMALIZED_PATHS=1 CIV_TRIGGER_TAIL=100000 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2500000 CIV_CANARY_DEPTH=5 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round25-full.log
```

§7's own finding says the normalized form resolves `result 0` + `inline u32 1`; the expectation
bugs that previously made normalized runs look like overwrites were fixed in §13.7. If the type
flips with the window already deterministic — that is `selectedMarker && hasWindow` and the gate
to `negative`/`positive`.

## 37. Round-25 (23:29): normalized form is a null result; windowOk=TRUE preserved

- Attempt 2: **ALL-SPAN + windowOk=True** with `CIV_NORMALIZED_PATHS=1` and the re-derived held
  gate (heldFreed 5376 ≥ CIV_HELD_MIN 5000) — preserved as
  `runs/MILESTONE-windowOk-normalized-r25a2.json`.
- **The normalized descriptor did NOT flip the trigger's type** (result 0x202, type 0 — identical
  to the short form). Combined with round-19's marker-offset invariance, the trigger's terminal
  component is insensitive to: marker position, descriptor form. The canary's own lookups keep
  returning result 0 / **type 1** under all of it.
- Attempt 3 (canary depth 3, the next queued variable) wedged at re-reserve — the payload's
  3-cycle lifetime; the depth test is still pending.
- Also eliminated this session: the trigger-descriptor-size theory (ns2jb's trigger is
  `pathPacket(7, ..., triggerInput, 0x800)` — byte-identical to ours at line 379; the fold is
  server-side, not descriptor-side).

### The surviving selectedMarker hypotheses (in queue order)

1. **Canary depth** (`CIV_CANARY_DEPTH=3`) — the heap layout the fold resolves against is the one
   thing not yet swept (5 has been constant since round-9).
2. **`CIV_CANARY_SIZE`** — the canary's file size (0x5a0 default) sets the extent-alias boundary
   in the heap; ns2jb's canary geometry differs.
3. **Close-phase fine-tuning** — `CIV_RELEASE_DELAY` between 1.5–2.5 ms so the close lands
   *inside* the trigger's 538–703 µs resolution window rather than before its start.

### Round-26 (queued — needs a game relaunch)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_HELD_MIN=5000 CIV_NORMALIZED_PATHS=1 CIV_TRIGGER_TAIL=100000 CIV_CANARY_DEPTH=3 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2500000 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round26-full.log
```

## 38. Review pass-4 (pre-round-25): three code-level fixes + the type-0 reframe; docs reordered

The fourth external pass verified the round-23/24 data to the microsecond (§34's arithmetic;
`heldFreed` 5344/5376 = 12160 − 6880 per-wave retention, exact to the byte) and found three
issues that would have made round 25 a wasted boot — all fixed:

1. **The retry loop broke on `windowOk` alone** — round-23's boot stopped after ONE attempt
   (r23a1 had windowOk=True but selectedMarker still open; ns2jb's retryWave requires both for
   extent-present). **Fixed**: the break now also requires `selectedMarker` for extent-present.
2. **`CIV_NORMALIZED_PATHS` + `CIV_CANARY_DEPTH` was a false-signal combination**: the depth
   branch won staging (canary resolves 0/type 1) while `canary_expected` still read 0x202 → every
   round-25 sample would have reported a phantom `canaryChanged`. Root cause found with it: the
   normalized canary returned 0x202 because **the 32-'A' parent chain was never created on disk**
   (ns2jb creates it and verifies GetEntryType 0/1, probe-reserved-gap.cjs:210-215);
   `CIV_CANARY_DEPTH` was invented as a workaround for exactly that. **Fixed**: the normalized
   branch now creates the full chain + file and verifies result 0 / type 1 live;
   `canary_expected` is 0 in every resolvable form.
3. **Guard/filler fidelity** (ns2jb:282-290): guards are ONE 0x300 normalized missing path plus
   the same path with the last char → 'C' (never created; 0x202 is the expected control); the
   filler's second descriptor is page_absent (0x7ef), not the filler's own path; and the FS
   exercises the declared length, so a ~26-byte string under a 0x301 descriptor is a different
   request. **Fixed**: guard0/guard1/page_absent now always staged ns2jb-faithful; the filler
   packet's descriptors are [marker16, page_absent].

**And the reframe that changes the interpretation of every type-0 sample** (Finding 3): fsp-srv
resolves the receive extent until the first NUL; pieces 0–3 end at existing directories, so
result 0 / type 0 = "terminated at a directory boundary before the marker" — NOT proof the fold
fired. The 0x202 samples under flush placement are the ones that prove the fold CROSSES into the
marker piece but terminates before the basename completes. Round-18's 0x2EEA02 was likewise
re-diagnosed: the native abort (guards finished before the trigger), not the reach. §29/§30 now
carry these corrections inline, and §13.5/§29/§30 are back in append order; the tracker's rounds
are chronological and its stale pre-round-3 blocks are marked superseded.

### Round-25 command (updated with the fidelity fixes — the queued experiment now tests the
### normalized canary as ns2jb actually stages it)

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_HELD_MIN=5000 CIV_NORMALIZED_PATHS=1 CIV_TRIGGER_TAIL=100000 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1500000 \
CIV_CANARY_DELAY=2500000 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round25b-full.log
```

(Drop `CIV_CANARY_DEPTH` — under the fixed staging the normalized canary IS the 0x5a0 path with
its chain created; the depth knob is only for the non-normalized form. Watch:
`canaryChainVerify` = result 0/type 1 in the setup output, the canary's expected-vs-actual
(changedPaths must stay empty), the trigger's result/type at all-span, and `heldFreed` (5344/5376
expected, ≥5000 passes).)

## 39. Round-25b (23:38): the normalized canary CANNOT be created on Civ VII — likely MaxPathLength

The pass-4-faithful staging ran; the chain creation **failed silently** (my filter swallowed
0x202s — for mkdir those mean "parent missing", not "already exists"; diagnostics fixed: every
chain step's result is now printed). The verify read result 0x202 — the 1440-char canary path
does not exist on the FS. The likely law: **`0x300` (768) is the FS MaxPathLength constant**;
Civ VII's fsp-srv refuses to create paths longer than that. ns2jb's SmileBASIC accepted the
1440-char canary; Civ's does not (or its creation path is otherwise capped — the per-step
diagnostics next boot will show exactly where it fails).

What the boot still delivered:

- **Attempt 2: ALL-SPAN + windowOk=True** (heldFreed 5344 ≥ 5000) — the **sixth** all-span
  sample, and the window is now deterministic across staging variants (short, depth, broken-
  normalized) — it depends only on the release/config timing, not the canary's form.
- **The missing canary STILL SPANS** (its 0x202 missing-path lookup deferred like the others) —
  notable: the held-path role doesn't need the path to resolve to participate in the deferral.
- The ns2jb-faithful guard pair (0x300 normalized missing + 'C' variant) behaved exactly as the
  reference predicts (0x202 control results, guards spanning 3/4 samples).

### Consequence for the selectedMarker hunt

The normalized-canary route is **closed on Civ VII** (pending the per-step diagnostics
confirmation). The canary must stay deep-form (CIV_CANARY_DEPTH — it resolves result 0 / type 1
reliably). The surviving hypotheses shrink to **close-phase fine-tuning**: `CIV_RELEASE_DELAY`
between 1.5–2.5 ms so CloseObject(outer) completes INSIDE the trigger's 538–906 µs resolution
(round-24 attempt 3 had it land inside and got 0x202; attempt 2 before the start got result 0 —
the phase response is live, sweep it). Command queued:

```
CIV_HARNESS_PORT=9029 CIV_RESERVE_TRACE=1 CIV_PIN_OFFSET=0 CIV_PRE_BRACKET=1 CIV_HELD_GATE=1 \
CIV_HELD_MIN=5000 CIV_TRIGGER_TAIL=100000 CIV_CANARY_DEPTH=5 \
CIV_PIN_EARLY=1 CIV_HOLE_EARLY=1 CIV_RELEASE_EARLY=1 CIV_PRE_CLONE=1 \
CIV_TRIGGER_SLEEP=2050000 CIV_RELEASE_DELAY=1900000 \
CIV_CANARY_DELAY=2500000 \
CIV_GUARD_DELAY=2550000 CIV_GUARD_SPREAD=0 \
CIV_FILLER_DELAYS=2480000,2300000 \
CIV_TAIL_DELAYS=2480000,2250000,2280000,2450000 \
  python host.py --host 192.168.103.215 worker-wave extent-present 3 \
  2>&1 | tee runs/round26-full.log
```

(RELEASE_DELAY 1.5 → 1.9 ms puts the close's completion ~600 µs later — inside a mid-class
trigger's resolution instead of before its start. Sweep 1.7/1.9/2.1 across boots if the first
 misses. The normalized staging stays ON for guards/fillers; only the canary returns to depth.)

## 40. Round-26 (23:42): close-phase swept; the question moves to the ALIAS OFFSET

`CIV_RELEASE_DELAY=1900000`, deep canary:

- Attempts 1–2: **ALL-SPAN + windowOk=True** (heldFreed 56544/5376), canary healthy (0/1). That's
  **eight all-span samples across the campaign** — the window is fully deterministic.
- The close-phase response is REAL but neither phase yields the marker: close before the
  trigger's start (round-24, 1.5 ms) → result **0/type 0**; close inside/after (1.9 ms) →
  result **0x202**. Both terminate before the basename. Attempt 3 (8/10) misses only
  filler1/guard0 — the usual lottery, secondary now.

### The alias-offset analysis (offline, from the accumulated data)

Working through the resolution mechanics: the request construction is byte-verified ns2jb-identical
(descriptor at the staged piece, ≤0x800, empty SFCI input; the reference's `pathPacket(7,
Buffer.alloc(0), triggerInput, 0x800)` matches ours exactly — confirmed against paths.cjs). The
seeds return canonical **0x320002 — ns2jb's own REQUIRED deferred-seed result** (line 476), so the
seed state is correct. The pieces join seamlessly in the staged payload ('/'-filled). Yet the
trigger's resolution terminates before SPAN_MARKER in every all-span sample.

The mechanism that remains: the pieces are copied into **fsp-srv's own heap** by the seeds'
processing, and the trigger's resolution flows through them only if fsp-srv's copies are ADJACENT.
Adjacency is governed by the reservation shape — and **Civ's reservation measures 12160 vs
ns2jb's 12272** (a 112-byte shift in the per-object geometry). A shifted alias means the
resolution crosses into fsp-srv's copies at the wrong offset, picking up a NUL/heap byte before
the marker's basename completes — exactly the observed 202-before-the-marker and 0-at-boundary
outcomes (the phase split follows which side of the misalignment the resolution stopped on).

ns2jb has machinery for exactly this: `cache-address-plan.cjs` / the `address-encoding-control`
profile compute and encode the alias offset. **The next work item is offline, not a knob: apply
the address-planner to Civ's measured geometry** (12160-byte reservation, Civ's 0x300
MaxPathLength) to derive the expected alias offset, then use `CIV_MARKER_OFFSET` (or an encoded
offset like ns2jb's) to place the marker where the shifted fold reaches it. See
[[address-planner]] and the `address-encoding-control` profile in the reference.

### Boot-27 config (unchanged harvest; planner work proceeds offline)

The frozen round-24/26 config remains the sample generator. No new knob is queued until the
planner analysis lands — the last three knob sweeps (marker offset, descriptor form, close phase)
all came back null, which is itself the evidence that the remaining variable is the heap-alias
geometry, not the wave's knobs.

## 41. The native cache-read stage is PORTED and LIVE — the alias is now directly measurable

The planner dig established what the reference's address machinery actually needs:

- **`signed-cache-plan.cjs` is pure arithmetic** (portable as-is): given a *measured target
  address* + storage size + cycle, it emits a 40-byte UTF-8 "signed one-shot" header. No game
  dependency except storage ≥ 0x4000 and one of the two fsp-srv bucket cycles (0x28000/0x500000
  — to be verified on Civ).
- The measured target comes from **`native-links.cjs`**: scan saved 0x4000-block LEAK READS for
  aligned 39-bit pointer candidates (zeroTail/changedLink strength heuristics).
- The leak reads go **through the reserved gap's own content storage** (`IStorage::Read`,
  **command 0** — not 806; that was this session's bug: 806 echoed the request back).

**Implemented on Civ VII** (`leak_read` in host.py + `leak-read` CLI): reserve → open the
content storage on the reserved root (command 200, as the wave does) → GetSize (4.975 GB) → read
0x4000-aligned blocks (six blocks captured, hashed) → inline findNativeLinks scan. The read
stage WORKS — Civ VII's first native cache reads.

**The decisive test is queued** (`harness/leak-cycle.py`, one process, one setup — the double-
setup issue this session hit is why it must be one process): wave (2 attempts) → leak-read →
grep the dumps for the wave's OWN strings (`CIV_GAP`, `/S0//`, `SPAN_MARKER`, the 32-'A' runs).
**If the strings appear, the reserved storage aliases the wave's heap — the pieces' physical
layout, their NUL seams and adjacency, become directly readable**, and the alias-offset question
(12160 vs 12272) stops being inference: the planner gets its measured target from the pointer
candidates, or the marker gets placed at the observed seam offset.

If no strings appear and no candidates change across waves: the gap storage does NOT alias the
wave's heap on Civ VII (different aliasing law), and the address-planner route needs the
lower-window machinery (`retained/lower-window.cjs`) instead — the next offline dig.

## 42. Round-28 (00:00): leak-cycle EXECUTED — clean; the gap storage does NOT mirror the wave's heap

The decisive test ran end-to-end in one process:

- Wave 1: **AP=True, windowOk=True** (all-span, the frozen config — consistent with rounds
  24–26). Wave 2: AP=False (lottery).
- **Post-wave leak-reads (8 blocks each, both waves): ZERO wave strings (`CIV_GAP`, `/S0//`,
  `SPAN_MARKER`, the A-runs) and ZERO aligned pointer candidates — identical across the two
  waves and identical hashes to the pre-wave control reads.**

**Verdict: the reserved gap's content storage does NOT mirror the wave's heap on Civ VII.** The
aliasing law differs from ns2jb's (there, the storage's reads DID expose the shaped heap — the
whole cache-alias chain depends on it). Either the alias forms only under the wave's own
processing (the wave's requests never read the storage — only our leak-reads do), or Civ's
fsp-srv caches reads differently (the `clean`-alias machinery ns2jb primes with
`memset(poison)` + requalification).

The discriminating next measurement (cheap, same payload pattern): **read the storage DURING
the wave's busy window** — the alias may only populate while fsp-srv is actively serving the
wave's requests (its copies live in a cache that eviction/normalization refreshes on request
boundaries). Add to leak-cycle.py: a third leak-read issued ~200 µs after the trigger's start
(the hole's 60 ms busy window makes this trivially schedulable from a native worker — or simpler:
issue the leak-read from the HOST between the seeds' send and the wave's poll, racing the busy
window from outside).

If the during-wave read is still clean, the alias chain needs ns2jb's full priming sequence
(`fs-memory-control.cjs`'s memset-poison + ordinary-read requalification + signed-header install)
ported BEFORE the read can see the heap — that is the next offline dig, and it is a known,
bounded port (every primitive in it already exists on Civ: memset, IPC with receive descriptors,
the wave's own transport).

## 43. §42's verdict is WITHDRAWN — pass-5 review corrects the leak machinery itself

Pass-5 reviewed the leak code (not the docs) and found the test never measured what it claimed:

1. **`leak_read` was saving 224 bytes of TLS IPC residue, not the DMA.** IStorage::Read's 0x4000
   lands in the Type-B receive descriptor's console buffer (rx_page); the inline reply is only
   TLS residue. The proof is in the artifacts themselves: every `leak-*.bin` was exactly 224
   bytes, and bytes 16..32 of block01 carried MY OWN outbound `u64(0x4000)` argument — the
   SHA-differences between blocks were our request offsets echoing back. **Fixed**: one reusable
   rx_page (the per-block alloc leaked 16 KB/block), poisoned with 0xa5 before each IPC, and the
   buffer is read back via the console's `read` command after the IPC returns — with an
   all-poison detector so a failed DMA can't masquerade as data again.
2. **The scan was wrong too** (every 8-byte word instead of ns2jb's 0x4000-block-heads with
   zeroTail/zeroAfter32 qualification) — fixed to findNativeLinks-faithful. Storage now closes
   with the CMIF Close (control 2), not raw svcCloseHandle.
3. **And the conceptual model was wrong** (the important one): a command-200 handle is
   **OpenDataStorageByCurrentProcess = Civ VII's RomFS** (4.98 GB — that's what GetSize returned);
   reading it just reads clean RomFS through fsp-srv's cache. It does NOT read the heap. ns2jb's
   heap leak requires the full chain: **positive wave** (canary slot freed via /..\) →
   **candidateHandle prime** (command 200's 0x5a0 cache bucket table allocated INSIDE the freed
   canary slot) → **copyField waves** (overwrite the cache entry's offset+range) → **over-read**
   (0x1c000 served from one 0x4000 buffer — bytes past 0x4000 are adjacent heap) → neighbor
   cache invalidation (freelist pointers into block heads). Plain extent-present + read = clean
   RomFS forever, which is exactly what round-28 measured. **§42's "does not mirror" conclusion
   is retracted** — the test never primed the alias.

`leak-cycle.py` is rewritten (v2): romfs-control stage only by default; the full alias chain is
stage-gated behind the wave prerequisites (canary chain staging, release-before-trigger), with
the stage order documented in its docstring. The leak machinery is now *correct*; the port of
fs-memory-control.cjs's priming/over-read sequence is the next offline work item, after the wave
prerequisites (pass-5's list) are verified on the next boots.

Console hygiene note: the earlier CLI-session setup + leak_read's internal setup + the wave's
internal setup = three setups per payload; the wave aborted at the third's clone step and the
console wedged (open-but-silent). **One setup per payload** is the rule leak-cycle.py encodes.

## 44. The fs-memory-control port is IN (offline-verified) — pass-6 review fixes landed

§43's "next offline work item" is done. The controller is ported and wired end-to-end; every
step is verified offline (no console since the pass-5 leak fixes), and the pass-6 review that
followed was applied. The action plan lives in [[fs-memory-control-plan]]; this is the state.

### Ported

- **`harness/signed_cache_plan.py`** — verbatim port of `signed-cache-plan.cjs` (40-byte UTF-8
  signed-one-shot header, memoized backtracking search, 5 s deadline, forbidden-byte machinery).
  Cross-checked byte-for-byte against the .cjs via node across 6 (target, storage, cycle)
  triples + 3 bad-arg cases (`signed_cache_plan_selftest.py`).
- **`harness/fs_memory_control.py`** — the controller core: `select` (bias arithmetic, aligned
  bucket hit test, retarget branch), `read_block` (reusable poisoned rx region, IPC Read cmd 0,
  chunked read-back, all-poison detector, failed-read artifact), `read` (cycle-bound step +
  overlap-window prefix slicing), `write_at`, `qualify_signed_header` (control-handle diff,
  0xa5/0x5a double-poison verify, `SIGNED-RW` marker write + restore), `qualify_single_bucket`
  (0x28000→0x4000 demotion), and the `dispatch` surface. 10/10 offline selftest
  (`fs_memory_control_selftest.py`, fake console + fake DMA).
- **`harness/leak-cycle.py`** v3 — stage `memctl` is reachable (the stage guard is
  `if stage not in ("full", "memctl")`), instantiates `FsMemoryControl(h.console, m, ...)` (the
  object with `.request()`, not the Harness), and OPENS the candidate handle itself: command 200
  on the cached root → GetSize → the 16-byte priming read (alias-chain stage B) → candidateHandle
  stored in `h.state`. Initial plan = `signed_cache_plan.plan(0x123456789, storage_size,
  0x500000)` (test target; a real run derives it from the leak-read pointer candidates).
- **`harness/host.py`** — `worker_wave(profile, tries, payload, read_size)` payload pass-through
  and hole read-size override; `_stage_extent_ns2jb` custom-payload path (ns2jb's `/..\\` prefix,
  no embedded NUL, five 0x800 pieces); `Harness.copy_field_wave` (the copyField port).

### Pass-6 review (this session) — all items fixed

1. **`memctl` unreachable / wrong ctor** — the deeper root cause (found on the first live run)
   was that `leak-cycle.py` clobbered `sys.argv = ["host.py"]` BEFORE reading `sys.argv[1]`, so
   `stage` was always `"romfs"` and no alias stage could ever run — the line-68 guard was
   necessary but not sufficient. Fixed by parsing `stage` before the clobber. `h.console` (not
   `h`) and a real candidate handle (command 200 on the cached root → GetSize → 16-byte prime
   read) are also in place.
2. **`copy_field_wave`:** (a) now calls `self.rereserve()` + frees the previous wave's
   `wave_allocs` before every field-copy wave (each wave's `release` role closes `outer`, and
   `worker_wave(tries=1)` never re-reserves); the `tries>1` recursion now forwards
   `payload`/`read_size`; (b) the inner attempt loop breaks as soon as
   `confirmed == required_copies`; (c) the acceptance threshold follows the reported metric —
   `CIV_HELD_MIN` under `CIV_HELD_GATE=1`, else ns2jb's `0x2810`.
3. **console 0x8000 JSON-frame cap** — `host.write_chunked` (0x1000 bytes/call) is used for the
   `leak_read` poison fill, and `fs_memory_control._write` chunks the poison fills and `write_at`
   input. `leak_read` now re-poisons `rx_page` before EVERY block's IPC, not once outside the loop.
4. **Normalized canary chain** — rooted at `self.scratch` (`walked = "/" + parts[0]`), so it
   creates `/CIV_GAP_<ts>/canary/AAAA...` instead of `/canary/AAAA...`.
5. **Release-before-trigger** — new opt-in `CIV_RELEASE_CLOSE_GATE`: the release role signals a
   `closed` gate after the `close_object(outer)` send, and the trigger waits on it (when set), so
   `release.endUs < trigger.startUs` no longer depends on the early `released` handshake.
   `leak-cycle.py` sets it for its prerequisite wave. **CAVEAT (needs a boot):** under the frozen
   harvest config (`CIV_RELEASE_EARLY=1`) the close lands ~4.4 ms while the protected windows end
   ~3.7-4.2 ms, so `windowOk` (which requires `allPending`) may not hold at the same time as the
   close gate. P3 must confirm the geometry, or the memctl wave needs `CIV_RELEASE_EARLY` off so
   the guards/trigger order themselves after the close.

### P3 first live attempts (this session) — crash root-caused to the close gate

`leak-cycle.py memctl` against live payloads at `192.168.103.215`; three fresh boots, three
different failure surface, one root cause found and fixed.

- **Run 1 (pre-fix):** took the romfs branch (the `sys.argv` clobber) and spent one setup; the 4
  romfs control reads were clean — the §43 regression check holds across the chunked `write`
  change.
- **Run 2 (post-fix, boot 1):** reached the wave, launched all 13 frames, then the control socket
  closed mid-`_poll_results` — the payload/game died (all of :9026/:9028/:9029 gone; the Switch
  still pinged).
- **Run 3 (boot 2, with a diagnostic baseline):** crashed at the **same** point, but on the
  *baseline* variant — the proven wave with `CIV_RELEASE_CLOSE_GATE` **off**, and only one setup
  in the process. That exonerated both the duplicate setup and the close-gate wait.

**Root cause (code bug, now fixed):** the new `closed` event was placed at `ctrl+0x1c`. The SDK
`initialize`/`clear` event struct is 8 bytes (`clear` does `add x8,x0,#4`), and the existing gates
are 8-byte spaced (0, 8, 0x10, 0x18) — so `closed` overlapped `guard` (0x18–0x1f) and `CF`
(0x20), and the per-wave `initialize`/`clear` loop corrupted the guard event and the completed
flag before the first worker ran. Moved to `ctrl+0x40` (next free 8-byte slot, past the
0x20–0x3f C..O struct); the ctrl page is zeroed to 0x80. This fired regardless of
`CIV_RELEASE_CLOSE_GATE`, which is why the baseline crashed too.

Next boot: run `leak-cycle.py memctl` once. The baseline variant should now complete; then the
close-gated variants answer the original windowOk-plus-close_gap question.

### Pass-7 review fixes + the live geometry result

Pass-7 (external) reviewed the P3 code and found four real issues; all fixed:

1. **Gate overlap (confirmed independently here).** The new `closed` event at `ctrl+0x1c`
   overlapped `guard` (0x18) and `CF` (0x20): SDK events are 8 bytes, so `initialize`/`clear`
   clobbered the guard state word and the completed flag every wave. Moved to `ctrl+0x40`,
   ctrl zeroed to 0x80. This was already the fixed cause of the earlier crashes.
2. **`PREREQ_VARIANTS` env overrides were no-ops for import-time constants.** `RELEASE_DELAY`,
   `TRIGGER_SLEEP`, `CANARY_DELAY`, `GUARD_DELAY`, `GUARD_SPREAD`, `FILLER_DELAYS` were read at
   host.py import; the sweep mutated `os.environ` after import. Fixed: `worker_wave` now reads all
   of them at wave time.
3. **Throwaway baseline burned a wave cycle.** Made opt-in (`CIV_PREREQ_BASELINE=1`). Also
   `stage_extent` (3 pages) and `_stage_extent_ns2jb` (9 pages) allocations were never added to
   `wave_allocs`, so `freeAddresses` could not reclaim them across waves — now recorded.
4. **Wiring gaps.** `stage full` was a silent no-op — now exits with a clear message. The
   `memctl` comments now state it is a clean-RomFS smoke test, not the alias chain.
   `FsMemoryControl._ipc` fell back to the raw `result` instead of the CMIF `canonical` — fixed to
   `reply.get("canonical", reply.get("result"))`.

**Live geometry result (fresh boot, one setup, 4 variants):** with the close gate ON, `close_gap`
is now negative (`-107`, `-2661`, `-2659` µs) — but `allPending` goes False in every close-gated
variant, while the gate-OFF baseline had `allPending=True` with `close_gap=+6906` µs. So the two
prerequisites **do not co-hold with the current wave structure**: the trigger waits for the close,
by which time the protected windows (canary/tails/fillers) have already ended. This is a geometry
problem, not a code problem. Host.py now saves `runs/prereq-variant-N.json` per variant so the
next sweep can be diagnosed properly. Fixing it likely needs the protected roles sent later (so
their windows span the post-close trigger) or the close made earlier (fewer/deferred counter
reads) — a console-tuning task.

**Second geometry run (3 samples, per-variant reports):** early close works — `RELEASE_DELAY=600000`
+ `CIV_PRE_BRACKET` off moved `release.end` from 8874 → 4002 µs and the trigger to 4109 µs — but
the miss is END-side: canary 3695, filler0 3625, filler1 3801, guards 3524/3339 all end before the
trigger. `PROTECT_SHIFT` shifts commit/tails/canary/fillers later but explicitly EXCLUDES the
guards, so `CIV_GUARD_DELAY` must rise with it. Next 3 samples: `{RELEASE_DELAY=600000, no
PRE_BRACKET, GUARD_DELAY=4000000, PROTECT_SHIFT=1200000}` first, then the same with
`RELEASE_DELAY=300000`, then the ambient control.

**Pass-8 review corrections.** (1) The `release.endUs` drop is partly a **cold-vs-warm hole**
effect, not just `RELEASE_DELAY`: wave 1's hole read is cold (`hole.endUs` ~60 ms,
`release.endUs` ~8.5 ms) while wave 2+ reads the 1.5 MB range cached (`hole` ~39 ms,
`release.endUs` ~3.5-4.0 ms). So variant 0 (wave 1) is a warm-up and variant 1 (wave 2) is the
expected hitter. (2) The drain-order invariant explains why `release` drains 200-700 µs after the
saved clones even when its CloseObject is sent >1 ms earlier (`clients[9]`, the owner, is created
after the saved clones and drains after them in the same batch). (3) **Fixed a real
`timing_valid` bug:** `origin` was `roles[0]` (commit)'s start tick, but `CIV_PROTECT_SHIFT`
moves commit later than the hole/release, making their `startUs` negative and forcing
`timing_valid=False`/`allPending=False` even when every role spanned the trigger. `origin` is now
the minimum start tick across all roles (identical to commit-first when unshifted).

**Third geometry run (3 samples).** Confirmed the timing fix (`timingValid=True` for all) and
isolated the real control: the trigger's START is set by **guard1's `guard` signal**
(`wait(released) + GUARD_DELAY`) plus the trigger tail — not by the close, unless the close lands
later. The previous sweep's `GUARD_DELAY=4000000` + `PROTECT_SHIFT=1200000` moved the trigger to
~3.8 ms, past the protected windows, guaranteeing an END-side miss. Corrected direction: keep the
harvest protected/guard timing and only make the close early (`RELEASE_DELAY=300000`,
`CIV_PRE_BRACKET` off → `release.end` ~1.0 ms on the warm wave), so the trigger stays in the
proven ~2.4 ms window with `release.end` far before it. Next sweep: ambient warm-up (wave 1),
`300000`+no-PRE_BRACKET (wave 2, likely), `600000`+no-PRE_BRACKET (wave 3).

**Fourth geometry run — ★ PREREQUISITE PASSED + CONTROLLER READ LIVE.** Pass-9 review supplied
the missing piece: with commit at t=0 the release's `CloseObject(outer)` queues behind it and
defers to 3.5-4.5 ms; delaying commit past the close but before the trigger keeps the close at
~1.0 ms. Added **`CIV_COMMIT_DELAY`**. Winning sample (wave 2):

```
CIV_RELEASE_DELAY=300000  CIV_PRE_BRACKET off  CIV_COMMIT_DELAY=1800000
  release 311 -> 935 us (close, fast)
  commit  1697 -> ...          (delayed past the close, defers with the harvest batch)
  trigger 2367 -> 4076 us      (harvest window)
  allPending True, windowOk True, closeEnd-vs-triggerStart = -1432 us
```

With the prerequisite satisfied in the same process, `memctl` ran: candidate handle opened
(command 200 → GetSize → 16-byte prime, result 0), `FsMemoryControl.dispatch(info)` returned the
plan, and the smoke `read` of 0x1000 through `select`/`readBlock` returned **4096 bytes of real
RomFS** (248 distinct byte values, not poison) written to `runs/memctl-memory-1.bin`. This is the
first live exercise of the controller read path. `selectedMarker` is still False (separate hunt),
so this validates the controller, not the alias chain.

### The A-D alias chain is implemented — and hits the `selectedMarker` wall

Ported `cache_address_plan.nul_writes` (byte-for-byte against `cache-address-plan.cjs`; the
combined prefix yields exactly 7 copies) and added an `alias`/`full` stage to `leak-cycle.py`:
A positive wave → B reserve + open candidate + prime → C `installCombinedLeak` (7 copyField
writes) → D over-read. `CIV_ALIAS_UPTO` stops after each sub-stage so a boot validates one step.

Live run (this session): the prerequisite passed on variant 2 (`COMMIT_DELAY=2000000`), then
**Stage A failed**: the positive wave qualified (`allPending=True`, `windowOk=True`) but
`canaryChanged=False`, `canaryType=1`, `changedPaths=[]`, and the trigger returned **`0x2EEA02`**
(the normalizer abort) with `nativeGuard` all 0. That is not the native guard abort — it is
fsp-srv's underflow response. This is exactly the campaign's unresolved
`selectedMarker`/`markerReproduced` blocker (rounds 16-26): without the trigger resolving to the
marker, the canary slot is never freed and Stage B can never reuse it.

Handoff §30 (round-19) recorded that `CIV_MARKER_OFFSET=0x2140` let the trigger **resolve
(result 0)** for *extent-present*, whereas the default `0x2340` aborts. The `positive` branch now
honours the knob — but the live marker sweep (this session, `0x2140` then `0x1b40`, one boot)
showed the positive trigger still returns **`0x2EEA02`** and `canaryChanged=False` for both. Re-reading
§30 explains why: at `0x2140` only the *extent-present* trigger resolved, and it resolved to
**type 0 (a directory)** — `selectedMarker` needs type 1, and §30's own conclusion is that the fold
carries a directory regardless of marker placement. So the marker knob cannot fix Stage A.
The §30/§25 lead is the **normalized one-descriptor form** (`CIV_NORMALIZED_PATHS=1`), which
resolves `result 0 + inline u32 1`; round-25b found the normalized canary cannot be created on
Civ VII (likely MaxPathLength). Stage A therefore needs a new idea, not another marker offset.
The alias scaffolding (nul_writes + A/B/C/D, `CIV_ALIAS_UPTO`) is ready to run the moment the
positive overwrite lands.

**Payload-lifetime lesson (validated the reviewer's point 3):** a *second* `leak-cycle.py` run on
the same payload (a second setup + wave) wedged the console at the wave's storage open
(open-but-silent), exactly as §43 warned. **One `leak-cycle.py` run per payload; relaunch the game
for each sweep.** To de-risk the controller work independently of the wave, `CIV_PREREQ_SKIP=1`
skips the wave prerequisites and runs the controller against clean RomFS (setup + leak_read +
select/read only).

### Not yet done (P3, continued)

The copyField wave path (custom payload) and the controller read/write still have not completed
against the console. Open items from the plan still stand: Civ's bucketPeriod (0x28000 vs
0x500000 — default 0x500000), the ordinary-content control handle for `qualifySignedHeader`, and
bulk reads > 0x8000. Keep **one setup per payload**; `memctl` reuses the romfs stage's setup.
## 46. Round-33 (2026-11-05): external vault review — sign fix + Stage-A probes

1. **The gated-canary endgame ([[gated-canary-plan]] boots 10–12) had `allPending`'s
   signs inverted.** The gate (`host.py`) needs **start delta < 0 AND end delta > 0**;
   boots 10/11 measured start +55/+52 (fail) with end +709/+84 (pass), yet the queued
   levers attacked the passing end side (size 0x400 thinned it to +32; boot 12's TAIL
   raise was predicted to flip it to −68 and "close the window"). Origin: the boot-8
   shift model in [[selected-marker-plan]] ("need start delta > 0 AND end delta < 0"),
   corrected inline. **Corrected boot 12:** `CIV_TRIGGER_TAIL=200000` + size **back to
   0x5a0** + offset 150000 → start ≈ −48, end ≈ +606 → the normalized canary spans for
   the first time. Non-deferred-canary regimes (boots 5–8) remain unwinnable at any
   offset — lottery losses. Full working in the plan's correction block.
2. **Stage A is form-blocked, not marker-blocked.** Round-32's positive trigger
   `0x2EEA02` with `nativeGuard=0` is the normalizer rejecting the `/\..\` prefix piece
   itself; the marker-offset sweeps (0x2140/0x1b40) cannot touch it (and the `host.py`
   staging comment citing round-19's "0x2140 resolves" conflates the extent-present
   profile — type 0 — with positive). SM5 queued: sequential positive-form probe +
   `/../` variant, no wave budget.
3. **SM6 queued: downward marker sweep** (`CIV_MARKER_OFFSET=0x240/0x640/0xa40/
   0x1400`) — every swept offset so far sits in piece 4, and the direct form resolves
   1440-char paths, so any fold-reach cap is measurable directly. This is the missing
   input to the §40/SM3 alias-offset analysis.
4. **Stale verdicts superseded:** §39/§44's "normalized canary uncreatable on Civ VII
   (likely MaxPathLength)" is refuted live (SM0 boot 2: the 1440-char chain creates and
   verifies; the real cause was the one-byte staging descriptor bug, since fixed in
   `host.py`). §45's "Stage A needs a new idea, not another marker offset" is superseded
   by SM0's descriptor fix + SM5's form probe. Docs: index links added (gated-canary
   plan, save-I/O note, Questions folder); date typos fixed (2025/2024 → 2026).