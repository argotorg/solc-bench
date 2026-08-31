#!/usr/bin/env python3
"""Compare one metric across bench-results.json runs, or across pipelines.

One row per benchmark (readable with dozens of projects), sorted by effect size,
percentages relative to a baseline. Each series is a dot (median) with whiskers
(min-max over its iterations); the baseline is a grey band, so the band is the
noise floor -- a dot inside it is not distinguishable from run jitter.

Two things vary in a result file: which run it came from and which pipeline
compiled it. --compare picks which of the two is put side by side; the other
becomes one panel per value.

    # runs against each other, one panel per pipeline (the default)
    ./.venv/bin/python scripts/plot_bench.py \
        base=out-muh1/bench-results.json \
        feat=out-muh3/bench-results.json --min-baseline 1

    # pipelines against each other, one panel per run
    ./.venv/bin/python scripts/plot_bench.py --compare pipelines \
        may07=out-feat2/bench-results.json
"""

import argparse
import json
import os

import matplotlib

matplotlib.use("Agg")
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from scipy.stats import gmean  # noqa: E402

BAND_COLOR = "0.85"

# metric -> (how to print a baseline value, word for "less", word for "more")
UNITS = {
    "wall_time": ("{:.2f}s", "faster", "slower"),
    "cpu_time": ("{:.2f}s", "faster", "slower"),
    "peak_rss": ("{:.0f}MiB", "smaller", "larger"),
    "bytecode_size": ("{:,.0f}B", "smaller", "larger"),
    "creation_size": ("{:,.0f}B", "smaller", "larger"),
    "runtime_size": ("{:,.0f}B", "smaller", "larger"),
    "instructions": ("{:.3g}", "fewer", "more"),
}
GENERIC_UNIT = ("{:.3g}", "lower", "higher")


def load(spec):
    label, _, path = spec.partition("=")
    if not path:
        path, label = label, os.path.basename(os.path.dirname(label)) or label
    with open(path) as fh:
        return label, json.load(fh)


# Recorded in the result JSON but never plotted; see solc_bench.metrics.HIDDEN.
HIDDEN = {"cycles"}


def available_metrics(run):
    """Metric names that carry a stats block (median/values), not a bare count."""
    return sorted({
        metric
        for bench in run.get("results", {}).values()
        for pipeline in bench.values()
        for metric, block in pipeline.items()
        if isinstance(block, dict) and "median" in block and metric not in HIDDEN
    })


def samples(runs, metric):
    """Long form: one row per (benchmark, pipeline, run, iteration sample)."""
    rows = []
    for label, run in runs:
        for bench, pipelines in run.get("results", {}).items():
            for pipeline, blocks in pipelines.items():
                block = blocks.get(metric)
                if not isinstance(block, dict) or not block.get("median"):
                    continue
                for value in block.get("values") or [block["median"]]:
                    rows.append(dict(benchmark=bench, pipeline=pipeline,
                                     run=label, value=value))
    return pd.DataFrame(rows)


def against_baseline(df, series, facet, baseline, min_baseline):
    """Percentages vs `baseline`'s median, within each (benchmark, facet) cell.

    Cells the baseline does not cover, or that some series is missing from, are
    dropped -- a percentage against a missing reference means nothing.
    """
    ref = (df[df[series] == baseline].groupby(["benchmark", facet]).value.median()
           .rename("ref"))
    df = df.merge(ref, on=["benchmark", facet])
    df = df[df.ref >= min_baseline]
    complete = df.groupby(["benchmark", facet])[series].transform("nunique")
    df = df[complete == df[series].nunique()]
    return df.assign(pct=(df.value / df.ref - 1) * 100)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("runs", nargs="+", metavar="[label=]bench-results.json",
                    help="with --compare runs, the first one is the baseline")
    ap.add_argument("-c", "--compare", choices=("runs", "pipelines"), default="runs",
                    help="what to put side by side (default: runs)")
    ap.add_argument("-b", "--baseline",
                    help="with --compare pipelines, the reference pipeline "
                         "(default: the alphabetically first one)")
    ap.add_argument("--only", metavar="A,B",
                    help="restrict to these runs/pipelines (the baseline is "
                         "always kept); useful when one series is so far from "
                         "the rest that it flattens the others")
    ap.add_argument("-m", "--metric", default="wall_time",
                    help="metric to plot (default: wall_time); "
                         "an unknown name lists what the baseline file offers")
    ap.add_argument("-o", "--output", help="default: <metric>.png")
    ap.add_argument("--min-baseline", type=float, default=0.0,
                    dest="min_baseline",
                    help="drop benchmarks whose baseline value is below this, "
                         "in the metric's own units")
    args = ap.parse_args()

    runs = [load(s) for s in args.runs]
    labels = [l for l, _ in runs]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"run labels must be unique, got: {', '.join(labels)}")
    metric = args.metric
    output = args.output or f"{metric.replace('_', '-')}.png"

    offered = available_metrics(runs[0][1])
    if metric not in offered:
        raise SystemExit(f"unknown metric {metric!r}; {labels[0]} has: {', '.join(offered)}")
    unit_fmt, less, more = UNITS.get(metric, GENERIC_UNIT)

    raw = samples(runs, metric)
    if raw.empty:
        raise SystemExit(f"no benchmark carries {metric}")

    series, facet = ("run", "pipeline") if args.compare == "runs" else ("pipeline", "run")
    order_of = {"run": labels, "pipeline": sorted(raw.pipeline.unique())}
    baseline = args.baseline or order_of[series][0]
    if baseline not in order_of[series]:
        raise SystemExit(f"unknown baseline {baseline!r}; "
                         f"{series}s are: {', '.join(order_of[series])}")
    hue_order = [baseline] + [s for s in order_of[series] if s != baseline]
    if args.only:
        keep = {baseline, *args.only.split(",")}
        unknown = keep - set(hue_order)
        if unknown:
            raise SystemExit(f"--only: unknown {series}(s) {', '.join(sorted(unknown))}; "
                             f"have: {', '.join(hue_order)}")
        hue_order = [s for s in hue_order if s in keep]
        raw = raw[raw[series].isin(keep)]
    others = hue_order[1:]
    if not others:
        raise SystemExit(
            f"only one {series} to plot; with --compare {args.compare} you need "
            f"at least two (pass more files, or --compare "
            f"{'pipelines' if args.compare == 'runs' else 'runs'})"
        )

    df = against_baseline(raw, series, facet, baseline, args.min_baseline)
    if df.empty:
        raise SystemExit(
            f"--min-baseline {args.min_baseline:g} dropped everything"
            if args.min_baseline else
            f"no benchmark has {metric} for every {series} in a common {facet}"
        )

    # the baseline value belongs in the row label only when it is unambiguous:
    # with several panels the same benchmark has a different baseline in each
    if df[facet].nunique() == 1:
        df["benchmark"] += "  (" + df.ref.map(unit_fmt.format) + ")"

    medians = df.groupby([facet, "benchmark", series]).pct.median()
    # rows are shared across panels, so a benchmark in several panels needs one
    # position: sort by its typical effect in the last series listed
    order = (medians.xs(hue_order[-1], level=series)
             .groupby("benchmark").median().sort_values().index)
    colors = sns.color_palette("tab10", len(others))

    sns.set_theme(style="whitegrid", rc={"axes.edgecolor": "0.8"})
    height = max(0.24 * len(order) + 1.6, 3.2)
    g = sns.catplot(
        data=df[df[series] != baseline], kind="point", x="pct", y="benchmark",
        hue=series, col=facet, order=order, hue_order=others, palette=colors,
        estimator="median", errorbar=("pi", 100), linestyle="none",
        dodge=0.5 if len(others) > 1 else False,
        marker="o", markersize=4, err_kws={"linewidth": 1.2},
        height=height, aspect=7.5 / height, legend=False,
    )
    g.refline(x=0, color="0.3", linewidth=1)
    g.set_axis_labels(
        f"{metric.replace('_', ' ')} vs {baseline} (%)   ← {less} / {more} →", ""
    )
    for ax in g.axes.flat:
        ax.tick_params(axis="y", labelsize=8)

    spread = (df[df[series] == baseline].groupby([facet, "benchmark"]).pct
              .agg(["min", "max"]))

    handles = [Line2D([], [], marker="o", linestyle="none", color=c, label=l)
               for l, c in zip(others, colors)]
    if (spread["max"] > spread["min"]).any():
        handles.append(Patch(facecolor=BAND_COLOR, label=f"{baseline} spread"))
    g.figure.legend(handles=handles, loc="center left", bbox_to_anchor=(1.0, 0.5),
                    frameon=False)

    # keep the axis on the real signal: one outlier iteration on a small benchmark
    # must not squash everything else; whiskers past the edge are clipped. Panels
    # share the axis, so this spans them all -- no dot can fall off-panel.
    lo, hi = min(medians.min(), 0.0), max(medians.max(), 0.0)
    pad = max((hi - lo) * 0.35, 0.25)
    g.axes.flat[0].set_xlim(lo - pad, hi + pad)

    for ax, panel in zip(g.axes.flat, g.col_names):
        # the baseline's own iteration spread, as a band behind each row
        for i, bench in enumerate(order):
            if (panel, bench) in spread.index:
                blo, bhi = spread.loc[(panel, bench)]
                ax.barh(i, bhi - blo, left=blo, height=0.56, color=BAND_COLOR,
                        linewidth=0, zorder=0.5)

        med = medians.xs(panel, level=facet)
        summary = "   ".join(
            f"{s} {(gmean(med.xs(s, level=series) / 100 + 1) - 1) * 100:+.2f}%"
            for s in others
        )
        ax.set_title(f"[{panel}]  {metric}  geomean:  {summary}", fontsize=10)
        print(f"[{panel}] {metric}, "
              f"{len(med.index.get_level_values(0).unique())} benchmarks, "
              f"baseline {baseline}:  {summary}")

    g.figure.savefig(output, dpi=160, bbox_inches="tight")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
