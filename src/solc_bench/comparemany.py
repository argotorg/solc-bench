"""Many-file benchmark comparison with a Welch t-test significance check.

Unlike :mod:`solc_bench.compare`, which compares exactly two result files (or
two pipelines within one file), this module takes a baseline plus an arbitrary
number of other result files and reports, for each, the percentage delta vs the
baseline and a Welch t-test flagging whether the difference is likely real
given the few iterations per build.
"""

import math
import os
import statistics

from solc_bench.compare import load_results

# Metrics worth comparing by default. Others (instructions, *_size) can be
# requested explicitly with --metric or --all-metrics.
DEFAULT_METRICS = ["cpu_time", "wall_time", "cycles", "peak_rss"]

# |t| threshold above which we call a difference "significant". With n=3 per
# build the Welch df is tiny (~2-4), where the 95% two-sided critical value of
# t is roughly 3-4; 4.0 is a deliberately conservative cutoff.
T_SIGNIFICANT = 4.0


def fmt(x):
    """Compact human-readable number: 1.23G, 4.56M, or 7.4886 for small ones."""
    a = abs(x)
    if a >= 1e9:
        return f"{x/1e9:.4f}G"
    if a >= 1e6:
        return f"{x/1e6:.4f}M"
    if a >= 1e3:
        return f"{x/1e3:.4f}k"
    return f"{x:.4f}"


def welch_t(v1, v2):
    """Welch t-statistic for two small samples. Returns None if undefined."""
    if len(v1) < 2 or len(v2) < 2:
        return None
    s1, s2 = statistics.stdev(v1), statistics.stdev(v2)
    se = math.sqrt(s1**2 / len(v1) + s2**2 / len(v2))
    if se == 0:
        return math.inf if statistics.mean(v2) != statistics.mean(v1) else 0.0
    return (statistics.mean(v2) - statistics.mean(v1)) / se


def stat(node, key):
    """Pull a precomputed stat, or compute it from values if absent."""
    if key in node:
        return node[key]
    vals = node["values"]
    if key == "median":
        return statistics.median(vals)
    if key == "mean":
        return statistics.mean(vals)
    if key == "stddev":
        return statistics.stdev(vals) if len(vals) > 1 else 0.0
    raise KeyError(key)


def is_metric_node(node):
    return isinstance(node, dict) and "values" in node


def compare_many(files, metrics=None, all_metrics=False):
    """Compare a baseline result file against one or more others.

    ``files`` is a list of paths; the first is the baseline. ``metrics`` is the
    list of metric names to compare (defaults to :data:`DEFAULT_METRICS`); when
    ``all_metrics`` is true every metric present in the baseline is compared.
    The report is printed to stdout. Returns 0.
    """
    if len(files) < 2:
        raise ValueError("need at least 2 files (a baseline and one to compare)")

    metrics = metrics or DEFAULT_METRICS
    data = [load_results(p) for p in files]
    labels = [os.path.basename(p) for p in files]
    base_lbl = labels[0]

    bar = "=" * 78
    print(bar)
    print("BUILDS UNDER COMPARISON")
    print(bar)
    for i, (p, d) in enumerate(zip(files, data)):
        role = "BASELINE — everything is measured against this" if i == 0 \
            else "compared against the baseline (#0)"
        print(f"  #{i}  {os.path.basename(p)}")
        print(f"      role : {role}")
        print(f"      solc : {d['solc_version']}")
        print(f"      run  : {d['timestamp']}, {d['iterations']} iterations")
    print()

    results = [d["results"] for d in data]
    base = results[0]

    print(f"Baseline = #0 ({base_lbl}). delta% = (build - baseline) / baseline; "
          "all metrics are")
    print(f"lower-is-better, so negative delta% = improvement. t = Welch "
          f"t-statistic; verdict")
    print(f"is BETTER/WORSE when |t| > {T_SIGNIFICANT:g}, else ~noise (within "
          "run-to-run noise).")
    print()

    w_pm = 26   # project/mode column
    w_me = 13   # metric column
    w_num = 13  # each numeric cell

    # Header: two rows. First row groups columns under each build's #N tag.
    grp = [f"{'':<{w_pm}}{'':<{w_me}}"]
    grp.append(f"{'#0 baseline':^{w_num}}")
    for i in range(1, len(labels)):
        grp.append(f"{'<<< #' + str(i) + ' compared vs #0 >>>':^{w_num + 9 + 8 + 9}}")
    print("".join(grp))

    cols = [f"{'project/mode':<{w_pm}}{'metric':<{w_me}}"]
    cols.append(f"{'median':>{w_num}}")
    for _ in labels[1:]:
        cols.append(f"{'median':>{w_num}}{'delta%':>9}{'t':>8}{'verdict':>9}")
    hdr = "".join(cols)
    print(hdr)
    print("-" * len(hdr))

    for proj in base:
        for mode in base[proj]:
            base_node = base[proj][mode]
            keys = list(base_node.keys()) if all_metrics else metrics
            for m in keys:
                if m not in base_node or not is_metric_node(base_node[m]):
                    continue
                a = stat(base_node[m], "median")
                a_vals = base_node[m]["values"]

                row = [f"{proj+'/'+mode:<{w_pm}}{m:<{w_me}}"]
                row.append(f"{fmt(a):>{w_num}}")

                for i in range(1, len(results)):
                    r = results[i]
                    if (proj not in r or mode not in r[proj]
                            or m not in r[proj][mode]
                            or not is_metric_node(r[proj][mode][m])):
                        row.append(f"{'-':>{w_num}}{'-':>9}{'-':>8}{'-':>9}")
                        continue
                    node = r[proj][mode][m]
                    b = stat(node, "median")
                    dp = (b - a) / a * 100 if a else 0.0
                    t = welch_t(a_vals, node["values"])
                    if t is None:
                        ts, sig = "n/a", False
                    elif math.isinf(t):
                        ts, sig = "inf", True
                    else:
                        ts, sig = f"{t:.2f}", abs(t) > T_SIGNIFICANT
                    if not sig:
                        verdict = "~noise"
                    elif dp < 0:               # lower than baseline = better
                        verdict = "BETTER"
                    else:
                        verdict = "WORSE"
                    row.append(f"{fmt(b):>{w_num}}{dp:>8.2f}%{ts:>8}"
                               f"{verdict:>9}")
                print("".join(row))

    print()
    print("Caveat: with only a few iterations per build the t-test is")
    print("suggestive, not definitive — re-run with more iterations to confirm.")
    return 0
