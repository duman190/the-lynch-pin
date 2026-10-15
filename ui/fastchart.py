"""The per-ticker PEG chart drawn by the native renderer in ui/native (Rust: tiny-skia + ttf-parser), a drop-in for
``graphics.visualizer.LynchPinVisualizer.plot_ticker_distribution``.

matplotlib takes about a second for the 300-dpi chart and Pillow another quarter of a second for its JPEG preview,
and pyplot is process-global, so threads drew charts one at a time. The native renderer lays the chart out the way
matplotlib does (same DejaVu fonts, same text boxes, ticks and tight layout), draws it in bands on several threads,
compresses the PNG in parallel and draws the preview at its own size at the same time — in tens of milliseconds.
ctypes releases the GIL for the call, so concurrent analyses render in parallel without a lock.

Build it once with ``cargo build --release`` in ui/native (or ``python -m ui.fastchart --build``). Without the
library — or with ``LYNCH_UI_NATIVE_CHART=0`` — the portal keeps using matplotlib, and an input the renderer does
not draw exactly like matplotlib (a degenerate distribution, math text) falls back to matplotlib per chart.

The text of the chart is formatted here with the visualizer's own f-strings (ui/tests/test_fastchart.py checks
they stay identical); the library only lays it out and draws it.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import uuid

from ui import imaging

NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
ABI = 1
PYPLOT_LOCK = threading.Lock()  # pyplot and the visualizer's rcParams are process-global

_lib = None
_lib_error = None
_lib_guard = threading.Lock()
_fonts = None


class NativeChartError(RuntimeError):
    pass


class NativeChartUnsupported(NativeChartError):
    """An input the native renderer leaves to matplotlib."""


def library_path():
    env = os.environ.get("LYNCH_UI_CHART_LIB")
    if env:
        return env
    name = {"darwin": "liblynch_chart.dylib", "win32": "lynch_chart.dll"}.get(sys.platform, "liblynch_chart.so")
    return os.path.join(NATIVE_DIR, "target", "release", name)


def library():
    """The loaded renderer, or None (not built, wrong version, or disabled with LYNCH_UI_NATIVE_CHART=0)."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    with _lib_guard:
        if _lib is not None or _lib_error is not None:
            return _lib
        if os.environ.get("LYNCH_UI_NATIVE_CHART", "1").strip().lower() in ("0", "false", "no", "off"):
            _lib_error = "disabled by LYNCH_UI_NATIVE_CHART"
            return None
        path = library_path()
        try:
            lib = ctypes.CDLL(path)
            lib.lynch_chart_abi.restype = ctypes.c_uint32
            if lib.lynch_chart_abi() != ABI:
                raise OSError(f"ABI {lib.lynch_chart_abi()} != {ABI} (rebuild ui/native)")
            lib.lynch_chart_render.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
            lib.lynch_chart_render.restype = ctypes.c_int32
        except (OSError, AttributeError) as e:
            _lib_error = f"{path}: {e}"
            return None
        _lib = lib
        return lib


def unavailable_reason():
    library()
    return _lib_error


def font_paths():
    """The TrueType files matplotlib picks for the visualizer's text (DejaVu, bundled with matplotlib)."""
    global _fonts
    if _fonts is None:
        import matplotlib
        from matplotlib.font_manager import FontProperties, findfont
        ttf = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
        wanted = {"sans": ("sans-serif", "normal", "DejaVuSans.ttf"),
                  "sans_bold": ("sans-serif", "bold", "DejaVuSans-Bold.ttf"),
                  "mono_bold": ("monospace", "bold", "DejaVuSansMono-Bold.ttf")}
        fonts = {}
        for key, (family, weight, bundled) in wanted.items():
            try:
                fonts[key] = findfont(FontProperties(family=family, weight=weight), fallback_to_default=True)
            except Exception:
                fonts[key] = os.path.join(ttf, bundled)
        _fonts = fonts
    return _fonts


def render(spec):
    """Renders ``spec`` (see ui/native/src/spec.rs); returns the timings (ms). Raises NativeChartUnsupported for
    an input left to matplotlib, NativeChartError for anything else."""
    lib = library()
    if lib is None:
        raise NativeChartError(f"native renderer unavailable: {_lib_error}")
    data = json.dumps(spec).encode()
    out = ctypes.create_string_buffer(4096)
    rc = lib.lynch_chart_render(data, len(data), out, len(out))
    msg = out.value.decode("utf-8", "replace")
    if rc == 0:
        return json.loads(msg)
    if rc == 2:
        raise NativeChartUnsupported(msg)
    raise NativeChartError(msg or f"renderer failed ({rc})")


# ── the chart's text: graphics/visualizer.py's f-strings, verbatim ─────────────
def _div_pct(row):
    """The dividend yield (%) the engine included in the row's ROIs, or 0 (non-payers, older rows)."""
    try:
        return float(str(row.get('DivYield', '0')).rstrip('%'))
    except (TypeError, ValueError):
        return 0.0


def chart_text(row, grade_result=None, bs_result=None, tech_result=None, edge_result=None):
    """The ``chart`` part of a render request: the strings plot_ticker_distribution draws, and its numbers."""
    ticker = row['Ticker'].replace('*', '')
    current_peg, mean_peg, z_score = row['PEG'], row['Mean'], row['Dev_SD']

    stats_text = (
        f"Ticker:     {ticker:>8}\n"
        f"--------------------\n"
        f"- PE:       {row.get('PE', 0):>8.1f}\n"
        f"- Fwd PE:   {row.get('FwdPE', 0):>8.1f}\n"
        f"- 2YFwd PE: {row.get('2YFwd', 0):>8.1f}\n"
        f"- PEG:      {current_peg:>8.2f}\n"
        f"- 5Y Growth:{row.get('5YGrowth', '0%'):>8}\n"
        + (f"- Div Yield:{row['DivYield']:>8}\n" if _div_pct(row) else "") +  # included in the ROIs
        f"- Bull ROI: {row.get('Bull', '0%'):>8}\n"
        f"- Base ROI: {row.get('Base', '0%'):>8}\n"
        f"- Bear ROI: {row.get('Bear', '0%'):>8}"
    )
    if tech_result:
        stats_text += (
            f"\n\n"
            f"Technicals: {tech_result['signal']:>8}\n"
            f"--------------------\n"
            f"- RSI:{int(tech_result['rsi'])}|SMA200:{tech_result['price_vs_sma200']:>+3.0f}%"
        )
        zone = tech_result.get('accumulation_zone')
        if zone:
            lo, hi = int(zone[0]), int(zone[1])
            accum_val = "$" + str(lo) + "-$" + str(hi)
            stats_text += f"\n- ACCUM: {accum_val:>11}"
    if edge_result:
        edge = f"{edge_result['bull_acc']:.0f}/{edge_result['bear_acc']:.0f}"
        stats_text += f"\n- Bull/Bear %:{edge:>6}"

    right_lines = []
    if grade_result:
        sig_map = {'\U0001f7e2': '✓', '\U0001f535': '~', '\U0001f534': '✗', '⚪': ' '}
        right_lines.append(f"Income Grade:{grade_result['grade']:>8}")
        right_lines.append("---------------------")
        for label, growth, sig in grade_result['items']:
            if growth is not None:
                marker = sig_map.get(sig, ' ')
                right_lines.append(f"{marker} {label:<12} {growth*100:>+5.0f}%")

    if bs_result:
        if right_lines:
            right_lines.append("")
        right_lines.append(f"Credit Rating:{bs_result['rating']:>7}")
        right_lines.append("---------------------")
        for label, val in bs_result['metrics']:
            if val is not None:
                fmt = f"{val:.1f}" if abs(val) < 100 else f"{val:.0f}"
                right_lines.append(f"  {label:<10} {fmt:>8}")
            else:
                right_lines.append(f"  {label:<10} {'N/A':>8}")

    chart = {"title": f'{ticker} VALUATION DEVIATION (PEG)', "badge": f' {z_score} SD ',
             "left_box": stats_text, "right_box": "\n".join(right_lines),
             "peg": float(current_peg), "mean": float(mean_peg), "z": float(z_score)}
    # matplotlib parses these three as math text when they hold '$' (the stats box is drawn with parse_math=False)
    if any("$" in chart[k] for k in ("title", "badge", "right_box")):
        raise NativeChartUnsupported("math text")
    return chart


class FastVisualizer:
    """``plot_ticker_distribution`` of LynchPinVisualizer, drawn natively. Writes ``<TICKER>_valuation.png`` (300 dpi,
    like the original) and its inline preview where ui/imaging.py's ``jpeg_preview`` looks for it."""

    thread_safe = True  # no pyplot: callers need no lock

    def __init__(self, output_dir="tmp", threads=0):
        self.output_dir = output_dir
        self.threads = threads
        self.last_timings = None
        self._mpl = None
        os.makedirs(output_dir, exist_ok=True)

    def _matplotlib(self):
        if self._mpl is None:
            import matplotlib
            matplotlib.use("Agg")
            from graphics.visualizer import LynchPinVisualizer
            self._mpl = LynchPinVisualizer(output_dir=self.output_dir)
        return self._mpl

    def plot_ticker_distribution(self, row, grade_result=None, bs_result=None, tech_result=None, edge_result=None):
        args = (row, grade_result, bs_result, tech_result, edge_result)
        ticker = row['Ticker'].replace('*', '')
        path = os.path.join(self.output_dir, f"{ticker}_valuation.png")
        preview_dir = imaging.plot_preview_dir(path)
        rendered = os.path.join(preview_dir, f".{ticker}-{uuid.uuid4().hex[:12]}.jpg")
        try:
            spec = {"png": path, "jpeg": rendered, "jpeg_width": imaging.PLOT_PREVIEW_WIDTH, "jpeg_quality": 85,
                    "threads": self.threads, "fonts": font_paths(), "chart": chart_text(*args)}
            os.makedirs(preview_dir, exist_ok=True)
            self.last_timings = render(spec)
        except Exception as e:
            try:
                os.remove(rendered)
            except OSError:
                pass
            if not isinstance(e, NativeChartUnsupported):
                print(f"⚠️  {ticker} native chart failed, drawing it with matplotlib: {type(e).__name__}: {e}")
            with PYPLOT_LOCK:
                return self._matplotlib().plot_ticker_distribution(*args)
        try:
            imaging.adopt_preview(path, preview_dir, imaging.PLOT_PREVIEW_WIDTH, rendered)
        except OSError as e:  # the chart is fine; jpeg_preview will shrink the PNG on request instead
            print(f"⚠️  {ticker} chart preview not kept: {e}")
            try:
                os.remove(rendered)
            except OSError:
                pass
        return path


def visualizer_class():
    """FastVisualizer when the native renderer is built, else matplotlib's LynchPinVisualizer."""
    if library() is not None:
        return FastVisualizer
    from graphics.visualizer import LynchPinVisualizer
    return LynchPinVisualizer


def build(release=True):
    """``cargo build`` of ui/native (needs a Rust toolchain: https://rustup.rs)."""
    cmd = ["cargo", "build"] + (["--release"] if release else [])
    return subprocess.call(cmd, cwd=NATIVE_DIR)


if __name__ == "__main__":
    if "--build" in sys.argv[1:]:
        sys.exit(build())
    print(library_path(), "loaded" if library() else f"unavailable: {unavailable_reason()}")
