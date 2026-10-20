"""Compare two benchmark result sets."""

import math
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from solc_bench.metrics import SIGNIFICANCE_ALPHA, welch_test
from solc_bench.results import FunctionGas, Number, PipelineResult, ResultFile, RunInfo, Stats


class MetricComparison(BaseModel):
    """One metric of one benchmark, base vs target.

    Holds mean + stddev + delta_pct, plus a Welch t-test: `t` is the
    t-statistic, `p` its two-sided p-value, and `significant` is True/False
    (`p` below the significance level) when it can be computed, or None
    when there are too few iterations to tell.
    """

    base_mean: Number | None
    target_mean: Number | None
    base_stddev: float | None
    target_stddev: float | None
    delta_pct: float | None
    t: float | None
    p: float | None
    significant: bool | None

    def to_json(self, base_label: str) -> dict[str, Any]:
        return {
            f"{base_label}_mean": self.base_mean,
            "target_mean": self.target_mean,
            f"{base_label}_stddev": self.base_stddev,
            "target_stddev": self.target_stddev,
            "delta_pct": self.delta_pct,
            "t": self.t,
            "p": self.p,
            "significant": self.significant,
        }


class StatDelta(BaseModel):
    baseline: Number
    target: Number
    delta_pct: float | None


class FunctionComparison(BaseModel):
    """Per-function gas, for each of min/mean/median/max both sides report."""

    stats: dict[str, StatDelta]
    base_calls: int | None
    target_calls: int | None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {stat: delta.model_dump() for stat, delta in self.stats.items()}
        if self.base_calls is not None:
            out["calls"] = {"baseline": self.base_calls, "target": self.target_calls}
        return out


class PipelineComparison(BaseModel):
    """One benchmark/pipeline pair, metric by metric."""

    metrics: dict[str, MetricComparison]
    functions: dict[str, FunctionComparison] = {}

    def to_json(self, base_label: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            metric: comparison.to_json(base_label) for metric, comparison in self.metrics.items()
        }
        if self.functions:
            out["functions"] = {sig: f.to_json() for sig, f in self.functions.items()}
        return out


class CrossVersionComparison(BaseModel):
    """Two result files, benchmarks[benchmark][pipeline]."""

    baseline: RunInfo
    target: RunInfo
    benchmarks: dict[str, dict[str, PipelineComparison]]

    def comparisons(self) -> Iterator[tuple[str, str | None, PipelineComparison]]:
        for name, pipelines in self.benchmarks.items():
            for pipeline, comparison in pipelines.items():
                yield name, pipeline, comparison

    def to_json(self) -> dict[str, Any]:
        meta = {"solc_bench_version"}
        return {
            "mode": "cross-version",
            "baseline": self.baseline.model_dump(exclude=meta),
            "target": self.target.model_dump(exclude=meta),
            "benchmarks": {
                name: {p: c.to_json("baseline") for p, c in pipelines.items()}
                for name, pipelines in self.benchmarks.items()
            },
        }


class CrossPipelineComparison(BaseModel):
    """Two pipelines in one result file, benchmarks[benchmark]."""

    run: RunInfo
    ref_pipeline: str
    target_pipeline: str
    benchmarks: dict[str, PipelineComparison]

    def comparisons(self) -> Iterator[tuple[str, str | None, PipelineComparison]]:
        for name, comparison in self.benchmarks.items():
            yield name, None, comparison

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": "cross-pipeline",
            "solc_version": self.run.solc_version,
            "timestamp": self.run.timestamp,
            "iterations": self.run.iterations,
            "ref_pipeline": self.ref_pipeline,
            "target_pipeline": self.target_pipeline,
            "benchmarks": {name: c.to_json("ref") for name, c in self.benchmarks.items()},
        }


def _delta_pct(baseline, target):
    """Percent change of target vs baseline. None if baseline is not positive."""
    if baseline is None or target is None or baseline <= 0:
        return None
    return round((target - baseline) / baseline * 100, 2)


def _metric_comparison(base: Stats | None, target: Stats | None) -> MetricComparison:
    t = None
    p = None
    significant = None
    if base is not None and target is not None:
        t, p = welch_test(base.values, target.values)
        if t is None:
            significant = None
        elif math.isinf(t):
            # A difference with no measurable noise is significant, but inf is
            # not valid JSON, so store the verdict and drop t.
            significant, t, p = True, None, round(p, 4)
        else:
            significant = p < SIGNIFICANCE_ALPHA
            t, p = round(t, 2), round(p, 4)
    base_mean = base.mean if base is not None else None
    target_mean = target.mean if target is not None else None
    return MetricComparison(
        base_mean=base_mean,
        target_mean=target_mean,
        base_stddev=base.stddev if base is not None else None,
        target_stddev=target.stddev if target is not None else None,
        delta_pct=_delta_pct(base_mean, target_mean),
        t=t,
        p=p,
        significant=significant,
    )


def _compare_metrics(base: PipelineResult, target: PipelineResult) -> dict[str, MetricComparison]:
    return {
        metric: _metric_comparison(base.metrics.get(metric), target.metrics.get(metric))
        for metric in dict.fromkeys([*base.metrics, *target.metrics])
    }


_FUNCTION_STATS = ("min", "mean", "median", "max")


def _compare_functions(
    base_funcs: dict[str, FunctionGas], tgt_funcs: dict[str, FunctionGas]
) -> dict[str, FunctionComparison]:
    """Per-function deltas across min/mean/median/max."""
    out = {}
    for sig, base_func in base_funcs.items():
        tgt_func = tgt_funcs.get(sig)
        if tgt_func is None:
            continue
        stats = {}
        for stat in _FUNCTION_STATS:
            base_v = getattr(base_func, stat)
            tgt_v = getattr(tgt_func, stat)
            if base_v is None or tgt_v is None:
                continue
            stats[stat] = StatDelta(
                baseline=base_v, target=tgt_v, delta_pct=_delta_pct(base_v, tgt_v)
            )
        out[sig] = FunctionComparison(
            stats=stats, base_calls=base_func.calls, target_calls=tgt_func.calls
        )
    return out


def compare_compiler_versions(baseline: ResultFile, target: ResultFile) -> CrossVersionComparison:
    """Compare two result sets, return per-benchmark per-pipeline deltas."""
    benchmarks = {}

    for name, pipelines in baseline.results.items():
        for pipeline, base_result in pipelines.items():
            tgt_result = target.results.get(name, {}).get(pipeline)
            if tgt_result is None:
                continue
            benchmarks.setdefault(name, {})[pipeline] = PipelineComparison(
                metrics=_compare_metrics(base_result, tgt_result),
                functions=_compare_functions(base_result.functions, tgt_result.functions),
            )

    return CrossVersionComparison(
        baseline=baseline.run_info, target=target.run_info, benchmarks=benchmarks
    )


def compare_pipelines(
    results: ResultFile, ref_pipeline: str, target_pipeline: str
) -> CrossPipelineComparison:
    """Compare two pipelines within a single result set, return per-benchmark deltas."""
    benchmarks = {}

    for name, pipelines in results.results.items():
        ref_result = pipelines.get(ref_pipeline)
        tgt_result = pipelines.get(target_pipeline)
        if ref_result is None or tgt_result is None:
            continue
        # TODO: per-function ratios across pipelines (e.g. evmasm vs ir
        # for the same function) could be useful. But it is currently
        # not supported.
        benchmarks[name] = PipelineComparison(metrics=_compare_metrics(ref_result, tgt_result))

    return CrossPipelineComparison(
        run=results.run_info,
        ref_pipeline=ref_pipeline,
        target_pipeline=target_pipeline,
        benchmarks=benchmarks,
    )
