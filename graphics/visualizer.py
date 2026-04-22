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
            "font.family": "sans-serif"
        })
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def _get_benchmark_data(self, source_name):
        """Maps source files to tickers and fetches CAGR from available history.
        Falls back to S&P 500 if the mapped ticker has no data."""
        src = os.path.basename(source_name).lower().replace('.txt', '')

        ticker_sym, label = "^GSPC", "S&P 500"
        for key, (sym, lbl) in BENCHMARKS.items():
            if key in src:
                ticker_sym, label = sym, lbl
                break

        # Try mapped ticker first, fall back to S&P 500
        for sym, lbl in [(ticker_sym, label), ("^GSPC", "S&P 500")]:
            try:
                data = yf.download(sym, period="5y", progress=False)
                if data.empty:
                    continue

                for col in ['Adj Close', 'Close']:
                    if col in data.columns:
                        prices = data[col]
                        break
                else:
                    continue

                if hasattr(prices, 'columns'):
                    prices = prices.iloc[:, 0]

                prices = prices.dropna()
                if len(prices) < 2:
                    continue

                start_price = float(prices.iloc[0])
                end_price = float(prices.iloc[-1])

                days = (prices.index[-1] - prices.index[0]).days
                years = days / 365.25
                if years < 0.5:
                    continue

                cagr = ((end_price / start_price) ** (1 / years) - 1) * 100
                return f"{lbl} ({years:.1f}Y)", float(cagr)
            except Exception as e:
                print(f"⚠️ Benchmark fetch error ({sym}): {e}")
                continue

        return "S&P 500", 10

    def plot_comparative_benchmark(self, df, source_name):
        """Styled Bar Chart with Index Benchmark line and PEG badges."""
        plt.figure(figsize=(10, 6))
        index_label, index_return = self._get_benchmark_data(source_name)

        labels, returns, pegs = [index_label], [index_return], [None]
        for _, row in df.iterrows():
            labels.append(row['Ticker'].replace('*', ''))
            returns.append(float(row['Base'].replace('%', '')))
            pegs.append(row['PEG'])

        colors = ['#444444'] + ['#00B4DB' for _ in range(len(labels) - 1)]
        bars = plt.bar(labels, returns, color=colors, alpha=0.9, edgecolor='#0083B0', linewidth=1.5)

        plt.axhline(index_return, color='#FF4B2B', linestyle='--', lw=2, alpha=0.8, zorder=3)
        plt.text(len(labels) - 0.5, index_return + 0.5, 'INDEX FLOOR', color='#FF4B2B',
                 fontweight='bold', fontsize=9, ha='right')

        plt.title('5Y INDEX RETURN vs PROJECTED 5Y BASE CASE ROI',
                  loc='left', fontsize=13, fontweight='bold', pad=25, color='#00B4DB')
        plt.ylabel('% ANNUALIZED RETURN', fontweight='bold', fontsize=10, alpha=0.7)
        plt.gca().spines['top'].set_visible(False)
        plt.gca().spines['right'].set_visible(False)

        for i, bar in enumerate(bars):
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width() / 2, yval + 1, f'{yval:.1f}%',
                     ha='center', va='bottom', color='white', fontweight='bold')

            if pegs[i] is not None:
                plt.text(bar.get_x() + bar.get_width() / 2, yval - 4, f'PEG {pegs[i]:.2f}',
                         ha='center', va='top', color='#B0B0B0', fontsize=8, fontweight='bold',
                         bbox=dict(facecolor='#252525', edgecolor='#444444', boxstyle='round,pad=0.3'))

        plt.tight_layout()
        path = os.path.join(self.output_dir, "benchmark_comparison.png")
        plt.savefig(path, dpi=300, facecolor='#121212')
        plt.close()
        return path

    def plot_ticker_distribution(self, row):
        """Styled Distribution plot with ALIGNED tabulated legend box."""
        ticker = row['Ticker'].replace('*', '')
        current_peg, mean_peg, z_score = row['PEG'], row['Mean'], row['Dev_SD']

        sd = abs((current_peg - mean_peg) / z_score) if z_score != 0 else 0.5
        x = np.linspace(mean_peg - 4 * sd, mean_peg + 4 * sd, 300)
        y = stats.norm.pdf(x, mean_peg, sd)

        plt.figure(figsize=(9, 5))
        plt.plot(x, y, color='#00B4DB', lw=3)
        plt.fill_between(x, y, color='#00B4DB', alpha=0.15)

        plt.axvline(current_peg, color='#FF4B2B', linestyle='--', lw=2.5)
        plt.scatter([current_peg], [stats.norm.pdf(current_peg, mean_peg, sd)],
                    color='#FF4B2B', s=100, zorder=5, edgecolor='white')

        plt.title(f'{ticker} VALUATION DEVIATION (PEG)', loc='left', fontsize=14, fontweight='bold', color='#00B4DB', pad=15)
        plt.text(current_peg, max(y) * 0.95, f' {z_score} SD ', color='white', fontweight='bold',
                 bbox=dict(facecolor='#FF4B2B', edgecolor='none', boxstyle='round,pad=0.3'))

        stats_text = (
            f"  Ticker:     {ticker:>10}\n"
            f"------------------------\n"
            f"- PE:         {row['PE']:>10.1f}\n"
            f"- Fwd PE:     {row['FwdPE']:>10.1f}\n"
            f"- 2YFwd PE:   {row['2YFwd']:>10.1f}\n"
            f"- PEG:        {current_peg:>10.2f}\n"
            f"- 5Y Growth:  {str(row['5YGrowth']):>10}\n"
            f"- Bull ROI:   {row['Bull']:>10}\n"
            f"- Base ROI:   {row['Base']:>10}\n"
            f"- Bear ROI:   {row['Bear']:>10}"
        )

        plt.gca().text(0.97, 0.92, stats_text, transform=plt.gca().transAxes,
                       fontsize=9, color='#E0E0E0', family='monospace',
                       verticalalignment='top', horizontalalignment='right',
                       bbox=dict(facecolor='#1A1A1A', edgecolor='#333333', boxstyle='round,pad=0.8', alpha=0.8))

        plt.xlabel('PEG RATIO', fontweight='bold', alpha=0.7)
        plt.yticks([])
        plt.gca().spines['top'].set_visible(False)
        plt.gca().spines['right'].set_visible(False)
        plt.gca().spines['left'].set_visible(False)

        plt.tight_layout()
        path = os.path.join(self.output_dir, f"{ticker}_valuation.png")
        plt.savefig(path, dpi=300, facecolor='#121212')
        plt.close()
        return path

