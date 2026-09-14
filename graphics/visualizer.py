import matplotlib.pyplot as plt
import numpy as np
import scipy.stats as stats
import os
import yfinance as yf

BENCHMARKS = {
    "mag7":   ("MAGS", "Mag 7 (MAGS)"),
    "mags":   ("MAGS", "Mag 7 (MAGS)"),
    "nasdaq": ("QQQ",  "QQQ"),
    "qqq":    ("QQQ",  "QQQ"),
    "schd":   ("SCHD", "SCHD"),
    "smh":    ("SMH",  "SMH"),
    "igv":    ("IGV",  "IGV"),
    "spy":    ("SPY",  "S&P 500"),
}

class LynchPinVisualizer:
    def __init__(self, output_dir="tmp"):
        self.output_dir = output_dir
        plt.rcParams.update({
            "figure.facecolor": "#121212",
            "axes.facecolor": "#121212",
            "axes.edgecolor": "#333333",
            "axes.labelcolor": "#E0E0E0",
            "xtick.color": "#B0B0B0",
            "ytick.color": "#B0B0B0",
            "grid.color": "#252525",
            "text.color": "#FFFFFF",
            "font.family": "sans-serif",
            "font.size": 14,          
            "axes.titlesize": 20,      
            "axes.labelsize": 16       
        })
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def _get_benchmark_data(self, source_name):
        src = os.path.basename(source_name).lower().replace('.txt', '')
        ticker_sym, label = "^GSPC", "S&P 500"
        for key, (sym, lbl) in BENCHMARKS.items():
            if key in src:
                ticker_sym, label = sym, lbl
                break

        for sym, lbl in [(ticker_sym, label), ("^GSPC", "S&P 500")]:
            try:
                data = yf.download(sym, period="5y", progress=False)
                if data.empty: continue
                prices = data['Adj Close'] if 'Adj Close' in data.columns else data['Close']
                if hasattr(prices, 'columns'): prices = prices.iloc[:, 0]
                prices = prices.dropna()
                if len(prices) < 2: continue
                years = (prices.index[-1] - prices.index[0]).days / 365.25
                cagr = ((float(prices.iloc[-1]) / float(prices.iloc[0])) ** (1 / years) - 1) * 100
                return f"{lbl} ({years:.1f}Y)", float(cagr)
            except Exception: continue
        return "S&P 500", 10

    def plot_comparative_benchmark(self, df, source_name):
        """High-pop bar chart with corrected top-heavy glowing highlights."""
        plt.figure(figsize=(12, 7))
        index_label, index_return = self._get_benchmark_data(source_name)

        labels, returns, pegs = [index_label], [index_return], [None]
        for _, row in df.iterrows():
            labels.append(row['Ticker'].replace('*', ''))
            returns.append(float(row['Base'].replace('%', '')))
            pegs.append(row['PEG'])

        x_pos = np.arange(len(labels))
        sky_blue = '#5D9CEC'
        pure_white = '#FFFFFF'
        
        from matplotlib.patches import FancyBboxPatch

        # 1. BARS WITH ROUNDED EDGES AND GLOW
        ax = plt.gca()
        bar_width = 0.8
        for i, val in enumerate(returns):
            is_index = (i == 0)
            base_color = '#444444' if is_index else sky_blue

            # Glow layer (behind)
            glow = FancyBboxPatch((x_pos[i] - bar_width/2 - 0.02, -0.5), bar_width + 0.04, val + 0.5,
                                  boxstyle='round,pad=0,rounding_size=0.5',
                                  facecolor='none', edgecolor=sky_blue,
                                  linewidth=5, alpha=0.15, zorder=1)
            ax.add_patch(glow)

            # Main bar with white border
            bar = FancyBboxPatch((x_pos[i] - bar_width/2, 0), bar_width, val,
                                 boxstyle='round,pad=0,rounding_size=0.5',
                                 facecolor=base_color, edgecolor=pure_white,
                                 linewidth=2.5, alpha=0.9, zorder=2)
            ax.add_patch(bar)

            # Top-Down Airy Glow clipped to rounded bar shape
            glow_start = val * 0.7
            for level in np.linspace(glow_start, val, 20):
                intensity = ((level - glow_start) / (val - glow_start)) ** 2.0
                glow_bar = FancyBboxPatch((x_pos[i] - bar_width/2, level), bar_width, val - level,
                                          boxstyle='round,pad=0,rounding_size=0.5',
                                          facecolor=pure_white, edgecolor='none',
                                          alpha=intensity * 0.15, zorder=3)
                ax.add_patch(glow_bar)

        # 2. RED INDEX FLOOR LINE
        plt.axhline(index_return, color='#FF4B2B', linestyle='--', lw=3, alpha=1.0, zorder=5)
        plt.text(len(labels) - 0.5, index_return + 1.2, 'INDEX FLOOR', color='#FF4B2B',
                 fontweight='black', fontsize=12, ha='right', zorder=6)

        # 3. STYLING THE AXIS
        ax = plt.gca()
        ax.set_facecolor('#121212')
        plt.title('5Y INDEX RETURN vs PROJECTED 5Y BASE CASE ROI',
                  loc='left', fontsize=20, fontweight='bold', pad=15, color=sky_blue)
        
        for spine in ['top', 'right']: ax.spines[spine].set_visible(False)
        ax.spines['bottom'].set_color('#444444')
        ax.spines['left'].set_color('#444444')

        # 4. ANNOTATIONS: BOLD PERCENTAGES & GLOWING BADGES
        for i, val in enumerate(returns):
            # Top Percentage
            plt.text(i, val + 1.8, f'{val:.1f}%', ha='center', va='bottom', 
                     color=pure_white, fontweight='black', fontsize=15)

            # PEG Badges: Bold White borders with high contrast
            if pegs[i] is not None:
                # Main Badge Body
                plt.text(i, val * 0.35, f'PEG {pegs[i]:.2f}',
                         ha='center', va='center', color=pure_white, 
                         fontsize=12, fontweight='black',
                         bbox=dict(facecolor='#121212', 
                                   edgecolor=pure_white, 
                                   boxstyle='round,pad=0.6', 
                                   linewidth=2.5), zorder=10)
                
                # Subtle Cyan Glow behind the badge to make it "pop"
                plt.text(i, val * 0.35, f'PEG {pegs[i]:.2f}',
                         ha='center', va='center', color='none',
                         bbox=dict(facecolor='none', 
                                   edgecolor=sky_blue, 
                                   boxstyle='round,pad=0.8', 
                                   linewidth=5, 
                                   alpha=0.2), zorder=9)

        plt.xticks(x_pos, labels, fontweight='bold', color='#E0E0E0', fontsize=12)
        ax.set_xlim(-0.6, len(labels) - 0.4)
        ax.set_ylim(0, max(returns) * 1.25)
        plt.ylabel('% ANNUALIZED RETURN', fontweight='bold', alpha=0.9, color='#B0B0B0', labelpad=15)
        
        plt.tight_layout()
        path = os.path.join(self.output_dir, "benchmark_comparison.png")
        plt.savefig(path, dpi=300, facecolor='#121212')
        plt.close()
        return path

    def plot_ticker_distribution(self, row, grade_result=None, bs_result=None, tech_result=None, edge_result=None):
        ticker = row['Ticker'].replace('*', '')
        current_peg, mean_peg, z_score = row['PEG'], row['Mean'], row['Dev_SD']
        
        sd = abs((current_peg - mean_peg) / z_score) if z_score != 0 else 0.5
        x = np.linspace(mean_peg - 4 * sd, mean_peg + 4 * sd, 500)
        y = stats.norm.pdf(x, mean_peg, sd)

        fig, ax = plt.subplots(figsize=(12, 7))

        # Final Vibrant Palette
        sky_blue = '#5D9CEC'
        pure_white = '#FFFFFF'
        y_max = np.max(y)

        # 1. BRIGHT BASE FILL: Increased alpha to 0.45 for maximum blue "pop"
        ax.fill_between(x, 0, y, color=sky_blue, alpha=0.45, zorder=1)

        # 2. ULTRA-TRANSPARENT TOP GLOW: Higher exponent (4.0) makes it more localized to the peak
        for level in np.linspace(0, y_max, 100):
            # The higher the exponent, the more the white is restricted to the top
            intensity = (level / y_max) ** 4.0 
            ax.fill_between(x, level, y, where=(y > level), 
                            color=pure_white, alpha=intensity * 0.05, zorder=2)

        # 3. GLOWING WHITE VERTICAL SD LINES
        for i in range(-3, 4):
            x_pos = mean_peg + i * sd
            line_h = stats.norm.pdf(x_pos, mean_peg, sd)
            # Sharp Core
            ax.vlines(x_pos, 0, line_h, color=pure_white, linestyle='-', lw=1.2, alpha=0.5, zorder=3)
            # Subtle glow to match the airy feel
            ax.vlines(x_pos, 0, line_h, color=pure_white, linestyle='-', lw=6, alpha=0.05, zorder=3)

        # 4. BOLD GLOWING WHITE BELL CURVE
        ax.plot(x, y, color=pure_white, lw=4, zorder=6) 
        ax.plot(x, y, color=pure_white, lw=12, alpha=0.15, zorder=5) # Clean White Glow
        ax.plot(x, y, color=sky_blue, lw=22, alpha=0.12, zorder=4) # Bright Blue Glow

        # 5. Red Marker (Current Position)
        marker_h = stats.norm.pdf(current_peg, mean_peg, sd)
        ax.vlines(current_peg, 0, marker_h, color='#FF4B2B', linestyle='--', lw=2.5, zorder=7)
        ax.scatter([current_peg], [marker_h], color='#FF4B2B', s=180, zorder=9, edgecolor='white', lw=2)

        ax.text(current_peg, marker_h * 1.12, f' {z_score} SD ',
                 color='white', fontweight='bold', fontsize=14, zorder=10, ha='center',
                 bbox=dict(facecolor='#FF4B2B', edgecolor='none', boxstyle='round,pad=0.4'))

        # Header & Labels
        ax.set_title(f'{ticker} VALUATION DEVIATION (PEG)', loc='left', 
                     fontsize=20, fontweight='bold', color=sky_blue, pad=35)
        
        box_style = dict(facecolor='#1A1A1A', edgecolor='#333333', boxstyle='round,pad=1.2', alpha=0.9)
        
        # Stats box (left) — valuation + technicals
        stats_text = (
            f"Ticker:     {ticker:>8}\n"
            f"--------------------\n"
            f"- PE:       {row.get('PE', 0):>8.1f}\n"
            f"- Fwd PE:   {row.get('FwdPE', 0):>8.1f}\n"
            f"- 2YFwd PE: {row.get('2YFwd', 0):>8.1f}\n" 
            f"- PEG:      {current_peg:>8.2f}\n"
            f"- 5Y Growth:{row.get('5YGrowth', '0%'):>8}\n"   
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
            stats_text += (
                f"\n- 6M Bull edge: {edge_result['bull_acc']:>3.0f}%\n"
                f"- 6M Bear edge: {edge_result['bear_acc']:>3.0f}%"
            )
        ax.text(0.02, 1.02, stats_text, transform=ax.transAxes, fontsize=16, 
                color='#E0E0E0', family='monospace', verticalalignment='top',
                bbox=box_style, zorder=9, fontweight='bold', parse_math=False)

        # Income grade + Credit rating (single box, right side)
        right_lines = []
        if grade_result:
            sig_map = {'\U0001f7e2': '\u2713', '\U0001f535': '~', '\U0001f534': '\u2717', '\u26aa': ' '}
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

        if right_lines:
            ax.text(0.98, 1.09, "\n".join(right_lines), transform=ax.transAxes, 
                    fontsize=16, color='#E0E0E0', family='monospace', ha='right',
                    verticalalignment='top', bbox=box_style, zorder=9, fontweight='bold')

        ax.set_xlabel('PEG RATIO', fontweight='bold', fontsize=14, alpha=0.7)
        ax.set_yticks([])
        for spine in ['top', 'right', 'left']: ax.spines[spine].set_visible(False)

        plt.tight_layout()
        path = os.path.join(self.output_dir, f"{ticker}_valuation.png")
        plt.savefig(path, dpi=300, facecolor='#121212')
        plt.close()
        return path

    # Any position too thin to carry its label on the wedge is folded into "Other".
    _PIE_MIN_SLICE = 0.035

    def _pie_slices(self, weights):
        """Collapses positions below ``_PIE_MIN_SLICE`` into a single 'Other (n)' slice.

        Every remaining slice is wide enough to be labelled on the wedge itself,
        so no labels ever need to spill outside the donut.
        """
        items = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
        keep = [(t, w) for t, w in items if w >= self._PIE_MIN_SLICE]
        small = [(t, w) for t, w in items if w < self._PIE_MIN_SLICE]
        if len(small) == 1:
            # A lone small position is clearer named than hidden behind "Other"
            keep.append(small[0])
        elif small:
            keep.append((f"Other ({len(small)})", sum(w for _, w in small)))
        return keep

    def plot_portfolio(self, weights, wm, benchmark_returns=None):
        """Portfolio X-ray: market-value weight donut + weighted GARP stats.

        Args:
            weights: ticker → weight (fractions summing to ~1). No $ values.
            wm: output of ``engine.portfolio.weighted_metrics``.
            benchmark_returns: optional ``[(label, cagr_pct), ...]``; when
                None, QQQ and S&P 500 5Y CAGRs are fetched via yfinance.
        """
        if benchmark_returns is None:
            benchmark_returns = [self._get_benchmark_data("qqq"), self._get_benchmark_data("spy")]

        sky_blue = '#5D9CEC'
        pure_white = '#FFFFFF'
        fig = plt.figure(figsize=(12, 7))
        ax = fig.add_axes([0.01, 0.02, 0.52, 0.86])  # donut occupies the left half

        slices = self._pie_slices(weights)
        labels = [t for t, _ in slices]
        vals = [w for _, w in slices]

        # Blue-to-white gradient so the largest positions "pop" brightest
        cmap = plt.get_cmap('Blues')
        colors = [cmap(0.85 - 0.55 * (i / max(len(vals) - 1, 1))) for i in range(len(vals))]

        wedges, _ = ax.pie(vals, startangle=90, counterclock=False, colors=colors,
                           wedgeprops=dict(width=0.42, edgecolor='#121212', linewidth=2.5))
        # Soft glow ring behind the donut
        ax.add_patch(plt.Circle((0, 0), 1.03, color=sky_blue, alpha=0.10, zorder=0, lw=0))

        # Labels: "TICKER  12%" on each wedge. Slices are pre-grouped so all are
        # wide enough, except possibly a lone small position / thin "Other" —
        # those get a compact label just outside the ring.
        for wedge, lbl, w in zip(wedges, labels, vals):
            ang = np.deg2rad((wedge.theta1 + wedge.theta2) / 2)
            r = 0.79
            x, y = r * np.cos(ang), r * np.sin(ang)
            if w >= self._PIE_MIN_SLICE:
                ax.text(x, y, f"{lbl}\n{w * 100:.1f}%", ha='center', va='center',
                        fontsize=11 if w >= 0.08 else 9, fontweight='bold',
                        color='#121212' if w >= 0.12 else pure_white, zorder=5)
            else:
                ax.text(1.14 * np.cos(ang), 1.14 * np.sin(ang), f"{lbl} {w * 100:.1f}%",
                        ha='center', va='center', fontsize=8, color='#B0B0B0', zorder=5)

        # Center: headline weighted PEG vs historical mean
        peg = wm.get('PEG')
        dev = wm.get('Dev_SD')
        ax.text(0, 0.12, f"{peg:.2f}" if peg is not None else "N/A", ha='center', va='center',
                fontsize=30, fontweight='black', color=pure_white)
        ax.text(0, -0.16, "WEIGHTED PEG", ha='center', va='center',
                fontsize=11, fontweight='bold', color='#B0B0B0')
        if dev is not None:
            ax.text(0, -0.36, f"{dev:+.2f} SD", ha='center', va='center', fontsize=12,
                    fontweight='bold', color=pure_white, zorder=10,
                    bbox=dict(facecolor='#FF4B2B' if dev > 0 else '#2ECC71',
                              edgecolor='none', boxstyle='round,pad=0.3'))
        ax.set_xlim(-1.08, 1.08)
        ax.set_ylim(-1.08, 1.08)
        ax.set_aspect('equal')
        ax.axis('off')

        # Rendered vertical extent of the donut ring (figure coords) — the title and
        # the right-hand boxes are laid out relative to it.
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        to_fig = fig.transFigure.inverted()
        ring_top = to_fig.transform(ax.transData.transform((0, 1.0)))[1]
        ring_bottom = to_fig.transform(ax.transData.transform((0, -1.0)))[1]

        title = fig.text(0.03, ring_top + 0.035, 'PORTFOLIO X-RAY: WEIGHT & WEIGHTED GARP METRICS',
                         fontsize=20, fontweight='bold', color=sky_blue, va='bottom')
        title_bottom = to_fig.transform((0, title.get_window_extent(renderer).y0))[1]

        box_style = dict(facecolor='#1A1A1A', edgecolor='#333333', boxstyle='round,pad=0.9', alpha=0.9)

        def fmt(v, spec, suffix=''):
            return (format(v, spec) + suffix) if v is not None else 'N/A'

        # Stats box (right, top) — mirrors the per-ticker box, "Portfolio:" instead of "Ticker:"
        n_pos = len(weights)
        stats_text = (
            f"Portfolio:{(str(n_pos) + ' positions'):>13}\n"
            f"-----------------------\n"
            f"- W. PE:       {fmt(wm.get('PE'), '.1f'):>8}\n"
            f"- W. Fwd PE:   {fmt(wm.get('FwdPE'), '.1f'):>8}\n"
            f"- W. 2YFwd PE: {fmt(wm.get('2YFwd'), '.1f'):>8}\n"
            f"- W. PEG:      {fmt(wm.get('PEG'), '.2f'):>8}\n"
            f"- Median PEG:  {fmt(wm.get('MedianPEG'), '.2f'):>8}\n"
            f"- W. Growth:   {fmt(wm.get('Growth'), '.1f', '%'):>8}\n"
            f"- W. Bull ROI: {fmt(wm.get('Bull'), '.1f', '%'):>8}\n"
            f"- W. Base ROI: {fmt(wm.get('Base'), '.1f', '%'):>8}\n"
            f"- W. Bear ROI: {fmt(wm.get('Bear'), '.1f', '%'):>8}"
        )

        # Quality + benchmark box (right, lower)
        base = wm.get('Base')
        q_lines = [
            f"Quality (weighted)",
            f"-----------------------",
            f"- Income Grade: {wm.get('IncomeGrade', 'N/A'):>7}",
            f"- Credit Rating:{wm.get('CreditRating', 'NR'):>7}",
            "",
            f"5Y Index Return vs Base",
            f"-----------------------",
        ]
        for lbl, cagr in benchmark_returns:
            short = lbl.split(' (')[0]
            q_lines.append(f"- {short:<10}{cagr:>10.1f}%")
        q_lines.append(f"- {'Portfolio':<10}{fmt(base, '.1f', '%'):>11}")
        quality_text = "\n".join(q_lines)

        # Lay the two boxes out in the span between the title's bottom and the ring's
        # bottom: lower box stands on the ring's bottom edge, and the gap title→upper
        # box equals the gap upper→lower box. Font steps down only if they can't fit.
        def _patch_extent(t):
            bb = t.get_bbox_patch().get_window_extent(renderer)
            return to_fig.transform((0, bb.y0))[1], to_fig.transform((0, bb.y1))[1]

        for fs in (14, 13, 12, 11):
            top_box = fig.text(0.55, 0.5, stats_text, fontsize=fs, color='#E0E0E0',
                               family='monospace', va='top', bbox=box_style, fontweight='bold')
            low_box = fig.text(0.55, 0.5, quality_text, fontsize=fs, color='#E0E0E0',
                               family='monospace', va='bottom', bbox=box_style, fontweight='bold')
            fig.canvas.draw()
            t_lo, t_hi = _patch_extent(top_box)
            l_lo, l_hi = _patch_extent(low_box)
            gap = (title_bottom - ring_bottom - (t_hi - t_lo) - (l_hi - l_lo)) / 2.0
            if gap >= 0.015 or fs == 11:
                break
            top_box.remove()
            low_box.remove()

        # Shift each box so its frame (not just its text) lands on the target edge
        top_box.set_position((0.55, 0.5 + (title_bottom - gap) - t_hi))
        low_box.set_position((0.55, 0.5 + ring_bottom - l_lo))

        # Crop to the visible content (ring, title, boxes) with an equal outer
        # margin on every side = half the ring→box gap. Computed explicitly
        # because bbox_inches='tight' would also include the invisible axes frame.
        fig.canvas.draw()
        from matplotlib.transforms import Bbox
        fw, fh = fig.get_figwidth(), fig.get_figheight()
        ring_left = to_fig.transform(ax.transData.transform((-1.03, 0)))[0]
        ring_right = to_fig.transform(ax.transData.transform((1.03, 0)))[0]
        ring_low = to_fig.transform(ax.transData.transform((0, -1.03)))[1]
        box_exts = [t.get_bbox_patch().get_window_extent(renderer) for t in (top_box, low_box)]
        box_left = min(to_fig.transform((b.x0, 0))[0] for b in box_exts)
        box_right = max(to_fig.transform((b.x1, 0))[0] for b in box_exts)
        title_top = to_fig.transform((0, title.get_window_extent(renderer).y1))[1]
        pad_in = max((box_left - ring_right) * fw / 2.0, 0.05)

        crop = Bbox.from_extents(min(ring_left, 0.03) * fw - pad_in,
                                 ring_low * fh - pad_in,
                                 max(box_right, ring_right) * fw + pad_in,
                                 title_top * fh + pad_in)

        path = os.path.join(self.output_dir, "portfolio_allocation.png")
        plt.savefig(path, dpi=300, facecolor='#121212', bbox_inches=crop)
        plt.close(fig)
        return path
