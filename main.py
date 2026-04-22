import argparse
import pandas as pd
import os
from engine.lynch_pin_core import LynchPinEngine
from engine.ai_research import LynchPinResearcher
from graphics.visualizer import LynchPinVisualizer

def main():
    parser = argparse.ArgumentParser(description="Lynch Pin v5.7 - GARP Analysis with AI")
    parser.add_argument("--src", type=str, default="nasdaq_100.txt", help="Ticker file")
    parser.add_argument("--top", type=int, default=None, help="Top N results")
    parser.add_argument("--excl-bad", action="store_true", help="Exclude * tickers")
    parser.add_argument("--research", action="store_true", help="Enable Gemini AI research")
    parser.add_argument("--plot", action="store_true", help="Generate N+1 charts in tmp/")
    
    args = parser.parse_args()

    # 1. Load Source
    if not os.path.exists(args.src):
        print(f"❌ Error: {args.src} not found.")
        return

    with open(args.src, 'r') as f:
        tickers = [line.strip() for line in f if line.strip()]

    # 2. Analyze
    all_data = []
    print(f"📡 Lynch Pin v5.7 | Source: {args.src}")
    
    for s in tickers:
        engine = LynchPinEngine(s)
        res = engine.get_ticker_stats()
        if res: all_data.append(res)

    if not all_data:
        print("⚠️ No data processed.")
        return

    df = pd.DataFrame(all_data)
    if args.excl_bad:
        df = df[~df['Ticker'].str.endswith('*')]
    
    df = df.sort_values(by='Dev_SD', ascending=True)
    if args.top:
        df = df.head(args.top)

    # 3. Quantitative Output
    header = (f"{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | "
              f"{'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | "
              f"{'Bull':>8} | {'Base':>8} | {'Bear':>8}")
    print("\n" + header + "\n" + "-" * len(header))
    
    for _, r in df.iterrows():
        print(f"{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | "
              f"{r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | "
              f"{r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | "
              f"{r['Base']:>8} | {r['Bear']:>8}")

    # 4. AI Narrative
    if args.research:
        print("\n🧠 GENERATING X-THREAD NARRATIVE...")
        researcher = LynchPinResearcher()
        print(researcher.get_batch_narrative(df.to_dict('records')))

    # 5. Stylized Visuals
    if args.plot:
        print(f"\n📊 GENERATING DARK-MODE VISUALS IN tmp/...")
        viz = LynchPinVisualizer(output_dir="tmp")
        
        # Comparative Chart with Dynamic ROI Title
        comp_path = viz.plot_comparative_benchmark(df, args.src)
        print(f"  [+] Comparison Chart: {comp_path}")
        
        # Distribution Plots
        for _, row in df.iterrows():
            ticker_path = viz.plot_ticker_distribution(row)
            print(f"  [+] Distribution: {ticker_path}")
            
        print(f"\n✨ Assets ready. Use 'open tmp' to inspect the thread images.")

if __name__ == "__main__":
    main()
