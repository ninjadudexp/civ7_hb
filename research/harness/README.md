# Civ VII wave harness (M2)

Two halves, plus a byte-parity selftest that runs without a console.

| File | Side | Purpose |
| --- | --- | --- |
| `console.js` | Switch 2, inside Civ VII | Relocate SVC wrappers, stage page-aligned request buffers, send synchronous requests, post asynchronous requests, collect replies, timestamp with the native counter, run a wave plan. Loaded after `core.js` with `send_js.py`. Control port `9027`. |
| `host.py` | workstation | Every wire format: CMIF packet, domain/control framing, close-object, path packets, context marking, reply parsing. Owns the filesystem bootstrap, the reservation group, path staging, wave profiles, and report files in `runs/`. |
| `wire-selftest.cjs` | workstation | Builds 17 frames and 4 crafted replies with ns2jb's own codecs (`engine/shared/ipc.cjs`, `engine/shared/domain.cjs`, `engine/switch2/memory/paths.cjs`). |
| `wire-selftest.py` | workstation | Builds the same 17 frames and 4 replies with the Python port. |
| `wire-diff.py` | workstation | Compares sha256 per frame and per parsed reply. Exit code 0 means identical. |

## Run the selftest

```sh
node wire-selftest.cjs && python wire-selftest.py && python wire-diff.py
```

Current result: `{"frames": 17, "replies": 4, "identical": true, "issues": []}`.

## Run against the console

```sh
python ../send_js.py <console-ip> console.js       # after core.js is live on port 9026
python host.py --host <console-ip>                 # status + relocation report
python host.py --host <console-ip> setup           # open fsp-srv, scratch tree, reservation group
python host.py --host <console-ip> wave abort-control   # qualification wave, writes runs/*.json
```

### Console-side prerequisites (measured 2026-11-03)

The harness does not relocate anything itself. `host.py` pushes the decoded tables
from `../relocation/signatures/` before anything runs:

- `setSvcTable` — `imm -> sdk offset` for all 60 syscall stubs. Scanning for
  `svc; ret` misses every multi-instruction stub (e.g. `queryMemory`, `getInfo`).
- `setFunctions` — relocated SDK library functions (`getService`, `tick`, ...).
- The tick comes from the SDK `tick` (`mrs x0, cntvct_el0; ret`); there is no
  `getSystemTick` stub in the table.
- `openService` uses the SDK `getService` (`getService(out, name, nameLen)`). The raw
  `connectToNamedPort` SVC is refused in compat mode and returns a kernel error for
  every name, including `fsp-srv`.

`abort-control`, `extent-absent`, `extent-present`, and `positive` differ only in the
trigger payload bytes, mirroring ns2jb's profiles.

## What is verified and what is not

* **Verified offline:** wire codecs are byte-identical to ns2jb's codecs, and reply parsing
  agrees on crafted replies.
* **Not yet run:** nothing in `console.js` has executed on the console. The SVC scan has
  never been run against this game build, so wrapper addresses and the Switch 1 versus
  Switch 2 numbering question are still open.
* **Inferred command numbers:** `10`, `0`, `1`, `2`, `5`, `7`, `1002`, `1009`, `1200` come
  from ns2jb's call sites, not from a published spec. The first `setup` run must confirm
  each one by its reply, especially the reservation group's expected `59 * 208` allocation.
* **Timing is not portable:** ns2jb's delays were measured against its SmileBASIC native
  thread. Civ VII's game thread has different scheduling, so the spacing gate is a
  measurement target, not a copied constant.
* **Path descriptor size stays `0x800` while the staged page holds `0x2800`;** that gap is
  the thing being probed, not a bug in the harness.
