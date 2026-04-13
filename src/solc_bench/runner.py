"""Run solc and collect metrics.

Uses perf stat for hardware counters when available, falls back to
os.wait4() rusage for timing and memory. Parses solc standard-json
output for bytecode size.
"""

import json
import os
import shutil
import statistics
import subprocess
import time

METRICS = {
    "cpu_time": ("CPU time (user + system) via os.wait4()", "seconds"),
    "wall_time": ("Wall clock time via time.monotonic()", "seconds"),
    "peak_rss": ("Peak resident set size via rusage.ru_maxrss", "MiB"),
    "instructions": ("Hardware instruction count via perf stat", "count"),
    "cycles": ("CPU cycle count via perf stat", "count"),
    "bytecode_size": ("Total bytecode size from solc standard-json output", "bytes"),
}


def perf_available():
    """Check if perf stat is available and usable."""
    if not shutil.which("perf"):
        return False
    try:
        result = subprocess.run(
            ["perf", "stat", "-e", "instructions", "true"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except OSError:
        return False


def run_benchmark(solc, input_file, iterations, use_perf=None, on_iteration=None):
    """Run solc N times, collect all available metrics, return aggregated results.

    If use_perf is None, auto-detect perf availability.
    on_iteration is an optional callback(iteration_index, metrics) for progress reporting.
    """
    if use_perf is None:
        use_perf = perf_available()

    samples = []
    for i in range(iterations):
        metrics = run_once(solc, input_file, use_perf)

        if on_iteration:
            on_iteration(i, metrics)

        if metrics.get("exit_code", 0) != 0:
            break

        samples.append(metrics)

    if not samples:
        return None

    return aggregate(samples)


def run_once(solc, input_file, use_perf):
    """Run solc once on input_file, collect all available metrics."""
    if use_perf:
        metrics, stdout = invoke_with_perf(solc, input_file)
    else:
        metrics, stdout = invoke_with_rusage(solc, input_file)

    metrics.update(parse_solc_output(stdout))
    return metrics


def invoke_with_rusage(solc, input_file):
    """Run solc via subprocess + os.wait4(), collect timing and memory.

    Returns (metrics_dict, stdout_bytes).
    See https://docs.python.org/3/library/os.html#os.wait4
    """
    with open(input_file) as stdin_f:
        wall_start = time.monotonic()

        proc = subprocess.Popen(
            [solc, "--standard-json"],
            stdin=stdin_f,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        stdout = proc.stdout.read()
        _, status, rusage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)

        wall_time = time.monotonic() - wall_start

    metrics = {
        "cpu_time": rusage.ru_utime + rusage.ru_stime,
        "wall_time": wall_time,
        "peak_rss": rusage.ru_maxrss / 1024,  # KiB -> MiB on Linux
        "exit_code": proc.returncode,
    }

    return metrics, stdout


def invoke_with_perf(solc, input_file):
    """Run solc wrapped with perf stat, collect hardware counters + rusage.

    Returns (metrics_dict, stdout_bytes).
    """
    # perf stat writes counters to stderr. We capture both solc's stdout
    # (for output parsing) and perf's stderr (for counter values).
    with open(input_file) as stdin_f:
        wall_start = time.monotonic()

        proc = subprocess.Popen(
            [
                "perf",
                "stat",
                "-e",
                "instructions,cycles",
                "-x",
                ";",
                "--",
                solc,
                "--standard-json",
            ],
            stdin=stdin_f,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout = proc.stdout.read()
        perf_stderr = proc.stderr.read()
        _, status, rusage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)

        wall_time = time.monotonic() - wall_start

    metrics = {
        "cpu_time": rusage.ru_utime + rusage.ru_stime,
        "wall_time": wall_time,
        "peak_rss": rusage.ru_maxrss / 1024,
        "exit_code": proc.returncode,
    }

    metrics.update(parse_perf_output(perf_stderr.decode(errors="replace")))

    return metrics, stdout


def parse_perf_output(perf_text):
    """Parse perf stat -x ';' output for instructions and cycles.

    Format: value;unit;event-name;...
    Examples:
      1234567;;instructions;...
      1234567;;cpu_core/instructions/u;...   (hybrid CPUs)
    On hybrid CPUs, perf reports separate
    counters per core type. We take the non-zero one.
    """
    metrics = {}

    for line in perf_text.splitlines():
        parts = line.split(";")
        if len(parts) < 3:
            continue

        value_str = parts[0].strip()
        event = parts[2].strip()

        if not value_str or value_str == "<not counted>":
            continue

        try:
            value = int(value_str)
        except ValueError:
            continue

        if value == 0:
            continue

        if "instructions" in event:
            metrics["instructions"] = value
        elif "cycles" in event:
            metrics["cycles"] = value

    return metrics


def parse_solc_output(stdout):
    """Parse solc standard-json output for bytecode size and error count."""
    metrics = {}

    try:
        output = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return metrics

    # Count compilation errors.
    errors = [e for e in output.get("errors", []) if e.get("severity") == "error"]
    metrics["errors"] = len(errors)

    # Sum bytecode size across all contracts.
    total_size = 0
    contracts = output.get("contracts", {})
    for source_contracts in contracts.values():
        for contract_data in source_contracts.values():
            evm = contract_data.get("evm", {})
            bytecode = evm.get("bytecode", {})
            obj = bytecode.get("object", "")
            if obj and obj != "0x":
                # Hex string, 2 chars per byte.
                total_size += len(obj) // 2

    if total_size > 0:
        metrics["bytecode_size"] = total_size

    return metrics


def aggregate(samples):
    """Aggregate multiple samples into per-metric stats (median, mean, values)."""
    if not samples:
        return {}

    # Collect all metric names across samples (excluding exit_code and errors).
    all_keys = set()
    for s in samples:
        all_keys.update(s.keys())
    all_keys.discard("exit_code")
    all_keys.discard("errors")

    result = {}
    for key in sorted(all_keys):
        values = [s[key] for s in samples if key in s]
        if not values:
            continue
        result[key] = {
            "values": values,
            "median": statistics.median(values),
            "mean": statistics.mean(values),
        }
        if len(values) > 1:
            result[key]["stddev"] = statistics.stdev(values)

    # Errors come from the last sample (they're the same across runs).
    if "errors" in samples[-1]:
        result["errors"] = samples[-1]["errors"]

    return result


def get_solc_version(solc):
    """Get the version string from a solc binary.

    Raises FileNotFoundError, PermissionError, or ValueError if the
    binary is missing, not executable, or not a valid solc.
    """
    result = subprocess.run(
        [solc, "--version"],
        capture_output=True,
        text=True,
    )

    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split("Version: ", 1)[1].strip()

    raise ValueError(f"not a solc binary: {solc}")
