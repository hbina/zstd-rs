# Phase 1: Correctness Fixes

## Summary

Two concrete panics existed in the codebase that have been converted to proper errors.

---

## Fix 1 — Reserved Block Type: `panic!` → `Err`

**Files changed:**
- `ruzstd/src/decoding/errors.rs` — added `ReservedBlockType` variant to `DecodeBlockContentError`
- `ruzstd/src/decoding/block_decoder.rs:87` — replaced `panic!` with `return Err(DecodeBlockContentError::ReservedBlockType)`

**Motivation:** RFC 8878 §3.1.1.2 specifies that a block with type `Reserved` is invalid. The decoder should return a recoverable error rather than crashing the process.

---

## Fix 2 — Compression Level `unimplemented!()` → `Err` (Breaking API Change)

**Files changed:**
- `ruzstd/src/encoding/errors.rs` — **new file** with `EncodeError` enum
- `ruzstd/src/encoding/mod.rs` — re-exported `EncodeError`; changed `compress()` and `compress_to_vec()` return types to `Result`; added `Debug` derive to `CompressionLevel`
- `ruzstd/src/encoding/frame_compressor.rs` — changed `compress()` to return `Result<(), EncodeError>`; replaced all `.unwrap()` with `?` and error mapping; replaced `unimplemented!()` with `Err(EncodeError::UnsupportedCompressionLevel(...))`

### Breaking API Change

| Old signature | New signature |
|---|---|
| `compress(...) -> ()` | `compress(...) -> Result<(), EncodeError>` |
| `compress_to_vec(...) -> Vec<u8>` | `compress_to_vec(...) -> Result<Vec<u8>, EncodeError>` |
| `FrameCompressor::compress(&mut self)` | `FrameCompressor::compress(&mut self) -> Result<(), EncodeError>` |

### `EncodeError` Variants

- `UnsupportedCompressionLevel(CompressionLevel)` — compression level is not yet implemented
- `WriteFailed(io::Error)` — I/O error writing to the drain
- `ReadFailed(io::Error)` — I/O error reading from the source
- `NoSource` — `compress()` called without setting a source
- `NoDrain` — `compress()` called without setting a drain

**Migration:** All existing call sites that did not handle the `Result` need to add `.unwrap()` or proper error propagation.

---

## Fix 3 — Dead `DecoderStateIsFailed` Code

The `DecodeBlockContentError::DecoderStateIsFailed` variant is never returned anywhere. Since the enum is `#[non_exhaustive]`, it was left in place to avoid any potential user breakage. A future cleanup pass may remove it.
