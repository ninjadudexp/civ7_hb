# FS question: save-data path resolution limit (long directory chains)

**Context:** porting a save-data archiving tool to the Switch 2 SDK (fsp-srv). We store deep
directory trees in save data and hit an inconsistency between how a long path is **created** and
how it is **looked up**, depending on which resolution form the caller uses. Any pointers to the
relevant constant or code path would save us a lot of guessing.

## What we observe

Same filesystem, same file, three operations:

| Operation | Path form | Result |
| --- | --- | --- |
| `CreateDirectory` × 43 + `CreateFile` (one per component, 32-char names) | direct path strings, each under `0x300` | all succeed (`0x0`) |
| `GetEntryType` on the **same `IFileSystem` handle** used for creation | one direct path string, total length **1440 chars** | succeeds, returns file (type 1) |
| `GetEntryType` submitted as a **split path** — the path supplied as multiple fragments (each fragment ≤ 0x800) that the service assembles during resolution | same 1440-char path | fails with **`0x202` (path not found)**, even though the file verifiably exists |

So creation and direct-string lookup both accept the 1440-char path, but the split/fragment
resolution form rejects it as missing. This smells like a length cap applied on one resolution
path and not the others — `0x300` (768) is the obvious suspect, since paths under it resolve fine
in our tests.

## Questions

1. Which limit governs the **fragment/split path resolution** form? Is it `FS_MAX_PATH` (0x300),
   a per-request path buffer, or something accumulated across fragments?
2. Is the cap applied at a different stage for the fragment form vs a direct path string
   (e.g. validation at submission vs normalization at resolution)?
3. Is there a documented or known way to make fragment resolution accept a path longer than the
   cap — e.g. constraints on fragment count, fragment sizes, ordering, or a terminator convention?
4. Same question for `DeleteDirectory`/`RenameDirectory` on long chains, if the resolution form
   is shared.

## Environment

- Switch 2 SDK-era fsp-srv, save data filesystem (`save:` mount).
- Paths are plain ASCII, no NULs, components ≤ 32 chars, total 1440 chars including separators.
- The failing lookups return promptly with `0x202` — not a timeout or a full-path-buffer error.

Happy to provide exact hexdumps of the failing request forms if useful. The practical goal is
storing long archive trees in save data and reading them back reliably through whatever path form
the service prefers.
