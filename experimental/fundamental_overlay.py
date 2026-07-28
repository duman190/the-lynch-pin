"""Fundamental Overlay Test — does adding Lynch Pin GARP data improve edge?

Tests whether combining:
  - PEG deviation (cheap vs expensive relative to history)
  - Income statement grade (A/B/C/D)
  - Balance sheet rating (AAA → D)
  - Base ROI projection

...with the technical trade assistant improves backtest accuracy.

Hypothesis: If a stock is fundamentally undervalued (Dev_SD < -0.5) AND
the technical model says BULL, accuracy should be higher than technicals alone.

Usage:
    python -m experimental.fundamental_overlay --ticker MU --index SMH
    python -m experimental.fundamental_overlay --src database/smh.txt
"""

import argparse
import warnings
import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# Add project root to path for engine imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.lynch_pin_core import LynchPinEngine
from engine.income_statement_grader import grade_ticker as grade_income
from engine.balance_sheet_grader import grade_ticker as grade_balance
from experimental.back_test import backtest


def get_fundamental_bias(symbol):
    """Get fundamental bias from Lynch Pin GARP engine.

    Returns:
        dict with:
          - dev_sd: PEG deviation in standard deviations (negative = cheap)
          - base_roi: projected 5Y annualized return
          - income_grade: A+/A/B+/B/B-/C/D
          - credit_rating: AAA → D
          - fundamental_bias: BULLISH / BEARISH / NEUTRAL
    """
    try:
        engine = LynchPinEngine(symbol)
        stats = engine.get_ticker_stats()
        if not stats:
            return None

        dev_sd = stats["Dev_SD"]
        base_roi = float(stats["Base"].replace("%", ""))

        # Income grade
        ig = grade_income(engine.ticker)
        income_grade = ig["grade"] if ig else "N/A"

        # Balance sheet
        bg = grade_balance(engine.ticker)
        credit_rating = bg["rating"] if bg else "NR"

        # Fundamental bias logic:
        # BULLISH: undervalued (dev_sd < -0.5) AND decent fundamentals (grade >= B, rating >= BBB)
        # BEARISH: overvalued (dev_sd > 1.0) OR poor fundamentals (grade <= C OR rating <= BB)
        # NEUTRAL: everything else

        good_grade = income_grade in {'A+', 'A', 'A-', 'B+', 'B'}
        good_rating = credit_rating not in {'BB+', 'BB', 'BB-', 'B+', 'B', 'B-', 'CCC+', 'CCC', 'CC', 'D', 'NR'}
        bad_grade = income_grade in {'C', 'D', 'N/A'}
        bad_rating = credit_rating in {'B+', 'B', 'B-', 'CCC+', 'CCC', 'CC', 'D'}

        if dev_sd < -0.5 and good_grade and good_rating and base_roi > 12:
            fundamental_bias = "BULLISH"
        elif dev_sd > 1.0 or bad_grade or bad_rating or base_roi < 5:
            fundamental_bias = "BEARISH"
        else:
            fundamental_bias = "NEUTRAL"

        return {
            "dev_sd": dev_sd,
            "base_roi": base_roi,
            "income_grade": income_grade,
            "credit_rating": credit_rating,
            "fundamental_bias": fundamental_bias,
        }
    except Exception:
        return None


def test_fundamental_overlay(symbol, index_symbol="SMH", days=180, horizon=5):
    """Compare backtest accuracy with and without fundamental alignment."""

    # Get fundamentals
    print(f"\n   Fetching fundamentals for {symbol}...")
    fund = get_fundamental_bias(symbol)
    if not fund:
        return {"error": f"Could not get fundamentals for {symbol}"}

    print(f"   Dev(SD): {fund['dev_sd']:+.2f} | ROI: {fund['base_roi']:.1f}% | Grade: {fund['income_grade']} | Rating: {fund['credit_rating']}")
    print(f"   Fundamental Bias: {fund['fundamental_bias']}")

    # Run baseline backtest
    bt_all = backtest(symbol, index_symbol, days=days, horizon=horizon)
    if "error" in bt_all:
        return bt_all

    # Run bias-aligned backtest
    if fund["fundamental_bias"] == "BULLISH":
        bt_aligned = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bull")
        bt_opposed = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bear")
        aligned_label = "BULL (fundamentals say cheap)"
        opposed_label = "BEAR (against fundamentals)"
    elif fund["fundamental_bias"] == "BEARISH":
        bt_aligned = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bear")
        bt_opposed = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bull")
        aligned_label = "BEAR (fundamentals say expensive)"
        opposed_label = "BULL (against fundamentals)"
    else:
        bt_aligned = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bull")
        bt_opposed = backtest(symbol, index_symbol, days=days, horizon=horizon, only_bias="bear")
        aligned_label = "BULL (neutral fundamentals)"
        opposed_label = "BEAR (neutral fundamentals)"

    return {
        "ticker": symbol,
        "fundamentals": fund,
        "baseline": bt_all,
        "aligned": bt_aligned,
        "opposed": bt_opposed,
        "aligned_label": aligned_label,
        "opposed_label": opposed_label,
    }


def print_results(results):
    """Print comparison table."""
    if "error" in results:
        print(f"   ❌ {results['error']}")
        return

    fund = results["fundamentals"]
    bt_all = results["baseline"]
    bt_aligned = results["aligned"]
    bt_opposed = results["opposed"]

    print(f"\n{'─' * 65}")
    print(f"   📊 FUNDAMENTAL OVERLAY — {results['ticker']}")
    print(f"{'─' * 65}")
    print(f"   PEG Dev: {fund['dev_sd']:+.2f}σ | ROI: {fund['base_roi']:.1f}% | Grade: {fund['income_grade']} | Rating: {fund['credit_rating']}")
    print(f"   Fundamental Bias: {fund['fundamental_bias']}")
    print(f"\n   {'Strategy':<40} {'Signals':<9} {'Accuracy':<11} {'P&L':<8}")
    print(f"   {'─' * 60}")

    if "error" not in bt_all:
        print(f"   {'All signals (baseline)':<40} {bt_all['test_days']:<9} {bt_all['bias_accuracy']:<11.1f}% {bt_all['avg_pnl_pct']:+.2f}%")
    if "error" not in bt_aligned:
        print(f"   {results['aligned_label']:<40} {bt_aligned['test_days']:<9} {bt_aligned['bias_accuracy']:<11.1f}% {bt_aligned['avg_pnl_pct']:+.2f}%")
    if "error" not in bt_opposed:
        print(f"   {results['opposed_label']:<40} {bt_opposed['test_days']:<9} {bt_opposed['bias_accuracy']:<11.1f}% {bt_opposed['avg_pnl_pct']:+.2f}%")

    # Verdict
    if "error" not in bt_aligned and "error" not in bt_opposed:
        aligned_acc = bt_aligned['bias_accuracy']
        opposed_acc = bt_opposed['bias_accuracy']
        if aligned_acc > opposed_acc + 5:
            print(f"\n   ✅ FUNDAMENTALS ADD EDGE: +{aligned_acc - opposed_acc:.1f}% accuracy when trading WITH fundamentals")
        elif opposed_acc > aligned_acc + 5:
            print(f"\n   ⚠️ CONTRARIAN WORKS: trading AGAINST fundamentals is +{opposed_acc - aligned_acc:.1f}% better")
        else:
            print(f"\n   ➡️ NEUTRAL: fundamentals don't significantly change accuracy ({aligned_acc:.1f}% vs {opposed_acc:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Test if GARP fundamentals improve trade assistant edge")
    parser.add_argument("--ticker", default=None, help="Single ticker to test")
    parser.add_argument("--src", default=None, help="Ticker file to scan")
    parser.add_argument("--index", default="SMH", help="Reference index (default: SMH)")
    parser.add_argument("--days", type=int, default=180, help="Backtest days (default: 180)")
    args = parser.parse_args()

    if args.ticker:
        tickers = [args.ticker]
    elif args.src:
        with open(args.src) as f:
            tickers = [l.strip() for l in f if l.strip()]
    else:
        print("❌ Provide --ticker or --src")
        return

    print(f"\n{'═' * 65}")
    print(f"🔬 FUNDAMENTAL OVERLAY TEST — Does GARP valuation add edge?")
    print(f"{'═' * 65}")

    summary = []
    for sym in tickers:
        print(f"\n{'═' * 65}")
        print(f"   {sym} vs {args.index}")
        results = test_fundamental_overlay(sym, args.index, days=args.days)
        print_results(results)
        if "error" not in results:
            fund = results["fundamentals"]
            aligned = results["aligned"]
            opposed = results["opposed"]
            if "error" not in aligned and "error" not in opposed:
                summary.append({
                    "ticker": sym,
                    "dev_sd": fund["dev_sd"],
                    "fund_bias": fund["fundamental_bias"],
                    "aligned_acc": aligned["bias_accuracy"],
                    "opposed_acc": opposed["bias_accuracy"],
                    "delta": aligned["bias_accuracy"] - opposed["bias_accuracy"],
                })

    if len(summary) > 1:
        df = pd.DataFrame(summary)
        print(f"\n\n{'═' * 65}")
        print(f"📊 SUMMARY — Fundamental overlay impact across {len(df)} tickers")
        print(f"{'═' * 65}")
        print(f"\n   {'Ticker':<8} {'Dev(SD)':<9} {'Fund Bias':<12} {'Aligned':<10} {'Opposed':<10} {'Delta':<8}")
        print(f"   {'─' * 55}")
        for _, row in df.sort_values("delta", ascending=False).iterrows():
            marker = "✅" if row["delta"] > 5 else "⚠️" if row["delta"] < -5 else "➡️"
            print(f"   {row['ticker']:<8} {row['dev_sd']:+6.2f}σ  {row['fund_bias']:<12} {row['aligned_acc']:<10.1f}% {row['opposed_acc']:<10.1f}% {row['delta']:+.1f}% {marker}")

        avg_delta = df["delta"].mean()
        positive = (df["delta"] > 5).sum()
        negative = (df["delta"] < -5).sum()
        print(f"\n   Average delta: {avg_delta:+.1f}%")
        print(f"   Fundamentals help: {positive}/{len(df)} tickers")
        print(f"   Fundamentals hurt: {negative}/{len(df)} tickers")

        if avg_delta > 3:
            print(f"\n   🎯 VERDICT: Fundamentals ADD meaningful edge (+{avg_delta:.1f}% avg)")
        elif avg_delta < -3:
            print(f"\n   ⚠️ VERDICT: Fundamentals HURT — contrarian approach works better")
        else:
            print(f"\n   ➡️ VERDICT: Fundamentals are neutral — technicals dominate on this timeframe")

    print(f"\n{'═' * 65}\n")


if __name__ == "__main__":
    main()
