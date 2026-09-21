# The Lynch Pin

A Peter Lynch-inspired **GARP (Growth at a Reasonable Price)** stock screener that calculates PEG ratios, historical valuation statistics, 5-year ROI projections and income statement grading — with optional AI narratives (Gemini), dark-mode charts, and automated X (Twitter) thread publishing.

## Project Structure

```
.
├── database/              # Ticker lists (one symbol per line)
│   ├── igv.txt            # iShares Expanded Tech-Software ETF
│   ├── mag7.txt           # Magnificent 7
│   ├── nasdaq_100.txt     # Nasdaq 100
│   ├── schd.txt           # Schwab US Dividend Equity ETF
│   └── smh.txt            # VanEck Semiconductor ETF
├── engine/
│   ├── lynch_pin_core.py           # Core GARP engine (PEG, SD, ROI projections)
│   ├── growth_estimator.py         # Multi-source 5Y EPS growth (Yahoo + FMP + fundamental cap)
│   ├── income_statement_grader.py  # Quant income statement waterfall grader
│   ├── balance_sheet_grader.py     # Synthetic credit rating (Damodaran methodology)
│   ├── technical_timing.py         # Technical trend, momentum, accumulation & 6M directional edge
│   ├── portfolio.py                # Holdings parser, market-value weights & weighted roll-ups
│   └── ai_research.py              # Gemini AI batch narrative generation
├── experimental/          # Quant trading research (order flow, IV surface, backtesting)
├── graphics/
│   └── visualizer.py      # Dark-mode benchmark, distribution & portfolio X-ray charts
├── social/
│   ├── x_publisher.py     # Threaded X (Twitter) publisher
│   └── threads_publisher.py # Threaded Threads (Meta) publisher
├── main.py                # CLI entry point
├── run_lynch.sh           # Automated daily scheduler (cron/launchd)
├── LICENSE                # MIT
└── .gitignore
```

## Usage

```bash
python main.py --src database/mag7.txt --top 5 --excl-bad --research --plot --post
python main.py --portfolio my_holdings.txt --research --plot --post
```

| Flag | Description |
|---|---|
| `--src` | Path to ticker file (default: `database/mag7.txt`) |
| `--top N` | Limit output to top N stocks by valuation deviation; also runs income statement grading |
| `--excl-bad` | Exclude risk-flagged (`*`) tickers, income grade < B, and credit rating < BBB |
| `--portfolio FILE` | Portfolio mode: analyze every holding in `FILE` (`TICKER, SHARES` per line) and roll up weighted metrics. Not compatible with `--top`, `--excl-bad`, `--weekly` |
| `--research` | Generate Gemini AI narratives per ticker |
| `--plot` | Output dark-mode charts to `tmp/` |
| `--post` | Publish full analysis thread to X |
| `--post_threads` | Publish full analysis thread to Threads |

## Environment Variables

| Variable | Required For |
|---|---|
| `FMP_API_KEY` | Multi-source growth enrichment (free: [financialmodelingprep.com](https://site.financialmodelingprep.com/register)) |
| `GEMINI_API_KEY` | `--research` / `--post` |
| `OPENROUTER_API_KEY` | Optional 3rd AI fallback tier (free: [openrouter.ai](https://openrouter.ai/keys)) |
| `META_API_KEY` (or `MODEL_API_KEY`) | Optional 4th, paid, last-resort AI tier ([dev.meta.ai](https://dev.meta.ai) pay-as-you-go key) |
| `X_API_KEY` | `--post` |
| `X_API_SECRET` | `--post` |
| `X_ACCESS_TOKEN` | `--post` |
| `X_ACCESS_SECRET` | `--post` |
| `THREADS_ACCESS_TOKEN` | `--post_threads` |
| `THREADS_USER_ID` | `--post_threads` |
| `GITHUB_IMAGE_PATH` | `--post_threads` (default: `https://raw.githubusercontent.com/duman190/the-lynch-pin/main/images`) |

## AI Fallback Chain

Narrative generation (`--research` / `--post`) walks a 4-layer fallback, 3 attempts per layer (`ATTEMPTS_PER_TIER`), so a busy or rate-limited model never kills a scheduled run:

| Tier | Model | Attempts |
|---|---|---|
| 1 | Best Gemini free model (`gemini-3.7-flash`) | 3 |
| 2 | Backup Gemini free model (`gemini-3.6-flash`) | 3 |
| 3 | OpenRouter [Free Models Router](https://openrouter.ai/openrouter/free) (`openrouter/free`) | 3 |
| 4 | Meta [Muse Spark 1.3 Contributor](https://dev.meta.ai) (`muse-spark-1.3-contributor`, **paid**) | 3 |

Transient errors (503 / 429 / `UNAVAILABLE` / `RESOURCE_EXHAUSTED`) are retried on the same tier with exponential backoff — 30s, then 60s, then 120s (`MAX_DELAY`) — so the three attempts span 3.5 minutes rather than sitting inside one congestion window; any other error skips straight to the next tier. The OpenRouter tier is the exception: `openrouter/free` is a random router, so a non-transient error from one draw (e.g. a model returning empty content) says nothing about the next, and the tier is simply re-rolled instead of abandoned. Tier 3 only joins the chain when `OPENROUTER_API_KEY` is set, Tier 4 only when `META_API_KEY` is set.

Tier 4 is the paid safety net and is reached only after both Gemini tiers and the free router have failed or produced unusable replies. *Muse Spark 1.3 Contributor* is the same model as Muse Spark 1.3 at up to 95% off ($0.10 / M input tokens, $0.20 / M output) in exchange for Meta using the inputs and outputs to train its models — so nothing sensitive goes into the prompt (it only ever contains public market data). One batch narrative is roughly 3K tokens in and 4K out, i.e. about a tenth of a cent. The tier is rate-limited by tokens; a 429 is treated as transient and retried with the same backoff. The call is a non-streaming POST to Meta's OpenAI-Responses-style endpoint (`https://api.meta.ai/v1/responses`), and only `output_text` parts of `message` items are read — `reasoning` items are discarded. Token usage is logged per call.

The small models the free router can land on sometimes copy the response template literally (`$TICKER: ARM` instead of `$ARM:`), use bare `ARM:` / markdown-bold headers, or drop the `SENTIMENT:` label. `LynchPinResearcher.normalize_narrative` rewrites those into the exact layout `main.py` parses, so a Tier 3 run still yields per-ticker replies instead of the generic placeholder. Well-formed Gemini output passes through unchanged.

The router can also hand the prompt to a model that is simply not up to it — a safety classifier answering `User Safety: safe`, a model that truncates or skips half the names. Every reply is therefore validated with `LynchPinResearcher.narrative_gaps` (a non-empty `SENTIMENT:` line plus a `$TICKER` block containing the 🤖 overview for every ticker). An unusable reply burns the attempt and is retried immediately — no backoff, since it is not a capacity problem, and on the free router the retry lands on a different model. If every attempt is rejected, the rejected reply covering the most tickers (`narrative_coverage`) is used — but only if it covers at least one; a reply with no ticker blocks at all is never posted, and the run falls through to the generic placeholders instead.

`openrouter/free` is OpenRouter's router that "selects free models at random from the models available on OpenRouter", smartly filtering for models that support the features the request needs. It costs nothing per token and has a 200K-token context window, so the full batch prompt fits comfortably. The request is streamed (`stream: true`) and only `delta.content` is collected — the `reasoning` deltas emitted by thinking models are discarded — and the model the router actually picked is logged.

## Portfolio Mode

`--portfolio FILE` turns the screener into a portfolio X-ray. The holdings file lists one position per line as `TICKER, SHARES` (comma, semicolon, tab or space separated; `#` comments and blank lines are ignored). A ticker may appear multiple times (e.g. separate lots) — repeats are de-duplicated and their share counts summed:

```
# my_holdings.txt
AAPL, 10
MSFT, 5
AAPL, 5      # second lot → AAPL = 15 shares
NVDA, 20
```

Every position gets the full deep analysis (PEG statistics, ROI projections, income grade, credit rating, technicals, 6M edge — the same treatment `--top` picks receive), then everything is rolled up by **market-value weight**:

```
weight_i = shares_i × current_price_i / Σ(shares × price)
```

Only weights are ever printed or plotted — never dollar values.

| Portfolio metric | Aggregation | Notes |
|---|---|---|
| **PE, Fwd PE, 2Y Fwd PE** | Harmonic weighted mean `1 / Σ(wᵢ / PEᵢ)` | Equals total value / total earnings (how index providers compute a fund's P/E). Unprofitable names (PE ≤ 0) are excluded and weights renormalised. |
| **PEG, Mean PEG, PEG SD, Dev(SD), 5Y Growth, Bull/Base/Bear ROI** | Arithmetic weighted mean `Σ(wᵢ × xᵢ)` | Per-position PEG SD is recovered from `\|PEG − Mean\| / \|Dev_SD\|`. |
| **Median PEG** | Weight-aware median | Smallest PEG whose cumulative weight reaches 50%. |
| **Income Grade, Credit Rating** | Letter → ordinal score → weighted mean → nearest letter | `A++…D` and `AAA…D` scales; `N/A`/`NR` positions are skipped. |

Positions whose GARP metrics cannot be computed (negative forward PE, no growth estimate) stay in the weight donut but are excluded from the roll-up; the summary reports the **coverage** (share of portfolio weight with valuation data).

**Output differences vs. a normal scan:**

- Terminal table gains a `Weight` column and is sorted by weight descending; a weighted summary block follows the grading/technicals tables.
- `--plot` produces `tmp/portfolio_allocation.png` instead of the index benchmark bar chart: a weight donut (small positions folded into *Other*), the weighted PEG and its deviation in the centre, a stats box mirroring the per-ticker chart (`Portfolio:` instead of `Ticker:`, plus median PEG, no technicals), weighted income grade / credit rating, and **QQQ + S&P 500 5Y CAGR** next to the portfolio's weighted base ROI. Per-position `TICKER_valuation.png` charts are still generated.
- `--post` publishes: a *Weekly Portfolio Update* main post carrying the X-ray chart, the AI's one-line portfolio verdict and a portfolio-level 🐂 Bull / 🐻 Bear thesis (weights live in the chart, so no position list); one reply per position in **descending weight order** (identical format to the daily scan, prefixed with `x% of portfolio`); and a closing post asking **@grok** whether the portfolio is well built and which positions are the best and worst.
- Positions without GARP data (e.g. no forward earnings) keep their weight in the pie and in the position count, and are named in the main post as *weight only*; they simply get no reply of their own.
- The Gemini prompt receives the weighted summary (including positions excluded for lack of GARP data) and each position's weight; it returns the portfolio verdict + Bull/Bear block in place of the sector sentiment. The per-ticker section of the prompt is unchanged from the index scan.

## 5Y EPS Growth Estimation

The growth estimate is the keystone of the entire PEG valuation framework. A single-source projection can be stale or driven by outlier analysts. The engine uses a multi-source consensus approach:

**Fast mode** (all tickers): Yahoo PEG-derived 5Y analyst consensus + fundamental cap validation.

**Enriched mode** (`--top N` with `FMP_API_KEY` set): adds Financial Modeling Prep 5Y forward EPS CAGR, then simple-averages all sources.

| Source | What it provides | When used |
|---|---|---|
| **Yahoo PEG** | `Forward PE / PEG Ratio` = implied 5Y EPS growth | Always |
| **FMP Analyst Estimates** | 5Y forward EPS CAGR from analyst consensus | Enrichment only |
| **Fundamental Cap** | Revenue CAGR + Margin Expansion + Buyback Rate (3Y trailing) | Ceiling validation |

**Blend logic:**
1. Simple average across all available 5Y sources
2. If the average exceeds 1.5× the fundamental cap → haircut: `avg × 0.6 + cap × 0.4`
3. Fallbacks (2Y analyst CAGR, trailing earnings growth) only used when no 5Y source is available

This prevents fantasy projections (e.g., TSLA 40% growth with 1% fundamental support) from making expensive stocks appear cheap, while trusting analyst consensus when it aligns with demonstrated performance.

## Historical Forward PEG Reconstruction

Historical PEG statistics (Mean, SD, deviation) compare today's PEG against where the stock has traded over 5 years. Free data sources only provide historical *trailing* EPS, which is contaminated by one-time gains (e.g., investment mark-ups) and low-base explosions (recently profitable companies show absurd trailing PEs). Naively dividing historical trailing PE by today's growth estimate inflates the historical mean and makes such stocks look falsely cheap (e.g., PLTR's mean PEG computed that way is ~6 vs ~1.8 in reality).

Instead, the engine **reconstructs the historical forward PEG** each month over 5 years ("Method B2"):

| Component | Formula | Rationale |
|---|---|---|
| **Forward EPS proxy** | `fwd_eps_now × √(rev(t)/rev(now) × eps(t)/eps(now))` | Walks today's forward consensus back in time. Revenue ratio = business scale (immune to one-time gains); EPS ratio = margin/share-count trajectory. The geometric mean halves the impact of one-time items in trailing EPS. |
| **Blended growth** | `max((k × realized_rev_CAGR + (5−k) × proj_5Y) / 5, 4%)` | k years back, an investor's 5Y view covered k years that have since happened (realized revenue CAGR, clean data) plus (5−k) years still ahead (today's projection). |
| **PEG(t)** | `(price(t) / fwd_eps_proxy(t)) / blended_growth(t)` | Historical forward PEG as the market would have seen it. |

The **4% growth floor** prevents denominator blow-ups for mature/shrinking businesses whose realized revenue CAGR is near zero or negative (e.g., dividend names in SCHD).

TTM revenue comes from SEC EDGAR quarterly filings (same companyfacts download as quarterly EPS, most-recently-filed value per period). When revenue data is unavailable (some banks, missing CIK), the engine falls back to the legacy trailing-PE-based series, then to PE volatility.

Validated against ~200 tickers (Nasdaq 100 + SCHD): the reconstructed forward EPS proxy lands within ±26% median error of the EPS actually realized 12 months later, and corrects 3–8× mean-PEG distortions for names with distorted trailing EPS history (PLTR, AMD, WDAY, MRK, PANW, AXON) while leaving stable compounders (PEP, KO) unchanged.

## Balance Sheet Credit Rating

Assigns a synthetic S&P-style credit rating (AAA → D) using [Damodaran's interest coverage methodology](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/syntrating.htm), adjusted by leverage and liquidity metrics.

| Metric | Formula | Interpretation |
|---|---|---|
| **IntCov** | Operating Income / Interest Expense (TTM) | How many times over a company can pay its interest. Higher = safer. |
| **ND/EBITDA** | (Total Debt − Cash) / EBITDA (TTM) | Years to repay net debt from earnings. Negative = net cash position. |
| **Cash/Debt** | Cash & Equivalents / Total Debt | Liquidity buffer. >1 means more cash than debt on hand. |
| **Svc/FCF%** | Interest Expense / Free Cash Flow × 100 (TTM) | What % of free cash flow is consumed by debt service. Lower = better. |

The primary rating is derived from the interest coverage ratio (Damodaran's published lookup table), then adjusted ±1-2 notches based on the secondary metrics. The AI narrative incorporates the credit rating when assessing risk.

## Technical Timing

Computes trend, momentum, and accumulation signals from 1-year daily price history to identify favorable entry points for top picks.

| Signal | Condition | Interpretation |
|---|---|---|
| **BULLISH** | Price > EMA50 > SMA200 | Strong uptrend, momentum confirmed. |
| **NEUTRAL** | Price > SMA200 but EMA50 < SMA200 | Above long-term support but trend not fully confirmed. |
| **BEARISH** | Price < SMA200 | Below long-term support, caution warranted. |
| **ACCUMULATION** | Non-bearish + (RSI < 45 or near SMA200) + ATR compression | Low-volatility consolidation near support — ideal entry window. |

| Metric | Formula | Interpretation |
|---|---|---|
| **RSI** | 14-period Relative Strength Index | Momentum oscillator (30=oversold, 70=overbought). |
| **SMA200** | Price vs 200-day Simple Moving Average (%) | Distance from long-term trend. Positive = above support. |
| **ATR Compression** | Current ATR / 3-month average ATR | <1 = volatility contracting (coiling for a move). |
| **Accumulation Zone** | SMA200 ± 1 ATR | Price band around long-term support — natural entry range. |

The accumulation zone and signal are displayed on per-ticker charts and fed to the AI narrative for entry timing context.

## 6M Directional Edge

Runs a 180-day backtest per ticker using order flow intensity, IV-derived expected moves, trend/regime detection, and relative strength scoring to determine which direction (bull or bear) has a statistical edge.

```
📉 TECHNICAL TIMING + 6M DIRECTIONAL EDGE
────────────────────────────────────────────────────────────────────────────────────────────────────────
  Ticker Signal        RSI  SMA200   ATR   Accum Zone  Bull Acc  Bull P&L  Bear Acc  Bear P&L   Edge
────────────────────────────────────────────────────────────────────────────────────────────────────────
  MSFT   BEARISH        60     -8%  0.96    $422-$445    73%(22)   +4.02%    47%(68)   +0.44%   BULL
  META   BEARISH        48     -7%  1.27    $610-$662    38%(37)   -0.68%    53%(70)   -0.54%   BEAR
  KLAC   NEUTRAL        33    +17%  1.03    $170-$195    63%(92)   +2.52%    67%(30)   +3.57%   BEAR
  MU     NEUTRAL        39    +60%  1.06     $85-$100    66%(108)  +4.46%    47%(32)   -4.22%   BULL
────────────────────────────────────────────────────────────────────────────────────────────────────────
  💡 BULL edge → sell cash-secured puts on dips | BEAR edge → sell covered calls on bounces
```

**Options income application:**

| Edge Direction | Strategy | Rationale |
|---|---|---|
| **BULL** (>60% acc) | Sell cash-secured puts on dips | Stock statistically moves up — put expires worthless or you get assigned at a great entry |
| **BEAR** (>60% acc) | Sell covered calls on bounces | Stock statistically moves down — call expires worthless, you collect premium on existing long position |
| **Neither** (<55%) | No options income | Low-conviction — edge is not reliable enough to sell premium against |

The directional edge is incorporated into the AI narrative, displayed on per-ticker charts, and printed in the technical timing table for `--top` picks.

## 5Y ROI Projections

Projects annualized 5-year returns under three scenarios (Bull, Base, Bear) using a **terminal multiple framework** that accounts for growth deceleration.

**Terminal Growth Decay** — higher current growth rates receive more aggressive deceleration assumptions:

| Current Growth | Decay Exponent | Example: 40% → Terminal |
|---|---|---|
| < 20% | 1.0 (no decay) | 15% → 15% |
| 20–30% | 0.95 | 25% → 21.3% |
| 30–50% | 0.90 | 40% → 27.7% |
| 50%+ | 0.85 | 60% → 32.5% |

**Terminal PEG** — the multiple assigned at maturity:

| Growth Regime | Terminal PEG Formula |
|---|---|
| Mature (< 20%) | `min(2.5, 28 / growth, mean_peg)` — i.e. mean PEG capped at 2.5 **and** at a 28x terminal PE |
| High-growth (20%+) | `min(mean_peg, max(0.8, 1.5 - 0.5 × (growth/30 - 1)))` |

The **28x mature terminal PE cap** exists because mature names get no growth decay, so terminal PE = PEG × growth, and a stock whose reconstructed PEG history is inflated by years of depressed earnings (AMZN's capex build-out: mean PEG 2.9 → 40x; ISRG, NTNX, WSO: 45–48x) would otherwise be assigned a maturity multiple far above peers with the same growth (MSFT 22x, GOOG 28x). 28x is where the high-growth formula lands at exactly 20% growth (1.67 PEG × 17.2% decayed growth ≈ 28.7x), so the terminal PE is now continuous across the regime boundary, and it sits at roughly the 90th percentile of terminal PEs across Nasdaq 100 + IGV + SMH + SCHD. The cap only binds when `growth > 11.2%` (below that `28 / growth > 2.5`), so low-growth compounders whose PEG is structurally high (AAPL, COST, KO) are unaffected, and names whose own history already implies < 28x (MSFT, GOOG) are unchanged. In a 267-ticker scan it re-rated 32 names, all mature stocks previously assigned 30–50x.

**ROI Scenarios:**

| Scenario | PEG Used (current PEG ≤ historical mean) | Interpretation |
|---|---|---|
| **Bull** | `terminal_peg + 0.5 × SD` | Market re-rates above mean — multiple expansion. |
| **Base** | `terminal_peg` | Mean reversion — fair value at maturity. |
| **Bear** | `max(0.5, min(curr_peg, terminal_peg - 0.5 × SD))` | No re-rating or compression — market stays skeptical. |

When the stock already trades **above** its historical mean PEG, mean reversion would assume a *de-rating* — and for names whose reconstructed history is distorted (e.g. a memory cyclical whose trough EPS drags the 5Y mean PEG toward zero, MU) it produces absurd terminal multiples. In that case the scenarios anchor on **today's multiple holding** instead (the growth-regime cap on the terminal PEG still applies):

| Scenario | PEG Used (current PEG > historical mean) | Interpretation |
|---|---|---|
| **Bull** | `min(curr_peg, cap)` | Today's multiple holds through maturity. |
| **Base** | `bull − 0.5 × SD` (≥ 50% of bull) | Mild compression from today's level. |
| **Bear** | `bull − 1.0 × SD` (≥ 25% of bull) | Meaningful compression — market cools on the story. |

The AI prompt's "Base ROI math" line uses the same scenario logic, so the implied terminal PE it cites always matches the Base ROI shown.

**Final formula:** `ROI = ((terminal_peg × terminal_growth × projected_EPS) / current_price) ^ (1/5) - 1`

**EPS Base Selection** — the projection base uses forward EPS to reflect the market's current pricing of near-term earnings trajectory (e.g., AMD's AI shift). Falls back to trailing EPS only when forward EPS is unavailable or negative (temporary headwinds).

This prevents hypergrowth companies (LYFT, CELH) from producing fantasy ROIs by capping terminal PE at realistic levels (~32–39x), while leaving mature compounders (MSFT, PEP) unchanged.

## Testing

```bash
python -m pytest test_unit.py -v
```

Unit tests covering all modules:

| Module | Coverage |
|---|---|
| `engine/lynch_pin_core.py` | Growth derivation, PEG statistics, PE volatility fallback |
| `engine/growth_estimator.py` | Yahoo/FMP blend, fundamental cap, fallback logic, rate limiting |
| `engine/income_statement_grader.py` | YoY growth, item grading, letter grade assignment |
| `engine/balance_sheet_grader.py` | Coverage-to-score mapping, notch adjustments |
| `engine/technical_timing.py` | Trend detection, RSI, ATR compression, accumulation zone, signal labels, 6M directional edge |
| `engine/ai_research.py` | Prompt building (index & portfolio modes), format helpers, ticker parsing, 3-tier AI fallback chain (Gemini best → Gemini backup → OpenRouter free) |
| `engine/portfolio.py` | Holdings parsing (dedupe/sum, malformed lines), market-value weights, harmonic/arithmetic roll-ups, weighted median, weighted grades |
| `graphics/visualizer.py` | Benchmark resolution, output directory creation, portfolio X-ray plot, slice grouping |
| `social/x_publisher.py` | Media upload, retry logic, tweet creation |
| `social/threads_publisher.py` | Truncation, container creation, threading, topic tags |
| `main.py` | Sentiment parsing, ticker regex, cashtag removal, IDX mapping, portfolio helpers (price extraction, weight sort, flag exclusivity, Grok closer) |

## Dependencies

- `yfinance`, `curl_cffi` — market data
- `pandas`, `numpy`, `scipy` — analysis
- `matplotlib` — charting
- `google-genai` — Gemini AI
- `tweepy` — X API
- `requests` — Threads API (Meta Graph API), OpenRouter API
- `pytest` — testing

## License

MIT — see [LICENSE](LICENSE).
