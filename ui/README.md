# Lynch Pin Quant Portal (web UI)

Dark-mode web UI for the Lynch Pin engine, for PC and phone. Everything lives in `ui/`; outside it, the portal only reads the scan archive that `main.py` writes to `scans/<kind>/` (see *Latest scans*). It uses only the standard library plus the project's existing dependencies.

```bash
python -m ui.server                  # http://127.0.0.1:8765, this machine only
python -m ui.server --lan            # phones/PCs on the same private network (prints the URL to open)
python -m pytest ui/tests -q         # offline tests (fake engine + fake LM Studio), incl. a quick benchmark
python ui/tests/test_benchmark.py    # throughput benchmark: req/s and latency per endpoint (AI off)
python ui/tests/cold_bench.py --base http://127.0.0.1:8765 --clients 24 --count 48   # cold lookups/min (real Yahoo)
python ui/tests/cold_bench.py --base http://127.0.0.1:8790 --clients 24 --count 48 --visitors --home   # same, as --public visitors
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

`--public` listens on 127.0.0.1 only (it refuses `--lan` or another `--host`), so the tunnel is the only way in and no router port is opened. It accepts any `Host` name, reads each visitor's IP from Cloudflare's `CF-Connecting-IP` header (trusted only from the local tunnel), allows **one analysis at a time per visitor** (a second ticker gets "busy" and the page retries; cached tickers, polls and joining a ticker someone else is already analysing are free), turns `/api/cache` off and leaves other visitors' tickers, the LAN setting and the local model's address out of `/api/health`. What the portal can serve stays the same as on the LAN: its own pages, charts by ticker and analysis JSON — never other files on this machine. There is still no login: put Cloudflare Access in front of the hostname to choose who can use it. Visitors' lookups use this machine's Yahoo quota (and FMP's, with enrichment on).

## Features
- Hero artwork built from the Lynch Pin badge.
- **Latest scans** (home page, under the ticker search): an iOS-style widget for each of the last 7 daily `run_lynch.sh` scans (MAGS, QQQ, SCHD, SMH, IGV, X Favorite 100, Portfolio X-Ray), oldest to newest, opening on the newest. Each widget previews the scan's first post and chart; swipe (phone), use the ‹ › arrows, or tap a day in the date strip above to switch scans. Tapping a widget opens the whole thread at `?scan=mags`, laid out like the X post, with each reply's chart (tap to enlarge) and an *Analyze $TICKER* shortcut; ‹ › links at the bottom step to the neighbouring days.
  - **Storage:** with `--post` / `--post_threads`, `main.py` archives the thread in its own folder per scan kind: `scans/<kind>/scan.json` (every post's text plus the run's raw AI overview: sentiment, portfolio thesis, each ticker's untruncated overview) and that run's charts. Monday's `scans/mags/GOOGL_valuation.png` is never overwritten by Saturday's `scans/fintwit/GOOGL_valuation.png`; the next MAGS run replaces only `scans/mags/`. `scans/` is local and git-ignored; `images/` stays the Threads upload area that each run clears and pushes to GitHub.
  - **Caching:** the scan index is held in memory, re-read daily after 3 PM (the 1 PM run is archived by then) and as soon as a `scan.json` changes on disk (checked at most once a minute). Chart URLs carry the scan's version, so browsers keep them until next week's run replaces them.
  - `python -m social.scan_archive --backfill logs/run_YYYYMMDD.log ...` rebuilds a scan folder from a run log and the charts that run pushed to git.
- **Socials** (home page, under Latest scans): an app-icon widget linking to @lynch_pin_quant on X, Instagram and Threads, and a **Latest on X** widget with the account's 5 latest posts (text, chart thumbnail, replies / reposts / likes / views; tap to open on X). It is read with `main.py`'s X posting tokens (`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`, from `venv/bin/activate`, else the environment); the tokens never reach the page. X reads are metered, so the server reads X once a day in the background, at 9 AM Pacific (`--social-read-at HH:MM`, `LYNCH_UI_SOCIAL_TZ` = `America/Los_Angeles`, so PDT / PST whatever the server's clock says; on startup if that day's read was missed). That is before the 1 PM scan posts, so the widget shows what else is on X rather than repeating Latest scans. Page views never call X. The server keeps the posts and their re-encoded images in `ui/.cache/socials/` and serves everyone from there; a failed read keeps the last posts and is retried an hour later. Without tokens the widget is hidden and the links stay.
- **Home button:** on a ticker page or a scan thread, the ⌂ Home pill in the top bar (or the logo) returns to the home page (search, Latest scans) without reloading; Back/Forward work as usual.
- Ticker search at `?t=MSFT`, which you can bookmark and share. It shows valuation (PEG, Dev SD bell, 5Y Bull/Base/Bear ROI), the same chart `main.py` renders, the income grade, the credit rating, technicals and the 6M edge. Results stream in stage by stage.
- An AI overview from a local LM Studio server that types in real time (Server-Sent Events), with live time to first token, tokens/s, token count and thinking tokens; a reasoning model's thinking streams into a collapsible box. The prompt is a static system message (the task) plus a terse, pre-computed data block for the ticker (company profile, analysts' target, the reverse-DCF math and the Quick Overview's red flags), answered in three 2-3 sentence sections (see *Local AI tuning*). Each ticker's overview is generated once and cached with its analysis for the day. When no model is loaded, the UI shows "AI offline" and keeps working.
- **Quick Overview** with `--no-ai`: the AI overview's three sections built by fixed rules from the same data, no model involved (`ui/quick.py`). *Overview*: name, sector, HQ, employees, market cap, Yahoo's business summary, margins, dividend, analyst consensus and a valuation snapshot. *Reverse 5Y DCF*: "X% base ROI requires EPS to compound at Y%/yr for 5 years and a re-rating from Zx forward PE to a terminal ZZx" (the daily scan's Base ROI math), split into the EPS and multiple contributions, with how demanding those assumptions are. *Stomach test*: red flags from thresholds such as trailing PE > 50, forward PE > 40, growth > 40%, PEG ≥ 2.5, PEG > 1 SD above its mean, base ROI < 9%, a losing bear case, shrinking revenue, an income grade below A (subpar; C/D are serious), a credit rating below A (balance-sheet risk; junk is serious), negative free cash flow, an uncovered dividend, a falling trend, high beta or short interest.
- With `--no-ai` there is no **↻ Refresh**: a cached ticker stays cached until midnight (or eviction), so repeat lookups never spend Yahoo calls again. Uncacheable results (errors, a Yahoo 429, a missing PEG history) still re-run when searched again.
- **Price levels** in the Technicals card, computed with the experimental trade assistant's toolkit (`experimental/quant_engine.py`): support and resistance from clustered pivots (6 months), the volume point of control (3 months), a 1-month expected range from realised volatility, and the 52-week range. Each level shows its chance of being touched within a month.
- **Deep Dive Prompt** (above *How to read*): a research brief to paste into Claude, ChatGPT or Gemini. It casts the model as a hedge-fund portfolio manager and walks it through the last two earnings reports, call transcripts, the 10-Q, news, management's answers and red flags. It then makes the model loop, stress-testing its bull and bear case until another pass changes nothing, and asks for a verdict against the S&P 500 with price levels and signals to watch. The quant data, the price levels and the local AI overview are attached at the end. **Copy** works on plain-http LAN and Tailscale URLs too; **Show / Hide** toggles the full text.
- The 5Y Growth value is tagged **Enriched** (Yahoo + FMP) or **Not enriched** (Yahoo only).
- **Concurrent lookups** with the AI overview off: several users' tickers are analysed at the same time, each in its own worker process (1.5 per CPU core, at most 16, and no more than free RAM holds at ~200 MB each; `--workers` to change). A model loaded in LM Studio can take most of an 8 GB Mac: unload it when running `--no-ai`. On an 8-core M1 this takes cold lookups from ~11 to ~117 tickers/min (`ui/tests/cold_bench.py`). Through `--public` with every visitor on its own IP and loading the full home page first (22 requests, ~1.3 MB: Latest scans and Socials included), the same 48 tickers ran at 110 tickers/min (2026-10-05). A burst of 200 visitors at once, each with a new ticker, ran at 62 tickers/min: 12 analyse, 20 queue, the rest get "queue full" and their page retries, and Yahoo answered 429 eleven times, so the circuit breaker paused new work; all 200 finished, the slowest in ~3 min. With the AI overview on, tickers are analysed one at a time unless `--workers` is set. Yahoo throttles at a few hundred tickers in a short burst (each cold lookup makes ~12 Yahoo calls), so the cache below does the heavy lifting under sustained load.
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
| `--llm-parallel` | `LYNCH_LLM_PARALLEL` | `1` AI overviews generated at once (see *Local AI tuning*) |
| `--cache-size` | `LYNCH_UI_CACHE_SIZE` | `500` |
| | `LYNCH_UI_SCANS_DIR` / `LYNCH_UI_SCANS` | `scans/` / `7`: the scan archive and how many scans *Latest scans* shows |
| | `LYNCH_UI_SOCIALS=0` / `LYNCH_UI_SOCIAL_ENV` | on / `venv/bin/activate`: turn the Latest on X read off / where the X tokens are read from |
| `--social-read-at` | `LYNCH_UI_SOCIAL_READ_AT` / `LYNCH_UI_SOCIAL_TZ` | `09:00` / `America/Los_Angeles`: when the daily Latest on X read happens (9 AM PDT / PST) |
| `--benchmark` | `LYNCH_UI_BENCHMARK` | `SPY` (6M edge) |
| `--no-ai` | | AI enabled |
| `--public` | `LYNCH_UI_PUBLIC=1` | off (see *Public access* above) |
| `--workers` | `LYNCH_UI_WORKERS` | `0` = auto: 1 with the AI overview, 1.5 × CPU cores (max 16, capped by free RAM at ~200 MB each) without; above 1, one process each |
| `--enrich` | `LYNCH_UI_ENRICH` | `auto`: FMP multi-source growth when `FMP_API_KEY` is set (`on` / `off` to force) |
| `--allow-net` / `--allow-host` | `LYNCH_UI_ALLOWED_NETS` / `LYNCH_UI_ALLOWED_HOSTS` | none: extra client networks / Host names beyond LAN + Tailscale |

The LLM context window is the smaller of `--llm-ctx` and the loaded model's real context (from LM Studio's `/api/v0/models`). To try the AI card without a model, run `python -m ui.tests.fake_lmstudio --port 18080` (streams a canned reply; `--delay` sets the typing speed) and start the server with `--llm-url http://127.0.0.1:18080`.

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
