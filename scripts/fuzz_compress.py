#!/usr/bin/env python3
"""
Continuous Compression Fuzzer for ruzstd (Parallel + Hypothesis)

This script tests ruzstd's compressor by:
1. Building the ruzstd-cli binary
2. Spawning multiple worker processes simultaneously
3. Each worker uses hypothesis to generate test data
4. Compressing with ruzstd, then decompressing with reference zstd
5. Verifying the decompressed output matches the original

Any of the following counts as a bug:
  - ruzstd compress exits with a non-zero status
  - reference zstd cannot decompress ruzstd's output (invalid compressed data)
  - decompressed data does not match original

Note: ruzstd only implements a subset of compression levels:
  0 = Uncompressed
  1 = Fastest
  2 = Default
  3 = Better
  4 = Best

Usage:
    python3 scripts/fuzz_compress.py                          # Run indefinitely
    python3 scripts/fuzz_compress.py --iterations 1000        # Run N total iterations
    python3 scripts/fuzz_compress.py --workers 4              # Use 4 parallel workers
    python3 scripts/fuzz_compress.py --help                   # Show all options

Requirements:
    - Rust toolchain (cargo)
    - Reference zstd CLI tool (apt install zstd / brew install zstd)
    - Python 3.7+
    - hypothesis (pip install hypothesis)
"""

import argparse
import hashlib
import multiprocessing
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# =============================================================================
# Configuration
# =============================================================================


class Config:
    """Global configuration for the fuzzer."""

    def __init__(self):
        self.workspace_root = self._find_workspace_root()
        self.ruzstd_binary = self.workspace_root / "target" / "release" / "ruzstd-cli"
        self.zstd_binary = "zstd"
        self.temp_dir = Path(tempfile.mkdtemp(prefix="ruzstd_fuzz_compress_"))
        self.crash_dir = Path("/tmp/ruzstd_fuzz_compress_crashes")
        self.min_size = 1024  # 1 KB minimum for meaningful compression testing
        self.max_size = 10 * 1024 * 1024  # 10 MB
        self.report_interval = 100
        self.verbose = False
        self.workers = multiprocessing.cpu_count()

    def _find_workspace_root(self) -> Path:
        script_dir = Path(__file__).parent.resolve()
        workspace = script_dir.parent
        if (workspace / "Cargo.toml").exists():
            return workspace
        return Path.cwd()


# =============================================================================
# Statistics Tracking
# =============================================================================


class FuzzStats:
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

    def print_summary(self):
        runtime = self.get_runtime()
        ratio = self.total_bytes_compressed / self.total_bytes_tested * 100 if self.total_bytes_tested else 0
        print(f"\n{'=' * 70}")
        print(f"Fuzzing Statistics")
        print(f"{'=' * 70}")
        print(f"  Iterations:          {self.iterations}")
        print(f"  Runtime:             {runtime:.1f}s")
        print(f"  Rate:                {self.get_rate():.1f} iter/sec")
        print(f"  Data tested:         {_fmt_size(self.total_bytes_tested)}")
        print(f"  Data compressed:     {_fmt_size(self.total_bytes_compressed)}")
        print(f"  Avg compression:     {ratio:.1f}%")
        print(f"  Crashes:             {self.crashes}")
        print(f"{'=' * 70}")


def _fmt_size(size: int) -> str:
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


# =============================================================================
# Compression / Decompression
# =============================================================================


def compress_with_ruzstd(config: Config, input_path: Path, output_path: Path, level: int) -> bool:
    cmd = [
        str(config.ruzstd_binary),
        "compress",
        "--level", str(level),
        str(input_path),
        str(output_path),
    ]
    try:
        run_command(cmd)
        return True
    except Exception as e:
        if config.verbose:
            print(f"    ruzstd compress failed: {e}")
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

    (crash_subdir / "input.bin").write_bytes(original_data)
    (crash_subdir / "level").write_text(str(level))
    if compressed_data:
        (crash_subdir / "compressed.zst").write_bytes(compressed_data)

    hex_preview = " ".join(f"{b:02x}" for b in original_data[:32])
    if len(original_data) > 32:
        hex_preview += " ..."

    with open(crash_subdir / "error.txt", "w") as f:
        f.write(f"Worker:           {worker_id}\n")
        f.write(f"Iteration:        {iteration}\n")
        f.write(f"Error:            {error}\n")
        f.write(f"Compression level:{level}\n")
        f.write(f"Original size:    {len(original_data)}\n")
        f.write(f"Compressed size:  {len(compressed_data)}\n")
        f.write(f"Input (first 32B):{hex_preview}\n")

    (crash_subdir / "reproduce.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# Run from the ruzstd workspace root: bash reproduce.sh\n"
        'CRASH="$(cd "$(dirname "$0")" && pwd)"\n'
        'LEVEL=$(cat "$CRASH/level")\n'
        'BINARY="${RUZSTD_BINARY:-./target/release/ruzstd-cli}"\n'
        '"$BINARY" compress --level "$LEVEL" "$CRASH/input.bin" "$CRASH/reproduced.zst" \\\n'
        '  && zstd -d -f -o "$CRASH/reproduced.bin" "$CRASH/reproduced.zst" \\\n'
        '  && diff "$CRASH/input.bin" "$CRASH/reproduced.bin" \\\n'
        '  && echo "FIXED" || echo "STILL FAILING"\n'
    )

    print(f"    Crash artifacts saved to: {crash_subdir}")
    return crash_subdir


# =============================================================================
# Regression checker
# =============================================================================


def check_regressions(config: Config) -> bool:
    """
    Re-run all saved crash cases against the current binary.

    Reads each crash directory's input.bin and level, then calls _run_single_test.
    Useful for verifying that a fix actually resolves previously found bugs.

    Returns True if all cases pass (all bugs fixed).
    """
    crash_dirs = (
        sorted(d for d in config.crash_dir.iterdir() if d.is_dir() and d.name.startswith("crash_"))
        if config.crash_dir.exists()
        else []
    )

    if not crash_dirs:
        print(f"No crash cases found in {config.crash_dir}")
        return True

    print(f"Found {len(crash_dirs)} crash case(s) in {config.crash_dir}\n")

    worker_temp = config.temp_dir / "regression"
    worker_temp.mkdir(exist_ok=True, parents=True)

    passed = failed = skipped = 0
    try:
        for i, crash_dir in enumerate(crash_dirs, 1):
            input_file = crash_dir / "input.bin"
            level_file = crash_dir / "level"

            if not input_file.exists() or not level_file.exists():
                print(f"  [{i}/{len(crash_dirs)}] SKIP  {crash_dir.name}: missing input.bin or level")
                skipped += 1
                continue

            data = input_file.read_bytes()
            level = int(level_file.read_text().strip())
            success, _, _, error = _run_single_test(config, worker_temp, data, level, f"reg_{i}")

            if success:
                print(f"  [{i}/{len(crash_dirs)}] FIXED {crash_dir.name}  (level={level}, {len(data)}B)")
                passed += 1
            else:
                print(f"  [{i}/{len(crash_dirs)}] FAIL  {crash_dir.name}: {error}")
                failed += 1
    finally:
        shutil.rmtree(worker_temp, ignore_errors=True)

    print(f"\nResults: {passed} fixed, {failed} still failing, {skipped} skipped")
    return failed == 0


# =============================================================================
# Core test logic
# =============================================================================


def _run_single_test(
    config: Config,
    worker_temp: Path,
    data: bytes,
    level: int,
    prefix: str,
) -> Tuple[bool, int, bytes, str]:
    """
    Compress data with ruzstd, decompress with reference zstd, compare to original.

    Returns (success, compressed_size, compressed_bytes, error_message).
    compressed_bytes is only populated on failure.
    """
    original = worker_temp / f"{prefix}_orig.bin"
    compressed = worker_temp / f"{prefix}_comp.zst"
    decompressed = worker_temp / f"{prefix}_decomp.bin"

    try:
        original.write_bytes(data)

        if not compress_with_ruzstd(config, original, compressed, level):
            return False, 0, b"", f"ruzstd compression failed (level {level})"

        compressed_size = compressed.stat().st_size

        if not decompress_with_zstd(config, compressed, decompressed):
            return (
                False,
                compressed_size,
                compressed.read_bytes(),
                "reference zstd could not decompress ruzstd output (invalid compressed data)",
            )

        # Compare original vs round-tripped data
        if not decompressed.exists():
            return False, compressed_size, compressed.read_bytes(), "decompressed file missing"

        if original.stat().st_size != decompressed.stat().st_size:
            orig_sz = original.stat().st_size
            decomp_sz = decompressed.stat().st_size
            return (
                False,
                compressed_size,
                compressed.read_bytes(),
                f"size mismatch after round-trip: original={orig_sz} decompressed={decomp_sz}",
            )

        if compute_hash(original) != compute_hash(decompressed):
            return (
                False,
                compressed_size,
                compressed.read_bytes(),
                "hash mismatch after round-trip: decompressed data differs from original",
            )

        return True, compressed_size, b"", ""

    finally:
        for f in [original, compressed, decompressed]:
            f.unlink(missing_ok=True)


# =============================================================================
# Worker
# =============================================================================


def worker(
    worker_id: int,
    config: Config,
    stop_event,
    result_queue,
    max_per_worker: Optional[int],
):
    """
    Worker process using hypothesis for property-based test generation.

    Compresses with ruzstd, decompresses with reference zstd, and verifies
    the output matches the original input.

    The _is_shrinking flag prevents stop_event from aborting hypothesis's
    shrink phase, which would produce a larger (non-minimal) crash artifact.
    """
    worker_temp = config.temp_dir / f"w{worker_id}"
    worker_temp.mkdir(exist_ok=True, parents=True)

    counter = [0]
    last_failure = [None]  # (data, level, compressed_bytes, error)
    is_shrinking = [False]

    # Cap max_size: shrinking very large binaries is extremely slow.
    hyp_max_size = min(config.max_size, 1 * 1024 * 1024)

    @given(
        data=st.binary(min_size=config.min_size, max_size=hyp_max_size),
        level=st.integers(min_value=0, max_value=1),
    )
    @settings(
        max_examples=max_per_worker if max_per_worker else 10**9,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    def property_check(data: bytes, level: int) -> None:
        # Honour external stop requests, but never during shrinking.
        if stop_event.is_set() and not is_shrinking[0]:
            assume(False)

        counter[0] += 1
        success, compressed_size, compressed_bytes, error = _run_single_test(
            config, worker_temp, data, level, f"w{worker_id}_{counter[0]}"
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
            result_queue.put(("crash", worker_id, counter[0], data, level, compressed_bytes, error))
            stop_event.set()
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
        result = subprocess.run(["cargo", "--version"], capture_output=True, check=False)
        version = result.stdout.decode().strip() if result.stdout else "version unknown"
        print(f"  Found cargo: {version}")
    except FileNotFoundError:
        print("  ERROR: cargo not found")
        print("  Install the Rust toolchain from https://rustup.rs")
        return False

    import hypothesis
    print(f"  Found hypothesis: {hypothesis.__version__}")

    print("  Prerequisites satisfied")
    return True


# =============================================================================
# Main Fuzzing Loop
# =============================================================================


def fuzz_loop(config: Config, max_iterations: Optional[int] = None) -> bool:
    """Spawn parallel workers and coordinate until done, crashed, or interrupted."""
    stats = FuzzStats()
    num_workers = config.workers
    max_per_worker = (
        (max_iterations + num_workers - 1) // num_workers if max_iterations else None
    )

    print(f"\nStarting parallel compression fuzzing...")
    print(f"  Workspace:    {config.workspace_root}")
    print(f"  Binary:       {config.ruzstd_binary}")
    print(f"  Temp dir:     {config.temp_dir}")
    print(f"  Crash dir:    {config.crash_dir}")
    print(f"  Workers:      {num_workers}")
    print(f"  Size range:   {config.min_size} - {config.max_size} bytes")
    print(f"  Levels:       0 (Uncompressed), 1 (Fastest)")
    if max_iterations:
        print(f"  Iterations:   {max_iterations} total (~{max_per_worker} per worker)")
    else:
        print(f"  Iterations:   unlimited (Ctrl+C to stop)")
    print()

    stop_event = multiprocessing.Event()
    result_queue = multiprocessing.Queue()

    workers = [
        multiprocessing.Process(
            target=worker,
            args=(i, config, stop_event, result_queue, max_per_worker),
            name=f"fuzz-worker-{i}",
            daemon=True,
        )
        for i in range(num_workers)
    ]
    for p in workers:
        p.start()

    found_crash = False
    last_report_time = time.time()

    try:
        while True:
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

                save_crash_case(config, iteration, worker_id, data, compressed_bytes, level, error)
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
        description="Parallel compression fuzzer for ruzstd using hypothesis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Run indefinitely with all CPU cores
  %(prog)s --workers 4                  # Use 4 parallel workers
  %(prog)s --iterations 1000            # Run 1000 total iterations
  %(prog)s --max-size 1M --verbose      # Test up to 1MB files with verbose output
        """,
    )

    parser.add_argument(
        "-i", "--iterations",
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
        default="1K",
        help="Minimum file size (supports K/M/G suffix, default: 1K)",
    )
    parser.add_argument(
        "--max-size",
        type=str,
        default="10M",
        help="Maximum file size (supports K/M/G suffix, default: 10M)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
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
    parser.add_argument(
        "--check-crashes",
        action="store_true",
        help=f"Replay all saved crash cases from the crash dir and report which are fixed",
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
    config.workers = args.workers

    print("=" * 70)
    print("RUZSTD COMPRESSION FUZZER (PARALLEL + HYPOTHESIS)")
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

        if args.check_crashes:
            success = check_regressions(config)
        else:
            success = fuzz_loop(config, args.iterations)
        return 0 if success else 1

    finally:
        if config.temp_dir.exists():
            shutil.rmtree(config.temp_dir, ignore_errors=True)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
