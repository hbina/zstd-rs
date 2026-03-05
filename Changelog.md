# Changelog

This document records the changes made between versions, starting with version 0.5.0

# After 0.8.2 (Current)
* **BREAKING**: `encoding::compress()` now returns `Result<(), EncodeError>` instead of `()`
* **BREAKING**: `encoding::compress_to_vec()` now returns `Result<Vec<u8>, EncodeError>` instead of `Vec<u8>`
* **BREAKING**: `FrameCompressor::compress()` now returns `Result<(), EncodeError>` instead of `()`
* Add `encoding::EncodeError` enum to represent compression errors; replaces panics with proper errors
* Fix `BlockType::Reserved` in block decoder: `panic!` replaced with `Err(DecodeBlockContentError::ReservedBlockType)`
* Fix `CompressionLevel` variants `Default`/`Better`/`Best` in compressor: `unimplemented!()` replaced with `Err(EncodeError::UnsupportedCompressionLevel(...))`
* Fix latent encoding bugs found via compression fuzzing and RFC 8878 review:
  - `encode_match_len` ML code 52: wrong baseline (`len - 32771` → `len - 65539`)
  - `encode_seqnum` 2-byte format: allowed first byte 0xFF (3-byte sentinel); range narrowed to 128..=0x7EFF
  - `encode_seqnum` 3-byte format: byte order was big-endian; fixed to little-endian per RFC 8878
  - `write_bits_64` debug assertion strengthened to correctly detect overflowing values
* Fix `interop.rs` fuzz target: exhausted reader was passed to `compress_to_vec` instead of the buffered input; `encode_ruzstd_compressed` now uses `CompressionLevel::Fastest`
* Compression fuzzer (`scripts/fuzz_compress.py`) now defaults to level 1 (Fastest) with 1 KB minimum input size
* Add `scripts/regression_compress.py` for compression regression tests
* Add `docs/compression_fuzzing_findings.md` documenting all findings
* `StreamingDecoder` now transparently handles multiple concatenated zstd frames and skippable frames, matching RFC 8878 requirements
* Introduce the `rust-version` field

# After 0.8.1
* The CLI has been refactored to use `clap`
* The MatchDriverGenerator has been made public so users can name it as `M` in `FrameCompressor<R,W,M>`

# After 0.8.0
* The compressor now includes a `content_checksum` when the `hash` feature is enabled
* Dictionary generation has been added

# After 0.7.3
* Add initial compression support
* **Breaking** Refactor modules to reflect that this is now also a compression library

# After 0.7.2
* Soundness fix in decoding::RingBuffer. The lengths of the diferent regions where sometimes calculated wrongly, resulting in reads of heap memory not belonging to that ringbuffer
    * Fixed by https://github.com/paolobarbolini
    * Affected versions: 0.7.0 up to and including 0.7.2

* Added convenience functions to FrameDecoder to decode multiple frames from a buffer (https://github.com/philipc)

# After 0.7.1

* Remove byteorder dependency (https://github.com/workingjubilee)
* Preparations to become a std dependency (https://github.com/workingjubilee)

# After 0.7.0
* Fix for drain_to functions into limited targets (https://github.com/michaelkirk)

# After 0.6.0
* Small fix in the zstd binary, progress tracking was slighty off for skippable frames resulting in an error only when the last frame in a file was skippable
* Small performance improvement by reorganizing code with `#[cold]` annotations
* Documentation for `StreamDecoder` mentioning the limitations around multiple frames (https://github.com/Sorseg)
* Documentation around skippable frames (https://github.com/Sorseg)
* **Breaking** `StreamDecoder` API changes to get access to the inner parts (https://github.com/ifd3f)
* Big internal documentation contribution (https://github.com/zleyyij)
* Dropped derive_more as a dependency (https://github.com/xd009642)
* Small improvement by removing the error cases from the reverse bitreader (and making sure invalid requests can't even happen)

# After 0.5.0
* Make the hashing checksum optional (thanks to [@tamird](https://github.com/tamird))
    * breaking change as the public API changes based on features
* The FrameDecoder is now Send + Sync (RingBuffer impls these traits now)
