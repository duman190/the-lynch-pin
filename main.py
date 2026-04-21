import yfinance as yf
import pandas as pd
import numpy as np
import warnings
import os
import argparse

warnings.filterwarnings("ignore", category=UserWarning, module='urllib3')

def get_ticker_stats(symbol):
    try:
        t = yf.Ticker(symbol)
        info = t.info
        
        curr_price = info.get('currentPrice')
        curr_pe = info.get('trailingPE')
        fwd_pe = info.get('forwardPE')
        fwd_eps = info.get('forwardEps')
        eps = info.get('trailingEps', 1)
        
        # 1. DERIVE GROWTH (Implied from 2Y P/E Waterfall)
        # Using 2Y Fwd PE to back-calculate the 2-year CAGR
        eps_2y = fwd_eps * 1.15 # Placeholder logic if 2Y specific EPS isn't in info
        # Standard approach: back-calculate growth from PE compression
        # g = sqrt(PE_curr / PE_2y) - 1
        
        # Pulling explicit 2y forward estimates if possible, else using fwd_pe
        pe_2y_fwd = info.get('forwardPE', curr_pe) * 0.85 # Conservative estimate
        
        # Better: Derive g from (Price/ForwardEPS_1 / Price/TrailingEPS)
        # We calculate the growth analysts expect from Year 0 to Year 1
        g_implied = (eps * (curr_pe / fwd_pe) / eps) - 1 if fwd_pe else 0
        
        # If we want the 5Y analyst consensus specifically:
        growth_pct = info.get('earningsGrowth', 0) * 100
        if growth_pct == 0 or growth_pct < 2: # Fallback to implied if data is missing/stale
            growth_pct = g_implied * 100

        # 2. Risk Flag
        curr_peg = curr_pe / growth_pct if growth_pct > 0 else 0
        display_ticker = f"{symbol}*" if (growth_pct > 25 or curr_peg >= 2.0) else symbol

        # 3. Valuation Sequence
        pe_2y_fwd_calc = curr_price / (fwd_eps * (1 + (growth_pct/100))) if fwd_eps else curr_pe

        # 4. PEG Mean & SD
        hist = t.history(period="5y", interval="1mo")
        mean_peg = (hist['Close'].mean() / eps) / growth_pct if not hist.empty else 1.0
        std_peg = mean_peg * 0.25 
        dev_sd = (curr_peg - mean_peg) / std_peg if std_peg > 0 else 0

        # 5. ROI Scenarios
        projected_5y_eps = eps * ((1 + (growth_pct/100)) ** 5)
        def calc_5y_roi(target_peg):
            pt = target_peg * growth_pct * projected_5y_eps
            return ((pt / curr_price) ** 0.2) - 1 if pt > 0 else -1

        return {
            "Ticker": display_ticker,
            "PE": round(curr_pe, 1),
            "FwdPE": round(fwd_pe, 1) if fwd_pe else 0,
            "2YFwd": round(pe_2y_fwd_calc, 1),
            "5YGrowth": f"{round(growth_pct, 1)}%",
            "PEG": round(curr_peg, 2),
            "Mean": round(mean_peg, 2),
            "Dev_SD": round(dev_sd, 2),
            "Bull": f"{round(calc_5y_roi(mean_peg + std_peg) * 100, 1)}%",
            "Base": f"{round(calc_5y_roi(mean_peg) * 100, 1)}%",
            "Bear": f"{round(calc_5y_roi(min(curr_peg, mean_peg)) * 100, 1)}%"
        }
    except: return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=str, default="nasdaq_100.txt")
    args = parser.parse_args()
    
    with open(args.src, 'r') as f:
        tickers = [line.strip() for line in f if line.strip()]

    all_data = [get_ticker_stats(s) for s in tickers if get_ticker_stats(s)]
    df = pd.DataFrame(all_data).sort_values(by='Dev_SD')

    header = f"{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | {'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | {'Bull':>8} | {'Base':>8} | {'Bear':>8}"
    print(header + "\n" + "-" * len(header))
    for _, r in df.iterrows():
        print(f"{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | {r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | {r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | {r['Base']:>8} | {r['Bear']:>8}")

if __name__ == "__main__":
    main()
