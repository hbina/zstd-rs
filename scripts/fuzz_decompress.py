#!/usr/bin/env python3
"""
Continuous Decompression Fuzzer for ruzstd

This script performs continuous fuzzing of the ruzstd decompressor by:
1. Building the ruzstd-cli binary
2. Generating random test data in /tmp
3. Compressing with reference zstd
4. Decompressing with both zstd and ruzstd
5. Verifying 3-way equivalence (original == zstd_decompressed == ruzstd_decompressed)

The fuzzer runs continuously until a failure is found or interrupted.
Crash cases are saved for debugging.

Usage:
    python3 scripts/fuzz_decompress.py                      # Run indefinitely
    python3 scripts/fuzz_decompress.py --iterations 1000    # Run N iterations
    python3 scripts/fuzz_decompress.py --help               # Show all options

Requirements:
    - Rust toolchain (cargo)
    - Reference zstd CLI tool (apt install zstd / brew install zstd)
    - Python 3.7+
"""

import argparse
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple


# =============================================================================
# Configuration
# =============================================================================

class Config:
    """Global configuration for the fuzzer."""
    def __init__(self):
        self.workspace_root = self._find_workspace_root()
        self.ruzstd_binary = self.workspace_root / "target" / "release" / "ruzstd-cli"
        self.zstd_binary = "zstd"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="ruzstd_fuzz_"))
        self.crash_dir = Path("/tmp/ruzstd_fuzz_crashes")
        self.min_size = 0
        self.max_size = 10 * 1024 * 1024  # 10 MB
        self.report_interval = 100
        self.verbose = False
        self.seed = None

    def _find_workspace_root(self) -> Path:
        """Find the workspace root by looking for Cargo.toml."""
        script_dir = Path(__file__).parent.resolve()
        workspace = script_dir.parent
        if (workspace / "Cargo.toml").exists():
            return workspace
        return Path.cwd()


# =============================================================================
# Statistics Tracking
# =============================================================================

class FuzzStats:
    """Track fuzzing statistics."""
    def __init__(self):
        self.iterations = 0
        self.total_bytes_tested = 0
        self.total_bytes_compressed = 0
        self.start_time = time.time()
        self.crashes = 0

    def record_iteration(self, original_size: int, compressed_size: int):
        """Record a successful iteration."""
        self.iterations += 1
        self.total_bytes_tested += original_size
        self.total_bytes_compressed += compressed_size

    def record_crash(self):
        """Record a crash."""
        self.crashes += 1

    def get_runtime(self) -> float:
        """Get total runtime in seconds."""
        return time.time() - self.start_time

    def get_rate(self) -> float:
        """Get iterations per second."""
        runtime = self.get_runtime()
        return self.iterations / runtime if runtime > 0 else 0

    def get_avg_compression_ratio(self) -> float:
        """Get average compression ratio."""
        if self.total_bytes_tested == 0:
            return 0
        return self.total_bytes_compressed / self.total_bytes_tested

    def print_summary(self):
        """Print statistics summary."""
        runtime = self.get_runtime()
        rate = self.get_rate()
        avg_ratio = self.get_avg_compression_ratio()

        print(f"\n{'='*70}")
        print(f"Fuzzing Statistics")
        print(f"{'='*70}")
        print(f"  Iterations:          {self.iterations}")
        print(f"  Runtime:             {runtime:.1f}s")
        print(f"  Rate:                {rate:.1f} iter/sec")
        print(f"  Data tested:         {self._format_size(self.total_bytes_tested)}")
        print(f"  Data compressed:     {self._format_size(self.total_bytes_compressed)}")
        print(f"  Avg compression:     {avg_ratio*100:.1f}%")
        print(f"  Crashes:             {self.crashes}")
        print(f"{'='*70}")

    @staticmethod
    def _format_size(size: int) -> str:
        """Format byte size as human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


# =============================================================================
# Data Generation
# =============================================================================

class DataGenerator:
    """Generate various types of test data."""

    @staticmethod
    def random_bytes(size: int) -> bytes:
        """Generate truly random bytes (incompressible)."""
        return bytes(random.getrandbits(8) for _ in range(size))

    @staticmethod
    def compressible_patterns(size: int) -> bytes:
        """Generate data with repeated patterns (compressible)."""
        patterns = [
            b"Hello, World! ",
            b"The quick brown fox jumps over the lazy dog. ",
            b"AAAAAAAAAA",
            b"0123456789" * 10,
            b"\x00" * 64,
            b"\xff" * 64,
            bytes(range(256)),
        ]

        result = bytearray()
        while len(result) < size:
            pattern = random.choice(patterns)
            repeat = random.randint(1, 50)
            result.extend(pattern * repeat)

        return bytes(result[:size])

    @staticmethod
    def highly_compressible(size: int) -> bytes:
        """Generate highly compressible data (mostly zeros)."""
        result = bytearray(size)
        # Scatter some random bytes
        for _ in range(max(1, size // 1000)):
            pos = random.randint(0, size - 1)
            result[pos] = random.randint(0, 255)
        return bytes(result)

    @staticmethod
    def text_like(size: int) -> bytes:
        """Generate text-like data."""
        words = ["the", "be", "to", "of", "and", "a", "in", "that", "have", "I",
                 "it", "for", "not", "on", "with", "he", "as", "you", "do", "at"]

        result = []
        current = 0
        while current < size:
            word = random.choice(words)
            sep = " " if random.random() > 0.1 else random.choice(["\n", "  ", ". "])
            chunk = word + sep
            result.append(chunk)
            current += len(chunk)

        return ''.join(result)[:size].encode('utf-8')

    @staticmethod
    def all_zeros(size: int) -> bytes:
        """Generate all zeros."""
        return b'\x00' * size

    @staticmethod
    def all_ones(size: int) -> bytes:
        """Generate all 0xFF bytes."""
        return b'\xff' * size

    @staticmethod
    def sequential(size: int) -> bytes:
        """Generate sequential bytes (0, 1, 2, ..., 255, 0, 1, ...)."""
        return bytes([i % 256 for i in range(size)])

    @staticmethod
    def generate_random_data(min_size: int, max_size: int) -> bytes:
        """Generate random data of random type and size."""
        size = random.randint(min_size, max_size)

        # Choose random generation strategy
        generators = [
            DataGenerator.random_bytes,
            DataGenerator.compressible_patterns,
            DataGenerator.highly_compressible,
            DataGenerator.text_like,
            DataGenerator.all_zeros,
            DataGenerator.all_ones,
            DataGenerator.sequential,
        ]

        # Weight towards more interesting data
        weights = [0.2, 0.3, 0.2, 0.15, 0.05, 0.05, 0.05]
        generator = random.choices(generators, weights=weights)[0]

        return generator(size)


# =============================================================================
# Utility Functions
# =============================================================================

def run_command(cmd: list, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr.decode()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}")


def compute_hash(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def compare_files_3way(original: Path, zstd_out: Path, ruzstd_out: Path) -> Tuple[bool, str]:
    """
    Compare three files for equality.
    Returns (success, error_message).
    """
    # Check file existence
    for f in [original, zstd_out, ruzstd_out]:
        if not f.exists():
            return False, f"File missing: {f}"

    # Check sizes
    orig_size = original.stat().st_size
    zstd_size = zstd_out.stat().st_size
    ruzstd_size = ruzstd_out.stat().st_size

    if orig_size != zstd_size:
        return False, f"Size mismatch: original({orig_size}) != zstd({zstd_size})"

    if orig_size != ruzstd_size:
        return False, f"Size mismatch: original({orig_size}) != ruzstd({ruzstd_size})"

    # Check hashes
    orig_hash = compute_hash(original)
    zstd_hash = compute_hash(zstd_out)
    ruzstd_hash = compute_hash(ruzstd_out)

    if orig_hash != zstd_hash:
        return False, f"Hash mismatch: original({orig_hash[:16]}...) != zstd({zstd_hash[:16]}...)"

    if orig_hash != ruzstd_hash:
        return False, f"Hash mismatch: original({orig_hash[:16]}...) != ruzstd({ruzstd_hash[:16]}...)"

    return True, "All files identical"


# =============================================================================
# Compression/Decompression
# =============================================================================

def compress_with_zstd(config: Config, input_path: Path, output_path: Path, level: int) -> bool:
    """Compress a file using reference zstd."""
    cmd = [
        config.zstd_binary,
        "-f",  # Force overwrite
        f"-{level}",
        "-o", str(output_path),
        str(input_path)
    ]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    zstd compress failed: {e}")
        return False


def decompress_with_zstd(config: Config, input_path: Path, output_path: Path) -> bool:
    """Decompress a file using reference zstd."""
    cmd = [
        config.zstd_binary,
        "-d",  # Decompress
        "-f",  # Force overwrite
        "-o", str(output_path),
        str(input_path)
    ]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    zstd decompress failed: {e}")
        return False


def decompress_with_ruzstd(config: Config, input_path: Path, output_path: Path) -> bool:
    """Decompress a file using ruzstd."""
    cmd = [
        str(config.ruzstd_binary),
        "decompress",
        str(input_path),
        str(output_path)
    ]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    ruzstd decompress failed: {e}")
        return False


# =============================================================================
# Main Fuzzing Logic
# =============================================================================

def save_crash_case(config: Config, iteration: int, original: Path, compressed: Path,
                    zstd_out: Optional[Path], ruzstd_out: Optional[Path], error: str):
    """Save crash artifacts for debugging."""
    config.crash_dir.mkdir(exist_ok=True)

    crash_subdir = config.crash_dir / f"crash_{iteration}_{int(time.time())}"
    crash_subdir.mkdir(exist_ok=True)

    # Copy files
    shutil.copy2(original, crash_subdir / "original.bin")
    shutil.copy2(compressed, crash_subdir / "compressed.zst")

    if zstd_out and zstd_out.exists():
        shutil.copy2(zstd_out, crash_subdir / "zstd_decompressed.bin")

    if ruzstd_out and ruzstd_out.exists():
        shutil.copy2(ruzstd_out, crash_subdir / "ruzstd_decompressed.bin")

    # Write error info
    with open(crash_subdir / "error.txt", 'w') as f:
        f.write(f"Iteration: {iteration}\n")
        f.write(f"Error: {error}\n")
        f.write(f"Original size: {original.stat().st_size}\n")
        f.write(f"Compressed size: {compressed.stat().st_size}\n")

    print(f"    Crash artifacts saved to: {crash_subdir}")


def fuzz_iteration(config: Config, stats: FuzzStats, iteration: int) -> bool:
    """
    Run a single fuzzing iteration.
    Returns True if successful, False if a bug was found.
    """
    # Generate random test data
    data = DataGenerator.generate_random_data(config.min_size, config.max_size)
    data_size = len(data)

    # Choose random compression level (1-19)
    compression_level = random.randint(1, 19)

    # Create temp files
    original_file = config.temp_dir / f"iter_{iteration}_original.bin"
    compressed_file = config.temp_dir / f"iter_{iteration}_compressed.zst"
    zstd_decompressed = config.temp_dir / f"iter_{iteration}_zstd_out.bin"
    ruzstd_decompressed = config.temp_dir / f"iter_{iteration}_ruzstd_out.bin"

    try:
        # Write original data
        original_file.write_bytes(data)

        # Compress with reference zstd
        if not compress_with_zstd(config, original_file, compressed_file, compression_level):
            print(f"    SKIP: zstd compression failed (iteration {iteration})")
            return True  # Not a bug in ruzstd

        compressed_size = compressed_file.stat().st_size

        # Decompress with reference zstd
        if not decompress_with_zstd(config, compressed_file, zstd_decompressed):
            print(f"    SKIP: zstd decompression failed (iteration {iteration})")
            return True  # Not a bug in ruzstd

        # Decompress with ruzstd
        if not decompress_with_ruzstd(config, compressed_file, ruzstd_decompressed):
            error = "ruzstd decompression failed"
            print(f"\n{'='*70}")
            print(f"BUG FOUND: {error}")
            print(f"{'='*70}")
            print(f"  Iteration:    {iteration}")
            print(f"  Data size:    {data_size} bytes")
            print(f"  Compressed:   {compressed_size} bytes")
            print(f"  Level:        {compression_level}")
            save_crash_case(config, iteration, original_file, compressed_file,
                          zstd_decompressed, ruzstd_decompressed, error)
            return False

        # Compare all three files
        success, message = compare_files_3way(original_file, zstd_decompressed, ruzstd_decompressed)

        if not success:
            error = f"3-way comparison failed: {message}"
            print(f"\n{'='*70}")
            print(f"BUG FOUND: {error}")
            print(f"{'='*70}")
            print(f"  Iteration:    {iteration}")
            print(f"  Data size:    {data_size} bytes")
            print(f"  Compressed:   {compressed_size} bytes")
            print(f"  Level:        {compression_level}")
            save_crash_case(config, iteration, original_file, compressed_file,
                          zstd_decompressed, ruzstd_decompressed, error)
            return False

        # Success!
        stats.record_iteration(data_size, compressed_size)

        if config.verbose or (iteration % config.report_interval == 0):
            ratio = compressed_size / data_size * 100 if data_size > 0 else 0
            print(f"  [{iteration:6d}] OK: {data_size:8d} bytes -> {compressed_size:8d} bytes "
                  f"({ratio:5.1f}%, level {compression_level:2d})")

        return True

    finally:
        # Cleanup temp files
        for f in [original_file, compressed_file, zstd_decompressed, ruzstd_decompressed]:
            if f.exists():
                f.unlink()


def build_ruzstd(config: Config) -> bool:
    """Build the ruzstd-cli binary."""
    print("Building ruzstd-cli...")

    try:
        cmd = ["cargo", "build", "--release", "-p", "ruzstd-cli"]
        run_command(cmd, timeout=600)

        if not config.ruzstd_binary.exists():
            print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
            return False

        print(f"  Build successful: {config.ruzstd_binary}")
        return True

    except Exception as e:
        print(f"  Build failed: {e}")
        return False


def check_prerequisites(config: Config) -> bool:
    """Check that all prerequisites are met."""
    print("Checking prerequisites...")

    # Check for reference zstd
    try:
        result = subprocess.run([config.zstd_binary, "--version"],
                              capture_output=True, check=False)
        version = result.stdout.decode().strip() if result.stdout else "version unknown"
        print(f"  Found zstd: {version}")
    except FileNotFoundError:
        print(f"  ERROR: Reference zstd not found")
        print("  Install with: apt install zstd / brew install zstd")
        return False

    # Check for cargo
    try:
        result = subprocess.run(["cargo", "--version"],
                              capture_output=True, check=False)
        version = result.stdout.decode().strip() if result.stdout else "version unknown"
        print(f"  Found cargo: {version}")
    except FileNotFoundError:
        print("  ERROR: cargo not found")
        print("  Install the Rust toolchain from https://rustup.rs")
        return False

    print("  All prerequisites satisfied")
    return True


def fuzz_loop(config: Config, max_iterations: Optional[int] = None):
    """Run the main fuzzing loop."""
    stats = FuzzStats()
    iteration = 1

    print(f"\nStarting fuzzing loop...")
    print(f"  Workspace:    {config.workspace_root}")
    print(f"  Binary:       {config.ruzstd_binary}")
    print(f"  Temp dir:     {config.temp_dir}")
    print(f"  Crash dir:    {config.crash_dir}")
    print(f"  Size range:   {config.min_size} - {config.max_size} bytes")
    if config.seed is not None:
        print(f"  Random seed:  {config.seed}")
    if max_iterations:
        print(f"  Iterations:   {max_iterations}")
    else:
        print(f"  Iterations:   unlimited (Ctrl+C to stop)")
    print()

    try:
        while True:
            if max_iterations and iteration > max_iterations:
                print(f"\nReached maximum iterations ({max_iterations})")
                break

            success = fuzz_iteration(config, stats, iteration)

            if not success:
                stats.record_crash()
                print(f"\nFuzzing stopped due to bug discovery")
                return False

            iteration += 1

    except KeyboardInterrupt:
        print(f"\n\nFuzzing interrupted by user (Ctrl+C)")

    finally:
        stats.print_summary()

    return True


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Continuous decompression fuzzer for ruzstd",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Run indefinitely
  %(prog)s --iterations 1000            # Run 1000 iterations
  %(prog)s --max-size 1M --verbose      # Test up to 1MB files with verbose output
  %(prog)s --seed 42                    # Use specific random seed
        """
    )

    parser.add_argument(
        "-i", "--iterations",
        type=int,
        default=None,
        help="Maximum number of iterations (default: unlimited)"
    )
    parser.add_argument(
        "--min-size",
        type=str,
        default="0",
        help="Minimum file size (supports K/M/G suffix, default: 0)"
    )
    parser.add_argument(
        "--max-size",
        type=str,
        default="10M",
        help="Maximum file size (supports K/M/G suffix, default: 10M)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "-s", "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility"
    )
    parser.add_argument(
        "--report-interval",
        type=int,
        default=100,
        help="Report progress every N iterations (default: 100)"
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the cargo build step"
    )

    args = parser.parse_args()

    # Parse size arguments
    def parse_size(size_str: str) -> int:
        """Parse size string like '10M' into bytes."""
        size_str = size_str.upper().strip()
        multipliers = {'K': 1024, 'M': 1024**2, 'G': 1024**3}

        for suffix, mult in multipliers.items():
            if size_str.endswith(suffix):
                return int(float(size_str[:-1]) * mult)
        return int(size_str)

    # Setup config
    config = Config()
    config.min_size = parse_size(args.min_size)
    config.max_size = parse_size(args.max_size)
    config.verbose = args.verbose
    config.report_interval = args.report_interval
    config.seed = args.seed

    if config.seed is not None:
        random.seed(config.seed)

    # Print header
    print("="*70)
    print("RUZSTD DECOMPRESSION FUZZER")
    print("="*70)

    try:
        # Check prerequisites
        if not check_prerequisites(config):
            return 1

        # Build project
        if not args.skip_build:
            if not build_ruzstd(config):
                return 1
        else:
            print("\nSkipping build (--skip-build)")
            if not config.ruzstd_binary.exists():
                print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
                return 1

        # Run fuzzing loop
        success = fuzz_loop(config, args.iterations)

        return 0 if success else 1

    finally:
        # Cleanup temp directory
        if config.temp_dir.exists():
            shutil.rmtree(config.temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
