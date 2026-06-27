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
│   ├── smh.txt            # VanEck Semiconductor ETF
│   └── fintwit_100.txt    # Fallback: 100 most popular FinTwit tickers
├── engine/
│   ├── lynch_pin_core.py           # Core GARP engine (PEG, SD, ROI projections)
│   ├── income_statement_grader.py  # Quant income statement waterfall grader
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
python main.py --weekly --top 10 --excl-bad --post
```

| Flag | Description |
|---|---|
| `--src` | Path to ticker file (default: `database/mag7.txt`) |
| `--top N` | Limit output to top N stocks by valuation deviation; also runs income statement grading |
| `--excl-bad` | Exclude risk-flagged (`*`) tickers |
| `--research` | Generate Gemini AI narratives per ticker |
| `--plot` | Output dark-mode charts to `tmp/` |
| `--post` | Publish full analysis thread to X |
| `--weekly` | Weekly scan: AI-sourced top 100 FinTwit trending tickers, benchmarked against SPY |

## Environment Variables

| Variable | Required For |
|---|---|
| `GEMINI_API_KEY` | `--research` / `--post` |
| `X_API_KEY` | `--post` |
| `X_API_SECRET` | `--post` |
| `X_ACCESS_TOKEN` | `--post` |
| `X_ACCESS_SECRET` | `--post` |

## Dependencies

- `yfinance`, `curl_cffi` — market data
- `pandas`, `numpy`, `scipy` — analysis
- `matplotlib` — charting
- `google-genai` — Gemini AI
- `tweepy` — X API

## License

MIT — see [LICENSE](LICENSE).
