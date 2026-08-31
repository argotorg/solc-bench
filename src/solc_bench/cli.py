"""CLI entry point for solc-bench."""

import json
import os
import sys
from argparse import ArgumentParser, ArgumentTypeError, RawDescriptionHelpFormatter
from collections import Counter
from pathlib import Path

from solc_bench import VERSION
from solc_bench.benchmark import BenchmarkSuite
from solc_bench.compare import (
    compare_datasets,
    compare_pipelines,
    compare_compiler_versions,
    load_results,
)
from solc_bench.config import (
    DEFAULT_PIPELINES,
    DEFAULT_RESULT_FILENAME,
    RUN_PIPELINES,
    load_benchmarks,
)
from solc_bench.extract import extract_inputs
from solc_bench.fetch import FetchError, fetch_solc
from solc_bench.host import check_variance_factors
from solc_bench.metrics import ALL_METRICS, DISPLAYED_METRICS
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

    if args.output_dir is not None:
        output_dir = args.output_dir
    elif args.output_file:
        output_dir = str(Path(args.output_file).parent)
    else:
        output_dir = "."

    result_path = (
        Path(args.output_file)
        if args.output_file
        else Path(output_dir) / DEFAULT_RESULT_FILENAME
    )
    if result_path.exists():
        flag = "--output-file" if args.output_file else "--output-dir"
        raise FileExistsError(
            f"results file already exists: {result_path} "
            f"(remove it or choose a different {flag})"
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
        args.solc,
        args.iterations,
        output_dir,
        keep_inputs=args.keep_inputs,
        output_file=args.output_file,
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

    pipelines = _resolve_pipelines(args)
    if args.input_file:
        suite.run_file(args.input_file, pipelines, args.no_optimize)
    else:
        tags = _split_tags(args.tags)
        suite.run_suite(
            args.benchmark_dir,
            args.only,
            pipelines,
            args.no_optimize,
            tags,
        )

    suite.write_results(stdout=args.stdout)
    return 0


def _resolve_pipelines(args):
    """The pipelines `run` was asked for, or None for the per-benchmark default."""
    if args.pipelines:
        names = [p.strip() for p in args.pipelines.split(",") if p.strip()]
        unknown = [p for p in names if p not in RUN_PIPELINES]
        if unknown:
            raise ValueError(
                f"unknown pipeline(s) {', '.join(unknown)}; "
                f"choose from {', '.join(RUN_PIPELINES)}"
            )
        return names
    return [args.pipeline] if args.pipeline else None


def cmd_compare(args):
    if args.pipelines and len(args.results) != 1:
        raise ValueError("--pipelines cannot be combined with a second file")
    if args.vs and args.pipelines:
        raise ValueError("--vs cannot be combined with --pipelines")
    if args.vs and args.per_function:
        raise ValueError("--per-function is not supported with --vs")
    if args.vs and args.plot:
        raise ValueError("--plot is not supported with --vs")
    if not args.pipelines and not args.vs and len(args.results) != 2:
        raise ValueError(
            "provide two files, --pipelines TARGET:REF, or --vs TARGET REF"
        )
    if args.pipelines and args.per_function:
        raise ValueError(
            "--per-function is not supported with --pipelines "
            "(cross-version mode only)"
        )
    known = ALL_METRICS if args.show_hidden else DISPLAYED_METRICS
    max_regressions = [
        _parse_max_regression(spec, known) for spec in args.max_regression
    ]
    plot_metrics = _parse_plot_metrics(args.plot_metric, known)

    if args.vs:
        inputs = [_load_named_result(arg) for arg in args.results]
        referenced = {label for pair in args.vs for label in pair}
        for label, path, _ in inputs:
            if not any(r == label or r.startswith(f"{label}:") for r in referenced):
                raise ValueError(
                    f"result file is not referenced by any --vs pair: {path}"
                )
        result = compare_datasets(inputs, args.vs, args.show_hidden)
        table_fn = reporter.dataset_pairs_table
        plot_fn = None
    elif args.pipelines:
        baseline_data = load_results(_result_path(args.results[0]))
        target_pipe, sep, ref = args.pipelines.partition(":")
        if not (sep and target_pipe and ref):
            raise ValueError("--pipelines must be 'TARGET:REF'")
        result = compare_pipelines(baseline_data, ref, target_pipe, args.show_hidden)
        table_fn = reporter.cross_pipeline_table
        plot_fn = lambda path: _plot_cross_pipeline(
            baseline_data, ref, target_pipe, plot_metrics, path
        )
    else:
        baseline_data = load_results(_result_path(args.results[0]))
        target_data = load_results(_result_path(args.results[1]))
        result = compare_compiler_versions(baseline_data, target_data, args.show_hidden)
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

    failures = _max_regression_failures(result, max_regressions)
    if failures:
        sys.stdout.flush()
        print("\nRegression threshold exceeded:", file=sys.stderr)
        for label, metric, delta_pct, max_pct in failures:
            print(
                f"  {label}: {metric} {delta_pct:+.2f}% > {max_pct:.2f}%",
                file=sys.stderr,
            )
        return 1

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


def _load_named_result(result_arg):
    label, path = _result_label_and_path(result_arg)
    return label, str(path), load_results(path)


def _result_path(result_arg):
    return _result_label_and_path(result_arg)[1]


def _result_label_and_path(result_arg):
    """Resolve a positional ``results`` entry into (label, Path).

    Each entry is ``PATH`` or ``LABEL=PATH``. The ``=`` is read as the alias
    separator only when the entry is not itself an existing file, so paths
    containing ``=`` still load; a label therefore cannot contain ``=``.
    Without an alias the label is the file stem, or the parent directory name
    when the file uses the default result filename.
    """
    if "=" in result_arg and not Path(result_arg).exists():
        label, _, raw_path = result_arg.partition("=")
        if not label or not raw_path:
            raise ValueError("result aliases must be formatted as LABEL=PATH")
        return label, Path(raw_path)

    path = Path(result_arg)
    if path.name == DEFAULT_RESULT_FILENAME and path.parent.name:
        return path.parent.name, path
    return path.stem, path


def _parse_plot_metrics(raw, known):
    metrics = [m.strip() for m in raw.split(",") if m.strip()]
    if not metrics:
        raise ValueError("--plot-metric must list at least one metric")
    unknown = [m for m in metrics if m not in known]
    if unknown:
        raise ValueError(f"unknown metric in --plot-metric: {', '.join(unknown)}")
    return metrics


def _parse_max_regression(raw, known):
    metric, sep, threshold = raw.partition(":")
    if sep != ":" or not metric or not threshold:
        raise ValueError("--max-regression must be formatted as METRIC:PCT")
    if metric not in known:
        raise ValueError(f"unknown metric in --max-regression: {metric}")
    try:
        max_pct = float(threshold)
    except ValueError as e:
        raise ValueError("--max-regression PCT must be a number") from e
    if max_pct < 0:
        raise ValueError("--max-regression PCT must be non-negative")
    return metric, max_pct


def _max_regression_failures(result, thresholds):
    failures = []
    if not thresholds:
        return failures

    if result.get("mode") == "dataset-pairs":
        for metric, max_pct in thresholds:
            for pair in result["comparisons"]:
                for name, comparison in pair["benchmarks"].items():
                    metric_comparison = comparison.get(metric)
                    if metric_comparison is None:
                        continue
                    delta_pct = metric_comparison.get("delta_pct")
                    if delta_pct is not None and delta_pct > max_pct:
                        failures.append(
                            (
                                f"{name} ({pair['target']} vs {pair['ref']})",
                                metric,
                                delta_pct,
                                max_pct,
                            )
                        )
        return failures

    if "ref_pipeline" in result:
        scopes = [
            (
                f"{name} ({result['target_pipeline']} vs {result['ref_pipeline']})",
                comparison,
            )
            for name, comparison in result["benchmarks"].items()
        ]
    else:
        scopes = [
            (f"{name} ({pipeline})", comparison)
            for name, pipelines in result["benchmarks"].items()
            for pipeline, comparison in pipelines.items()
        ]

    for metric, max_pct in thresholds:
        for label, comparison in scopes:
            metric_comparison = comparison.get(metric)
            if metric_comparison is None:
                continue
            delta_pct = metric_comparison.get("delta_pct")
            if delta_pct is not None and delta_pct > max_pct:
                failures.append((label, metric, delta_pct, max_pct))

    return failures


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
    output = Path(args.output) if args.output else Path.cwd() / f"solc-{args.ref.replace(':', '-')}"
    source = fetch_solc(args.ref, output, args.force)
    print(f"Source:  {source}", file=sys.stderr)
    print(f"Wrote:   {output.resolve()}", file=sys.stderr)
    return 0


def cmd_list(args):
    if args.metrics:
        known = ALL_METRICS if args.show_hidden else DISPLAYED_METRICS
        for name, (description, unit) in sorted(known.items()):
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
        default=None,
        help="Output directory for results and logs (default: current directory)",
    )
    run_parser.add_argument(
        "-o",
        "--output-file",
        default=None,
        help=(
            "Write result JSON to this file instead of "
            f"<output-dir>/{DEFAULT_RESULT_FILENAME}"
        ),
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
    pipeline_group = run_parser.add_mutually_exclusive_group()
    pipeline_group.add_argument(
        "--pipeline",
        choices=RUN_PIPELINES,
        default=None,
        help="Single compilation pipeline (default: per-benchmark defaults)",
    )
    pipeline_group.add_argument(
        "--pipelines",
        default=None,
        metavar="A,B",
        help=(
            "Comma-separated pipelines to run for every benchmark, e.g. "
            f"'{','.join(DEFAULT_PIPELINES)}'. Overrides the per-benchmark "
            "`pipelines` in benchmarks.toml"
        ),
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
        help="Compare result files, pipelines, or named dataset pairs",
        description=(
            "Compare benchmark results in one of three modes:\n"
            "  cross-version (two files):  "
            "solc-bench compare baseline/bench-results.json "
            "target/bench-results.json\n"
            "  cross-pipeline (one file):  "
            "solc-bench compare bench-results.json --pipelines ir:evmasm\n"
            "  named pairs (N files):      "
            "solc-bench compare dev-ir.json feat-ir.json --vs feat-ir dev-ir"
        ),
        formatter_class=RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    cmp_parser.set_defaults(func=cmd_compare)
    cmp_parser.add_argument(
        "results",
        metavar="bench-results.json",
        nargs="+",
        help=(
            "Result JSON files, each PATH or LABEL=PATH. The label names the "
            "file for --vs; without one it is inferred from the file name (or "
            "the parent directory for the default result filename)."
        ),
    )
    cmp_parser.add_argument(
        "--pipelines",
        default=None,
        help="Compare two pipelines in one file: TARGET:REF (e.g. ir:evmasm)",
    )
    cmp_parser.add_argument(
        "--vs",
        nargs=2,
        action="append",
        default=[],
        metavar=("TARGET", "REF"),
        help=(
            "Compare two datasets, TARGET vs REF, named by the positional "
            "files (no path here): a single-pipeline file is named by its "
            "label, a multi-pipeline file by LABEL:PIPELINE. Repeatable."
        ),
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
        "--show-hidden",
        action="store_true",
        help=(
            "Also report metrics hidden by default (currently 'cycles'), and "
            "accept them in --plot-metric and --max-regression."
        ),
    )
    cmp_parser.add_argument(
        "--max-regression",
        action="append",
        default=[],
        metavar="METRIC:PCT",
        help=(
            "Exit with failure if any benchmark regresses by more than PCT "
            "for METRIC, e.g. cpu_time:30. Can be passed multiple times."
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
            "  Branch (e.g. develop): fetched from the latest successful CircleCI b_ubu_static job.\n"
            "  Fork branch (e.g. owner:branch): looked up on the fork's owner/solidity repo."
        ),
        formatter_class=RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    fetch_parser.set_defaults(func=cmd_fetch)
    fetch_parser.add_argument(
        "ref",
        help="Release tag (e.g. v0.8.35), branch (e.g. develop), or fork branch (e.g. owner:branch)",
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
        "--show-hidden",
        action="store_true",
        help="Include metrics hidden by default (currently 'cycles')",
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
