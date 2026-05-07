"""Optional seaborn plots of comparison results: boxplot + per-sample scatter."""

_DEPS_HINT = (
    "plotting requires the 'plot' extra; install with: "
    "pip install 'solc-bench[plot]' (or: pip install seaborn)"
)

# Boxplot whiskers extend to the full range so every sample sits inside the
# whiskers; the strip overlay shows every individual measurement.
_WHIS = (0, 100)
_BOX_KW = dict(showfliers=False, whis=_WHIS, width=0.6)
_STRIP_KW = dict(
    palette="dark:k", dodge=True, size=3.5, alpha=0.7, jitter=0.15, legend=False
)


def _import_deps():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError as exc:
        raise RuntimeError(f"{_DEPS_HINT} ({exc})") from exc
    return sns, plt


def _samples(metrics_dict, metric):
    block = metrics_dict.get(metric) if metrics_dict else None
    if not block:
        return []
    values = block.get("values")
    if values:
        return list(values)
    median = block.get("median")
    return [median] if median is not None else []


def _rel_pct(samples, reference):
    """Convert samples to % change vs `reference` (the per-group baseline median)."""
    if reference is None or reference <= 0:
        return []
    return [(s - reference) / reference * 100.0 for s in samples]


def _rotate_xticks(ax, n_labels):
    if n_labels <= 1:
        return
    for label in ax.get_xticklabels():
        label.set_rotation(30)
        label.set_ha("right")


def _append(data, **kwargs):
    for key, value in kwargs.items():
        data[key].append(value)


def _draw_panel(sns, ax, d, hue, hue_order, palette, n_bench):
    sns.boxplot(
        data=d, x="benchmark", y="value", hue=hue,
        hue_order=hue_order, palette=palette, ax=ax, **_BOX_KW,
    )
    sns.stripplot(
        data=d, x="benchmark", y="value", hue=hue,
        hue_order=hue_order, ax=ax, **_STRIP_KW,
    )
    ax.axhline(0, color="0.4", linewidth=0.8, linestyle="--", zorder=0)
    ax.set_xlabel("")
    _rotate_xticks(ax, n_bench)


def plot_cross_version(baseline, target, metrics, output_path):
    sns, plt = _import_deps()

    base_label = "baseline"
    tgt_label = "target"

    # data[pipeline][metric] = {"benchmark":[], "version":[], "value":[]}
    data = {}
    for name, pipelines in baseline.get("results", {}).items():
        tgt_pipelines = target.get("results", {}).get(name, {})
        for pipeline, base_metrics in pipelines.items():
            tgt_metrics = tgt_pipelines.get(pipeline)
            if tgt_metrics is None:
                continue
            for metric in metrics:
                ref = base_metrics.get(metric, {}).get("median")
                base_rel = _rel_pct(_samples(base_metrics, metric), ref)
                tgt_rel = _rel_pct(_samples(tgt_metrics, metric), ref)
                if not base_rel and not tgt_rel:
                    continue
                d = data.setdefault(pipeline, {}).setdefault(
                    metric, {"benchmark": [], "version": [], "value": []}
                )
                for v in base_rel:
                    _append(d, benchmark=name, version=base_label, value=v)
                for v in tgt_rel:
                    _append(d, benchmark=name, version=tgt_label, value=v)

    metrics_present = [m for m in metrics if any(m in data[p] for p in data)]
    if not metrics_present:
        raise ValueError(f"no samples for metrics {metrics!r}")

    pipes = sorted(data)
    n_bench = max(
        len(set(d["benchmark"]))
        for pipe in pipes
        for d in data[pipe].values()
    )
    width_per = max(4.0, 1.6 * n_bench + 2.0)
    height_per = 3.6

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(
        len(metrics_present), len(pipes),
        figsize=(width_per * len(pipes), height_per * len(metrics_present)),
        sharey="row", squeeze=False,
    )
    hue_order = [base_label, tgt_label]
    palette = sns.color_palette("Set2", n_colors=2)

    for row, metric in enumerate(metrics_present):
        for col, pipe in enumerate(pipes):
            ax = axes[row][col]
            d = data[pipe].get(metric)
            if d is None:
                ax.set_visible(False)
                continue
            _draw_panel(sns, ax, d, "version", hue_order, palette, n_bench)
            ax.set_ylabel(f"{metric}: % change" if col == 0 else "")
            ax.set_title(f"pipeline: {pipe}" if row == 0 else "")
            legend = ax.get_legend()
            if legend is not None:
                if row == 0 and col == len(pipes) - 1:
                    legend.set_title("")
                else:
                    legend.remove()

    fig.suptitle("relative to baseline median", y=1.0)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return output_path, len(pipes), n_bench


def plot_cross_pipeline(results, ref_pipeline, target_pipeline, metrics, output_path):
    sns, plt = _import_deps()

    # data[metric] = {"benchmark":[], "pipeline":[], "value":[]}
    data = {}
    for name, pipelines in results.get("results", {}).items():
        if ref_pipeline not in pipelines or target_pipeline not in pipelines:
            continue
        for metric in metrics:
            ref = pipelines[ref_pipeline].get(metric, {}).get("median")
            ref_rel = _rel_pct(_samples(pipelines[ref_pipeline], metric), ref)
            tgt_rel = _rel_pct(_samples(pipelines[target_pipeline], metric), ref)
            if not ref_rel and not tgt_rel:
                continue
            d = data.setdefault(
                metric, {"benchmark": [], "pipeline": [], "value": []}
            )
            for v in ref_rel:
                _append(d, benchmark=name, pipeline=ref_pipeline, value=v)
            for v in tgt_rel:
                _append(d, benchmark=name, pipeline=target_pipeline, value=v)

    metrics_present = [m for m in metrics if m in data]
    if not metrics_present:
        raise ValueError(f"no samples for metrics {metrics!r}")

    n_bench = max(len(set(d["benchmark"])) for d in data.values())
    width = max(5.0, 1.6 * n_bench + 2.0)
    height_per = 3.6

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(
        len(metrics_present), 1,
        figsize=(width, height_per * len(metrics_present)),
        squeeze=False,
    )
    hue_order = [ref_pipeline, target_pipeline]
    palette = sns.color_palette("Set2", n_colors=2)

    for row, metric in enumerate(metrics_present):
        ax = axes[row][0]
        _draw_panel(sns, ax, data[metric], "pipeline", hue_order, palette, n_bench)
        ax.set_ylabel(f"{metric}: % change vs {ref_pipeline} median")
        legend = ax.get_legend()
        if legend is not None:
            if row == 0:
                legend.set_title("")
            else:
                legend.remove()

    suptitle = f"{target_pipeline} vs {ref_pipeline} (relative to {ref_pipeline} median)"
    solc_version = results.get("solc_version")
    if solc_version:
        suptitle += f" — {solc_version}"
    fig.suptitle(suptitle, y=1.0)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return output_path, n_bench
