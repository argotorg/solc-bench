"""CLI entry point for solc-bench."""

import json
import os
import sys
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

from solc_bench import VERSION
from solc_bench.compare import (
    compare_results,
    load_results,
    print_comparison_table,
    write_comparison,
)
from solc_bench.config import (
    DEFAULT_BENCHMARK_DIR,
    find_input_files,
    load_benchmarks,
    wrap_sol_as_standard_json,
)
from solc_bench.extract import extract_inputs
from solc_bench.gas import METRICS as GAS_METRICS
from solc_bench.runner import METRICS, get_solc_version, perf_available, run_benchmark

DEFAULT_ITERATIONS = 3


def print_iteration(i, metrics):
    """Progress callback for run_benchmark."""
    if metrics.get("exit_code", 0) != 0:
        print(f" FAILED (exit {metrics['exit_code']})", file=sys.stderr)
    elif i > 0:
        print(".", file=sys.stderr, end="", flush=True)


def run_and_record(solc, input_file, name, pipeline, iterations, use_perf, results):
    """Run a single benchmark and store the result."""
    print(f"  {name} ({pipeline})...", file=sys.stderr, end="", flush=True)
    result = run_benchmark(
        solc, input_file, iterations, use_perf=use_perf, on_iteration=print_iteration
    )
    if result:
        cpu = result.get("cpu_time", {})
        print(f" {cpu.get('median', 0):.1f}s", file=sys.stderr)
        if name not in results:
            results[name] = {}
        results[name][pipeline] = result
    else:
        print(file=sys.stderr)


def write_results(results, solc_version, iterations, output_path=None):
    """Build result JSON and write to file or stdout."""
    output = {
        "solc_bench_version": VERSION,
        "solc_version": solc_version,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "iterations": iterations,
        "results": results,
    }

    output_json = json.dumps(output, indent=2)

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(output_json)
            f.write("\n")
        print(f"\nResults written to {output_path}", file=sys.stderr)
    else:
        print(output_json)


def cmd_run(args):
    solc = args.solc
    benchmark_dir = args.benchmark_dir or DEFAULT_BENCHMARK_DIR

    if args.iterations < 1:
        print("Error: --iterations must be at least 1", file=sys.stderr)
        return 1

    if args.input_file and not os.path.isfile(args.input_file):
        raise FileNotFoundError(f"input file not found: {args.input_file}")

    solc_version = get_solc_version(solc)
    use_perf = perf_available()

    print(f"solc: {solc_version}", file=sys.stderr)
    print(f"iterations: {args.iterations}", file=sys.stderr)
    if use_perf:
        print("perf: available (using hardware counters)", file=sys.stderr)
    else:
        print("perf: not available (using rusage only)", file=sys.stderr)

    results = {}

    if args.input_file:
        if args.input_file.endswith(".sol"):
            pipelines = [args.pipeline] if args.pipeline else ["legacy", "ir"]
            name = Path(args.input_file).stem
            for pipeline in pipelines:
                with wrap_sol_as_standard_json(
                    args.input_file, pipeline=pipeline, optimize=args.optimize
                ) as tmp_file:
                    run_and_record(
                        solc,
                        tmp_file,
                        name,
                        pipeline,
                        args.iterations,
                        use_perf,
                        results,
                    )
        elif args.pipeline is not None or not args.optimize:
            print(
                "Error: --pipeline and --no-optimize only apply to .sol files",
                file=sys.stderr,
            )
            return 1
        else:
            name = Path(args.input_file).stem
            run_and_record(
                solc,
                args.input_file,
                name,
                "default",
                args.iterations,
                use_perf,
                results,
            )
    else:
        benchmarks = load_benchmarks(benchmark_dir)
        selected = args.only.split(",") if args.only else None

        print("\nRunning benchmarks...", file=sys.stderr)

        for name, config in benchmarks.items():
            if selected and name not in selected:
                continue

            pipelines = config.get("pipelines", ["legacy", "ir"])
            inputs = find_input_files(benchmark_dir, name, pipelines)

            if not inputs:
                print(f"  {name}: no input files found, skipping", file=sys.stderr)
                continue

            for pipeline, input_file in inputs.items():
                run_and_record(
                    solc, input_file, name, pipeline, args.iterations, use_perf, results
                )

    write_results(results, solc_version, args.iterations, args.output)
    return 0


def cmd_compare(args):
    baseline = load_results(args.baseline)
    target = load_results(args.target)
    result = compare_results(baseline, target)

    if args.output:
        write_comparison(result, args.output)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print_comparison_table(result)

    return 0


def cmd_extract(args):
    output_dir = args.output_dir or os.path.dirname(args.project)

    print(f"Extracting inputs from {args.project}...", file=sys.stderr)
    extract_inputs(args.solc, args.project, output_dir)
    print("Done.", file=sys.stderr)
    return 0


def cmd_list(args):
    if args.metrics:
        all_metrics = dict(METRICS)
        all_metrics.update(GAS_METRICS)

        for name, (description, unit) in sorted(all_metrics.items()):
            print(f"  {name:<16} [{unit}] {description}")
        return 0

    benchmarks = load_benchmarks(args.benchmark_dir or DEFAULT_BENCHMARK_DIR)

    for name, config in benchmarks.items():
        source = config.get("source", "")
        version = config.get("version", "")
        pipelines = ", ".join(config.get("pipelines", []))
        print(f"  {name:<30} {version:<12} {pipelines:<20} {source}")

    return 0


def build_parser():
    parser = ArgumentParser(
        prog="solc-bench",
        description="Solidity compiler benchmark tool",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Run benchmarks")
    run_parser.set_defaults(func=cmd_run)
    run_parser.add_argument("--solc", required=True, help="Path to solc binary")
    run_parser.add_argument(
        "--only", default=None, help="Comma-separated benchmark names"
    )
    run_parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Number of iterations (default: {DEFAULT_ITERATIONS})",
    )
    run_parser.add_argument(
        "--output", "-o", default=None, help="Output JSON file (default: stdout)"
    )
    run_parser.add_argument(
        "--benchmark-dir",
        default=None,
        help="Benchmark directory (default: benchmarks/)",
    )
    run_parser.add_argument(
        "--pipeline",
        choices=["legacy", "ir", "ir-ssacfg"],
        default=None,
        help="Compilation pipeline for .sol files (default: all pipelines)",
    )
    run_parser.add_argument(
        "--optimize",
        action="store_true",
        default=True,
        help="Enable optimizer (default: true)",
    )
    run_parser.add_argument(
        "--no-optimize",
        action="store_false",
        dest="optimize",
        help="Disable optimizer",
    )
    run_parser.add_argument(
        "input_file",
        nargs="?",
        default=None,
        help="Ad-hoc input file (.sol or .json standard-json)",
    )

    # compare
    cmp_parser = subparsers.add_parser("compare", help="Compare two result files")
    cmp_parser.set_defaults(func=cmd_compare)
    cmp_parser.add_argument("baseline", help="Baseline result JSON file")
    cmp_parser.add_argument("target", help="Target result JSON file")
    cmp_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    cmp_parser.add_argument(
        "--output", "-o", default=None, help="Write comparison JSON to file"
    )

    # extract
    ext_parser = subparsers.add_parser(
        "extract", help="Extract standard-json inputs from a Forge project"
    )
    ext_parser.set_defaults(func=cmd_extract)
    ext_parser.add_argument("--solc", required=True, help="Path to solc binary")
    ext_parser.add_argument(
        "--project", required=True, help="Path to Forge project directory"
    )
    ext_parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for generated files (default: project parent)",
    )

    # list
    list_parser = subparsers.add_parser(
        "list", help="List configured benchmarks or available metrics"
    )
    list_parser.set_defaults(func=cmd_list)
    list_parser.add_argument(
        "--metrics", action="store_true", help="List available metrics instead"
    )
    list_parser.add_argument(
        "--benchmark-dir",
        default=None,
        help="Benchmark directory (default: benchmarks/)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        return args.func(args)
    except (FileNotFoundError, PermissionError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
