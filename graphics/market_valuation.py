"""US stock market valuation charts for the portal's home page, in the Lynch Pin style (graphics/visualizer.py):
dark #121212 canvas, sky-blue title, a glowing white line over a sky-blue fill, red markers with a white edge.

    plot_shiller_pe(series, path)   # rear view mirror: the S&P 500 Shiller PE (CAPE), monthly since 1871
    plot_forward_peg(history, path) # forward looking: the S&P 500 forward PEG, monthly since 1995 (Yardeni Research,
                                    #   traced), then the Lynch Pin's weekly point

The portal draws them in a background thread, so this uses matplotlib's object API (a Figure per chart, every color
set on the artist) instead of pyplot, whose global state is not thread-safe.
"""
import datetime as _dt

import numpy as np

BG = '#121212'
BOX = '#1A1A1A'
EDGE = '#333333'
SKY = '#5D9CEC'
WHITE = '#FFFFFF'
TEXT = '#E0E0E0'
MUTED = '#B0B0B0'
DIM = '#8E8E8E'
RED = '#FF4B2B'
GREEN = '#2ECC71'

FIGSIZE = (7.2, 4.5)  # shown half-width on a laptop: a smaller canvas keeps the type readable there
DPI = 300

# The CAPE at the peak before each crash (the highest month in the window), as on multpl.com's chart, and where its
# label goes: years left of the dot, and how far above it (share of the y range). Black Monday's is lifted over the
# 1960s-90s line, with a leader down to the dot.
EVENTS = (("BLACK TUESDAY", "1929-06-01", "1929-10-31", 0, 0.035),
          ("DOT-COM PEAK", "1999-06-01", "2000-12-31", 0, 0.035),
          ("BLACK MONDAY", "1987-06-01", "1987-10-31", 5, 0.19))


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _figure():
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=FIGSIZE, facecolor=BG)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_facecolor(BG)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(EDGE)
    ax.tick_params(colors=MUTED, labelsize=10)
    return fig, ax


def _title(ax, text):
    ax.set_title(text, loc='left', fontsize=15, fontweight='bold', color=SKY, pad=12)


def _glow_line(ax, x, y, ymax):
    """The bell curve's look (visualizer.plot_ticker_distribution) on a time series."""
    ax.fill_between(x, 0, y, color=SKY, alpha=0.30, lw=0, zorder=1)
    for level in np.linspace(0, ymax, 80):  # white glow toward the peaks
        ax.fill_between(x, level, y, where=y > level, color=WHITE, alpha=(level / ymax) ** 4.0 * 0.05,
                        lw=0, zorder=2)
    ax.plot(x, y, color=SKY, lw=6.5, alpha=0.12, zorder=4, solid_capstyle='round')
    ax.plot(x, y, color=WHITE, lw=3.5, alpha=0.15, zorder=5, solid_capstyle='round')
    ax.plot(x, y, color=WHITE, lw=1.4, zorder=6, solid_capstyle='round')


def _now_marker(ax, x, y, label, dy):
    ax.scatter([x], [y], color=RED, s=70, zorder=9, edgecolor=WHITE, lw=1.6)
    ax.text(x, y + dy, f' {label} ', color=WHITE, fontweight='bold', fontsize=11, zorder=10, ha='center',
            va='bottom', bbox=dict(facecolor=RED, edgecolor='none', boxstyle='round,pad=0.35'))


def _stats_box(ax, text, x=0.02):
    ax.text(x, 0.97, text, transform=ax.transAxes, fontsize=10, color=TEXT, family='monospace',
            fontweight='bold', va='top', zorder=11, parse_math=False,
            bbox=dict(facecolor=BOX, edgecolor=EDGE, boxstyle='round,pad=0.8', alpha=0.92))


def _footnote(fig, text):
    fig.text(0.985, 0.015, text, color=DIM, fontsize=8, ha='right', va='bottom')


def _save(fig, path):
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=DPI, facecolor=BG)


def plot_shiller_pe(series, path, source="Robert Shiller · multpl.com"):
    """``series``: [(ISO date, CAPE)] oldest first. Writes the PNG to ``path``."""
    dates = np.array([np.datetime64(d[:10]) for d, _ in series])
    vals = np.array([float(v) for _, v in series])
    years = 1970 + dates.astype('datetime64[D]').astype(float) / 365.2425  # decimal years: a plain numeric axis
    now_d, now_v = series[-1][0], vals[-1]
    mean, median = float(vals.mean()), float(np.median(vals))
    ymax = float(vals.max()) * 1.25  # headroom: the stats box sits clear of the 1929 label

    fig, ax = _figure()
    _glow_line(ax, years, vals, float(vals.max()))

    ax.axhline(mean, color=MUTED, ls='--', lw=1.1, alpha=0.8, zorder=3)
    ax.text(years[-1], mean - ymax * 0.012, f'MEAN {mean:.1f}', color=MUTED, fontsize=9, fontweight='bold',
            ha='right', va='top', zorder=7)

    for label, start, end, left, lift in EVENTS:
        inside = np.flatnonzero((dates >= np.datetime64(start)) & (dates <= np.datetime64(end)))
        if not inside.size or years[-1] - years[inside[0]] < 1:
            continue
        i = inside[np.argmax(vals[inside])]
        ax.scatter([years[i]], [vals[i]], color=RED, s=34, zorder=9, edgecolor=WHITE, lw=1.2)
        if lift > 0.05:  # a leader from the dot up to the lifted label
            ax.plot([years[i], years[i] - left], [vals[i] + ymax * 0.02, vals[i] + ymax * (lift - 0.01)], color=DIM,
                    lw=0.8, zorder=8)
        ax.text(years[i] - left, vals[i] + ymax * lift, f'{label}\n{dates[i].astype(object):%b %Y}', color=MUTED,
                fontsize=8, fontweight='bold', ha='center', va='bottom', zorder=8, linespacing=1.1)

    _now_marker(ax, years[-1], now_v, f'{now_v:.1f}', ymax * 0.035)

    pct = min(99, int(float((vals < now_v).mean()) * 100))  # share of months below today's
    _stats_box(ax, f"CAPE now:    {now_v:>5.1f}\n"
                   f"Mean:        {mean:>5.1f}\n"
                   f"Median:      {median:>5.1f}\n"
                   f"Percentile:  {_ordinal(pct):>5}")

    _title(ax, 'S&P 500 SHILLER PE (CAPE)')
    ax.set_ylim(0, ymax)
    ax.set_xlim(years[0], years[-1] + (years[-1] - years[0]) * 0.03)
    first = int(np.ceil(years[0] / 20) * 20)
    ax.set_xticks(range(first, int(years[-1]) + 1, 20))
    ax.set_ylabel('PRICE / 10Y AVG REAL EARNINGS', color=MUTED, fontsize=9, fontweight='bold', labelpad=8)
    _footnote(fig, f'Source: {source} · monthly since {dates[0].astype(object):%b %Y} · '
                   f'{_dt.date.fromisoformat(now_d[:10]):%b %d, %Y}')
    _save(fig, path)
    return path


# S&P 500 bear markets (a fall of 20% or more, close to close), shaded as on the PEG history's source chart
BEAR_MARKETS = (("2000-03-24", "2002-10-09"), ("2007-10-09", "2009-03-09"),
                ("2020-02-19", "2020-03-23"), ("2022-01-03", "2022-10-12"))
# The source chart's labelled PEG peaks: its label, and the window holding that peak in the traced history
PEG_PEAKS = (("2000", "1999-06-01", "2001-06-30"), ("2007", "2007-06-01", "2009-06-30"),
             ("2020", "2020-01-01", "2021-03-31"), ("2022", "2021-09-01", "2023-03-31"))


def plot_forward_peg(history, path, reference=(), reference_source="Yardeni Research"):
    """``history``: the Lynch Pin's weekly points [{"date", "peg", "n", "total", "coverage"}] oldest first;
    ``reference``: the monthly history before them [(ISO date, PEG)] oldest first. Either may be empty."""
    ref = [(np.datetime64(d[:10]), float(v)) for d, v in reference]
    own = [(np.datetime64(p["date"][:10]), float(p["peg"])) for p in history]
    xs = np.array([d for d, _ in ref + own])
    vals = np.array([v for _, v in ref + own])
    ymax = max(2.0, float(vals.max()) * 1.25)
    fig, ax = _figure()

    for start, end in BEAR_MARKETS:
        if np.datetime64(end) > xs[0]:
            ax.axvspan(np.datetime64(start), np.datetime64(end), color=SKY, alpha=0.10, lw=0, zorder=0)
    for seg in (ref, own):  # two runs, never joined: the gap between them is time no source covers
        if len(seg) > 1:
            _glow_line(ax, np.array([d for d, _ in seg]), np.array([v for _, v in seg]), float(vals.max()))
        elif seg:
            ax.scatter([seg[0][0]], [seg[0][1]], color=WHITE, s=16, zorder=6)

    for label, start, end in PEG_PEAKS:
        inside = [(d, v) for d, v in ref if np.datetime64(start) <= d <= np.datetime64(end)]
        if inside:
            d, v = max(inside, key=lambda p: p[1])
            ax.scatter([d], [v], color=RED, s=34, zorder=9, edgecolor=WHITE, lw=1.2)
            ax.text(d, v + ymax * 0.035, f'{label}\n{v:.1f}', color=MUTED, fontsize=8, fontweight='bold',
                    ha='center', va='bottom', zorder=8, linespacing=1.1)

    ax.axhline(1.0, color=GREEN, ls='--', lw=1.4, alpha=0.9, zorder=3)
    ax.text(0.01, 1.0 - ymax * 0.015, 'PEG 1.0 · LYNCH FAIR VALUE', transform=ax.get_yaxis_transform(),
            color=GREEN, fontsize=9, fontweight='bold', ha='left', va='top', zorder=7)

    now_d, now_v = xs[-1], vals[-1]
    _now_marker(ax, now_d, now_v, f'{now_v:.2f}', ymax * 0.035)
    pct = min(99, int(float((vals < now_v).mean()) * 100))
    _stats_box(ax, f"PEG now:     {now_v:>5.2f}\n"
                   f"Mean:        {vals.mean():>5.2f}\n"
                   f"Median:      {np.median(vals):>5.2f}\n"
                   f"Percentile:  {_ordinal(pct):>5}",
               x=0.27 if ref else 0.02)  # with the history: over the quiet 2003-12 stretch, clear of the 2000 peak

    _title(ax, 'S&P 500 FORWARD PEG')
    ax.set_ylim(0, ymax)
    span = int((xs[-1] - xs[0]).astype('timedelta64[D]').astype(int))
    ax.set_xlim(xs[0] - np.timedelta64(10, 'D'), xs[-1] + np.timedelta64(max(10, int(span * 0.04)), 'D'))
    import matplotlib.dates as mdates
    if span > 3 * 365:
        ax.xaxis.set_major_locator(mdates.YearLocator(5 if span > 12 * 365 else 1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    else:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d' if span < 90 else '%b %Y'))
    ax.set_ylabel('PRICE / 5Y EXPECTED EPS GROWTH', color=MUTED, fontsize=9, fontweight='bold', labelpad=8)

    notes = []
    if ref:
        notes.append(f'{ref[0][0].astype(object):%b %Y} - {ref[-1][0].astype(object):%b %Y}: {reference_source} '
                     f'(I/B/E/S), traced from a Yahoo Finance chart')
    if own:
        last = history[-1]
        notes.append(f"Since {own[0][0].astype(object):%b %d, %Y}: Lynch Pin, cap-weighted Yahoo PEGs, weekly · "
                     f"{last['n']}/{last['total']} constituents, {last['coverage'] * 100:.0f}% of the index's cap · "
                     f"{own[-1][0].astype(object):%b %d, %Y}")
    fig.text(0.985, 0.012, '\n'.join(notes), color=DIM, fontsize=7.5, ha='right', va='bottom', linespacing=1.3)
    fig.tight_layout(rect=(0, 0.02 + 0.025 * len(notes), 1, 1))
    fig.savefig(path, dpi=DPI, facecolor=BG)
    return path
