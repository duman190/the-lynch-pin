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
│   ├── technical_timing.py         # Technical trend, momentum & accumulation signals
│   └── ai_research.py              # Gemini AI batch narrative generation
├── graphics/
│   └── visualizer.py      # Dark-mode benchmark & distribution charts
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
```

| Flag | Description |
|---|---|
| `--src` | Path to ticker file (default: `database/mag7.txt`) |
| `--top N` | Limit output to top N stocks by valuation deviation; also runs income statement grading |
| `--excl-bad` | Exclude risk-flagged (`*`) tickers, income grade < B, and credit rating < BBB |
| `--research` | Generate Gemini AI narratives per ticker |
| `--plot` | Output dark-mode charts to `tmp/` |
| `--post` | Publish full analysis thread to X |
| `--post_threads` | Publish full analysis thread to Threads |

## Environment Variables

| Variable | Required For |
|---|---|
| `FMP_API_KEY` | Multi-source growth enrichment (free: [financialmodelingprep.com](https://site.financialmodelingprep.com/register)) |
| `GEMINI_API_KEY` | `--research` / `--post` |
| `X_API_KEY` | `--post` |
| `X_API_SECRET` | `--post` |
| `X_ACCESS_TOKEN` | `--post` |
| `X_ACCESS_SECRET` | `--post` |
| `THREADS_ACCESS_TOKEN` | `--post_threads` |
| `THREADS_USER_ID` | `--post_threads` |
| `GITHUB_IMAGE_PATH` | `--post_threads` (default: `https://raw.githubusercontent.com/duman190/the-lynch-pin/main/images`) |

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
| Mature (< 20%) | `min(2.5, mean_peg)` |
| High-growth (20%+) | `min(mean_peg, max(0.8, 1.5 - 0.5 × (growth/30 - 1)))` |

**ROI Scenarios:**

| Scenario | PEG Used | Interpretation |
|---|---|---|
| **Bull** | `terminal_peg + 0.5 × SD` | Market re-rates above mean — multiple expansion. |
| **Base** | `terminal_peg` | Mean reversion — fair value at maturity. |
| **Bear** | `max(0.5, min(curr_peg, terminal_peg - 0.5 × SD))` | No re-rating or compression — market stays skeptical. |

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
| `engine/technical_timing.py` | Trend detection, RSI, ATR compression, accumulation zone, signal labels |
| `engine/ai_research.py` | Prompt building, format helpers, ticker parsing |
| `graphics/visualizer.py` | Benchmark resolution, output directory creation |
| `social/x_publisher.py` | Media upload, retry logic, tweet creation |
| `social/threads_publisher.py` | Truncation, container creation, threading, topic tags |
| `main.py` | Sentiment parsing, ticker regex, cashtag removal, IDX mapping |

## Dependencies

- `yfinance`, `curl_cffi` — market data
- `pandas`, `numpy`, `scipy` — analysis
- `matplotlib` — charting
- `google-genai` — Gemini AI
- `tweepy` — X API
- `requests` — Threads API (Meta Graph API)
- `pytest` — testing

## License

MIT — see [LICENSE](LICENSE).
