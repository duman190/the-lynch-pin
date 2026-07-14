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
│   ├── income_statement_grader.py  # Quant income statement waterfall grader
│   ├── balance_sheet_grader.py     # Synthetic credit rating (Damodaran methodology)
│   └── ai_research.py              # Gemini AI batch narrative generation
├── graphics/
│   └── visualizer.py      # Dark-mode benchmark & distribution charts
├── social/
│   └── x_publisher.py     # Threaded X (Twitter) publisher
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

## Environment Variables

| Variable | Required For |
|---|---|
| `GEMINI_API_KEY` | `--research` / `--post` |
| `X_API_KEY` | `--post` |
| `X_API_SECRET` | `--post` |
| `X_ACCESS_TOKEN` | `--post` |
| `X_ACCESS_SECRET` | `--post` |

## Balance Sheet Credit Rating

Assigns a synthetic S&P-style credit rating (AAA → D) using [Damodaran's interest coverage methodology](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/valquestions/syntrating.htm), adjusted by leverage and liquidity metrics.

| Metric | Formula | Interpretation |
|---|---|---|
| **IntCov** | Operating Income / Interest Expense (TTM) | How many times over a company can pay its interest. Higher = safer. |
| **ND/EBITDA** | (Total Debt − Cash) / EBITDA (TTM) | Years to repay net debt from earnings. Negative = net cash position. |
| **Cash/Debt** | Cash & Equivalents / Total Debt | Liquidity buffer. >1 means more cash than debt on hand. |
| **Svc/FCF%** | Interest Expense / Free Cash Flow × 100 (TTM) | What % of free cash flow is consumed by debt service. Lower = better. |

The primary rating is derived from the interest coverage ratio (Damodaran's published lookup table), then adjusted ±1-2 notches based on the secondary metrics. The AI narrative incorporates the credit rating when assessing risk.

## Dependencies

- `yfinance`, `curl_cffi` — market data
- `pandas`, `numpy`, `scipy` — analysis
- `matplotlib` — charting
- `google-genai` — Gemini AI
- `tweepy` — X API

## License

MIT — see [LICENSE](LICENSE).
