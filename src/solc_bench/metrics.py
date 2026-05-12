"""Metric definitions, value formatting, and aggregation."""

import statistics

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
}

# Gas metrics from forge test --gas-report
GAS = {
    "deployment_gas": ("Total deployment gas via forge test --gas-report", "gas"),
    "method_gas": ("Total method call gas via forge test --gas-report", "gas"),
}

ALL_METRICS = {**SYSTEM, **COMPILER, **GAS}

# Keys that aren't measured metrics, not aggregated
_NON_METRIC_KEYS = {"exit_code", "errors", "error_messages"}


def format_value(value, metric):
    """Format a metric value for display."""
    unit = ALL_METRICS.get(metric, (None, None))[1]
    if unit in ("count", "bytes", "gas"):
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
    if unit in ("count", "bytes", "gas"):
        return f"{value:,.0f} ± {stddev:,.0f}"
    if unit == "seconds":
        return f"{value:.4f}s ± {stddev:.4f}s"
    if unit == "MiB":
        return f"{value:.0f} ± {stddev:.0f} MiB"
    return f"{value} ± {stddev}"


def format_delta(delta_pct):
    """Format a percentage delta for display."""
    if delta_pct is None:
        return "N/A"
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
