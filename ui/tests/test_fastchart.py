"""The native chart renderer (ui/fastchart.py + ui/native): same text as graphics/visualizer.py, files where the
portal looks for them, close to matplotlib's pixels, matplotlib as the fallback, and no lock between threads.
Tests that draw natively are skipped when the library is not built (cd ui/native && cargo build --release)."""
import os
import threading

import numpy as np
import pytest
from PIL import Image

from ui import fastchart, imaging
from ui.tests.fakes import CREDIT, EDGE, INCOME, MSFT_ROW, TECH

native = pytest.mark.skipif(fastchart.library() is None, reason="native renderer not built (ui/native)")

CASES = {
    "full": (dict(MSFT_ROW), INCOME, CREDIT, TECH, EDGE),
    "bare": (dict(MSFT_ROW, Ticker="BARE*"), None, None, None, None),
    "dividend": (dict(MSFT_ROW, Ticker="DIV", DivYield="3.1%", PEG=0.35, Mean=1.9, Dev_SD=-5.2),
                 INCOME, None, dict(TECH, accumulation_zone=None), EDGE),
    "credit only": (dict(MSFT_ROW, Ticker="CRD", PEG=-0.4, Mean=0.6, Dev_SD=-1.25), None, CREDIT, TECH, None),
    "zero z": (dict(MSFT_ROW, Ticker="ZERO", PEG=1.5, Mean=1.5, Dev_SD=0), INCOME, CREDIT, None, None),
}


def _matplotlib_text(tmp_path, args):
    """What LynchPinVisualizer puts on the chart: title, badge, stats box, grades box."""
    import matplotlib.pyplot as plt
    from graphics.visualizer import LynchPinVisualizer
    seen = {}

    def capture(*a, **k):
        ax = plt.gcf().axes[0]
        texts = [t.get_text() for t in ax.texts]
        seen.update(title=ax._left_title.get_text(), badge=texts[0], left_box=texts[1],
                    right_box=texts[2] if len(texts) > 2 else "")
        plt.close()

    orig = plt.savefig
    plt.savefig = capture
    try:
        LynchPinVisualizer(output_dir=str(tmp_path)).plot_ticker_distribution(*args)
    finally:
        plt.savefig = orig
    return seen


@pytest.mark.parametrize("case", list(CASES))
def test_chart_text_is_the_visualizers(tmp_path, case):
    args = CASES[case]
    spec = fastchart.chart_text(*args)
    assert {k: spec[k] for k in ("title", "badge", "left_box", "right_box")} == _matplotlib_text(tmp_path, args)
    row = args[0]
    assert (spec["peg"], spec["mean"], spec["z"]) == (row["PEG"], row["Mean"], row["Dev_SD"])


def test_math_text_is_left_to_matplotlib():
    income = dict(INCOME, items=[("R$D", 0.1, "🟢"), ("Cash$", 0.2, "🟢")])
    with pytest.raises(fastchart.NativeChartUnsupported):
        fastchart.chart_text(dict(MSFT_ROW), income, None, None, None)


@native
def test_native_chart_and_its_preview(tmp_path, monkeypatch):
    viz = fastchart.FastVisualizer(str(tmp_path))
    path = viz.plot_ticker_distribution(*CASES["full"])
    assert path == str(tmp_path / "MSFT_valuation.png") and viz.last_timings is not None
    with Image.open(path) as im:
        assert im.size == (3600, 2100) and im.mode == "RGB"
    # the preview is already where jpeg_preview looks: the server never shrinks the PNG
    import PIL.Image
    monkeypatch.setattr(PIL.Image, "open", lambda *a, **k: pytest.fail("preview was regenerated"))
    preview = imaging.jpeg_preview(path, imaging.plot_preview_dir(path), imaging.PLOT_PREVIEW_WIDTH)
    monkeypatch.undo()
    with Image.open(preview) as im:
        assert im.size == (1100, 642) and im.format == "JPEG"
    assert os.listdir(imaging.plot_preview_dir(path)) == [os.path.basename(preview)]  # no stray temp files


@native
@pytest.mark.parametrize("case", ["full", "dividend", "zero z"])
def test_native_chart_matches_matplotlib(tmp_path, case):
    from graphics.visualizer import LynchPinVisualizer
    args = CASES[case]
    ours = fastchart.FastVisualizer(str(tmp_path / "native")).plot_ticker_distribution(*args)
    theirs = LynchPinVisualizer(output_dir=str(tmp_path / "mpl")).plot_ticker_distribution(*args)
    a = np.asarray(Image.open(ours).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(theirs).convert("RGB"), dtype=np.int16)
    diff = np.abs(a - b).max(axis=2)
    # same layout: only anti-aliasing at the edges of glyphs and lines differs
    assert diff.mean() < 2.5, diff.mean()
    assert (diff > 96).mean() < 0.001


def test_failures_fall_back_to_matplotlib(tmp_path, monkeypatch):
    def broken(spec):
        raise fastchart.NativeChartError("boom")

    monkeypatch.setattr(fastchart, "render", broken)
    viz = fastchart.FastVisualizer(str(tmp_path))
    path = viz.plot_ticker_distribution(*CASES["full"])
    assert viz.last_timings is None
    with Image.open(path) as im:
        assert im.size == (3600, 2100)
    assert not [f for f in os.listdir(tmp_path / "previews") if f.startswith(".")]


def test_disabled_library_means_matplotlib(monkeypatch):
    from graphics.visualizer import LynchPinVisualizer
    monkeypatch.setattr(fastchart, "_lib", None)
    monkeypatch.setattr(fastchart, "_lib_error", None)
    monkeypatch.setenv("LYNCH_UI_NATIVE_CHART", "0")
    assert fastchart.library() is None and "LYNCH_UI_NATIVE_CHART" in fastchart.unavailable_reason()
    assert fastchart.visualizer_class() is LynchPinVisualizer


def test_missing_library_means_matplotlib(tmp_path, monkeypatch):
    monkeypatch.setattr(fastchart, "_lib", None)
    monkeypatch.setattr(fastchart, "_lib_error", None)
    monkeypatch.setenv("LYNCH_UI_CHART_LIB", str(tmp_path / "nope.so"))
    assert fastchart.library() is None and "nope.so" in fastchart.unavailable_reason()


@native
def test_threads_draw_at_once(tmp_path):
    """No pyplot, no lock: tickers drawn from several threads at once all come out whole."""
    viz = fastchart.FastVisualizer(str(tmp_path), threads=2)
    syms = [f"T{i}" for i in range(8)]
    errors = []

    def draw(sym):
        try:
            row = dict(MSFT_ROW, Ticker=sym, PEG=1.0 + int(sym[1:]) / 10)
            viz.plot_ticker_distribution(row, INCOME, CREDIT, TECH, EDGE)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=draw, args=(s,)) for s in syms]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for s in syms:
        with Image.open(tmp_path / f"{s}_valuation.png") as im:
            im.load()
            assert im.size == (3600, 2100)
    assert len(os.listdir(tmp_path / "previews")) == len(syms)


@native
def test_analysis_uses_the_native_chart_without_the_plot_lock(tmp_path, monkeypatch):
    from ui import analysis
    from ui.config import Settings
    from ui.tests import fakes

    class Held:
        """The pyplot lock, held for the whole analysis: a native chart must not wait for it."""
        def __enter__(self):
            raise AssertionError("the native chart took the pyplot lock")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(analysis, "_PLOT_LOCK", Held())
    settings = Settings(cache_dir=str(tmp_path), enrich="off")
    a = analysis.TickerAnalyzer(settings, backends=fakes.backends(visualizer=fastchart.FastVisualizer))
    data = a.run("MSFT")
    assert data["stages"]["plot"] == "done" and data["plot_preview_url"].startswith("/plots/MSFT.jpg")
    assert os.path.exists(data["plot_file"])
