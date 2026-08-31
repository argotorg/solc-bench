"""Metric definitions, value formatting, and aggregation."""

import math
import statistics
import warnings

from scipy.stats import ttest_ind

# A difference is "significant" when the two-sided Welch t-test p-value is
# below this level. The p-value is taken against the Welch-Satterthwaite
# degrees of freedom, so the bar automatically tightens for small samples
# instead of relying on a fixed |t| cutoff that ignores how many iterations ran.
SIGNIFICANCE_ALPHA = 0.05

# Practical-significance floor: a difference smaller than this percentage is
# treated as "no winner" even when the t-test calls it statistically real.
# Near-deterministic metrics (e.g. instructions) can flag a 0.01% change as
# highly significant; that is real but too small to act on.
MIN_DELTA_PCT = 0.10

# System-level metrics collected by the benchmark harness (perf stat / rusage)
SYSTEM = {
    "cpu_time": ("CPU time (user + system) via os.wait4()", "seconds"),
    "wall_time": ("Wall clock time via time.monotonic()", "seconds"),
    "peak_rss": ("Peak resident set size via rusage.ru_maxrss", "MiB"),
    "instructions": ("Hardware instruction count via perf stat", "count"),
    "cycles": ("CPU cycle count via perf stat", "count"),
}

# Metrics parsed from compiler output
COMPILER = {
    "creation_size": (
        "Sum of creation bytecode size across all contracts in the Standard JSON Output",
        "bytes",
    ),
    "runtime_size": (
        "Sum of runtime bytecode size across all contracts in the Standard JSON Output",
        "bytes",
    ),
    "ethdebug_size": (
        "Serialized ETHDebug JSON output size across all requested ETHDebug artifacts",
        "bytes",
    ),
}

# Gas metrics from forge test --gas-report
GAS = {
    "deployment_gas": ("Total deployment gas via forge test --gas-report", "gas"),
    "method_gas": ("Total method call gas via forge test --gas-report", "gas"),
}

ALL_METRICS = {**SYSTEM, **COMPILER, **GAS}

# Recorded in the result JSON but kept out of every table, plot and listing: a
# cycle count is neither a clean work measure (instructions retire in very
# different cycle counts) nor a clean time measure, and memory stalls dominate
# it, so it mostly reflects cache behaviour on the measuring machine.
HIDDEN = {"cycles"}

DISPLAYED_METRICS = {k: v for k, v in ALL_METRICS.items() if k not in HIDDEN}

# Keys that aren't measured metrics, not aggregated
_NON_METRIC_KEYS = {"exit_code", "errors", "error_messages"}


def humanize(value):
    """Compact large counts with an SI suffix: 74.1261G, 4.5600M, 138."""
    a = abs(value)
    if a >= 1e9:
        return f"{value / 1e9:.4f}G"
    if a >= 1e6:
        return f"{value / 1e6:.4f}M"
    if a >= 1e3:
        return f"{value / 1e3:.4f}k"
    return f"{value:,.0f}"


def _format_large_bytes(value):
    return f"{value / 1024 / 1024:.2f} MiB"


def format_value(value, metric):
    """Format a metric value for display."""
    unit = ALL_METRICS.get(metric, (None, None))[1]
    if metric == "ethdebug_size":
        return _format_large_bytes(value)
    if unit == "count":
        return humanize(value)
    if unit in ("bytes", "gas"):
        return f"{value:,.0f}"
    if unit == "seconds":
        return f"{value:.4f}s"
    if unit == "MiB":
        return f"{value:.0f} MiB"
    return f"{value}"


def format_value_with_stddev(value, stddev, metric):
    """Format a metric value with its standard deviation, e.g. '2.83s ± 0.02s'."""
    if stddev is None:
        return format_value(value, metric)
    unit = ALL_METRICS.get(metric, (None, None))[1]
    if metric == "ethdebug_size":
        return f"{_format_large_bytes(value)} ± {_format_large_bytes(stddev)}"
    if unit == "count":
        return f"{humanize(value)} ± {humanize(stddev)}"
    if unit in ("bytes", "gas"):
        return f"{value:,.0f} ± {stddev:,.0f}"
    if unit == "seconds":
        return f"{value:.4f}s ± {stddev:.4f}s"
    if unit == "MiB":
        return f"{value:.0f} ± {stddev:.0f} MiB"
    return f"{value} ± {stddev}"


def welch_test(v1, v2):
    """Welch's t-test (unequal variances): ``(t, two-sided p-value)``.

    ``(None, None)`` when undefined (fewer than two samples on a side). When
    both samples are constant: ``(inf, 0.0)`` if their means differ (a real
    difference with no measurable noise, e.g. a changed bytecode size), else
    ``(0.0, 1.0)``. Otherwise delegates to ``scipy.stats.ttest_ind`` with
    ``equal_var=False``; ``t`` is signed so a larger ``v2`` mean is positive.
    """
    if not v1 or not v2 or len(v1) < 2 or len(v2) < 2:
        return None, None
    if statistics.stdev(v1) == 0 and statistics.stdev(v2) == 0:
        if statistics.mean(v1) != statistics.mean(v2):
            return math.inf, 0.0
        return 0.0, 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = ttest_ind(v2, v1, equal_var=False)
    return float(result.statistic), float(result.pvalue)


def format_delta(delta_pct):
    """Format a percentage delta for display."""
    if delta_pct is None:
        return "N/A"
    if delta_pct == 0:  # also catches -0.0 and values that rounded to zero
        return "0.0%"
    prefix = "+" if delta_pct > 0 else ""
    return f"{prefix}{delta_pct}%"


def format_ratio(value):
    """Format a multiplicative ratio for display (e.g. 2.17x)."""
    if value is None:
        return "n/a"
    return f"{value:.2f}x"


def aggregate(samples):
    """Aggregate multiple samples into per-metric stats."""
    if not samples:
        return {}

    all_keys = {k for s in samples for k in s} - _NON_METRIC_KEYS

    result = {}
    for key in sorted(all_keys):
        values = [s[key] for s in samples if key in s]
        if not values:
            continue
        result[key] = {
            "values": values,
            "median": statistics.median(values),
            "mean": statistics.mean(values),
        }
        if len(values) > 1:
            result[key]["stddev"] = statistics.stdev(values)

    if "errors" in samples[-1]:
        result["errors"] = samples[-1]["errors"]
    if "error_messages" in samples[-1]:
        result["error_messages"] = samples[-1]["error_messages"]

    return result
