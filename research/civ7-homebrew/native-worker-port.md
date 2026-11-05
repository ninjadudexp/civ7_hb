---
tags: [civ7, switch2, homebrew, workers, m2, m3]
status: solved
---

# Native worker port

What remains before the P7 qualification waves can run safely, derived from ns2jb `app/native-worker.cjs`, `app/worker.cjs`, `shared/frames.cjs`, `thread-code.json`, and `timing-code.json`.

> **Status (2026-11-03): solved.** Civ VII native workers run. `worker-direct` and
> `worker-direct 8` create, start, and complete frames of 1 and 8 calls, all result slots
> written, payload alive. The whole blocker was one instruction — see "Cause found" below.
> The game's object-based `create` is not needed; the launcher is three raw SVC calls.

## Why it is required

The first `abort-control` run aborted the game (hard reboot). ns2jb interleaves the wave from **13 native worker threads pinned across cores** with native sleeps; the current harness posts everything from the single JS thread with JS `sleep`. Without the workers the receive-order geometry is wrong, and the abort profiles have no native guard to trip — so a qualification violation aborts the FS process instead of aborting safely.

## Worker mechanics (ns2jb)

- `frames.cjs build(base, sdk, calls, code)` lays out a register-context frame at `base`: a dispatch table per call (`x21`), a stack slice per call (`STEP = 0x140`), result slots at `RESULT = 0x800`, and `SENTINEL = 0xfedcba9876543210`.
- Each call frame sets `x30` to `sdk + code.call4.offset`, stores args, then `saveResult64` writes the result and `loadTable` dispatches the next call; the last call `exit`s.
- Launch: `call4(create, slot, 0, sdk + longjmp, base)` — `create` is `pthread_create(thread*, attr, start, arg)` with `start = longjmp` (the trampoline) and `arg = base` (the frame).
- Wait/join: `tryWait(thread, 0, 0)` (0/1), then `join(thread, slot+8, 0)`.
- Dynamic arguments (the `{u32: address}` kind) are loaded with `memcpy(dest, src, 4)` calls, where `memcpy` is `sdk + 0x586fb0` in ns2jb. The same `memcpy` is the **abort guard**: `memcpy(triggerInput, zeroByte, decision)` replaces the trigger with a zero-length copy when a qualification invariant is violated.

### Required SDK functions

`create`, `join`, `exit`, `longjmp`, `loadTable`, `call4`, `saveResult64`, `tryWait`, `tryWaitImplementation`, `sleep`, `memcpy`.

## Relocation status (2026-11-03)

Found locally:

| Function | sdk offset | note |
| --- | --- | --- |
| `current` | `+0x137050` | exact |
| `tick` | `+0x137240` | exact |
| `frequency` | `+0x137270` | exact |
| `priority` | `+0x1370c0` | exact |
| `loadTable` | `+0x4e094` | exact |
| `saveResult` | `+0x28c980` | exact |
| `saveResult64` | `+0x19bcc0` | exact |
| `initialize`/`finalize`/`clear`/`tryWait` | `+0x1324e0`/`+0x132510`/`+0x132648`/`+0x1327e8` | event cluster |

Now also verified against the dump (see `signatures/sdk-resolved.json`):

- **`longjmp` = `+0x5511b4`** — prologue is the `ldp x19..x29` frame restore (`ldp x19,x20,[x0]` … `ldr x2,[x0,#0x68]`), matching ns2jb's body for the first 48/64 bytes. This is the worker trampoline.
- **`call4` = `+0x359b88`** — `mov x0..x3` from `x25/x21/x20/x19`, then `ldr x8,[x8]; blr x8`. Matches ns2jb's structure.
- **TLS current-thread slot = `[tpidr_el0, #0x1f8]`.** `current` = `+0x137050` (`mrs; ldr x0,[x8,#0x1f8]; ret`); `setCurrentThread` = `+0x1380d8` (`mrs; str x0,[x8,#0x1f8]; bl +0x5EA880`). The thread module is `+0x132xxx`–`+0x138xxx` (also holds the event cluster).
- **`memcpy` = `sdk_base + 0x5224E0`** (lead): `core.js` derives `sdk_base = read64(main_base + 0x4DC6088) - 0x5224E0`, i.e. that GOT value is the game's `memcpy`. It is both the frame's dynamic-arg loader and the abort guard.

Thread-implementation leads, found by tracing the SVC stubs (`bl` to `sdk+0x168b2c` createThread, `+0x168b4c` exitThread, `+0x168b54` sleepThread):

- **`create` = `+0x13DCC4` (lead)** — `sub sp, #0x50` prologue, four arguments, calls the `svcCreateThread` stub at `+0x13DD14`.
- **`exit` = `+0x13E0A8` (lead)** — calls the `svcExitThread` stub.
- **`sleep` = `+0x13E270` (lead)** — tail-branches to the `svcSleepThread` stub.

Still open: `join`, `tryWaitImplementation`, `memset`, `free`, `allocate`, `hash`, `sdk-proxy`. The ns2jb relative offsets do **not** transfer: predicted `create`/`join`/`exit` (`current + 0x9490/0x98d0/0x9760`) land in unrelated code. The timing sub-cluster (`current`/`tick`/`frequency`/`priority`) does preserve deltas, but the wider thread cluster does not.

### Tried `create` on hardware: faulted — and why

A minimal bring-up (`create(slot, 0, sdk+longjmp, base)` with a 1-call `tick` frame written at `base`) **faulted the process**. The frame write, the write-verify, and the sparse page writes all succeeded first, so the fault is in `create`/the thread entry, not the frame transport.

Decoding Civ VII's `longjmp` (`+0x5511b4`, byte-identical to ns2jb) gives its tail:

```
ldr x2, [x0, #0x68]   ; sp
mov sp, x2
<restore d8-d15>
mov x0, x1
cbnz x1, +8
movz x0, #1
br  x30
```

The final word is `0xd61f03c0` = **`br x30`** (not `br x1`; an earlier reading of this note had it backwards). `longjmp` restores `x19-x30` and `sp` from the frame at `x0` and **returns to the restored `x30`**. In `frames.cjs` that slot is `sdk + code.loadTable.offset`, so the frame itself carries the worker entry; `x1` only becomes the resumed `x0` (`x1 != 0 ? x1 : 1`), exactly like a setjmp return value.

So the launcher does **not** need to supply the entry in `x1`, and the earlier "longjmp branches to x1" conclusion — on which the fault was blamed — was a misdecode. The fault must instead come from `create`:

- `+0x13DCC4` has a `sub sp, #0x50` prologue, four arguments, and calls the `svcCreateThread` stub at `+0x13DD14`, but ns2jb's own `create` does **not** call the create-thread SVC directly — it calls helpers in `0x5c0xxx`/`0x1343xx`. So `+0x13DCC4` is a lower-level helper, not ns2jb's `create`.
- `longjmp` is the thread *start routine*: `create(slot, 0, longjmp, base)` must end up invoking `longjmp(base)` on the new thread with `x21`/`sp` taken from the frame. If `create` passes the wrong `x0` (or starts a fixed-entry pool thread), the restore reads the wrong memory.

**Better route: skip `create` entirely.** The kernel only needs six registers; the SVC stub at `+0x168b2c` is `str x0,[sp,#-16]!; svc #0x08; ldr x2,[sp]; str w1,[x2]; add sp,#16; ret`, so the ABI is exactly libnx's:

```
svcCreateThread(Handle* out, void* entry, void* arg, void* stack_top, int prio, int cpuid)   // svc #0x08
```

and the handle lands in `*out`. The cluster wrapper's own argument build confirms this order
(`x0` = a stack slot, `x1` = entry, `x2` = arg, `x3` = stack top, `x4` = prio, `x5` = cpuid).
No SDK function calls `svcStartThread` (`+0x168b44`) at all, and a raw `svc #0x09` appears nowhere
in the dump, so `CreateThread` starts the thread itself — no separate start step.

So the launcher is just `call("createThread", [out, sdk+longjmp, base, base+frameBytes, prio, 0])`,
with no game object and no TLS setup. Whether the frame chain needs the SDK's TLS current-thread
(which the game's `create` would have installed via `setCurrentThread`) is the open risk — it will
show up as `createResult != 0` or a result slot left at `SENTINEL`.

`host.py worker-direct` implements this. Results (2026-11-03):

1. Probe page, 23-page frame allocation, and sparse page writes with read-back all succeeded.
2. A diagnostic `call` to `sdk+priority` (`+0x1370c0`) with no argument crashed the game — that
   function takes a thread. Don't call SDK functions whose signature is unverified.
3. `createThread(out, longjmp, base, base+bytes, 0x2C, 0)` returned **`result 0`** and a valid
   handle (`0x3e81a8`). The payload survived, but the frame's result slot stayed at
   `SENTINEL` — so `CreateThread` alone leaves the thread created-but-not-running.
4. Adding `startThread(handle)` (svc #0x09, `+0x168b44`) then killed the process. So the thread
   **does** start on `StartThread` — and then faults inside the frame chain.

**Diagnosis.** The frame transport and the SVC calls are correct; the fault is environmental.
Offline decoding rules out the frame code itself: `saveResult64`/`saveResult` are
`str x0,[x19]; ldr x19,[sp,#0x10]; ldp x29,x30,[sp],#0x20; ret` (no TLS), `call4` is ns2jb's
`mov x0..x3 from x25/x21/x20/x19; ldr x8,[x8,#0x68]; blr x8` epilogue, and `loadTable` is the same
24-byte dispatch. The one thing the game's `create` does that a raw SVC does not is the thread
environment: `+0x13E044` stores the thread object into the TLS current-thread slot
(`str x0,[tpidr_el0,#0x1f8]`) before calling the body. Without it, any SDK helper in the chain that
reads the current thread faults.

**Cause found (2026-11-03): `call4`'s function offset differs.** Civ VII's `call4` is
`ldr x8,[x8,#0x70]` where ns2jb's is `ldr x8,[x8,#0x68]`; `loadTable` and the other 55 bytes are
byte-identical. Since `x8 = *(x21) = base+table+0x10`, the callee must live at `table+0x80`, not
ns2jb's `table+0x78` — so every worker frame jumped to zero. `frames.py` now uses
`CALL4_FN_OFFSET = 0x70` and writes the callee at `table + 0x10 + CALL4_FN_OFFSET`. The raw-SVC
route is otherwise complete: `worker-minimal` (thread runs) and `worker-marker` (`longjmp` +
frame restore) both passed live, so the game's `create` is not needed.

**Remaining options if the fixed chain still faults:**

1. TLS: if a chain helper reads the current-thread slot, point the thread through the game's
   trampoline (`+0x13E044`), which does `str x0,[tpidr_el0,#0x1f8]` at entry. It needs
   `[frame+0x50] + [frame+0x58] - 0x20` to resolve to `longjmp`, which collides with the `x29`/`x30`
   slots `longjmp` reads, so it is fiddly — but the raw-SVC frame is otherwise proven.
2. Find the game's object-filling `create` (callers are indirect, so match its 596-byte body
   against ns2jb's, which calls helpers at `+0x5c0xxx`/`+0x1343xx`).

**Still worth doing offline:** identify Civ VII's real `create` (the object-filling one) in case
the direct route turns out to need TLS — but it is no longer on the critical path.

### Cluster creators and the trampoline (2026-11-03)

Following `svcCreateThread` (`+0x168b2c`) to its `bl` sites lands in a tight cluster
(`+0x13DD14`, `+0x13DD60`, `+0x13DDA0`, … `+0x13DF60`) of ~0x40-byte wrappers. Decoding the
lead shows why they are unusable as `create`:

```
sub  sp, sp, #0x50
…
add  x0, x29, #0x1c        ; out handle (on stack)
str  wzr, [x29, #0x1c]
ldrsh w8, [x1, #0x44]      ; priority from the attribute object in x1
adr  x1, +0x13E044         ; FIXED entry pointer
mov  x2, x19               ; arg
mov  x3, x21               ; stack top
add  x4, x8, #0x1c         ; priority
mov  x5, x20               ; cpuid
bl   svcCreateThread
```

`x1` is an `adr`, i.e. every cluster wrapper has a **hard-wired entry**, and `x0` is a
stack slot, not the caller's thread handle. These are the game's internal pool-thread
creators, not a general `create`.

The fixed entry `+0x13E044` is the trampoline. It stores `x0` into the TLS current-thread
slot (`[tpidr_el0, #0x1f8]`), runs a global hook if set, then calls the routine recorded in
the thread object:

```
ldp x8, x9, [x19, #0x50]
add x8, x9, x8
ldr x8, [x8, #-0x20]
br  x8
```

So Civ VII uses an object-based thread model: the general `create(out, attr, entry, arg)`
**fills a thread object with `entry`/`arg` and starts the fixed trampoline**, which then
invokes `entry(arg)`. That object-filling wrapper is the function to find (ns2jb's `create`
is exactly this; it only *indirectly* reaches the SVC, via helpers). `+0x13DCC4` is not it.

**Search plan for the real `create`:** find callers of the cluster wrappers (they are called
via `bl` from the object-setting code, unlike the trampoline which is only `adr`'d), then keep
the one whose arguments are `(thread_out, attr, entry, arg)` and which writes `entry` into the
object at `+0x50`-relative before starting the thread. Cross-check against ns2jb's `create`
body (which saves the same `x19`–`x23` set and stores the entry/arg pair) rather than by
prefix length.

## Tooling in place

- `relocation/host/find_missing.py <dump.bin> <sdk-base>` — anchor search over a dumped SDK; reports the longest-prefix candidate per function.
- `relocation/host/a64.py` — focused AArch64 decoder: `functions`, `callers`, `disasm`, `globals` over the dump.
- `relocation/signatures/sdk-resolved.json` — the resolved Civ VII offsets with confidence (`exact`/`verified`/`anchor`/`lead`/`observed`) and the unresolved list.
- A fresh 6 MiB SDK dump is in `relocation/runs/` (`192.168.103.215-dump-0x68b4ca4000-…bin`); `relocation/signatures/nn-sdk-signatures.json` holds the ns2jb bodies.

## Plan

1. Verify the candidates by decoding their prologues (calling convention, saved registers) rather than trusting prefix length.
2. Relocate `create`/`join`/`exit` by finding the thread module around `current` (`+0x137050`) and cross-referencing `pthread_create` callers and the `tryWait`/`join` pair.
3. Port `frames.cjs build()` to the host (pure arithmetic; it is already language-neutral) and a `nativeWorker` client that writes the frame, `create`s the thread, and `join`s it.
4. Build the abort guard on `sdk_base + 0x5224E0`.
5. Only then re-run `abort-control`, gated on the measured reservation cost (~12160–12176).

## Related

- [[Wave harness]] — the console/host halves waiting on this
- [[Filesystem access]] — the mounted FS + reservation this drives
- [[Probe plan]] P7
