"""Cross-check harness/signed_cache_plan.py against the reference .cjs.

Runs the same (target, storageSize, cycle) triples through both the node
reference (research/ns2jb/engine/switch2/memory/signed-cache-plan.cjs) and
the Python port, then compares header bytes, readOffset, base, entryOffset,
and addressBias byte-for-byte. Also checks argument-validation errors agree.

Usage: python signed_cache_plan_selftest.py
Exit 0 = identical outputs; nonzero = mismatch (prints the diff).
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from signed_cache_plan import plan  # noqa: E402

REF = os.path.normpath(os.path.join(
    HERE, "..", "..", "ns2jb", "engine", "switch2", "memory",
    "signed-cache-plan.cjs"))

# Triples: (target, storageSize, cycle). Small storage sizes keep the
# prefix search short; the last one uses Civ's measured 4.975 GB RomFS.
TRIPLES = [
    (0x123456789, 0x140000, 0x28000),
    (0x1ABCDEF23, 0xA0000, 0x28000),
    (0x3456789AB, 0xA00000, 0x500000),
    (0x2FFFFFFFF, 0x140000, 0x28000),
    (0x777777777, 4975000000, 0x28000),
    (0x777777777, 4975000000, 0x500000),
]

BAD_ARGS = [
    (0x1000, 0x140000, 0x28000),      # target below 2**32
    (0x123456789, 0x1000, 0x28000),   # storage below 0x4000
    (0x123456789, 0x140000, 0x10000), # unsupported cycle
]

NODE_SCRIPT = """
const {plan} = require(process.argv[2]);
const triples = JSON.parse(process.argv[3]);
const bad = JSON.parse(process.argv[4]);
for (const [t, s, c] of triples) {
  try {
    const p = plan(t, s, c);
    console.log(JSON.stringify({ok: true, hex: p.bytes.toString('hex'),
      readOffset: p.readOffset, base: p.base, entryOffset: p.entryOffset,
      addressBias: p.addressBias, target: p.target, cycle: p.cycle,
      block: p.block, encoding: p.encoding}));
  } catch (e) {
    console.log(JSON.stringify({ok: false, error: e.message}));
  }
}
for (const [t, s, c] of bad) {
  try { plan(t, s, c); console.log(JSON.stringify({ok: true})); }
  catch (e) { console.log(JSON.stringify({ok: false, error: e.message})); }
}
"""


def reference_run():
    script = os.path.join(HERE, "_signed_plan_ref_probe.cjs")
    with open(script, "w") as f:
        f.write(NODE_SCRIPT)
    try:
        out = subprocess.run(
            ["node", script, REF, json.dumps(TRIPLES), json.dumps(BAD_ARGS)],
            capture_output=True, text=True, timeout=180, check=True)
    finally:
        os.remove(script)
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    return [json.loads(l) for l in lines]


def py_plan(t, s, c):
    p = plan(t, s, c)
    return {"ok": True, "hex": p["bytes"].hex(), "readOffset": p["readOffset"],
            "base": p["base"], "entryOffset": p["entryOffset"],
            "addressBias": p["addressBias"], "target": p["target"],
            "cycle": p["cycle"], "block": p["block"], "encoding": p["encoding"]}


def main():
    ref = reference_run()
    failures = 0
    for i, (t, s, c) in enumerate(TRIPLES):
        try:
            mine = py_plan(t, s, c)
        except Exception as exc:
            mine = {"ok": False, "error": str(exc)}
        theirs = ref[i]
        if mine != theirs:
            failures += 1
            print("MISMATCH triple %d %s" % ((t, s, c), ))
            print("  py : %s" % json.dumps(mine))
            print("  cjs: %s" % json.dumps(theirs))
        else:
            print("ok triple %d: target=0x%x storage=0x%x cycle=0x%x "
                  "readOffset=%d base=%s" %
                  (i, t, s, c, mine["readOffset"], mine["base"]))
    for i, (t, s, c) in enumerate(BAD_ARGS):
        try:
            plan(t, s, c)
            mine = {"ok": True}
        except Exception as exc:
            mine = {"ok": False, "error": str(exc)}
        theirs = ref[len(TRIPLES) + i]
        if mine["ok"] != theirs["ok"]:
            failures += 1
            print("MISMATCH bad-arg %d %s: py %s / cjs %s" %
                  (i, (t, s, c), json.dumps(mine), json.dumps(theirs)))
        else:
            print("ok bad-arg %d: %s" % (i, theirs.get("error", "accepted")))
    if failures:
        print("\n%d FAILURE(S)" % failures)
        return 1
    print("\nAll %d triples and %d bad-arg cases match the reference."
          % (len(TRIPLES), len(BAD_ARGS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
