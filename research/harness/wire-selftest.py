# Byte-parity selftest for the Python wire port in harness/host.py.
# Builds the same frames as wire-selftest.cjs (which uses ns2jb's own codecs) and
# writes a comparable dump. Run both, then wire-diff.py compares sha256 per frame.
import json
import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from host import packet, domain, control, close_object, context_bytes, path_packet, parse_reply, u32, u64

A, B, C = 0x12340000, 0x10000000, 0x24000000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def norm(parsed):
    return {"result": parsed["result"], "canonical": parsed["canonical"], "copies": parsed["copies"],
            "moves": parsed["moves"], "objects": parsed["objects"], "dataHex": parsed["data"][:16].hex()}


def craft_reply(objects=0, data_size=0, copies=(), domain_reply=False):
    buf = bytearray(256)
    struct.pack_into("<I", buf, 0, 4)
    at = 8
    if copies:
        struct.pack_into("<I", buf, 4, 0x80000000)
        struct.pack_into("<I", buf, 8, len(copies) << 1)
        at += 4
        for handle in copies:
            struct.pack_into("<I", buf, at, handle)
            at += 4
    at = (at + 15) & ~15
    if domain_reply:
        struct.pack_into("<I", buf, at, objects)
        struct.pack_into("<I", buf, at + 8, 0)
        buf[at + 16:at + 20] = b"SFCO"
        struct.pack_into("<I", buf, at + 24, 0)
        at += 32 + data_size
        for i in range(objects):
            struct.pack_into("<I", buf, at + 4 * i, 10 + i)
        return bytes(buf)
    buf[at:at + 4] = b"SFCO"
    struct.pack_into("<I", buf, at + 8, 0)
    return bytes(buf)


cases = []
def add(name, bytes_):
    cases.append({"name": name, "hex": bytes_.hex(), "sha": sha(bytes_)})

add("commit-request", packet(10, u64(0), send=[{"address": A, "size": 0x800}]))
add("hole-request", packet(0, u64(0) + u64(0x180000), receive=[{"address": B, "size": 0x180000, "mode": 1}]))
add("initialize", packet(1, u64(0), pid=True))
add("clone", packet(2))
add("copy-handle", packet(11, u32(7), copies=[7], send=[{"address": A, "size": 0x800}]))
add("domain-counters", domain(1, 1009))
add("domain-create", domain(1, 1200))
add("domain-control", control(0))
add("domain-copy-object", control(1, u32(3)))
add("domain-close-object", close_object(5))
add("domain-close-context", close_object(5, 3))
add("sequential-counters", context_bytes(domain(1, 1009), 9))
add("concurrent-counters", context_bytes(domain(1, 1009), 3))
add("path-query", path_packet(7, {"address": A, "size": 0x800}))
add("path-append", path_packet(1002, {"address": A, "size": 16}))
add("path-two-descriptors", path_packet(5, None, descriptors=[{"address": A, "size": 16}, {"address": C, "size": 0x7f0}]))
add("path-with-input", path_packet(0, {"address": A, "size": 0x300}, u64(0) + u64(1)))

replies = []
for spec in [
    {"name": "plain-reply", "objects": 0, "dataSize": 0},
    {"name": "reply-with-copies", "objects": 0, "dataSize": 0, "copies": [7, 8]},
    {"name": "domain-reply", "objects": 2, "dataSize": 16, "domainReply": True},
    {"name": "domain-reply-context", "objects": 1, "dataSize": 128, "copies": [9], "domainReply": True},
]:
    bytes_ = craft_reply(spec.get("objects", 0), spec.get("dataSize", 0), spec.get("copies", ()), spec.get("domainReply", False))
    replies.append({"name": spec["name"], "hex": bytes_.hex(), "sha": sha(bytes_),
                    "parsed": norm(parse_reply(bytes_, domain_reply=spec.get("domainReply", False), data_size=spec.get("dataSize", 0)))})

Path(__file__).with_name("selftest-py.json").write_text(json.dumps({"cases": cases, "replies": replies}, indent=2) + "\n")
print(json.dumps({"frames": len(cases), "replies": len(replies)}))
