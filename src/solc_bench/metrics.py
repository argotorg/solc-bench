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
    "bytecode_size": ("Total bytecode size from compiler output", "bytes"),
}

# Gas metrics from forge test --gas-report
GAS = {
    "deployment_gas": ("Total deployment gas via forge test --gas-report", "gas"),
    "method_gas": ("Total method call gas via forge test --gas-report", "gas"),
}

ALL_METRICS = {**SYSTEM, **COMPILER, **GAS}


def format_value(value, metric):
    """Format a metric value for display."""
    unit = ALL_METRICS.get(metric, (None, None))[1]
    if unit in ("count", "bytes", "gas"):
        return f"{value:,.0f}"
    if unit == "seconds":
        return f"{value:.2f}s"
    if unit == "MiB":
        return f"{value:.0f} MiB"
    return f"{value}"


def format_delta(delta_pct):
    """Format a percentage delta for display."""
    if delta_pct is None:
        return "N/A"
    prefix = "+" if delta_pct > 0 else ""
    return f"{prefix}{delta_pct}%"


def aggregate(samples):
    """Aggregate multiple samples into per-metric stats."""
    if not samples:
        return {}

    all_keys = set()
    for s in samples:
        all_keys.update(s.keys())
    all_keys.discard("exit_code")
    all_keys.discard("errors")
    all_keys.discard("error_messages")

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
