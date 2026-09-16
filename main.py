import argparse
import pandas as pd
import os
import re
import shutil
import time
from datetime import date
from engine.lynch_pin_core import LynchPinEngine
from engine.income_statement_grader import print_grader_table, grade_ticker
from engine.balance_sheet_grader import print_grader_table as print_bs_table, grade_ticker as grade_bs_ticker
from engine.ai_research import LynchPinResearcher
from engine.technical_timing import analyze as analyze_technicals, backtest_edge
from engine.portfolio import parse_portfolio_file, compute_weights, weighted_metrics, format_weighted_summary
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


def _resolve_idx_name(args):
    """Benchmark index symbol for the current run (SPY for weekly/portfolio)."""
    if args.weekly or getattr(args, 'portfolio', None):
        return "SPY"
    src_stem = os.path.basename(args.src).lower().replace('.txt', '')
    return next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")


def _extract_price(info):
    """Current share price from a yfinance info dict (None if unavailable)."""
    for key in ('currentPrice', 'regularMarketPrice', 'previousClose'):
        v = info.get(key) if info else None
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _sort_positions(df, weights):
    """Sorts the result frame by portfolio weight, descending."""
    df = df.copy()
    df['Weight'] = df['Ticker'].str.replace('*', '', regex=False).map(weights).fillna(0.0)
    return df.sort_values(by='Weight', ascending=False)


def _extract_portfolio_narrative(bulk_text):
    """Splits the AI's portfolio-level 'PORTFOLIO:' Bull/Bear block from the per-ticker text.

    Returns ``(portfolio_narrative, remaining_text)``; narrative is '' when absent.
    Cashtags are stripped from the narrative (X allows one per post and the
    header already uses none).
    """
    m = re.search(r'^PORTFOLIO:?\s*\n(.*?)(?=\n\$[A-Z]|\Z)', bulk_text, re.DOTALL | re.MULTILINE)
    if not m:
        return "", bulk_text
    narrative = re.sub(r'\$([A-Z]+)', r'\1', m.group(1).strip())
    remaining = (bulk_text[:m.start()] + bulk_text[m.end():]).strip()
    return narrative, remaining


def _grok_portfolio_question(n_positions):
    """Closing X post: asks Grok for an opinion on the whole portfolio.

    Two lenses on purpose: the thread (quant numbers, weights) and a live
    search of X for FinTwit sentiment on the same tickers — otherwise Grok
    just paraphrases the metrics already in the thread."""
    return (f"@grok Read every post in this thread (portfolio X-ray + {n_positions} positions, "
            f"listed in descending weight), then search recent X posts about each of these tickers.\n\n"
            f"1️⃣ On the numbers in the thread: is this a well-built GARP portfolio? "
            f"Which position is the BEST and which is the WORST right now, and why?\n"
            f"2️⃣ On FinTwit sentiment alone (ignore the numbers above): which of these positions "
            f"does FinTwit love most and hate most right now, and why?\n"
            f"3️⃣ Where do the quant view and the crowd disagree, and what single change would you make?\n\n"
            "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
            "Investing involves risk. Always DYOR. 🫶")


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
    parser.add_argument("--portfolio", type=str, default=None,
                        help="Holdings file ('TICKER, SHARES' per line; repeats are summed). "
                             "Analyzes every position and rolls up weighted metrics. "
                             "Not compatible with --top / --excl-bad / --weekly.")

    args = parser.parse_args()

    if args.portfolio and (args.top or args.excl_bad or args.weekly):
        print("❌ Error: --portfolio cannot be combined with --top, --excl-bad or --weekly "
              "(every position is analyzed, nothing is filtered).")
        return

    positions = None   # ticker -> shares (portfolio mode only)
    weights = {}       # ticker -> market-value weight (portfolio mode only)
    deep = bool(args.top) or bool(args.portfolio)  # full grading/technicals per ticker

    # 1. Load Source
    if args.portfolio:
        try:
            positions = parse_portfolio_file(args.portfolio)
        except (FileNotFoundError, ValueError) as e:
            print(f"❌ Error reading portfolio: {e}")
            return
        tickers = list(positions.keys())
        print(f"💼 PORTFOLIO MODE: {len(tickers)} unique positions loaded from {args.portfolio}")
    elif args.weekly:
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
    prices = {}
    src_label = ('FinTwit AI (weekly)' if args.weekly else args.portfolio if args.portfolio else args.src)
    print(f"📡 Processing Source: {src_label}")

    for s in tickers:
        engine = LynchPinEngine(s)
        px = _extract_price(engine.info)
        if px is not None:
            prices[s] = px
        res = engine.get_ticker_stats()
        if res:
            all_data.append(res)
            engines[s] = engine
        elif args.portfolio:
            print(f"  ⚠️ {s}: no GARP data (negative/absent forward PE or growth) — kept in weights, excluded from metrics")

    if not all_data:
        print("⚠️ No data processed.")
        return

    df = pd.DataFrame(all_data)

    if args.portfolio:
        weights = compute_weights(positions, prices)
        unpriced = [t for t in positions if t not in weights]
        if unpriced:
            print(f"  ⚠️ No price for {', '.join(unpriced)} — dropped from weights")
        if not weights:
            print("⚠️ Could not price any position.")
            return

    # Pre-filter: grade all tickers for --excl-bad quality gate
    if args.excl_bad:
        df = df[~df['Ticker'].str.endswith('*')]
        _BAD_GRADES = {'B-', 'C', 'D'}
        _BAD_RATINGS = {'BB+', 'BB', 'BB-', 'B+', 'B', 'B-', 'CCC+', 'CCC', 'CCC-', 'CC', 'D', 'NR'}
        exclude = set()
        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            if sym not in engines:
                continue
            ig = grade_ticker(engines[sym].ticker)
            if ig and ig['grade'] in _BAD_GRADES:
                exclude.add(row['Ticker'])
            bg = grade_bs_ticker(engines[sym].ticker)
            if bg and bg['rating'] in _BAD_RATINGS:
                exclude.add(row['Ticker'])
        if exclude:
            df = df[~df['Ticker'].isin(exclude)]

    if args.portfolio:
        df = _sort_positions(df, weights)
    else:
        df = df.sort_values(by='Dev_SD', ascending=True)
    if args.top:
        df = df.head(args.top)

    # 3. Quantitative Terminal Output (Yahoo-only growth)
    wcol = f"{'Weight':<6} | " if args.portfolio else ""
    header = (f"{wcol}{'Ticker':<9} | {'PE':<6} | {'FwdPE':<7} | {'2YFwd':<7} | "
              f"{'5YGrowth':<8} | {'PEG':<6} | {'Mean':<6} | {'Dev(SD)':<7} | "
              f"{'Bull':>8} | {'Base':>8} | {'Bear':>8}")

    def _print_table(frame):
        print("\n" + header + "\n" + "-" * len(header))
        for _, r in frame.iterrows():
            w = f"{r['Weight'] * 100:>5.1f}% | " if args.portfolio else ""
            print(f"{w}{r['Ticker']:<9} | {r['PE']:>6.1f} | {r['FwdPE']:>7.1f} | "
                  f"{r['2YFwd']:>7.1f} | {r['5YGrowth']:>8} | {r['PEG']:>6.2f} | "
                  f"{r['Mean']:>6.2f} | {r['Dev_SD']:>7.2f} | {r['Bull']:>8} | "
                  f"{r['Base']:>8} | {r['Bear']:>8}")

    _print_table(df)

    # 3b. Enrich top picks / portfolio positions with multi-source growth (FMP)
    if deep and os.environ.get('FMP_API_KEY'):
        print(f"\n\n🔬 Enriching {len(df)} {'positions' if args.portfolio else 'top picks'} with FMP analyst estimates...")
        enriched_data = []
        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            if sym in engines:
                res = engines[sym].get_ticker_stats(enrich=True)
                if res:
                    enriched_data.append(res)
                else:
                    enriched_data.append(row.to_dict())
            else:
                enriched_data.append(row.to_dict())
        df = pd.DataFrame(enriched_data)
        if args.portfolio:
            df = _sort_positions(df, weights)
        else:
            df = df.sort_values(by='Dev_SD', ascending=True)

        # Reprint with enriched data
        _print_table(df)

    # 3c. Income Statement & Balance Sheet Grading + Technicals (for top picks / all positions)
    grader_data = {}
    bs_data = {}
    tech_data = {}
    edge_data = {}
    portfolio_metrics = None
    portfolio_summary = ""
    excluded_note = ""
    if deep:
        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            if sym in engines:
                g = grade_ticker(engines[sym].ticker)
                if g:
                    grader_data[sym] = g
                b = grade_bs_ticker(engines[sym].ticker)
                if b:
                    bs_data[sym] = b
                t = analyze_technicals(engines[sym].ticker)
                if t:
                    tech_data[sym] = t
        print_grader_table(df.to_dict('records'), engines)
        print_bs_table(df.to_dict('records'), engines)

        # 3d. Technical Timing + 6M Directional Edge
        idx_name = _resolve_idx_name(args)

        print(f"\n\n📉 TECHNICAL TIMING + 6M DIRECTIONAL EDGE")
        print(f"{'─' * 100}")
        print(f"  {'Ticker':<6} {'Signal':<12} {'RSI':>4} {'SMA200':>7} {'ATR':>5} {'Accum Zone':>12} "
              f"{'Bull Acc':>9} {'Bull P&L':>9} {'Bear Acc':>9} {'Bear P&L':>9} {'Edge':>6}")
        print(f"{'─' * 100}")

        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            t = tech_data.get(sym)
            print(f"\r  Running 6M backtest for {sym}...", end="", flush=True)
            e = backtest_edge(sym, idx_name, days=180)
            if e:
                edge_data[sym] = e
            sig = t['signal'] if t else '—'
            rsi = f"{int(t['rsi'])}" if t else '—'
            sma = f"{t['price_vs_sma200']:+.0f}%" if t else '—'
            atr = f"{t['atr_compression']:.2f}" if t else '—'
            zone = t.get('accumulation_zone') if t else None
            zone_str = f"${int(zone[0])}-${int(zone[1])}" if zone else '—'
            if e:
                print(f"\r  {sym:<6} {sig:<12} {rsi:>4} {sma:>7} {atr:>5} {zone_str:>12} "
                      f"{e['bull_acc']:>6.0f}%({e['bull_n']:>2d}) {e['bull_pnl']:>+7.2f}% "
                      f"{e['bear_acc']:>6.0f}%({e['bear_n']:>2d}) {e['bear_pnl']:>+7.2f}% "
                      f"{e['best_edge']:>6}")
            else:
                print(f"\r  {sym:<6} {sig:<12} {rsi:>4} {sma:>7} {atr:>5} {zone_str:>12} "
                      f"{'—':>9} {'—':>9} {'—':>9} {'—':>9} {'—':>6}")

        print(f"{'─' * 100}")
        print(f"  💡 BULL edge → sell cash-secured puts on dips | BEAR edge → sell covered calls on bounces")

    # 3e. Portfolio roll-up: weighted valuation, growth, ROI and quality
    if args.portfolio:
        portfolio_metrics = weighted_metrics(df.to_dict('records'), weights, grader_data, bs_data)
        portfolio_summary = format_weighted_summary(portfolio_metrics)
        covered = set(df['Ticker'].str.replace('*', '', regex=False))
        excluded = [(t, w) for t, w in weights.items() if t not in covered]
        if excluded:
            portfolio_summary += ("\n  Excluded (no GARP data — negative/absent forward PE or growth): "
                                  + ", ".join(f"{t} ({w * 100:.1f}%)" for t, w in excluded))
            # Positions that stay in the weights/pie but get no reply of their own
            excluded_note = " (" + ", ".join(f"{t} {w * 100:.0f}%" for t, w in excluded) + \
                            ": no forward earnings, weight only)"
        print(f"\n\n💼 PORTFOLIO WEIGHTED METRICS (weights = shares × price / total value)")
        print(f"{'─' * 100}")
        print(portfolio_summary)
        print(f"{'─' * 100}")

    # 4. AI Narrative (Batch) & Visuals
    researcher = LynchPinResearcher() if args.research or args.post or args.post_threads else None
    bulk_ai_text = ""
    sentiment_text = ""
    portfolio_narrative = ""

    if researcher:
        print("\n🧠 GENERATING AI NARRATIVE...")
        idx_name = _resolve_idx_name(args)

        raw_ai = researcher.get_batch_narrative(df.to_dict('records'), grader_data, idx_name, bs_data,
                                                tech_data, edge_data,
                                                portfolio_summary=portfolio_summary or None)

        # Parse sentiment from response
        sent_match = re.search(r'SENTIMENT:\s*(.+)', raw_ai)
        if sent_match:
            sentiment_text = sent_match.group(1).strip()
            # Strip leading "SENTIMENT:" if AI doubled it, and remove cashtags
            sentiment_text = re.sub(r'^SENTIMENT:\s*', '', sentiment_text)
            sentiment_text = re.sub(r'\$([A-Z]+)', r'\1', sentiment_text)
            # Remove sentiment line from bulk text
            bulk_ai_text = raw_ai[sent_match.end():].strip()
        else:
            bulk_ai_text = raw_ai
        # Strip section headers AI sometimes includes
        bulk_ai_text = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk_ai_text)

        portfolio_narrative = ""
        if args.portfolio:
            portfolio_narrative, bulk_ai_text = _extract_portfolio_narrative(bulk_ai_text)

        print(f"📰 Sentiment: {sentiment_text}")
        if portfolio_narrative:
            print("-" * 30 + "\n" + portfolio_narrative)
        print("-" * 30 + "\n" + bulk_ai_text + "\n" + "-" * 30)

    if args.plot or args.post or args.post_threads:
        print(f"\n📊 GENERATING DARK-MODE VISUALS IN tmp/...")
        viz = LynchPinVisualizer(output_dir="tmp")
        if args.portfolio:
            # Weight donut + weighted stats replaces the index benchmark bar chart
            comparison_img = viz.plot_portfolio(weights, portfolio_metrics)
        else:
            comparison_img = viz.plot_comparative_benchmark(df, "spy" if args.weekly else args.src)

        for _, row in df.iterrows():
            sym = row['Ticker'].replace('*', '')
            viz.plot_ticker_distribution(row, grader_data.get(sym), bs_data.get(sym), tech_data.get(sym), edge_data.get(sym))

    # 5. X (Twitter) Posting Support
    if args.post:
        print("\n🐦 PREPARING X THREAD...")
        x_client = XPublisher()

        idx_name = _resolve_idx_name(args)

        # Main tweet with sentiment + all tickers
        idx_display = IDX_DISPLAY.get(idx_name, idx_name)
        if args.portfolio:
            main_tweet = f"💼 WEEKLY PORTFOLIO UPDATE: #LynchPin X-Ray\n\n"
        elif args.weekly:
            main_tweet = f"🔥 WEEKLY SPECIAL: Top deals among 💯 most discussed stocks on #FinTwit this week..👀\n\n#LynchPin Detector\n\n"
        else:
            main_tweet = f"🚨 MARKET CLOSE: ${idx_name} #LynchPin Detector\n\n"
        if sentiment_text:
            # Strip cashtags from AI sentiment to avoid X's one-cashtag limit
            sent_clean = sentiment_text.replace(f'${idx_name}', idx_display).replace('$', '')
            main_tweet += f"🤖: {sent_clean}\n\n"
        if args.portfolio:
            # Weights live in the pie chart; the post carries the AI's portfolio-level thesis
            main_tweet += (portfolio_narrative or "Portfolio bull/bear thesis unavailable.") + \
                          f"\n\n👇 {len(weights)} positions, largest first{excluded_note}:"
        else:
            main_tweet += f"Top {len(df)} GARP deals + ROI Projections:\n\n"

        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        ticker_sub_tweets = []

        for i, (_, r) in enumerate(df.iterrows()):
            clean_t = r['Ticker'].replace('*', '')
            emoji = num_emojis[i] if i < 10 else f"{i+1}."
            if not args.portfolio:
                main_tweet += f"{emoji} {clean_t}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"

            # Extract full AI narrative for this ticker.
            # Anchored to a line-start "$TICKER" header (case-sensitive) so short
            # tickers that are English words (e.g. ON) can't match prose inside
            # another ticker's narrative.
            pattern = rf"^\${re.escape(clean_t)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
            match = re.search(pattern, bulk_ai_text, re.DOTALL | re.MULTILINE)
            raw_narrative = match.group(1).strip() if match else "Valuation disconnect detected via quantitative analysis."
            # Update section labels for tweet
            raw_narrative = raw_narrative.replace('📊 Reverse DCF:', '📊 Reverse 5Y DCF:')
            raw_narrative = raw_narrative.replace('🧪 Stomach Test:', '🐻 "Stomach Test" (why it can underperform in the next 5 years):')

            formatted_reply = f"${clean_t}\n\n{raw_narrative}"
            if args.portfolio:
                formatted_reply = f"${clean_t} — {r['Weight'] * 100:.1f}% of portfolio\n\n{raw_narrative}"

            ticker_sub_tweets.append({
                "ticker": clean_t,
                "text": formatted_reply,
                "image": f"tmp/{clean_t}_valuation.png"
            })

        # Footer with @grok callout
        if args.portfolio:
            disclaimer = _grok_portfolio_question(len(weights))
        else:
            if args.weekly:
                universe_label = "💯 most discussed stocks on FinTwit this week"
                grok_ref = "#FinTwit"
            else:
                universe_label = idx_display
                grok_ref = f"${idx_name}"
            disclaimer = (f"In this market, you'll miss the best compounders waiting for a perfect 1.0 PEG."
                          f" Which of these {universe_label} anomalies are the hardest for your stomach? 👇\n\n"
                          f"@grok Search recent X posts about the {grok_ref} tickers in this thread. "
                          f"Based on FinTwit sentiment alone (not the numbers above), which one does "
                          f"FinTwit love most and hate most right now, and why?\n\n"
                          "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
                          "Investing involves risk. Always DYOR. 🫶")

        x_client.post_thread(
            main_tweet=main_tweet,
            sub_tweets=ticker_sub_tweets,
            comparison_img=comparison_img,
            disclaimer=disclaimer
        )

    # 6. Threads Posting Support
    if args.post_threads:
        print("\n🧵 PREPARING THREADS POST...")
        threads_client = ThreadsPublisher()

        idx_name = _resolve_idx_name(args)

        idx_display = IDX_DISPLAY.get(idx_name, idx_name)

        # Topic tag: ticker for daily, FinTwit for weekly, Portfolio for holdings
        topic_tag = "Portfolio" if args.portfolio else "FinTwit" if args.weekly else idx_name

        # Copy images to images/, remove old and push to GitHub for public URLs
        img_dir = "images"
        os.makedirs(img_dir, exist_ok=True)
        os.system(f"rm -f {img_dir}/*")

        if args.portfolio:
            bench_src = "tmp/portfolio_allocation.png"
            bench_name = "portfolio_allocation.png"
        else:
            bench_src = "tmp/benchmark_comparison.png"
            bench_name = f"{idx_name.lower()}_benchmark.png"
        bench_dest = f"{img_dir}/{bench_name}"
        if os.path.exists(bench_src):
            shutil.copy2(bench_src, bench_dest)

        for _, r in df.iterrows():
            sym = r['Ticker'].replace('*', '')
            src_img = f"tmp/{sym}_valuation.png"
            if os.path.exists(src_img):
                shutil.copy2(src_img, f"{img_dir}/{sym}_valuation.png")

        # Git commit and push images
        print("  [git] Pushing images to GitHub...")
        commit_msg = f"{idx_name}: update chart images on {date.today()} scan"
        os.system(f'cd {os.getcwd()} && git add -A {img_dir}/ && git commit -s -m "{commit_msg}" --quiet && git push --quiet')
        time.sleep(5)  # Give GitHub CDN a moment to propagate

        GITHUB_RAW = os.environ.get("GITHUB_IMAGE_PATH", "https://raw.githubusercontent.com/duman190/the-lynch-pin/main/images")

        # Main post (no cashtags)
        if args.portfolio:
            threads_main = f"💼 WEEKLY PORTFOLIO UPDATE: LynchPin X-Ray\n\n"
        elif args.weekly:
            threads_main = f"🔥 WEEKLY SPECIAL: Top deals among 💯 most discussed stocks on FinTwit this week..👀\n\nLynchPin Detector\n\n"
        else:
            threads_main = f"🚨 MARKET CLOSE: {idx_name} LynchPin Detector\n\n"
        if sentiment_text:
            sent_clean = sentiment_text.replace(f'${idx_name}', idx_display).replace('$', '')
            threads_main += f"🤖: {sent_clean}\n\n"
        if args.portfolio:
            threads_main += (portfolio_narrative or "Portfolio bull/bear thesis unavailable.") + \
                            f"\n\n👇 {len(weights)} positions, largest first{excluded_note}:"
        else:
            threads_main += f"Top {len(df)} GARP deals + ROI Projections:\n\n"

        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        threads_sub = []

        for i, (_, r) in enumerate(df.iterrows()):
            clean_t = r['Ticker'].replace('*', '')
            emoji = num_emojis[i] if i < 10 else f"{i+1}."
            if not args.portfolio:
                threads_main += f"{emoji} {clean_t}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"

            pattern = rf"^\${re.escape(clean_t)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
            match = re.search(pattern, bulk_ai_text, re.DOTALL | re.MULTILINE)
            raw_narrative = match.group(1).strip() if match else "Valuation disconnect detected via quantitative analysis."
            raw_narrative = raw_narrative.replace('📊 Reverse DCF:', '\n📊:')
            raw_narrative = raw_narrative.replace('🧪 Stomach Test:', '\n🐻 "Stomach Test" (why it can underperform in the next 5 years):')
            # Remove cashtags
            formatted_reply = re.sub(r'\$([A-Z]+)', r'\1', raw_narrative)
            if args.portfolio:
                formatted_reply = f"{clean_t} — {r['Weight'] * 100:.1f}% of portfolio\n\n{formatted_reply}"

            threads_sub.append({
                "ticker": clean_t,
                "text": formatted_reply,
                "topic_tag": clean_t,
                "image_url": f"{GITHUB_RAW}/{clean_t}_valuation.png",
            })

        # Footer (no @grok sentence)
        if args.portfolio:
            threads_disclaimer = (
                f"That's the whole book, {len(weights)} positions in descending weight. "
                f"Which one would you trim first, and which would you add to? 👇\n\n"
                "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
                "Investing involves risk. Always DYOR. 🫶"
            )
        else:
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
            comparison_img_url=f"{GITHUB_RAW}/{bench_name}",
            disclaimer=threads_disclaimer,
            topic_tag=topic_tag,
        )

    print(f"\n✨ Done. Assets available in tmp/")


if __name__ == "__main__":
    main()
