# Civ VII relocation toolchain (M1)

Console-side SDK relocation and environment telemetry for the Civ VII → Switch 2
port, built to run on top of the Remote JS Loader (`../core.js`) and to leave every
result as a file the workstation (and the research agent) can read back.

```
relocation/
  signatures/nn-sdk-signatures.json   generated from ns2jb's verified bytes
  build-signatures.cjs                regenerates the signature table
  payload/relocate.js                 console-side, resident control server on :9028
  host/relocate_host.py               workstation driver + offline scanner
  selftest.py                         offline scanner test, no console needed
  runs/                               every report and dump lands here
```

## Why this exists

The game image's SDK is a different build from SmileBASIC's, so every offset in
`research/ns2jb` is a *hypothesis* for Civ VII. This tool turns each ns2jb offset
into a byte signature, finds it (or reports that it drifted) in the game image,
and records the result as JSON. It also captures the environment facts the port
needs: `queryMemory` regions and `getInfo`/`IsSvcPermitted`.

## 1. Offline checks (no console)

```sh
node build-signatures.cjs            # regenerate signatures/nn-sdk-signatures.json
python selftest.py                   # scanner self-test against a synthetic image
node --check payload/relocate.js     # payload syntax
```

Expected self-test output ends with `SELFTEST PASS`.

## 2. Load the payload on the console

With the RS Loader live on `:9026` (boot-persistent install, or a fresh chat-XSS):

```sh
python host/relocate_host.py <console-ip> load
```

That sends `payload/relocate.js` and the payload stays resident on `:9028`. The
loader evaluates one payload at a time; closing a control socket does **not** unload
the payload, so commands can be run repeatedly. To swap in an edited payload, run
`relocate_host.py <ip> stop` first (or restart the game), then `load` again.

When a payload misbehaves after it announces itself, run `diag` first: it reads the
first 64 bytes at each base (proving the region is readable) and histograms every
`svc #imm` in the region, which answers the numbering question before any by-id scan.

## 3. Capture telemetry

```sh
python host/relocate_host.py <console-ip> status        # bases
python host/relocate_host.py <console-ip> env           # P1-P5 environment report
python host/relocate_host.py <console-ip> scan          # M1 signature relocation
python host/relocate_host.py <console-ip> svcscan       # svc #imm; ret wrappers
python host/relocate_host.py <console-ip> dump-sdk      # pull 0x600000 of the SDK
python host/relocate_host.py --offline runs/<dump>.bin  # rescan a dump with no console
```

Every command writes to `runs/`:

| File | Contents |
| --- | --- |
| `<ip>-env-<ts>.json` | bases, memory regions, getInfo, IsSvcPermitted |
| `<ip>-relocate-<ts>.json` | per-function exact/anchor matches with sdk offsets |
| `<ip>-svc-<ts>.json` | svc wrapper candidates for Switch 1 and Switch 2 numbering |
| `<ip>-sdk-<ts>.bin` | raw SDK image dump for offline re-analysis |

## Reading a relocation report

`best.kind` is `exact` when the full ns2jb body matched, `anchor` when only the
short window matched (body drifted, function localised), and `null` when the
function was not found. `sdkOffset` is `address - sdk_base`. The host print uses
`=` for exact and `~` for anchor-only.

## Console commands (control protocol)

4-byte little-endian length, then UTF-8 JSON. Implemented in `payload/relocate.js`:

| command | fields | returns |
| --- | --- | --- |
| `status` | – | `mainBase`, `sdkBase`, `defaultSpan`, loaded count |
| `setSignatures` | `functions`, `svc` | counts (host sends the table) |
| `setSvcTable` | `entries` (`imm`→`entryOffset`) | decoded stub entries; used by `query`/`info`/`svccall` |
| `scan` | `base?`, `span?` | per-function matches + svc scan + summary |
| `svcscan` | `base?`, `span?` | svc candidates per id |
| `svcindex` | `base?`, `span?` | every `svc #imm` in the region, with a histogram of immediates |
| `bytescan` | `base?`, `span?`, `hex` | offsets where an arbitrary byte pattern occurs |
| `svccall` | `id`/`name`, `args` | int64 result of a scanned wrapper |
| `query` | `address` | raw MemoryInfo + pageInfo |
| `info` | `type`, `handle?`, `argument?` | int64 result + value |
| `read` | `address`, `length` | hex |
| `call` | `address`, `args` | int64 result of a raw call |
| `exit` | – | leaves the serve loop |

## SVC relocation (syscall stubs)

The SDK's `svc #imm` stubs are a contiguous table (this build: `sdk+0x168AD8`). Some
are `svc; ret`; others save an output pointer, store the returned handle, then `ret`.
So relocation cannot rely on `svc; ret` adjacency — it must use the declared **entry**.

```sh
python host/relocate_host.py <ip> dump-svc        # dumps sdk+0x168000, 8 KiB
# dump base is sdk+0x168000; --ref makes entryOffset relative to sdk_base
SDK_BASE=0x...   # from relocate_host.py <ip> status
python host/analyze_svc.py runs/<ip>-svctable-<ts>.bin $((SDK_BASE+0x168000)) \
    --ref $SDK_BASE runs/<ip>-svc-relocation.json
```

`analyze_svc.py` writes `imm`→`entryOffset` **relative to `sdk_base`** (that is what the
payload adds). The curated copy lives at `signatures/svc-relocation.json` and the host
sends it to the payload with `setSvcTable` before `query`, `info`, and `svccall`, so
calls are ASLR-independent. The payload also refuses to call an entry that does not
contain the expected `svc`, so a bad offset errors instead of aborting the game.

Confirmed on this title: Switch 1 numbering, 60 stubs, including `0x06` queryMemory
(`sdk+0x168b0c`), `0x1f` connectToNamedPort (`sdk+0x168c34`), `0x21` sendSyncRequest
(`sdk+0x168c4c`), `0x22` sendSyncRequestWithUserBuffer (`sdk+0x168c54`), `0x29` getInfo
(`sdk+0x168ca4`), `0x45` createEvent (`sdk+0x168ddc`).

## Memory safety (read this before widening a scan)

Reading an **unmapped** address aborts the game process, and JavaScript cannot catch
it. Every sweeping command (`scan`, `svcindex`, `bytescan`) is therefore capped at
`safe max 0x800000` by the payload, with a `force` override up to `0x8000000`.

- `sdk_base + 0x600000` is proven readable (swept word-by-word with no fault).
- Keep `main_base` spans well inside the image; `0x100000` is safe, `0x4000000` crashed.
- Widen one step at a time and expect a full game restart after any fault.
- `queryMemory` is the correct way to learn real region sizes, once its wrapper is
  located; until then, treat every span as a guess.

## Note on the existing harness

`../harness/console.js` scanned SVC wrappers with `0xD4000000 | id`, but AArch64
encodes SVC as `0xD4000001 | (imm << 5)`. That was fixed in both this toolchain and
the harness; without it, SVC relocation would silently find nothing.
