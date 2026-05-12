"""User-facing output: progress, results, comparison tables."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from solc_bench import VERSION
from solc_bench import host
from solc_bench.metrics import (
    format_delta,
    format_value_with_stddev,
)


def _print_table(header, rows):
    cols = list(zip(*([header] + rows)))
    widths = [max(map(len, col)) for col in cols]
    sep = "  "

    def render(row):
        return sep.join(cell.ljust(w) for cell, w in zip(row, widths))

    print(render(header))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(render(row))


def _print_host_mismatch_banner(baseline_meta, target_meta):
    """Warn when baseline and target were measured on different hosts."""
    diffs = []
    for side, key, label in [
        ("hardware", "cpu_model",   "CPU"),
        ("hardware", "hostname",    "host"),
        ("hardware", "kernel",      "kernel"),
        ("environment", "governor",        "governor"),
        ("environment", "mitigations_off", "mitigations_off"),
        ("environment", "aslr",            "ASLR"),
        ("environment", "thp",             "THP"),
        ("environment", "smt_active",      "SMT"),
    ]:
        b = baseline_meta.get(side, {}).get(key)
        t = target_meta.get(side, {}).get(key)
        if b is None and t is None:
            continue
        if b != t:
            diffs.append(f"{label}: baseline={b!r} target={t!r}")
    if diffs:
        print()
        print("WARNING: baseline and target measured on different hosts or postures:")
        for d in diffs:
            print(f"  {d}")


def benchmark_start(name, pipeline, solc_settings):
    assert "optimizer" in solc_settings, "solc_settings must include optimizer"
    opt_str = "optimize" if solc_settings["optimizer"]["enabled"] else "no-optimize"
    print(
        f"  {name} ({pipeline}, {opt_str})...",
        file=sys.stderr,
        end="",
        flush=True,
    )


def benchmark_done(result, error_log=None):
    if result:
        cpu = result.get("cpu_time", {})
        errors = result.get("errors", 0)
        print(f" {cpu.get('median', 0):.1f}s", file=sys.stderr)
        if errors:
            msg = f"    WARNING: {errors} compilation error(s)"
            if error_log:
                msg += f", see {error_log}"
            print(msg, file=sys.stderr)
    else:
        print(file=sys.stderr)


def missing_input_file(name, input_file, source, version, benchmark_dir):
    print(
        f"  {name}: input file not found at {input_file}, skipping",
        file=sys.stderr,
    )
    if source:
        suffix = f" ({version})" if version else ""
        print(f"    source: {source}{suffix}", file=sys.stderr)
    print(
        f"    generate it with: solc-bench extract --solc <solc> "
        f"--project <path-to-project> --output-dir {benchmark_dir}",
        file=sys.stderr,
    )


def build_result_json(results, solc_version, iterations):
    return {
        "solc_bench_version": VERSION,
        "solc_version": solc_version,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "iterations": iterations,
        "hardware": host.hardware(),
        "environment": host.environment(),
        "results": results,
    }


def write_result_json(data, output_path, stdout=False):
    output_json = json.dumps(data, indent=2)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output_json)
        f.write("\n")
    print(f"\nResults written to {output_path}", file=sys.stderr)
    if stdout:
        print(output_json)


def write_comparison_json(result, output_path):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(f"Comparison written to {output_path}", file=sys.stderr)


def cross_version_table(result):
    print(f"Baseline: {result['baseline']['solc_version']}")
    print(f"Target:   {result['target']['solc_version']}")
    print(
        "\u0394% = (target - baseline) / baseline. Negative = improvement "
        "(lower is better), positive = regression."
    )
    _print_host_mismatch_banner(result["baseline"], result["target"])
    print()

    metric_names = list(dict.fromkeys(
        m
        for pipelines in result["benchmarks"].values()
        for comparison in pipelines.values()
        for m in comparison
        if m not in ("errors", "functions")
    ))

    if not metric_names:
        print("No results to compare.")
        return

    row_header = ["Benchmark", "Pipeline", "Metric", "Base", "Target", "\u0394%", "winner"]
    rows = []

    for name, pipelines in result["benchmarks"].items():
        for pipeline, comparison in pipelines.items():
            first = True
            for metric in metric_names:
                c = comparison.get(metric)
                if c is None:
                    continue
                delta_pct = c.get("delta_pct")
                rows.append(
                    [
                        name if first else "",
                        pipeline if first else "",
                        metric,
                        format_value_with_stddev(
                            c.get("baseline_median", 0),
                            c.get("baseline_stddev"),
                            metric,
                        ),
                        format_value_with_stddev(
                            c.get("target_median", 0),
                            c.get("target_stddev"),
                            metric,
                        ),
                        format_delta(delta_pct),
                        _format_winner(delta_pct, "target", "baseline"),
                    ]
                )
                first = False
            if not first:
                rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows)


def _shorten(text, width):
    """Middle-truncate so the start and tail of `text` both stay visible."""
    if len(text) <= width:
        return text
    left = (width - 3) // 2
    right = width - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def cross_version_per_function_table(result, sort_by="median", max_func_width=60):
    """Per-function gas deltas, all four stats per row, sorted by |delta_pct| of sort_by."""
    stats = ("min", "mean", "median", "max")
    if sort_by not in stats:
        raise ValueError(f"sort_by must be one of {stats}, got {sort_by}")

    print(f"\nPer-function gas (delta % per stat, sorted by |{sort_by}|):\n")
    row_header = ["Benchmark", "Pipeline", "Function", "Calls"] + [
        f"{s} \u0394" for s in stats
    ]
    rows = []

    for name, pipelines in result["benchmarks"].items():
        for pipeline, comparison in pipelines.items():
            funcs = comparison.get("functions") or {}

            entries = []
            for sig, sig_stats in funcs.items():
                key_stat = sig_stats.get(sort_by)
                key_delta = key_stat.get("delta_pct") if key_stat else None
                if key_delta is None:
                    continue
                entries.append((sig, sig_stats, key_delta))
            entries.sort(key=lambda e: abs(e[2]), reverse=True)

            first = True
            for sig, sig_stats, _ in entries:
                calls = sig_stats.get("calls", {}).get("baseline")
                row = [
                    name if first else "",
                    pipeline if first else "",
                    _shorten(sig, max_func_width),
                    f"{calls:,}" if isinstance(calls, int) else "",
                ]
                for s in stats:
                    s_data = sig_stats.get(s)
                    row.append(format_delta(s_data.get("delta_pct")) if s_data else "n/a")
                rows.append(row)
                first = False
            if not first:
                rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()
    if not rows:
        print("(no per-function gas data)")
        return
    _print_table(row_header, rows)


def cross_pipeline_table(result):
    print(f"solc:      {result['solc_version']}")
    print(f"timestamp: {result['timestamp']}")
    print(
        f"Pipeline comparison: {result['target_pipeline']} vs "
        f"{result['ref_pipeline']}"
    )
    print(
        "\u0394% = (target - ref) / ref. Negative = improvement "
        "(lower is better), positive = regression."
    )
    print()

    metric_names = list(dict.fromkeys(
        m
        for comparison in result["benchmarks"].values()
        for m in comparison
    ))

    if not metric_names:
        print("No results to compare.")
        return

    ref = result["ref_pipeline"]
    tgt = result["target_pipeline"]
    row_header = ["Benchmark", "Metric", tgt, ref, "\u0394%", "winner"]
    rows = []

    for name, comparison in result["benchmarks"].items():
        first = True
        for metric in metric_names:
            c = comparison.get(metric)
            if c is None:
                continue
            delta_pct = c.get("delta_pct")
            rows.append(
                [
                    name if first else "",
                    metric,
                    format_value_with_stddev(
                        c.get("target_median", 0),
                        c.get("target_stddev"),
                        metric,
                    ),
                    format_value_with_stddev(
                        c.get("ref_median", 0),
                        c.get("ref_stddev"),
                        metric,
                    ),
                    format_delta(delta_pct),
                    _format_winner(delta_pct, tgt, ref),
                ]
            )
            first = False
        if not first:
            rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows)


def _format_winner(delta_pct, target, ref):
    """Pick the winner based on signed delta. Only an exact 0 counts as a tie."""
    if delta_pct is None:
        return "n/a"
    if delta_pct == 0:
        return "tie"
    return target if delta_pct < 0 else ref
