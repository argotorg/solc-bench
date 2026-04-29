"""Benchmark solc: low-level invocation and high-level orchestration.

Benchmark class: runs solc with perf stat or rusage, collects metrics.
BenchmarkSuite class: orchestrates benchmarks across pipelines and inputs.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from solc_bench.config import (
    DEFAULT_PIPELINES,
    find_input_file,
    load_benchmarks,
)
from solc_bench.metrics import aggregate
from solc_bench import reporter
from solc_bench.solidity import (
    get_solc_version,
    override_json_settings,
    parse_solc_output,
    resolve_solc_settings,
    wrap_sol_as_standard_json,
)


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


class Benchmark:
    """Runs solc and collects all metrics."""

    def __init__(self, solc, output_parser=None, use_perf=None):
        self.solc = solc
        self.output_parser = output_parser
        self.use_perf = use_perf if use_perf is not None else perf_available()

    def run(self, input_file, iterations):
        """Run solc N times, return aggregated metrics or None on failure."""
        samples = []
        for i in range(iterations):
            metrics = self.run_once(input_file)

            if metrics["exit_code"] != 0:
                break

            if i > 0:
                print(".", file=sys.stderr, end="", flush=True)
            samples.append(metrics)

        if not samples:
            return None

        return aggregate(samples)

    def run_once(self, input_file):
        """Run solc once, collect system metrics and parse output."""
        if self.use_perf:
            metrics, stdout = self.invoke_with_perf(input_file)
        else:
            metrics, stdout = self.invoke_with_rusage(input_file)

        if self.output_parser:
            metrics.update(self.output_parser(stdout))

        return metrics

    def invoke_with_rusage(self, input_file):
        """Run solc via subprocess + os.wait4().

        Returns (metrics_dict, stdout_bytes).
        See https://docs.python.org/3/library/os.html#os.wait4
        """
        with open(input_file, encoding="utf-8") as stdin_f:
            wall_start = time.monotonic()

            proc = subprocess.Popen(
                [self.solc, "--standard-json"],
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

    def invoke_with_perf(self, input_file):
        """Run solc wrapped with perf stat.

        Returns (metrics_dict, stdout_bytes).
        """
        with open(input_file, encoding="utf-8") as stdin_f:
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
                    self.solc,
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


class BenchmarkSuite:
    """Orchestrates benchmarks across pipelines and inputs."""

    def __init__(self, solc, iterations, output_dir):
        self.solc_version = get_solc_version(solc)
        self.benchmark = Benchmark(solc, output_parser=parse_solc_output)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.iterations = iterations
        self.results = {}

    def run_one(self, input_file, name, pipeline, solc_settings):
        """Run one benchmark and record the result."""
        reporter.benchmark_start(name, pipeline, solc_settings)
        result = self.benchmark.run(input_file, self.iterations)

        error_log = None
        if result and result.get("errors", 0):
            error_messages = result.pop("error_messages", [])
            if error_messages:
                log_path = self.output_dir / f"{name}-{pipeline}.errors.log"
                log_path.write_text("\n".join(error_messages), encoding="utf-8")
                error_log = str(log_path)

        reporter.benchmark_done(result, error_log)

        if result and not result.get("errors", 0):
            self.results.setdefault(name, {})[pipeline] = result

    def run_from_input(self, input_file, pipeline, no_optimize):
        """Run benchmark on a single .sol or .json input file.

        pipeline is a pipeline name (str) or None for all pipelines.
        """
        name = Path(input_file).stem
        pipelines = [pipeline] if pipeline else DEFAULT_PIPELINES

        for p in pipelines:
            solc_settings = resolve_solc_settings(p, no_optimize)

            if input_file.endswith(".sol"):
                ctx = wrap_sol_as_standard_json(input_file, solc_settings)
            else:
                ctx = override_json_settings(input_file, solc_settings)

            with ctx as tmp_file:
                self.run_one(tmp_file, name, p, solc_settings)

    def run_from_benchmarks(self, benchmark_dir, only, pipeline, no_optimize):
        """Run configured benchmarks from benchmarks.toml.

        pipeline is a pipeline name (str) or None for per-project defaults.
        """
        benchmarks = load_benchmarks(benchmark_dir)
        selected = only.split(",") if only else None

        print("\nRunning benchmarks...", file=sys.stderr)

        for name, config in benchmarks.items():
            if selected and name not in selected:
                continue

            input_file = find_input_file(benchmark_dir, name)
            if not input_file:
                expected = Path(benchmark_dir) / f"{name}.json"
                print(
                    f"  {name}: input file not found at {expected}, skipping",
                    file=sys.stderr,
                )
                source = config.get("source")
                version = config.get("version")
                if source:
                    suffix = f" ({version})" if version else ""
                    print(f"    source: {source}{suffix}", file=sys.stderr)
                print(
                    f"    generate it with: solc-bench extract --solc <solc> "
                    f"--project <path-to-project> --output-dir {benchmark_dir}",
                    file=sys.stderr,
                )
                continue

            if pipeline:
                pipelines = [pipeline]
            else:
                pipelines = config.get("pipelines", DEFAULT_PIPELINES)

            for p in pipelines:
                solc_settings = resolve_solc_settings(p, no_optimize)
                with override_json_settings(input_file, solc_settings) as tmp_file:
                    self.run_one(tmp_file, name, p, solc_settings)

    def write_results(self, stdout=False):
        """Write results JSON to output dir, optionally also to stdout."""
        if not self.results:
            print("\nNo results to write.", file=sys.stderr)
            return

        output = reporter.build_result_json(
            self.results, self.solc_version, self.iterations
        )
        result_path = self.output_dir / "results.json"
        reporter.write_result_json(output, result_path, stdout=stdout)


def parse_perf_output(perf_text):
    """Parse perf stat -x ';' output for instructions and cycles.

    On hybrid CPUs, perf reports separate counters per core type.
    Accumulates values across all core types.
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
            metrics["instructions"] = metrics.get("instructions", 0) + value
        elif "cycles" in event:
            metrics["cycles"] = metrics.get("cycles", 0) + value

    return metrics
