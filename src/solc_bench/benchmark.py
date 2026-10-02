import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import NamedTuple

from solc_bench.config import (
    DEFAULT_PIPELINES,
    DEFAULT_RESULT_FILENAME,
    load_benchmarks,
)
from solc_bench.gas import ensure_project, run_gas_benchmark
from solc_bench.fixture_bytecode import (
    add_gas_headroom,
    extract_deployed_bytecode,
    merge_gas_bench_output_selection,
    read_targets_config,
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
    write_temp_json,
)
from solc_bench.statetest import fixture_replay


PERF_EVENTS = ("instructions", "cycles", "cache-references", "cache-misses")


def perf_available():
    if not shutil.which("perf"):
        return False
    try:
        result = subprocess.run(
            ["perf", "stat", "-x", ";", "-e", ",".join(PERF_EVENTS), "true"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    # An event perf knows but cannot count still exits 0, with '<not supported>'.
    return result.returncode == 0 and "<not supported>" not in result.stderr


def _ru_maxrss_mib(ru_maxrss):
    """Normalize resource.ru_maxrss to MiB.

    Linux reports ru_maxrss in KiB, while macOS reports it in bytes.
    """
    if sys.platform == "darwin":
        return ru_maxrss / (1024 * 1024)
    return ru_maxrss / 1024


class SolcFailed(Exception):

    def __init__(self, exit_code, stderr):
        if exit_code < 0:
            reason = f"killed by signal {-exit_code} ({signal.strsignal(-exit_code)})"
        else:
            reason = f"exit code {exit_code}"
        super().__init__(f"solc {reason}")
        self.stderr = stderr


class GasFixtures(NamedTuple):
    """A benchmark's `gas/<name>/` fixtures, and the output selection the bytecode swap needs compiled."""

    fixture_dir: Path
    targets: list[dict]
    output_selection: dict


class Benchmark:
    """Runs solc and collects all metrics."""

    def __init__(self, solc, use_perf=None):
        self.solc = solc
        self.use_perf = use_perf if use_perf is not None else perf_available()
        # Kept around so gas-fixture benchmarking can reuse this compile's
        # bytecode instead of recompiling.
        self.last_output = None

    def run(self, input_file, iterations):
        """Run solc N times, return aggregated metrics."""
        samples = []
        counter_len = 0
        self.last_output = None
        for i in range(iterations):
            metrics = self.run_once(input_file)

            counter = f" [{i + 1}/{iterations}]"
            print("\b" * counter_len + counter, file=sys.stderr, end="", flush=True)
            counter_len = len(counter)
            samples.append(metrics)

            # Skip remaining iterations: same input, same errors.
            if metrics.get("errors", 0) > 0:
                break

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
        """
        cmd = [self.solc, "--standard-json"]
        if self.use_perf:
            cmd = ["perf", "stat", "-e", ",".join(PERF_EVENTS), "-x", ";", "--", *cmd]

        with (
            open(input_file, encoding="utf-8") as f,
            tempfile.TemporaryFile() as stderr_file,
        ):
            wall_start = time.monotonic()

            proc = subprocess.Popen(
                cmd, stdin=f, stdout=subprocess.PIPE, stderr=stderr_file,
            )

            stdout = proc.stdout.read()
            _, status, rusage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)

            wall_time = time.monotonic() - wall_start

            stderr_file.seek(0)
            stderr = stderr_file.read().decode(errors="replace")

        if proc.returncode != 0:
            raise SolcFailed(proc.returncode, stderr)

        metrics = {
            "cpu_time": rusage.ru_utime + rusage.ru_stime,
            "wall_time": wall_time,
            "peak_rss": _ru_maxrss_mib(rusage.ru_maxrss),
        }

        if self.use_perf:
            metrics.update(parse_perf_output(stderr))
            metrics["cache_miss_rate"] = (
                100 * metrics["cache_misses"] / metrics["cache_references"]
            )

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
        evmone=None,
    ):
        self.solc_version = get_solc_version(solc)
        self.benchmark = Benchmark(solc)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = Path(output_file) if output_file else None
        self.iterations = iterations
        self.keep_inputs = keep_inputs
        # Path to evmone, or None to skip gas-fixture benchmarking.
        self.evmone = evmone
        self.results = {}

    @property
    def use_perf(self):
        return self.benchmark.use_perf

    def run_pipeline(
        self, input_file, name, pipeline, solc_settings, gas_project_dir=None, gas_fixtures=None,
    ):
        """Run one pipeline, record the result if no errors. Optionally run gas."""
        if self.keep_inputs:
            kept_input = self.output_dir / "inputs" / f"{name}.{pipeline}.json"
            kept_input.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(input_file, kept_input)
        reporter.benchmark_start(name, pipeline, solc_settings)
        try:
            result = self.benchmark.run(input_file, self.iterations)
        except SolcFailed as e:
            log_path = self._write_log(name, pipeline, "solc", f"{e}\n\n{e.stderr}")
            reporter.benchmark_failed(e, log_path)
            return

        has_errors = bool(result.get("errors", 0))
        error_log = self._write_error_log(result, name, pipeline) if has_errors else None

        reporter.benchmark_done(result, error_log)

        if has_errors:
            return

        if gas_project_dir is not None:
            self._run_gas(result, gas_project_dir, name, pipeline, solc_settings)

        if gas_fixtures is not None:
            self._run_gas_fixtures(result, gas_fixtures)

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

    def _gas_fixtures(self, benchmark_dir, name, input_file):
        """This benchmark's `gas/<name>/` fixtures, or None if it has none
        or evmone wasn't given."""
        if self.evmone is None:
            return None
        fixture_dir = Path(benchmark_dir) / "gas" / name
        targets_toml = fixture_dir / "targets.toml"
        if not targets_toml.is_file():
            return None
        try:
            targets = read_targets_config(targets_toml)
        except ValueError as e:
            print(f"  {name}: skipping gas fixtures: {e}", file=sys.stderr)
            return None
        with open(input_file, encoding="utf-8") as f:
            output_selection = json.load(f).get("settings", {}).get("outputSelection")
        return GasFixtures(fixture_dir, targets, merge_gas_bench_output_selection(output_selection))

    def _run_gas_fixtures(self, result, gas_fixtures: GasFixtures):
        """Swap this pipeline's freshly compiled bytecode into each fixture and replay it.
        Merges a per-fixture breakdown into result, plus the summed gas_used if nothing failed.
        A partial sum wouldn't be comparable."""
        output = self.benchmark.last_output
        if output is None:
            return

        failures = []
        codes = {}
        for target in gas_fixtures.targets:
            try:
                codes[target["address"]] = extract_deployed_bytecode(output, target)
            except ValueError as e:
                failures.append(f"{target['contract_name']}: {e}")

        functions = {}
        for fixture_path in sorted(gas_fixtures.fixture_dir.glob("*.json")):
            fixture = json.loads(fixture_path.read_text())
            (test_name,) = fixture.keys()
            pre_addresses = {a.lower() for a in fixture[test_name]["pre"]}
            touched = [target for target in gas_fixtures.targets if target["address"] in pre_addresses]
            if not touched:
                failures.append(f"{fixture_path.name}: touches none of the targets in targets.toml")
                continue
            for target in touched:
                address = target["address"]
                if address not in codes:
                    continue  # already a failure: its bytecode couldn't be built
                func_key = f"{target['contract_name']}@{address[2:10]}.{fixture_path.stem}"
                swapped = add_gas_headroom(swap_contract_code(deepcopy(fixture), address, codes[address]))
                try:
                    with write_temp_json(swapped) as path:
                        replay = fixture_replay(Path(path), self.evmone, exempt_sender_balance=True)
                except (ValueError, RuntimeError) as e:
                    failures.append(f"{func_key}: {e}")
                    continue
                functions[func_key] = {
                    "values": [replay.gas_used], "median": replay.gas_used, "mean": replay.gas_used,
                }

        if functions:
            result.setdefault("functions", {}).update(functions)
        for failure in failures:
            print(f"    [gas] FAILED {failure}", file=sys.stderr)
        if failures:
            print(f"    [gas] WARNING: {len(failures)} failure(s), not reporting gas_used", file=sys.stderr)
            return
        if not functions:
            print("    [gas] WARNING: no fixtures", file=sys.stderr)
            return
        total_gas = sum(f["median"] for f in functions.values())
        print(f"    [gas] fixtures={len(functions)} gas_used={total_gas:,}", file=sys.stderr)
        result["gas_used"] = {"values": [total_gas], "median": total_gas, "mean": total_gas}

    def _write_error_log(self, result, name, pipeline):
        error_messages = result.pop("error_messages", [])
        if not error_messages:
            return None
        return self._write_log(name, pipeline, "errors", "\n".join(error_messages))

    def _write_log(self, name, pipeline, kind, text):
        log_path = self.output_dir / f"{name}-{pipeline}.{kind}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
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

            gas_fixtures = self._gas_fixtures(benchmark_dir, name, input_file)

            for label, solc_settings, ethdebug in self._pipeline_runs(
                bench_pipelines,
                no_optimize,
            ):
                pipeline_gas_fixtures = None if ethdebug else gas_fixtures
                if pipeline_gas_fixtures is not None:
                    solc_settings = {**solc_settings, "outputSelection": pipeline_gas_fixtures.output_selection}

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
                        pipeline_gas_fixtures,
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
    """Parse perf stat -x ';' output for PERF_EVENTS.

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

        for name in PERF_EVENTS:
            if name in event:
                key = name.replace("-", "_")
                metrics[key] = metrics.get(key, 0) + value
                break

    missing = [e for e in PERF_EVENTS if e.replace("-", "_") not in metrics]
    if missing:
        raise RuntimeError(
            f"perf stat reported no count for {', '.join(missing)}:\n{perf_text}"
        )
    return metrics
