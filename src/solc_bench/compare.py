"""Compare two benchmark result sets."""

import json
import math

from solc_bench.metrics import T_SIGNIFICANT, welch_t


def load_results(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _delta_pct(baseline, target):
    """Percent change of target vs baseline. None if baseline is not positive."""
    if baseline is None or target is None or baseline <= 0:
        return None
    return round((target - baseline) / baseline * 100, 2)


def _metric_comparison(base_data, tgt_data, base_label="baseline"):
    """Build a comparison record for a single metric.

    Holds median + stddev + delta_pct, plus a Welch t-test: ``t`` is the
    t-statistic and ``significant`` is True/False when it can be computed, or
    None when there are too few iterations to tell.
    """
    base_median = base_data.get("median") if base_data is not None else None
    tgt_median = tgt_data.get("median") if tgt_data is not None else None
    t = None
    significant = None
    if base_data is not None and tgt_data is not None:
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
        f"{base_label}_stddev": (
            base_data.get("stddev") if base_data is not None else None
        ),
        "target_stddev": tgt_data.get("stddev") if tgt_data is not None else None,
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
            for metric in dict.fromkeys([*base_metrics, *tgt_metrics]):
                base_data = base_metrics.get(metric)
                tgt_data = tgt_metrics.get(metric)
                if metric == "errors":
                    comparison["errors"] = {
                        "baseline": base_data,
                        "target": tgt_metrics.get("errors", 0),
                    }
                    continue

                if metric == "functions":
                    comparison["functions"] = _compare_functions(
                        base_data or {}, tgt_metrics.get("functions", {})
                    )
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
        for metric in dict.fromkeys([*ref_metrics, *tgt_metrics]):
            # TODO: per-function ratios across pipelines (e.g. evmasm vs ir
            # for the same function) could be useful. But it is currently
            # not supported.
            if metric in ("errors", "functions"):
                continue
            ref_data = ref_metrics.get(metric)
            tgt_data = tgt_metrics.get(metric)

            comparison[metric] = _metric_comparison(
                ref_data, tgt_data, base_label="ref"
            )

        benchmarks[name] = comparison

    return {
        "solc_version": results.get("solc_version", "unknown"),
        "timestamp": results.get("timestamp", ""),
        "ref_pipeline": ref_pipeline,
        "target_pipeline": target_pipeline,
        "benchmarks": benchmarks,
    }


def compare_ethdebug_branches(
    baseline,
    target,
    ref_pipeline="ir",
    ethdebug_pipeline="ir-ethdebug",
    baseline_label="baseline",
    target_label="target",
):
    """Compare ETHDebug overhead runs across two compiler result sets.

    The expected inputs are two result files produced with ``run
    --ethdebug-overhead``.  For each benchmark/metric, the report keeps the
    same-pipeline branch comparison for ``ethdebug_pipeline`` and
    ``ref_pipeline``, plus the ETHDebug overhead within each branch.
    """
    benchmarks = {}

    for name in dict.fromkeys(
        [
            *baseline.get("results", {}),
            *target.get("results", {}),
        ]
    ):
        baseline_pipelines = baseline.get("results", {}).get(name, {})
        target_pipelines = target.get("results", {}).get(name, {})
        baseline_ref = baseline_pipelines.get(ref_pipeline, {})
        baseline_ethdebug = baseline_pipelines.get(ethdebug_pipeline, {})
        target_ref = target_pipelines.get(ref_pipeline, {})
        target_ethdebug = target_pipelines.get(ethdebug_pipeline, {})

        metrics = dict.fromkeys(
            [
                *baseline_ref,
                *baseline_ethdebug,
                *target_ref,
                *target_ethdebug,
            ]
        )

        metric_results = {}
        for metric in metrics:
            if metric in ("errors", "functions"):
                continue

            baseline_ethdebug_data = baseline_ethdebug.get(metric)
            target_ethdebug_data = target_ethdebug.get(metric)
            baseline_ref_data = baseline_ref.get(metric)
            target_ref_data = target_ref.get(metric)

            ethdebug_branch = _metric_comparison(
                baseline_ethdebug_data,
                target_ethdebug_data,
            )
            ref_branch = _metric_comparison(
                baseline_ref_data,
                target_ref_data,
            )
            baseline_overhead = _metric_comparison(
                baseline_ref_data,
                baseline_ethdebug_data,
                base_label="ref",
            )
            target_overhead = _metric_comparison(
                target_ref_data,
                target_ethdebug_data,
                base_label="ref",
            )

            baseline_overhead_pct = baseline_overhead.get("delta_pct")
            target_overhead_pct = target_overhead.get("delta_pct")
            overhead_delta_pct_points = None
            if baseline_overhead_pct is not None and target_overhead_pct is not None:
                overhead_delta_pct_points = round(
                    target_overhead_pct - baseline_overhead_pct,
                    2,
                )

            metric_results[metric] = {
                "ethdebug_branch": ethdebug_branch,
                "ref_branch": ref_branch,
                "baseline_overhead": baseline_overhead,
                "target_overhead": target_overhead,
                "overhead_delta_pct_points": overhead_delta_pct_points,
            }

        if metric_results:
            benchmarks[name] = metric_results

    baseline_meta = _side_meta(baseline)
    baseline_meta["label"] = baseline_label
    target_meta = _side_meta(target)
    target_meta["label"] = target_label
    return {
        "mode": "ethdebug-branches",
        "baseline": baseline_meta,
        "target": target_meta,
        "ref_pipeline": ref_pipeline,
        "ethdebug_pipeline": ethdebug_pipeline,
        "benchmarks": benchmarks,
    }
