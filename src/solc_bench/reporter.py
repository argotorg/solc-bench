"""User-facing output: progress, results, comparison tables."""

import json
import os
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from solc_bench.compare import (
    CrossPipelineComparison,
    CrossVersionComparison,
    MetricComparison,
)
from solc_bench.metrics import (
    ALL_METRICS,
    MIN_DELTA_PCT,
    format_delta,
    format_value,
    format_value_with_stddev,
)

_SUMMARY_TOP_CHANGES = 5
# Metrics that get largest-changes lists in the summary; the rest only get an overview row.
_SUMMARY_DETAIL_METRICS = ("cpu_time", "creation_size", "deployment_gas")
_METRIC_ORDER = {metric: position for position, metric in enumerate(ALL_METRICS)}

_ANSI = {
    "green": "\033[32m",
    "red": "\033[31m",
    "heading": "\033[1m",
    "reset": "\033[0m",
}


def use_color():
    """True only when stdout is an interactive terminal, not a file or pipe.

    Honors the NO_COLOR convention (https://no-color.org/) as an opt-out.
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colorize(text, color):
    """Wrap text in an _ANSI style ('green'/'red'/'heading'); a no-op when color is None."""
    if color is None:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


def _winner_color(target, ref):
    """Build a color_fn for a winner column: green for `target`, red for `ref`."""
    def color(value):
        if value == target:
            return "green"
        if value == ref:
            return "red"
        return None
    return color


def _print_table(header, rows, color_fn=None, column_color_fns=None):
    """Print an aligned table. On a terminal, cells are colored by
    column_color_fns[col](cell) or else color_fn(cell).
    Column widths are computed on the plain text, so color never misaligns it.
    """
    cols = list(zip(*([header] + rows)))
    widths = [max(map(len, col)) for col in cols]
    sep = "  "
    column_color_fns = column_color_fns or {}
    colorize_cells = use_color()

    def render(row, color=False):
        cells = []
        for i, (cell, w) in enumerate(zip(row, widths)):
            pad = " " * (w - len(cell))
            fn = column_color_fns.get(i, color_fn)
            name = fn(cell) if (color and fn) else None
            cells.append(colorize(cell, name) + pad)
        return sep.join(cells)

    print(render(header))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(render(row, color=colorize_cells))


def _print_host_mismatch_banner(baseline, target):
    """Warn when baseline and target were measured on different hosts."""
    diffs = []
    for side, key, label in [
        ("hardware", "cpu_model",   "CPU"),
        ("hardware", "hostname",    "host"),
        ("hardware", "kernel",      "kernel"),
        ("environment", "governor",        "governor"),
        ("environment", "mitigations_off", "mitigations_off"),
        ("environment", "aslr",            "ASLR"),
        ("environment", "thp",             "THP"),
        ("environment", "smt_active",      "SMT"),
    ]:
        b = getattr(baseline, side).get(key)
        t = getattr(target, side).get(key)
        if b is None and t is None:
            continue
        if b != t:
            diffs.append(f"{label}: baseline={b!r} target={t!r}")
    if diffs:
        print()
        print("WARNING: baseline and target measured on different hosts or postures:")
        for d in diffs:
            print(f"  {d}")


def benchmark_start(name, pipeline, solc_settings):
    assert "optimizer" in solc_settings, "solc_settings must include optimizer"
    opt_str = "optimize" if solc_settings["optimizer"]["enabled"] else "no-optimize"
    print(
        f"  {name} ({pipeline}, {opt_str})...",
        file=sys.stderr,
        end="",
        flush=True,
    )


def benchmark_done(result):
    print(f" {result.metrics['cpu_time'].median:.1f}s", file=sys.stderr)


def benchmark_failed(reason, log_path):
    print(f" FAILED ({reason}, see {log_path})", file=sys.stderr)


def missing_input_file(name, input_file, source, version, benchmark_dir):
    print(
        f"  {name}: input file not found at {input_file}, skipping",
        file=sys.stderr,
    )
    if source:
        suffix = f" ({version})" if version else ""
        print(f"    source: {source}{suffix}", file=sys.stderr)
    print(
        f"    generate it with: solc-bench extract --solc <solc> "
        f"--project <path-to-project> --output-dir {benchmark_dir}",
        file=sys.stderr,
    )


def write_result_json(data, output_path, stdout=False):
    output_json = json.dumps(data, indent=2)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output_json)
        f.write("\n")
    print(f"\nResults written to {output_path}", file=sys.stderr)
    if stdout:
        print(output_json)


def write_comparison_json(result, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.to_json(), f, indent=2)
        f.write("\n")
    print(f"Comparison written to {output_path}", file=sys.stderr)


def _format_metric_cell(mean, stddev, metric):
    if mean is None:
        return "n/a"
    return format_value_with_stddev(mean, stddev, metric)


def _metric_names(result):
    """Every metric compared anywhere in `result`, in first-seen order."""
    return list(dict.fromkeys(
        metric for _, _, comparison in result.comparisons() for metric in comparison.metrics
    ))


def cross_version_table(result: CrossVersionComparison):
    baseline = result.baseline
    target = result.target
    print(f"Baseline: {baseline.solc_version}{_iterations_suffix(baseline)}")
    print(f"Target:   {target.solc_version}{_iterations_suffix(target)}")
    print(
        "Values are mean \u00b1 sample stddev. \u0394% = "
        "(target mean - baseline mean) / baseline mean. Negative = "
        "improvement (lower is better), positive = regression."
    )
    print(
        f"winner = '~noise' unless the gap passes a Welch t-test and "
        f"|Δ%| ≥ {MIN_DELTA_PCT:g}%."
    )
    _print_host_mismatch_banner(baseline, target)
    print()

    metric_names = _metric_names(result)

    if not metric_names:
        print("No results to compare.")
        return

    row_header = ["Benchmark", "Pipeline", "Metric", "Base", "Target", "\u0394%", "winner"]
    rows = []

    for name, pipelines in result.benchmarks.items():
        for pipeline, comparison in pipelines.items():
            first = True
            for metric in metric_names:
                c = comparison.metrics.get(metric)
                if c is None:
                    continue
                rows.append(
                    [
                        name if first else "",
                        pipeline if first else "",
                        metric,
                        _format_metric_cell(c.base_mean, c.base_stddev, metric),
                        _format_metric_cell(c.target_mean, c.target_stddev, metric),
                        format_delta(c.delta_pct),
                        _format_winner(c.delta_pct, c.significant, "TARGET", "BASELINE"),
                    ]
                )
                first = False
            if not first:
                rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows, color_fn=_winner_color("TARGET", "BASELINE"))


def _shorten(text, width):
    """Middle-truncate so the start and tail of `text` both stay visible."""
    if len(text) <= width:
        return text
    left = (width - 3) // 2
    right = width - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def cross_version_per_function_table(
    result: CrossVersionComparison, sort_by="median", max_func_width=60
):
    """Per-function gas deltas, all four stats per row, sorted by |delta_pct| of sort_by."""
    stats = ("min", "mean", "median", "max")
    if sort_by not in stats:
        raise ValueError(f"sort_by must be one of {stats}, got {sort_by}")

    print(f"\nPer-function gas (delta % per stat, sorted by |{sort_by}|):\n")
    row_header = ["Benchmark", "Pipeline", "Function", "Calls"] + [
        f"{s} \u0394" for s in stats
    ]
    rows = []

    for name, pipelines in result.benchmarks.items():
        for pipeline, comparison in pipelines.items():
            entries = []
            for sig, func in comparison.functions.items():
                key_stat = func.stats.get(sort_by)
                key_delta = key_stat.delta_pct if key_stat else None
                if key_delta is None:
                    continue
                entries.append((sig, func, key_delta))
            entries.sort(key=lambda e: abs(e[2]), reverse=True)

            first = True
            for sig, func, _ in entries:
                calls = func.base_calls
                row = [
                    name if first else "",
                    pipeline if first else "",
                    _shorten(sig, max_func_width),
                    f"{calls:,}" if calls is not None else "",
                ]
                for s in stats:
                    s_data = func.stats.get(s)
                    row.append(format_delta(s_data.delta_pct) if s_data else "n/a")
                rows.append(row)
                first = False
            if not first:
                rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()
    if not rows:
        print("(no per-function gas data)")
        return
    _print_table(row_header, rows)


def cross_pipeline_table(result: CrossPipelineComparison):
    print(f"solc:      {result.run.solc_version}")
    print(f"timestamp: {result.run.timestamp}")
    if result.run.iterations is not None:
        print(f"iterations: {result.run.iterations}")
    print(
        f"Pipeline comparison: {result.target_pipeline} vs "
        f"{result.ref_pipeline}"
    )
    print(
        "Values are mean \u00b1 sample stddev. \u0394% = "
        "(target mean - ref mean) / ref mean. Negative = improvement "
        "(lower is better), positive = regression."
    )
    print(
        f"winner = '~noise' unless the gap passes a Welch t-test and "
        f"|Δ%| ≥ {MIN_DELTA_PCT:g}%."
    )
    print()

    metric_names = _metric_names(result)

    if not metric_names:
        print("No results to compare.")
        return

    ref = result.ref_pipeline
    tgt = result.target_pipeline
    row_header = ["Benchmark", "Metric", tgt, ref, "\u0394%", "winner"]
    rows = []

    for name, comparison in result.benchmarks.items():
        first = True
        for metric in metric_names:
            c = comparison.metrics.get(metric)
            if c is None:
                continue
            rows.append(
                [
                    name if first else "",
                    metric,
                    _format_metric_cell(c.target_mean, c.target_stddev, metric),
                    _format_metric_cell(c.base_mean, c.base_stddev, metric),
                    format_delta(c.delta_pct),
                    _format_winner(c.delta_pct, c.significant, tgt, ref),
                ]
            )
            first = False
        if not first:
            rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows, color_fn=_winner_color(tgt, ref))


def _change_outcome(delta_pct, significant):
    """Classify a signed delta as 'improved', 'regressed', '~noise', 'tie' or 'n/a'.

    Reports '~noise' when the Welch t-test says the gap is not significant,
    or when it is below the practical-significance floor MIN_DELTA_PCT, and
    'tie' on an exact zero delta. When the t-test could not be computed
    (significant is None) it falls back to the raw sign.
    """
    if delta_pct is None:
        return "n/a"
    if delta_pct == 0:
        return "tie"
    if significant is False or abs(delta_pct) < MIN_DELTA_PCT:
        return "~noise"
    return "improved" if delta_pct < 0 else "regressed"


def _format_winner(delta_pct, significant, target, ref):
    """Like _change_outcome, but names the winning side: target or ref."""
    outcome = _change_outcome(delta_pct, significant)
    if outcome == "improved":
        return target
    if outcome == "regressed":
        return ref
    return outcome


def _iterations_suffix(run_info):
    iterations = run_info.iterations
    return f" n={iterations}" if iterations is not None else ""


@dataclass
class _Measurement:
    """One benchmark/pipeline/metric comparison, as used by the summary."""
    benchmark: str
    pipeline: str | None
    metric: str
    base_mean: float
    target_mean: float
    delta_pct: float | None
    outcome: str  # see _change_outcome


def summary(result: CrossVersionComparison | CrossPipelineComparison):
    """Condensed overview of a cross-version or --pipelines compare result."""
    print("\nSummary\n=======")
    _print_summary_legend()
    _print_summary([
        measurement
        for benchmark, pipeline, comparison in result.comparisons()
        for metric, c in comparison.metrics.items()
        if (measurement := _make_measurement(benchmark, pipeline, metric, c)) is not None
    ])


def _make_measurement(benchmark, pipeline, metric, comparison: MetricComparison):
    if metric not in ALL_METRICS:
        return None
    base_mean = comparison.base_mean
    target_mean = comparison.target_mean
    if base_mean is None or target_mean is None:
        return None
    # Non-positive means have no ratio, so skip them to keep every summary column
    # (n, geomean, total, min/max) over the same benchmarks.
    if base_mean <= 0 or target_mean <= 0:
        return None
    return _Measurement(
        benchmark=benchmark,
        pipeline=pipeline,
        metric=metric,
        base_mean=base_mean,
        target_mean=target_mean,
        delta_pct=comparison.delta_pct,
        outcome=_change_outcome(comparison.delta_pct, comparison.significant),
    )


def _print_summary_legend():
    print()
    print("n          = number of benchmarks in the row")
    print("geomean Δ% = GM(target / base) - 1, GM over benchmarks; benchmarks weigh equally")
    print("total Δ%   = (Σ target - Σ base) / Σ base; large benchmarks dominate")
    print("min/max Δ% = best/worst single-benchmark Δ%")
    print(f"top lists  = changes passing a Welch t-test with |Δ%| ≥ {MIN_DELTA_PCT:g}%")


def _print_summary(measurements):
    if not measurements:
        print("No results to compare.")
        return
    show_pipeline = any(m.pipeline is not None for m in measurements)

    print()
    _print_overview_table(measurements, show_pipeline)

    for metric in _SUMMARY_DETAIL_METRICS:
        of_metric = [m for m in measurements if m.metric == metric]
        if not of_metric:
            continue
        regressions = [m for m in of_metric if m.outcome == "regressed"]
        improvements = [m for m in of_metric if m.outcome == "improved"]
        if not regressions and not improvements:
            _print_heading(f"{metric}: no significant changes")
            continue
        _print_largest_changes(metric, "regressions", regressions, show_pipeline)
        _print_largest_changes(metric, "improvements", improvements, show_pipeline)


def _print_overview_table(measurements, show_pipeline):
    """One row per (metric, pipeline), aggregated over all benchmarks."""
    groups = {}
    for m in measurements:
        groups.setdefault((m.metric, m.pipeline), []).append(m)

    delta_columns = ("geomean Δ%", "total Δ%", "min Δ%", "max Δ%")
    header = ["Metric"]
    if show_pipeline:
        header.append("Pipeline")
    header += ["n", *delta_columns]

    rows = []
    # Stable sort: pipelines of the same metric keep their first-seen order.
    group_keys = sorted(groups, key=lambda group_key: _METRIC_ORDER[group_key[0]])
    for metric, pipeline in group_keys:
        group = groups[(metric, pipeline)]
        deltas = [m.delta_pct for m in group if m.delta_pct is not None]

        row = [metric]
        if show_pipeline:
            row.append(pipeline)
        row += [
            str(len(group)),
            format_delta(_geomean_delta_pct(group)),
            format_delta(_total_delta_pct(group)),
            format_delta(min(deltas) if deltas else None),
            format_delta(max(deltas) if deltas else None),
        ]
        rows.append(row)

    column_color_fns = {}
    for column in delta_columns:
        column_color_fns[header.index(column)] = _delta_sign_color
    _print_table(header, rows, column_color_fns=column_color_fns)


def _print_largest_changes(metric, title, measurements, show_pipeline):
    if not measurements:
        _print_heading(f"{metric}: no significant {title}")
        return
    largest_first = sorted(measurements, key=lambda m: abs(m.delta_pct), reverse=True)
    shown = largest_first[:_SUMMARY_TOP_CHANGES]
    _print_heading(f"{metric}: largest {title} ({len(shown)} of {len(measurements)})")
    print()

    header = ["Benchmark"]
    if show_pipeline:
        header.append("Pipeline")
    header += ["Base", "Target", "Δ%"]

    rows = []
    for m in shown:
        row = [m.benchmark]
        if show_pipeline:
            row.append(m.pipeline)
        row += [
            format_value(m.base_mean, m.metric),
            format_value(m.target_mean, m.metric),
            format_delta(m.delta_pct),
        ]
        rows.append(row)

    _print_table(header, rows, column_color_fns={header.index("Δ%"): _delta_sign_color})


def _print_heading(text):
    print()
    print(colorize(f"=== {text} ===", "heading" if use_color() else None))


def _delta_sign_color(cell):
    if cell.startswith("+"):
        return "red"
    if cell.startswith("-"):
        return "green"
    return None


def _geomean_delta_pct(measurements):
    ratios = [m.target_mean / m.base_mean for m in measurements]
    return round((statistics.geometric_mean(ratios) - 1) * 100, 2)


def _total_delta_pct(measurements):
    base_total = sum(m.base_mean for m in measurements)
    target_total = sum(m.target_mean for m in measurements)
    return round((target_total - base_total) / base_total * 100, 2)
