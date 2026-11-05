---
tags: [civ7, switch2, homebrew, launch, m4]
status: active
---

# Launch gate

Deep dive into the M4 step: how ns2jb gets a different filesystem reference into a launched process, derived from `engine/switch2/ro/launch.cjs` and `engine/switch2/ro/gate.cjs`.

## What it actually does

The swap is a **session-level TOCTOU on the FS domain object table**, not a data copy. A bound FS server session is parked, then a queued `code:` mount request for the target title is intercepted; when it adds a filesystem entry, the entry's object pointer is exchanged with a donor entry's object pointer so the launched process receives the donor's filesystem implementation. No bytes are copied at all.

## Preconditions

`launch.cjs` refuses to run unless all proofs are present and belong to the same session/report:

- `<session>-layout.json`, `<session>-loader-root-copy.json`, `<session>-bound-sessions.json`, `<session>-filesystem-types.json`, `<session>-gate-event.json`
- `root-native-info` reports `donor.ready` and a joined worker
- target is in the accepted list and equals `donor.title`

## Server-session layout used by the gate

`gate.cjs` reads and verifies these offsets in the bound server session object (`0x88` bytes):

| Offset | Field |
| --- | --- |
| `0x00` | vptr (must equal `row.raw` vptr) |
| `0x28` | parent waiter link |
| `0x30` | kernel wait word (the handle selected on) |
| `0x40` | state |
| `0x44` | receive result |
| `0x48` | receive handle |
| `0x60` | request buffer pointer |
| `0x68` | request size (`0x100`) |
| `0x70` | owning domain |
| `0x78` | metadata |

The waiter array must be exactly 7 contiguous entries at `0x110` stride; identity checks fail otherwise.

## Park, step, restore

1. **Park** — write the wait word at `0x30` to the parking handle, cancel the worker so the session rearms on the known waiter, then run one *empty* step to rebuild the kernel wait array (native FS has separate wait queues; a null-selected cancellation retries the same kernel wait array, so a selection is needed to rebuild it).
2. **Step** — write `0xffffffff` into the request header, poll `selections()`/`inspect()` up to 30 times, and inject a one-shot holder (`retained-inject-holder`) carrying `server/parent/vptr/park/receive/buffer/domain/metadata`. The step succeeds when a real request header appears and the session is idle on a known waiter. The captured request is `0x100` bytes.
3. **Restore** — write `0x30` back to the real wait word. If a parking event is used, signal it and wait for the parent epoch to change (retiring the old wait snapshot); otherwise cancel and run another step. `restoreGates()` handles several sessions that share one event by signalling once and clearing after all leave.

## Detecting the mount and swapping

Each captured request is decoded (`requestInfo`). The target mount is domain command `0` on the Loader root id with input `u64 == target`. Before and after each step, `entries()` walks the domain entry table (entries at `0x30` stride, owner at `0x18`, object at `0x20`, metadata at `0x28`) and computes added/removed entries.

When the wanted mount (first or second) arrives:

- require exactly one added entry whose metadata equals the donor's metadata;
- verify the filesystem identity and concrete adapter vptr for both the code and donor entries;
- `code.entry + 0x20 := donor.object`, then `donor.entry + 0x20 := code.object`;
- if the second write fails, verify the donor write and roll back with `code.entry + 0x20 := donor.object`;
- verify both owners (`code.entry + 0x18 == root.targetDomain`, `donor.entry + 0x18 == donor.domain`).

The swap is always restored in `finally` via `gate.restore()`, and after restoration the pipeline waits for the PM launch to finish, joins the worker, and reads the reply. Success is a zero result plus a pid.

## Why this matters for Civ VII

The FS breakout gives the game process FS-shaped memory access; the gate is what turns that into a *launched process running the payload*. Porting it needs the two verified native primitives the gate depends on — `retained-inject-holder` and, for the held proofs, a way to walk the domain table — which are privesc-independent but rely on the native-root worker. Those primitives are part of the [[Primitive matrix]] `needs relocation` work, not the filesystem wave work.

`retained-inject-holder` is supplied by the [[Retained memory]] channel: the gate's `step()` writes `0xffffffff` into the request header, then injects a one-shot holder with `server/parent/vptr/park/receive/buffer/domain/metadata`. The retained channel performs the unverified pointer store and immediately cancels the seven API workers, which is what makes the single step atomic with respect to the parked FS session.
