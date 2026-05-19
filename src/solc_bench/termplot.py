"""Baseline-vs-target scatter plot of benchmark timings via gnuplot.

A classic comparison scatter: one dot per benchmark, x = BASELINE time,
y = TARGET time, with the y=x diagonal drawn in. Dots on the diagonal are
unchanged; below it the target is faster, above it slower.

The plot is always saved to disk as a PNG so it can be reused. When stdout is
an interactive terminal that speaks an inline image protocol -- the kitty
graphics protocol, or sixel -- it is *also* drawn straight into the terminal.

It silently does nothing when gnuplot is missing or there is no data, so
callers can invoke it unconditionally.
"""

import os
import shutil
import subprocess
import sys

# gnuplot terminal driver per detected protocol. Both cairo/gd drivers write
# the encoded image straight to stdout (no `set output` needed).
# `noenhanced` keeps gnuplot from reading `_` in labels (e.g. "cpu_time") as a
# subscript.
_TERMINALS = {
    "kitty": 'kittycairo size 640,640 background "white" noenhanced',
    "sixel": 'sixelgd size 640,640 truecolor background "white" noenhanced',
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


def _gnuplot_script(points, hi, terminal, title, xlabel, ylabel, output=None):
    """Build a gnuplot script: a square white box, a y=x diagonal, blue dots."""
    data = "\n".join(f"{x} {y}" for x, y in points)
    output_line = f'set output "{_quote(output)}"\n' if output else ""
    return f"""\
set terminal {terminal}
{output_line}set encoding utf8
unset key
set size square
set border 31 lw 1 lc rgb "black"
set grid lc rgb "#cccccc"
set title "{_quote(title)}"
set xlabel "{_quote(xlabel)}"
set ylabel "{_quote(ylabel)}"
set xrange [0:{hi}]
set yrange [0:{hi}]
plot x with lines lc rgb "black" lw 1, \\
     "-" with points pt 7 ps 1.1 lc rgb "blue"
{data}
e
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


def _collect_points(result, metric):
    """Pull (baseline_median, target_median) pairs for `metric`, one per row."""
    points = []
    for pipelines in result.get("benchmarks", {}).values():
        for comparison in pipelines.values():
            c = comparison.get(metric)
            if not c:
                continue
            x, y = c.get("baseline_median"), c.get("target_median")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                points.append((x, y))
    return points


def show_comparison(result, metric="cpu_time", png_path="solc-bench-compare.png"):
    """Save a baseline-vs-target scatter of `metric` to `png_path`, and draw it inline.

    `result` is the dict from compare.compare_compiler_versions. The PNG is
    written whenever gnuplot and data are available; the inline image is drawn
    additionally when the terminal supports a graphics protocol. Returns True
    when a PNG was written, False otherwise -- callers can ignore the result.
    """
    if shutil.which("gnuplot") is None:
        return False

    points = _collect_points(result, metric)
    if not points:
        return False
    hi = max(max(x, y) for x, y in points) * 1.08
    if hi <= 0:
        return False

    base = result.get("baseline", {}).get("solc_version", "baseline")
    target = result.get("target", {}).get("solc_version", "target")
    labels = dict(
        title=f"{metric}: each dot is one benchmark (below the line = TARGET faster)",
        xlabel=f"BASELINE  {base}",
        ylabel=f"TARGET  {target}",
    )

    # Always save a PNG copy so the plot can be reused outside the terminal.
    png_ok = _run_gnuplot(
        _gnuplot_script(
            points, hi, 'pngcairo size 640,640 background "white" noenhanced',
            output=png_path, **labels,
        )
    ) is not None

    # Draw it inline when the terminal speaks an image protocol.
    protocol = _detect_graphics()
    if protocol is not None:
        image = _run_gnuplot(_gnuplot_script(points, hi, _TERMINALS[protocol], **labels))
        if image:
            for ctl in _SCREEN_CONTROL:
                image = image.replace(ctl, b"")
            print()
            sys.stdout.flush()
            sys.stdout.buffer.write(image)
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()

    if png_ok:
        print(f"Comparison plot written to {png_path}")
    return png_ok
