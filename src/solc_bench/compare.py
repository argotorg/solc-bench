"""Compare two benchmark result sets."""

import json


def load_results(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _delta_pct(baseline, target):
    """Percent change of target vs baseline. None if baseline is not positive."""
    if baseline <= 0:
        return None
    return round((target - baseline) / baseline * 100, 2)


def _metric_comparison(base_data, tgt_data, base_label="baseline"):
    """Build a comparison record for a single metric (median + stddev + delta_pct)."""
    base_median = base_data.get("median", 0)
    tgt_median = tgt_data.get("median", 0)
    return {
        f"{base_label}_median": base_median,
        "target_median": tgt_median,
        f"{base_label}_stddev": base_data.get("stddev"),
        "target_stddev": tgt_data.get("stddev"),
        "delta_pct": _delta_pct(base_median, tgt_median),
    }


_FUNCTION_STATS = ("min", "mean", "median", "max")


def _compare_functions(base_funcs, tgt_funcs):
    """Per-function deltas across min/mean/median/max."""
    out = {}
    for sig, base_func in base_funcs.items():
        tgt_func = tgt_funcs.get(sig)
        if tgt_func is None:
            continue
        stats = {}
        for stat in _FUNCTION_STATS:
            base_v = base_func.get(stat)
            tgt_v = tgt_func.get(stat)
            if base_v is None or tgt_v is None:
                continue
            stats[stat] = {
                "baseline": base_v,
                "target": tgt_v,
                "delta_pct": _delta_pct(base_v, tgt_v),
            }
        if "calls" in base_func:
            stats["calls"] = {
                "baseline": base_func["calls"],
                "target": tgt_func.get("calls"),
            }
        out[sig] = stats
    return out


def compare_compiler_versions(baseline, target):
    """Compare two result sets, return per-benchmark per-pipeline deltas."""
    benchmarks = {}

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

                if metric == "functions":
                    comparison["functions"] = _compare_functions(
                        base_data, tgt_metrics.get("functions", {})
                    )
                    continue

                tgt_data = tgt_metrics.get(metric)
                if tgt_data is None:
                    continue

                comparison[metric] = _metric_comparison(base_data, tgt_data)

            if name not in benchmarks:
                benchmarks[name] = {}
            benchmarks[name][pipeline] = comparison

    return {
        "baseline": {
            "solc_version": baseline.get("solc_version", "unknown"),
            "timestamp": baseline.get("timestamp", ""),
        },
        "target": {
            "solc_version": target.get("solc_version", "unknown"),
            "timestamp": target.get("timestamp", ""),
        },
        "benchmarks": benchmarks,
    }


def compare_pipelines(results, ref_pipeline, target_pipeline):
    """Compare two pipelines within a single result set, return per-benchmark deltas."""
    benchmarks = {}

    for name, pipelines in results.get("results", {}).items():
        ref_metrics = pipelines.get(ref_pipeline)
        tgt_metrics = pipelines.get(target_pipeline)
        if ref_metrics is None or tgt_metrics is None:
            continue

        comparison = {}
        for metric, ref_data in ref_metrics.items():
            # TODO: per-function ratios across pipelines (e.g. evmasm vs ir
            # for the same function) could be useful. But it is currently
            # not supported.
            if metric in ("errors", "functions"):
                continue
            tgt_data = tgt_metrics.get(metric)
            if tgt_data is None:
                continue

            comparison[metric] = _metric_comparison(ref_data, tgt_data, base_label="ref")

        benchmarks[name] = comparison

    return {
        "solc_version": results.get("solc_version", "unknown"),
        "timestamp": results.get("timestamp", ""),
        "ref_pipeline": ref_pipeline,
        "target_pipeline": target_pipeline,
        "benchmarks": benchmarks,
    }
