"""OpenRouter probe — send the real Lynch Pin prompt straight to the free router,
collect the reply exactly as engine/ai_research.py does, then build the X thread
preview exactly as main.py would. Nothing is posted.

Run from the project root:

    python tmp/openrouter_probe.py                              # openrouter/free, live call
    python tmp/openrouter_probe.py --model qwen/qwen3-8b:free   # pin a specific free model
    python tmp/openrouter_probe.py --raw tmp/openrouter_raw.txt # re-parse a saved reply, no API call
    python tmp/openrouter_probe.py --no-normalize               # how main.py fares on the untouched reply

The raw reply is always saved to tmp/openrouter_raw.txt so a bad one can be replayed.
"""
import argparse
import os
import re
import sys

# tmp/ lives one level below the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_research import LynchPinResearcher, OPENROUTER_FREE_MODEL  # noqa: E402
from main import IDX_DISPLAY  # noqa: E402

# Dataset from the 2026-09-17 SMH run (--top 8 --excl-bad, post-FMP enrichment) — the run
# where liquid/lfm-2.5-2.6b:free wrote "$TICKER: ARM" headers and main.py parsed nothing.
SAMPLE = [
    {'Ticker': 'ARM',  'PE': 270.3, 'FwdPE': 86.7, '2YFwd': 64.3, '5YGrowth': '34.8%',
     'PEG': 2.49, 'Mean': 5.94, 'Dev_SD': -2.32, 'Bull': '22.1%', 'Base': '12.3%', 'Bear': '-3.3%'},
    {'Ticker': 'ASML', 'PE': 55.6, 'FwdPE': 27.3, '2YFwd': 23.0, '5YGrowth': '19.0%',
     'PEG': 1.44, 'Mean': 2.34, 'Dev_SD': -1.82, 'Bull': '23.3%', 'Base': '19.6%', 'Bear': '15.2%'},
    {'Ticker': 'TSM',  'PE': 32.0, 'FwdPE': 19.6, '2YFwd': 15.5, '5YGrowth': '26.3%',
     'PEG': 0.75, 'Mean': 1.01, 'Dev_SD': -1.67, 'Bull': '31.9%', 'Base': '29.9%', 'Bear': '22.2%'},
    {'Ticker': 'CDNS', 'PE': 55.7, 'FwdPE': 29.4, '2YFwd': 25.9, '5YGrowth': '13.7%',
     'PEG': 2.14, 'Mean': 2.83, 'Dev_SD': -1.55, 'Bull': '15.0%', 'Base': '12.6%', 'Bear': '10.1%'},
    {'Ticker': 'ALAB', 'PE': 145.3, 'FwdPE': 45.9, '2YFwd': 24.6, '5YGrowth': '86.4%',
     'PEG': 0.53, 'Mean': 1.26, 'Dev_SD': -1.43, 'Bull': '87.0%', 'Base': '77.0%', 'Bear': '63.1%'},
    {'Ticker': 'ADI',  'PE': 43.0, 'FwdPE': 22.2, '2YFwd': 15.7, '5YGrowth': '41.1%',
     'PEG': 0.54, 'Mean': 0.76, 'Dev_SD': -1.24, 'Bull': '43.5%', 'Base': '40.3%', 'Bear': '31.0%'},
    {'Ticker': 'NXPI', 'PE': 19.4, 'FwdPE': 12.6, '2YFwd': 9.8, '5YGrowth': '28.6%',
     'PEG': 0.44, 'Mean': 1.25, 'Dev_SD': -0.88, 'Bull': '63.3%', 'Base': '53.3%', 'Bear': '27.6%'},
    {'Ticker': 'NVDA', 'PE': 27.7, 'FwdPE': 14.0, '2YFwd': 10.6, '5YGrowth': '32.4%',
     'PEG': 0.43, 'Mean': 0.59, 'Dev_SD': -0.76, 'Bull': '35.9%', 'Base': '31.5%', 'Bear': '27.1%'},
]

# Income statement waterfall: (label, YoY growth as fraction or None, signal emoji)
_G, _B, _R = '🟢', '🔵', '🔴'


def _inc(grade, rev, cogs, gross, rnd, snm, gna, opinc, ni, eps):
    vals = [rev, cogs, gross, rnd, snm, gna, opinc, ni, eps]
    labels = ['Revenue', 'COGS', 'Gross', 'R&D', 'S&M', 'G&A', 'OpIncome', 'NetIncome', 'EPS']
    return {'grade': grade,
            'items': [(l, None if v is None else v[0] / 100, v[1] if v else '') for l, v in zip(labels, vals)]}


GRADES = {
    'ARM':  _inc('B',   (22, _G), (20, _G), (22, _G), (29, _B), None, None, (-14, _R), (108, _G), (108, _G)),
    'ASML': _inc('A+',  (21, _G), (20, _G), (22, _G), (9, _G), None, None, (30, _G), (27, _G), (28, _G)),
    'TSM':  _inc('A++', (36, _G), (6, _G), (57, _G), (19, _G), (5, _G), None, (65, _G), (77, _G), (77, _G)),
    'CDNS': _inc('B+',  (24, _G), (30, _B), (23, _B), (20, _G), (20, _G), (28, _B), (21, _B), (129, _G), (125, _G)),
    'ALAB': _inc('A+',  (104, _G), (126, _B), (98, _B), (104, _G), (42, _G), (76, _G), (124, _G), (199, _G), (186, _G)),
    'ADI':  _inc('A++', (40, _G), (21, _G), (51, _G), (17, _G), None, None, (93, _G), (158, _G), (163, _G)),
    'NXPI': _inc('A++', (19, _G), (10, _G), (28, _G), (5, _G), None, None, (57, _G), (72, _G), (73, _G)),
    'NVDA': _inc('A+',  (106, _G), (87, _G), (113, _G), (64, _G), None, None, (124, _G), (126, _G), (128, _G)),
}


def _bs(rating, intcov, nd_ebitda, cash_debt, svc_fcf):
    return {'rating': rating, 'metrics': [('IntCov', intcov), ('ND/EBITDA', nd_ebitda),
                                          ('Cash/Debt', cash_debt), ('Svc/FCF%', svc_fcf)]}


RATINGS = {
    'ARM':  _bs('AAA', None, -2.2, 6.6, None),
    'ASML': _bs('AAA', None, -0.4, 3.4, None),
    'TSM':  _bs('AAA', 211.0, -0.6, 2.9, 1.0),
    'CDNS': _bs('AAA', 14.4, 0.5, 0.5, 7.5),
    'ALAB': _bs('AAA', None, None, None, None),
    'ADI':  _bs('AAA', 14.1, 1.0, 0.2, 7.1),
    'NXPI': _bs('AA+', 7.7, 1.6, 0.3, 17.8),
    'NVDA': _bs('AAA', 427.0, 0.1, 0.6, 0.4),
}


def _tech(signal, trend, rsi, sma, atr, zone):
    return {'signal': signal, 'trend': trend, 'rsi': rsi, 'price_vs_sma200': sma,
            'ema50_vs_sma200': 0.0, 'atr_compression': atr, 'accumulation_zone': zone}


TECH = {
    'ARM':  _tech('BULLISH', 'BULLISH', 54, 28, 0.60, (192, 221)),
    'ASML': _tech('ACCUMULATION', 'NEUTRAL', 40, 8, 0.72, (1449, 1557)),
    'TSM':  _tech('BULLISH', 'BULLISH', 52, 14, 0.64, (366, 387)),
    'CDNS': _tech('BEARISH', 'BEARISH', 13, -14, 0.79, (316, 337)),
    'ALAB': _tech('NEUTRAL', 'NEUTRAL', 48, 27, 0.63, (210, 251)),
    'ADI':  _tech('ACCUMULATION', 'NEUTRAL', 42, 3, 0.73, (341, 362)),
    'NXPI': _tech('BEARISH', 'BEARISH', 52, -6, 0.68, (235, 251)),
    'NVDA': _tech('BULLISH', 'BULLISH', 43, 11, 0.91, (191, 204)),
}


def _edge(best, bull_acc, bull_n, bull_pnl, bear_acc, bear_n, bear_pnl):
    return {'best_edge': best, 'bull_acc': bull_acc, 'bull_n': bull_n, 'bull_pnl': bull_pnl,
            'bear_acc': bear_acc, 'bear_n': bear_n, 'bear_pnl': bear_pnl}


EDGE = {
    'ARM':  _edge('BULL', 64, 45, 6.04, 43, 44, -4.13),
    'ASML': _edge('BULL', 53, 88, 0.06, 32, 19, -4.06),
    'TSM':  _edge('BEAR', 60, 73, 1.17, 71, 14, 2.73),
    'CDNS': _edge('BULL', 67, 42, 0.50, 60, 70, 0.34),
    'ALAB': _edge('BULL', 43, 35, -1.12, 40, 72, -2.94),
    'ADI':  _edge('BULL', 72, 67, 1.55, 48, 23, 1.05),
    'NXPI': _edge('BULL', 51, 55, -0.20, 42, 43, -1.77),
    'NVDA': _edge('BULL', 48, 56, -0.11, 40, 35, -1.60),
}


def parse_like_main(text, tickers):
    """Mirror of the sentiment + per-ticker extraction in main.py."""
    sent = re.search(r'SENTIMENT:\s*(.+)', text)
    if sent:
        sentiment = re.sub(r'^SENTIMENT:\s*', '', sent.group(1).strip())
        sentiment = re.sub(r'\$([A-Z]+)', r'\1', sentiment)
        bulk = text[sent.end():].strip()
    else:
        sentiment, bulk = "", text
    bulk = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk)

    replies = {}
    for t in tickers:
        pattern = rf"^\${re.escape(t)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        m = re.search(pattern, bulk, re.DOTALL | re.MULTILINE)
        replies[t] = m.group(1).strip() if m else None
    return sentiment, replies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=OPENROUTER_FREE_MODEL, help="OpenRouter model id")
    ap.add_argument("--raw", help="Skip the API call; parse a saved raw reply instead")
    ap.add_argument("--idx", default="SMH")
    ap.add_argument("--no-normalize", action="store_true", help="Show how main.py fares on the untouched reply")
    args = ap.parse_args()

    tickers = [d['Ticker'] for d in SAMPLE]
    prompt = LynchPinResearcher.build_prompt(SAMPLE, GRADES, args.idx, RATINGS, TECH, EDGE)

    if args.raw:
        with open(args.raw, encoding="utf-8") as f:
            raw = f.read()
        print(f"📄 Loaded raw reply from {args.raw} ({len(raw)} chars)")
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY not set")
        os.environ.pop("GEMINI_API_KEY", None)  # force the OpenRouter-only path
        researcher = LynchPinResearcher()
        print(f"📤 Prompt: {len(prompt)} chars → {args.model}")
        raw = researcher._call_openrouter_model(args.model, prompt)
        os.makedirs("tmp", exist_ok=True)
        with open("tmp/openrouter_raw.txt", "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"📥 Reply: {len(raw)} chars (saved to tmp/openrouter_raw.txt)")

    print("\n" + "═" * 30 + " RAW REPLY " + "═" * 30)
    print(raw)

    text = raw if args.no_normalize else LynchPinResearcher.normalize_narrative(raw, tickers)
    if not args.no_normalize and text != raw:
        print("\n🔧 normalize_narrative rewrote the reply (headers / SENTIMENT label were off-template)")

    sentiment, replies = parse_like_main(text, tickers)
    gaps = LynchPinResearcher.narrative_gaps(text, tickers)
    print(f"\n🔎 narrative_gaps → {'usable ✅' if gaps is None else 'REJECTED ❌ (' + gaps + ') — main.py would retry, not post this'}")

    # ── Same thread main.py would hand to XPublisher.post_thread (preview only, nothing is posted) ──
    idx_display = IDX_DISPLAY.get(args.idx, args.idx)
    main_tweet = f"🚨 MARKET CLOSE: ${args.idx} #LynchPin Detector\n\n"
    if sentiment:
        main_tweet += f"🤖: {sentiment.replace(f'${args.idx}', idx_display).replace('$', '')}\n\n"
    main_tweet += f"Top {len(SAMPLE)} GARP deals + ROI Projections:\n\n"
    num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, r in enumerate(SAMPLE):
        main_tweet += f"{num_emojis[i]} {r['Ticker']}: PEG {r['PEG']:.1f} ({r['Dev_SD']:.1f}SD)| 🎯ROI:{r['Base']}\n"

    footer = (f"In this market, you'll miss the best compounders waiting for a perfect 1.0 PEG."
              f" Which of these {idx_display} anomalies are the hardest for your stomach? 👇\n\n"
              f"@grok Search recent X posts about the ${args.idx} tickers in this thread. "
              f"Based on FinTwit sentiment alone (not the numbers above), which one does "
              f"FinTwit love most and hate most right now, and why?\n\n"
              "⚠️ DISCLAIMER: Quant scans, not financial advice. Math can be mistaken. "
              "Investing involves risk. Always DYOR. 🫶")

    print("\n--- 📝 PREVIEW OF POST (dry run — nothing is posted) ---")
    print("MAIN TWEET:")
    print(main_tweet)
    print("[Attach: tmp/benchmark_comparison.png]\n")

    ok = 0
    for t in tickers:
        body = replies[t]
        if body is None:
            narrative = "Valuation disconnect detected via quantitative analysis."
            status = "❌ NO MATCH → generic placeholder"
        else:
            ok += 1
            narrative = body.replace('📊 Reverse DCF:', '📊 Reverse 5Y DCF:')
            narrative = narrative.replace('🧪 Stomach Test:',
                                          '🐻 "Stomach Test" (why it can underperform in the next 5 years):')
            have = [lbl for lbl in ('🤖:', '📊 Reverse 5Y DCF:', '🐻 "Stomach Test"') if lbl in narrative]
            status = f"✅ {len(narrative)} chars, sections: {', '.join(have) or 'none'}"
        print(f"REPLY TWEET ({t}):  {status}")
        print(f"${t}\n\n{narrative}")
        print(f"[Attach: tmp/{t}_valuation.png]\n")

    print("FOOTER:")
    print(footer)
    print("--------------------------")

    print(f"\n{'✅' if ok == len(tickers) and sentiment else '⚠️ '} {ok}/{len(tickers)} ticker replies parsed, "
          f"sentiment {'found' if sentiment else 'MISSING (main tweet has no 🤖 line)'}")


if __name__ == "__main__":
    main()
