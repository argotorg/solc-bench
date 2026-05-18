"""Metric definitions, value formatting, and aggregation."""

import math
import os
import statistics
import sys

# |t| threshold above which a difference is called "significant". With only a
# few iterations per build the Welch df is tiny (~2-4), where the 95% two-sided
# critical value of t is roughly 3-4; 4.0 is a deliberately conservative cutoff.
T_SIGNIFICANT = 4.0

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
}

# Gas metrics from forge test --gas-report
GAS = {
    "deployment_gas": ("Total deployment gas via forge test --gas-report", "gas"),
    "method_gas": ("Total method call gas via forge test --gas-report", "gas"),
}

ALL_METRICS = {**SYSTEM, **COMPILER, **GAS}

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


def format_value(value, metric):
    """Format a metric value for display."""
    unit = ALL_METRICS.get(metric, (None, None))[1]
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
    if unit == "count":
        return f"{humanize(value)} ± {humanize(stddev)}"
    if unit in ("bytes", "gas"):
        return f"{value:,.0f} ± {stddev:,.0f}"
    if unit == "seconds":
        return f"{value:.4f}s ± {stddev:.4f}s"
    if unit == "MiB":
        return f"{value:.0f} ± {stddev:.0f} MiB"
    return f"{value} ± {stddev}"


def welch_t(v1, v2):
    """Welch t-statistic for two small samples. None if undefined.

    Returns ``math.inf`` when both samples have zero variance but different
    means (a difference with no measurable noise).
    """
    if not v1 or not v2 or len(v1) < 2 or len(v2) < 2:
        return None
    s1, s2 = statistics.stdev(v1), statistics.stdev(v2)
    se = math.sqrt(s1**2 / len(v1) + s2**2 / len(v2))
    if se == 0:
        return math.inf if statistics.mean(v2) != statistics.mean(v1) else 0.0
    return (statistics.mean(v2) - statistics.mean(v1)) / se


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


_ANSI = {"green": "\033[32m", "red": "\033[31m", "reset": "\033[0m"}


def use_color():
    """True only when stdout is an interactive terminal, not a file or pipe.

    Honors the NO_COLOR convention (https://no-color.org/) as an opt-out.
    """
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def colorize(text, color):
    """Wrap text in an ANSI color ('green'/'red'); a no-op when color is None."""
    if color is None:
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


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
