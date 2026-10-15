"""Per-ticker chart benchmark: the native renderer (ui/native) against matplotlib, one chart at a time and from
several threads at once (as concurrent analyses draw them). Each chart is the 300-dpi PNG plus the 1100-px JPEG
preview the page shows (matplotlib's preview is shrunk from its PNG by ui/imaging.py, as the portal does).

    python ui/tests/chart_bench.py                     # native: 20 charts, then 48 on 8 threads
    python ui/tests/chart_bench.py --matplotlib        # and matplotlib, for comparison (pyplot: one at a time)
    python ui/tests/chart_bench.py --threads 16 --count 96
"""
import argparse
import os
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

from ui import fastchart, imaging  # noqa: E402
from ui.tests.fakes import CREDIT, EDGE, INCOME, MSFT_ROW, TECH  # noqa: E402


def _args(i):
    return (dict(MSFT_ROW, Ticker=f"T{i:03d}", PEG=1.2 + (i % 17) / 20, Dev_SD=-1.5 + (i % 7) / 2),
            INCOME, CREDIT, TECH, EDGE)


def _matplotlib_chart(viz, args):
    with fastchart.PYPLOT_LOCK:
        path = viz.plot_ticker_distribution(*args)
    imaging.jpeg_preview(path, imaging.plot_preview_dir(path), imaging.PLOT_PREVIEW_WIDTH)


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def bench(name, draw, runs, threads, count):
    draw(0)  # warm up: fonts and glyph masks, pyplot's caches
    lat = []
    for i in range(1, runs + 1):
        t = time.perf_counter()
        draw(i)
        lat.append((time.perf_counter() - t) * 1000)
    print(f"{name:10} one at a time: median {statistics.median(lat):7.1f} ms   p90 {_pct(lat, 90):7.1f} ms   "
          f"({1000 / statistics.median(lat):6.1f} charts/s)")
    todo = list(range(runs + 1, runs + 1 + count))
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                i = todo.pop()
            draw(i)

    t = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    wall = time.perf_counter() - t
    print(f"{name:10} {threads} threads:    {count} charts in {wall:6.2f} s   ({count / wall:6.1f} charts/s, "
          f"{60 * count / wall:7.0f}/min)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=20, help="charts drawn one at a time")
    ap.add_argument("--threads", type=int, default=8, help="threads drawing at once")
    ap.add_argument("--count", type=int, default=48, help="charts drawn by the threads")
    ap.add_argument("--matplotlib", action="store_true", help="also time matplotlib + Pillow")
    a = ap.parse_args()
    out = tempfile.mkdtemp(prefix="lynch-chart-bench-")
    print(f"charts in {out}; {os.cpu_count()} CPUs")
    if fastchart.library() is None:
        print(f"native renderer unavailable: {fastchart.unavailable_reason()}\n"
              f"build it with: cd ui/native && cargo build --release")
    else:
        viz = fastchart.FastVisualizer(os.path.join(out, "native"))
        timings = []

        def native(i):
            viz.plot_ticker_distribution(*_args(i))
            timings.append(viz.last_timings)

        bench("native", native, a.runs, a.threads, a.count)
        one = timings[1:a.runs + 1]
        med = {k: statistics.median(t[k] for t in one) for k in one[0] if k.endswith("_ms")}
        print("           one chart, median ms: " + ", ".join(f"{k[:-3]} {v:.1f}" for k, v in med.items())
              + f"  ({one[0]['threads']} threads, {one[0]['bands']} bands; PNG {one[0]['png_bytes'] // 1024} KB,"
                f" JPEG {one[0]['jpeg_bytes'] // 1024} KB)")
    if a.matplotlib:
        from graphics.visualizer import LynchPinVisualizer
        mviz = LynchPinVisualizer(output_dir=os.path.join(out, "matplotlib"))
        bench("matplotlib", lambda i: _matplotlib_chart(mviz, _args(i)), max(3, a.runs // 4), a.threads,
              max(a.threads, a.count // 4))


if __name__ == "__main__":
    main()
