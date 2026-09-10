import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

from solc_bench.config import (
    DEFAULT_PIPELINES,
    DEFAULT_RESULT_FILENAME,
    load_benchmarks,
)
from solc_bench.gas import ensure_project, run_gas_benchmark
from solc_bench.gas_fixture import run_replay_and_collect
from solc_bench.fixture_bytecode import (
    ensure_dummy_library_account,
    extract_deployed_bytecode,
    maximize_gas_headroom,
    merge_gas_bench_output_selection,
    merge_target_libraries,
    swap_contract_code,
)
from solc_bench.metrics import aggregate
from solc_bench import reporter
from solc_bench.solidity import (
    get_solc_version,
    metrics_from_standard_json_output,
    override_json_settings,
    resolve_solc_settings,
    wrap_sol_as_standard_json,
)
from solc_bench.targets_config import read_targets_config


def perf_available():
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


def _ru_maxrss_mib(ru_maxrss):
    """Normalize resource.ru_maxrss to MiB.

    Linux reports ru_maxrss in KiB, while macOS reports it in bytes.
    """
    if sys.platform == "darwin":
        return ru_maxrss / (1024 * 1024)
    return ru_maxrss / 1024


class Benchmark:
    """Runs solc and collects all metrics."""

    def __init__(self, solc, use_perf=None):
        self.solc = solc
        self.use_perf = use_perf if use_perf is not None else perf_available()
        # Kept around so gas-fixture benchmarking can reuse this compile's
        # bytecode instead of recompiling.
        self.last_output = None

    def run(self, input_file, iterations):
        """Run solc N times, return aggregated metrics or None on failure."""
        samples = []
        counter_len = 0
        self.last_output = None
        for i in range(iterations):
            metrics = self.run_once(input_file)

            if metrics["exit_code"] != 0:
                break

            counter = f" [{i + 1}/{iterations}]"
            print("\b" * counter_len + counter, file=sys.stderr, end="", flush=True)
            counter_len = len(counter)
            samples.append(metrics)

            # Skip remaining iterations: same input, same errors.
            if metrics.get("errors", 0) > 0:
                break

        if not samples:
            return None

        return aggregate(samples)

    def run_once(self, input_file):
        """Run solc once, collect system metrics and parse output."""
        metrics, stdout = self.invoke_solc(input_file)
        try:
            output = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            output = None
        if self.last_output is None:
            self.last_output = output
        metrics.update(metrics_from_standard_json_output(output) if output is not None else {})
        return metrics

    def invoke_solc(self, input_file):
        """Run solc via subprocess + os.wait4(), optionally wrapped in perf stat.

        Returns (metrics_dict, stdout_bytes).
        See https://docs.python.org/3/library/os.html#os.wait4
        """
        cmd = [self.solc, "--standard-json"]
        if self.use_perf:
            cmd = ["perf", "stat", "-e", "instructions,cycles", "-x", ";", "--", *cmd]

        stderr = subprocess.PIPE if self.use_perf else subprocess.DEVNULL

        with open(input_file, encoding="utf-8") as f:
            wall_start = time.monotonic()

            proc = subprocess.Popen(
                cmd, stdin=f, stdout=subprocess.PIPE, stderr=stderr,
            )

            stdout = proc.stdout.read()
            perf_stderr = proc.stderr.read() if self.use_perf else None
            _, status, rusage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)

            wall_time = time.monotonic() - wall_start

        metrics = {
            "cpu_time": rusage.ru_utime + rusage.ru_stime,
            "wall_time": wall_time,
            "peak_rss": _ru_maxrss_mib(rusage.ru_maxrss),
            "exit_code": proc.returncode,
        }

        if self.use_perf:
            metrics.update(parse_perf_output(perf_stderr.decode(errors="replace")))

        return metrics, stdout


class BenchmarkSuite:
    """Orchestrates benchmarks across pipelines and inputs."""

    def __init__(
        self,
        solc,
        iterations,
        output_dir,
        keep_inputs=False,
        output_file=None,
        evmone_statetest=None,
    ):
        self.solc_version = get_solc_version(solc)
        self.benchmark = Benchmark(solc)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = Path(output_file) if output_file else None
        self.iterations = iterations
        self.keep_inputs = keep_inputs
        # Path to evmone-statetest, or None to skip gas-fixture benchmarking.
        self.evmone_statetest = evmone_statetest
        self.results = {}

    @property
    def use_perf(self):
        return self.benchmark.use_perf

    def run_pipeline(
        self, input_file, name, pipeline, solc_settings, gas_project_dir=None, gas_bench_config=None,
    ):
        """Run one pipeline, record the result if no errors. Optionally run gas."""
        if self.keep_inputs:
            kept_input = self.output_dir / "inputs" / f"{name}.{pipeline}.json"
            kept_input.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(input_file, kept_input)
        reporter.benchmark_start(name, pipeline, solc_settings)
        result = self.benchmark.run(input_file, self.iterations)

        has_errors = bool(result and result.get("errors", 0))
        error_log = self._write_error_log(result, name, pipeline) if has_errors else None

        reporter.benchmark_done(result, error_log)

        if not result or has_errors:
            return

        if gas_project_dir is not None:
            self._run_gas(result, gas_project_dir, name, pipeline, solc_settings)

        if gas_bench_config is not None:
            self._run_gas_fixtures(result, name, pipeline, gas_bench_config)

        self.results.setdefault(name, {})[pipeline] = result

    def _run_gas(self, result, project_dir, name, pipeline, solc_settings):
        """Run gas benchmark, merge metrics into result. Mutates result on success."""
        if pipeline == "ir-ssacfg":
            # TODO: forge doesn't support --viaSSACFG yet, skip gas for ir-ssacfg
            return
        via_ir = solc_settings.get("viaIR", False)
        log_path = self.output_dir / f"{name}-{pipeline}.gas.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print("    [gas] running...", file=sys.stderr, end="", flush=True)
        gas, had_failures = run_gas_benchmark(
            self.benchmark.solc, project_dir, via_ir, log_path=log_path,
        )
        if gas is None:
            print(
                f"\r    [gas] WARNING: no gas data produced, see {log_path}",
                file=sys.stderr,
            )
            return
        suffix = f" (some tests failed, see {log_path})" if had_failures else ""
        print(
            f"\r    [gas] deployment={gas['deployment_gas']:,} "
            f"method={gas['method_gas']:,}{suffix}",
            file=sys.stderr,
        )
        functions = gas.pop("functions", None)
        for key, val in gas.items():
            result[key] = {"values": [val], "median": val, "mean": val}
        if functions:
            result["functions"] = functions

    def _gas_bench_config(self, benchmark_dir, config, input_file):
        """This benchmark's gas-bench config: `(fixture_dir, targets,
        extra_settings)` from its `gas-bench-fixtures` group, or None if
        unconfigured or evmone-statetest wasn't given."""
        if self.evmone_statetest is None:
            return None
        group_name = config.get("gas-bench-fixtures")
        if not group_name:
            return None
        fixture_dir = Path(benchmark_dir) / "gas" / group_name
        targets_toml = fixture_dir / "targets.toml"
        if not targets_toml.is_file():
            return None
        targets = read_targets_config(targets_toml)
        if not targets:
            return None
        source_json = json.loads(Path(input_file).read_text())
        extra_settings = {
            "outputSelection": merge_gas_bench_output_selection(
                source_json.get("settings", {}).get("outputSelection")
            ),
            "libraries": merge_target_libraries(source_json.get("sources", {}), targets),
        }
        return fixture_dir, targets, extra_settings

    def _run_gas_fixtures(self, result, name, pipeline, gas_bench_config):
        """Swap this pipeline's own compiled bytecode into each fixture in
        fixture_dir and replay it. Merges a summed gas_used and per-fixture
        breakdown into result, mutating it in place on success."""
        fixture_dir, own_targets = gas_bench_config
        standard_json_output = self.benchmark.last_output
        if standard_json_output is None or not own_targets:
            return

        fixture_paths = sorted(fixture_dir.glob("*.json"))
        if not fixture_paths:
            return

        print("    [gas] running fixtures...", file=sys.stderr, end="", flush=True)
        total_gas = 0
        functions = {}
        for fixture_path in fixture_paths:
            fixture = json.loads(fixture_path.read_text())
            (test_name,) = fixture.keys()
            pre_addresses = {a.lower() for a in fixture[test_name]["pre"]}
            for target in own_targets:
                if target["address"].lower() not in pre_addresses:
                    continue
                # Keyed by filename, not test_name - two fixtures can share
                # a test_name but filenames are always unique.
                func_key = f"{target['contract_name']}@{target['address'][2:10]}.{fixture_path.stem}"
                try:
                    code = extract_deployed_bytecode(
                        standard_json_output, target["contract_name"], target["source_name"],
                        target["immutables"],
                    )
                    swapped = ensure_dummy_library_account(deepcopy(fixture))
                    swapped = swap_contract_code(swapped, target["address"], code)
                    swapped = maximize_gas_headroom(swapped)
                    log_label = f"{name}/{func_key} [{pipeline}]"
                    metrics = run_replay_and_collect(swapped, self.evmone_statetest, True, log_label)
                except (ValueError, RuntimeError) as e:
                    print(f"\n    [gas] {fixture_path.name}: {e}", file=sys.stderr)
                    continue
                gas_used = metrics["gas_used"]["median"]
                total_gas += gas_used
                functions[func_key] = metrics["gas_used"]

        if not functions:
            print("\r    [gas] WARNING: no fixture matched any applicable target", file=sys.stderr)
            return
        print(f"\r    [gas] fixtures gas_used={total_gas:,}", file=sys.stderr)
        result["gas_used"] = {"values": [total_gas], "median": total_gas, "mean": total_gas}
        result.setdefault("functions", {}).update(functions)

    def _write_error_log(self, result, name, pipeline):
        error_messages = result.pop("error_messages", [])
        if not error_messages:
            return None
        log_path = self.output_dir / f"{name}-{pipeline}.errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(error_messages), encoding="utf-8")
        return str(log_path)

    def run_file(self, input_file, pipelines, no_optimize):
        """Run benchmark on a single .sol or .json input file.

        pipelines is a list of pipeline names, or None for all pipelines.
        """
        name = Path(input_file).stem
        pipeline_runs = self._pipeline_runs(pipelines or DEFAULT_PIPELINES, no_optimize)

        for label, solc_settings, ethdebug in pipeline_runs:
            if input_file.endswith(".sol"):
                ctx = wrap_sol_as_standard_json(input_file, solc_settings, ethdebug)
            else:
                ctx = override_json_settings(input_file, solc_settings, ethdebug)

            with ctx as tmp_file:
                self.run_pipeline(tmp_file, name, label, solc_settings)

    def run_suite(
        self,
        benchmark_dir,
        only,
        pipelines,
        no_optimize,
        tags=None,
    ):
        """Run configured benchmarks from benchmarks.toml.

        pipelines is a list of pipeline names, or None for per-project defaults.
        tags is a list of lowercase tag names; benchmarks must carry at
        least one of them to be selected (combined with `only` via AND).
        """
        benchmarks = load_benchmarks(benchmark_dir)
        selected = only.split(",") if only else None
        tag_set = set(tags) if tags else None

        print("\nRunning benchmarks...", file=sys.stderr)

        matched_any = False
        for name, config in benchmarks.items():
            if selected and name not in selected:
                continue
            if tag_set and not tag_set & set(config.get("tags", [])):
                continue
            matched_any = True

            input_file = Path(benchmark_dir) / f"{name}.json"
            if not input_file.is_file():
                reporter.missing_input_file(
                    name,
                    input_file,
                    config.get("source"),
                    config.get("version"),
                    benchmark_dir,
                )
                continue

            bench_pipelines = pipelines or config.get("pipelines", DEFAULT_PIPELINES)

            gas_project_dir = None
            if config.get("gas"):
                try:
                    gas_project_dir = ensure_project(
                        benchmark_dir,
                        name,
                        config.get("source"),
                        config.get("version"),
                    )
                except (subprocess.CalledProcessError, RuntimeError) as e:
                    print(
                        f"  {name}: skipping gas: {e}",
                        file=sys.stderr,
                    )

            gas_bench_config = self._gas_bench_config(benchmark_dir, config, input_file)

            for label, solc_settings, ethdebug in self._pipeline_runs(
                bench_pipelines,
                no_optimize,
            ):
                if gas_bench_config is not None and not ethdebug:
                    fixture_dir, own_targets, extra_settings = gas_bench_config
                    solc_settings = {**solc_settings, **extra_settings}

                with override_json_settings(
                    input_file,
                    solc_settings,
                    ethdebug,
                ) as tmp_file:
                    self.run_pipeline(
                        tmp_file,
                        name,
                        label,
                        solc_settings,
                        None if ethdebug else gas_project_dir,
                        None if (ethdebug or gas_bench_config is None) else (fixture_dir, own_targets),
                    )

        if (selected or tag_set) and not matched_any:
            print(
                "warning: no benchmarks matched the given --only/--tags filter",
                file=sys.stderr,
            )

    @staticmethod
    def _pipeline_runs(pipelines, no_optimize):
        runs = []
        for pipeline in pipelines:
            if pipeline == "ir-ethdebug":
                # ETHDebug program output does not support the optimizer yet;
                # resolve_solc_settings requires --no-optimize for this pipeline.
                runs.append(
                    (
                        pipeline,
                        resolve_solc_settings("ir", no_optimize, ethdebug=True),
                        True,
                    )
                )
            else:
                runs.append(
                    (
                        pipeline,
                        resolve_solc_settings(pipeline, no_optimize),
                        False,
                    )
                )
        return runs

    def write_results(self, stdout=False):
        """Write results JSON to output dir, optionally also to stdout."""
        if not self.results:
            print("\nNo results to write.", file=sys.stderr)
            return

        output = reporter.build_result_json(
            self.results, self.solc_version, self.iterations
        )
        result_path = self.output_file or self.output_dir / DEFAULT_RESULT_FILENAME
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
