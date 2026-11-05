#!/usr/bin/env python3
"""relocate_host.py - drive the M1 relocation payload on a Switch 2 and capture telemetry.

Console side:  payload/relocate.js  (send with ../send_js.py, resident on :9028)
Signatures:    signatures/nn-sdk-signatures.json  (regenerate with build-signatures.cjs)
Telemetry:     everything is written under relocation/runs/ so it can be read back
               from the vault without copy/paste.

Examples
--------
  python relocate_host.py 192.168.1.215 load            # send payload/relocate.js to :9026
  python relocate_host.py 192.168.1.215 stop            # unload it so a new payload can be sent
  python relocate_host.py 192.168.1.215 diag            # bases, readability, every svc immediate
  python relocate_host.py 192.168.1.215 status
  python relocate_host.py 192.168.1.215 scan            # writes runs/<ip>-relocate-<ts>.json
  python relocate_host.py 192.168.1.215 dump-sdk        # writes runs/<ip>-sdk-<ts>.bin (0x600000)
  python relocate_host.py 192.168.1.215 dump 0x700000 0x20000
  python relocate_host.py 192.168.1.215 svcscan
  python relocate_host.py 192.168.1.215 env              # P1-P5 region + SVC-permission report
  python relocate_host.py 192.168.1.215 codemem          # P3 fallback: probe CreateCodeMemory (aborts if denied)
  python relocate_host.py 192.168.1.215 services         # P4 via the raw named-port SVC
  python relocate_host.py 192.168.1.215 getservice       # P4 via the SDK getService path
  python relocate_host.py 192.168.1.215 read 0x700000 64
  python relocate_host.py 192.168.1.215 call 0x70a000 1 2
  python relocate_host.py --offline runs/<ip>-sdk-<ts>.bin        # scan a dump without a console
"""

import json
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # relocation/
SIGNATURES = os.path.join(HERE, "signatures", "nn-sdk-signatures.json")
SVC_RELOCATION = os.path.join(HERE, "signatures", "svc-relocation.json")
SDK_RELOCATION = os.path.join(HERE, "signatures", "sdk-relocation.json")
RUNS = os.path.join(HERE, "runs")
PORT = 9028
CHUNK = 0x4000

# Spans proven readable on this title. sdk+0x600000 was swept word-by-word without
# a fault; main stays well inside the image (the loader reads main+0x4DC6088).
# Widen one step at a time: an unmapped read aborts the game and cannot be caught.
SAFE_SDK_SPAN = 0x600000
SAFE_MAIN_SPAN = 0x100000


# --------------------------------------------------------------------------- #
# control transport
# --------------------------------------------------------------------------- #
class Console:
    def __init__(self, host, port=PORT, timeout=120):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(timeout)
        self.counter = 0

    def request(self, **payload):
        self.counter += 1
        payload["id"] = self.counter
        body = json.dumps(payload).encode()
        self.sock.sendall(struct.pack("<I", len(body)) + body)
        head = self._recv_exact(4)
        size = struct.unpack("<I", head)[0]
        reply = json.loads(self._recv_exact(size).decode())
        if not reply.get("ok", True):
            raise RuntimeError(reply.get("error", "console error") + "\n" + reply.get("stack", ""))
        return reply

    def _recv_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise EOFError("console closed the control connection")
            out += chunk
        return out

    def close(self):
        # Close only the socket; the payload keeps listening for the next command.
        # Use `stop` to deliberately unload it.
        self.sock.close()


def load_signatures():
    with open(SIGNATURES, encoding="utf-8") as fh:
        return json.load(fh)


def load_svc_table():
    with open(SVC_RELOCATION, encoding="utf-8") as fh:
        return json.load(fh)


def send_svc_table(con):
    """Load the decoded syscall stub entries into the payload (idempotent)."""
    try:
        table = load_svc_table()
    except FileNotFoundError:
        return None
    return con.request(command="setSvcTable", entries=table["entries"])


def send_sdk_table(con):
    """Load the relocated SDK library functions into the payload (idempotent)."""
    try:
        with open(SDK_RELOCATION, encoding="utf-8") as fh:
            table = json.load(fh)
    except FileNotFoundError:
        return None
    return con.request(command="setFunctions", entries=table["functions"])


def timestamp():
    return time.strftime("%Y%m%d-%H%M%S")


def ensure_runs():
    os.makedirs(RUNS, exist_ok=True)
    return RUNS


def save_json(name, value):
    ensure_runs()
    path = os.path.join(RUNS, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)
    return path


# --------------------------------------------------------------------------- #
# offline scanner (same rules as the payload, so a dump can be re-analysed)
# --------------------------------------------------------------------------- #
def _word(buf, off):
    return int.from_bytes(buf[off:off + 4], "little")


def _equals(buf, off, sig):
    return buf[off:off + len(sig)] == sig


def offline_scan(buf, table, base=0):
    exact = {}
    anchor = {}
    for f in table["functions"]:
        sig = bytes.fromhex(f["bytes"])
        if len(sig) >= 4:
            exact.setdefault(_word(sig, 0), []).append((f, sig, 0))
        asig = bytes.fromhex(f.get("anchorBytes") or f["bytes"])
        if len(asig) >= 4:
            anchor.setdefault(_word(asig, 0), []).append((f, asig, int(f.get("anchorOffset", 0))))

    rows = {f["name"]: {"name": f["name"], "milestone": f.get("milestone", "M1"),
                        "exact": [], "anchor": []} for f in table["functions"]}
    for off in range(0, max(0, len(buf) - 4), 4):
        w = _word(buf, off)
        for f, sig, _ in exact.get(w, ()):
            if len(rows[f["name"]]["exact"]) < 8 and _equals(buf, off, sig):
                rows[f["name"]]["exact"].append(base + off)
        for f, sig, aoff in anchor.get(w, ()):
            if len(rows[f["name"]]["anchor"]) < 8 and _equals(buf, off, sig):
                rows[f["name"]]["anchor"].append({"address": base + off, "base": base + off - aoff,
                                                  "matched": len(sig)})

    for row in rows.values():
        if row["exact"]:
            row["best"] = {"kind": "exact", "address": row["exact"][0], "sdkOffset": row["exact"][0] - base}
        elif row["anchor"]:
            row["best"] = {"kind": "anchor", **row["anchor"][0], "sdkOffset": row["anchor"][0]["base"] - base}

    svc = []
    for entry in table["svc"]:
        found1, found2 = [], []
        for off in range(0, max(0, len(buf) - 8), 4):
            w = _word(buf, off)
            # AArch64 SVC: base 0xD4000001, imm16 in bits [20:5].
            if (w & 0xFFE0001F) != 0xD4000001:
                continue
            imm = (w >> 5) & 0xFFFF
            if entry.get("s1") is not None and imm == entry["s1"]:
                found1.append(base + off)
            if entry.get("s2") is not None and imm == entry["s2"]:
                found2.append(base + off)
        svc.append({"name": entry["name"], "switch1": entry.get("s1"), "switch2": entry.get("s2"),
                    "foundSwitch1": found1[:8], "foundSwitch2": found2[:8]})
    return {"base": base, "functions": list(rows.values()), "svc": svc}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def summarize(report):
    print("  SDK relocation: %d/%d functions localised" %
          (report.get("summary", {}).get("found", 0), len(report["functions"])))
    for row in report["functions"]:
        best = row.get("best")
        if best:
            kind = best["kind"]
            mark = "=" if kind == "exact" else "~"
            detail = "0x%x (sdk+0x%x)" % (best["address"], best["sdkOffset"] & 0xFFFFFFFFFFFFFFFF)
            if kind == "anchor":
                detail += " anchor-only, %d bytes" % best["matched"]
            print("  %s %-22s %s" % (mark, row["name"], detail))
        else:
            print("  ? %-22s not found" % row["name"])
    print("  svc wrappers:")
    for row in report.get("svc", []):
        if row["foundSwitch1"] or row["foundSwitch2"]:
            print("    %-32s s1=%s s2=%s" % (row["name"],
                  ["0x%x" % a for a in row["foundSwitch1"]],
                  ["0x%x" % a for a in row["foundSwitch2"]]))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_scan(host, base=None, span=None, force=False):
    table = load_signatures()
    con = Console(host, timeout=300)
    try:
        con.request(command="setSignatures", functions=table["functions"], svc=table["svc"])
        request = {"command": "scan"}
        if base is not None:
            request["base"] = base
        if span is not None:
            request["span"] = span
        if force:
            request["force"] = True
        report = con.request(**request)
    finally:
        con.close()
    report["signaturesFrom"] = table["generatedFrom"]
    report["capturedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    report["host"] = host
    path = save_json("%s-relocate-%s.json" % (host, timestamp()), report)
    print("wrote %s" % path)
    summarize(report)
    return report


def cmd_dump(host, address, length, out=None):
    con = Console(host, timeout=300)
    ensure_runs()
    data = bytearray()
    try:
        done = 0
        while done < length:
            n = min(CHUNK, length - done)
            reply = con.request(command="read", address=address + done, length=n)
            data += bytes.fromhex(reply["hex"])
            done += n
            print("\r  dumped 0x%x/0x%x" % (done, length), end="", flush=True)
    finally:
        con.close()
    print()
    path = out or os.path.join(RUNS, "%s-dump-0x%x-%s.bin" % (host, address, timestamp()))
    with open(path, "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes)" % (path, len(data)))
    return path


def cmd_dump_svc(host, offset=0x168000, length=0x2000):
    """Dump the syscall stub table (sdk_base + offset), which is small and proven safe."""
    con = Console(host, timeout=30)
    try:
        status = con.request(command="status")
    finally:
        con.close()
    address = status["sdkBase"] + offset
    print("  sdk_base = 0x%x, stub table @ +0x%x = 0x%x" % (status["sdkBase"], offset, address))
    return cmd_dump(host, address, length, os.path.join(RUNS, "%s-svctable-%s.bin" % (host, timestamp())))


def cmd_dump_sdk(host, span=0x600000):
    con = Console(host, timeout=30)
    try:
        status = con.request(command="status")
    finally:
        con.close()
    print("  sdk_base = 0x%x, main_base = 0x%x" % (status["sdkBase"], status["mainBase"]))
    return cmd_dump(host, status["sdkBase"], span)


def cmd_load(host, console_port=9026, settle=4.0):
    """Send payload/relocate.js to the Remote JS Loader; it stays resident on :9028."""
    code_path = os.path.join(HERE, "payload", "relocate.js")
    with open(code_path, encoding="utf-8") as fh:
        code = fh.read()
    probe = socket.create_connection((host, console_port), timeout=8)
    probe.sendall(code.encode())
    probe.shutdown(socket.SHUT_WR)
    probe.settimeout(settle)
    seen = b""
    try:
        while True:
            chunk = probe.recv(65536)
            if not chunk:
                break
            seen += chunk
    except socket.timeout:
        pass
    probe.close()
    print(seen.decode("utf-8", "replace")[-4000:])
    print("relocate payload sent; control should be live on :%d" % PORT)
    return seen.decode("utf-8", "replace")


def cmd_regions(host):
    con = Console(host, timeout=300)
    try:
        send_svc_table(con)
        reply = con.request(command="regions")
    finally:
        con.close()
    path = save_json("%s-regions-%s.json" % (host, timestamp()), reply)
    for r in reply["regions"]:
        print("  0x%012x size 0x%-9x type=%-3s perm=%s" % (r["base"], r["size"], r["type"], r["perm"]))
    print("wrote %s" % path)
    return reply


def cmd_query(host, address):
    con = Console(host)
    try:
        send_svc_table(con)
        reply = con.request(command="query", address=address)
    finally:
        con.close()
    print(json.dumps(reply, indent=2))
    return reply


def cmd_info(host, type_id, handle=0, argument=0):
    con = Console(host)
    try:
        send_svc_table(con)
        reply = con.request(command="info", type=type_id, handle=handle, argument=argument)
    finally:
        con.close()
    print(json.dumps(reply, indent=2))
    return reply


# getInfo type 26: IsSvcPermitted. The ids the port cares about.
IS_SVC_PERMITTED = 26
PROBE_SVC = [0x4B, 0x4C, 0x73, 0x74, 0x77, 0x78, 0x60, 0x21, 0x22, 0x23, 0x1F, 0x29]
REGION_INFO_TYPES = [2, 3, 4, 5, 6, 7]  # alias, heap, total, used
# Process-info getInfo calls need the current-process pseudo-handle; 0 is rejected.
PROCESS_HANDLE = 0xFFFF8001


def parse_region(memory_info_hex):
    """This SDK's queryMemory wrapper writes {u64 base; u64 size; ...}."""
    raw = bytes.fromhex(memory_info_hex)
    if len(raw) < 16:
        return None, None
    return int.from_bytes(raw[0:8], "little"), int.from_bytes(raw[8:16], "little")


def cmd_env(host):
    """Capture the P1-P5 environment report: bases, regions, SVC permission, timing."""
    con = Console(host)
    report = {"host": host, "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        print("  status...")
        report["status"] = con.request(command="status")
        print("  svc table...")
        report["svcTable"] = send_svc_table(con)
        main_base = report["status"]["mainBase"]
        sdk_base = report["status"]["sdkBase"]
        report["regions"] = []
        for address in [main_base, sdk_base, main_base + 0x1AF7FA0, main_base + 0x4DC6088]:
            try:
                row = con.request(command="query", address=address)
                row["base"], row["size"] = parse_region(row.get("memoryInfoHex", ""))
                report["regions"].append(row)
            except Exception as exc:  # noqa: BLE001
                report["regions"].append({"address": address, "error": str(exc)})
        print("  getInfo...")
        report["getInfo"] = []
        for type_id in REGION_INFO_TYPES:
            try:
                report["getInfo"].append(con.request(command="info", type=type_id, handle=PROCESS_HANDLE, argument=0))
            except Exception as exc:  # noqa: BLE001
                report["getInfo"].append({"type": type_id, "error": str(exc)})
        print("  IsSvcPermitted...")
        report["svcPermitted"] = []
        for svc_id in PROBE_SVC:
            try:
                reply = con.request(command="info", type=IS_SVC_PERMITTED, handle=PROCESS_HANDLE, argument=svc_id)
                report["svcPermitted"].append({"svc": svc_id, "result": reply["result"], "permitted": reply["value"]})
            except Exception as exc:  # noqa: BLE001
                report["svcPermitted"].append({"svc": svc_id, "error": str(exc)})
    finally:
        con.close()
    path = save_json("%s-env-%s.json" % (host, timestamp()), report)
    print("wrote %s" % path)
    print("  main_base = 0x%x  sdk_base = 0x%x" % (main_base, sdk_base))
    for row in report["regions"]:
        if "size" in row:
            print("  region @0x%x base=0x%x size=0x%x" % (row["address"], row["base"] or 0, row["size"] or 0))
    for row in report["getInfo"]:
        if "result" in row:
            print("  getInfo type %d result=%s value=%s" % (row["type"], row["result"], row["value"]))
    for row in report["svcPermitted"]:
        print("  svc 0x%02x result=%s permitted=%s" % (row["svc"], row.get("result"), row.get("permitted", row.get("error"))))
    return report


def cmd_svcindex(host, base, span=SAFE_SDK_SPAN, force=False):
    con = Console(host, timeout=300)
    try:
        reply = con.request(command="svcindex", base=base, span=span, **({"force": True} if force else {}))
    finally:
        con.close()
    print("  base=0x%x span=0x%x -> %d svc instructions" % (base, span, reply["total"]))
    for imm, count in sorted(reply["byImm"].items(), key=lambda kv: -kv[1])[:40]:
        print("    svc #0x%02x  x%d" % (int(imm), count))
    path = save_json("%s-svcindex-0x%x-%s.json" % (host, base, timestamp()), reply)
    print("wrote %s" % path)
    return reply


def cmd_bytescan(host, base, span, pattern):
    con = Console(host, timeout=300)
    try:
        reply = con.request(command="bytescan", base=base, span=span, hex=pattern)
    finally:
        con.close()
    print("  hits: %s" % [hex(a) for a in reply["hits"]])
    return reply


def cmd_diag(host, sdk_span=SAFE_SDK_SPAN, main_span=SAFE_MAIN_SPAN):
    """One report: bases, readability, and every SVC immediate in bounded regions.

    Spans are deliberately small. Widen them one step at a time; an unmapped read
    aborts the game process and cannot be caught.
    """
    con = Console(host, timeout=300)
    report = {"host": host, "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps": []}
    try:
        print("  status...")
        status = con.request(command="status")
        report["status"] = status
        main_base, sdk_base = status["mainBase"], status["sdkBase"]
        for label, address, span in [("main", main_base, main_span), ("sdk", sdk_base, sdk_span)]:
            print("  read %s @0x%x ..." % (label, address))
            try:
                head = con.request(command="read", address=address, length=64)
                report["steps"].append({"step": "read-" + label, "address": address, "hex": head["hex"]})
            except Exception as exc:  # noqa: BLE001
                report["steps"].append({"step": "read-" + label, "address": address, "error": str(exc)})
            print("  svcindex %s span=0x%x ..." % (label, span))
            try:
                index = con.request(command="svcindex", base=address, span=span)
                report["steps"].append({"step": "svcindex-" + label, "address": address, "span": span,
                                        "total": index["total"], "byImm": index["byImm"],
                                        "instructions": index["instructions"]})
            except Exception as exc:  # noqa: BLE001
                report["steps"].append({"step": "svcindex-" + label, "address": address, "span": span,
                                        "error": str(exc)})
    finally:
        con.close()
    path = save_json("%s-diag-%s.json" % (host, timestamp()), report)
    print("wrote %s" % path)
    for step in report["steps"]:
        if step["step"].startswith("read"):
            print("  %s @0x%x: %s" % (step["step"], step["address"], step.get("hex", step.get("error", ""))[:48]))
        else:
            print("  %s span=0x%x: %s" % (step["step"], step.get("span", 0),
                  ("%d svc instructions" % step["total"]) if "total" in step else step.get("error", "")))
    return report


def cmd_services(host):
    """P4: probe service reachability. Denials return errors, not aborts."""
    con = Console(host, timeout=120)
    try:
        send_svc_table(con)
        reply = con.request(command="serviceProbe")
    finally:
        con.close()
    path = save_json("%s-services-%s.json" % (host, timestamp()), reply)
    for row in reply.get("services", []):
        print("  %-10s opened=%-5s result=%s handle=%s" %
              (row["name"], row["opened"], row["resultHex"], row["handle"]))
    print("wrote %s" % path)
    return reply


def cmd_getservice(host):
    """P4 via the SDK getService path (the raw named-port SVC is refused here)."""
    con = Console(host, timeout=120)
    try:
        send_svc_table(con)
        send_sdk_table(con)
        reply = con.request(command="getServiceProbe")
    finally:
        con.close()
    path = save_json("%s-getservice-%s.json" % (host, timestamp()), reply)
    for row in reply.get("services", []):
        print("  %-10s opened=%-5s result=%s handle=%s" %
              (row["name"], row["opened"], row["resultHex"], row["handle"]))
    print("wrote %s" % path)
    return reply


def cmd_codemem(host, size=0x1000):
    """P3: probe CreateCodeMemory directly since IsSvcPermitted is unsupported."""
    con = Console(host, timeout=60)
    try:
        send_svc_table(con)
        reply = con.request(command="codeMemoryProbe", size=size)
    finally:
        con.close()
    path = save_json("%s-codemem-%s.json" % (host, timestamp()), reply)
    print("  CreateCodeMemory: result=%s handle=%s permitted=%s" %
          (reply.get("resultHex"), reply.get("handle"), reply.get("permitted")))
    print("wrote %s" % path)
    return reply


MOUNT_NAMES = ["save\x00", "tmp\x00", "SdCard\x00", "GameCard\x00", "SaveData\x00",
               "Content\x00", "Host\x00", "Nand\x00", "Registered\x00", "System\x00",
               "User\x00", "Prod\x00", "Safe\x00", "Aoc\x00", "Rom\x00"]


def cmd_mounts(host):
    """Enumerate regions and scan only readable+writable ones for mount names.

    A region with perm bit 0 unset is never read: reading it faults the process.
    """
    con = Console(host, timeout=600)
    try:
        send_svc_table(con)
        status = con.request(command="status")
        sdk, main = status["sdkBase"], status["mainBase"]
        regions = con.request(command="regions")["regions"]
        # Readable+writable only. Include small RW regions anywhere (SDK data can be
        # a separate mapping), plus larger ones near main/sdk.
        cands = [r for r in regions
                 if (int(r["perm"]) & 3) == 3
                 and (r["size"] <= 0x200000
                      or (r["size"] <= 0x2000000
                          and (abs(r["base"] - sdk) <= 0x8000000 or abs(r["base"] - main) <= 0x8000000)))]
        total = sum(r["size"] for r in cands)
        print("  %d regions, %d candidates, %.1f MB to scan" % (len(regions), len(cands), total / 1048576.0))
        print("  %d regions, %d readable+writable candidates" % (len(regions), len(cands)))
        for r in cands:
            print("    cand base=0x%x size=0x%x perm=%s type=%s" % (r["base"], r["size"], r["perm"], r["type"]))
        reply = con.request(command="scanRegions",
                            regions=[{"base": r["base"], "size": r["size"]} for r in cands],
                            names=MOUNT_NAMES, window=0x60)
    finally:
        con.close()
    path = save_json("%s-mounts-%s.json" % (host, timestamp()), reply)
    hits = 0
    accessor = None
    for block in reply.get("results", []):
        for hit in block["hits"]:
            hits += 1
            print("    0x%x +0x%x  %s" % (block["base"], hit["offset"], hit["hex"][:48]))
            # An accessor has the mount name at +0 and a high implementation pointer
            # at +0x10; the name-registry entries have a small tag instead.
            window = bytes.fromhex(hit["hex"])
            if len(window) >= 0x18:
                name = window[:8].split(b"\x00")[0]
                # An accessor is {name[16]; ...; implementation@+0x10}; the name field
                # continues with zeros. Name-registry entries put a 0x2251 tag at +8.
                pad = int.from_bytes(window[8:16], "little")
                impl = int.from_bytes(window[0x10:0x18], "little")
                if (name in (b"save", b"tmp") and pad == 0 and impl > 0x100000000
                        and accessor is None):
                    accessor = block["base"] + hit["offset"] - 24
    if accessor is not None:
        target = os.path.join(HERE, "..", "harness", "mount-accessor.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("0x%x\n" % accessor)
        print("  save accessor = 0x%x -> %s" % (accessor, target))
    else:
        print("  (no accessor identified; set harness/mount-accessor.txt manually)")
    print("  %d name hits; wrote %s" % (hits, path))
    return reply


def cmd_stop(host):
    """Unload the resident payload so a new one can be sent on :9026."""
    con = Console(host, timeout=30)
    try:
        reply = con.request(command="exit")
        print(json.dumps(reply, indent=2))
    finally:
        con.close()
    return reply


def cmd_status(host):
    con = Console(host)
    try:
        reply = con.request(command="status")
    finally:
        con.close()
    print(json.dumps(reply, indent=2))
    return reply


def cmd_svcscan(host):
    table = load_signatures()
    con = Console(host)
    try:
        reply = con.request(command="svcscan")
    finally:
        con.close()
    for row in reply["svc"]:
        print("  %-32s s1=%s s2=%s" % (row["name"],
              ["0x%x" % a for a in row["foundSwitch1"]],
              ["0x%x" % a for a in row["foundSwitch2"]]))
    path = save_json("%s-svc-%s.json" % (host, timestamp()), reply)
    print("wrote %s" % path)
    return reply


def cmd_read(host, address, length):
    con = Console(host)
    try:
        reply = con.request(command="read", address=address, length=length)
    finally:
        con.close()
    print(reply["hex"])
    return reply


def cmd_call(host, address, args):
    con = Console(host)
    try:
        reply = con.request(command="call", address=address, args=args)
    finally:
        con.close()
    print(json.dumps(reply, indent=2))
    return reply


def cmd_svccall(host, name, args):
    con = Console(host)
    try:
        send_svc_table(con)
        reply = con.request(command="svccall", name=name, args=args)
    finally:
        con.close()
    print(json.dumps(reply, indent=2))
    return reply


def cmd_offline(path, base=0):
    table = load_signatures()
    with open(path, "rb") as fh:
        buf = fh.read()
    report = offline_scan(buf, table, base=base)
    report.update({"summary": {"found": sum(1 for r in report["functions"] if r.get("best")),
                               "total": len(report["functions"])}, "offline": path})
    out = save_json("offline-%s-%s.json" % (os.path.basename(path), timestamp()), report)
    print("wrote %s" % out)
    summarize(report)
    return report


def usage():
    print(__doc__)


def main():
    argv = sys.argv[1:]
    if not argv:
        usage()
        sys.exit(1)

    if argv[0] == "--offline":
        base = int(argv[2], 0) if len(argv) > 2 else 0
        cmd_offline(argv[1], base)
        return

    host = argv[0]
    if len(argv) == 1:
        cmd_status(host)
        return
    command = argv[1]
    rest = argv[2:]

    if command == "load":
        cmd_load(host)
    elif command == "stop":
        cmd_stop(host)
    elif command == "status":
        cmd_status(host)
    elif command == "scan":
        base = int(rest[0], 0) if rest else None
        span = int(rest[1], 0) if len(rest) > 1 else None
        cmd_scan(host, base, span)
    elif command == "dump":
        if len(rest) < 2:
            raise SystemExit("dump <address> <length> [out]")
        cmd_dump(host, int(rest[0], 0), int(rest[1], 0), rest[2] if len(rest) > 2 else None)
    elif command == "dump-sdk":
        cmd_dump_sdk(host, int(rest[0], 0) if rest else SAFE_SDK_SPAN)
    elif command == "dump-svc":
        cmd_dump_svc(host, int(rest[0], 0) if rest else 0x168000,
                     int(rest[1], 0) if len(rest) > 1 else 0x2000)
    elif command == "svcscan":
        cmd_svcscan(host)
    elif command == "query":
        cmd_query(host, int(rest[0], 0))
    elif command == "info":
        cmd_info(host, int(rest[0], 0), int(rest[1], 0) if len(rest) > 1 else 0,
                 int(rest[2], 0) if len(rest) > 2 else 0)
    elif command == "env":
        cmd_env(host)
    elif command == "svcindex":
        cmd_svcindex(host, int(rest[0], 0), int(rest[1], 0) if len(rest) > 1 else SAFE_SDK_SPAN,
                     "force" in rest)
    elif command == "bytescan":
        cmd_bytescan(host, int(rest[0], 0), int(rest[1], 0), rest[2])
    elif command == "diag":
        cmd_diag(host, int(rest[0], 0) if rest else SAFE_SDK_SPAN,
                 int(rest[1], 0) if len(rest) > 1 else SAFE_MAIN_SPAN)
    elif command == "codemem":
        cmd_codemem(host, int(rest[0], 0) if rest else 0x1000)
    elif command == "mounts":
        cmd_mounts(host)
    elif command == "regions":
        cmd_regions(host)
    elif command == "services":
        cmd_services(host)
    elif command == "getservice":
        cmd_getservice(host)
    elif command == "read":
        cmd_read(host, int(rest[0], 0), int(rest[1], 0))
    elif command == "call":
        cmd_call(host, int(rest[0], 0), [int(a, 0) for a in rest[1:]])
    elif command == "svccall":
        cmd_svccall(host, rest[0], [int(a, 0) for a in rest[1:]])
    else:
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
