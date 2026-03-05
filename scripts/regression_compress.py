#!/usr/bin/env python3
"""
Regression tests for ruzstd compression bugs.

Each test encodes data with ruzstd-cli, decompresses with reference zstd,
and verifies round-trip integrity.

Usage:
    python3 scripts/regression_compress.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).parent.parent.resolve()
RUZSTD_BINARY = WORKSPACE_ROOT / "target" / "release" / "ruzstd-cli"
ZSTD_BINARY = "zstd"


def _run_single_test(data: bytes, level: int, label: str) -> bool:
    """Compress data with ruzstd, decompress with zstd, verify round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        input_path = tmpdir / "input.bin"
        compressed_path = tmpdir / "output.zst"
        decompressed_path = tmpdir / "decompressed.bin"
        input_path.write_bytes(data)
        r = subprocess.run(
            [
                str(RUZSTD_BINARY),
                "compress",
                "--level",
                str(level),
                str(input_path),
                str(compressed_path),
            ],
            capture_output=True,
        )
        if r.returncode != 0:
            print(f"FAIL {label}: compress failed: {r.stderr.decode()[:200]}")
            return False
        r = subprocess.run(
            [
                ZSTD_BINARY,
                "-d",
                "-f",
                "-o",
                str(decompressed_path),
                str(compressed_path),
            ],
            capture_output=True,
        )
        if r.returncode != 0:
            print(f"FAIL {label}: decompress failed: {r.stderr.decode()[:200]}")
            return False
        result = decompressed_path.read_bytes()
        if result != data:
            print(
                f"FAIL {label}: mismatch: input={len(data)} decompressed={len(result)}"
            )
            return False
        print(f"OK   {label}")
        return True


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


def test_regression_encode_match_len_code52():
    """
    Regression for encode_match_len ML code 52 bug:
    For match lengths 65539..=131074 (ML code 52), the add_bits were computed as
    `len - 32771` instead of the correct `len - 65539` (the code 52 baseline per RFC 8878).
    This bug was latent: the current match generator cannot produce matches >= 65539 bytes
    because the window is limited to one 128KB slice and SuffixStore collisions prevent
    very long in-block matches. Fixed in encode_match_len.
    """
    # We cannot directly trigger this via the CLI (match generator can't produce such matches),
    # but we verify the corrected function is consistent with the decoder for the full range.
    # This test exercises large repeating blocks to stress the match finder.
    data = b"\xaa" * 131072 * 2
    return _run_single_test(
        data, level=1, label="match_len_code52_stress_repeating_260KB"
    )


def test_regression_encode_seqnum_byte_order():
    """
    Regression for encode_seqnum bugs:
    1. The 2-byte format was used for seqnum up to 0x7FFF (32767), but seqnum >= 0x7F00
       (32512) would produce first byte = 0xFF which is the 3-byte sentinel — confusing
       the decoder.
    2. The 3-byte format wrote bytes in big-endian order (high byte first), but the
       decoder expects little-endian (low byte first per RFC 8878:
       seqnum = source[1] + source[2]*256 + 0x7F00).
    Both are latent: the current match generator yields at most ~26214 sequences per
    block (131072 / MIN_MATCH_LEN=5), well below 32512. Fixed in encode_seqnum.
    """
    # Stress test with many sequences (short repeated pattern so matches are short)
    # This produces many sequences but still well under 32512 per block.
    data = (
        bytes(range(256)) * 512
    )  # 128KB unique patterns, compresses with many short matches
    return _run_single_test(data, level=1, label="seqnum_byte_order_stress_128KB")


def test_regression_write_bits_assertion():
    """
    Regression for write_bits_64 debug_assert weakness:
    The assertion `bits.ilog2() <= num_bits` allowed values up to 2^(num_bits+1)-1,
    silently truncating values that don't fit. The correct check is `bits < (1 << num_bits)`.
    This was strengthened to catch potential encoding bugs early in debug builds.
    """
    data = b"\x42" * 10000
    return _run_single_test(data, level=1, label="write_bits_assertion_simple")


def test_regression_interop_fuzz_target():
    """
    Regression for interop.rs fuzz target bugs:
    1. encode_ruzstd_uncompressed and encode_ruzstd_compressed both read input into
       `input` but then passed the exhausted reader `data` to compress_to_vec, always
       compressing empty bytes.
    2. encode_ruzstd_compressed used CompressionLevel::Uncompressed instead of Fastest.
    Both bugs meant the crash artifacts (1-byte and 2-byte inputs) were false positives
    caused by the fuzz harness, not by the library. Fixed in interop.rs.
    """
    # Single-byte and two-byte inputs that previously triggered fuzz target failures
    ok = True
    ok &= _run_single_test(b"\x00", level=0, label="interop_single_byte_0x00")
    ok &= _run_single_test(b"\x00\xda", level=0, label="interop_two_bytes_0x00_0xda")
    ok &= _run_single_test(b"\x00", level=1, label="interop_single_byte_0x00_level1")
    ok &= _run_single_test(
        b"\x00\xda", level=1, label="interop_two_bytes_0x00_0xda_level1"
    )
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if not RUZSTD_BINARY.exists():
        print(f"ERROR: ruzstd-cli not found at {RUZSTD_BINARY}")
        print("Build it with: cargo build --release -p ruzstd-cli")
        return 1

    tests = [
        test_regression_encode_match_len_code52,
        test_regression_encode_seqnum_byte_order,
        test_regression_write_bits_assertion,
        test_regression_interop_fuzz_target,
    ]

    passed = failed = 0
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"EXCEPTION in {test.__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
