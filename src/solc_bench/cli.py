"""CLI entry point for solc-bench."""

import json
import os
import sys
from argparse import ArgumentParser, ArgumentTypeError, RawDescriptionHelpFormatter
from collections import Counter
from pathlib import Path

from solc_bench import VERSION
from solc_bench.benchmark import BenchmarkSuite
from solc_bench.compare import compare_pipelines, compare_compiler_versions, load_results
from solc_bench.config import DEFAULT_PIPELINES, load_benchmarks
from solc_bench.extract import extract_inputs
from solc_bench.fetch import FetchError, fetch_solc
from solc_bench.host import check_variance_factors
from solc_bench.metrics import ALL_METRICS
from solc_bench import reporter
from solc_bench.solidity import validate_standard_json
from solc_bench.sourcify import extract as extract_sourcify

DEFAULT_ITERATIONS = 3


def _split_tags(raw):
    """Parse a comma-separated --tags value into a normalized list."""
    if not raw:
        return None
    out = []
    seen = set()
    for item in raw.split(","):
        cleaned = item.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out or None


def solc_binary(value):
    """argparse type for --solc: existing executable, returned as absolute path."""
    path = Path(value).resolve()
    if not path.is_file():
        raise ArgumentTypeError(f"solc binary not found: {value}")
    if not os.access(path, os.X_OK):
        raise ArgumentTypeError(f"solc not executable: {path}")
    return str(path)


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
        if args.tags:
            raise ValueError("--tags cannot be used with an input file")
        if not Path(args.input_file).is_file():
            raise FileNotFoundError(f"input file not found: {args.input_file}")
        if not args.input_file.endswith((".sol", ".json")):
            raise ValueError(
                f"unsupported file type: {args.input_file} (expected .sol or .json)"
            )
        if args.input_file.endswith(".json"):
            validate_standard_json(args.input_file)
    elif not args.benchmark_dir:
        raise ValueError(
            "--benchmark-dir is required for suite runs. "
            "Populate one with `solc-bench extract`."
        )

    suite = BenchmarkSuite(
        args.solc, args.iterations, args.output_dir, keep_inputs=args.keep_inputs
    )
    print(f"solc: {suite.solc_version}", file=sys.stderr)
    print(f"iterations: {args.iterations}", file=sys.stderr)
    perf_str = (
        "available (using hardware counters)"
        if suite.use_perf
        else "not available (using rusage only)"
    )
    print(f"perf: {perf_str}", file=sys.stderr)

    for w in check_variance_factors():
        print(f"warning: {w}", file=sys.stderr)

    if args.input_file:
        suite.run_file(args.input_file, args.pipeline, args.no_optimize)
    else:
        tags = _split_tags(args.tags)
        suite.run_suite(
            args.benchmark_dir,
            args.only,
            args.pipeline,
            args.no_optimize,
            tags,
        )

    suite.write_results(stdout=args.stdout)
    return 0


def cmd_compare(args):
    if args.pipelines and args.target:
        raise ValueError("--pipelines cannot be combined with a second file")
    if not args.pipelines and not args.target:
        raise ValueError("provide a target file or --pipelines TARGET:REF")
    if args.pipelines and args.per_function:
        raise ValueError("--per-function is not supported with --pipelines (cross-version mode only)")
    baseline_data = load_results(args.baseline)
    plot_metrics = _parse_plot_metrics(args.plot_metric)

    if args.pipelines:
        target_pipe, sep, ref = args.pipelines.partition(":")
        if not (sep and target_pipe and ref):
            raise ValueError("--pipelines must be 'TARGET:REF'")
        result = compare_pipelines(baseline_data, ref, target_pipe)
        table_fn = reporter.cross_pipeline_table
        plot_fn = lambda path: _plot_cross_pipeline(
            baseline_data, ref, target_pipe, plot_metrics, path
        )
    else:
        target_data = load_results(args.target)
        result = compare_compiler_versions(baseline_data, target_data)
        table_fn = reporter.cross_version_table
        plot_fn = lambda path: _plot_cross_version(
            baseline_data, target_data, plot_metrics, path
        )

    if args.output:
        reporter.write_comparison_json(result, args.output)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        table_fn(result)
        if args.per_function:
            reporter.cross_version_per_function_table(result, sort_by=args.per_function)

    if args.plot:
        plot_fn(args.plot)
        print(f"Plot written to {args.plot}", file=sys.stderr)

    return 0


def _plot_cross_version(baseline, target, metrics, path):
    from solc_bench.plot import plot_cross_version
    plot_cross_version(baseline, target, metrics, path)


def _plot_cross_pipeline(results, ref, target, metrics, path):
    from solc_bench.plot import plot_cross_pipeline
    plot_cross_pipeline(results, ref, target, metrics, path)


def _parse_plot_metrics(raw):
    metrics = [m.strip() for m in raw.split(",") if m.strip()]
    if not metrics:
        raise ValueError("--plot-metric must list at least one metric")
    return metrics


def cmd_extract(args):
    output_dir = args.output_dir or Path(args.project).parent
    project_name = Path(args.project).resolve().name
    exclude = _benchmark_exclude(output_dir, project_name)

    print(f"Extracting inputs from {args.project}...", file=sys.stderr)
    if not extract_inputs(args.solc, args.project, output_dir, exclude=exclude):
        return 1
    print("Done.", file=sys.stderr)
    return 0


def _benchmark_exclude(benchmark_dir, name):
    """Return the ``exclude`` source-path list for a benchmark, or [].

    Looks up the entry matching the project directory name in
    benchmarks.toml; returns [] if there is no benchmarks.toml, no matching
    entry, or no ``exclude`` key.
    """
    try:
        benchmarks = load_benchmarks(benchmark_dir)
    except FileNotFoundError:
        return []
    raw = benchmarks.get(name, {}).get("exclude", [])
    if not isinstance(raw, (list, tuple)):
        print(
            f"warning: '{name}' has 'exclude' of unsupported type "
            f"{type(raw).__name__}, ignoring",
            file=sys.stderr,
        )
        return []
    return [str(x) for x in raw]


def cmd_extract_sourcify(args):
    extract_sourcify(
        args.output_dir,
        top_n=args.top_n,
        min_version=args.min_version,
        force=args.force,
    )
    return 0


def cmd_fetch(args):
    output = Path(args.output) if args.output else Path.cwd() / f"solc-{args.ref}"
    source = fetch_solc(args.ref, output, args.force)
    print(f"Source:  {source}", file=sys.stderr)
    print(f"Wrote:   {output.resolve()}", file=sys.stderr)
    return 0


def cmd_list(args):
    if args.metrics:
        for name, (description, unit) in sorted(ALL_METRICS.items()):
            print(f"  {name:<16} [{unit}] {description}")
        return 0

    if not args.benchmark_dir:
        raise ValueError(
            "--benchmark-dir is required (or pass --metrics to list metrics). "
            "Populate a suite with `solc-bench extract`."
        )
    benchmarks = load_benchmarks(args.benchmark_dir)

    if args.tags:
        counts = Counter(
            tag for cfg in benchmarks.values() for tag in cfg.get("tags", [])
        )
        if not counts:
            print("(no tags defined)", file=sys.stderr)
            return 0
        for tag, count in sorted(counts.items()):
            print(f"  {tag:<20} {count} benchmark(s)")
        return 0

    has_tags = any(cfg.get("tags") for cfg in benchmarks.values())
    for name, config in benchmarks.items():
        source = config.get("source", "")
        version = config.get("version", "")
        pipelines = ", ".join(config.get("pipelines", []))
        line = f"  {name:<30} {version:<12} {pipelines:<20}"
        if has_tags:
            tags = ", ".join(config.get("tags", []))
            line += f" {tags:<24}"
        line += f" {source}"
        print(line)
    return 0


def build_parser():
    parser = ArgumentParser(
        prog="solc-bench",
        description="Solidity compiler benchmark tool",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(required=True)

    run_parser = subparsers.add_parser("run", help="Run benchmarks", allow_abbrev=False)
    run_parser.set_defaults(func=cmd_run)
    run_parser.add_argument("--solc", required=True, type=solc_binary, help="Path to solc binary")
    run_parser.add_argument(
        "--only", default=None, help="Comma-separated benchmark names"
    )
    run_parser.add_argument(
        "--tags",
        default=None,
        help=(
            "Comma-separated tags; selects benchmarks carrying any listed "
            "tag. Combined with --only via AND."
        ),
    )
    run_parser.add_argument(
        "--iterations",
        type=int,
        default=DEFAULT_ITERATIONS,
        help=f"Number of iterations (default: {DEFAULT_ITERATIONS})",
    )
    run_parser.add_argument(
        "--output-dir",
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
        default=None,
        help=(
            "Directory containing benchmarks.toml and the input JSONs it "
            "references. Required for suite runs, ignored when an "
            "input_file is given. Populate with `solc-bench extract`."
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
        "--keep-inputs",
        action="store_true",
        default=False,
        help=(
            "Save each post-override standard-json input under "
            "<output-dir>/inputs/<name>.<pipeline>.json"
        ),
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
        allow_abbrev=False,
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
        "--output", default=None, help="Write comparison JSON to file"
    )
    cmp_parser.add_argument(
        "--per-function",
        nargs="?",
        const="median",
        default=None,
        choices=["min", "mean", "median", "max"],
        help=(
            "Print per-function gas deltas, sort by |delta of STAT| "
            "(default: median). Cross-version mode only."
        ),
    )
    cmp_parser.add_argument(
        "--plot",
        default=None,
        metavar="PATH",
        help=(
            "Write a boxplot of the per-iteration samples to PATH "
            "(e.g. plot.png). Requires the 'plot' extra: "
            "pip install 'solc-bench[plot]'."
        ),
    )
    cmp_parser.add_argument(
        "--plot-metric",
        default="cpu_time",
        help=(
            "Metric(s) to plot, comma-separated for multiple panels "
            "(default: cpu_time). E.g. wall_time,instructions"
        ),
    )

    ext_parser = subparsers.add_parser(
        "extract", help="Extract standard-json inputs from a Forge project",
        allow_abbrev=False,
    )
    ext_parser.set_defaults(func=cmd_extract)
    ext_parser.add_argument("--solc", required=True, type=solc_binary, help="Path to solc binary")
    ext_parser.add_argument(
        "--project", required=True, help="Path to Forge project directory"
    )
    ext_parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for generated files (default: project parent)",
    )

    sf_parser = subparsers.add_parser(
        "extract-sourcify",
        help="Extract real-world contracts from Sourcify for benchmarking",
        allow_abbrev=False,
    )
    sf_parser.set_defaults(func=cmd_extract_sourcify)
    sf_parser.add_argument(
        "--output-dir", required=True,
        help="Where to write Standard JSON inputs and benchmarks.toml",
    )
    sf_parser.add_argument(
        "--top-n", type=int, default=100,
        help="Top-N most-used mainnet contracts to extract (default: 100)",
    )
    sf_parser.add_argument(
        "--min-version", default="0.8.0",
        help="Minimum solc version (default: 0.8.0)",
    )
    sf_parser.add_argument(
        "--force", action="store_true",
        help="Wipe --output-dir contents before writing the new suite",
    )

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Download a solc binary from a release tag or branch",
        description=(
            "Download a Linux x86_64 solc binary for the given ref.\n"
            "  Release tag (e.g. v0.8.35): fetched from the argotorg/solidity GitHub release.\n"
            "  Branch (e.g. develop): fetched from the latest successful CircleCI b_ubu_static job."
        ),
        formatter_class=RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    fetch_parser.set_defaults(func=cmd_fetch)
    fetch_parser.add_argument(
        "ref",
        help="Release tag (e.g. v0.8.35) or branch name (e.g. develop)",
    )
    fetch_parser.add_argument(
        "--output",
        default=None,
        help="Destination path (default: ./solc-{ref})",
    )
    fetch_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Overwrite the destination if it already exists",
    )

    list_parser = subparsers.add_parser(
        "list", help="List configured benchmarks or available metrics",
        allow_abbrev=False,
    )
    list_parser.set_defaults(func=cmd_list)
    list_parser.add_argument(
        "--metrics", action="store_true", help="List available metrics instead"
    )
    list_parser.add_argument(
        "--tags",
        action="store_true",
        help="List all tags defined across benchmarks instead",
    )
    list_parser.add_argument(
        "--benchmark-dir",
        default=None,
        help="Suite directory containing benchmarks.toml (required unless --metrics)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        return args.func(args)
    except (FileNotFoundError, FileExistsError, PermissionError, ValueError, FetchError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
