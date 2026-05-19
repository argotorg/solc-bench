"""Baseline-vs-target scatter plots of benchmark metrics via gnuplot.

A classic comparison scatter: one dot per benchmark (the median across its
iterations), x = BASELINE value, y = TARGET value, with the y=x diagonal drawn
in. Dots on the diagonal are unchanged; below it the target is faster/smaller,
above it slower/larger. One such panel is drawn per metric (cpu_time, cycles,
peak_rss), the panels rendered side by side.

The plots are always saved to disk -- one PNG per metric -- so they can be
reused. When stdout is an interactive terminal that speaks an inline image
protocol -- the kitty graphics protocol, or sixel -- they are *also* drawn
straight into the terminal, side by side in a single image.

It silently does nothing when gnuplot is missing or there is no data, so
callers can invoke it unconditionally.
"""

import os
import shutil
import subprocess
import sys

# Pixel size of one square scatter panel.
_PANEL = 384

# Metrics plotted side by side, in display order.
_PLOT_METRICS = ("cpu_time", "cycles", "peak_rss")

# gnuplot terminal driver per detected protocol, as a `{w},{h}`-format string.
# Both cairo/gd drivers write the encoded image straight to stdout (no
# `set output` needed). `noenhanced` keeps gnuplot from reading `_` in labels
# (e.g. "cpu_time") as a subscript.
_TERMINALS = {
    "kitty": 'kittycairo size {w},{h} background "white" noenhanced',
    "sixel": 'sixelgd size {w},{h} truecolor background "white" noenhanced',
}

# CSI cursor-home / clear-screen escapes that gnuplot's kitty and sixel
# terminals wrap around the image: they assume the plot owns the whole
# terminal, which would draw it on top of our table (and stack every run at
# the top-left corner). Stripped, the image flows inline below the table.
# These byte sequences cannot occur inside kitty base64 or sixel payloads, so
# removing every occurrence is safe.
_SCREEN_CONTROL = (b"\033[H", b"\033[J", b"\033[0J", b"\033[1J", b"\033[2J")


def _detect_graphics():
    """Return 'kitty', 'sixel', or None by querying the controlling terminal.

    Sends a kitty-graphics capability probe followed by a Primary Device
    Attributes (DA1) request. The DA1 reply always arrives, so it bounds the
    wait; if the kitty probe is understood its reply lands first.
    """
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return None
    try:
        import select
        import termios
        import tty
    except ImportError:
        return None  # not a POSIX terminal

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None

    buf = ""
    try:
        tty.setraw(fd)
        # kitty graphics query (image id 31, a 1x1 RGB pixel) + DA1 request.
        sys.stdout.write("\033_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\033\\\033[c")
        sys.stdout.flush()
        while "c" not in buf:  # the DA1 reply ends with 'c'
            ready, _, _ = select.select([fd], [], [], 0.4)
            if not ready:
                break
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            buf += chunk.decode("latin-1", "replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    if "_Gi=31;OK" in buf:
        return "kitty"
    # DA1 reply: ESC [ ? <attrs separated by ;> c -- attribute 4 means sixel.
    da_attrs = buf.partition("\033[?")[2].partition("c")[0]
    if "4" in da_attrs.split(";"):
        return "sixel"
    return None


def _quote(text):
    """Make a string safe to drop inside a double-quoted gnuplot literal."""
    return str(text).replace("\\", "").replace('"', "'").replace("\n", " ")


def _panel(panel):
    """gnuplot commands for one square scatter: white box, y=x diagonal, dots.

    `panel` is a (points, hi, title, xlabel, ylabel) tuple.
    """
    points, hi, title, xlabel, ylabel = panel
    data = "\n".join(f"{x} {y}" for x, y in points)
    return f"""\
set size square
set border 31 lw 1 lc rgb "black"
set grid lc rgb "#cccccc"
set title "{_quote(title)}"
set xlabel "{_quote(xlabel)}"
set ylabel "{_quote(ylabel)}"
set xrange [0:{hi}]
set yrange [0:{hi}]
plot x with lines lc rgb "black" lw 1, \\
     "-" with points pt 7 ps 0.7 lc rgb "blue"
{data}
e
"""


def _gnuplot_script(panels, terminal, output=None):
    """Build a gnuplot script laying `panels` out in a single row.

    `panels` is a list of (points, hi, title, xlabel, ylabel) tuples. A single
    panel renders as a plain plot; several use `multiplot` so the scatters sit
    side by side in one image.
    """
    output_line = f'set output "{_quote(output)}"\n' if output else ""
    body = "\n".join(_panel(p) for p in panels)
    if len(panels) > 1:
        body = f"set multiplot layout 1,{len(panels)}\n{body}\nunset multiplot"
    return f"""\
set terminal {terminal}
{output_line}set encoding utf8
unset key
{body}
"""


def _run_gnuplot(script):
    """Run gnuplot with `script` on stdin; return its stdout bytes, or None on failure.

    Output is handled as raw bytes: gnuplot emits binary image data, and
    decoding it as text would corrupt or crash.
    """
    try:
        proc = subprocess.run(
            ["gnuplot"], input=script.encode(), capture_output=True
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _collect_points(baseline, target, metric):
    """Pair the baseline and target `metric` medians, one point per benchmark.

    Each (benchmark, pipeline) yields a single point: x = baseline median,
    y = target median across that run's iterations.
    """
    points = []
    tgt_results = target.get("results", {})
    for name, pipelines in baseline.get("results", {}).items():
        for pipeline, base_metrics in pipelines.items():
            tgt_metrics = tgt_results.get(name, {}).get(pipeline)
            if tgt_metrics is None:
                continue
            x = (base_metrics.get(metric) or {}).get("median")
            y = (tgt_metrics.get(metric) or {}).get("median")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                points.append((x, y))
    return points


def show_comparison(baseline, target, metrics=_PLOT_METRICS, png_prefix="compare_"):
    """Save a baseline-vs-target scatter per metric, and draw them inline.

    `baseline` and `target` are the raw result dicts loaded from two
    bench-results.json files. Each benchmark contributes one point per metric:
    its baseline median against its target median. One PNG is written
    per metric as `<png_prefix><metric>.png` whenever gnuplot and data are
    available; the panels are also drawn inline, side by side in a single
    image, when the terminal supports a graphics protocol. Returns True when at
    least one PNG was written, False otherwise -- callers can ignore the result.
    """
    if shutil.which("gnuplot") is None:
        return False

    base_v = baseline.get("solc_version", "baseline")
    target_v = target.get("solc_version", "target")

    # One panel per metric that actually has data.
    panels = []
    for metric in metrics:
        points = _collect_points(baseline, target, metric)
        if not points:
            continue
        hi = max(max(x, y) for x, y in points) * 1.08
        if hi <= 0:
            continue
        # Axis labels stay short ("BASELINE"/"TARGET") -- a small panel has no
        # room for the full solc version strings; those are printed below.
        panels.append((points, hi, metric, "BASELINE", "TARGET"))
    if not panels:
        return False

    # Save one standalone PNG per metric so the plots can be reused outside the terminal.
    written = []
    png_term = f'pngcairo size {_PANEL},{_PANEL} background "white" noenhanced'
    for panel in panels:
        png_path = f"{png_prefix}{panel[2]}.png"
        if _run_gnuplot(_gnuplot_script([panel], png_term, output=png_path)) is not None:
            written.append(png_path)

    # Draw every panel inline, side by side, when the terminal speaks an image protocol.
    protocol = _detect_graphics()
    if protocol is not None:
        term = _TERMINALS[protocol].format(w=_PANEL * len(panels), h=_PANEL)
        image = _run_gnuplot(_gnuplot_script(panels, term))
        if image:
            for ctl in _SCREEN_CONTROL:
                image = image.replace(ctl, b"")
            print()
            sys.stdout.flush()
            sys.stdout.buffer.write(image)
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()

    if written:
        print(f"BASELINE = {base_v}   TARGET = {target_v}")
        print(f"Comparison plots written to {', '.join(written)}")
    return bool(written)
