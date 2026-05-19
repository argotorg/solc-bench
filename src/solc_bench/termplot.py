"""Baseline-vs-target scatter plots of benchmark metrics via gnuplot.

One dot per benchmark: x = baseline median, y = target median, with a y=x
diagonal -- dots below the line mean the target improved. One panel per
metric (cpu_time, cycles, peak_rss), drawn side by side.

Panels are saved as one PNG per metric, and also drawn inline when the
terminal speaks the kitty or sixel image protocol. Does nothing (no error)
when gnuplot is missing or there is no data, so callers can invoke it freely.
"""

import os
import shutil
import subprocess
import sys

# Pixel size of one square scatter panel.
_PANEL = 384

# Metrics plotted side by side, in display order.
_PLOT_METRICS = ("cpu_time", "cycles", "peak_rss")

# gnuplot driver per protocol ({w},{h}-format). Both write the image to
# stdout; `noenhanced` stops `_` in labels being read as a subscript.
_TERMINALS = {
    "kitty": 'kittycairo size {w},{h} background "white" noenhanced',
    "sixel": 'sixelgd size {w},{h} truecolor background "white" noenhanced',
}

# gnuplot's kitty/sixel terminals wrap the image in cursor-home/clear-screen
# escapes that would draw it over our table. Strip them so the image flows
# inline below; these bytes never occur inside the base64/sixel payload.
_SCREEN_CONTROL = (b"\033[H", b"\033[J", b"\033[0J", b"\033[1J", b"\033[2J")


def _detect_graphics():
    """Return 'kitty', 'sixel', or None by querying the controlling terminal.

    Sends a kitty-graphics probe then a DA1 request; the DA1 reply always
    arrives, so it bounds the wait.
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
        # kitty graphics probe (image id 31) + DA1 request.
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
    # DA1 reply is `ESC [ ? <attrs;...> c`; attribute 4 means sixel.
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
    """Build a gnuplot script laying `panels` (panel tuples) out in one row.

    A single panel is a plain plot; several use `multiplot` to sit side by side.
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
    """Run gnuplot with `script` on stdin; return stdout bytes, None on failure.

    Bytes, not text -- the output is a binary image.
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
    """One (baseline median, target median) point per (benchmark, pipeline)."""
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
    """Save a baseline-vs-target scatter PNG per metric, and draw them inline.

    `baseline`/`target` are the dicts loaded from two bench-results.json files.
    Writes `<png_prefix><metric>.png` per metric when gnuplot and data exist,
    and also draws the panels inline on a graphics-capable terminal. Returns
    True if any PNG was written.
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
        # Short axis labels -- a small panel has no room for version strings.
        panels.append((points, hi, metric, "BASELINE", "TARGET"))
    if not panels:
        return False

    # A standalone PNG per metric, for reuse outside the terminal.
    written = []
    png_term = f'pngcairo size {_PANEL},{_PANEL} background "white" noenhanced'
    for panel in panels:
        png_path = f"{png_prefix}{panel[2]}.png"
        if _run_gnuplot(_gnuplot_script([panel], png_term, output=png_path)) is not None:
            written.append(png_path)

    # Draw all panels inline when the terminal speaks an image protocol.
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
