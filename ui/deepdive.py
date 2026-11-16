"""Deep Dive Prompt: a copy-paste brief for a stronger AI (Claude, ChatGPT, Gemini).

The brief casts the model as a hedge-fund portfolio manager, walks it through primary sources
(last two earnings reports, call transcripts, 10-Q), news, management's answers and red flags,
makes it *loop* (stress-test the bull and bear case against the evidence until another pass would
not change the conclusion), and asks for a report with a verdict versus the S&P 500, price levels
and signals to watch. The Lynch Pin quant snapshot, the price levels and the local AI overview are
attached as data at the end.
"""
import datetime as _dt
import math
import re

ROLE = """You are a senior portfolio manager at a long-only fundamental hedge fund. The investment committee \
meets tomorrow and wants your call on {name} ({cashtag}): does it earn a place in the portfolio for the next \
3-5 years, or should we pass because it is likely to underperform the S&P 500?

At the end of this prompt is a quantitative snapshot from our in-house GARP screener, "The Lynch Pin" \
(valuation against the stock's own PEG history, 5-year ROI scenarios, income statement and balance sheet \
grades, technicals, price levels to watch and a 6-month backtested directional edge), followed by a \
first-pass note from a small local AI model. Treat the snapshot as a starting point and the AI note as a \
junior analyst's draft: verify, don't trust.

If you can browse or search the web, use it for every step below. Prefer primary sources (SEC filings, \
company investor relations, earnings call transcripts) and cite each fact with its source and date. If you \
cannot browse, say so up front, work from your own knowledge and flag anything that may be out of date.

## How to work

Work through the steps in order and don't write the verdict before step 7.

1. Snapshot. Read the Lynch Pin data below. Note what it implies (cheap or expensive against its own \
history, what growth the price assumes, balance sheet strength, trend) and anything that looks inconsistent. \
The forward PE is the valuation anchor: it uses the analyst consensus EPS for the next fiscal year, which \
excludes one-off items. The trailing PE uses GAAP earnings and can be distorted by them. Don't rebuild the \
forward multiple from a single quarter's run-rate; if you doubt the forward EPS, compare it with the consensus \
for the same fiscal year.
2. Primary sources. Read the last two quarterly earnings releases, both earnings call transcripts and the \
latest 10-Q (and the 10-K if it is recent). Extract, with numbers: revenue and segment growth, gross and \
operating margins, guidance and how it changed, capex and free cash flow, buybacks, dilution and stock-based \
compensation, debt and liquidity, and any question management dodged on the calls.
3. News and issues. Find the material news of the last 6-12 months: litigation and regulation (for example \
antitrust), management changes, competitive threats, customer concentration, product cycles, pricing \
pushback, short-seller reports, insider selling.
4. Management's answer. For each material issue, explain how management says it will address it and judge \
whether the plan is credible. Examples of the kind of issue meant: FICO defending its pricing power against \
antitrust scrutiny over monopoly claims and price increases; Meta spending heavily on AI capex without a \
clear road to a return on that spend.
5. Red flags and worrying trends. Receivables or inventory growing faster than revenue, falling margins, \
capex or acquisitions without a visible return, rising leverage, heavy adjustments or one-offs, guidance \
cuts, customer or supplier concentration, dilution, aggressive accounting.
6. Stress-test and loop. Write the strongest bull case and the strongest bear case. Check every claim in \
both against the evidence you gathered. If a claim is unsupported, or a new question comes up, go back to \
steps 2-5 for that point and dig until it is settled. Reconcile your view with the quant snapshot: where \
they disagree (for example a low PEG with deteriorating fundamentals, or a BULL edge in a weakening \
business), say which one you trust and why. Repeat this step until another pass would not change your \
conclusion, and report how many passes you made and what changed in each.
7. Report, in the format below.

## Report format

1. Verdict: INVEST, WATCHLIST or PASS against the S&P 500 over 3-5 years, your conviction (low, medium or \
high) and the three reasons that decide it.
2. The business and its moat, in a few sentences.
3. What the last two quarters, the calls and the 10-Q show: numbers, trends, guidance.
4. Key issues and how management is addressing them, with your judgement of credibility.
5. Red flags and worrying trends, most serious first.
6. Valuation: is the snapshot's Base ROI realistic? What revenue growth, margins and exit multiple must be \
true, and how does the expected return compare with the S&P 500? State the S&P 500 return you assume and how \
you derived it.
7. Price levels to watch: a table with the level, what it is (support, resistance, volume node, \
accumulation zone, expected range, moving average, 52-week high or low), the expected annual return if \
bought there, and what to do there (start a position, add, trim, re-assess the thesis). Start from the levels \
in the snapshot and adjust them with your own reading.
8. Signals and news to watch: upcoming catalysts with dates (next earnings, product launches, court or \
regulatory decisions) and the metrics and thresholds that would confirm or break the thesis.
9. What would change your mind, in either direction.

Open the report with the sources you read in full and those you only skimmed. Be specific and \
quantitative, use plain language and don't hedge the verdict."""

NOTES = """- Trailing PE = price / GAAP EPS of the last 12 months (includes one-off items such as gains on equity \
investments). Forward PE = price / analyst consensus EPS for the next fiscal year (excludes them). 2-year \
forward PE = price / forward EPS grown one more year at the 5-year growth estimate.
- PEG = forward P/E / blended 5-year EPS growth. "Enriched" growth averages the Yahoo analyst \
consensus with FMP analyst estimates; otherwise it is Yahoo only. A haircut applies when the estimate runs far \
above the company's historical revenue, margin and buyback growth, and it is capped at next fiscal year's \
consensus EPS growth after a rebound year (this year's growth above 1.5x the 5-year rate) or from 20% growth.
- Hist. mean PEG / SD / Dev (SD): today's PEG against the stock's own reconstructed 5-year forward-PEG \
history. Negative = cheaper than usual. Cyclicals coming off trough earnings can have a distorted history.
- ROI scenarios: annualised 5-year return from EPS growth (from 20% growth it fades over the 5 years to the \
decayed terminal growth) and a terminal multiple. The Base case is mean reversion toward the historical PEG, or today's multiple holding when \
the stock already trades above its mean.
- Income grade (A++ to D): year-over-year change of each income statement line relative to revenue. Net \
income and EPS are GAAP, so non-operating gains or losses flow into them and into the grade.
- Credit rating (AAA to D): synthetic, Damodaran's interest coverage method (TTM operating income / interest), \
notched for leverage and liquidity. Cash means cash and cash equivalents on the latest balance sheet; \
marketable securities and long-term investments are excluded, so Cash / debt and net debt are conservative.
- Technicals: BULLISH = price > EMA50 > SMA200; NEUTRAL = above SMA200 only; BEARISH = below SMA200; \
ACCUMULATION = not bearish, near support or oversold, with volatility compressing.
- Price levels: support and resistance from KDE-clustered pivot highs and lows over 6 months, the volume \
profile's point of control over 3 months, and expected ranges from 20-day realised volatility. P(touch) = the \
probability of trading at the level within a month.
- 6M directional edge: how often the screener's bull or bear signals were right over the last 180 trading \
days versus the benchmark, with average P&L per signal. It is a short-term timing aid for options income, not \
a fundamental view."""

_SIGNAL_WORDS = {"good": "healthy vs revenue", "neutral": "in line", "bad": "costs outgrowing revenue", "na": "n/a"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _f(v, nd=1, suffix=""):
    return f"{v:.{nd}f}{suffix}" if _num(v) else "N/A"


def _pct(v, nd=1):
    return f"{v:+.{nd}f}%" if _num(v) else "N/A"


def _money(v):
    return f"${v:,.2f}" if _num(v) else "N/A"


def _big(v):
    if not _num(v):
        return None
    for n, s in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= n:
            return f"${v / n:.2f}{s}"
    return f"${v:,.0f}"


def _base_roi_math(row):
    """The daily scan's "Base ROI math" sentence for this row (implied terminal PE etc.), if available."""
    if not row:
        return None
    try:
        from engine.ai_research import LynchPinResearcher as R
        m = re.search(r"Base ROI math: (.*?)(?:\n|$)", R.build_prompt([row]))
        return m.group(1).strip() if m else None
    except Exception:  # google-genai missing or the upstream layout changed
        return None


def _as_of(data):
    ts = data.get("generated_at")
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return _dt.date.today().isoformat()


def data_block(data):
    sym = data.get("ticker", "?")
    out = []
    add = out.append
    head = [data.get("name") or sym]
    for k in ("sector", "industry"):
        if data.get(k):
            head.append(data[k])
    add(f"Company: {' · '.join(head)} (${sym})")
    cap = _big(data.get("market_cap"))
    add(f"Price: {_money(data.get('price'))}" + (f" · Market cap: {cap}" if cap else ""))

    st = data.get("stats")
    add("")
    add("VALUATION")
    if st:
        growth = st.get("display", {}).get("5YGrowth") or _f(st.get("growth_pct"), 1, "%")
        enriched = "enriched: Yahoo + FMP analyst estimates" if data.get("growth_enriched") else "Yahoo consensus only"
        price = data.get("price")
        implied = lambda pe: price / pe if _num(price) and _num(pe) and pe > 0 else None  # noqa: E731
        t_eps, f_eps = implied(st.get("PE")), implied(st.get("FwdPE"))
        add(f"- Trailing PE {_f(st.get('PE'))}" + (f" (GAAP EPS last 12 months ≈ {_money(t_eps)})" if t_eps else "")
            + f" · Forward PE {_f(st.get('FwdPE'))}"
            + (f" (next fiscal year consensus EPS ≈ {_money(f_eps)})" if f_eps else "")
            + f" · 2-year forward PE {_f(st.get('2YFwd'))}")
        if t_eps and f_eps and t_eps > f_eps * 1.1:
            add("- Trailing EPS is above next year's consensus: trailing earnings likely include one-off gains, so the "
                "trailing PE understates the multiple; the forward PE is the cleaner one")
        add(f"- 5Y EPS growth estimate: {growth} ({enriched})")
        dev = st.get("Dev_SD")
        rel = "cheaper than its history" if _num(dev) and dev < 0 else "more expensive than its history"
        if st.get("history") == "unavailable":
            add(f"- PEG {_f(st.get('PEG'), 2)} (5-year PEG history unavailable; mean and SD are placeholders)")
        else:
            add(f"- PEG {_f(st.get('PEG'), 2)} · historical mean {_f(st.get('Mean'), 2)} · SD {_f(st.get('SD'), 2)} · "
                f"Dev {_f(dev, 2)} SD ({rel})")
        add(f"- 5Y ROI scenarios (annualised): Bull {_pct(st.get('Bull'))} · Base {_pct(st.get('Base'))} · "
            f"Bear {_pct(st.get('Bear'))}")
        math_line = _base_roi_math((data.get("_ai_inputs") or {}).get("row"))
        if math_line:
            add(f"- Base ROI math: {math_line}")
        if data.get("flagged"):
            add("- Screener risk flag: growth > 99%, PEG >= 2.5, no PEG deviation, no trailing PE or Base ROI < 9%")
    else:
        add(f"- No GARP valuation: {data.get('reason') or 'insufficient data'}")

    inc = data.get("income")
    add("")
    add(f"INCOME STATEMENT (year over year) · Grade {inc.get('grade', 'N/A') if inc else 'N/A'}")
    if inc:
        for i in inc.get("items", []):
            if _num(i.get("growth")):
                note = "" if i["label"] == "Revenue" else f" ({_SIGNAL_WORDS.get(i.get('signal'), 'n/a')})"
                add(f"- {i['label']}: {i['growth'] * 100:+.0f}%{note}")
    else:
        add("- unavailable")

    cr = data.get("credit")
    add("")
    add(f"BALANCE SHEET · Synthetic credit rating {cr.get('rating', 'NR') if cr else 'N/A'}")
    if cr:
        names = {"IntCov": "Interest coverage (x)", "ND/EBITDA": "Net debt / EBITDA", "Cash/Debt": "Cash / debt",
                 "Svc/FCF%": "Interest / free cash flow (%)"}
        for m in cr.get("metrics", []):
            add(f"- {names.get(m['label'], m['label'])}: {_f(m.get('value'))}")
    else:
        add("- unavailable")

    t = data.get("technicals")
    add("")
    add(f"TECHNICALS · {t.get('signal') if t else 'N/A'}")
    if t:
        z = t.get("accumulation_zone") or [None, None]
        add(f"- Trend {t.get('trend')} · RSI(14) {_f(t.get('rsi'), 0)} · price {_pct(t.get('price_vs_sma200'))} vs "
            f"SMA200 · ATR compression {_f(t.get('atr_compression'), 2)}")
        if _num(z[0]) and _num(z[1]):
            add(f"- Accumulation zone (SMA200 ± 1 ATR): {_money(z[0])} - {_money(z[1])}")

    lv = data.get("levels")
    add("")
    add("PRICE LEVELS TO WATCH")
    if lv:
        def lvl(x):
            p = x.get("p_touch_1m")
            return _money(x["price"]) + (f" (P(touch) 1M {p:.0f}%)" if _num(p) else "")
        none_up = f"none from the last 6 months' pivots; the next reference is the 52-week high {_money(lv.get('high_52w'))}"
        none_dn = f"none from the last 6 months' pivots; the next reference is the 52-week low {_money(lv.get('low_52w'))}"
        add(f"- Resistance, nearest first: {', '.join(lvl(x) for x in lv.get('resistance', [])) or none_up}")
        add(f"- Support, nearest first: {', '.join(lvl(x) for x in lv.get('support', [])) or none_dn}")
        if _num(lv.get("poc")):
            hvn = ", ".join(_money(h) for h in lv.get("hvn") or [])
            add(f"- Volume profile (3M): point of control {_money(lv['poc'])}" + (f"; high-volume nodes {hvn}" if hvn else ""))
        for key, label in (("1w", "1-week"), ("1m", "1-month")):
            r = (lv.get("ranges") or {}).get(key)
            if r:
                add(f"- {label} expected range: ±1σ {_money(r['1sigma_lower'])} - {_money(r['1sigma_upper'])}, "
                    f"±2σ {_money(r['2sigma_lower'])} - {_money(r['2sigma_upper'])} (1σ = {_f(r.get('move_pct'))}%)")
        add(f"- SMA50 {_money(lv.get('sma50'))} · SMA200 {_money(lv.get('sma200'))} · 52-week high "
            f"{_money(lv.get('high_52w'))} · low {_money(lv.get('low_52w'))} · realised vol {_f(lv.get('realized_vol_pct'))}%")
    else:
        add("- unavailable")

    e = data.get("edge")
    add("")
    add(f"6-MONTH DIRECTIONAL EDGE (vs {data.get('benchmark') or 'SPY'})")
    if e:
        add(f"- Bull signals: {_f(e.get('bull_acc'), 0)}% accurate ({e.get('bull_n')} signals), avg {_pct(e.get('bull_pnl'), 2)}")
        add(f"- Bear signals: {_f(e.get('bear_acc'), 0)}% accurate ({e.get('bear_n')} signals), avg {_pct(e.get('bear_pnl'), 2)}")
        add(f"- Stronger side: {e.get('best_edge') or 'none'}")
    else:
        add("- unavailable")
    return "\n".join(out)


def ai_block(ai, ai_state):
    if ai and ai.get("status") == "done":
        n = ai.get("narrative") or {}
        parts = []
        for key, title in (("overview", "Overview"), ("reverse_dcf", "Reverse DCF"), ("stomach_test", "Stomach test")):
            if n.get(key):
                parts.append(f"{title}: {n[key].strip()}")
        return "\n\n".join(parts) or "(the local model returned no usable text)"
    return {"pending": "(still being written by the local model; copy again in a minute to include it)",
            "disabled": "(AI overview is disabled in this portal)"}.get(ai_state, "(not available for this ticker)")


def build_deep_dive(data, ai=None, ai_state=None):
    """Returns the full prompt text for an analysed ticker (``data`` = the stored analysis result)."""
    sym = data.get("ticker", "?")
    name = data.get("name") or sym
    model = (ai or {}).get("model_short") or (ai or {}).get("model") or "local model"
    return "\n".join([
        ROLE.format(name=name, cashtag=f"${sym}"),
        "",
        f"=== LYNCH PIN DATA: ${sym} as of {_as_of(data)} ===",
        data_block(data),
        "",
        f"=== LOCAL AI OVERVIEW ({model}; a first draft, verify it) ===",
        ai_block(ai, ai_state),
        "",
        "=== NOTES ON THE DATA ===",
        NOTES,
    ])


# ── Portfolio Deep Dive Prompt (the Portfolio Analyzer, ui/portfolio.py) ──────────────────────────────────────────
PORTFOLIO_ROLE = """You are a senior portfolio manager at a long-only fundamental hedge fund. A client has brought \
their portfolio of {n} position{s} ({tickers}) to tomorrow's investment committee and wants your call: is it built \
to beat the S&P 500 over the next 3-5 years, and what should they keep, add to, trim or sell?

At the end of this prompt is a quantitative X-ray from our in-house GARP screener, "The Lynch Pin": the portfolio's \
market-value weights, its weighted valuation, growth, 5-year ROI scenarios and quality grades, then a full snapshot \
of every position (valuation against the stock's own PEG history, 5-year ROI scenarios, income statement and \
balance sheet grades, technicals, price levels to watch and a 6-month backtested directional edge), followed by a \
first-pass verdict from a small local AI model. Treat the X-ray as a starting point and the AI verdict as a junior \
analyst's draft: verify, don't trust. Only weights are given, never dollar amounts.

If you can browse or search the web, use it for every step below. Prefer primary sources (SEC filings, company \
investor relations, earnings call transcripts) and cite each fact with its source and date. If you cannot browse, \
say so up front, work from your own knowledge and flag anything that may be out of date.

## How to work

Work through the steps in order and don't write the verdict before step 7. The largest positions drive the \
result: give them the deepest work, and size your effort on each position by its weight.

1. Snapshot. Read the X-ray below. Note what the weighted numbers imply (cheap or expensive against history, what \
growth the price assumes and how it compares with the weighted trailing EPS trend, quality, how concentrated the \
portfolio is) and which positions drive them. The \
weighted PE ratios are harmonic means (total value / total earnings), the rest are weighted averages. Positions \
without GARP data still count for their weight in the allocation.
2. Each position, largest first. Read the last two quarterly earnings releases and call transcripts (and the \
latest 10-Q or 10-K) of every position above 5% of the portfolio; for the smaller ones the latest quarter and \
guidance are enough. Extract, with numbers: revenue and segment growth, margins, guidance and how it changed, free \
cash flow, buybacks, dilution, debt, and the material news of the last 6-12 months (litigation, regulation, \
management changes, competitive threats, insider selling). Note how management says it will handle each issue \
and whether the plan is credible.
3. Portfolio construction. Concentration (top position, top three, number of meaningful bets), sector, industry \
and geographic exposure, overlapping bets (several names riding the same theme, customer, supplier or cycle), how \
the positions are likely to move together, and sensitivity to rates, the economy, AI capex, consumer spending, \
currency and commodity prices. Name what is missing as well as what is doubled up.
4. Red flags and worrying trends, position by position: receivables or inventory growing faster than revenue, \
falling margins, spending without a visible return, rising leverage, heavy adjustments, guidance cuts, dilution, \
aggressive accounting, valuations that need a re-acceleration the history does not support.
5. Expected return. For each position judge whether the snapshot's Base ROI is realistic, then weight them into a \
portfolio expected return and compare it with the S&P 500's. State the S&P 500 return you assume and how you \
derived it.
6. Stress-test and loop. Write the strongest bull case and the strongest bear case for the portfolio as a whole. \
Check every claim against the evidence; if a claim is unsupported or a new question comes up, go back to steps \
2-5 for that point. Where your reading disagrees with the X-ray (for example a low PEG with deteriorating \
fundamentals), say which one you trust and why. Repeat until another pass would not change your conclusion, and \
report how many passes you made and what changed in each.
7. Report, in the format below.

## Report format

1. Verdict: is the portfolio likely to BEAT, MATCH or LAG the S&P 500 over 3-5 years, your conviction (low, medium \
or high) and the three reasons that decide it.
2. Position calls: a table with each position, its current weight, your call (ADD, HOLD, TRIM or SELL), a target \
weight and the one-line reason. Target weights add up to 100% and may include new positions or cash.
3. What the last quarters show for the largest positions: numbers, trends, guidance, issues and management's \
answers.
4. Portfolio construction: concentration, exposures, overlaps and the risks they create.
5. Red flags and worrying trends, most serious first, with the position they belong to.
6. Expected return: per position and weighted, against the S&P 500.
7. Rebalancing plan: what to do first, the price levels at which to add or trim each position (start from the \
levels in the snapshots and adjust them with your own reading), and replacements worth researching for anything \
you would sell.
8. Signals and news to watch: upcoming catalysts with dates and the metrics and thresholds that would confirm or \
break the thesis for each large position and for the portfolio.
9. What would change your mind, in either direction.

Open the report with the sources you read in full and those you only skimmed. Be specific and quantitative, use \
plain language and don't hedge the verdict."""

PORTFOLIO_NOTES = """- Weights = each position's market value / the portfolio's total (shares are valued at the \
analysis price). No dollar amounts are given.
- Weighted PE, Fwd PE and 2Y Fwd PE are harmonic weighted means (= total value / total earnings, the way index \
providers compute a fund's PE); unprofitable positions are left out and the other weights renormalised. PEG, the \
historical mean PEG, its SD and Dev (SD), 5Y growth (fwd: the consensus EPS forecast), 5Y trend (trl: the trailing \
EPS trend of up to 5 years, over the positions that have one) and the Bull / Base / Bear ROIs are arithmetic weighted \
means. \
Median PEG is weight-aware. Income grade and credit rating are weighted ordinal averages rounded to the nearest \
grade. Coverage is the share of the portfolio's weight that has GARP data."""


def portfolio_ai_block(ai, ai_state):
    if ai and ai.get("status") == "done":
        parts = [f"{label}: {ai[key].strip()}" for key, label in (("verdict", "Verdict"), ("bull", "Bull case"),
                                                                   ("bear", "Bear case")) if ai.get(key)]
        return "\n\n".join(parts) or (ai.get("text") or "(the local model returned no usable text)")
    return {"pending": "(still being written by the local model; copy again in a minute to include it)",
            "disabled": "(AI overview is disabled in this portal)"}.get(ai_state, "(not available for this portfolio)")


def build_portfolio_deep_dive(result, weights, entries, summary, ai=None, ai_state=None):
    """The Portfolio Deep Dive Prompt: a copy-paste brief on the whole portfolio for a stronger model. ``result`` is
    the X-ray (ui/portfolio.py), ``weights`` ticker → weight, ``entries`` ticker → that ticker's stored analysis."""
    syms = list(weights)
    as_of = result.get("day") or _dt.date.today().isoformat()
    model = (ai or {}).get("model") or "local model"
    lines = [
        PORTFOLIO_ROLE.format(n=len(syms), s="" if len(syms) == 1 else "s",
                              tickers=", ".join(f"${s} {weights[s] * 100:.1f}%" for s in syms)),
        "",
        f"=== LYNCH PIN PORTFOLIO X-RAY as of {as_of} ===",
        "Positions by weight: " + ", ".join(f"${s} {weights[s] * 100:.1f}%" for s in syms),
        "Weighted metrics:",
        summary,
    ]
    unvalued = [p for p in result.get("positions") or [] if p.get("weight") is None]
    if unvalued:
        lines.append("Not valued (left out of the weights): "
                     + ", ".join(f"${p['ticker']} ({p.get('reason') or 'no price'})" for p in unvalued))
    for i, sym in enumerate(syms, 1):
        lines += ["", f"--- POSITION {i}: ${sym} · {weights[sym] * 100:.1f}% of the portfolio ---"]
        entry = entries.get(sym)
        lines.append(data_block(entry) if entry else "- no analysis available")
    lines += ["", f"=== LOCAL AI PORTFOLIO VERDICT ({model}; a first draft, verify it) ===",
              portfolio_ai_block(ai, ai_state), "", "=== NOTES ON THE DATA ===", PORTFOLIO_NOTES, NOTES]
    return "\n".join(lines)
