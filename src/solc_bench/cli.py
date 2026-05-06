"""CLI entry point for solc-bench."""

import json
import sys
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from pathlib import Path

from solc_bench import VERSION
from solc_bench.benchmark import BenchmarkSuite
from solc_bench.compare import compare_pipelines, compare_compiler_versions, load_results
from solc_bench.config import DEFAULT_BENCHMARK_DIR, DEFAULT_PIPELINES, load_benchmarks
from solc_bench.extract import extract_inputs
from solc_bench.metrics import ALL_METRICS
from solc_bench import reporter
from solc_bench.solidity import validate_standard_json

DEFAULT_ITERATIONS = 3


def cmd_run(args):
    if args.iterations < 1:
        raise ValueError("--iterations must be at least 1")

    result_path = Path(args.output_dir) / "bench-results.json"
    if result_path.exists():
        raise FileExistsError(
            f"results file already exists: {result_path} "
            "(remove it or choose a different --output-dir)"
        )

    if args.input_file:
        if args.only:
            raise ValueError("--only cannot be used with an input file")
        if not Path(args.input_file).is_file():
            raise FileNotFoundError(f"input file not found: {args.input_file}")
        if not args.input_file.endswith((".sol", ".json")):
            raise ValueError(
                f"unsupported file type: {args.input_file} (expected .sol or .json)"
            )
        if args.input_file.endswith(".json"):
            validate_standard_json(args.input_file)

    suite = BenchmarkSuite(args.solc, args.iterations, args.output_dir)
    print(f"solc: {suite.solc_version}", file=sys.stderr)
    print(f"iterations: {args.iterations}", file=sys.stderr)
    perf_str = (
        "available (using hardware counters)"
        if suite.use_perf
        else "not available (using rusage only)"
    )
    print(f"perf: {perf_str}", file=sys.stderr)

    if args.input_file:
        suite.run_file(args.input_file, args.pipeline, args.no_optimize)
    else:
        suite.run_suite(
            args.benchmark_dir,
            args.only,
            args.pipeline,
            args.no_optimize,
        )

    suite.write_results(stdout=args.stdout)
    return 0


def cmd_compare(args):
    if args.pipelines and args.target:
        raise ValueError("--pipelines cannot be combined with a second file")
    if not args.pipelines and not args.target:
        raise ValueError("provide a target file or --pipelines TARGET:REF")
    if args.pipelines:
        target_pipe, sep, ref = args.pipelines.partition(":")
        if not (sep and target_pipe and ref):
            raise ValueError("--pipelines must be 'TARGET:REF'")
        result = compare_pipelines(load_results(args.baseline), ref, target_pipe)
        table_fn = reporter.cross_pipeline_table
    else:
        result = compare_compiler_versions(
            load_results(args.baseline), load_results(args.target)
        )
        table_fn = reporter.cross_version_table

    if args.output:
        reporter.write_comparison_json(result, args.output)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        table_fn(result)

    return 0


def cmd_extract(args):
    output_dir = args.output_dir or Path(args.project).parent

    print(f"Extracting inputs from {args.project}...", file=sys.stderr)
    if not extract_inputs(args.solc, args.project, output_dir):
        return 1
    print("Done.", file=sys.stderr)
    return 0


def cmd_list(args):
    if args.metrics:
        for name, (description, unit) in sorted(ALL_METRICS.items()):
            print(f"  {name:<16} [{unit}] {description}")
        return 0

    benchmarks = load_benchmarks(args.benchmark_dir)
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
        "--output-dir",
        "-o",
        default=".",
        help="Output directory for results and logs (default: current directory)",
    )
    run_parser.add_argument(
        "--stdout",
        action="store_true",
        default=False,
        help="Also print results to stdout",
    )
    run_parser.add_argument(
        "--benchmark-dir",
        default=str(Path.cwd() / DEFAULT_BENCHMARK_DIR),
        help=(
            "Directory containing input JSONs (and optionally a "
            "benchmarks.toml override). Default: %(default)s"
        ),
    )
    run_parser.add_argument(
        "--pipeline",
        choices=DEFAULT_PIPELINES,
        default=None,
        help="Compilation pipeline (default: all pipelines)",
    )
    run_parser.add_argument(
        "--no-optimize",
        action="store_true",
        default=False,
        help="Disable optimizer (default: optimizer enabled)",
    )
    run_parser.add_argument(
        "input_file",
        nargs="?",
        default=None,
        help="Solidity source file (.sol) or standard-json input (.json)",
    )

    cmp_parser = subparsers.add_parser(
        "compare",
        help="Compare two result files, or two pipelines within one file",
        description=(
            "Compare benchmark results in one of two modes:\n"
            "  cross-version (two files):  "
            "solc-bench compare baseline/bench-results.json "
            "target/bench-results.json\n"
            "  cross-pipeline (one file):  "
            "solc-bench compare bench-results.json --pipelines ir:evmasm"
        ),
        formatter_class=RawDescriptionHelpFormatter,
    )
    cmp_parser.set_defaults(func=cmd_compare)
    cmp_parser.add_argument(
        "baseline",
        metavar="bench-results.json",
        help=(
            "Result JSON file (baseline in cross-version mode, "
            "single file in cross-pipeline mode)"
        ),
    )
    cmp_parser.add_argument(
        "target",
        metavar="target_bench-results.json",
        nargs="?",
        default=None,
        help="Target result JSON (cross-version mode only)",
    )
    cmp_parser.add_argument(
        "--pipelines",
        default=None,
        help="Compare two pipelines in one file: TARGET:REF (e.g. ir:evmasm)",
    )
    cmp_parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    cmp_parser.add_argument(
        "--output", "-o", default=None, help="Write comparison JSON to file"
    )

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
        help="Override directory for benchmarks.toml (default: bundled)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        return args.func(args)
    except (FileNotFoundError, FileExistsError, PermissionError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
