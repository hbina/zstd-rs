# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ruzstd is a pure Rust implementation of the Zstandard compression format (RFC8878). It provides both decompression (complete) and compression (partial - only "Fastest" level implemented). The library is `#![no_std]` compatible.

## Build Commands

```bash
# Build the library
cargo build -p ruzstd

# Build with all features
cargo build -p ruzstd --all-features

# Build the CLI
cargo build -p ruzstd-cli

# Run tests (from ruzstd directory)
cd ruzstd && cargo test

# Run a specific test
cd ruzstd && cargo test test_streaming

# Run tests without default features (no_std mode)
cd ruzstd && cargo test --no-default-features

# Run benchmarks
cd ruzstd && cargo bench

# Run fuzzing (requires nightly)
cd ruzstd/fuzz && cargo +nightly fuzz run decode
```

## Feature Flags (ruzstd crate)

- `std` (default): Enables standard library support
- `hash` (default): Enables xxhash checksums via twox-hash
- `dict_builder`: Enables dictionary creation (requires std)
- `fuzz_exports`: Exposes internal FSE/Huff0 modules for fuzzing

## Architecture

### Workspace Structure
- `ruzstd/`: Main library crate
- `cli/`: Command-line tool (`ruzstd-cli`)
- `ruzstd/fuzz/`: Fuzz testing targets (decode, encode, interop, huff0, fse)

### Core Modules (ruzstd/src/)

**Decoding Pipeline:**
- `decoding/streaming_decoder.rs`: High-level `io::Read` wrapper - recommended entry point
- `decoding/frame_decoder.rs`: Low-level frame decoding with `FrameDecoder`
- `decoding/block_decoder.rs`: Individual block decompression
- `decoding/sequence_section_decoder.rs`: Sequence decoding (major performance hotspot)
- `decoding/decode_buffer.rs`: Output buffer management with ringbuffer

**Encoding Pipeline:**
- `encoding/frame_compressor.rs`: Main compression entry point via `FrameCompressor`
- `encoding/match_generator.rs`: LZ77-style match finding (major performance hotspot)
- `encoding/blocks/compressed.rs`: Block compression logic

**Shared Components:**
- `fse/`: Finite State Entropy codec (decoder + encoder)
- `huff0/`: Huffman codec (decoder + encoder)
- `bit_io/`: Bit-level I/O (forward reader, reverse reader, writer)
- `blocks/`: Block and frame header parsing/writing

### Key Types

```rust
// Decompression
ruzstd::decoding::StreamingDecoder  // Implements io::Read
ruzstd::decoding::FrameDecoder      // Low-level, reusable decoder

// Compression
ruzstd::encoding::compress()        // Simple function API
ruzstd::encoding::FrameCompressor   // Configurable compressor
ruzstd::encoding::CompressionLevel  // Uncompressed, Fastest (others unimplemented)

// Custom matching
ruzstd::encoding::Matcher           // Trait for custom compression algorithms
```

### no_std Support

The library uses conditional compilation:
- `io_std.rs`: Re-exports `std::io` types when `std` feature enabled
- `io_nostd.rs`: Provides minimal `Read`/`Write` traits for no_std

## Testing Notes

- Tests in `ruzstd/src/tests/` require test files in `decodecorpus_files/` and `dict_tests/`
- Fuzz regression tests are in `tests/fuzz_regressions.rs`
- The `artifacts` test runs fuzzer-discovered crash cases

## Contributing

When making changes, add an entry to `Changelog.md`.

## Development

For every changes or issues that you encounter in the codebase, please create documentation file inside `./docs`.