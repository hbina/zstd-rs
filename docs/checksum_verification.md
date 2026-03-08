# Checksum Verification

## Overview

ruzstd now verifies the XXH64 content checksum stored at the end of zstd frames (when the `hash` feature is enabled). If the checksum does not match, decoding returns `FrameDecoderError::ChecksumMismatch { expected, got }`.

## How Checksums Work in zstd (RFC 8878)

- Both RFC 8878 and the reference C implementation use **XXH64** for the content checksum.
- The hash is computed over the entire decompressed content in order.
- The lower 32 bits of the XXH64 digest are stored as a 4-byte little-endian field at the end of the frame (after the last block).
- The frame's `Content_Checksum_Flag` in the frame descriptor controls whether a checksum is present.

## Implementation

### Hash Timing (produce-time vs drain-time)

The key design decision: the hash must be updated at **produce time** (when decompressed bytes are generated), not at drain time (when bytes are read out of the decode buffer by the caller).

**Why:** Checksum verification runs immediately after decoding the last block, before any bytes are drained. If the hash were only updated on drain, it would reflect 0 bytes at verification time.

### Changes Made

**`decoding/decode_buffer.rs`:**
- `push(data)`: hashes `data` immediately when new literal bytes are added.
- `repeat(offset, match_length)`: hashes the source bytes (from the ring buffer) before copying them via `hash_from_slices()`.
- `repeat_in_chunks(...)`: same, hashes each chunk before copying.
- `repeat_from_dict(...)`: hashes the dictionary bytes when they are used for back-references.
- Removed hash updates from `drain_to()` and `drain()` (they were the old drain-time approach).
- Added `hash_from_slices(hash, (s1, s2), start, len)` helper to hash a logical range from the ring buffer's two slices.

**`decoding/frame_decoder.rs`:**
- After reading the checksum bytes in `decode_blocks()` and both checksum paths in `decode_from_to()`, compare `hash.finish() as u32` against the stored value.
- Returns `Err(FrameDecoderError::ChecksumMismatch { expected, got })` on mismatch.
- All three checksum-reading sites are covered:
  1. `decode_blocks()` — normal path
  2. `decode_from_to()` — inline last-block path
  3. `decode_from_to()` — deferred path (checksum bytes arrive in a subsequent call)

**`decoding/errors.rs`:**
- Added `ChecksumMismatch { expected: u32, got: u32 }` variant to `FrameDecoderError` (gated on `#[cfg(feature = "hash")]`).
- Added corresponding `Display` arm.

### Feature Gating

Everything is gated on `#[cfg(feature = "hash")]`. Without the `hash` feature (no_std mode), checksums are read and stored but not verified — the same behaviour as before.
