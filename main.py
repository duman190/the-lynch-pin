import argparse
import pandas as pd
import os
from engine.lynch_pin_core import LynchPinEngine
from engine.ai_research import LynchPinResearcher
from graphics.visualizer import LynchPinVisualizer
from social.x_publisher import XPublisher

def main():
    parser = argparse.ArgumentParser(description="Lynch Pin v5.7 - GARP Analysis with AI")
    parser.add_argument("--src", type=str, default="nasdaq_100.txt", help="Ticker file")
    parser.add_argument("--top", type=int, default=None, help="Top N results")
    parser.add_argument("--excl-bad", action="store_true", help="Exclude * tickers")
    parser.add_argument("--research", action="store_true", help="Enable Gemini AI research")
    parser.add_argument("--plot", action="store_true", help="Generate N+1 charts in tmp/")
    parser.add_argument("--post", action="store_true", help="Publish full thread to X")
    
    args = parser.parse_args()

    # 1. Load Source
    if not os.path.exists(args.src):
        print(f"❌ Error: {args.src} not found.")
        return

    with open(args.src, 'r') as f:
        tickers = [line.strip() for line in f if line.strip()]

    # 2. Analyze
    all_data = []
    print(f"📡 Processing Source: {args.src}")
    
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

    # 3. Quantitative Terminal Output
    header = (f"{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | "
              f"{'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | "
              f"{'Bull':>8} | {'Base':>8} | {'Bear':>8}")
    print("\n" + header + "\n" + "-" * len(header))
    
    for _, r in df.iterrows():
        print(f"{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | "
              f"{r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | "
              f"{r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | "
              f"{r['Base']:>8} | {r['Bear']:>8}")

    # 4. AI Narrative & Visuals
    researcher = LynchPinResearcher() if args.research or args.post else None
    
    if args.plot or args.post:
        print(f"\n📊 GENERATING DARK-MODE VISUALS IN tmp/...")
        viz = LynchPinVisualizer(output_dir="tmp")
        comp_path = viz.plot_comparative_benchmark(df, args.src)
        print(f"  [+] Comparison Chart: {comp_path}")
        
        for _, row in df.iterrows():
            ticker_path = viz.plot_ticker_distribution(row)
            print(f"  [+] Distribution: {ticker_path}")

    # 5. X (Twitter) Posting Support
    if args.post:
        print("\n🐦 PREPARING X THREAD...")
        x_client = XPublisher()
        
        # Determine Index Ticker
        src_lower = args.src.lower()
        if "mag7" in src_lower:
            idx_name = "MAGS"
        elif "nasdaq" in src_lower:
            idx_name = "QQQ"
        else:
            idx_name = "SPY"

        # BUILD MAIN TWEET (Template: MARKET CLOSE: $SYMBOL...)
        main_tweet = f"🚨 MARKET CLOSE: ${idx_name} PEG Deal Detector is Live.\n"
        main_tweet += f"We scanned all index holdings, and here are the top {len(df)} deals + ROI Projections:\n\n"
        
        ticker_sub_tweets = []
        
        for i, (_, r) in enumerate(df.iterrows(), 1):
            clean_t = r['Ticker'].replace('*', '')
            
            # NOTE: We do NOT use the '$' here to stay within X's 1-cashtag limit for the main post
            main_tweet += (f"{i}) {clean_t} | PEG: {r['PEG']:.2f} ({r['Dev_SD']:.2f} SD)\n"
                           f"PE: {r['PE']:.0f} | FwdPE: {r['FwdPE']:.0f} | 2YFwdPE: {r['2YFwd']:.0f}\n"
                           f"ROI: Bull {r['Bull']} | Base {r['Base']} | Bear {r['Bear']}\n\n")
            
            print(f"  [AI] Analyzing {clean_t}...")
            ticker_narrative = researcher.get_batch_narrative([r.to_dict()])
            
            ticker_sub_tweets.append({
                "ticker": clean_t,
                "text": ticker_narrative,
                "image": f"tmp/{clean_t}_valuation.png"
            })

        disclaimer = ("⚠️ DISCLAIMER & RISK\nOur reports are quantitative scans, "
                      "not financial advice or a recommendation to buy/sell: "
                      "- Math can be mistaken. - Data sources vary. "
                      "Investing involves risk. Always perform your own research.")

        # Fire the thread
        x_client.post_thread(
            main_tweet=main_tweet,
            sub_tweets=ticker_sub_tweets,
            comparison_img="tmp/benchmark_comparison.png",
            disclaimer=disclaimer
        )

    print(f"\n✨ Done. Assets available in tmp/")

if __name__ == "__main__":
    main()
