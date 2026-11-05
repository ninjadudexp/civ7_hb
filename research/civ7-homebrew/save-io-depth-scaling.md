# Save I/O: deep-path access latency

## The question

Does the FS service cache per-directory lookup state across calls, and is there a supported way
to pre-populate that cache for a deep directory chain, so that repeated accesses into the same
nested tree don't re-walk every component on each call?

## What we're seeing

Benchmarking save-data access patterns for our title's archival feature on Switch 2
(`fsp-srv`, save filesystem):

- Access latency into a nested directory tree scales roughly linearly with the number of
  directory components in the path. A file 1 level deep resolves quickly; the same file 6 levels
  deep takes several times longer per operation, consistently, across repeated calls.
- The cost appears per-operation, not just on first access — repeated `GetEntryType`/read calls
  into the same deep path pay the walk every time within a session.
- Component name length also seems to contribute, but depth dominates.

## What we'd like to know

1. Is the per-component walk cost inherent to the service's path normalization (i.e. every call
   re-resolves the full string), or is there a directory-handle cache that we're failing to hit?
2. If there is a cache: what populates it? Holding an open `IDirectory` handle on each ancestor?
   A specific order of calls? Something in the session setup?
3. Is there a documented or known cap where the resolver behaves differently (e.g. per-path
   buffer limits around `0x300` bytes) that changes the lookup path entirely — we noticed one
   caller path accepts long paths for create/verify but a different caller path seems to stop
   resolving them around that size.

The goal is legitimate: we'd like deep archival trees to not cost O(depth) per access, and we're
hitting a wall where our deepest trees (6+ levels) are slow enough to matter for the title's
autosave window. Happy to share our measurement harness if useful.
