import argparse
import pandas as pd
from engine.lynch_pin_core import LynchPinEngine

def main():
    parser = argparse.ArgumentParser(description="Lynch Pin v5.6 - GARP Analysis Tool")
    parser.add_argument("--src", type=str, default="nasdaq_100.txt", help="Source file for tickers")
    # New Filtering Flags
    parser.add_argument("--top", type=int, default=None, help="Show top N results sorted by Dev(SD)")
    parser.add_argument("--excl-bad", action="store_true", help="Exclude non-sustainable tickers marked with *")
    
    args = parser.parse_args()

    try:
        with open(args.src, 'r') as f:
            tickers = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: File {args.src} not found.")
        return

    all_data = []
    print(f"📡 Lynch Pin v5.6 | Source: {args.src}")
    if args.excl_bad:
        print("🛡️  Filtering: High-risk/Non-sustainable (*) excluded.")
    print("")

    for s in tickers:
        engine = LynchPinEngine(s)
        result = engine.get_ticker_stats()
        if result:
            all_data.append(result)

    if not all_data:
        print("No valid data retrieved.")
        return

    df = pd.DataFrame(all_data)

    # 1. Handle Exclusions
    if args.excl_bad:
        # Tickers with '*' at the end of the string are removed
        df = df[~df['Ticker'].str.endswith('*')]

    # 2. Sort by Dev_SD (Ascending = most undervalued relative to historical mean)
    df = df.sort_values(by='Dev_SD', ascending=True)

    # 3. Handle Top N
    if args.top is not None:
        df = df.head(args.top)

    if df.empty:
        print("No tickers matched your filter criteria.")
        return

    header = (f"{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | "
              f"{'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | "
              f"{'Bull':>8} | {'Base':>8} | {'Bear':>8}")
    
    print(header)
    print("-" * len(header))
    
    for _, r in df.iterrows():
        print(f"{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | "
              f"{r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | "
              f"{r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | "
              f"{r['Base']:>8} | {r['Bear']:>8}")

if __name__ == "__main__":
    main()
