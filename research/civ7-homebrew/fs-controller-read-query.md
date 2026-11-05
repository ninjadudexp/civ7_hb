# Question for FS/memory-controller team: data read path integrity

## Context

We are implementing an automated backup/restore tool that performs bulk data reads against the
file-system memory controller. To validate the data we are reading, we pre-fill the destination
buffers with a known test pattern before issuing the read command, and verify the content
returned matches the expected file data on disk.

## The issue we are observing

Under certain conditions, the data returned by the controller read path does not fully overwrite
our pre-fill pattern in the destination buffer:

1.  **Partial overwrites:** Sometimes the read operation returns "success," but the last few
    bytes of the destination buffer still contain the pre-fill pattern instead of the actual file
    data.
2.  **Inconsistent alignment:** When we request a read of a specific size (e.g., 4096 bytes), the
    controller sometimes seems to write to an internal buffer of a different alignment or size,
    causing the data to appear shifted or truncated in our destination buffer.
3.  **Caching artifacts:** We suspect there is an intermediate caching layer. If we read the same
    block twice in quick succession, the second read sometimes returns the *stale* internal cache
    data instead of the fresh data, even if the source file has changed between the two calls.

## Our question

What is the officially supported or expected behavior for the controller's read path regarding
**buffer alignment and cache coherency**?

Specifically:

- Is there a specific alignment or size requirement for the destination buffer to ensure a
  complete, atomic overwrite of the buffer on every read?
- Is there a specific command or sequence we should issue to invalidate or flush the
  controller's internal cache before a read to ensure we always get the freshest data from the
  disk?
- Are there known limitations on the maximum size of a single read transaction that could lead to
  the partial overwrite behavior described above?

We want to ensure our tool is not falling into an edge case in the controller's caching logic.

## Technical details

- Controller interface: `fsp-srv` (standard file system service interface).
- Read command: Standard IStorage read command with the offset and size provided as 64-bit
  integers.
- Destination buffer: User-allocated, page-aligned memory.
- Environment: Switch 2 development environment, standard SDK libraries.
