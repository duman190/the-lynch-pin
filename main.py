import argparse
import pandas as pd
import os
import re
from engine.lynch_pin_core import LynchPinEngine
from engine.income_statement_grader import print_grader_table, grade_ticker
from engine.balance_sheet_grader import print_grader_table as print_bs_table, grade_ticker as grade_bs_ticker
from engine.ai_research import LynchPinResearcher
from graphics.visualizer import LynchPinVisualizer
from social.x_publisher import XPublisher
from social.threads_publisher import ThreadsPublisher

IDX_MAP = {
    "mag7": "MAGS", "mags": "MAGS",
    "nasdaq": "QQQ", "qqq": "QQQ",
    "schd": "SCHD", "smh": "SMH", "igv": "IGV",
}

IDX_DISPLAY = {
    "MAGS": "Magnificent 7",
    "QQQ": "Nasdaq 100",
    "SCHD": "Dow Jones Dividend 100",
    "SMH": "Semiconductor sector",
    "IGV": "Software sector",
    "SPY": "S&P 500",
}


def main():
    parser = argparse.ArgumentParser(description="Lynch Pin v6.0 - GARP Analysis with AI")
    parser.add_argument("--src", type=str, default="database/mag7.txt", help="Ticker file")
    parser.add_argument("--top", type=int, default=None, help="Number of stocks to analyze")
    parser.add_argument("--excl-bad", action="store_true", help="Exclude * tickers")
    parser.add_argument("--research", action="store_true", help="Enable Gemini AI research")
    parser.add_argument("--plot", action="store_true", help="Generate N+1 charts in tmp/")
    parser.add_argument("--post", action="store_true", help="Publish full thread to X")
    parser.add_argument("--post_threads", action="store_true", help="Publish full thread to Threads")
    parser.add_argument("--weekly", action="store_true", help="Weekly scan: AI-sourced FinTwit trending tickers")

    args = parser.parse_args()

    # 1. Load Source
    if args.weekly:
        print("🔥 WEEKLY SCAN: Fetching top 💯 FinTwit trending tickers...")
        researcher_init = LynchPinResearcher()
        tickers = researcher_init.get_fintwit_trending()
        if not tickers:
            print("⚠️ AI returned no tickers, falling back to database/fintwit_100.txt")
            with open("database/fintwit_100.txt", 'r') as f:
                tickers = [line.strip() for line in f if line.strip()]
        print(f"   Got {len(tickers)} tickers")
    else:
        if not os.path.exists(args.src):
            print(f"❌ Error: {args.src} not found.")
            return
        with open(args.src, 'r') as f:
            tickers = [line.strip() for line in f if line.strip()]

    # 2. Analyze
    all_data = []
    engines = {}
    print(f"📡 Processing Source: {'FinTwit AI (weekly)' if args.weekly else args.src}")

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

    # 3b. Income Statement & Balance Sheet Grading (for top picks)
    grader_data = {}
    bs_data = {}
    if args.top:
        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            if sym in engines:
                g = grade_ticker(engines[sym].ticker)
                if g:
                    grader_data[sym] = g
                b = grade_bs_ticker(engines[sym].ticker)
                if b:
                    bs_data[sym] = b
        print_grader_table(df.to_dict('records'), engines)
        print_bs_table(df.to_dict('records'), engines)

    # 4. AI Narrative (Batch) & Visuals
    researcher = LynchPinResearcher() if args.research or args.post or args.post_threads else None
    bulk_ai_text = ""
    sentiment_text = ""

    if researcher:
        print("\n🧠 GENERATING AI NARRATIVE...")
        if args.weekly:
            idx_name = "SPY"
        else:
            src_stem = os.path.basename(args.src).lower().replace('.txt', '')
            idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")

        raw_ai = researcher.get_batch_narrative(df.to_dict('records'), grader_data, idx_name, bs_data)

        # Parse sentiment from response
        sent_match = re.search(r'SENTIMENT:\s*(.+)', raw_ai)
        if sent_match:
            sentiment_text = sent_match.group(1).strip()
            # Remove sentiment line from bulk text
            bulk_ai_text = raw_ai[sent_match.end():].strip()
        else:
            bulk_ai_text = raw_ai

        print(f"📰 Sentiment: {sentiment_text}")
        print("-" * 30 + "\n" + bulk_ai_text + "\n" + "-" * 30)

    if args.plot or args.post or args.post_threads:
        print(f"\n📊 GENERATING DARK-MODE VISUALS IN tmp/...")
        viz = LynchPinVisualizer(output_dir="tmp")
        viz.plot_comparative_benchmark(df, "spy" if args.weekly else args.src)

        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            viz.plot_ticker_distribution(row, grader_data.get(sym), bs_data.get(sym))

    # 5. X (Twitter) Posting Support
    if args.post:
        print("\n🐦 PREPARING X THREAD...")
        x_client = XPublisher()

        if args.weekly:
            idx_name = "SPY"
        else:
            src_stem = os.path.basename(args.src).lower().replace('.txt', '')
            idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")

        # Main tweet with sentiment + all tickers
        idx_display = IDX_DISPLAY.get(idx_name, idx_name)
        if args.weekly:
            main_tweet = f"🔥 WEEKLY SPECIAL: Top deals among 💯 most discussed stocks on #FinTwit this week..👀\n\n#LynchPin Detector\n\n"
        else:
            main_tweet = f"🚨 MARKET CLOSE: ${idx_name} #LynchPin Detector\n\n"
        if sentiment_text:
            # Strip cashtags from AI sentiment to avoid X's one-cashtag limit
            sent_clean = sentiment_text.replace(f'${idx_name}', idx_display).replace('$', '')
            main_tweet += f"🤖: {sent_clean}\n\n"
        main_tweet += f"Top {len(df)} GARP deals + ROI Projections:\n\n"

        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        ticker_sub_tweets = []

        for i, (_, r) in enumerate(df.iterrows()):
            clean_t = r['Ticker'].replace('*', '')
            emoji = num_emojis[i] if i < 10 else f"{i+1}."
            main_tweet += f"{emoji} {clean_t}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"

            # Extract full AI narrative for this ticker
            pattern = rf"\$?\b{clean_t}\b:\s*(.*?)(?=\n\$|\Z)"
            match = re.search(pattern, bulk_ai_text, re.DOTALL | re.IGNORECASE)
            raw_narrative = match.group(1).strip() if match else "Valuation disconnect detected via quantitative analysis."
            # Update section labels for tweet
            raw_narrative = raw_narrative.replace('📊 Reverse DCF:', '📊 Reverse 5Y DCF:')
            raw_narrative = raw_narrative.replace('🧪 Stomach Test:', '🐻 "Stomach Test" (why it can underperform in the next 5 years):')

            formatted_reply = f"${clean_t}\n\n{raw_narrative}"

            ticker_sub_tweets.append({
                "ticker": clean_t,
                "text": formatted_reply,
                "image": f"tmp/{clean_t}_valuation.png"
            })

        # Footer with @grok callout
        if args.weekly:
            universe_label = "💯 most discussed stocks on FinTwit this week"
            grok_ref = "#FinTwit"
        else:
            universe_label = idx_display
            grok_ref = f"${idx_name}"
        disclaimer = (f"In this market, you'll miss the best compounders waiting for a perfect 1.0 PEG."
                      f" Which of these {universe_label} anomalies are the hardest for your stomach? 👇\n\n"
                      f"@grok What's the best and worst among above {grok_ref} deals and why?\n\n"
                      "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
                      "Investing involves risk. Always DYOR. 🫶")

        x_client.post_thread(
            main_tweet=main_tweet,
            sub_tweets=ticker_sub_tweets,
            comparison_img="tmp/benchmark_comparison.png",
            disclaimer=disclaimer
        )

    # 6. Threads Posting Support
    if args.post_threads:
        print("\n🧵 PREPARING THREADS POST...")
        threads_client = ThreadsPublisher()

        if args.weekly:
            idx_name = "SPY"
        else:
            src_stem = os.path.basename(args.src).lower().replace('.txt', '')
            idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")

        idx_display = IDX_DISPLAY.get(idx_name, idx_name)

        # Topic tag: ticker for daily, FinTwit for weekly
        topic_tag = "FinTwit" if args.weekly else idx_name

        # Main post (no cashtags)
        if args.weekly:
            threads_main = f"🔥 WEEKLY SPECIAL: Top deals among 💯 most discussed stocks on FinTwit this week..👀\n\nLynchPin Detector\n\n"
        else:
            threads_main = f"🚨 MARKET CLOSE: {idx_name} LynchPin Detector\n\n"
        if sentiment_text:
            sent_clean = sentiment_text.replace(f'${idx_name}', idx_display).replace('$', '')
            threads_main += f"🤖: {sent_clean}\n\n"
        threads_main += f"Top {len(df)} GARP deals + ROI Projections:\n\n"

        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        threads_sub = []

        for i, (_, r) in enumerate(df.iterrows()):
            clean_t = r['Ticker'].replace('*', '')
            emoji = num_emojis[i] if i < 10 else f"{i+1}."
            threads_main += f"{emoji} {clean_t}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"

            pattern = rf"\$?\b{clean_t}\b:\s*(.*?)(?=\n\$|\Z)"
            match = re.search(pattern, bulk_ai_text, re.DOTALL | re.IGNORECASE)
            raw_narrative = match.group(1).strip() if match else "Valuation disconnect detected via quantitative analysis."
            raw_narrative = raw_narrative.replace('📊 Reverse DCF:', '\n📊:')
            raw_narrative = raw_narrative.replace('🧪 Stomach Test:', '\n🐻 "Stomach Test" (why it can underperform in the next 5 years):')
            # Remove cashtags
            formatted_reply = re.sub(r'\$([A-Z]+)', r'\1', raw_narrative)

            threads_sub.append({
                "ticker": clean_t,
                "text": formatted_reply,
                "image_url": None,  # Threads requires public CDN URLs, not local files
            })

        # Footer (no @grok sentence)
        if args.weekly:
            universe_label = "💯 most discussed stocks on FinTwit this week"
        else:
            universe_label = idx_display
        threads_disclaimer = (
            f"In this market, you'll miss the best compounders waiting for a perfect 1.0 PEG."
            f" Which of these {universe_label} anomalies are the hardest for your stomach? 👇\n\n"
            "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
            "Investing involves risk. Always DYOR. 🫶"
        )

        threads_client.post_thread(
            main_tweet=threads_main,
            sub_tweets=threads_sub,
            comparison_img_url=None,  # Would need public CDN URL
            disclaimer=threads_disclaimer,
            topic_tag=topic_tag,
        )

    print(f"\n✨ Done. Assets available in tmp/")


if __name__ == "__main__":
    main()
