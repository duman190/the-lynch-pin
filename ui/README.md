# Lynch Pin Quant Portal (web UI)

Dark-mode web UI for the Lynch Pin engine, for PC and phone. Everything lives in `ui/`; outside it, the portal only reads the scan archive that `main.py` writes to `scans/<kind>/` (see *Latest scans*). It uses only the standard library plus the project's existing dependencies.

```bash
(cd ui/native && cargo build --release)  # once: the native chart renderer + trading view bars (Rust from rustup.rs; optional, see *Native chart renderer*)
python -m ui.server                  # http://127.0.0.1:8765, this machine only
python -m ui.server --lan            # phones/PCs on the same private network (prints the URL to open)
                                     #   + the stats page on port 190 (see *Stats page*)
python -m ui.server --public --no-ai # behind a Cloudflare Tunnel; the stats page on port 190, LAN / Tailscale only
python -m pytest ui/tests -q         # offline tests (fake engine + fake LM Studio), incl. a quick benchmark
python ui/tests/test_benchmark.py    # throughput benchmark: req/s and latency per endpoint (AI off)
python ui/tests/cold_bench.py --base http://127.0.0.1:8765 --clients 24 --count 48   # cold lookups/min (real Yahoo)
python ui/tests/cold_bench.py --base http://127.0.0.1:8790 --clients 24 --count 48 --visitors --home   # same, as --public visitors
python ui/tests/inference_bench.py --llm-url http://HOST:8000 --llm-parallel 4 --minutes 1   # AI overviews: local + Gemini at 50/min
python ui/tests/chart_bench.py --matplotlib   # per-ticker chart: native vs matplotlib, one at a time and from 8 threads
python ui/tests/tv_bench.py record && python ui/tests/tv_bench.py offline [--python]   # trading view: server capacity (no Yahoo)
python ui/tests/tv_bench.py live --viewers 0,100,200,300,0   # trading view: cold lookups/min with N chart viewers (real Yahoo)
python -m ui.assets.make_hero        # re-render the artwork from tmp/x_logo.jpeg + tmp/x_banner.png
```

**Security:** `--lan` listens on all interfaces **without authentication**, so every device on your Wi-Fi or your Tailscale tailnet can use the portal. The portal is read-only. Clients outside loopback, RFC 1918, ULA, link-local and Tailscale (`100.64.0.0/10`) ranges get a 403, and so do requests with a foreign `Host` header, which blocks DNS rebinding. The server log says why a request was refused. Do not port-forward it to the internet; use `--public` behind a tunnel instead (below).

**Public access (`--public`):** run a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) on this machine and point it at the portal only:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: lynch.example.com
    service: http://localhost:8765
  - service: http_status:404      # nothing else on this machine is reachable
```

```bash
python -m ui.server --public --no-ai   # then: cloudflared tunnel run
```

`--public` listens on 127.0.0.1 only (it refuses `--lan` or another `--host`), so the tunnel is the only way in and no router port is opened. The one exception is the stats page on port 190 (see *Stats page*): it listens on all interfaces but answers LAN and Tailscale clients only, never the tunnel (`--no-stats` keeps everything on loopback). It accepts any `Host` name, reads each visitor's IP from Cloudflare's `CF-Connecting-IP` header (trusted only from the local tunnel), allows **one analysis at a time per visitor** (a second ticker gets "busy" and the page retries; cached tickers, polls and joining a ticker someone else is already analysing are free), turns `/api/cache` off and leaves other visitors' tickers, the LAN setting and the local model's address out of `/api/health`. What the portal can serve stays the same as on the LAN: its own pages, charts by ticker and analysis JSON — never other files on this machine. There is still no login: put Cloudflare Access in front of the hostname to choose who can use it. Visitors' lookups use this machine's Yahoo quota (and FMP's, with enrichment on).

## Features
- Hero artwork built from the Lynch Pin badge.
- **US stock market valuation** (home page, between the ticker search and Latest scans): two S&P 500 charts drawn in the Lynch Pin chart style (`graphics/market_valuation.py`), each tap-to-enlarge with a download button, and Peter Lynch on overvalued markets under them.
  - **Rear view mirror:** the Shiller PE (CAPE), monthly since 1881, with its mean, its percentile and the peaks before Black Tuesday, Black Monday and the dot-com crash. It is Robert Shiller's data as [multpl.com](https://www.multpl.com/shiller-pe/table/by-month) publishes it, extended to the current month: one request a day.
  - **Forward looking:** the S&P 500 forward PEG, with Peter Lynch's fair value (1.0), bear markets shaded and the 2000 / 2007 / 2020 / 2022 peaks marked.
    - **1995 – Jan 2026:** Yardeni Research's monthly series (forward P/E over I/B/E/S's long-term growth consensus, which is paid data and not public), traced from Yahoo Finance's chart *S&P 500 PEG Ratio: A Cheaper Market?* and hard-coded in `ui/sp500_peg_history.py`, a line per year (about ±0.03; the chart's labelled peaks and its last point, 0.70, are its own numbers).
    - **After that:** the Lynch Pin's own point each week, computed on Sunday morning from Friday's close: every constituent's PEG from Yahoo (*PEG Ratio (5yr expected)*), weighted by market cap as index PEG = Σ cap / Σ (cap / PEG). That is the index's P/E over the growth of its total earnings, Yardeni's definition, whatever P/E Yahoo's PEG is built on; a plain cap-weighted mean of PEGs would let slow growers swamp it (two equal-size companies with PEGs of 1 and 5 average 3.0, while the index's PEG is 1.67). Constituents without a PEG (losses, no growth estimate, a PEG outside 0.1-50) are left out; each point records how many names and what share of the index's cap it covers (the chart's footnote), and needs 80% of the cap. The two sources are drawn as separate runs, so a gap or a jump between them shows where one ends and the other begins.
    - **The sweep:** `database/sp500.txt` (one Yahoo ticker per company: GOOGL without GOOG, FOXA without FOX, NWSA without NWS), one constituent at a time, Yahoo's quote and PEG (2 requests each, half a second apart, ~15 min). It waits while the portal's Yahoo circuit breaker is open and pauses on a 429 (1 min, doubling to 15 min; it gives up after 6 in a row and is retried 3 hours later).
  - **Schedule:** a background thread fetches the Shiller PE each weekday at 6 PM Pacific (`--valuation-at HH:MM`, `LYNCH_UI_VALUATION_TZ`: after the close and the 1 PM scan), and sweeps the S&P 500 each Sunday at 8 AM Pacific (`PEG_DAY` / `PEG_AT` in `ui/valuation.py`), when markets are shut and the portal is quiet; that point is dated Friday's close. On startup it runs whatever was missed (a late sweep is dated the last weekday's close). Page views only read `ui/.cache/valuation/` (`shiller.json`, `peg.json` with the PEG history, and the charts, redrawn at each startup). A failed run keeps the last chart and is retried later (1 h for the Shiller PE, 3 h for the sweep). `--no-valuation` turns the section off.
  - The S&P 500 list changes a few names a quarter; refresh `database/sp500.txt` now and then (it was built from [datasets/s-and-p-500-companies](https://github.com/datasets/s-and-p-500-companies), de-duplicated by CIK, with Yahoo's `BRK-B` / `BF-B`).
- **Latest scans** (home page, under the ticker search and the market valuation): an iOS-style widget for each of the last 7 daily `run_lynch.sh` scans (MAGS, QQQ, SCHD, SMH, IGV, X Favorite 100, Portfolio X-Ray), oldest to newest, opening on the newest. Each widget previews the scan's first post and chart; swipe (phone), use the ‹ › arrows, or tap a day in the date strip above to switch scans. Tapping a widget opens the whole thread at `?scan=mags`, laid out like the X post, with each reply's chart (tap to enlarge) and an *Analyze $TICKER* shortcut; ‹ › links at the bottom step to the neighbouring days.
  - **Storage:** with `--post` / `--post_threads`, `main.py` archives the thread in its own folder per scan kind: `scans/<kind>/scan.json` (every post's text plus the run's raw AI overview: sentiment, portfolio thesis, each ticker's untruncated overview) and that run's charts. Monday's `scans/mags/GOOGL_valuation.png` is never overwritten by Saturday's `scans/fintwit/GOOGL_valuation.png`; the next MAGS run replaces only `scans/mags/`. `scans/` is local and git-ignored; `images/` stays the Threads upload area that each run clears and pushes to GitHub.
  - **Caching:** the scan index is held in memory, re-read daily after 3 PM (the 1 PM run is archived by then) and as soon as a `scan.json` changes on disk (checked at most once a minute). Chart URLs carry the scan's version, so browsers keep them until next week's run replaces them.
  - `python -m social.scan_archive --backfill logs/run_YYYYMMDD.log ...` rebuilds a scan folder from a run log and the charts that run pushed to git.
- **Socials** (home page, under Latest scans): an app-icon widget linking to @lynch_pin_quant on X, Instagram and Threads, and a **Latest on X** widget with the account's 5 latest posts (text, chart thumbnail, replies / reposts / likes / views; tap to open on X). It is read with `main.py`'s X posting tokens (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`, from `venv/bin/activate`, else the environment); the tokens never reach the page. X reads are metered, so the server reads X once a day in the background, at 9 AM Pacific (`--social-read-at HH:MM`, `LYNCH_UI_SOCIAL_TZ` = `America/Los_Angeles`, so PDT / PST whatever the server's clock says; on startup if that day's read was missed). That is before the 1 PM scan posts, so the widget shows what else is on X rather than repeating Latest scans. Page views never call X. The server keeps the posts and their re-encoded images in `ui/.cache/socials/` and serves everyone from there; a failed read keeps the last posts and is retried an hour later. Without tokens the widget is hidden and the links stay.
- **Home button:** on a ticker page or a scan thread, the ⌂ Home pill in the top bar (or the logo) returns to the home page (search, Latest scans) without reloading; Back/Forward work as usual.
- Ticker search at `?t=MSFT`, which you can bookmark and share. It shows valuation (PEG, Dev SD bell, 5Y Bull/Base/Bear ROI), the same chart `main.py` renders (drawn in ~11 ms on an M1 by the native renderer, see *Native chart renderer*), the income grade, the credit rating, technicals and the 6M edge. Results stream in stage by stage.
- An AI overview from a local LM Studio server that types in real time (Server-Sent Events), with live time to first token, tokens/s, token count and thinking tokens; a reasoning model's thinking streams into a collapsible box. The prompt is a static system message (the task) plus a terse, pre-computed data block for the ticker (company profile, analysts' target, the reverse-DCF math and the Quick Overview's red flags), answered in three 2-3 sentence sections (see *Local AI tuning*). Each ticker's overview is generated once and cached with its analysis for the day. When no model is loaded, the UI shows "AI offline" and keeps working.
- **AI inference queue:** above the local model's share of AI overviews a minute, Gemini writes them; above Gemini's 15 a minute, the page shows the Quick overview with ↻ Retry AI (see *AI inference queue*).
- **Quick Overview** with `--no-ai`: the AI overview's three sections built by fixed rules from the same data, no model involved (`ui/quick.py`). *Overview*: name, sector, HQ, employees, market cap, Yahoo's business summary, margins, dividend, analyst consensus and a valuation snapshot. *Reverse 5Y DCF*: "X% base ROI requires EPS to compound at Y%/yr for 5 years and a re-rating from Zx forward PE to a terminal ZZx" (the daily scan's Base ROI math), split into the EPS and multiple contributions, with how demanding those assumptions are. *Stomach test*: red flags from thresholds such as trailing PE > 50, forward PE > 40, growth > 40%, PEG ≥ 2.5, PEG > 1 SD above its mean, base ROI < 9%, a losing bear case, shrinking revenue, an income grade below A (subpar; C/D are serious), a credit rating below A (balance-sheet risk; junk is serious), negative free cash flow, an uncovered dividend, a falling trend, high beta or short interest.
- With `--no-ai` there is no **↻ Refresh**: a cached ticker stays cached until midnight (or eviction), so repeat lookups never spend Yahoo calls again. Uncacheable results (errors, a Yahoo 429, a missing PEG history) still re-run when searched again.
- **Trading view**: a *Trading view* button at the top of the Technicals card opens the ticker's candlestick chart (full screen on a phone) at 18 intervals — 1m, 2m, 3m, 5m, 10m, 15m, 30m, 45m, 1H, 2H, 3H, 4H, 1D, 1W, 1M, 3M, 6M, 12M — with date ranges (1D, 5D, 1M, 3M, 6M, YTD, 1Y, 5Y, All), extended hours (ETH), SMA 50/100/200, EMA 8/21, Bollinger Bands, volume and a volume shelf, RSI 14 and MACD 12/26/9, BUY / SELL candles (blue / pink signal bars), a countdown to the next candle, a LIVE last candle, drawings (trend line, ray, horizontal line, Fibonacci retracement, rectangle), the price levels below drawn on it (R/S lines with their touch probabilities, POC, 52-week high / low, the 1-month ±1σ range, the accumulation zone), and *🔬 Assess*: a prompt for Claude / ChatGPT written for the candles' horizon, with a snapshot of the setup to attach. See *Trading view*.
- **Price levels** in the Technicals card, computed with the experimental trade assistant's toolkit (`experimental/quant_engine.py`): support and resistance from clustered pivots (6 months), the volume point of control (3 months), a 1-month expected range from realised volatility, and the 52-week range. Each level shows its chance of being touched within a month.
- **Deep Dive Prompt** (above *How to read*): a research brief to paste into Claude, ChatGPT or Gemini. It casts the model as a hedge-fund portfolio manager and walks it through the last two earnings reports, call transcripts, the 10-Q, news, management's answers and red flags. It then makes the model loop, stress-testing its bull and bear case until another pass changes nothing, and asks for a verdict against the S&P 500 with price levels and signals to watch. The quant data, the price levels and the local AI overview are attached at the end. **Copy** works on plain-http LAN and Tailscale URLs too; **Show / Hide** toggles the full text.
- The 5Y Growth value is tagged **Enriched** (Yahoo + FMP) or **Not enriched** (Yahoo only).
- **Concurrent lookups** with the AI overview off: several users' tickers are analysed at the same time, each in its own worker process (2 per CPU core, at most 16, and no more than free RAM holds at ~200 MB each; `--workers` to change). A model loaded in LM Studio can take most of an 8 GB Mac: unload it when running `--no-ai`. On an 8-core M1 this takes cold lookups from ~11 to ~117 tickers/min (`ui/tests/cold_bench.py`), and to 140 tickers/min with the native chart renderer and 16 workers (2026-10-15, `--clients 24 --count 48`, FMP off): 12 workers ran at 132.9 and 129.8/min (108.7/min with matplotlib on 2026-10-07). The chart stage went from ~1 s to 16-73 ms under load, and a cold lookup is now Yahoo / SEC (*stats*, ~2-3.5 s) and the 6M edge backtest (~2.5-3.5 s). At 16 workers the 8 cores are saturated: each lookup takes ~1 s longer at full load (the backtests share the cores), so more workers would only queue. With the 6M edge off (the default since 2026-10-15, `--edge` turns it on) the same run reaches 164 tickers/min: a cold lookup is now mostly Yahoo / SEC (*stats*, ~2-4 s, network-bound), grades (~0.45 s), technicals (~0.15 s) and the chart (~25 ms). Through `--public` with every visitor on its own IP and loading the full home page first (22 requests, ~1.3 MB: Latest scans and Socials included), the same 48 tickers ran at 110 tickers/min (2026-10-05). A burst of 200 visitors at once, each with a new ticker, ran at 62 tickers/min: 12 analysed (the default then), 20 queued, the rest got "queue full" and their pages retried, and Yahoo answered 429 eleven times, so the circuit breaker paused new work; all 200 finished, the slowest in ~3 min. With the AI overview on, tickers are analysed one at a time unless `--workers` is set. Yahoo throttles at a few hundred tickers in a short burst (each cold lookup makes ~12 Yahoo calls), so the cache below does the heavy lifting under sustained load.
- A daily LFU cache of 2500 tickers: about what the AI writes in a day at full load before Gemini's daily cap runs out (39 overviews a minute, 24 local + 15 Gemini, for 975 / 15 = 65 minutes). Typing a ticker again the same day skips the quant pipeline, the chart and the LLM. The cache and old charts are cleared at the first access after midnight.
  - **RAM:** ~35 KB per entry with its AI overview (~21 KB without), ~85 MB when full.
  - **Disk:** ~560 KB of charts per ticker analysed that day, evicted or not, until midnight.
  - **Measured** on an 8-core M1 with 8 GB RAM, AI off, 2026-10-07: the server's RSS was 80 MB higher with a full 2500 cache than with a full 500 cache. Cold lookups (`cold_bench.py --clients 24 --count 48`, 12 workers, every lookup evicting, A/B/B/A) ran at 108.7/min against 105.8/min, within the ±2.5% between repeat runs, with no swapping.
  - Every cache operation is O(1). The top-5 list in `/api/health` is a partial selection, 0.3 ms at 2500 entries.

## Native chart renderer (`ui/native`, Rust)
The per-ticker chart is the one `graphics/visualizer.py` draws for `main.py` (PEG bell curve, stats box, grades box), drawn again by a small Rust library: `ui/fastchart.py` formats the chart's text with the visualizer's own f-strings and loads `ui/native/target/release/liblynch_chart.dylib` (`.so` on Linux) with ctypes. The library lays the chart out the way matplotlib 3.11 does (the DejaVu fonts of the matplotlib install, its text boxes, `MaxNLocator` ticks, `ScalarFormatter` labels and one pass of `tight_layout`), rasterises it with [tiny-skia](https://github.com/linebender/tiny-skia) and writes the 300-dpi PNG (the lightbox) and the page's 1100-px JPEG preview. `graphics/visualizer.py` is unchanged and still draws the daily scans' charts.

- **Build:** `cd ui/native && cargo build --release` (or `python -m ui.fastchart --build`), with a Rust toolchain from [rustup.rs](https://rustup.rs); rebuild after pulling changes to `ui/native`. `python -m ui.fastchart` says whether the portal finds the library. Without it, or with `LYNCH_UI_NATIVE_CHART=0`, charts are drawn with matplotlib as before. A chart the renderer leaves to matplotlib (math text in a label, a degenerate distribution) or a failed render falls back to matplotlib for that chart, with a warning in the log for a failure.
- **Speed** (`python ui/tests/chart_bench.py --matplotlib`; a chart = the 300-dpi PNG and its 1100-px preview):

  | | matplotlib + Pillow | native |
  |---|---|---|
  | one chart, M1 MacBook Air (8 cores), 2026-10-15 | 498 ms | 11.3 ms (p90 12.0) |
  | 8 threads drawing at once, M1 | 154 charts/min | 6919 charts/min |
  | one chart, 4-vCPU Xeon VM (Linux) | 1172 ms | 31 ms |
  | PNG / JPEG preview (MSFT) | 525 KB / 87 KB | 475 KB / 89 KB |

  On the M1 a chart's 11 ms are 0.8 ms of layout, then the PNG (9.1 ms: 16 bands drawn and deflated on 8 threads, ~32 ms of drawing and ~25 ms of compression in CPU time) and the preview (10 ms, on its own thread) side by side.
- **How:**
  - The canvas is cut into horizontal bands, two per core (up to 8 cores), that threads draw and compress independently: rows are PNG-filtered *Up* (*Sub* for a band's first row, so no band needs the one above it) and deflated into segments that end on a byte boundary, joined into one zlib stream as pigz does. Meanwhile another thread draws the preview at its own size, instead of Pillow shrinking the 3600×2100 PNG.
  - The 100 translucent white fills matplotlib stacks under the curve depend only on height, so they collapse to one colour per row. Glyphs are rasterised once per size and ¼-pixel position and then blitted (the cache lives as long as the worker, so after the first chart text costs almost nothing). Stroke outlines are computed once per chart, not once per band.
  - ctypes releases the GIL for the call and the library has no process-global state, so concurrent analyses draw their charts in parallel: the pyplot lock is only taken for a matplotlib fallback. The analysis worker writes the preview where `/plots/SYM.jpg` looks for it, so the server process no longer resizes images on request, and workers no longer import pyplot.
- **Why not the GPU (Metal)?** Measured, on the M1 (`ui/native/gpu_probe`, 2026-10-15): the CPU draws the whole chart in 5.35 ms on 8 threads (13.9 ms on one), while a Metal renderer could not go below 4.58 ms — an empty render pass over the 3600×2100 canvas (0.29 ms) plus reading the 30 MB image back for the PNG / JPEG encoders (4.29 ms). That leaves at most 0.77 ms of an 11 ms chart, before the GPU does any actual drawing (tessellation, uploads, text), and the compression that takes most of the time stays on the CPU either way. To measure it again:
  ```bash
  python -m ui.fastchart --spec /tmp/msft.json
  cd ui/native/gpu_probe && cargo run --release -- /tmp/msft.json
  ```
- **Still drawn with the analysis:** at ~11 ms and ~0.07 s of CPU across threads on the M1, charts take ~1% of its 8 cores at 100 cold lookups a minute, where matplotlib took ~0.8 of a core; drawing it only when the chart is opened would save little and cost a round-trip on every view. Charts stay cached for the day with the analysis: a cache hit serves files, and `?v=` URLs let browsers keep them.
- **Fidelity:** `ui/tests/test_fastchart.py` checks the text against what the visualizer draws and the pixels against matplotlib's (mean difference about 1/255: anti-aliasing at the edges of glyphs and lines; the layout is identical). The curve is stroked with miter joins where matplotlib uses round ones (under 0.02 px apart at its few-degree bends), and glyphs are placed on a ¼-pixel grid.

## Trading view (`ui/tradingview.py`, `ui/tvdata.py`, `ui/native/src/bars.rs`)

The per-ticker chart the Technicals card opens (`ui/static/tradingview.js`, `?t=MU&tv=240` links to MU's 4H chart; Back closes it). Yahoo Finance bars, regular session only.

- **The page:** two canvases, the chart and the crosshair over it, so moving the crosshair never redraws the candles; candles are drawn as four batched paths (up / down wicks and bodies) however many bars are on screen. Drag to pan (with inertia), pinch or wheel to zoom, double-click / `End` to go back to the last bar, arrows to step; hover (mouse) or tap / long-press (touch) for the crosshair. The legend shows the bar under it (OHLC, change, volume, RSI, MACD and each average, which can be hidden). The price levels are the ones the analysis computed (*Price levels* above), passed to the chart when it opens and drawn once the analysis lands when the chart is opened from a link. The interval sheet is TradingView's: date ranges, then minutes, hours and days. The last interval and the hidden averages are remembered (`localStorage`).
- **Intervals Yahoo doesn't serve** are built from one it does: 3m from 1m, 10m from 5m, 30m and 45m from 15m (yfinance skips Yahoo's 30m too: its bars are misaligned), 2H-4H from 1H, 3M / 6M / 12M from 1M. Intraday bars are counted from each session's open, with each day's own UTC offset from Yahoo's `tradingPeriods` (so DST changes nothing), as TradingView does: a 4H chart of a US stock has a 9:30-13:30 and a 13:30-16:00 bar, and one 9:30-13:00 bar on an early-close day. Yahoo's quirks are handled: the "live" row it appends at the close (16:00, or 16:00:01 on weekly / monthly series) is folded into its bar, and today's daily bar, which can come without a close, takes it (and the day's high / low / volume) from the quote.
- **History:** 1m bars for 7 sessions (Yahoo keeps 8), 2m for a month, 5m / 15m for 60 sessions, 1H for 730 sessions, daily for 10 years, weekly and monthly since the listing; the page gets the last 3000 bars, with the averages computed over the whole series first.
- **Indicators** as Pine Script computes them: SMA, EMA seeded with the SMA of its first *n* values, RSI with Wilder's average, MACD's signal the 9-EMA of the MACD line.
- **The build** — Yahoo's raw chart JSON → candles → indicators → the page's JSON and its gzip — runs in the native library (`ui/native/src/bars.rs`, the chart renderer's crate; `ui/tvdata.py` calls it with ctypes, which drops the GIL). Python never touches a bar. Without the library, numpy builds the same JSON (`test_tradingview.py` checks they agree on every interval, Yahoo's recorded series included). The JSON is columns: times as deltas (a column of `3600` compresses to nothing), prices and averages at the instrument's decimals, the MACD at one more, RSI at 0.1, volume at 3 significant digits; the page computes the MACD histogram. A 1m chart is ~80 KB gzipped, a 4H one ~50 KB.

  | Build (M1, 2026-10-20, MU) | native | numpy |
  |---|---|---|
  | 1m, 2730 bars (234 KB JSON → 81 KB gzip) | 3.1 ms | 28.8 ms |
  | 4H from 1H, 1447 bars | 2.7 ms | 20.0 ms |
  | 1D, 2511 bars | 3.0 ms | 26.7 ms |
  | 3M from 1M, 168 bars | 0.27 ms | 2.2 ms |

  Most of the native time is gzip (deflate level 2: 8% bigger than level 6 and 3-4× faster); parsing, bucketing and the indicators take ~1-2 ms.
- **Serving** (`ui/tradingview.py`), cheapest first:
  - A chart someone already asked for is bytes in memory: each (ticker, interval)'s gzipped JSON is kept until its Yahoo series is refreshed, with an ETag, so the page's polls get a 304.
  - Another interval of a series in memory is one build (above).
  - Otherwise one Yahoo request, made by a pool of `LYNCH_UI_TV_FETCHES` (4) fetcher threads on a curl_cffi session that impersonates Chrome, as yfinance does. Each thread keeps its connection to Yahoo open: ~30 ms a request instead of ~250 ms with a new TLS handshake (HTTP threads live for one connection each, so they would never reuse one). Viewers of the same series share it: requests that arrive while it is being fetched wait for that one request.
  - Series stay fresh while the market is open for 30 s (1m, 2m), 60 s (5m, 15m), 2 min (1H, daily) or 10 min (weekly, monthly); when it is closed, 15 min, or until the next open. The page polls at that pace (15-45 s, which also keeps its place, below) and while it is hidden doesn't poll at all.
  - Memory: up to 128 MB of Yahoo series and 64 MB of built charts (LRU).
- **Yahoo comes first for the lookups.**
  - A Yahoo budget: at most `--tv-yahoo-rpm` (120) chart fetches a minute for everyone, ~6% of the ~2100 Yahoo requests a minute the lookups make at full speed (~175 cold lookups/min × ~12 requests), and at most 20 a minute set off by one viewer (`LYNCH_UI_TV_VIEWER_RPM`; 429 *Trading view: new charts a minute*). So no viewer, nor a script cycling through tickers, can take Yahoo's quota from the lookups. Past it, viewers get the last series in memory (marked stale) or a countdown; cached charts and joining a fetch under way cost nothing.
  - A chart fetch that gets a 429 opens the analyses' circuit breaker (*Concurrent lookups*), and while it is open charts are not fetched: viewers get the last series in memory, marked stale, or wait.
  - An unknown symbol (Yahoo's 404) is remembered for an hour, a failed fetch for 15 s.
- **Viewers are limited** (`--tv-viewers`, default 50, from the benchmark below): a viewer is a page with the chart open, alive while it polls; one that stops polling for 75 s (`LYNCH_UI_TV_IDLE`) gives its place up, and closing the chart gives it up at once (`/api/tv/release`). Past the limit the page says "Trading view is full" with a countdown to when a place frees up and retries (429, *Trading view full* on the stats page). With `--public` a visitor (IP) may have 2 charts open. `/api/health` has the count under `tv`.

### On the chart

- **Extended hours (ETH, on by default for 1m-4H):** pre- and post-market bars (Yahoo's `includePrePost`), on a lighter background as TradingView shades them; each day's session then runs from the pre-market's open to the post-market's close, so 4H bars start at 04:00, 08:00, 12:00 and 16:00 New York. The page asks for `/api/tv/SYM?i=CODE&x=1` and the bars come with `sess` (r / e per bar); extended and regular series are cached apart.
- **Bollinger Bands (20, 2):** the 20-SMA ± 2 population standard deviations (Pine's `ta.bb`), upper red, basis white, lower teal, shaded between (`bbu` / `bbm` / `bbl` from bars.rs).
- **Volume shelf:** volume by price over the bars on screen (TradingView's visible-range profile), each bar's volume spread evenly over the 48 rows its high-low covers, drawn from the right edge; the value area (70% of the volume around the busiest row) brighter, and the point of control as an orange line across the pane with its price on the axis. It follows the view as you pan and zoom (computed in the page: ~0.1 ms).
- **Accumulation zone:** the Technicals card's (daily SMA 200 ± 1 ATR, `engine/technical_timing.py`), shaded green across every interval; out of view, a pill in the corner says where it is and how far (`▼ Accumulation zone 674–773 · −16%`).
- **Drawings:** trend line, ray, horizontal line, Fibonacci retracement (0, 0.236, 0.382, 0.5, 0.618, 0.786, 1 from the second point back to the first, TradingView's colours) and rectangle, from *✏ Draw* (TradingView's Drawings sheet). Drag to draw, or tap two points; tap a drawing to select it, drag a handle (or the whole of it) to move it, Delete / the sheet's *Delete selected* to remove it. Anchors are (time, price), so a drawing stays put across zoom, pan and intervals (a fib drawn on 4H sits at the same prices on 15m). They are kept per ticker in the browser (`localStorage`).

### BUY / SELL candles (reverse engineered)

TradexWhisperer's TradingView charts paint MU's candles with a private indicator: blue bars at bottoms ("BLUE": here **BUY**), pink at tops ("PINK": here **SELL**), lime ("TRIM") to take profits, bright / dark green and red / dark red in between. The trading view paints the same way, computed with the bars in bars.rs (`paint`; numpy mirror in tvdata.py); the toolbar's *BUY/SELL* button turns it off for plain green / red candles (remembered):

1. **A state machine.** Bars are bullish from a BUY until a SELL, bearish from a SELL until a BUY.
   - **BUY** (blue bar, to bull): Stochastic %K (14, 3) was under 20 within the last 5 bars and RSI 14 is above its 14-bar average (momentum turning up after oversold), **or** the close clears the prior bar's high while the fast MACD (8, 17, 9) histogram is positive.
   - **SELL** (pink bar, to bear): the close breaks the prior bar's low while the fast MACD histogram is negative.
2. **Shades.** Bullish bars: lime (TRIM) on the first down bar of each %K > 80 episode, dark green when the close is under the 9-EMA (a pullback), green otherwise. Bearish bars: dark red for a bounce (close at the prior high or above, over the 9-EMA), red otherwise.

**How it was found** (`tmp/candle_re/`, not shipped): 6 screenshots (MU 1D, 3m × 2, 5m, 15m; 2026-10-15 to 10-20). Every candle body was cut out of the pixels (7 colours; body borders, wicks, labels and translucent drawn boxes masked) and matched to Yahoo's bar at the same time by fitting the bodies' heights to the bars' prices (r = −0.996 to −0.9999 on every chart; the two 3m screenshots agree on all 64 bars they share, so the indicator doesn't repaint). Then:
- Colour is not the candle's direction (green on down bars, red on up bars) but a state; BLUE only on up bars and PINK only on down bars, and every green run starts with a BLUE, every red one with a PINK.
- A single oscillator's sign gets at most 92.4% of states (78,000 MACD variants: sources, lengths and average types; RSI vs its average, WaveTrend, Stochastic, CCI, TSI, Fisher, SuperTrend, Heikin-Ashi did worse): it flips a few bars late. The flips were then searched as triggers — every AND of up to 3 of 100 conditions, scored on the bars where the true state could flip — and the state machine above was the best that also runs free from any start.

| TradexWhisperer chart | Bars | Bull / bear state | Exact colour | BUY / SELL on the exact bar |
|---|---|---|---|---|
| 1D, Sep 15 - Oct 16 | 24 | 100% | 83% | 2 / 2 |
| 3m, Oct 15 (2 screenshots) | 104 | 93% | 73% | 4 / 7 |
| 5m, Oct 20 | 34 | 94% | 88% | 2 / 3 |
| 15m, Oct 20 | 22 | 100% | 77% | 1 / 1 |
| **All** | **184** | **95.1%** | **77.7%** (all-green: 50.5%) | **9 / 13** |

Intraday with extended hours, as the screenshots are; on regular-session bars the opening bars lack the pre-market's warm-up and the state agrees 88%. Lime is the weakest colour (TRIM fires on its own rule, a first pullback after overbought, rather than the screenshots' exact bars), and a short bull run in the 3m chop is missed. More screenshots (other tickers, timeframes) would tell whether the thresholds hold or were fitted to MU in October 2026.

### Countdown and LIVE

- **Countdown:** under the last price, the time left in the current candle (to the close for 1D), as TradingView shows it; when it runs out the page asks for the chart again, and the server has the new candle: while the market is open, a series expires at the next bar of its Yahoo interval (+3 s), not only after its usual 30 s-2 min.
- **LIVE** (toolbar, off until pressed): the last candle follows the market every 2 s. The page polls `/api/tv/SYM/live`, the server reads Yahoo's 1-minute bars for the day (`range=1d`, a few dozen KB) at most every 2 s per ticker for everyone watching it live, and the page folds the bars since its last candle's start into that candle (high, low, close, volume). 1-minute bars past the candle's end start the next candle on the page at once (its indicators come with the chart, fetched again at most every 20 s: Yahoo's 7-day 1-minute series lags its 1-day one by a minute or so). Yahoo publishes a new minute's first bar ~10 s into it, so a new candle appears that much after the countdown runs out. Checked on BTC-USD (trades around the clock), 2026-10-22: the 02:06 and 02:07 candles appeared live with the countdown running. Market shut: it says so and asks every 15 s. Hidden tab: it stops.
- **LIVE is for 2 people at a time** (`--tv-live`): a live ticker costs up to 30 Yahoo requests a minute, on top of the trading view's 120-a-minute budget for charts, so 2 live tickers add at most 60 (~3% of the lookups' ~2100). A third gets "live is in use" (429, *Trading view: live full* on the stats page) and the button turns off; a live viewer that stops polling for 10 s, or closes the chart, frees its place. Yahoo's circuit breaker pauses live reads too.

### 🔬 Assess this setup

The toolbar's *🔬 Assess* opens the *Trading setup prompt*, laid out like the Deep Dive Prompt: *Copy*, *Show* / *Hide*, and the chart lightbox's download button for a snapshot of the chart as it is on screen (with a header: ticker, interval, last bar, OHLC, RSI, MACD) to attach. Copy works on plain-http LAN pages too (a textarea inside the dialog: outside a modal dialog the page is inert). The prompt is written for the candles' horizon:

| Interval | Horizon | Prompt's emphasis |
|---|---|---|
| 1m-5m | scalp: minutes to an hour | tight stops (last swing / one bar-ATR), session timing (open, lunch, close, pre / post-market liquidity) |
| 10m-1H | intraday swing: hours to ~3 days | the session's trend against the daily's, overnight gaps |
| 2H-1D | swing: days to weeks | earnings, gaps, the weekly trend |
| 1W-12M | position: months to years | valuation decides: the Lynch Pin's PEG, its deviation and the 5-year ROI cases are included |

The prompt carries the exact numbers (the image is for structure): the last bar, every moving average with the price above or below it, RSI, MACD with the histogram's direction, Bollinger %B and bandwidth (and a squeeze), the volume shelf's POC and value area, the BUY / SELL state and how many bars ago it flipped, the analysis' S/R levels with their touch probabilities, the 3-month POC, 52-week and 1-month ranges, the accumulation zone, and the drawings (a fib's levels at their prices). It asks for the read, a bias with a conviction score, entry / stop / two targets with reward:risk for that horizon, confirmation and invalidation, risks and a one-line verdict. Paste it into Claude or ChatGPT with the snapshot attached.

### Benchmark: how many viewers, and what they cost the lookups (2026-10-20)

`ui/tests/tv_bench.py`, M1 MacBook Air (8 cores, 8 GB), evening (market closed, so market-hours refresh rates were forced on: `LYNCH_UI_TV_FORCE_OPEN=1`).

**Offline** (`offline`): the server against Yahoo's series for MU recorded once, every ticker getting its own copy: what the server itself can serve.

| | native bars | numpy bars |
|---|---|---|
| builds/s (32 connections, each request a ticker + interval never seen) | **1533** (5.4 cores busy) | 51 (1.2 cores: the GIL) |
| cached charts and 304s, req/s (32 connections) | 7305 | 2608 (builds still holding the GIL) |
| 1000 viewers: interval switch p50 / p99 | **5 / 11 ms** | 1804 / 13111 ms |
| server CPU for 1000 viewers | 0.19 cores (12 ms per viewer-minute) | 0.55 cores |

The native build is what lets the HTTP threads scale: it runs without the GIL, so 32 concurrent builds use 5 cores, where numpy's serialise on one and every other request waits behind them. The server isn't what limits viewers: thousands fit in its CPU. Yahoo is.

**Live** (`live`): real Yahoo. 48 cold Nasdaq-100 lookups from 24 clients (cold_bench.py's loop, 8 analysis workers: what the free RAM held, FMP off), while N people use the trading view, each on their own S&P 500 ticker outside the Nasdaq-100 (nothing shared: the worst case for Yahoo). A viewer opens the chart (people arriving over 10 s), switches interval every ~12 s (weighted towards 1D, 4H, 1H, 15m, 5m) and polls in between as the page does.

| Viewers | Limit / Yahoo budget | Cold lookups/min | vs baseline* | Chart open p50 | Switch p50 / p90 | Yahoo requests by charts | Yahoo 429s |
|---|---|---|---|---|---|---|---|
| 0 | | 172.4 → 178.7 | | | | | 0 |
| 25 | none | 172.3 | −0.9% | 183 ms | 121 / 228 ms | 2.2 per viewer-minute | 0 |
| 50 | none | 171.7 | −2.2% | 189 ms | 103 / 245 ms | 2.0 | 0 |
| 100 | none | 167.9 | −5.2% | 408 ms | 135 / 253 ms | 2.1 | 0 |
| 0 | | 168.2 → 157.3 | | | | | 0 |
| 50 | 50 viewers, 120 a min | 163.1 | −0.9% | 203 ms | 17 / 7950 ms | 1.3 | 0 |
| 100 (50 let in) | 50 viewers, 120 a min | 155.1 | −3.6% | 256 ms | 18 / 7818 ms | 0.65 | 0 |

\* Against the baseline interpolated between the runs with no viewers before and after, which move 4-7% within a sweep: Yahoo's latency sets a cold lookup's pace (~7 s, mostly Yahoo / SEC).

**Reading:**
- **The cost is Yahoo's, not the CPU's.** An active viewer makes ~2 Yahoo requests a minute in the worst case (market hours, everyone on a different ticker), 1/6 of a cold lookup's ~12. Unlimited, 100 viewers made ~200 a minute, ~10% of the ~2100 the lookups make at full speed, and the lookups lost ~5%: their Yahoo requests answer a little slower (p50 7.1 → 7.3 s). When the market is closed, a viewer costs a request per series per 15 min.
- **Yahoo throttles the machine, not a caller.** In a longer sweep (96 lookups a run, after ~6000 Yahoo requests in 20 minutes), the 100-viewer run came with nine 429s on the *lookups*, whose circuit breaker paused them (46 lookups/min). Every chart request is one the lookups don't get.
- **Hence the defaults: 50 viewers and 120 Yahoo requests a minute** (≤ ~6% of the lookups' Yahoo traffic). Cold lookups lose 1-4%, within the run-to-run noise; 100 people asking cost no more than 50 (the other 50 see "Trading view is full" and retry); no 429s.
- **The price is the chart's when a crowd arrives at once.** 50 people on 50 different tickers within 10 s exceed the budget's burst (30), so ~1 switch in 10 waits its turn (p90 ~8 s) and ~5% are asked to retry a few seconds later. Switching between intervals already loaded takes ~17 ms. People looking at the same tickers share their series and cost nothing more.
- To measure again: `python ui/tests/tv_bench.py live --viewers 0,50,100,0` (the shipped limits) or `--unlimited` (the raw cost). Mind Yahoo: a sweep makes ~3000 requests, and back-to-back sweeps got the lookups throttled.

## Stats page (`--lan` or `--public`)

With `--lan` or `--public` the portal also starts a second server, the **stats page**, on port **190** (`--stats-port`): `http://<lan-ip>:190` or `http://<tailscale-ip>:190`, printed at startup. It has the portal's header and no other routes: no tickers, no charts. It doesn't run in the default loopback-only mode, and `--no-stats` turns it off. On Linux, ports below 1024 need root, so pick e.g. `--stats-port 8190` there; if the port can't be bound, the portal runs on without it and says why.

**Who can open it: LAN and Tailscale clients only.** Clients outside loopback, RFC 1918, ULA, link-local and Tailscale ranges, and foreign `Host` headers, get a 403, as on the `--lan` portal. So does anything carrying Cloudflare's headers (`CF-Connecting-IP`, `CF-Ray`).
- **With `--lan`** the portal and the stats page are both on the LAN; `http://localhost:190` works on this machine too.
- **With `--public`** the portal stays on 127.0.0.1 behind the tunnel, and the stats page records its traffic, with visitors counted by `CF-Connecting-IP`. The stats page itself refuses loopback, because that is where cloudflared connects from, so even a tunnel pointed at port 190 by mistake gets a 403. Open it from a phone on the Wi-Fi or over Tailscale, or on this machine through its LAN address. Its *Portal* button is hidden, since the portal isn't on the LAN.

A range switch (24 h / 7 d / 30 d) scopes everything but *Visitors*; the page refreshes every minute.
- **Tiles:** cold lookups (ticker queries that waited for an analysis: a new one, a ↻ Refresh, or one already running), peak cold lookups per minute, ticker queries, cold-lookup latency p99.9, cache hit rate, rejected %, DAU today, MAU.
- **Ticker lookups vs other actions:** a donut of what visitors did in the window: ticker lookups (↻ Refresh included) against the other actions, opening the portal or a scan thread, with each slice's count and share beside it. Only these actions (and refused requests, for the rejection rate) count as requests: what the page then loads on its own is left out, i.e. its files and images, the scan list and X feed, the AI overview and its stream, the deep-dive prompt, the health check every 30 s and the polls while a lookup or an AI overview runs (`?poll=1`).
- **Ticker query latency:** CDF on a log scale over cold lookups only, from the lookup to its result, with **p99.9 highlighted**: the time until its analysis finished, queue and Yahoo pauses included; a lookup that joins an analysis already running counts from when it joined. Cache hits (well under a millisecond) are left out.
- **Cache hit rate per day:** CDF over days of the share of each day's lookups answered from the daily cache, plus where all lookups were answered from (cache, new analysis, joined one in progress, a recent result).
- **Most queried tickers:** the top 10 as a share of all ticker queries.
- **Rejected requests:** by reason (client outside the LAN, foreign `Host`, invalid ticker, write method, analysis / AI queue full, one analysis per visitor) as a share of all requests, and per day. Yahoo's 429s are listed too, marked *upstream*: they are Yahoo refusing the portal's own calls (the circuit breaker pauses lookups), not requests to the portal.
- **Where AI overviews came from:** a donut of visitors' new AI overviews: written by the local model, offloaded to Gemini, or neither (the page showed the Quick overview: at capacity, no model up, or the model failed, broken down beside the slice). Cached overviews are not counted (see *AI inference queue*).
- **AI overview:** CDFs of time to first token, speed (tok/s) and total time per generated overview (cached overviews cost nothing and are not counted), with failures and the queue wait.
- **Visitors:** daily active users (distinct source IPs per day) and monthly active users (distinct source IPs in the 30 days up to each day) over the last 12 months, plus today's DAU, MAU and DAU/MAU in the tiles. The 12-month window is fixed: the range switch stops at 30 days. A visitor is the source of a request the portal served (status below 400), so refused probes don't count. Behind a tunnel on this machine (cloudflared) that is the visitor's `CF-Connecting-IP`, trusted only from loopback, so a LAN device can't set it.

Each chart has its percentiles (p50, p90, p99, p99.9, max, n) underneath, and a crosshair that reads off P(X ≤ x) on hover or with the arrow keys.

**Recording (`ui/stats.py`).** No lock on the request path: HTTP and worker threads bump per-minute counters with `next()` on an `itertools.count` (one C call, atomic under the GIL) and append finished lookups and AI overviews to a `deque` (`append` is atomic). A served request also marks its visitor in a dict used as a set (one atomic store). One flusher thread writes them to SQLite (`ui/.cache/stats.sqlite3`, WAL) every 5 s; a minute's counters are written two minutes after it ends. The stats page reads through its own connection. `Ctrl-C` and `SIGTERM` write the last minutes before exiting.

**Retention:** a rolling 30 days (`LYNCH_UI_STATS_DAYS`), and a rolling year for DAU / MAU (`LYNCH_UI_STATS_VISITOR_DAYS`); older rows are deleted every hour. Visitors are stored once per day as a keyed hash of their IP (HMAC-SHA256, random key kept in the database), never as the IP: enough to count distinct visitors, not to list them.

**Overhead** (8-core M1, Python 3.9, 2026-10-06): recording costs ~1.7 µs of CPU per request (~2.1 µs from loopback, where the `CF-Connecting-IP` lookup runs):
- the per-minute counter: 0.4 µs
- the visitor: marked once per connection and hour
- the rest: clock reads and bookkeeping

Measured cost:
- **Offline** (`python ui/tests/test_benchmark.py --stats`, cached tickers, AI off, A/B/B/A): 0.2-2.3% fewer requests/s. That is the cheapest requests (0.2-0.3 ms each) with the server saturated.
- **Cold lookups** with `--no-ai` (`cold_bench.py --clients 24 --count 48`, 12 workers, no Yahoo 429s), the same two sets of 48 tickers in A/B/B/A order. Measured with the request counters, before visitors were added (they add a few tenths of a µs per request):

  | Tickers | Stats off | Stats on |
  |---|---|---|
  | Nasdaq-100 #1-48 | 104.0 /min | 105.8 /min |
  | Nasdaq-100 #49-96 | 99.4 /min | 96.8 /min |
  | Mean | 101.7 /min | 101.3 /min |

  The 0.4% difference is well inside the ±2.5% between repeat runs: each lookup takes seconds of Yahoo / SEC time and about 60 requests, so the stats cost ~0.1 ms of a ~10 s lookup.

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
| `--llm-parallel` | `LYNCH_LLM_PARALLEL` | `1` AI overviews generated at once (see *Local AI tuning*) |
| `--llm-rpm` | `LYNCH_LLM_RPM` | `24`: low watermark, AI overviews a minute the local model is given; 24 is Qwen3.6-35B-A3B on Splash with `--llm-parallel 4`, use `4` for the 27B (see *AI inference queue*) |
| `--gemini-model` | `LYNCH_GEMINI_MODEL` | `gemini-flash-lite-latest`: offload above the low watermark, with `GEMINI_API_KEY` set |
| `--gemini-rpm` / `--gemini-rpd` | `LYNCH_GEMINI_RPM` / `LYNCH_GEMINI_RPD` | `15` a minute (the free tier's limit per model) / `975` a Pacific day (room for the daily scans' backup tier) |
| `--no-gemini` | `LYNCH_GEMINI=0` | Gemini offload on when `GEMINI_API_KEY` is set |
| `--cache-size` | `LYNCH_UI_CACHE_SIZE` | `2500` |
| | `LYNCH_UI_SCANS_DIR` / `LYNCH_UI_SCANS` | `scans/` / `7`: the scan archive and how many scans *Latest scans* shows |
| | `LYNCH_UI_SOCIALS=0` / `LYNCH_UI_SOCIAL_ENV` | on / `venv/bin/activate`: turn the Latest on X read off / where the X tokens are read from |
| `--social-read-at` | `LYNCH_UI_SOCIAL_READ_AT` / `LYNCH_UI_SOCIAL_TZ` | `09:00` / `America/Los_Angeles`: when the daily Latest on X read happens (9 AM PDT / PST) |
| `--valuation-at` | `LYNCH_UI_VALUATION_AT` / `LYNCH_UI_VALUATION_TZ` | `18:00` / `America/Los_Angeles`: the weekday Shiller PE refresh (the S&P 500 forward PEG sweep runs on Sunday mornings) |
| `--no-valuation` | `LYNCH_UI_VALUATION=0` | on: no market valuation section, no Shiller PE fetch, no S&P 500 sweep |
| `--benchmark` | `LYNCH_UI_BENCHMARK` | `SPY` (6M edge) |
| `--edge` | `LYNCH_UI_EDGE=1` | off: no 6M directional edge backtest (about half of a cold lookup's time, mostly CPU); the page hides its section. On: the Technicals card shows it again, and the chart, Quick overview, AI overview and Deep Dive prompt use it |
| `--no-ai` | | AI enabled |
| `-v` / `--verbose` | `LYNCH_UI_VERBOSE=1` | off: the header shows only *AI* with a green/red dot (no model name, no cache chip) and the AI card hides the model name, token counts and thinking setting |
| `--public` | `LYNCH_UI_PUBLIC=1` | off (see *Public access* above) |
| `--stats-port` | `LYNCH_UI_STATS_PORT` | `190`: the stats page, with `--lan` or `--public`, LAN / Tailscale clients only (see *Stats page*) |
| `--no-stats` | `LYNCH_UI_STATS=0` | on with `--lan` or `--public`: no stats page, nothing recorded |
| | `LYNCH_UI_STATS_DAYS` | `30`: days of stats kept (rolling) |
| | `LYNCH_UI_STATS_VISITOR_DAYS` | `365`: days of DAU / MAU kept (rolling) |
| `--workers` | `LYNCH_UI_WORKERS` | `0` = auto: 1 with the AI overview, 2 × CPU cores (max 16, capped by free RAM at ~200 MB each) without; above 1, one process each |
| `--enrich` | `LYNCH_UI_ENRICH` | `auto`: FMP multi-source growth when `FMP_API_KEY` is set (`on` / `off` to force) |
| `--fmp-limit` | `LYNCH_UI_FMP_LIMIT` | `225`: FMP requests the portal may make in any rolling 24 h (the free plan's 250 a day minus 25 for the daily scans). Every request FMP answers counts, errors included (a 402 for a symbol outside the free plan counts on FMP's dashboard); only a request FMP didn't answer (network error, timeout, a 5xx server failure) is handed back, and lookups that never call FMP cost nothing. Past the cap lookups are simply not enriched; the count is shared by the analysis processes and kept across restarts (`<cache_dir>/fmp_budget.sqlite3`), and `/api/health` shows it under `enrich` (not with `--public`) |
| `--precache` | `LYNCH_UI_PRECACHE` | `100`: right after the cache resets at midnight, analyse the N most looked-up tickers of the last 30 days (from the stats page, so `--lan` / `--public`; fewer if fewer were looked up), one at a time: with the AI overview on, the next ticker starts only once the previous one's overview is finished (~1 h for 100). These lookups don't count on the stats page or in the cache's hit rate, but spend the usual Yahoo calls and FMP requests (within `--fmp-limit`). `0` = off; `/api/health` shows the last run under `precache` |
| | `LYNCH_UI_NATIVE_CHART=0` / `LYNCH_UI_CHART_LIB` | on when built / `ui/native/target/release/liblynch_chart.dylib` (`.so` on Linux): draw per-ticker charts with matplotlib instead / where the native renderer is loaded from (see *Native chart renderer*) |
| `--tv-viewers` | `LYNCH_UI_TV_VIEWERS` | `50`: people who may have the trading view open at once (0 = no limit; see *Trading view*) |
| `--tv-live` | `LYNCH_UI_TV_LIVE` | `2`: people who may use the LIVE last candle at once (each live ticker: up to 30 Yahoo requests a minute) |
| `--tv-yahoo-rpm` | `LYNCH_UI_TV_YAHOO_RPM` / `LYNCH_UI_TV_VIEWER_RPM` | `120` / `20`: Yahoo requests the trading view may make a minute, for everyone (0 = no budget) / set off by one viewer |
| `--no-tv` | `LYNCH_UI_TV=0` | on: no trading view and no button for it |
| | `LYNCH_UI_TV_FETCHES` / `LYNCH_UI_TV_IDLE` | `4` / `75`: Yahoo requests the trading view makes at once / seconds a viewer that stopped polling keeps its place |
| | `LYNCH_UI_NATIVE_BARS=0` / `LYNCH_UI_TV_FORCE_OPEN=1` | on when built / off: build the trading view's bars with numpy instead / refresh series at market-hours pace at any hour (benchmarks) |
| `--allow-net` / `--allow-host` | `LYNCH_UI_ALLOWED_NETS` / `LYNCH_UI_ALLOWED_HOSTS` | none: extra client networks / Host names beyond LAN + Tailscale |

The LLM context window is the smaller of `--llm-ctx` and the loaded model's real context (from LM Studio's `/api/v0/models`). To try the AI card without a model, run `python -m ui.tests.fake_lmstudio --port 18080` (streams a canned reply; `--delay` sets the typing speed) and start the server with `--llm-url http://127.0.0.1:18080`.

## AI inference queue (local model + Gemini)

Every new AI overview request (a ticker without one today) is admitted by `ui/inference_queue.py` against two watermarks on the last minute's AI requests:

| Requests in the last minute | Who writes the overview |
|---|---|
| up to the **low watermark** (`--llm-rpm`, default 24) | the local model; at most that many waiting or being written at once, so a model slower than its setting never builds more than about a minute of backlog |
| low … **high watermark** (= `--llm-rpm` + `--gemini-rpm`) | **Gemini** (`--gemini-model`, default `gemini-flash-lite-latest`) through its OpenAI-compatible API, streamed like the local model, at most 15 a minute and 975 a Pacific day (`ui/gemini.py`) |
| above the high watermark | nobody for now: the page shows the rule-based **Quick overview** with **↻ Retry AI**, which counts down `retry_after` (when a slot frees up) |

- **No model up:** a local model that doesn't answer its status probe counts as full, so its share goes to Gemini. Without Gemini (no `GEMINI_API_KEY`, or `--no-gemini`), the Quick overview.
- **A failure** falls back to the Quick overview with ↻ Retry AI too: Gemini's 503 ("high demand") or 429, LM Studio gone, a timeout.
- **One overview per ticker per day.** Visitors asking for a ticker that is being written join that generation. A complete overview is cached with the analysis and reused until the cache resets at midnight: ↻ Retry AI doesn't regenerate it, and neither does a quant ↻ Refresh or an LFU eviction. A partial reply (fewer than three sections) shows ↻ Retry AI, and a retry replaces it.
- **The nightly pre-cache** only uses the local model and is never paused by the window (one ticker at a time); its requests still count toward the minute.
- **Gemini's budget** (`<cache_dir>/gemini_budget.sqlite3`) counts every request sent, answered or not, per rolling minute and per Pacific day, across restarts. A 429 closes Gemini for the delay Google names (30 s when it names none; until midnight Pacific for the per-day quota). Measured on the free tier (2026-10-07): a burst of 25 got 429s naming `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, `quotaValue 15`, for gemini-3.5-flash-lite, the model `gemini-flash-lite-latest` points to. Google doesn't publish the per-day cap any more (it points to AI Studio); 975 leaves room for the daily scans' backup tier, which uses the same model.
- **No reasoning switch for Gemini.** Flash-Lite doesn't think, and the API refuses any attempt to turn thinking off (400 for `reasoning_effort: "none"` and for `thinking_budget: 0`). A refused request still counts: sending it once cost one of the minute's 15 and the 15th request got a 429. `"minimal"` is accepted but turned a ~1 s first token into ~7 s.
- `/api/health` shows the queue under `cache.inference`: watermarks, last minute's local and paused requests, Gemini's minute and day. The stats page charts where overviews came from (see *Stats page*).

### Benchmark: 50 requests a minute (2026-10-07)

`ui/tests/inference_bench.py` sends AI overview requests at a fixed rate through the portal's real `JobManager`:
- **Prompts.** Every request is a different ticker, and no ticker is used twice in a day across runs.
  - The tickers come from `database/*.txt`, analysed once a day from live Yahoo data with FMP off.
  - A prompt the server has seen before comes almost entirely from Splash's prefix cache (~610 of ~630 tokens instead of the 256-token system prompt), which made the local model's TTFT look about 2× better than it is.
- **Warm-up.** Two spare tickers first load the weights and prime the shared system prompt.
- **Timing.** Time to first token is measured from the request, so it includes the queue wait. Total time runs from the request to the finished overview.
- **Output.** The results are written to `tmp/`, with CDF charts; `--compare` draws several runs on one chart.

Setup: Qwen3.6-35B-A3B on standalone Splash (M3 Pro 36 GB, `--llm-parallel 4` = the batch width, 32K context) and Gemini Flash-Lite at 15 a minute. Each run sent 50 requests in 1 minute; every local request had exactly the 256-token system prompt cached.

| Low watermark | Local | Gemini | Quick overview | Got AI | AI started by 1:00 | Local TTFT p50 / p90 | Local total p50 / p100 |
|---|---|---|---|---|---|---|---|
| 12/min | 12 | 15 | 23 | **27 (54%)** | 27 (all done) | 8.4 / 18.3 s | 19.7 / 31.0 s |
| 16/min | 16 | 15 | 19 | **31 (62%)** | 31 (all done) | 13.2 / 28.0 s | 27.5 / 37.8 s |
| 20/min | 20 | 15 | 15 | **35 (70%)** | 33 (30 done, 3 typing) | 19.5 / 37.4 s | 29.9 / 49.7 s |
| 24/min | 24 | 15 | 11 | **39 (78%)** | 32 (30 done, 2 typing) | 20.9 / 48.2 s | 34.6 / 61.4 s |

Gemini over the 60 requests of the four runs: TTFT p50 0.70 s, p90 0.89 s; total p50 1.60 s, p100 3.47 s; no failures.

**Reading the table:**
- No AI request failed in any of the four runs.
- The local model kept pace up to 24 a minute: every overview it took finished within 61 s.
- 24 is the default: it turns the most requests into AI overviews (78% at 50 a minute) while the slowest local overview still finishes in about a minute.
- Past that, the local queue (wait p50 19.5 s at 24/min) would grow by the minute.
- For a first token within ~20 s at p90, use 12.

**Quality** (the same 4 tickers on both models, NVDA / KO / INTC / PLTR): both models had three sections every time, copied the reverse-DCF numbers (4/4) and verdict, copied the analyst-target sentence (the local model once lowercased it mid-sentence), and used no number that wasn't in the data.
- **Gemini** reads more fluently and cites more of the data.
- **The local A3B** reasons more but slips more: "losing money at -4.4% annually due to its high Beta" (INTC), "realistic because historical mean PEG is 4.43, meaning the stock is already cheap" (KO).
- **Gemini's slips:** a generic KO reverse DCF, and "burning through its $4.9B in FCF" for a positive FCF.
- **Verdict:** offloading to Gemini doesn't lower the overview's quality.

## Local AI tuning

Measured on 2026-10-04 against LM Studio's **Splash** engine on an M3 Pro 36 GB laptop, serving the portal over Wi-Fi, with real analyses of 26 large caps. Two models were tested: `qwen3.8-27b-splash` (dense 27B, 4-bit, 17.4 GB) and `qwen3.6-35b-a3b-splash` (MoE with 3B active, 4-bit, 20.9 GB). Re-run with `ui/tests/llm_bench.py` (see the end of this section).

### Where the time goes

One overview = **prefill** (≈ time to first token) + **decode** (reply tokens ÷ tokens/s).

| | 27B dense | 35B-A3B MoE | Scales with |
|---|---|---|---|
| Prefill | ~100 tok/s | ~800 tok/s | prompt tokens not already cached |
| Fixed cost per request | ~0.9 s | ~0.4 s | — |
| Decode, one stream | ~22 tok/s | ~70 tok/s | reply tokens (memory bandwidth, active params) |

**Prefix cache.** LM Studio reuses the KV cache of a prompt prefix once it has seen that prefix **twice**. A shared 800-token prefix plus 100 new words took 8.0 s → 7.9 s → 2.0 s → 2.0 s on the 27B (1.4 → 1.5 → 0.4 s on the A3B), and an exact repeat took 0.7 s. So the static instructions come first, as the system message, and cost nothing after warm-up. Only the per-ticker data is prefilled: on the 27B, every 100 data tokens add about 1 s of TTFT (about 0.67 s on an M6 whose prefill is ~1.5× faster). The LM Studio developer log shows it as `Done · input 606 · cached 192 · output 331 · TTFT 4.3s`: the 192-token system prompt came from the cache.

### The prompt (`ui/llm.py`: `PORTAL_SYSTEM` + `portal_data()`)

27B, 1 user, 6 tickers each:

| Prompt | Prompt tokens | Reply tokens | TTFT | Time / overview | Overviews/min |
|---|---|---|---|---|---|
| Old: daily-scan DATASET first, task last, "one paragraph" each | 893 | 556 | 6.3 s (9 s cold) | 34.5 s | 1.72 |
| v1: task as system message + compact data, 3-4 sentences | 687 | 389 | 6.0 s | 25.8 s | 2.31 |
| **Shipped**: terse pre-computed data, 2-3 sentences | ~620 | ~300 | **4.4 s** | **17.7 s** | **3.36** |

Rules that got there:
- **Static instructions first, data last.** The old prompt put the data first, so no tokens were ever reused across tickers.
- **Compute in Python, let the model write.** The data block hands the model these, already computed:
  - the reverse-DCF sentence and the Quick Overview's verdict on its assumptions (`assumptions: a stretch`)
  - the whole analyst price-target sentence ("Analysts' target $116.37 (2% downside)."), which 🤖 is told to copy word for word. With only a template, small models skipped it (4/32) or wrote "(-2% downside)"; copying, the A3B included it 32/32 with no sign errors (measured with the longer "Analysts' average price target is $116.37 (2% downside from today's $119.33)." it replaced on 2026-10-05, to match the daily scan's thread)
  - the reverse-DCF verdict is asked for as one plain sentence with its reason, never a label like "Assumptions: achievable"
  - "(fortress)" next to an AAA/AA+ credit rating
  - the options-edge reading
  - the Quick Overview's red-flag headlines

  Models are bad at signs, thresholds and verdicts. Even the 27B wrote "2% upside" for a target 2% *below* the price until the word "downside" was in the data.
- **The company description is in the data.** The Quick Overview's first two sentences of Yahoo's summary feed 🤖 ("start with what the company does and its moat"), so a small model knows what the company sells. 📊 Reverse DCF covers only the math, what it demands operationally, and the verdict.
- **Nothing twice.** Income lines are cut to revenue, op income and EPS plus RED items. Credit is cut to interest cover and net debt/EBITDA. Red flags that repeat a data line are dropped.
- **2-3 sentences per section.** Reply length is the biggest lever on total time, because decode is 5-10× slower per token than prefill.
- **For small models:** "Quote numbers exactly as given, never add or combine them, and never mention 'the data' or these instructions". Without it the A3B wrote "86% combined operating and net margin" and "which the data explicitly labels as a stretch". The parser also accepts the label words after a wrong emoji (the A3B once wrote "🧧 Stomach Test:", and the whole section was lost).

### Model comparison (shipped prompt, 1 user, same 6 tickers)

| Model | TTFT | Decode | Time / overview | Overviews/min | Complete 3/3 |
|---|---|---|---|---|---|
| qwen3.8-27b-splash | 4.4 s | 22 tok/s | 17.7 s | 3.4 | 6/6 |
| qwen3.6-35b-a3b-splash | **0.84 s** | **70 tok/s** | **4.9 s** | **12.2** | 6/6 (5/6 before the label fix) |

**Quality.**
- Both models cite the reverse-DCF numbers and the analyst target correctly.
- The **27B** writes better prose. It knows the moats on its own (CUDA for NVIDIA, the dealer network for Caterpillar), and it reasons about the verdict instead of repeating it.
- The **A3B** is fine for a quick read, but it still slips:
  - It mixes up "the assumptions are realistic" with "the return is poor" (XOM: realistic assumptions called "a stretch").
  - It occasionally misquotes a number (UNH's bear ROI).
  - Its moats are thinner unless the description supplies them.
- The pre-computed verdict fixed most of its contradictions: before it, INTC came out as "realistic only if … miraculously".

**Small models.** `qwen2.5-7b-instruct`, tested through the portal:
- It follows the shipped prompt well. It opens 🤖 with what the company does and ends it with "Analysts' average price target is $619.51 (2% downside from today's $633.91)".
- It quotes the reverse-DCF and red-flag numbers exactly.
- It paraphrases the verdict ("seems optimistic" rather than "a stretch"), sometimes writes a sentence that doesn't make sense ("assumes a significant margin of safety"), and tends to skip what the math demands operationally.
- It once skipped the "🤖:" label but labelled the other two sections. The parser now treats unlabelled text before the first label as the overview, instead of dropping it ("partial reply").

**Pick:** the A3B on standalone Splash with `--llm-parallel 4` when several people use the portal: ~15 overviews/min with a first token in ~2 s (see below). The 27B when you're the only user and want the better write-up.

**Small-model labelling.** About 1 reply in 30 from the A3B had all three paragraphs in order but no labels at all. The parser now assigns exactly three unlabelled paragraphs to overview, reverse DCF and stomach test, in that order.

### Concurrency

LM Studio overlaps requests only when the loaded model's **Max Concurrent Predictions** is above 1 (continuous batching). It is a load-time setting in the model loader's *Load → Advanced* section, and the REST API and `lms` have no parameter for it. It exists for llama.cpp (GGUF) models. **For Splash models the Advanced section reads "No configurable parameters"**, so the server runs one generation at a time and queues the rest:

| Model | Users at once | Overviews/min | Total tok/s | TTFT mean / max |
|---|---|---|---|---|
| 27B | 1 | 3.36 | 16.5 | 4.4 / 5.0 s |
| 27B | 2 | 3.22 | 16.5 | 17.6 / 22.7 s |
| A3B | 1 | 12.2 | 57 | 0.8 / 1.1 s |
| A3B | 2 | 12.1 | 55 | 4.7 / 6.1 s |
| A3B | 4 | 10.8 | 54 | 13.4 / 20.6 s |

- **The optimal number of concurrent requests on Splash is 1, so keep `--llm-parallel 1` (the default).** Total tokens/s is flat, and each extra request only waits a whole generation in LM Studio's queue.
- With one AI worker, the queue stays in the portal, which shows "Queued — position N" instead of a mysteriously long TTFT.
- Two copies of the model would give two queues, but 2 × 17-21 GB does not fit in 32-36 GB.
- A queued user waits *position × time per overview*, so a short reply and a fast model are the levers that help everyone.
- Each ticker's overview is generated once and cached with its analysis for the day, so concurrent requests are always different tickers.

`--llm-parallel N` (`LYNCH_LLM_PARALLEL`) exists for engines that do batch (llama.cpp GGUF with Max Concurrent Predictions > 1, or a future Splash). Set it where total tok/s stops growing in the sweep below, or where TTFT gets too long.

### Standalone Splash server (batches concurrent requests)

[Splash](https://github.com/incoai/splash) itself batches concurrent requests; only LM Studio's integration queues them. Run it instead of LM Studio, with LM Studio's model ejected first:

```bash
brew install incoai/tap/splash
splash serve --model incoai/Qwen3.8-27B-Splash --host 0.0.0.0 --port 8000 --default-reasoning-effort none --max-context 32K --max-memory 30G
python -m ui.server --lan --llm-url http://127.0.0.1:8000 --llm-model incoai/Qwen3.8-27B-Splash --llm-ctx 32768
```

Notes on running it:
- Use the `incoai/…-Splash` packages: `incoai/Qwen3.6-35B-A3B-Splash` is the fast model. `--language-only` is rejected for them ("source selection options require an upstream model ID") because the vision encoder is built into the package.
- `/status` shows the memory plan. On the M3 Pro with `--max-memory 30G`, the 27B gets `maximum_batch_width = 4` (at most 4 streams decode together; the rest wait) and room for 282K KV tokens.
- The portal's client works with Splash unchanged. It notices there is no LM Studio `/api/v0` and stops probing for it.
- **The portal primes Splash's prefix cache itself.**
  - Splash keeps a request's reusable state "at the last whole 32-token page before its generation prompt", so a request can only resume from a state left by an earlier request whose prompt *ended* there. That suits multi-turn chats.
  - This Qwen mixes attention and recurrent layers, and the recurrent state can't be rebuilt from cached attention pages. So a new ticker never reused the system prompt left behind by another ticker's request: `/status` showed 23 sequential requests with 0 hits, 22 `lazy_junctions` and 0 `junction_materializations`. On the 27B, one materialization happened by chance under concurrency.
  - `LocalLLMClient._prime` therefore sends the system prompt once with an empty user message and a 1-token reply. Its saved state ends exactly where every ticker's prompt diverges, and from then on every overview logs `cached 224`: the A3B sweep below had 45 hits and 0 new misses.
  - It primes again, at most once a minute, whenever the server reports `cached_tokens: 0` (a restart or an eviction). LM Studio doesn't report that field, so there the client primes once, which is harmless.

27B, standalone Splash, M3 Pro (each request a different ticker):

| Users at once | Overviews/min | Total tok/s | TTFT mean / max | Tok/s per stream | Latency | Complete 3/3 |
|---|---|---|---|---|---|---|
| 1 | 2.95 | 16.3 | 6.2 / 6.3 s | 23.8 | 20 s | 4/4 |
| 2 | 3.32 | 16.4 | 11.6 / 17.0 s | 13.8 | 33 s | 4/4 |
| 3 | 4.12 | 18.8 | 13.3 / 26.6 s | 9.7 | 42 s | 6/6 |
| 4 | 4.36 | 19.0 | 13.0 / 28.3 s | 7.7 | 50 s | 8/8 |

(The 1-2 user rows were measured before the prefix cache warmed, with ~600 uncached prompt tokens. 6 and 8 users were not run: past the batch width of 4 they only queue.)

**Reading:**
- The batching works, but on an M3 Pro the dense 27B gains little: +17% total tok/s at 4 streams, and +48% overviews/min partly because those replies were shorter.
- The cost is a 2-4× longer TTFT.
- Prompt processing (~95 tok/s on the 18-core GPU) is compute-bound, doesn't speed up with batching, and pauses the other streams' decoding (`--decode-share`, default 0.5).
- With 4 users arriving at once, the mean wait is about the same (~50 s) serial or parallel. Parallel finishes the last user sooner and the first one later.
- **27B on an M3 Pro: `--llm-parallel 4`** (= `maximum_batch_width`) to maximise overviews/min.
  - Normalised to the same ~300-token reply and a warm cache, that is ~3.8 vs ~3.55 overviews/min serial, about +7-10%. The raw table's +48% is inflated by shorter replies and a cold c=1 run.
  - The price is the first token: ~13 s mean instead of ~4 s.
  - Use `--llm-parallel 1` instead if the first token matters more than throughput.
- Batching pays off when processing the prompt is cheap relative to writing the reply, as with the A3B MoE below. Inco reports 357 tok/s combined at 4 streams for the A3B on a 48 GB M5 Pro, and 170 tok/s for the 27B.

35B-A3B, standalone Splash, M3 Pro, primed cache, the same 8 tickers at every level (`maximum_batch_width = 4`):

| Users at once | Overviews/min | Total tok/s | TTFT mean / max | Tok/s per stream | Latency | Complete 3/3 |
|---|---|---|---|---|---|---|
| 1 | 10.65 | 56.3 | 0.82 / 0.93 s | 68.3 | 5.6 s | 8/8 |
| 2 | 13.21 | 68.5 | 1.46 / 1.95 s | 42.7 | 8.9 s | 8/8 |
| 3 | 13.22 | 73.8 | 1.78 / 3.33 s | 32.8 | 12.5 s | 8/8 |
| **4** | **14.27** | **78.2** | 2.74 / 4.75 s | 25.7 | 15.7 s | 8/8 |
| 4 (16 tickers, final prompt) | 14.98 | 82.5 | 1.98 / 4.38 s | 25.8 | 15.2 s | 16/16 |

**The A3B on Splash: `--llm-parallel 4`.**
- At equal reply length (311-335 tokens), that is +34-41% overviews/min and +39-47% total tok/s over one at a time, and the first token still arrives in about 2-3 s.
- At 1 user it matches the A3B under LM Studio (~56 tok/s, ~0.8 s TTFT). LM Studio just can't go past 1.
- This is the fastest setup measured: about 15 overviews/min on an M3 Pro.
- It is about 4.4× the 27B's ~3.4/min, and 8.7× the old prompt on the 27B (1.72/min).
- **On the M6:** warm the cache first (until Splash logs `cached 224`), sweep `--conc 1,2,3,4` on the same tickers, compare overviews/min at equal reply length (or total tok/s), and set `--llm-parallel` to the best level, capped at `maximum_batch_width` from `/status`.
- Inco's stated minimum is 36 GB (48 GB recommended), so on the 32 GB M6 check the memory plan (`/status` → `memory_plan.budget`) and the batch width it leaves.

### For the M6 Mac mini (32 GB, Splash)

- **Both models fit on their own** (17.4 GB and 20.9 GB), but not together. Keep `--llm-ctx 65536` (the prompt is about 650 tokens; the context only caps the KV cache).
- **Prefill ~1.5× faster** means the 27B's TTFT should drop from 4.4 s to about 3 s (≈ 414 uncached tokens ÷ ~150 tok/s, plus overhead). The A3B's TTFT is already under 1 s.
- **Decode is memory-bandwidth bound,** and it is most of the time per overview. Measure it rather than assume it: time per overview ≈ TTFT + ~300 tokens ÷ decode tok/s.
- For concurrency, run **standalone Splash** (above) rather than LM Studio, and sweep `--conc 1,2,3,4`.
- Re-check reply quality with `--show 3`, comparing against the notes above.

```bash
python ui/tests/llm_bench.py --llm-url http://HOST:1234 --llm-model qwen3.6-35b-a3b-splash --conc 1,2,4 --show 2
```

What the benchmark does:
- It analyses 26 large caps live and caches them for the day in `ui/.cache/llm_bench.pkl`.
- It runs max(4, 2·C) overviews at each concurrency C. Each is a different ticker, and each starts with a nonce, so no earlier prompt is an exact cache hit.
- It prints overviews/min, total tok/s, TTFT, per-stream tok/s and how many replies have all 3 sections.
- **Pin `--llm-model`.** Without it, a request sent while models are being swapped can JIT-load whatever model LM Studio lists first.
- If C=2 raises total tok/s, the engine batches.
