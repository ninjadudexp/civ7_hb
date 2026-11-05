---
tags: [civ7, switch2, homebrew, probes]
status: draft
---
ok 
# Probe Plan

Ordered experiments. Each probe ends with a saved report, and a failed probe stops the chain rather than skipping ahead.

## P0 environment

Obsidian CLI is working as of 2026-11-03, so retrieval works. Then link these notes into a project note. Keep a per-run folder for logs, hashes, and captured buffers.

## P1 entry verification

Run the Pico bootstrap on fw 23.0.1 and record main_base, sdk_base, calloc and free offsets, plus the loader port. Expect the same bases across boots of the same game build.

## P2 which image is running

Search the loaded image for SVC encodings. Legacy Switch 1 numbering gives send 0x21 and close 0x16. (An earlier version of this note claimed SmileBASIC shows 0x441/0x2c1; that was a byte-slicing error — SmileBASIC's wrappers are also Switch 1.) **Measured 2026-11-03: Civ VII's stub table is Switch 1, so the payload ABI matches; see [[Compat ABI]].**

## P3 SVC permission bitmap

Call GetInfo type 26 IsSvcPermitted for ids 0x4B, 0x4C, 0x73, 0x74, 0x77, 0x78, 0x60. Expect denial for a retail game. Sweep the full range only if the call is cheap.

**Measured (2026-11-03):** `IsSvcPermitted` returns kernel error `0xE401` even with the correct current-process handle, so it is unusable. The direct fallback fails hard: calling `CreateCodeMemory` (`svc #0x4b`, stub at `sdk+0x168d34`) **aborts the game process** — the compat kernel does not return a result for it. `ControlCodeMemory` (`0x4c`) is not even present in the compat SVC table.

**P3 verdict: code memory is denied to the game and the in-process RX path is closed.** Do not call these SVCs again; use the filesystem-service route.

## P4 service reachability

Try opening pm:shell, pm:dmnt, and jit:u from the game. Expect failures, which is why the filesystem route is the only candidate.

**Measured (2026-11-03):** the raw `connectToNamedPort` (`0x1f`) path is refused in compat mode — every name, including `fsp-srv` and `pm:info`, returned a **kernel** error (`0xf201`; `sm:` gave `0x10801`), which is the SVC being rejected, not the services. The raw SVC is not how a title reaches services.

The game reaches services through the SDK `getService`, which rides an SM session the process already holds. ns2jb's convention is `getService(out, name, nameLen)` with the name written into a scratch buffer and the handle read back from `out` (`ns2jb/engine/switch2/app/session.cjs`). `getService` relocated exactly at `sdk+0x14cc90`. Tooling: `relocate_host.py <ip> getservice` (payload `getServiceProbe`). This is the path to reuse for P6 (open `fsp-srv`).

**Result (2026-11-03):** via the SDK path, `fsp-srv` **opens** (handle returned) and `ldr:ro` also opens. Everything privileged is denied with SM result `0x1015` (module 21/SM): `pm:info`, `pm:shell`, `pm:dmnt`, `jit:u`, `ro:dmnt`, `spl:`, `ncm:`, `ns:`, `sm:`. So the game reaches the **filesystem service** but not the privileged ones — exactly the [[Porting roadmap]] assumption. **P4 closed.**

## P5 memory layout

Read alias, heap, ASLR, and stack region info plus QueryMemory walks. Determines whether any region can hold executable pages for the in-process fallback.

## P6 filesystem surface from the game

Open fsp-srv, Initialize, create and query scratch paths in save: and tmp:, and read allocation counters with command 1009. Expect the counter layout ns2jb records.

**Resolved (2026-11-03).** Opening and domain ops work (`Initialize 1`, `ConvertToDomain 0`, `Clone 2`, content storage `200`, `GetSize 4` = 4.98 GB), but path methods are refused with `0xf601` on the session, the domain, and the storage — a content storage is not an `IFileSystem`.

The fix is ns2jb's mounted-filesystem copy. The SDK mount table is in a **separate RW mapping** (not the `sdk_base` RX region), found by scanning readable+writable regions for `"save\0"`. The `save` accessor (name at `+24`, implementation at `+40`) leads to `service → manager → slots → borrowed`, and `CopyObject(1, u32(objectId))` on `borrowed` returns the **save `IFileSystem`**. Verified: `GetFreeSpace` ~62.6 MB, `GetEntryType` dir, `CreateDirectory`, `CreateFile`, `GetEntryType` = file — all canonical `0`.

Counters (`1009`) are a plain command on an **initialised, non-domain** session (16 × `u64`). The 59-object reservation (`domain(root, 1200)`) aborted the process standalone; it is opt-in until the wave preconditions are understood. Full detail in [[Filesystem access]].

## P7 filesystem qualification from the game

Port the wave profiles from engine/switch2/memory/probe-reserved-gap.cjs: abort-control, abort-hole-control, extent-absent, extent-present, positive. Re-measure geometry for the game's own traffic, since the game writes saves heavily and changes the general heap. Accept only when receive-order margin is at least 300 microseconds, held bytes before release reach 0x2600, allocation checks stay within the measured reservation cost, and the canary file remains intact.

**Measured (2026-11-03):** the reservation group works (59 `CreateObject` `1200`, each one object) but counter[5] moves ~12160–12176, **not** ns2jb's `59 * 208 = 12272`. The acceptance gate must use the measured cost. `abort-control` (the safe first wave) has not run yet.

**Abort profiles qualified (2026-11-03):** `worker-wave abort-control` and `worker-wave abort-hole-control` both reproduce ns2jb's 13-role wave exactly on Civ VII — every transport `0`, tails `0x320002`, canary `0`, fillers/guards `0x0202`, trigger `0x2EEA02`, and `nativeGuard 1/1/1`; both safety assertions hold. Remaining: `extent-*`/`negative`/`positive` (blocked on finding the `tmp` mount accessor), which qualify `readSize` and the overwrite window. See [[Work log]].

**Substrate complete (2026-11-03):** every primitive the wave needs is verified live — raw-SVC worker threads (`createThread`/`startThread`), the `longjmp` frame chain, `memcpy` + dynamic `{u32:addr}` args, the live-length abort guard, the event gates, blocking `wait` woken by a host `signal`, and a worker doing a real FS IPC with a valid reply. The remaining P7 work is orchestration: porting the 13-role driver. See [[Native worker port]] and [[Work log]].

**Async substrate refuted (2026-11-03):** the planned post-driven interleave is dead. `svcSendAsyncRequestWithUserBuffer` (#0x23), called with the correct libnx order `(out, session, buffer, size)`, returns `0xCC01` for an initialised fsp-srv session at every size, and does not create an event (the out-word is left holding the session handle). Async IPC is only supported for servers that registered an async handler, and fsp-srv has not. Concurrency must come from threads, so P7 stays gated on the worker thread-entry contract in [[Native worker port]].

## P8 payload build

Assemble engine/switch2/ro/payload for platform 2 and compare against manifest.json payloadBytes 4757 and payloadSha256. Write the missing npdm assembler using the schema in Atmosphere tests/TestSvc/TestSvc.npdm.json and validation rules in stratosphere/loader/source/ldr_meta.cpp.

## P9 launch and endpoint

From the filesystem service, launch one supported target and swap the CodeFS filesystem reference. Then confirm the payload answers commands 0x524f0010 through 0x524f0016 for query, map, unmap, permission change, and verified patch.

## P10 launcher bring-up

Map launcher code RX, publish one patch per 64-byte cache line, create the display, then launch one app package. Decide the display policy before writing any game render patch.

## Probe payload skeleton (P1–P4, P6)

Save the runnable copy as `research/CivSwitch/payloads/probes.js`; this note only keeps the design. The payload uses the loader globals from `core.js`: `log`, `toHex`, `read8`, `read32`, `read64`, `write64`, `write_buffer`, `malloc`, `call_rop`, and `main_base` / `sdk_base`.

```js
(async () => {
    // P1: bases the session already derives.
    await log("main_base " + toHex(main_base));
    await log("sdk_base  " + toHex(sdk_base));

    // P2: find SVC wrapper instructions, then read the immediate field.
    // AArch64 svc encoding is 0xD4000000 | imm16.
    function svcImmediates(base, span, limit = 40) {
        const found = [];
        for (let off = 0n; off < BigInt(span) && found.length < limit; off += 4n) {
            const insn = read32(base + off);
            if ((insn & 0xFF000000) === 0xD4000000) {
                const imm = insn & 0xFFFF;
                if (imm !== 0) found.push({ off, imm });
            }
        }
        return found;
    }

    // Legacy Switch 1 numbers: send 0x21, close 0x16, get_info 0x29,
    // connect_to_named_port 0x1F, sleep_thread 0x0B.
    const sdkSvcs = svcImmediates(sdk_base, 0x200000, 40);
    for (const item of sdkSvcs) await log("svc " + toHex(item.imm) + " @ sdk_base + " + toHex(item.off));

    // P3 and P4 need two wrappers: GetInfo and ConnectToNamedPort.
    const getInfo = sdkSvcs.find(x => x.imm === 0x29);
    const connect = sdkSvcs.find(x => x.imm === 0x1F);
    if (!getInfo || !connect) { await log("wrapper missing, abort"); return; }

    const out = malloc(16n);

    // svcGetInfo(u64* out, u32 id0, Handle handle, u64 id1)
    // IsSvcPermitted: id0 = 26, handle = 0 for the current process, id1 = SVC number
    for (const id of [0x4B, 0x4C, 0x73, 0x74, 0x77, 0x78, 0x60, 0x21]) {
        write64(out, 0n);
        const rc = call_rop(sdk_base + getInfo.off, out, 26n, 0n, BigInt(id), 0n, 0n);
        await log("svc 0x" + id.toString(16) + " rc=" + toHex(BigInt(rc)) +
                  " permitted=" + toHex(read64(out)));
    }

    // ConnectToNamedPort(name pointer, 0, 0, 0, 0, outHandle)
    const nameBuf = malloc(64n);
    const handleOut = out;
    for (const name of ["pm:shell", "pm:dmnt", "jit:u", "fsp-srv", "pm:info"]) {
        write_buffer(nameBuf, new TextEncoder().encode(name + "\0"));
        write64(handleOut, 0n);
        const rc = call_rop(sdk_base + connect.off, nameBuf, 0n, 0n, 0n, 0n, handleOut);
        await log(name + " rc=" + toHex(BigInt(rc)) + " handle=" + toHex(read32(handleOut)));
    }

    // P6: filesystem root then allocation counters, command 1009.
    // Build the request frame with write_buffer, send through the send wrapper
    // located the same way, and read sixteen u64 counters from the reply buffer.
    await log("probe set complete");
})()
```

Expected first-run result: the SVC immediate list should show either the legacy family or the Switch 2 family, `IsSvcPermitted` should report zero for the code-memory and process-memory ids, and `pm:shell`, `pm:dmnt`, and `jit:u` should fail while `fsp-srv` and `pm:info` succeed.

Caveat on P3: libnx marks `InfoType_IsSvcPermitted` as usable only with `svcSynchronizePreemptionState` on 14.0.0 and later, so the GetInfo call may itself error on current firmware. Fallback is to attempt `svcCreateCodeMemory` directly and record the returned result code; a non-zero result is the denial evidence.

P6 and P7 now have tooling: `research/CivSwitch/harness/console.js` plus `research/CivSwitch/harness/host.py`, with a byte-parity selftest against ns2jb's codecs. See [Wave harness](wave-harness.md).
