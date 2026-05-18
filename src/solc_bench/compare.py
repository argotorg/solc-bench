"""Compare two benchmark result sets."""

import json
import math

from solc_bench.metrics import T_SIGNIFICANT, welch_t


def load_results(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _delta_pct(baseline, target):
    """Percent change of target vs baseline. None if baseline is not positive."""
    if baseline <= 0:
        return None
    return round((target - baseline) / baseline * 100, 2)


def _metric_comparison(base_data, tgt_data, base_label="baseline"):
    """Build a comparison record for a single metric.

    Holds median + stddev + delta_pct, plus a Welch t-test: ``t`` is the
    t-statistic and ``significant`` is True/False when it can be computed, or
    None when there are too few iterations to tell.
    """
    base_median = base_data.get("median", 0)
    tgt_median = tgt_data.get("median", 0)
    t = welch_t(base_data.get("values"), tgt_data.get("values"))
    if t is None:
        significant = None
    elif math.isinf(t):
        # Infinite t (a difference with no measurable noise) is significant,
        # but inf is not valid JSON, so store the verdict and drop t.
        significant, t = True, None
    else:
        significant, t = abs(t) > T_SIGNIFICANT, round(t, 2)
    return {
        f"{base_label}_median": base_median,
        "target_median": tgt_median,
        f"{base_label}_stddev": base_data.get("stddev"),
        "target_stddev": tgt_data.get("stddev"),
        "delta_pct": _delta_pct(base_median, tgt_median),
        "t": t,
        "significant": significant,
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
        "baseline": _side_meta(baseline),
        "target": _side_meta(target),
        "benchmarks": benchmarks,
    }


def _side_meta(result):
    """Pick out the metadata fields that describe a single result file."""
    return {
        "solc_version": result.get("solc_version", "unknown"),
        "timestamp": result.get("timestamp", ""),
        "hardware": result.get("hardware", {}),
        "environment": result.get("environment", {}),
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
