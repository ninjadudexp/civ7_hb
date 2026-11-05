// M1 SDK relocation payload, console side.
//
// Load after core.js with send_js.py, then drive it from relocation/host/relocate_host.py
// on port 9028.
//
// It does NOT embed signatures: the host sends the ns2jb-derived table at runtime
// (setSignatures), so the table can be regenerated without resending this payload.
//
// Commands: status, setSignatures, scan, svcscan, svcindex, bytescan, svccall,
// query, info, read, call, exit. The SVC index is cached, so repeated env lookups
// are free. Closing the control socket does not unload the payload; send `exit`.
//
// Payloads are eval'd inside core.js's scope, so log, toHex, read8/16/32/64,
// write8/16/32/64, read_buffer, write_buffer, text_encode, bytes_to_string, malloc,
// call_rop, socket/bind/listen/accept/recv/send/close, sleep, main_base and sdk_base
// are already in scope.
(async () => {
    const CONTROL_PORT = 9028;
    const RET = 0xD65F03C0;
    const DEFAULT_SPAN = 0x600000n;
    const MAX_READ = 0x10000;

    // Reading an unmapped address aborts the game process, and JS cannot catch it.
    // So every sweep is bounded. sdk+0x600000 is proven readable; wider spans need
    // force:true and should be widened one step at a time.
    const MAX_SPAN = 0x800000n;
    const FORCE_MAX = 0x8000000n;

    const say = window.log;
    const rd32 = addr => Number(read32(addr)) >>> 0;
    const rd64 = addr => read64(addr);

    // ---- hex helpers (per-byte read8 is too slow for dumps) ----
    const HEX = "0123456789abcdef";
    function readHex(address, length) {
        const parts = [];
        let i = 0;
        for (; i + 8 <= length; i += 8) {
            let v = rd64(address + BigInt(i));
            for (let b = 0; b < 8; b++) { const byte = Number(v & 0xffn); v >>= 8n; parts.push(HEX[byte >> 4], HEX[byte & 15]); }
        }
        if (i < length) {
            let v = rd64(address + BigInt(i));
            for (; i < length; i++) { const byte = Number(v & 0xffn); v >>= 8n; parts.push(HEX[byte >> 4], HEX[byte & 15]); }
        }
        return parts.join("");
    }

    const fromHex = text => {
        const out = new Uint8Array(text.length >> 1);
        for (let i = 0; i < out.length; i++) out[i] = parseInt(text.slice(i * 2, i * 2 + 2), 16);
        return out;
    };

    const word = (u8, off) => (u8[off] | u8[off + 1] << 8 | u8[off + 2] << 16 | u8[off + 3] << 24) >>> 0;

    // ---- signature store ----
    const store = { functions: [], svc: [], exact: new Map(), anchor: new Map() };
    // Decoded syscall stub entries: SVC number (hex value) -> offset from sdk_base.
    // Loaded from the host (`setSvcTable`) so SVC calls survive ASLR and drifted stubs.
    const svcTable = new Map();
    // Relocated SDK library functions: name -> offset from sdk_base (`setFunctions`).
    const sdkFunctions = new Map();

    function addTo(map, key, entry) {
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(entry);
    }

    function setSignatures(payload) {
        store.functions = (payload.functions || []).map(f => ({
            name: f.name, milestone: f.milestone || "M1", purpose: f.purpose || "",
            bytes: fromHex(f.bytes), exactReliable: f.exactReliable !== false,
            anchorOffset: f.anchorOffset | 0, anchorBytes: fromHex(f.anchorBytes || f.bytes),
        }));
        store.svc = (payload.svc || []).map(s => ({ name: s.name, s1: s.s1, s2: s.s2 ?? null, code: s.code || null }));
        store.exact = new Map();
        store.anchor = new Map();
        for (const f of store.functions) {
            if (f.bytes.length >= 4) addTo(store.exact, word(f.bytes, 0), f);
            if (f.anchorBytes.length >= 4) addTo(store.anchor, word(f.anchorBytes, 0), f);
        }
        return { functions: store.functions.length, svc: store.svc.length };
    }

    function equalsAt(address, u8, length = u8.length) {
        let i = 0;
        for (; i + 8 <= length; i += 8) {
            let v = rd64(address + BigInt(i));
            for (let b = 0; b < 8; b++) { if (Number(v & 0xffn) !== u8[i + b]) return false; v >>= 8n; }
        }
        if (i < length) {
            let v = rd64(address + BigInt(i));
            for (; i < length; i++) { if (Number(v & 0xffn) !== u8[i]) return false; v >>= 8n; }
        }
        return true;
    }

    // One pass over the region, two alignments per 8-byte read, checking the
    // exact and anchor first-word maps. Candidates are verified in full.
    function scan(base, span) {
        const found = new Map(store.functions.map(f => [f.name, { name: f.name, milestone: f.milestone,
            purpose: f.purpose, exact: [], anchor: [], best: null }]));
        const limit = base + span;
        for (let addr = base; addr + 8n <= limit; addr += 8n) {
            let v = rd64(addr);
            const positions = [[addr, Number(v & 0xffffffffn) >>> 0], [addr + 4n, Number((v >> 32n) & 0xffffffffn) >>> 0]];
            for (const [at, w] of positions) {
                const exactHits = store.exact.get(w);
                if (exactHits) for (const f of exactHits) {
                    const row = found.get(f.name);
                    if (row.exact.length < 8 && equalsAt(at, f.bytes)) row.exact.push(Number(at));
                }
                const anchorHits = store.anchor.get(w);
                if (anchorHits) for (const f of anchorHits) {
                    const row = found.get(f.name);
                    if (row.anchor.length >= 8) continue;
                    if (equalsAt(at, f.anchorBytes)) {
                        const baseAddr = Number(at) - f.anchorOffset;
                        row.anchor.push({ address: Number(at), base: baseAddr, matched: f.anchorBytes.length });
                    }
                }
            }
        }
        for (const row of found.values()) {
            if (row.exact.length) row.best = { kind: "exact", address: row.exact[0], sdkOffset: row.exact[0] - Number(base) };
            else if (row.anchor.length) row.best = { kind: "anchor", ...row.anchor[0], sdkOffset: row.anchor[0].base - Number(base) };
        }
        return { base: Number(base), span: Number(span), functions: [...found.values()] };
    }

    // svc wrappers are located by immediate, not by bytes. One pass indexes every
    // `svc #imm` in the region; results are cached so repeated lookups are free.
    const SVC_TOP = 0x6a0;   // 0xD4000001 >>> 21
    let svcIndex = { base: null, span: null, byImm: new Map(), all: [] };

    function ensureSvcIndex(base, span, cap = 4096) {
        if (svcIndex.base === base && svcIndex.span === span) return svcIndex;
        const byImm = new Map(), all = [];
        for (let off = 0n; off + 8n <= span; off += 4n) {
            const w = rd32(base + off);
            if ((w & 0x1f) !== 1 || (w >>> 21) !== SVC_TOP) continue;  // svc #imm
            const imm = (w >>> 5) & 0xffff;
            const address = Number(base + off);
            const ret = rd32(base + off + 4n) === RET;
            if (all.length < cap) all.push({ address, imm, ret });
            if (!byImm.has(imm)) byImm.set(imm, []);
            if (byImm.get(imm).length < 8) byImm.get(imm).push({ address, ret });
        }
        svcIndex = { base, span, byImm, all };
        return svcIndex;
    }

    const svcCandidates = (id, base, span) => {
        if (id === null || id === undefined) return [];
        const list = ensureSvcIndex(base, span).byImm.get(id) || [];
        return list.map(entry => entry.address);
    };

    function svcScan(base, span) {
        const index = ensureSvcIndex(base, span);
        return store.svc.map(entry => ({
            name: entry.name, switch1: entry.s1, switch2: entry.s2,
            foundSwitch1: svcCandidates(entry.s1, base, span),
            foundSwitch2: svcCandidates(entry.s2, base, span),
            allSwitch1: ((index.byImm.get(entry.s1) || []).map(x => x.ret)),
        }));
    }

    // A stub may start with a save before its svc, so accept the svc anywhere in
    // the first few instructions but stop at a return/branch (next stub boundary).
    function entryContainsSvc(entry, id) {
        for (let i = 0n; i < 6n; i++) {
            const w = rd32(entry + i * 4n);
            if ((w & 0x1f) === 1 && (w >>> 21) === SVC_TOP) return ((w >>> 5) & 0xffff) === id;
            if (w === RET || (w & 0xFC000000) === 0x14000000) return false;
        }
        return false;
    }

    function findSvc(id, base, span) {
        if (id !== null && id !== undefined && svcTable.has(id)) {
            const at = base + BigInt(svcTable.get(id));
            // Guard: never call a table entry that is not the expected syscall. A
            // bad offset otherwise jumps into arbitrary code and aborts the game.
            if (!entryContainsSvc(at, id))
                throw new Error("svc table entry 0x" + id.toString(16) + " is not a matching svc at sdk+0x" +
                    svcTable.get(id).toString(16) + "; refusing to call");
            return at;
        }
        const candidates = svcCandidates(id, base, span);
        return candidates.length ? BigInt(candidates[0]) : null;
    }

    // Arbitrary byte-pattern search over a region; used to hunt drifted code.
    function byteScan(base, span, u8, cap = 64) {
        if (u8.length < 4) throw new Error("pattern must be at least 4 bytes");
        const hits = [], first = word(u8, 0);
        const end = base + span - BigInt(u8.length);
        for (let at = base; at <= end; at += 4n) {
            if (rd32(at) !== first) continue;
            if (equalsAt(at, u8)) hits.push(Number(at));
            if (hits.length >= cap) break;
        }
        return hits;
    }

    // ---- control transport (mirrors harness/console.js) ----
    const pageAddress = pages => {
        const raw = malloc(Number(BigInt(pages || 1) * 4096n) + 4096);
        return (raw + 4095n) & ~4095n;
    };
    // 160 KiB each: large enough for a dump reply, small enough that calloc succeeds.
    const io = { head: pageAddress(1), out: pageAddress(40), in: pageAddress(40) };

    function openListener(port) {
        const addr = malloc(16), on = malloc(4);
        write_buffer(addr, new Uint8Array(16));
        write32(on, 1);
        const fd = socket(AF_INET, SOCK_STREAM, 0);
        if (fd === -1n) throw new Error("relocate control socket failed");
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, on, 4);
        write8(addr, 16n); write8(addr + 1n, 2n);
        write16(addr + 2n, BigInt(((port & 0xff) << 8) | (port >> 8)));
        write32(addr + 4n, 0n);
        if (bind(fd, addr, 16) === -1n) throw new Error("relocate control bind failed on " + port);
        if (listen(fd, 2) === -1n) throw new Error("relocate control listen failed on " + port);
        return fd;
    }

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
        if (size <= 0 || size > 0x100000) throw new Error("Bad frame size " + size);
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
        head[0] = bytes.length & 255; head[1] = (bytes.length >> 8) & 255;
        head[2] = (bytes.length >> 16) & 255; head[3] = (bytes.length >> 24) & 255;
        write_buffer(io.head, head);
        if (Number(send(fd, io.head, 4, 0)) !== 4) throw new Error("frame header truncated");
        let sent = 0;
        while (sent < bytes.length) {
            const chunk = bytes.subarray(sent, sent + 0x8000);
            write_buffer(io.out, chunk);
            const n = Number(send(fd, io.out, chunk.length, 0));
            if (n <= 0) throw new Error("frame body truncated");
            sent += n;
        }
    }

    const regionOf = request => {
        const base = BigInt(request.base ?? Number(sdk_base));
        const span = BigInt(request.span ?? Number(DEFAULT_SPAN));
        const cap = request.force ? FORCE_MAX : MAX_SPAN;
        if (span <= 0n || span > cap) throw new Error("span 0x" + span.toString(16) + " exceeds " +
            (request.force ? "force max" : "safe max") + " 0x" + cap.toString(16) +
            "; an unmapped read aborts the game");
        return { base, span };
    };

    let stop = false;
    function handle(request) {
        switch (request.command) {
            case "status":
                return { ok: true, mainBase: Number(main_base), sdkBase: Number(sdk_base),
                    defaultSpan: Number(DEFAULT_SPAN), loaded: store.functions.length };
            case "setSignatures": {
                const counts = setSignatures(request);
                say("relocate: signatures set (" + counts.functions + " functions, " + counts.svc + " svc)");
                return { ok: true, ...counts };
            }
            case "setSvcTable": {
                svcTable.clear();
                for (const entry of request.entries || []) svcTable.set(entry.imm, entry.entryOffset);
                say("relocate: svc table set (" + svcTable.size + " entries)");
                return { ok: true, entries: svcTable.size };
            }
            case "setFunctions": {
                sdkFunctions.clear();
                for (const entry of request.entries || []) sdkFunctions.set(entry.name, entry.sdkOffset);
                say("relocate: sdk table set (" + sdkFunctions.size + " functions)");
                return { ok: true, entries: sdkFunctions.size };
            }
            case "scan": {
                if (!store.functions.length) return { ok: false, error: "setSignatures first" };
                const { base, span } = regionOf(request);
                const report = scan(base, span);
                const svc = svcScan(base, span);
                const byMilestone = {};
                let found = 0;
                for (const row of report.functions) if (row.best) { found++; byMilestone[row.milestone] = (byMilestone[row.milestone] || 0) + 1; }
                return { ok: true, mainBase: Number(main_base), sdkBase: Number(sdk_base),
                    ...report, svc, summary: { total: report.functions.length, found, byMilestone } };
            }
            case "svcscan": {
                const { base, span } = regionOf(request);
                return { ok: true, svc: svcScan(base, span) };
            }
            case "svcindex": {
                const { base, span } = regionOf(request);
                const index = ensureSvcIndex(base, span);
                const counts = {};
                for (const entry of index.all) counts[entry.imm] = (counts[entry.imm] || 0) + 1;
                return { ok: true, base: Number(base), span: Number(span), total: index.all.length,
                    byImm: counts, instructions: index.all.slice(0, 512) };
            }
            case "bytescan": {
                const { base, span } = regionOf(request);
                const hits = byteScan(base, span, fromHex(request.hex));
                return { ok: true, base: Number(base), span: Number(span), hits };
            }
            case "regions": {
                // Enumerate the whole address space via queryMemory (0x06).
                const qAddr = findSvc(0x06, sdk_base, DEFAULT_SPAN);
                if (qAddr === null) return { ok: false, error: "queryMemory not available" };
                const out = pageAddress(1), page = pageAddress(1);
                const regions = [];
                let at = 0n;
                for (let guard = 0; guard < 512 && at < (1n << 39n); guard++) {
                    write64(out, 0n); write64(out + 8n, 0n); write64(out + 16n, 0n); write64(out + 24n, 0n);
                    write32(page, 0);
                    const result = BigInt.asIntN(64, call_rop(qAddr, out, page, at));
                    if (result !== 0n) break;
                    const base = rd64(out), size = rd64(out + 8n);
                    if (size === 0n) break;
                    regions.push({ base: Number(base), size: Number(size),
                        type: rd32(out + 16n), attr: rd32(out + 20n), perm: rd32(out + 24n) });
                    at = base + size;
                }
                return { ok: true, regions };
            }
            case "scanRegions": {
                // Bounded scan of explicit regions the host has verified readable.
                // CRITICAL: rd32/read8 internally read 8 bytes, so every access must
                // keep 8 bytes of slack from the region end or it can fault the process.
                const SLACK = 8;
                const names = (request.names || []).map(text_encode).filter(n => n.length >= 4);
                const window = Number(request.window ?? 0x60);
                const results = [];
                let scanned = 0;
                for (const reg of request.regions || []) {
                    const base = BigInt(reg.base), size = Number(reg.size);
                    const hits = [];
                    // stop short of the end by the name length plus slack
                    const limit = size - Math.max(...names.map(n => n.length)) - SLACK;
                    for (let off = 0; off < limit && hits.length < 32; off += 4) {
                        scanned += 4;
                        const w = rd32(base + BigInt(off));
                        for (const nm of names) {
                            if (w !== word(nm, 0)) continue;
                            if (equalsAt(base + BigInt(off), nm)) {
                                const want = Math.min(window, size - off - SLACK);
                                hits.push({ offset: off, hex: readHex(base + BigInt(off), Math.max(0, want)) });
                            }
                            break;
                        }
                    }
                    if (hits.length) results.push({ base: Number(base), size, hits });
                }
                return { ok: true, scanned, results };
            }
            case "svccall": {
                const { base, span } = regionOf(request);
                const id = request.id ?? (store.svc.find(s => s.name === request.name) || {}).s1;
                const addr = findSvc(id, base, span);
                if (addr === null) return { ok: false, error: "no svc wrapper for id " + id };
                const args = Array.from({ length: 6 }, (_, i) => BigInt(request.args?.[i] ?? 0));
                const result = BigInt.asIntN(64, call_rop(addr, ...args));
                return { ok: true, id, address: Number(addr), result: result.toString() };
            }
            case "query": {
                const { base, span } = regionOf(request);
                const out = pageAddress(1), page = pageAddress(1);
                write64(out, 0n); write64(out + 8n, 0n); write64(out + 16n, 0n); write64(out + 24n, 0n);
                write32(page, 0);
                const addr = findSvc(0x06, base, span);  // queryMemory
                if (addr === null) return { ok: false, error: "queryMemory wrapper not found" };
                const result = BigInt.asIntN(64, call_rop(addr, out, page, BigInt(request.address)));
                return { ok: true, address: Number(request.address), result: result.toString(),
                    memoryInfoHex: readHex(out, 0x40), pageInfo: rd32(page) };
            }
            case "info": {
                const { base, span } = regionOf(request);
                const out = pageAddress(1);
                write64(out, 0n);
                const addr = findSvc(0x29, base, span);  // getInfo
                if (addr === null) return { ok: false, error: "getInfo wrapper not found" };
                const result = BigInt.asIntN(64, call_rop(addr, out, BigInt(request.type), BigInt(request.handle ?? 0), BigInt(request.argument ?? 0)));
                return { ok: true, type: request.type, handle: request.handle ?? 0, argument: request.argument ?? 0,
                    result: result.toString(), value: rd64(out).toString() };
            }
            case "codeMemoryProbe": {
                // P3 fallback: IsSvcPermitted is unsupported here, so ask the kernel
                // directly. A non-zero result is the permission denial; a zero result
                // returns a code-memory handle that we close immediately.
                const { base, span } = regionOf(request);
                const out = pageAddress(1);
                write64(out, 0n);
                const addr = findSvc(0x4b, base, span);  // CreateCodeMemory
                if (addr === null) return { ok: false, error: "CreateCodeMemory (0x4b) not in svc table" };
                const result = BigInt.asIntN(64, call_rop(addr, out, BigInt(request.size ?? 0x1000)));
                const handle = rd32(out);
                let closed = null;
                if (!result && handle) {
                    const closeAddr = findSvc(0x16, base, span);
                    if (closeAddr !== null) closed = BigInt.asIntN(64, call_rop(closeAddr, BigInt(handle))).toString();
                }
                return { ok: true, result: result.toString(),
                    resultHex: "0x" + BigInt.asUintN(64, result).toString(16),
                    handle, permitted: result === 0n, closed };
            }
            case "serviceProbe": {
                // P4: connectToNamedPort (0x1f) is a permitted SVC; a denied service
                // returns an error (it does not abort the process).
                const { base, span } = regionOf(request);
                const connectAddr = findSvc(0x1f, base, span);
                if (connectAddr === null) return { ok: false, error: "connectToNamedPort (0x1f) not in svc table" };
                const closeAddr = findSvc(0x16, base, span);
                const out = pageAddress(1), nameBuf = pageAddress(1);
                const names = request.names || ["pm:info", "fsp-srv", "sm:", "pm:shell", "pm:dmnt",
                    "jit:u", "ldr:ro", "ro:dmnt", "spl:", "ncm:", "ns:"];
                const services = [];
                for (const name of names) {
                    write_buffer(nameBuf, text_encode(name + "\0"));
                    write64(out, 0n);
                    const result = BigInt.asIntN(64, call_rop(connectAddr, out, nameBuf));
                    const handle = rd32(out);
                    let closed = null;
                    if (!result && handle && closeAddr)
                        closed = BigInt.asIntN(64, call_rop(closeAddr, BigInt(handle))).toString();
                    services.push({ name, result: result.toString(),
                        resultHex: "0x" + BigInt.asUintN(64, result).toString(16), handle,
                        opened: result === 0n, closed });
                }
                return { ok: true, services };
            }
            case "getServiceProbe": {
                // The raw named-port SVC is refused in compat mode; the SDK getService
                // rides an SM session the game already holds.
                // ns2jb convention: getService(out, name, nameLen), handle written to *out.
                const { base } = regionOf(request);
                const off = sdkFunctions.get("getService");
                if (off === undefined) return { ok: false, error: "getService is not in the SDK table" };
                const fn = base + BigInt(off);
                const closeOff = sdkFunctions.get("close");
                const closeFn = closeOff === undefined ? null : base + BigInt(closeOff);
                const values = pageAddress(1), nameAt = values + 8n;
                const names = request.names || ["fsp-srv", "pm:info", "pm:shell", "pm:dmnt", "jit:u",
                    "ldr:ro", "ro:dmnt", "spl:", "ncm:", "ns:", "sm:"];
                const services = [];
                for (const name of names) {
                    write_buffer(values, new Uint8Array(32));
                    write_buffer(nameAt, text_encode(name));
                    const result = BigInt.asIntN(64, call_rop(fn, values, nameAt, BigInt(name.length)));
                    const handle = rd32(values);
                    let closed = null;
                    if (!result && handle && closeFn)
                        closed = BigInt.asIntN(64, call_rop(closeFn, BigInt(handle))).toString();
                    services.push({ name, result: result.toString(),
                        resultHex: "0x" + BigInt.asUintN(64, result).toString(16), handle,
                        opened: result === 0n, closed });
                }
                return { ok: true, services };
            }
            case "read": {
                const length = Number(request.length);
                if (!Number.isInteger(length) || length <= 0 || length > MAX_READ) return { ok: false, error: "length 1.." + MAX_READ };
                return { ok: true, address: Number(request.address), hex: readHex(BigInt(request.address), length) };
            }
            case "call": {
                const args = Array.from({ length: 6 }, (_, i) => BigInt(request.args?.[i] ?? 0));
                const result = BigInt.asIntN(64, call_rop(BigInt(request.address), ...args));
                return { ok: true, address: Number(request.address), result: result.toString(),
                    resultHex: "0x" + BigInt.asUintN(64, result).toString(16) };
            }
            case "exit":
                stop = true;
                return { ok: true, stopping: true };
            default:
                throw new Error("Unknown command " + request.command);
        }
    }

    async function serve(fd) {
        await say("relocate control ready on " + CONTROL_PORT);
        while (!stop) {
            const request = readFrame(fd);
            if (!request) break;
            let reply;
            try { reply = handle(request); }
            catch (error) { reply = { ok: false, error: String(error?.message ?? error), stack: String(error?.stack ?? "") }; }
            writeFrame(fd, { id: request.id, ...reply });
            if (stop) break;
        }
        close(fd);
        await say("relocate control closed");
    }

    await say("relocate payload up; send setSignatures + scan from relocate_host.py");
    const control = openListener(CONTROL_PORT);
    while (!stop) {
        const client = accept(control, malloc(16), malloc(8));
        if (client === -1n) { await sleep(200); continue; }
        await say("relocate client connected");
        await serve(client);
    }
    close(control);
})()
;
