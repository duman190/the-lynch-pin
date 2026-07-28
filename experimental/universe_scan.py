"""Universe Scanner — find which tickers have real bull/bear edge.

Runs a quick backtest across all tickers in a universe file and ranks
by directional P&L to find the best candidates for the trade assistant.

Usage:
    python -m experimental.universe_scan --src database/smh.txt
    python -m experimental.universe_scan --all --top 5
"""

import argparse
import os
import sys
import warnings
import pandas as pd
import yfinance as yf
from datetime import timedelta

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

from experimental.back_test import backtest


def load_tickers(src_paths):
    """Load tickers from one or more database files."""
    tickers = []
    for path in src_paths:
        if not os.path.exists(path):
            print(f"⚠️  {path} not found, skipping")
            continue
        with open(path) as f:
            for line in f:
                sym = line.strip()
                if sym and sym not in tickers:
                    tickers.append(sym)
    return tickers


def scan_universe(tickers, index_symbol, days=180, horizon=5):
    """Run backtest for each ticker and collect results."""
    results = []
    total = len(tickers)

    for i, sym in enumerate(tickers):
        sys.stdout.write(f"\r  [{i+1}/{total}] {sym:<6}")
        sys.stdout.flush()
        try:
            bt = backtest(sym, index_symbol, days=days, horizon=horizon)
            if "error" in bt:
                continue

            bull = bt["breakdown"].get("BULLISH", {})
            bear = bt["breakdown"].get("BEARISH", {})

            # Fundamental filter: determine which signals to trust
            fund_bias = _get_fundamental_filter(sym)

            results.append({
                "ticker": sym,
                "total_days": bt["test_days"],
                "bull_count": bull.get("count", 0),
                "bull_acc": bull.get("accuracy", 0),
                "bull_pnl": bull.get("avg_dir_pnl", 0),
                "bear_count": bear.get("count", 0),
                "bear_acc": bear.get("accuracy", 0),
                "bear_pnl": bear.get("avg_dir_pnl", 0),
                "fund_bias": fund_bias,
            })
        except Exception:
            continue

    sys.stdout.write(f"\r  [{total}/{total}] Done.{' '*20}\n")
    return pd.DataFrame(results)


def _get_fundamental_filter(symbol):
    """Quick fundamental check to determine which direction to trust.

    Rules (from fundamental_overlay backtest, +11.4% avg edge):
      - Dev_SD < 0 AND Grade >= B AND Rating >= BBB → BULL only
      - Dev_SD > 0.5 OR Grade <= C → BEAR only
      - Otherwise → BOTH (no filter)

    Returns: "BULL", "BEAR", or "BOTH"
    """
    try:
        from engine.lynch_pin_core import LynchPinEngine
        from engine.income_statement_grader import grade_ticker as grade_income
        from engine.balance_sheet_grader import grade_ticker as grade_balance

        engine = LynchPinEngine(symbol)
        stats = engine.get_ticker_stats()
        if not stats:
            return "BOTH"

        dev_sd = stats["Dev_SD"]

        ig = grade_income(engine.ticker)
        income_grade = ig["grade"] if ig else "N/A"

        bg = grade_balance(engine.ticker)
        credit_rating = bg["rating"] if bg else "NR"

        good_grades = {'A++', 'A+', 'A', 'A-', 'B+', 'B'}
        bad_grades = {'C', 'D', 'N/A'}
        bad_ratings = {'BB+', 'BB', 'BB-', 'B+', 'B', 'B-', 'CCC+', 'CCC', 'CC', 'D', 'NR'}

        # Bull only: cheap + good fundamentals
        if dev_sd < 0 and income_grade in good_grades and credit_rating not in bad_ratings:
            return "BULL"

        # Bear only: expensive or poor fundamentals
        if dev_sd > 0.5 or income_grade in bad_grades:
            return "BEAR"

        return "BOTH"
    except Exception:
        return "BOTH"


def main():
    parser = argparse.ArgumentParser(description="Scan universe for bull/bear edge")
    parser.add_argument("--src", action="append", default=None, help="Ticker file(s)")
    parser.add_argument("--all", action="store_true", help="Scan all database files")
    parser.add_argument("--index", default="SPY", help="Reference index (default: SPY)")
    parser.add_argument("--days", type=int, default=180, help="Backtest days (default: 180)")
    parser.add_argument("--horizon", type=int, default=5, help="Forward horizon (default: 5)")
    parser.add_argument("--top", type=int, default=5, help="Top N bull + N bear per universe (default: 5)")
    parser.add_argument("--list", type=int, nargs="?", const=10, default=None,
                        help="Only show top N ranked table (default: 10, skip detailed cards)")
    args = parser.parse_args()

    # Resolve source files
    if args.all:
        db_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
        src_files = sorted([os.path.join(db_dir, f) for f in os.listdir(db_dir) if f.endswith('.txt')])
        print(f"\n📂 --all mode: {len(src_files)} database files")
    elif args.src:
        src_files = args.src
    else:
        print("❌ Provide --src or --all")
        return

    # Index mapping per source file
    IDX_MAP = {
        "mag7": "QQQ", "mags": "QQQ", "nasdaq": "QQQ",
        "schd": "SCHD", "smh": "SMH", "igv": "IGV", "fintwit": "SPY",
    }

    # Phase 1: Collect top picks from each universe
    all_bull = []
    all_bear = []

    for src_path in src_files:
        src_name = os.path.basename(src_path).replace('.txt', '')
        idx = next((v for k, v in IDX_MAP.items() if k in src_name.lower()), args.index)
        tickers = load_tickers([src_path])
        if not tickers:
            continue

        print(f"\n{'─' * 50}")
        print(f"📡 {src_name.upper()} ({len(tickers)} tickers) vs {idx}")

        df = scan_universe(tickers, idx, days=args.days, horizon=args.horizon)
        if df.empty:
            continue

        # Top N bull from this universe (only if fundamentals allow bull)
        if "fund_bias" in df.columns:
            bull_eligible = df[(df["bull_count"] >= 5) & (df["bull_acc"] > 50) & (df["fund_bias"].isin(["BULL", "BOTH"]))]
            bear_eligible = df[(df["bear_count"] >= 5) & (df["bear_acc"] > 50) & (df["fund_bias"].isin(["BEAR", "BOTH"]))]
        else:
            bull_eligible = df[(df["bull_count"] >= 5) & (df["bull_acc"] > 50)]
            bear_eligible = df[(df["bear_count"] >= 5) & (df["bear_acc"] > 50)]

        bull_df = bull_eligible.sort_values("bull_pnl", ascending=False).head(args.top)
        for _, row in bull_df.iterrows():
            all_bull.append({"ticker": row["ticker"], "acc": row["bull_acc"], "pnl": row["bull_pnl"],
                            "count": row["bull_count"], "source": src_name, "index": idx})

        # Top N bear from this universe (only if fundamentals allow bear)
        bear_df = bear_eligible.sort_values("bear_pnl", ascending=False).head(args.top)
        for _, row in bear_df.iterrows():
            all_bear.append({"ticker": row["ticker"], "acc": row["bear_acc"], "pnl": row["bear_pnl"],
                            "count": row["bear_count"], "source": src_name, "index": idx})

    # Deduplicate: keep best P&L per ticker
    def dedup(picks):
        seen = {}
        for p in picks:
            if p["ticker"] not in seen or p["pnl"] > seen[p["ticker"]]["pnl"]:
                seen[p["ticker"]] = p
        return sorted(seen.values(), key=lambda x: x["pnl"], reverse=True)

    all_bull = dedup(all_bull)
    all_bear = dedup(all_bear)

    # Phase 2: Deep scan top picks with live scores
    print(f"\n\n{'═' * 70}")
    print(f"🔬 PHASE 2: Live scoring top {args.top} bull + {args.top} bear picks")
    print(f"{'═' * 70}")

    from experimental.trade_assistant import scan as ta_scan

    scored = []
    candidates = [(p, "BULL") for p in all_bull[:args.top]] + [(p, "BEAR") for p in all_bear[:args.top]]

    for p, edge in candidates:
        sym = p["ticker"]
        try:
            result = ta_scan(sym, p["index"], sentiment="bull" if edge == "BULL" else "bear")
            if "error" in result:
                continue

            idea = result["trade_idea"]
            score = idea.get("score", 0)
            bias = idea.get("bias", "NEUTRAL")

            # Skip if live bias is NEUTRAL (no actionable trade right now)
            if bias == "NEUTRAL":
                continue

            em = result["expected_move"]
            levels = result.get("levels")

            # Current price + trend
            tk = yf.Ticker(sym)
            info = tk.info
            price = info.get("currentPrice") or info.get("regularMarketPrice") or result["price"]
            tk_hist = tk.history(period="1y", interval="1d").dropna(subset=['Close'])
            if len(tk_hist) >= 200:
                sma200 = tk_hist['Close'].rolling(200).mean().iloc[-1]
                sma50 = tk_hist['Close'].rolling(50).mean().iloc[-1]
                if price > sma50 > sma200:
                    trend = "📈BULL"
                elif price < sma200:
                    trend = "📉BEAR"
                else:
                    trend = "➡️ NEUT"
            else:
                trend = "?"

            scored.append({
                "ticker": sym, "edge": edge, "score": score,
                "price": price, "trend": trend,
                "acc": p["acc"], "pnl": p["pnl"], "source": p["source"], "index": p["index"],
                "target": idea.get("target"), "stop": idea.get("stop"), "rr": idea.get("risk_reward"),
                "em_pct": em["move_pct"],
                "support": levels["support"] if levels else [],
                "resistance": levels["resistance"] if levels else [],
            })
        except Exception:
            continue

    # Sort by R:R descending (best risk/reward first), None goes to bottom
    scored.sort(key=lambda x: x["rr"] if x["rr"] else 0, reverse=True)
    list_n = args.list if args.list else 10

    # Print final ranked table
    print(f"\n   {'#':<3} {'Ticker':<7} {'Edge':<6} {'Score':<8} {'Trend':<8} {'Price':<10} {'Acc':<7} {'P&L':<8} {'Target':<10} {'R:R':<6} {'Source'}")
    print(f"   {'─' * 82}")
    for i, p in enumerate(scored[:list_n]):
        s = p['score']
        if abs(s) >= 6:
            marker = "⚠️"
        elif abs(s) >= 4:
            marker = "⭐"
        elif abs(s) >= 3:
            marker = "✅"
        else:
            marker = "⏸️"

        target_str = f"${p['target']:.1f}" if p['target'] else "—"
        rr_str = f"{p['rr']}:1" if p['rr'] else "—"
        print(f"   {i+1:<3} {p['ticker']:<7} {p['edge']:<6} {s:+d} {marker}  {p['trend']:<8} ${p['price']:<9.2f} {p['acc']:<7.0f}% {p['pnl']:+6.2f}% {target_str:<10} {rr_str:<6} {p['source']}")

    print(f"\n   Legend: ⭐ Sweet spot (4-5) | ✅ Actionable (3) | ⏸️ Wait (<3) | ⚠️ Contrarian (6+)")

    # News strategy comparison for top 10
    print(f"\n{'─' * 70}")
    print(f"   📰 NEWS FILTER IMPACT (index gap as overnight news proxy):")
    print(f"   {'Ticker':<7} {'Edge':<6} {'Baseline':<18} {'Align (w/ gap)':<18} {'Contra (vs gap)':<18}")
    print(f"   {'─' * 67}")
    for p in scored[:list_n]:
        sym = p["ticker"]
        bias = "bull" if p["edge"] == "BULL" else "bear"
        try:
            bt_base = backtest(sym, p["index"], days=args.days, horizon=args.horizon, only_bias=bias)
            bt_align = backtest(sym, p["index"], days=args.days, horizon=args.horizon, only_bias=bias, news_filter="align")
            bt_contra = backtest(sym, p["index"], days=args.days, horizon=args.horizon, only_bias=bias, news_filter="contra")

            base_str = f"{bt_base['test_days']}sig {bt_base['bias_accuracy']:.0f}%" if "error" not in bt_base else "—"
            align_str = f"{bt_align['test_days']}sig {bt_align['bias_accuracy']:.0f}%" if "error" not in bt_align else "—"
            contra_str = f"{bt_contra['test_days']}sig {bt_contra['bias_accuracy']:.0f}%" if "error" not in bt_contra else "—"
            print(f"   {sym:<7} {p['edge']:<6} {base_str:<18} {align_str:<18} {contra_str:<18}")
        except Exception:
            continue

    # If --list, show cards + AI prompts then stop (no full deep scan)
    if args.list is not None:
        print(f"\n\n{'═' * 70}")
        print(f"🎯 TOP {list_n} TRADE IDEAS")
        print(f"{'═' * 70}")

        for p in scored[:list_n]:
            s = p['score']
            if abs(s) >= 6:
                score_note = "⚠️ CONTRARIAN"
            elif abs(s) >= 4:
                score_note = "⭐ SWEET SPOT"
            elif abs(s) >= 3:
                score_note = "✅ ACTIONABLE"
            else:
                score_note = "⏸️ WAIT"

            # Leverage recommendation
            acc = p['acc']
            pnl = p['pnl']
            rr = p.get('rr')

            # R:R gate: if R:R < 1, trade is not worth taking regardless of accuracy
            if rr is not None and rr < 1.0:
                leverage = "🚫 bad R:R"
            elif acc >= 75 and pnl >= 4 and 4 <= abs(s) <= 5:
                leverage = "🔥 2x"
            elif acc >= 70 and pnl >= 4 and abs(s) >= 4:
                leverage = "✅ 2x"
            elif acc >= 70 and pnl >= 3 and abs(s) >= 3:
                leverage = "1.5x"
            elif acc >= 65 and abs(s) >= 3:
                leverage = "1x"
            else:
                leverage = "⏸️ skip"

            direction = "bullish" if p["edge"] == "BULL" else "bearish"
            opposite = "bearish" if p["edge"] == "BULL" else "bullish"
            support_str = ', '.join(f'${l}' for l in p['support']) if p['support'] else 'N/A'
            resist_str = ', '.join(f'${l}' for l in p['resistance']) if p['resistance'] else 'N/A'
            target_str = f"${p['target']:.2f}" if p['target'] else "—"
            stop_str = f"${p['stop']:.2f}" if p['stop'] else "—"
            rr_str = f"{p['rr']}:1" if p['rr'] else "—"

            print(f"\n{'─' * 70}")
            print(f"   {p['ticker']} | {p['edge']} | Score: {s:+d} {score_note} | Leverage: {leverage}")
            print(f"   Price: ${p['price']:.2f} | Trend: {p['trend']} | EM: ±{p['em_pct']}%/day")
            print(f"   180d: {p['acc']:.0f}% accuracy, {p['pnl']:+.2f}% avg P&L ({p['source']})")
            print(f"   Target: {target_str} | Stop: {stop_str} | R:R: {rr_str}")
            print(f"   Support: {support_str}")
            print(f"   Resistance: {resist_str}")

            print(f"\n   🤖 AI PROMPT:")
            print(f"")
            print(f"${p['ticker']} at ${p['price']:.2f}. Quant model: {direction} edge, {p['acc']:.0f}% accuracy / 180d. Score: {s:+d} ({score_note}). Leverage recommendation: {leverage}. Levels — Support: {support_str} | Resistance: {resist_str}")
            print(f"")
            print(f"1. FinTwit sentiment on ${p['ticker']}? Crowd {direction} or {opposite}?")
            print(f"2. Upcoming catalysts that could invalidate the {direction} thesis?")
            print(f"3. If crowd is also {direction} (crowded trade, score 6+), should I fade and go {opposite} instead?")
            print(f"4. Given {p['acc']:.0f}% accuracy and leverage rec '{leverage}', is this a high-confidence {direction} setup or a trap?")

        print(f"\n{'═' * 70}\n")
        return

    # Print detailed cards for top 3 by score
    print(f"\n\n{'═' * 70}")
    print(f"🎯 TOP 3 ACTIONABLE (highest |score|)")
    print(f"{'═' * 70}")

    for p in scored[:3]:
        sym = p["ticker"]
        direction = "bullish" if p["edge"] == "BULL" else "bearish"
        opposite = "bearish" if p["edge"] == "BULL" else "bullish"
        support_str = ', '.join(f'${l}' for l in p['support']) if p['support'] else 'N/A'
        resist_str = ', '.join(f'${l}' for l in p['resistance']) if p['resistance'] else 'N/A'

        s = p['score']
        if abs(s) >= 6:
            score_note = "⚠️ CONTRARIAN ZONE"
        elif abs(s) >= 4:
            score_note = "⭐ SWEET SPOT"
        elif abs(s) >= 3:
            score_note = "✅ ACTIONABLE"
        else:
            score_note = "⏸️ WAIT"

        print(f"\n{'─' * 70}")
        print(f"   {sym} | {p['edge']} | Score: {s:+d} {score_note}")
        print(f"   Price: ${p['price']:.2f} | Trend: {p['trend']} | EM: ±{p['em_pct']}%/day")
        print(f"   180d: {p['acc']:.0f}% accuracy, {p['pnl']:+.2f}% avg P&L ({p['source']})")
        if p['target']:
            print(f"   Target: ${p['target']:.2f} | Stop: ${p['stop']:.2f} | R:R: {p['rr']}:1" if p['stop'] and p['rr'] else f"   Target: ${p['target']:.2f}")
        print(f"   Support: {support_str}")
        print(f"   Resistance: {resist_str}")

        print(f"\n   🤖 AI PROMPT:")
        print(f"")
        print(f"${sym} at ${p['price']:.2f}. Quant model: {direction} edge, {p['acc']:.0f}% accuracy / 180d. Score: {s:+d} ({score_note}). Levels — Support: {support_str} | Resistance: {resist_str}")
        print(f"")
        print(f"1. FinTwit sentiment on ${sym}? Crowd {direction} or {opposite}?")
        print(f"2. Upcoming catalysts that could invalidate the {direction} thesis?")
        print(f"3. If crowd is also {direction} (crowded trade, score 6+), should I fade and go {opposite} instead?")
        print(f"4. One-paragraph conviction check: is this a high-confidence {direction} setup or a trap?")

    print(f"\n{'═' * 70}\n")


if __name__ == "__main__":
    main()
