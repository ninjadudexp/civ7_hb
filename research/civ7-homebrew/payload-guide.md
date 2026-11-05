---
tags: [civ7, switch2, homebrew, payload, guide, m1]
status: active
---

# Payload guide and telemetry loop

How to load payloads on the Switch 2 and hand results back so the research continues without copy/paste. Everything here is under `research/CivSwitch/`, which lives inside this vault, so any file written there can be read directly.

## The loop in one line

You send a console payload, the workstation driver captures its output into `research/CivSwitch/relocation/runs/`, and I read those files and update the notes. You never need to paste logs.

## Prerequisites

- Remote JS Loader live on the console, port `9026` (boot-persistent install, or a fresh chat-XSS session).
- Console and PC on the same LAN, consoles blocked from Nintendo servers (90DNS).
- Python 3 on the workstation. Node is only needed to regenerate signatures.
- Confirm the loader is reachable: the loader logs `Awaiting payload at <ip>:9026`.

## Where payloads live

| Path | Purpose |
| --- | --- |
| `research/CivSwitch/payloads/` | small one-shot payloads (e.g. `helloworld.js`) |
| `research/CivSwitch/harness/console.js` | M2 wave harness, resident on `:9027` |
| `research/CivSwitch/relocation/payload/relocate.js` | M1 relocation + environment probe, resident on `:9028` |

A payload is plain JavaScript, `eval`'d inside `core.js`'s scope. The helpers already in scope are `log`, `toHex`, `read8/16/32/64`, `write8/16/32/64`, `read_buffer`, `write_buffer`, `text_encode`, `bytes_to_string`, `malloc`, `free`, `call_rop`, `socket/bind/listen/accept/recv/send/close`, `sleep`, `main_base`, and `sdk_base`. Wrap top-level work in `(async () => { ... })()`, exactly like the existing payloads. Send any file with:

```
python research/CivSwitch/send_js.py <console-ip> <file.js>
```

The loader evaluates one payload at a time and streams `log()` back. A resident payload keeps that connection open until it returns, so `send_js.py` waits for its timeout — that is normal for `console.js` and `relocate.js`.

## Step 1 — M1 relocation telemetry (do this first)

From `research/CivSwitch/relocation/`:

```
python host/relocate_host.py <console-ip> stop      # only if an older payload is resident
python host/relocate_host.py <console-ip> load      # send payload/relocate.js, resident on :9028
python host/relocate_host.py <console-ip> diag      # bases, readability, every svc immediate
python host/relocate_host.py <console-ip> env       # bases, memory regions, SVC permission
python host/relocate_host.py <console-ip> scan      # SDK signature relocation
python host/relocate_host.py <console-ip> svcscan   # svc wrappers, both numbering schemes
python host/relocate_host.py <console-ip> dump-sdk  # pull the SDK region for offline work
```

Output files:

| File in `relocation/runs/` | What it tells us |
| --- | --- |
| `<ip>-env-<ts>.json` | `main_base`, `sdk_base`, image regions via `queryMemory`, `getInfo`, `IsSvcPermitted` for the code-memory and IPC SVCs (Probe plan P1–P5) |
| `<ip>-relocate-<ts>.json` | which ns2jb SDK signatures matched the game image, exact or anchor-only, with `sdkOffset` |
| `<ip>-svc-<ts>.json` | SVC wrapper candidates for Switch 1 and Switch 2 immediates |
| `<ip>-sdk-<ts>.bin` | raw SDK dump; I can rescan it offline with `--offline` |

Interpretation: `=` means the full ns2jb body matched, `~` means only the short anchor matched (function localised, body drifted), `?` means not found and needs a hand-built signature.

## Step 2 — ABI decision (Probe plan P2)

The `svcscan` result decides the number family. If `foundSwitch1` is populated and `foundSwitch2` is empty, Civ VII uses Switch 1 numbering and `console.js`'s table is correct. If it is reversed, the SVC table and payload ABI both change. Capture both.

## Step 3 — M2/M3 wave harness

Once the SVC table is confirmed, load the wave harness and requalify the breakout:

```
python research/CivSwitch/send_js.py <console-ip> research/CivSwitch/harness/console.js
cd research/CivSwitch/harness
python host.py
python host.py setup
python host.py wave abort-control
```

`host.py` writes `harness/runs/*.json`. The `abort-control` wave is the safe first step: it is gated to stop before any overwrite.

## Step 4 — hand results back

Tell me the run is done. I read `relocation/runs/` and `harness/runs/` directly, compare against the ns2jb constants, and update [[Porting roadmap]], [[Primitive matrix]], [[Probe plan]], and [[Work log]]. If a signature did not match, the dump lets me build a new anchor and regenerate the table with `node build-signatures.cjs`.

## Fixes already applied

- `harness/console.js` and this toolchain now decode SVC immediates correctly (`0xD4000001 | (imm << 5)`). The earlier `0xD4000000 | id` would have matched nothing.

## Safety

- The relocation payload only reads memory and calls `queryMemory`/`getInfo`/`getSystemTick`; it performs no writes.
- The `dump-sdk` and `env` commands are read-only.
- Reading an **unmapped** address aborts the game process and cannot be caught. Sweeps are capped (`safe max 0x800000`); `sdk+0x600000` is proven safe, `main+0x4000000` crashed. Widen one step at a time and expect a restart after any fault.
- The M3 wave harness *does* write into filesystem structures; keep using its abort controls and recovery paths, and reboot the console if `placementUncertain` is set.

## Related

- [[Porting roadmap]] — the stages this feeds
- [[Probe plan]] — P1–P7
- [[Primitive matrix]] — what must be relocated
- [[Compat ABI]] — the numbering decision
- [[Wave harness]] — M2/M3
