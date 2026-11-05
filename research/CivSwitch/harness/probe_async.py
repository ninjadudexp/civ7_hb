#!/usr/bin/env python3
"""probe_async.py - live probe of the SDK's sendAsyncRequestWithUserBuffer ABI.

The console gap is huge (0xCC01), so try several shapes and watch both the
returned Result and the out-word. The mounted FS is not needed; a plain
fsp-srv session + a benign counter packet is enough.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import host as H  # noqa: E402

host_ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.103.215"
h = H.Harness(host=host_ip)
c = h.console

print("status:", c.request("status"))
sess = c.request("open", name="fsp-srv")["handle"]
init = H.parse_reply(c.request("send", handle=sess, packet=H.packet(1, H.u64(0), pid=True).hex())["hex"])
print("session:", hex(sess), "init result:", init["result"])

out = c.request("alloc", pages=1)["address"]
buf = c.request("alloc", pages=1)["address"]
msg = H.packet(H.COUNT_OBJECTS)
c.request("write", address=buf, hex=msg.hex())
print("out:", hex(out), "buf:", hex(buf), "msg bytes:", len(msg))

cases = [
    ("out,sess,buf,0x1000", [out, sess, buf, 0x1000]),
    ("out,sess,buf,msglen", [out, sess, buf, len(msg)]),
    ("out,sess,buf,0x40", [out, sess, buf, 0x40]),
    ("sess,out,buf,0x1000", [sess, out, buf, 0x1000]),
    ("out,buf,sess,0x1000", [out, buf, sess, 0x1000]),
]
for label, args in cases:
    c.request("write", address=out, hex="00" * 8)
    r = c.request("svc", name="sendAsyncRequestWithUserBuffer", args=args)
    word = c.request("read", address=out, length=8)["hex"]
    print("%-22s result=%s (%#x)  out=%s" % (label, r["result"], r["result"] & 0xFFFFFFFF, word))
