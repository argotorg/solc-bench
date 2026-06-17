"""Metric definitions, value formatting, and aggregation."""

import math
import statistics

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
    """Welch t-statistic and two-sided p-value for two small samples.

    Returns ``(t, p)``. ``(None, None)`` when undefined (fewer than two
    samples on a side). When both samples have zero variance: ``(inf, 0.0)``
    if their means differ (a real difference with no measurable noise), else
    ``(0.0, 1.0)``. The p-value uses the Welch-Satterthwaite degrees of
    freedom, so a given ``|t|`` is judged against the right t-distribution
    rather than a fixed cutoff.
    """
    if not v1 or not v2 or len(v1) < 2 or len(v2) < 2:
        return None, None
    n1, n2 = len(v1), len(v2)
    var1 = statistics.stdev(v1) ** 2 / n1
    var2 = statistics.stdev(v2) ** 2 / n2
    se = math.sqrt(var1 + var2)
    if se == 0:
        if statistics.mean(v2) != statistics.mean(v1):
            return math.inf, 0.0
        return 0.0, 1.0
    t = (statistics.mean(v2) - statistics.mean(v1)) / se
    df = (var1 + var2) ** 2 / (var1**2 / (n1 - 1) + var2**2 / (n2 - 1))
    # Two-sided p-value: P(|T_df| > |t|) = I_x(df/2, 1/2), x = df / (df + t^2).
    p = _incomplete_beta(df / 2.0, 0.5, df / (df + t * t))
    return t, p


def _incomplete_beta(a, b, x):
    """Regularized incomplete beta I_x(a, b), the t-distribution tail kernel."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(a, b, x) / a
    return 1.0 - front * _beta_cf(b, a, 1.0 - x) / b


def _beta_cf(a, b, x):
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    max_iter, eps, tiny = 200, 3.0e-16, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


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
