"""
Equivalence Testing Script for ruzstd vs Reference zstd Implementation

This script performs comprehensive equivalence testing between the ruzstd
Rust implementation and the reference zstd tool. It tests:
1. Compression: ruzstd compress -> zstd decompress (verify output matches input)
2. Decompression: zstd compress -> ruzstd decompress (verify output matches input)
3. Round-trip: Various edge cases and random data

Usage:
    python3 scripts/equivalence_test.py [options]

Requirements:
    - Rust toolchain (cargo)
    - Reference zstd CLI tool installed (apt install zstd / brew install zstd)
    - Python 3.7+
"""

import argparse
import hashlib
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class TestConfig:
    """Configuration for the equivalence test suite."""

    workspace_root: Path
    temp_dir: Path
    ruzstd_binary: Path
    zstd_binary: str
    verbose: bool
    num_random_tests: int
    max_file_size: int
    num_workers: int
    seed: Optional[int]


class CompressionLevel(Enum):
    """Compression levels supported by ruzstd."""

    UNCOMPRESSED = 0
    FASTEST = 1
    DEFAULT = 2
    BETTER = 3
    BEST = 4


class TestResult(Enum):
    """Result of a single test case."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass
class TestCase:
    """A single test case."""

    name: str
    description: str
    result: TestResult
    duration_ms: float
    error_message: Optional[str] = None
    details: Optional[str] = None


# =============================================================================
# Utility Functions
# =============================================================================


def run_command(
    cmd: List[str],
    timeout: int = 300,
    capture_output: bool = True,
    check: bool = True,
    verbose: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    if verbose:
        print(f"  Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd, capture_output=capture_output, timeout=timeout, check=check, text=True
        )
        return result
    except subprocess.CalledProcessError as e:
        if verbose:
            print(f"  Command failed with code {e.returncode}")
            if e.stdout:
                print(f"  stdout: {e.stdout[:500]}")
            if e.stderr:
                print(f"  stderr: {e.stderr[:500]}")
        raise
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(cmd)}")


def compute_file_hash(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compare_files(file1: Path, file2: Path) -> Tuple[bool, str]:
    """
    Compare two files for equality.
    Returns (is_equal, message).
    """
    if not file1.exists():
        return False, f"File 1 does not exist: {file1}"
    if not file2.exists():
        return False, f"File 2 does not exist: {file2}"

    size1 = file1.stat().st_size
    size2 = file2.stat().st_size

    if size1 != size2:
        return False, f"Size mismatch: {size1} vs {size2} bytes"

    hash1 = compute_file_hash(file1)
    hash2 = compute_file_hash(file2)

    if hash1 != hash2:
        return False, f"Content mismatch: SHA256 {hash1[:16]}... vs {hash2[:16]}..."

    return True, "Files are identical"


def generate_random_bytes(size: int, seed: Optional[int] = None) -> bytes:
    """Generate random bytes of specified size."""
    if seed is not None:
        random.seed(seed)
    return bytes(random.getrandbits(8) for _ in range(size))


def generate_compressible_data(size: int, seed: Optional[int] = None) -> bytes:
    """Generate data that is somewhat compressible (repeated patterns)."""
    if seed is not None:
        random.seed(seed)

    patterns = [
        b"Hello, World! ",
        b"The quick brown fox jumps over the lazy dog. ",
        b"Lorem ipsum dolor sit amet, consectetur adipiscing elit. ",
        b"\x00" * 16,
        b"\xff" * 16,
        bytes(range(256)),
        b"AAAAAAAAAA",
        b"ABCDEFGHIJ",
    ]

    result = bytearray()
    while len(result) < size:
        pattern = random.choice(patterns)
        repeat = random.randint(1, 20)
        result.extend(pattern * repeat)

    return bytes(result[:size])


def generate_highly_compressible_data(size: int) -> bytes:
    """Generate highly compressible data (mostly zeros with some variation)."""
    result = bytearray(size)
    # Add some random bytes scattered throughout
    for _ in range(max(1, size // 100)):
        pos = random.randint(0, size - 1)
        result[pos] = random.randint(0, 255)
    return bytes(result)


def generate_text_data(size: int, seed: Optional[int] = None) -> bytes:
    """Generate random text-like data."""
    if seed is not None:
        random.seed(seed)

    words = [
        "the",
        "be",
        "to",
        "of",
        "and",
        "a",
        "in",
        "that",
        "have",
        "I",
        "it",
        "for",
        "not",
        "on",
        "with",
        "he",
        "as",
        "you",
        "do",
        "at",
        "this",
        "but",
        "his",
        "by",
        "from",
        "they",
        "we",
        "say",
        "her",
        "she",
        "or",
        "an",
        "will",
        "my",
        "one",
        "all",
        "would",
        "there",
        "their",
        "what",
    ]

    result = []
    current_size = 0
    while current_size < size:
        word = random.choice(words)
        if random.random() < 0.1:
            word = word.capitalize()
        if random.random() < 0.05:
            word = word.upper()

        separator = (
            " " if random.random() > 0.1 else random.choice(["\n", "  ", ". ", ", "])
        )
        chunk = word + separator
        result.append(chunk)
        current_size += len(chunk)

    return "".join(result)[:size].encode("utf-8")


# =============================================================================
# Test Data Generators
# =============================================================================


class TestDataGenerator:
    """Generates various types of test data."""

    def __init__(self, temp_dir: Path, seed: Optional[int] = None):
        self.temp_dir = temp_dir
        self.seed = seed
        self.file_counter = 0

    def _next_filename(self, prefix: str, extension: str = "") -> Path:
        self.file_counter += 1
        return self.temp_dir / f"{prefix}_{self.file_counter:04d}{extension}"

    def create_empty_file(self) -> Path:
        """Create an empty file."""
        filepath = self._next_filename("empty")
        filepath.touch()
        return filepath

    def create_single_byte_file(self, byte_value: int = 0x42) -> Path:
        """Create a file with a single byte."""
        filepath = self._next_filename("single_byte")
        filepath.write_bytes(bytes([byte_value]))
        return filepath

    def create_random_file(self, size: int) -> Path:
        """Create a file with random data."""
        filepath = self._next_filename(f"random_{size}")
        data = generate_random_bytes(size, self.seed)
        filepath.write_bytes(data)
        return filepath

    def create_compressible_file(self, size: int) -> Path:
        """Create a file with compressible data."""
        filepath = self._next_filename(f"compressible_{size}")
        data = generate_compressible_data(size, self.seed)
        filepath.write_bytes(data)
        return filepath

    def create_highly_compressible_file(self, size: int) -> Path:
        """Create a file with highly compressible data."""
        filepath = self._next_filename(f"highly_compressible_{size}")
        data = generate_highly_compressible_data(size)
        filepath.write_bytes(data)
        return filepath

    def create_text_file(self, size: int) -> Path:
        """Create a file with text-like data."""
        filepath = self._next_filename(f"text_{size}")
        data = generate_text_data(size, self.seed)
        filepath.write_bytes(data)
        return filepath

    def create_all_zeros_file(self, size: int) -> Path:
        """Create a file filled with zeros."""
        filepath = self._next_filename(f"zeros_{size}")
        filepath.write_bytes(b"\x00" * size)
        return filepath

    def create_all_ones_file(self, size: int) -> Path:
        """Create a file filled with 0xFF bytes."""
        filepath = self._next_filename(f"ones_{size}")
        filepath.write_bytes(b"\xff" * size)
        return filepath

    def create_sequential_file(self, size: int) -> Path:
        """Create a file with sequential bytes (0, 1, 2, ... 255, 0, 1, ...)."""
        filepath = self._next_filename(f"sequential_{size}")
        data = bytes([i % 256 for i in range(size)])
        filepath.write_bytes(data)
        return filepath

    def create_dev_urandom_file(self, size: int) -> Path:
        """Create a file from /dev/urandom."""
        filepath = self._next_filename(f"urandom_{size}")
        with open("/dev/urandom", "rb") as urandom:
            data = urandom.read(size)
        filepath.write_bytes(data)
        return filepath


# =============================================================================
# Test Runner
# =============================================================================


class EquivalenceTestRunner:
    """Runs equivalence tests between ruzstd and reference zstd."""

    def __init__(self, config: TestConfig):
        self.config = config
        self.results: List[TestCase] = []
        self.generator = TestDataGenerator(config.temp_dir, config.seed)

    def _log(self, message: str):
        """Print a log message if verbose mode is enabled."""
        if self.config.verbose:
            print(message)

    def _run_ruzstd_compress(
        self,
        input_file: Path,
        output_file: Path,
        level: CompressionLevel = CompressionLevel.FASTEST,
    ) -> bool:
        """Run ruzstd compress command."""
        cmd = [
            str(self.config.ruzstd_binary),
            "compress",
            str(input_file),
            str(output_file),
            "-l",
            str(level.value),
        ]
        try:
            run_command(cmd, verbose=self.config.verbose)
            return True
        except Exception as e:
            self._log(f"  ruzstd compress failed: {e}")
            return False

    def _run_ruzstd_decompress(self, input_file: Path, output_file: Path) -> bool:
        """Run ruzstd decompress command."""
        cmd = [
            str(self.config.ruzstd_binary),
            "decompress",
            str(input_file),
            str(output_file),
        ]
        try:
            run_command(cmd, verbose=self.config.verbose)
            return True
        except Exception as e:
            self._log(f"  ruzstd decompress failed: {e}")
            return False

    def _run_zstd_compress(
        self, input_file: Path, output_file: Path, level: int = 1
    ) -> bool:
        """Run reference zstd compress command."""
        cmd = [
            self.config.zstd_binary,
            "-f",  # Force overwrite
            f"-{level}",  # Compression level
            "-o",
            str(output_file),
            str(input_file),
        ]
        try:
            run_command(cmd, verbose=self.config.verbose)
            return True
        except Exception as e:
            self._log(f"  zstd compress failed: {e}")
            return False

    def _run_zstd_decompress(self, input_file: Path, output_file: Path) -> bool:
        """Run reference zstd decompress command."""
        cmd = [
            self.config.zstd_binary,
            "-d",  # Decompress
            "-f",  # Force overwrite
            "-o",
            str(output_file),
            str(input_file),
        ]
        try:
            run_command(cmd, verbose=self.config.verbose)
            return True
        except Exception as e:
            self._log(f"  zstd decompress failed: {e}")
            return False

    def test_ruzstd_compress_zstd_decompress(
        self,
        test_name: str,
        input_file: Path,
        level: CompressionLevel = CompressionLevel.FASTEST,
    ) -> TestCase:
        """
        Test: ruzstd compresses a file, zstd decompresses it.
        Verify the decompressed output matches the original input.
        """
        start_time = time.time()

        compressed_file = input_file.with_suffix(".zst")
        decompressed_file = input_file.with_suffix(".decompressed")

        try:
            # Compress with ruzstd
            if not self._run_ruzstd_compress(input_file, compressed_file, level):
                return TestCase(
                    name=test_name,
                    description=f"ruzstd compress -> zstd decompress (level {level.value})",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="ruzstd compression failed",
                )

            # Decompress with zstd
            if not self._run_zstd_decompress(compressed_file, decompressed_file):
                return TestCase(
                    name=test_name,
                    description=f"ruzstd compress -> zstd decompress (level {level.value})",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="zstd decompression failed",
                )

            # Compare files
            is_equal, message = compare_files(input_file, decompressed_file)

            return TestCase(
                name=test_name,
                description=f"ruzstd compress -> zstd decompress (level {level.value})",
                result=TestResult.PASSED if is_equal else TestResult.FAILED,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=None if is_equal else message,
                details=f"Input size: {input_file.stat().st_size} bytes, "
                f"Compressed: {compressed_file.stat().st_size} bytes",
            )

        except Exception as e:
            return TestCase(
                name=test_name,
                description=f"ruzstd compress -> zstd decompress (level {level.value})",
                result=TestResult.ERROR,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )
        finally:
            # Cleanup
            for f in [compressed_file, decompressed_file]:
                if f.exists():
                    f.unlink()

    def test_zstd_compress_ruzstd_decompress(
        self, test_name: str, input_file: Path, level: int = 1
    ) -> TestCase:
        """
        Test: zstd compresses a file, ruzstd decompresses it.
        Verify the decompressed output matches the original input.
        """
        start_time = time.time()

        compressed_file = input_file.with_suffix(".zst")
        decompressed_file = input_file.with_suffix(".decompressed")

        try:
            # Compress with zstd
            if not self._run_zstd_compress(input_file, compressed_file, level):
                return TestCase(
                    name=test_name,
                    description=f"zstd compress (level {level}) -> ruzstd decompress",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="zstd compression failed",
                )

            # Decompress with ruzstd
            if not self._run_ruzstd_decompress(compressed_file, decompressed_file):
                return TestCase(
                    name=test_name,
                    description=f"zstd compress (level {level}) -> ruzstd decompress",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="ruzstd decompression failed",
                )

            # Compare files
            is_equal, message = compare_files(input_file, decompressed_file)

            return TestCase(
                name=test_name,
                description=f"zstd compress (level {level}) -> ruzstd decompress",
                result=TestResult.PASSED if is_equal else TestResult.FAILED,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=None if is_equal else message,
                details=f"Input size: {input_file.stat().st_size} bytes, "
                f"Compressed: {compressed_file.stat().st_size} bytes",
            )

        except Exception as e:
            return TestCase(
                name=test_name,
                description=f"zstd compress (level {level}) -> ruzstd decompress",
                result=TestResult.ERROR,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )
        finally:
            # Cleanup
            for f in [compressed_file, decompressed_file]:
                if f.exists():
                    f.unlink()

    def test_roundtrip_ruzstd(
        self,
        test_name: str,
        input_file: Path,
        level: CompressionLevel = CompressionLevel.FASTEST,
    ) -> TestCase:
        """
        Test: ruzstd compress -> ruzstd decompress.
        Verify the round-trip output matches the original input.
        """
        start_time = time.time()

        compressed_file = input_file.with_suffix(".zst")
        decompressed_file = input_file.with_suffix(".decompressed")

        try:
            # Compress with ruzstd
            if not self._run_ruzstd_compress(input_file, compressed_file, level):
                return TestCase(
                    name=test_name,
                    description=f"ruzstd roundtrip (level {level.value})",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="ruzstd compression failed",
                )

            # Decompress with ruzstd
            if not self._run_ruzstd_decompress(compressed_file, decompressed_file):
                return TestCase(
                    name=test_name,
                    description=f"ruzstd roundtrip (level {level.value})",
                    result=TestResult.ERROR,
                    duration_ms=(time.time() - start_time) * 1000,
                    error_message="ruzstd decompression failed",
                )

            # Compare files
            is_equal, message = compare_files(input_file, decompressed_file)

            return TestCase(
                name=test_name,
                description=f"ruzstd roundtrip (level {level.value})",
                result=TestResult.PASSED if is_equal else TestResult.FAILED,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=None if is_equal else message,
                details=f"Input size: {input_file.stat().st_size} bytes, "
                f"Compressed: {compressed_file.stat().st_size} bytes",
            )

        except Exception as e:
            return TestCase(
                name=test_name,
                description=f"ruzstd roundtrip (level {level.value})",
                result=TestResult.ERROR,
                duration_ms=(time.time() - start_time) * 1000,
                error_message=str(e),
            )
        finally:
            # Cleanup
            for f in [compressed_file, decompressed_file]:
                if f.exists():
                    f.unlink()

    def run_edge_case_tests(self) -> List[TestCase]:
        """Run tests for edge cases."""
        results = []

        print("\n=== Edge Case Tests ===")

        # Empty file
        print("  Testing empty file...")
        empty_file = self.generator.create_empty_file()
        results.append(
            self.test_zstd_compress_ruzstd_decompress("empty_file", empty_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("empty_file", empty_file)
        )
        results.append(self.test_roundtrip_ruzstd("empty_file_roundtrip", empty_file))

        # Single byte
        print("  Testing single byte file...")
        single_byte_file = self.generator.create_single_byte_file()
        results.append(
            self.test_zstd_compress_ruzstd_decompress("single_byte", single_byte_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("single_byte", single_byte_file)
        )
        results.append(
            self.test_roundtrip_ruzstd("single_byte_roundtrip", single_byte_file)
        )

        # All zeros (various sizes)
        for size in [100, 1000, 10000, 100000]:
            print(f"  Testing all zeros file ({size} bytes)...")
            zeros_file = self.generator.create_all_zeros_file(size)
            results.append(
                self.test_zstd_compress_ruzstd_decompress(f"zeros_{size}", zeros_file)
            )
            results.append(
                self.test_ruzstd_compress_zstd_decompress(f"zeros_{size}", zeros_file)
            )

        # All 0xFF bytes
        print("  Testing all 0xFF file...")
        ones_file = self.generator.create_all_ones_file(10000)
        results.append(self.test_zstd_compress_ruzstd_decompress("all_0xff", ones_file))
        results.append(self.test_ruzstd_compress_zstd_decompress("all_0xff", ones_file))

        # Sequential bytes
        print("  Testing sequential bytes file...")
        seq_file = self.generator.create_sequential_file(10000)
        results.append(
            self.test_zstd_compress_ruzstd_decompress("sequential", seq_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("sequential", seq_file)
        )

        return results

    def run_size_variation_tests(self) -> List[TestCase]:
        """Run tests with various file sizes."""
        results = []

        print("\n=== Size Variation Tests ===")

        # Powers of 2 and boundary sizes
        sizes = [
            1,
            2,
            3,
            4,
            7,
            8,
            15,
            16,
            31,
            32,
            63,
            64,
            127,
            128,
            255,
            256,
            511,
            512,
            1023,
            1024,
            2048,
            4096,
            8192,
            16384,
            32768,
            65536,
            131072,
            262144,
            524288,
            1048576,  # Up to 1MB
        ]

        # Filter sizes based on max_file_size config
        sizes = [s for s in sizes if s <= self.config.max_file_size]

        for size in sizes:
            print(f"  Testing compressible data ({size} bytes)...")
            comp_file = self.generator.create_compressible_file(size)
            results.append(
                self.test_zstd_compress_ruzstd_decompress(
                    f"compressible_{size}", comp_file
                )
            )
            results.append(
                self.test_ruzstd_compress_zstd_decompress(
                    f"compressible_{size}", comp_file
                )
            )

        return results

    def run_data_type_tests(self) -> List[TestCase]:
        """Run tests with different data types."""
        results = []

        print("\n=== Data Type Tests ===")

        test_size = min(100000, self.config.max_file_size)

        # Highly compressible data
        print(f"  Testing highly compressible data ({test_size} bytes)...")
        hc_file = self.generator.create_highly_compressible_file(test_size)
        results.append(
            self.test_zstd_compress_ruzstd_decompress("highly_compressible", hc_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("highly_compressible", hc_file)
        )
        results.append(
            self.test_roundtrip_ruzstd("highly_compressible_roundtrip", hc_file)
        )

        # Text-like data
        print(f"  Testing text-like data ({test_size} bytes)...")
        text_file = self.generator.create_text_file(test_size)
        results.append(
            self.test_zstd_compress_ruzstd_decompress("text_data", text_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("text_data", text_file)
        )
        results.append(self.test_roundtrip_ruzstd("text_data_roundtrip", text_file))

        # Random (incompressible) data
        print(f"  Testing random data ({test_size} bytes)...")
        rand_file = self.generator.create_random_file(test_size)
        results.append(
            self.test_zstd_compress_ruzstd_decompress("random_data", rand_file)
        )
        results.append(
            self.test_ruzstd_compress_zstd_decompress("random_data", rand_file)
        )
        results.append(self.test_roundtrip_ruzstd("random_data_roundtrip", rand_file))

        # /dev/urandom data (truly random)
        if os.path.exists("/dev/urandom"):
            print(f"  Testing /dev/urandom data ({test_size} bytes)...")
            urandom_file = self.generator.create_dev_urandom_file(test_size)
            results.append(
                self.test_zstd_compress_ruzstd_decompress("urandom_data", urandom_file)
            )
            results.append(
                self.test_ruzstd_compress_zstd_decompress("urandom_data", urandom_file)
            )
            results.append(
                self.test_roundtrip_ruzstd("urandom_data_roundtrip", urandom_file)
            )

        return results

    def run_compression_level_tests(self) -> List[TestCase]:
        """Run tests with different zstd compression levels."""
        results = []

        print("\n=== Compression Level Tests (zstd -> ruzstd) ===")

        test_size = min(50000, self.config.max_file_size)
        test_file = self.generator.create_compressible_file(test_size)

        # Test zstd compression levels 1-19 (and 0 for no compression)
        for level in [1, 3, 5, 9, 15, 19]:
            print(f"  Testing zstd level {level}...")
            results.append(
                self.test_zstd_compress_ruzstd_decompress(
                    f"zstd_level_{level}", test_file, level=level
                )
            )

        return results

    def run_random_tests(self) -> List[TestCase]:
        """Run tests with randomly generated data."""
        results = []

        print(f"\n=== Random Tests ({self.config.num_random_tests} iterations) ===")

        for i in range(self.config.num_random_tests):
            # Random size between 1 byte and max_file_size
            size = random.randint(1, self.config.max_file_size)

            # Randomly choose data type
            data_type = random.choice(["random", "compressible", "text", "urandom"])

            print(
                f"  Test {i + 1}/{self.config.num_random_tests}: {data_type} data, {size} bytes..."
            )

            if data_type == "random":
                test_file = self.generator.create_random_file(size)
            elif data_type == "compressible":
                test_file = self.generator.create_compressible_file(size)
            elif data_type == "text":
                test_file = self.generator.create_text_file(size)
            else:  # urandom
                if os.path.exists("/dev/urandom"):
                    test_file = self.generator.create_dev_urandom_file(size)
                else:
                    test_file = self.generator.create_random_file(size)

            # Test both directions
            results.append(
                self.test_zstd_compress_ruzstd_decompress(
                    f"random_test_{i + 1}_zstd", test_file
                )
            )
            results.append(
                self.test_ruzstd_compress_zstd_decompress(
                    f"random_test_{i + 1}_ruzstd", test_file
                )
            )
            results.append(
                self.test_roundtrip_ruzstd(f"random_test_{i + 1}_roundtrip", test_file)
            )

        return results

    def run_all_tests(self) -> List[TestCase]:
        """Run all test suites."""
        all_results = []

        all_results.extend(self.run_edge_case_tests())
        all_results.extend(self.run_size_variation_tests())
        all_results.extend(self.run_data_type_tests())
        all_results.extend(self.run_compression_level_tests())
        all_results.extend(self.run_random_tests())

        return all_results


# =============================================================================
# Report Generation
# =============================================================================


def print_results_summary(results: List[TestCase]):
    """Print a summary of test results."""
    passed = sum(1 for r in results if r.result == TestResult.PASSED)
    failed = sum(1 for r in results if r.result == TestResult.FAILED)
    errors = sum(1 for r in results if r.result == TestResult.ERROR)
    skipped = sum(1 for r in results if r.result == TestResult.SKIPPED)
    total = len(results)

    total_time = sum(r.duration_ms for r in results) / 1000

    print("\n" + "=" * 70)
    print("TEST RESULTS SUMMARY")
    print("=" * 70)
    print(f"  Total tests:  {total}")
    print(
        f"  Passed:       {passed} ({100 * passed / total:.1f}%)"
        if total > 0
        else "  Passed:       0"
    )
    print(f"  Failed:       {failed}")
    print(f"  Errors:       {errors}")
    print(f"  Skipped:      {skipped}")
    print(f"  Total time:   {total_time:.2f}s")
    print("=" * 70)

    # Print failures and errors
    failures = [r for r in results if r.result in (TestResult.FAILED, TestResult.ERROR)]
    if failures:
        print("\nFAILURES AND ERRORS:")
        print("-" * 70)
        for result in failures:
            status = "FAILED" if result.result == TestResult.FAILED else "ERROR"
            print(f"  [{status}] {result.name}")
            print(f"    Description: {result.description}")
            if result.error_message:
                print(f"    Error: {result.error_message}")
            if result.details:
                print(f"    Details: {result.details}")
            print()

    return failed == 0 and errors == 0


# =============================================================================
# Main Entry Point
# =============================================================================


def check_prerequisites(config: TestConfig) -> bool:
    """Check that all prerequisites are met."""
    print("Checking prerequisites...")

    # Check for reference zstd
    try:
        result = run_command([config.zstd_binary, "--version"], check=False)
        print(
            f"  Found zstd: {result.stdout.strip() if result.stdout else 'version unknown'}"
        )
    except FileNotFoundError:
        print(f"  ERROR: Reference zstd not found at '{config.zstd_binary}'")
        print("  Please install zstd (apt install zstd / brew install zstd)")
        return False

    # Check for cargo
    try:
        result = run_command(["cargo", "--version"], check=False)
        print(
            f"  Found cargo: {result.stdout.strip() if result.stdout else 'version unknown'}"
        )
    except FileNotFoundError:
        print("  ERROR: cargo not found")
        print("  Please install the Rust toolchain")
        return False

    # Check workspace exists
    if not config.workspace_root.exists():
        print(f"  ERROR: Workspace not found at {config.workspace_root}")
        return False

    print("  All prerequisites satisfied")
    return True


def build_project(config: TestConfig) -> bool:
    """Build the ruzstd project."""
    print("\nBuilding ruzstd...")

    try:
        # Build in release mode for better performance
        result = run_command(
            ["cargo", "build", "--release", "-p", "ruzstd-cli"],
            verbose=config.verbose,
            timeout=600,
        )
        print("  Build successful")

        # Verify binary exists
        if not config.ruzstd_binary.exists():
            print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
            return False

        print(f"  Binary: {config.ruzstd_binary}")
        return True

    except subprocess.CalledProcessError as e:
        print(f"  Build failed with exit code {e.returncode}")
        if e.stderr:
            print(f"  stderr: {e.stderr[:1000]}")
        return False
    except Exception as e:
        print(f"  Build failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Equivalence testing for ruzstd vs reference zstd"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "-n",
        "--num-random-tests",
        type=int,
        default=50,
        help="Number of random test iterations (default: 50)",
    )
    parser.add_argument(
        "-m",
        "--max-file-size",
        type=int,
        default=10 * 1024 * 1024,  # 10 MB
        help="Maximum file size for tests in bytes (default: 10MB)",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1)",
    )
    parser.add_argument(
        "-s", "--seed", type=int, default=None, help="Random seed for reproducibility"
    )
    parser.add_argument(
        "--zstd",
        type=str,
        default="zstd",
        help="Path to reference zstd binary (default: zstd)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Path to workspace root (default: auto-detect)",
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="Skip the cargo build step"
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary files after test completion",
    )

    args = parser.parse_args()

    # Determine workspace root
    if args.workspace:
        workspace_root = Path(args.workspace).resolve()
    else:
        # Auto-detect: look for Cargo.toml
        script_dir = Path(__file__).parent.resolve()
        workspace_root = script_dir.parent
        if not (workspace_root / "Cargo.toml").exists():
            workspace_root = Path.cwd()

    print("=" * 70)
    print("RUZSTD EQUIVALENCE TEST SUITE")
    print("=" * 70)
    print(f"Workspace: {workspace_root}")
    print(f"Random tests: {args.num_random_tests}")
    print(f"Max file size: {args.max_file_size / (1024 * 1024):.1f} MB")
    if args.seed is not None:
        print(f"Random seed: {args.seed}")
        random.seed(args.seed)
    print()

    # Create temp directory
    temp_dir = Path(tempfile.mkdtemp(prefix="ruzstd_equiv_test_"))
    print(f"Temp directory: {temp_dir}")

    try:
        # Create config
        config = TestConfig(
            workspace_root=workspace_root,
            temp_dir=temp_dir,
            ruzstd_binary=workspace_root / "target" / "release" / "ruzstd-cli",
            zstd_binary=args.zstd,
            verbose=args.verbose,
            num_random_tests=args.num_random_tests,
            max_file_size=args.max_file_size,
            num_workers=args.workers,
            seed=args.seed,
        )

        # Check prerequisites
        if not check_prerequisites(config):
            return 1

        # Build project
        if not args.skip_build:
            if not build_project(config):
                return 1
        else:
            print("\nSkipping build (--skip-build)")
            if not config.ruzstd_binary.exists():
                print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
                print("  Run without --skip-build to build the project")
                return 1

        # Run tests
        runner = EquivalenceTestRunner(config)
        results = runner.run_all_tests()

        # Print summary
        success = print_results_summary(results)

        return 0 if success else 1

    finally:
        # Cleanup
        if not args.keep_temp:
            print(f"\nCleaning up temp directory: {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            print(f"\nKeeping temp directory: {temp_dir}")


if __name__ == "__main__":
    sys.exit(main())
