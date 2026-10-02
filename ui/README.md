# Lynch Pin Quant Portal (web UI)

Dark-mode web UI for the Lynch Pin engine, for PC and phone. Everything lives in `ui/`; no file outside it is modified. It uses only the standard library plus the project's existing dependencies.

```bash
python -m ui.server                  # http://127.0.0.1:8765, this machine only
python -m ui.server --lan            # phones/PCs on the same private network (prints the URL to open)
python -m pytest ui/tests -q         # offline tests (fake engine + fake LM Studio), incl. a quick benchmark
python ui/tests/test_benchmark.py    # throughput benchmark: req/s and latency per endpoint (AI off)
python ui/tests/cold_bench.py --base http://127.0.0.1:8765 --clients 24 --count 48   # cold lookups/min (real Yahoo)
python -m ui.assets.make_hero        # re-render the artwork from tmp/x_logo.jpeg + tmp/x_banner.png
```

**Security:** `--lan` listens on all interfaces **without authentication**, so every device on your Wi-Fi or your Tailscale tailnet can use the portal. The portal is read-only. Clients outside loopback, RFC 1918, ULA, link-local and Tailscale (`100.64.0.0/10`) ranges get a 403, and so do requests with a foreign `Host` header, which blocks DNS rebinding. The server log says why a request was refused. Do not port-forward it to the internet.

## Features
- Hero artwork built from the Lynch Pin badge.
- Ticker search at `?t=MSFT`, which you can bookmark and share. It shows valuation (PEG, Dev SD bell, 5Y Bull/Base/Bear ROI), the same chart `main.py` renders, the income grade, the credit rating, technicals and the 6M edge. Results stream in stage by stage.
- An AI overview from a local LM Studio server that types in real time (Server-Sent Events), with live time to first token, tokens/s, token count and thinking tokens; a reasoning model's thinking streams into a collapsible box. The prompt reuses the daily scan's DATASET block but asks for three one-paragraph sections for this ticker only (no sentiment line, no character limits). When no model is loaded, the UI shows "AI offline" and keeps working.
- **Quick Overview** with `--no-ai`: the AI overview's three sections built by fixed rules from the same data, no model involved (`ui/quick.py`). *Overview*: name, sector, HQ, employees, market cap, Yahoo's business summary, margins, dividend, analyst consensus and a valuation snapshot. *Reverse 5Y DCF*: "X% base ROI requires EPS to compound at Y%/yr for 5 years and a re-rating from Zx forward PE to a terminal ZZx" (the daily scan's Base ROI math), split into the EPS and multiple contributions, with how demanding those assumptions are. *Stomach test*: red flags from thresholds such as trailing PE > 50, forward PE > 40, growth > 40%, PEG ≥ 2.5, PEG > 1 SD above its mean, base ROI < 9%, a losing bear case, shrinking revenue, a C/D income grade, junk credit, negative free cash flow, an uncovered dividend, a falling trend, high beta or short interest.
- With `--no-ai` there is no **↻ Refresh**: a cached ticker stays cached until midnight (or eviction), so repeat lookups never spend Yahoo calls again. Uncacheable results (errors, a Yahoo 429, a missing PEG history) still re-run when searched again.
- **Price levels** in the Technicals card, computed with the experimental trade assistant's toolkit (`experimental/quant_engine.py`): support and resistance from clustered pivots (6 months), the volume point of control (3 months), a 1-month expected range from realised volatility, and the 52-week range. Each level shows its chance of being touched within a month.
- **Deep Dive Prompt** (above *How to read*): a research brief to paste into Claude, ChatGPT or Gemini. It casts the model as a hedge-fund portfolio manager and walks it through the last two earnings reports, call transcripts, the 10-Q, news, management's answers and red flags. It then makes the model loop, stress-testing its bull and bear case until another pass changes nothing, and asks for a verdict against the S&P 500 with price levels and signals to watch. The quant data, the price levels and the local AI overview are attached at the end. **Copy** works on plain-http LAN and Tailscale URLs too; **Show / Hide** toggles the full text.
- The 5Y Growth value is tagged **Enriched** (Yahoo + FMP) or **Not enriched** (Yahoo only).
- **Concurrent lookups** with the AI overview off: several users' tickers are analysed at the same time, each in its own worker process (1.5 per CPU core, at most 16, and no more than free RAM holds at ~200 MB each; `--workers` to change). A model loaded in LM Studio can take most of an 8 GB Mac: unload it when running `--no-ai`. On an 8-core M1 this takes cold lookups from ~11 to ~117 tickers/min (`ui/tests/cold_bench.py`). With the AI overview on, tickers are analysed one at a time unless `--workers` is set. Yahoo throttles at a few hundred tickers in a short burst (each cold lookup makes ~12 Yahoo calls), so the cache below does the heavy lifting under sustained load.
- A daily LFU cache of 500 tickers (~10 MB of RAM, ~280 MB of charts on disk). Typing a ticker again the same day skips the quant pipeline, the chart and the LLM. The cache and old charts are cleared at the first access after midnight.

## Configuration (CLI flag or env var)
| Flag | Env | Default |
|---|---|---|
| `--port` | `LYNCH_UI_PORT` | `8765` |
| `--lan` | `LYNCH_UI_LAN=1` | off (loopback) |
| `--llm-url` | `LYNCH_LLM_BASE_URL` | `http://127.0.0.1:1234` (LM Studio default) |
| `--llm-model` | `LYNCH_LLM_MODEL` | *(auto: first loaded model)* |
| `--llm-ctx` | `LYNCH_LLM_CTX` | `65536` |
| `--llm-max-tokens` | `LYNCH_LLM_MAX_TOKENS` | `8192` (includes thinking tokens) |
| `--llm-reasoning` | `LYNCH_LLM_REASONING` | `off` (sends `reasoning_effort: "none"`; `on` keeps the model's thinking) |
| `--llm-autoload` | `LYNCH_LLM_AUTOLOAD=1` | off (asks LM Studio to load the model with `--llm-ctx`) |
| `--cache-size` | `LYNCH_UI_CACHE_SIZE` | `500` |
| `--benchmark` | `LYNCH_UI_BENCHMARK` | `SPY` (6M edge) |
| `--no-ai` | | AI enabled |
| `--workers` | `LYNCH_UI_WORKERS` | `0` = auto: 1 with the AI overview, 1.5 × CPU cores (max 16, capped by free RAM at ~200 MB each) without; above 1, one process each |
| `--enrich` | `LYNCH_UI_ENRICH` | `auto`: FMP multi-source growth when `FMP_API_KEY` is set (`on` / `off` to force) |
| `--allow-net` / `--allow-host` | `LYNCH_UI_ALLOWED_NETS` / `LYNCH_UI_ALLOWED_HOSTS` | none: extra client networks / Host names beyond LAN + Tailscale |

The LLM context window is the smaller of `--llm-ctx` and the loaded model's real context (from LM Studio's `/api/v0/models`). To try the AI card without a model, run `python -m ui.tests.fake_lmstudio --port 18080` (streams a canned reply; `--delay` sets the typing speed) and start the server with `--llm-url http://127.0.0.1:18080`.
