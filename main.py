import argparse
import pandas as pd
import os
import re
from engine.lynch_pin_core import LynchPinEngine
from engine.income_statement_grader import print_grader_table, grade_ticker
from engine.ai_research import LynchPinResearcher
from graphics.visualizer import LynchPinVisualizer
from social.x_publisher import XPublisher

IDX_MAP = {
    "mag7": "MAGS", "mags": "MAGS",
    "nasdaq": "QQQ", "qqq": "QQQ",
    "schd": "SCHD", "smh": "SMH", "igv": "IGV",
}


def main():
    parser = argparse.ArgumentParser(description="Lynch Pin v6.0 - GARP Analysis with AI")
    parser.add_argument("--src", type=str, default="database/mag7.txt", help="Ticker file")
    parser.add_argument("--top", type=int, default=None, help="Number of stocks to analyze")
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
    engines = {}
    print(f"📡 Processing Source: {args.src}")

    for s in tickers:
        engine = LynchPinEngine(s)
        res = engine.get_ticker_stats()
        if res:
            all_data.append(res)
            engines[s] = engine

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

    # 3b. Income Statement Grading (for top picks)
    grader_data = {}
    if args.top:
        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            if sym in engines:
                g = grade_ticker(engines[sym].ticker)
                if g:
                    grader_data[sym] = g
        print_grader_table(df.to_dict('records'), engines)

    # 4. AI Narrative (Batch) & Visuals
    researcher = LynchPinResearcher() if args.research or args.post else None
    bulk_ai_text = ""

    if researcher:
        print("\n🧠 GENERATING BATCH AI NARRATIVE...")
        bulk_ai_text = researcher.get_batch_narrative(df.to_dict('records'), grader_data)
        print("-" * 30 + "\n" + bulk_ai_text + "\n" + "-" * 30)

    if args.plot or args.post:
        print(f"\n📊 GENERATING DARK-MODE VISUALS IN tmp/...")
        viz = LynchPinVisualizer(output_dir="tmp")
        viz.plot_comparative_benchmark(df, args.src)

        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            viz.plot_ticker_distribution(row, grader_data.get(sym))

    # 5. X (Twitter) Posting Support
    if args.post:
        print("\n🐦 PREPARING X THREAD...")
        x_client = XPublisher()

        src_stem = os.path.basename(args.src).lower().replace('.txt', '')
        idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")

        main_tweet = f"🚨 MARKET CLOSE: ${idx_name} #LynchPin Detector\n"
        main_tweet += f"Top {len(df)} GARP deals + ROI Projections:\n\n"

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        ticker_sub_tweets = []

        for i, (_, r) in enumerate(df.iterrows()):
            clean_t = r['Ticker'].replace('*', '')

            if i < 5:
                main_tweet += f"{emojis[i]} {clean_t}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"
            elif i == 5:
                main_tweet += "...\n"

            # Extract AI narrative for this ticker
            pattern = rf"\$?\b{clean_t}\b:\s*(.*?)(?=\n\n|\Z)"
            match = re.search(pattern, bulk_ai_text, re.DOTALL | re.IGNORECASE)
            raw_narrative = match.group(1).strip() if match else "Valuation disconnect detected via quantitative analysis."

            formatted_reply = f"${clean_t}\n\n🤖: {raw_narrative}"

            ticker_sub_tweets.append({
                "ticker": clean_t,
                "text": formatted_reply,
                "image": f"tmp/{clean_t}_valuation.png"
            })
        
        # f-string to inject the index name correctly
        disclaimer = (f"In this market, you'll miss the best compounders waiting for a perfect 1.0 PEG."
                      f" Which of these ${idx_name} anomalies are the hardest for your stomach? 👇\n\n"
                      "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
                      "Investing involves risk. Always DYOR. 🫶")

        x_client.post_thread(
            main_tweet=main_tweet,
            sub_tweets=ticker_sub_tweets,
            comparison_img="tmp/benchmark_comparison.png",
            disclaimer=disclaimer
        )

    print(f"\n✨ Done. Assets available in tmp/")


if __name__ == "__main__":
    main()
