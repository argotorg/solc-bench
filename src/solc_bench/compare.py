"""Compare two benchmark result sets."""

import json


def load_results(path):
    """Load a benchmark result JSON file."""
    with open(path, encoding="utf-8") as f:
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
