"""Compare two benchmark result files and format the output."""

import json
from pathlib import Path


def load_results(path):
    """Load a benchmark result JSON file."""
    with open(path) as f:
        return json.load(f)


def compare_results(baseline, target):
    """Compare two result sets, return per-benchmark per-pipeline deltas."""
    comparisons = {}

    for name, pipelines in baseline.get("results", {}).items():
        for pipeline, base_metrics in pipelines.items():
            tgt_metrics = target.get("results", {}).get(name, {}).get(pipeline)
            if tgt_metrics is None:
                continue

            comparison = {}
            for metric, base_data in base_metrics.items():
                if metric == "errors":
                    comparison["errors"] = {
                        "baseline": base_data,
                        "target": tgt_metrics.get("errors", 0),
                    }
                    continue

                tgt_data = tgt_metrics.get(metric)
                if tgt_data is None:
                    continue

                base_median = base_data.get("median", 0)
                tgt_median = tgt_data.get("median", 0)

                if base_median > 0:
                    delta_pct = round((tgt_median - base_median) / base_median * 100, 2)
                else:
                    delta_pct = None

                comparison[metric] = {
                    "baseline_median": base_median,
                    "target_median": tgt_median,
                    "delta_pct": delta_pct,
                }

            if name not in comparisons:
                comparisons[name] = {}
            comparisons[name][pipeline] = comparison

    return {
        "baseline": {
            "solc_version": baseline.get("solc_version", "unknown"),
            "timestamp": baseline.get("timestamp", ""),
        },
        "target": {
            "solc_version": target.get("solc_version", "unknown"),
            "timestamp": target.get("timestamp", ""),
        },
        "comparisons": comparisons,
    }


def format_delta(delta_pct):
    """Format a percentage delta for display."""
    if delta_pct is None:
        return "N/A"
    prefix = "+" if delta_pct > 0 else ""
    return f"{prefix}{delta_pct}%"


def format_value(value, metric):
    """Format a metric value for display based on its type."""
    if metric in ("instructions", "cycles", "bytecode_size"):
        return f"{value:,.0f}"
    if metric in ("cpu_time", "wall_time"):
        return f"{value:.2f}s"
    if metric == "peak_rss":
        return f"{value:.0f} MiB"
    return f"{value}"


def print_comparison_table(result):
    """Print a human-readable comparison table to stdout."""
    print(f"Baseline: {result['baseline']['solc_version']}")
    print(f"Target:   {result['target']['solc_version']}")
    print()

    # Determine which metrics are present across all comparisons.
    metric_names = []
    for pipelines in result["comparisons"].values():
        for comparison in pipelines.values():
            for m in comparison:
                if m != "errors" and m not in metric_names:
                    metric_names.append(m)

    if not metric_names:
        print("No results to compare.")
        return

    header = ["Benchmark", "Pipeline", "Metric", "Base", "Target", "Delta"]
    rows = []

    for name, pipelines in result["comparisons"].items():
        for pipeline, comparison in pipelines.items():
            first = True
            for metric in metric_names:
                c = comparison.get(metric)
                if c is None:
                    continue
                rows.append(
                    [
                        name if first else "",
                        pipeline if first else "",
                        metric,
                        format_value(c.get("baseline_median", 0), metric),
                        format_value(c.get("target_median", 0), metric),
                        format_delta(c.get("delta_pct")),
                    ]
                )
                first = False
            if not first:
                rows.append([""] * len(header))  # blank separator

    # Remove trailing blank row.
    if rows and rows[-1] == [""] * len(header):
        rows.pop()

    all_rows = [header] + rows
    widths = [max(len(row[i]) for row in all_rows) for i in range(len(header))]

    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


def write_comparison(result, output_path):
    """Write comparison result to a JSON file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
