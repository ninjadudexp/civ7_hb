#!/usr/bin/env python3
"""frames.py - Python port of ns2jb shared/frames.cjs `build`.

Lays out an original-SDK worker register frame: a dispatch table per call, a
stack slice per call, and result slots. Pure arithmetic, no console access.

The launcher is `create(slot, 0, sdk+longjmp, base)` (pthread_create-like), and
the frame's last call branches to `exit`.
"""

PAGE = 4096
STEP = 0x140
RESULT = 0x800
SDK_SIZE = 0x5E1000
SENTINEL = 0xFEDCBA9876543210
# Civ VII call4 (+0x359b88) is `ldr x8,[x8,#0x70]`; ns2jb's is `#0x68`. Everything else matches.
CALL4_FN_OFFSET = 0x70


def align(value):
    return (value + PAGE - 1) // PAGE * PAGE


def build(base, sdk, calls, code, fixed=False):
    """code: dict with offsets for loadTable, call4, saveResult64, exit (and memcpy)."""
    if base % PAGE or sdk % PAGE or not calls or len(calls) > (96 if fixed else 32768):
        raise ValueError("invalid worker address or call count")

    expanded, logical, patches = [], [], []
    for call in calls:
        if len(call["args"]) != 4:
            raise ValueError("workers require exactly four arguments")
        loads = []
        for index, argument in enumerate(call["args"]):
            if not isinstance(argument, dict):
                continue
            if fixed or list(argument.keys()) != ["u32"] or not isinstance(argument["u32"], int) \
                    or argument["u32"] <= 0 or argument["u32"] % 4:
                raise ValueError("dynamic arguments require an aligned u32 address")
            loads.append({"load": len(expanded), "argument": index})
            expanded.append({"function": sdk + code["memcpy"], "args": [0, argument["u32"], 4, 0]})
        target = len(expanded)
        patches.extend({**load, "target": target} for load in loads)
        logical.append(target)
        expanded.append({"function": call["function"],
                         "args": [0 if isinstance(a, dict) else a for a in call["args"]]})

    count = len(expanded)
    tables = 0x1000 if fixed else align(RESULT + count * 8)
    stack = 0x28000 if fixed else align(tables + count * 0x100) + 0x10000
    size = 0x30000 if fixed else align(stack + count * STEP + 0x4000)
    if size > 0x2000000 or RESULT + count * 8 > tables or tables + count * 0x100 > stack \
            or stack + count * STEP > size:
        raise ValueError("worker regions overlap or exceed the allocation")

    for patch in patches:
        argument, target = patch["argument"], patch["target"]
        if argument == 0:
            slot = (0x30 if target == 0 else stack + (target - 1) * STEP + 0xE8)
        else:
            slot = stack + target * STEP + [0, 0x10, 0x20, 0x28][argument]
        expanded[patch["load"]]["args"][0] = base + slot

    frame = bytearray(size)

    def put(offset, value):
        if not (0 <= value <= 0xFFFFFFFFFFFFFFFF):
            raise ValueError("worker word outside uint64")
        frame[offset:offset + 8] = value.to_bytes(8, "little")

    put(0x10, base + tables)
    put(0x30, expanded[0]["args"][0])
    put(0x58, sdk + code["loadTable"])
    put(0x68, base + stack)
    for index, call in enumerate(expanded):
        if not (call["function"] % 4 == 0 and sdk <= call["function"] < sdk + SDK_SIZE):
            raise ValueError("worker function outside the qualified SDK")
        table = tables + index * 0x100
        sp = stack + index * STEP
        put(table, base + table + 0x10)
        # call4 loads the callee from [x8 + CALL4_FN_OFFSET], where x8 = *(x21) = base+table+0x10.
        # With ns2jb's 0x68 that is table+0x78; Civ VII's call4 is `ldr x8,[x8,#0x70]` (identical
        # otherwise), so the callee must live at table+0x80. Using ns2jb's slot jumps to 0.
        put(table + 0x10 + CALL4_FN_OFFSET, call["function"])
        put(RESULT + index * 8, SENTINEL)
        put(sp + 8, sdk + code["call4"])
        for offset, argument in ((0x10, 1), (0x20, 2), (0x28, 3)):
            put(sp + offset, call["args"][argument])
        put(sp + 0xD8, sdk + code["saveResult64"])
        put(sp + 0x118, base + RESULT + index * 8)
        if index + 1 < count:
            put(sp + 0x108, base + tables + (index + 1) * 0x100)
            put(sp + 0xE8, expanded[index + 1]["args"][0])
            put(sp + 0x128, sdk + code["loadTable"])
        else:
            put(sp + 0x128, sdk + code["exit"])
    # The expanded call list, so a caller can locate a specific step (e.g. the IPC send) by its
    # function address instead of guessing from slot values. Dynamic arguments insert loader
    # calls, so the slot before a send is the loader's pointer return, not the preceding tick.
    functions = [call["function"] for call in expanded]
    proof = {"bytes": size, "logicalCalls": len(calls), "nativeCalls": count,
             "functions": functions,
             "resultOffsets": [RESULT + i * 8 for i in range(count)],
             "logicalResultIndices": logical,
             "resultOffset": RESULT + (count - 1) * 8,
             "stackOffset": stack, "tablesOffset": tables}
    return bytes(frame), proof


def send_frame(base, sdk, calls, code, fixed=False):    # placeholder for framing
    return build(base, sdk, calls, code, fixed)
