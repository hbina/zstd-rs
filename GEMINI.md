# ruzstd - Pure Rust Zstandard Implementation

## Project Overview

`ruzstd` is a pure Rust implementation of the Zstandard compression format, as defined in [RFC8878](https://www.rfc-editor.org/rfc/rfc8878.pdf).

The project consists of a Cargo workspace with two main members:
*   **`ruzstd`**: The core library crate providing decompression (fully operational) and compression (currently basic support). It supports `no_std` environments.
*   **`cli`**: A command-line interface (`ruzstd-cli`) for compressing and decompressing files using the `ruzstd` library.

**Current Status:**
*   **Decompression:** Fully operational `StreamingDecoder` and `FrameDecoder`.
*   **Compression:** usable but basic (supports generating compressed blocks at different levels, checksums; dictionary usage not yet fully implemented).
*   **Dictionary Builder:** Supported via the `dict_builder` feature (generating raw content dictionaries).

## Building and Running

### Prerequisites
*   Rust (stable for general dev, nightly for some tests/formatting).
*   `cargo-hack` (recommended for full feature matrix testing).

### Core Commands

*   **Build Workspace:**
    ```bash
    cargo build --workspace
    ```

*   **Run Tests:**
    ```bash
    cargo test --workspace
    ```

*   **Run CLI:**
    To use the CLI tool from the source:
    ```bash
    cargo run -p ruzstd-cli -- <command> [args]
    # Example:
    # cargo run -p ruzstd-cli -- help
    # cargo run -p ruzstd-cli -- decompress input.zst
    ```

### CI/Advanced Testing
The project uses `cargo-hack` to verify all feature combinations in CI:
```bash
cargo hack check --workspace --feature-powerset --exclude-features rustc-dep-of-std
cargo hack test --workspace --feature-powerset --exclude-features rustc-dep-of-std
```
Nightly toolchain is used for `fmt`, `clippy`, and `miri` tests:
```bash
cargo +nightly fmt --all -- --check
cargo +nightly miri test ringbuffer
```

## Development Conventions

*   **Code Style:** Adhere to standard Rust formatting (`rustfmt`).
*   **no_std:** The library `ruzstd` supports `no_std`. Ensure changes do not break this (unless guarded by the `std` feature).
*   **Testing:**
    *   Unit tests are present in `tests/` and inline.
    *   Fuzzing is used (see `fuzz/` directory and `fuzz_exports` feature).
    *   `decodecorpus_files` contains test data for verification.
*   **Features:**
    *   `std`: Enables std library support (default).
    *   `hash`: Enables checksum support via `twox-hash` (default).
    *   `dict_builder`: Enables dictionary creation tools.
    *   `rustc-dep-of-std`: Internal feature for use within the Rust standard library.

After every update, please update the documentation in `./docs`

## Directory Structure
*   `ruzstd/`: Core library source.
*   `cli/`: CLI application source.
*   `fuzz/`: Fuzzing targets.
*   `docs/`: Additional documentation.
*   `.github/`: CI configurations.
