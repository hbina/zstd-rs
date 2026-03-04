#!/usr/bin/env python3
"""
Continuous Decompression Fuzzer for ruzstd (Parallel + Hypothesis)

This script performs continuous parallel fuzzing of the ruzstd decompressor by:
1. Building the ruzstd-cli binary
2. Spawning multiple worker processes simultaneously
3. Each worker uses hypothesis (if installed) for property-based test generation,
   which automatically shrinks failing cases to minimal reproducers
4. Compressing with reference zstd, decompressing with both zstd and ruzstd
5. Verifying 3-way equivalence (original == zstd_decompressed == ruzstd_decompressed)

The fuzzer runs continuously until a failure is found or interrupted.
Crash cases are saved for debugging.

Usage:
    python3 scripts/fuzz_decompress.py                         # Run indefinitely
    python3 scripts/fuzz_decompress.py --iterations 1000       # Run N total iterations
    python3 scripts/fuzz_decompress.py --workers 4             # Use 4 parallel workers
    python3 scripts/fuzz_decompress.py --help                  # Show all options

Requirements:
    - Rust toolchain (cargo)
    - Reference zstd CLI tool (apt install zstd / brew install zstd)
    - Python 3.7+
    - hypothesis (pip install hypothesis)  [optional, enables smarter generation + shrinking]
"""

import argparse
import hashlib
import multiprocessing
import queue
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
        self.workers = multiprocessing.cpu_count()

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
        self.iterations += 1
        self.total_bytes_tested += original_size
        self.total_bytes_compressed += compressed_size

    def record_crash(self):
        self.crashes += 1

    def get_runtime(self) -> float:
        return time.time() - self.start_time

    def get_rate(self) -> float:
        runtime = self.get_runtime()
        return self.iterations / runtime if runtime > 0 else 0

    def get_avg_compression_ratio(self) -> float:
        if self.total_bytes_tested == 0:
            return 0
        return self.total_bytes_compressed / self.total_bytes_tested

    def print_summary(self):
        runtime = self.get_runtime()
        rate = self.get_rate()
        avg_ratio = self.get_avg_compression_ratio()

        print(f"\n{'=' * 70}")
        print(f"Fuzzing Statistics")
        print(f"{'=' * 70}")
        print(f"  Iterations:          {self.iterations}")
        print(f"  Runtime:             {runtime:.1f}s")
        print(f"  Rate:                {rate:.1f} iter/sec")
        print(f"  Data tested:         {self._format_size(self.total_bytes_tested)}")
        print(
            f"  Data compressed:     {self._format_size(self.total_bytes_compressed)}"
        )
        print(f"  Avg compression:     {avg_ratio * 100:.1f}%")
        print(f"  Crashes:             {self.crashes}")
        print(f"{'=' * 70}")

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"


# =============================================================================
# Utility Functions
# =============================================================================


def run_command(cmd: list, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Command failed: {' '.join(str(c) for c in cmd)}\n"
            f"Stderr: {e.stderr.decode()}"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out: {' '.join(str(c) for c in cmd)}")


def compute_hash(filepath: Path) -> str:
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def compare_files_3way(
    original: Path, zstd_out: Path, ruzstd_out: Path
) -> Tuple[bool, str]:
    for f in [original, zstd_out, ruzstd_out]:
        if not f.exists():
            return False, f"File missing: {f}"

    orig_size = original.stat().st_size
    zstd_size = zstd_out.stat().st_size
    ruzstd_size = ruzstd_out.stat().st_size

    if orig_size != zstd_size:
        return False, f"Size mismatch: original({orig_size}) != zstd({zstd_size})"
    if orig_size != ruzstd_size:
        return False, f"Size mismatch: original({orig_size}) != ruzstd({ruzstd_size})"

    orig_hash = compute_hash(original)
    zstd_hash = compute_hash(zstd_out)
    ruzstd_hash = compute_hash(ruzstd_out)

    if orig_hash != zstd_hash:
        return (
            False,
            f"Hash mismatch: original({orig_hash[:16]}...) != zstd({zstd_hash[:16]}...)",
        )
    if orig_hash != ruzstd_hash:
        return (
            False,
            f"Hash mismatch: original({orig_hash[:16]}...) != ruzstd({ruzstd_hash[:16]}...)",
        )

    return True, "All files identical"


# =============================================================================
# Compression/Decompression
# =============================================================================


def compress_with_zstd(
    config: Config, input_path: Path, output_path: Path, level: int
) -> bool:
    cmd = [
        config.zstd_binary,
        "-f",
        f"-{level}",
        "-o",
        str(output_path),
        str(input_path),
    ]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    zstd compress failed: {e}")
        return False


def decompress_with_zstd(config: Config, input_path: Path, output_path: Path) -> bool:
    cmd = [config.zstd_binary, "-d", "-f", "-o", str(output_path), str(input_path)]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    zstd decompress failed: {e}")
        return False


def decompress_with_ruzstd(config: Config, input_path: Path, output_path: Path) -> bool:
    cmd = [str(config.ruzstd_binary), "decompress", str(input_path), str(output_path)]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    ruzstd decompress failed: {e}")
        return False


# =============================================================================
# Crash Handling
# =============================================================================


def save_crash_case(
    config: Config,
    iteration: int,
    worker_id: int,
    original_data: bytes,
    compressed_data: bytes,
    level: int,
    error: str,
) -> Path:
    """Save crash artifacts for debugging. Returns the crash directory."""
    config.crash_dir.mkdir(exist_ok=True)
    crash_subdir = config.crash_dir / f"crash_{iteration}_{int(time.time())}"
    crash_subdir.mkdir(exist_ok=True)

    (crash_subdir / "original.bin").write_bytes(original_data)
    (crash_subdir / "compressed.zst").write_bytes(compressed_data)

    with open(crash_subdir / "error.txt", "w") as f:
        f.write(f"Worker:           {worker_id}\n")
        f.write(f"Iteration:        {iteration}\n")
        f.write(f"Error:            {error}\n")
        f.write(f"Compression level:{level}\n")
        f.write(f"Original size:    {len(original_data)}\n")
        f.write(f"Compressed size:  {len(compressed_data)}\n")

    print(f"    Crash artifacts saved to: {crash_subdir}")
    return crash_subdir


# =============================================================================
# Core test logic (shared by both worker types)
# =============================================================================


def _run_single_test(
    config: Config,
    worker_temp: Path,
    data: bytes,
    level: int,
    prefix: str,
) -> Tuple[bool, int, bytes, str]:
    """
    Compress data, then decompress with both zstd and ruzstd, and compare.

    Returns (success, compressed_size, compressed_bytes, error_message).
    compressed_bytes is populated on failure for crash artifact saving.
    """
    original = worker_temp / f"{prefix}_orig.bin"
    compressed = worker_temp / f"{prefix}_comp.zst"
    zstd_out = worker_temp / f"{prefix}_zstd.bin"
    ruzstd_out = worker_temp / f"{prefix}_ruzstd.bin"

    try:
        original.write_bytes(data)

        if not compress_with_zstd(config, original, compressed, level):
            return True, 0, b"", ""  # skip – not a ruzstd bug

        compressed_size = compressed.stat().st_size
        compressed_bytes = b""  # only read on failure to avoid overhead

        if not decompress_with_zstd(config, compressed, zstd_out):
            return True, compressed_size, b"", ""  # skip

        if not decompress_with_ruzstd(config, compressed, ruzstd_out):
            compressed_bytes = compressed.read_bytes()
            return (
                False,
                compressed_size,
                compressed_bytes,
                "ruzstd decompression failed",
            )

        success, message = compare_files_3way(original, zstd_out, ruzstd_out)
        if not success:
            compressed_bytes = compressed.read_bytes()
            return (
                False,
                compressed_size,
                compressed_bytes,
                f"3-way comparison failed: {message}",
            )

        return True, compressed_size, b"", ""

    finally:
        for f in [original, compressed, zstd_out, ruzstd_out]:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


# =============================================================================
# Worker: hypothesis-based (property-based testing with automatic shrinking)
# =============================================================================


def hypothesis_worker(
    worker_id: int,
    config: Config,
    stop_event,
    result_queue,
    max_per_worker: Optional[int],
):
    """
    Worker process that uses hypothesis for property-based test generation.

    Hypothesis provides:
    - Structured binary data generation with coverage-guided mutation
    - Automatic test case shrinking: when a bug is found, hypothesis reduces
      the input to the smallest case that still triggers the failure
    - An example database that remembers past failures across runs

    The strategy used:
    - st.binary() generates arbitrary byte strings (including edge cases like
      empty inputs, single bytes, repeated patterns)
    - st.integers(1, 19) covers all zstd compression levels
    - Hypothesis explores the space systematically, not just randomly
    """
    try:
        from hypothesis import HealthCheck, assume, given, settings
        from hypothesis import strategies as st
    except ImportError:
        print(f"[Worker {worker_id}] hypothesis not installed, using random fallback")
        random_worker(worker_id, config, stop_event, result_queue, max_per_worker)
        return

    worker_temp = config.temp_dir / f"w{worker_id}"
    worker_temp.mkdir(exist_ok=True, parents=True)

    counter = [0]
    # Mutable cells so the inner function and the outer scope share state.
    # _is_shrinking prevents stop_event from aborting hypothesis's shrink phase.
    last_failure = [None]  # (data, level, compressed_bytes, error)
    is_shrinking = [False]

    # Cap max_size for hypothesis: shrinking very large binaries is extremely slow.
    hyp_max_size = min(config.max_size, 1 * 1024 * 1024)

    @given(
        data=st.binary(min_size=config.min_size, max_size=hyp_max_size),
        level=st.integers(min_value=1, max_value=19),
    )
    @settings(
        max_examples=max_per_worker if max_per_worker else 10**9,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    def property_check(data: bytes, level: int) -> None:
        # Honour stop requests from other workers, but never during shrinking –
        # that would prevent hypothesis from finding the minimal failing case.
        if stop_event.is_set() and not is_shrinking[0]:
            assume(False)

        counter[0] += 1
        prefix = f"w{worker_id}_{counter[0]}"

        success, compressed_size, compressed_bytes, error = _run_single_test(
            config, worker_temp, data, level, prefix
        )

        if not success:
            is_shrinking[0] = True
            last_failure[0] = (data, level, compressed_bytes, error)
            raise AssertionError(error)

        if compressed_size > 0:
            result_queue.put(("ok", len(data), compressed_size))

    try:
        property_check()
    except Exception:
        pass
    finally:
        if last_failure[0] is not None:
            data, level, compressed_bytes, error = last_failure[0]
            result_queue.put(
                ("crash", worker_id, counter[0], data, level, compressed_bytes, error)
            )
            stop_event.set()
        shutil.rmtree(worker_temp, ignore_errors=True)


# =============================================================================
# Worker: random-based (fallback when hypothesis is not installed)
# =============================================================================


def random_worker(
    worker_id: int,
    config: Config,
    stop_event,
    result_queue,
    max_per_worker: Optional[int],
):
    """
    Worker process that uses random data generation.
    Used as fallback when hypothesis is not installed.
    """
    worker_temp = config.temp_dir / f"w{worker_id}"
    worker_temp.mkdir(exist_ok=True, parents=True)

    rng = random.Random()
    if config.seed is not None:
        rng.seed(config.seed + worker_id * 1000)

    iteration = 0
    try:
        while not stop_event.is_set():
            if max_per_worker and iteration >= max_per_worker:
                break

            iteration += 1
            size = rng.randint(config.min_size, config.max_size)
            data = bytes(rng.getrandbits(8) for _ in range(size))
            level = rng.randint(1, 19)
            prefix = f"w{worker_id}_{iteration}"

            success, compressed_size, compressed_bytes, error = _run_single_test(
                config, worker_temp, data, level, prefix
            )

            if not success:
                result_queue.put(
                    (
                        "crash",
                        worker_id,
                        iteration,
                        data,
                        level,
                        compressed_bytes,
                        error,
                    )
                )
                stop_event.set()
                break

            if compressed_size > 0:
                result_queue.put(("ok", len(data), compressed_size))

    finally:
        shutil.rmtree(worker_temp, ignore_errors=True)


# =============================================================================
# Build and Prerequisites
# =============================================================================


def build_ruzstd(config: Config) -> bool:
    print("Building ruzstd-cli...")
    try:
        run_command(["cargo", "build", "--release", "-p", "ruzstd-cli"], timeout=600)
        if not config.ruzstd_binary.exists():
            print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
            return False
        print(f"  Build successful: {config.ruzstd_binary}")
        return True
    except Exception as e:
        print(f"  Build failed: {e}")
        return False


def check_prerequisites(config: Config) -> bool:
    print("Checking prerequisites...")

    try:
        result = subprocess.run(
            [config.zstd_binary, "--version"], capture_output=True, check=False
        )
        version = result.stdout.decode().strip() if result.stdout else "version unknown"
        print(f"  Found zstd: {version}")
    except FileNotFoundError:
        print("  ERROR: Reference zstd not found")
        print("  Install with: apt install zstd / brew install zstd")
        return False

    try:
        result = subprocess.run(
            ["cargo", "--version"], capture_output=True, check=False
        )
        version = result.stdout.decode().strip() if result.stdout else "version unknown"
        print(f"  Found cargo: {version}")
    except FileNotFoundError:
        print("  ERROR: cargo not found")
        print("  Install the Rust toolchain from https://rustup.rs")
        return False

    try:
        import hypothesis

        print(
            f"  Found hypothesis: {hypothesis.__version__} (property-based testing + shrinking enabled)"
        )
    except ImportError:
        print(
            "  hypothesis not found (pip install hypothesis) – using random data generation"
        )

    print("  Prerequisites satisfied")
    return True


# =============================================================================
# Main Fuzzing Loop
# =============================================================================


def fuzz_loop(config: Config, max_iterations: Optional[int] = None) -> bool:
    """Spawn parallel workers and coordinate until done, crashed, or interrupted."""
    stats = FuzzStats()
    num_workers = config.workers

    # Divide iteration budget evenly across workers (None means unlimited).
    max_per_worker = (
        (max_iterations + num_workers - 1) // num_workers if max_iterations else None
    )

    # Decide worker type once so the print below is accurate.
    try:
        import hypothesis  # noqa: F401

        worker_fn = hypothesis_worker
        mode = "hypothesis property-based testing (with automatic shrinking)"
    except ImportError:
        worker_fn = random_worker
        mode = "random data generation"

    print(f"\nStarting parallel fuzzing...")
    print(f"  Workspace:    {config.workspace_root}")
    print(f"  Binary:       {config.ruzstd_binary}")
    print(f"  Temp dir:     {config.temp_dir}")
    print(f"  Crash dir:    {config.crash_dir}")
    print(f"  Workers:      {num_workers}")
    print(f"  Mode:         {mode}")
    print(f"  Size range:   {config.min_size} - {config.max_size} bytes")
    if config.seed is not None:
        print(f"  Random seed:  {config.seed}")
    if max_iterations:
        print(f"  Iterations:   {max_iterations} total (~{max_per_worker} per worker)")
    else:
        print(f"  Iterations:   unlimited (Ctrl+C to stop)")
    print()

    stop_event = multiprocessing.Event()
    result_queue = multiprocessing.Queue()

    workers = []
    for i in range(num_workers):
        p = multiprocessing.Process(
            target=worker_fn,
            args=(i, config, stop_event, result_queue, max_per_worker),
            name=f"fuzz-worker-{i}",
            daemon=True,
        )
        p.start()
        workers.append(p)

    found_crash = False
    last_report_time = time.time()

    try:
        while True:
            # Exit when all workers have finished.
            if all(not p.is_alive() for p in workers):
                break

            try:
                msg = result_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if msg[0] == "ok":
                _, data_size, compressed_size = msg
                stats.record_iteration(data_size, compressed_size)

                now = time.time()
                if config.verbose or (
                    stats.iterations % config.report_interval == 0
                    and now - last_report_time >= 1.0
                ):
                    ratio = compressed_size / data_size * 100 if data_size > 0 else 0
                    print(
                        f"  [{stats.iterations:6d}] OK: {data_size:8d}B -> "
                        f"{compressed_size:8d}B ({ratio:5.1f}%) "
                        f"[{stats.get_rate():.1f} iter/s, {num_workers} workers]"
                    )
                    last_report_time = now

            elif msg[0] == "crash":
                _, worker_id, iteration, data, level, compressed_bytes, error = msg
                stats.record_crash()
                found_crash = True

                print(f"\n{'=' * 70}")
                print(f"BUG FOUND by worker {worker_id}!")
                print(f"{'=' * 70}")
                print(f"  Error:        {error}")
                print(f"  Iteration:    {iteration}")
                print(f"  Data size:    {len(data)} bytes")
                print(f"  Level:        {level}")

                save_crash_case(
                    config, iteration, worker_id, data, compressed_bytes, level, error
                )
                print(f"\nFuzzing stopped due to bug discovery")
                break

    except KeyboardInterrupt:
        print(f"\n\nFuzzing interrupted by user (Ctrl+C)")
        stop_event.set()

    finally:
        stop_event.set()
        for p in workers:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()

        # Drain any remaining ok-messages to keep stats accurate.
        try:
            while True:
                msg = result_queue.get_nowait()
                if msg[0] == "ok":
                    stats.record_iteration(msg[1], msg[2])
        except queue.Empty:
            pass

        stats.print_summary()

    return not found_crash


# =============================================================================
# Main Entry Point
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Parallel decompression fuzzer for ruzstd using hypothesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Run indefinitely with all CPU cores
  %(prog)s --workers 4                  # Use 4 parallel workers
  %(prog)s --iterations 1000            # Run 1000 total iterations
  %(prog)s --max-size 1M --verbose      # Test up to 1MB files with verbose output
  %(prog)s --seed 42                    # Use specific random seed (random worker only)
        """,
    )

    parser.add_argument(
        "-i",
        "--iterations",
        type=int,
        default=None,
        help="Maximum total iterations across all workers (default: unlimited)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=multiprocessing.cpu_count(),
        help=f"Number of parallel worker processes (default: {multiprocessing.cpu_count()})",
    )
    parser.add_argument(
        "--min-size",
        type=str,
        default="0",
        help="Minimum file size (supports K/M/G suffix, default: 0)",
    )
    parser.add_argument(
        "--max-size",
        type=str,
        default="10M",
        help="Maximum file size (supports K/M/G suffix, default: 10M)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "-s",
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility (applies to random worker only)",
    )
    parser.add_argument(
        "--report-interval",
        type=int,
        default=100,
        help="Report progress every N iterations (default: 100)",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the cargo build step",
    )

    args = parser.parse_args()

    def parse_size(size_str: str) -> int:
        size_str = size_str.upper().strip()
        multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
        for suffix, mult in multipliers.items():
            if size_str.endswith(suffix):
                return int(float(size_str[:-1]) * mult)
        return int(size_str)

    config = Config()
    config.min_size = parse_size(args.min_size)
    config.max_size = parse_size(args.max_size)
    config.verbose = args.verbose
    config.report_interval = args.report_interval
    config.seed = args.seed
    config.workers = args.workers

    if config.seed is not None:
        random.seed(config.seed)

    print("=" * 70)
    print("RUZSTD DECOMPRESSION FUZZER (PARALLEL + HYPOTHESIS)")
    print("=" * 70)

    try:
        if not check_prerequisites(config):
            return 1

        if not args.skip_build:
            if not build_ruzstd(config):
                return 1
        else:
            print("\nSkipping build (--skip-build)")
            if not config.ruzstd_binary.exists():
                print(f"  ERROR: Binary not found at {config.ruzstd_binary}")
                return 1

        success = fuzz_loop(config, args.iterations)
        return 0 if success else 1

    finally:
        if config.temp_dir.exists():
            shutil.rmtree(config.temp_dir, ignore_errors=True)


if __name__ == "__main__":
    # Required on macOS / Windows for multiprocessing with spawn start method.
    multiprocessing.freeze_support()
    sys.exit(main())
