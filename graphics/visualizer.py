import matplotlib.pyplot as plt
import numpy as np
import scipy.stats as stats
import os
import yfinance as yf

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
        """Maps source files to tickers and fetches 5Y CAGR from yfinance."""
        # Hardcoded mapping: mag7.txt -> MAGS ticker
        if "mag7" in source_name.lower():
            ticker_sym = "MAGS"
            label = "Mag 7 (MAGS)"
        elif "nasdaq" in source_name.lower():
            ticker_sym = "^NDX"
            label = "Nasdaq 100"
        else:
            ticker_sym = "^GSPC"
            label = "S&P 500"

        try:
            # Fetch 5 years of data
            data = yf.download(ticker_sym, period="5y", progress=False)
            if data.empty:
                return label, 13.5 # Fallback if fetch fails
            
            start_price = data['Adj Close'].iloc[0]
            end_price = data['Adj Close'].iloc[-1]
            
            # Calculate CAGR: ((End/Start)^(1/5) - 1) * 100
            cagr = ((end_price / start_price) ** (1/5) - 1) * 100
            return label, float(cagr)
        except Exception as e:
            print(f"⚠️ Benchmark fetch error: {e}")
            return label, 13.5

    def plot_comparative_benchmark(self, df, source_name):
        """Styled Bar Chart: MAGS/Index vs. Projected ROI."""
        plt.figure(figsize=(10, 6))
        
        index_label, index_return = self._get_benchmark_data(source_name)
        
        labels = [f"{index_label}\n(5Y Hist)"]
        returns = [index_return]
        
        for _, row in df.iterrows():
            ticker = row['Ticker'].replace('*', '')
            base_val = float(row['Base'].replace('%', ''))
            labels.append(f"{ticker}\n(Base ROI)")
            returns.append(base_val)

        colors = ['#444444'] + ['#00B4DB' for _ in range(len(labels)-1)]
        bars = plt.bar(labels, returns, color=colors, alpha=0.9, edgecolor='#0083B0', linewidth=1.5)
        
        plt.title('5Y INDEX RETURN vs PROJECTED 5Y BASE CASE ROI', 
                  loc='left', fontsize=13, fontweight='bold', pad=25, color='#00B4DB')
        
        plt.ylabel('% ANNUALIZED RETURN', fontweight='bold', fontsize=10, alpha=0.7)
        plt.grid(axis='y', linestyle='-', alpha=0.1)
        plt.gca().spines['top'].set_visible(False)
        plt.gca().spines['right'].set_visible(False)

        for bar in bars:
            yval = bar.get_height()
            plt.text(bar.get_x() + bar.get_width()/2, yval + 1, f'{yval:.1f}%', 
                     ha='center', va='bottom', color='white', fontweight='bold')

        plt.tight_layout()
        path = os.path.join(self.output_dir, "benchmark_comparison.png")
        plt.savefig(path, dpi=300, facecolor='#121212')
        plt.close()
        return path

    def plot_ticker_distribution(self, row):
        """Styled Distribution plot for PEG analysis."""
        ticker = row['Ticker'].replace('*', '')
        current_peg, mean_peg, z_score = row['PEG'], row['Mean'], row['Dev_SD']
        
        # Recover SD: SD = |(Current - Mean) / Z|
        sd = abs((current_peg - mean_peg) / z_score) if z_score != 0 else 0.5
        
        x = np.linspace(mean_peg - 4*sd, mean_peg + 4*sd, 300)
        y = stats.norm.pdf(x, mean_peg, sd)
        
        plt.figure(figsize=(9, 5))
        plt.plot(x, y, color='#00B4DB', lw=3)
        plt.fill_between(x, y, color='#00B4DB', alpha=0.15)
        
        plt.axvline(current_peg, color='#FF4B2B', linestyle='--', lw=2.5)
        plt.scatter([current_peg], [stats.norm.pdf(current_peg, mean_peg, sd)], 
                    color='#FF4B2B', s=100, zorder=5, edgecolor='white')

        plt.title(f'{ticker} VALUATION DEVIATION (PEG)', loc='left', fontsize=14, fontweight='bold', color='#00B4DB', pad=15)
        
        plt.text(current_peg, max(y)*0.95, f' {z_score} SD ', 
                 color='white', fontweight='bold', fontsize=11,
                 bbox=dict(facecolor='#FF4B2B', edgecolor='none', boxstyle='round,pad=0.3'))

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
