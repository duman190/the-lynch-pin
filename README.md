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
| `--excl-bad` | Exclude risk-flagged (`*`) tickers |
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
