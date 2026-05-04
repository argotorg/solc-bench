"""User-facing output: progress, results, comparison tables."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from solc_bench import VERSION
from solc_bench.metrics import format_delta, format_ratio, format_value


def _print_table(header, rows):
    all_rows = [header] + rows
    widths = [max(len(row[i]) for row in all_rows) for i in range(len(header))]
    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(row))))


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


def build_result_json(results, solc_version, iterations):
    return {
        "solc_bench_version": VERSION,
        "solc_version": solc_version,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "iterations": iterations,
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


def comparison_table(result):
    print(f"Baseline: {result['baseline']['solc_version']}")
    print(f"Target:   {result['target']['solc_version']}")
    print()

    metric_names = []
    for pipelines in result["comparisons"].values():
        for comparison in pipelines.values():
            for m in comparison:
                if m != "errors" and m not in metric_names:
                    metric_names.append(m)

    if not metric_names:
        print("No results to compare.")
        return

    row_header = ["Benchmark", "Pipeline", "Metric", "Base", "Target", "Delta"]
    rows = []

    for name, pipelines in result["comparisons"].items():
        for pipeline, comparison in pipelines.items():
            first = True
            for metric in metric_names:
                c = comparison.get(metric)
                if c is None:
                    continue
                rows.append(
                    [
                        name if first else "",
                        pipeline if first else "",
                        metric,
                        format_value(c.get("baseline_median", 0), metric),
                        format_value(c.get("target_median", 0), metric),
                        format_delta(c.get("delta_pct")),
                    ]
                )
                first = False
            if not first:
                rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows)


def pipeline_comparison_table(result):
    print(f"solc:      {result['solc_version']}")
    print(f"timestamp: {result['timestamp']}")
    print(
        f"Pipeline comparison: {result['target_pipeline']} vs "
        f"{result['ref_pipeline']}"
    )
    print()

    metric_names = []
    for comparison in result["comparisons"].values():
        for m in comparison:
            if m not in metric_names:
                metric_names.append(m)

    if not metric_names:
        print("No results to compare.")
        return

    ref = result["ref_pipeline"]
    tgt = result["target_pipeline"]
    row_header = ["Benchmark", "Metric", tgt, ref, f"{tgt}/{ref}"]
    rows = []

    for name, comparison in result["comparisons"].items():
        first = True
        for metric in metric_names:
            c = comparison.get(metric)
            if c is None:
                continue
            rows.append(
                [
                    name if first else "",
                    metric,
                    format_value(c.get("target_median", 0), metric),
                    format_value(c.get("ref_median", 0), metric),
                    format_ratio(c.get("ratio")),
                ]
            )
            first = False
        if not first:
            rows.append([""] * len(row_header))

    if rows and rows[-1] == [""] * len(row_header):
        rows.pop()

    _print_table(row_header, rows)
