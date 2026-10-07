"""Inference-queue benchmark: AI overview requests at a fixed rate, every one a different ticker, through the
portal's real JobManager (local model up to --llm-rpm a minute, Gemini up to --gemini-rpm more, then the Quick
overview). Reports where each request went, time to first token and total time per request, and draws their CDFs.

    python ui/tests/inference_bench.py --llm-url http://HOST:PORT --llm-parallel 4 --minutes 1   # 50/min for 1 min
    python ui/tests/inference_bench.py --llm-url http://HOST:PORT --llm-rpm 16 --tag llm16      # another low watermark
    python ui/tests/inference_bench.py --llm-url http://HOST:PORT --rate 50 --minutes 3 --no-gemini
    python ui/tests/inference_bench.py --fake          # offline: a fake local model (~12/min) and a fake Gemini
    python ui/tests/inference_bench.py --prepare 140   # only analyse tickers (cached for the day)
    python ui/tests/inference_bench.py --compare tmp/inference_bench-llm12.json tmp/inference_bench-llm24.json

Tickers come from database/*.txt (372 unique), are analysed once a day with live Yahoo data (FMP enrichment off,
so the free plan's quota is left alone) in a few processes, and cached in ui/.cache/inference_bench/analyses.pkl.
No two requests share a ticker, so no overview is cached or joined, and no ticker is used twice in a day across
runs (warm-up included; the list is ui/.cache/inference_bench/used.json): a prompt the server has seen before comes
almost entirely from its prefix cache (Splash: ~610 of ~630 tokens instead of the ~256 of the shared system prompt),
which flatters the local model's time to first token. --reuse allows repeats.

Gemini is the real API (GEMINI_API_KEY) and its requests count against the portal's own daily budget
(ui/.cache/gemini_budget.sqlite3): a 2-minute run spends up to 2 × --gemini-rpm of the 975 a day.

Before the timed run, a warm-up (skip with --no-warmup) writes two overviews for spare tickers: the first loads
the weights and leaves the shared system prompt in the server's prefix cache (the client primes it); the second
shows the cache hit (Splash reports ``cached_tokens``, ~240 for the system prompt).

Per request: arrival → route (local / gemini / quick) → outcome. TTFT = arrival to the first word of the answer
(queue wait included); total = arrival to the finished overview; a "quick" request is answered at once with the
Quick overview. Results: <out>/inference_bench.json and the CDF chart <out>/inference_bench.png (default out: tmp/).
"""
import argparse
import copy
import datetime as dt
import json
import math
import os
import pickle
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from ui.config import Settings  # noqa: E402

LISTS = ("nasdaq_100", "fintwit_100", "schd", "igv", "smh", "mag7", "just_for_fun")
BENCH_DIR = os.path.join(ROOT, "ui", ".cache", "inference_bench")
COLORS = {"local": "#5596E8", "gemini": "#D95926"}  # the stats page's series colours (validated on #1A1A1A)
SURFACE, INK, MUTED, GRID = "#1A1A1A", "#E8E8E8", "#9A9A9A", "#2A2A2A"


def ticker_universe():
    seen, out = set(), []
    for name in LISTS:
        path = os.path.join(ROOT, "database", f"{name}.txt")
        if not os.path.exists(path):
            continue
        for line in open(path):
            sym = line.strip().replace("*", "").upper()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


# ── analyses (the quant data each AI prompt is built from) ────────────────────
USED = os.path.join(BENCH_DIR, "used.json")


def used_today():
    try:
        with open(USED) as f:
            d = json.load(f)
        return set(d["tickers"]) if d.get("day") == dt.date.today().isoformat() else set()
    except (OSError, ValueError, KeyError):
        return set()


def mark_used(tickers):
    os.makedirs(BENCH_DIR, exist_ok=True)
    used = sorted(used_today() | set(tickers))  # read before open(…, "w") empties the file
    with open(USED, "w") as f:
        json.dump({"day": dt.date.today().isoformat(), "tickers": used}, f)


def analyses(need, workers=8):
    """{sym: analysis} with at least ``need`` usable tickers, analysed today (cached for the day)."""
    path = os.path.join(BENCH_DIR, "analyses.pkl")
    today = dt.date.today().isoformat()
    cached = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            day, cached = pickle.load(f)
        cached = cached if day == today else {}
    usable = lambda: {s: d for s, d in cached.items() if d}  # noqa: E731
    universe = [s for s in ticker_universe() if s not in cached]
    if len(usable()) < need and universe:
        from ui.jobs import JobManager
        s = Settings()
        s.cache_dir, s.enrich = os.path.join(BENCH_DIR, "cache"), "off"
        jm = JobManager(s, workers=workers, processes=True, llm=None)
        todo = universe[:int((need - len(usable())) * 1.3) + workers]
        print(f"📈 analysing {len(todo)} tickers ({workers} processes, live Yahoo, no FMP)…", flush=True)
        t0, done = time.time(), [0]

        def one(sym):
            outcome, _ = jm.warm(sym, ai=False, timeout=300)
            entry = jm.lookup(sym)
            ok = outcome in ("done", "cached") and entry and (entry.get("_ai_inputs") or {}).get("row")
            cached[sym] = copy.deepcopy(entry) if ok else None
            done[0] += 1
            if done[0] % 10 == 0:
                print(f"   {done[0]}/{len(todo)} ({len(usable())} usable, {time.time() - t0:.0f}s)", flush=True)

        try:
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(one, todo))
        finally:
            jm.shutdown()
        os.makedirs(BENCH_DIR, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump((today, cached), f)
    got = usable()
    if len(got) < need:
        sys.exit(f"only {len(got)} usable analyses for {need} requests: lower --rate / --minutes")
    return got


def fake_analyses(need):
    """MSFT from the offline fakes, renamed per ticker: the prompt differs only in the ticker."""
    from ui.analysis import TickerAnalyzer
    from ui.tests import fakes
    s = Settings()
    s.cache_dir = os.path.join(BENCH_DIR, "fake-cache")
    base = TickerAnalyzer(s, backends=fakes.backends()).run("MSFT")
    out = {}
    for sym in ticker_universe()[:need]:
        d = copy.deepcopy(base)
        d["ticker"], d["name"] = sym, f"{sym} Corp"
        d["_ai_inputs"]["row"] = dict(d["_ai_inputs"]["row"], Ticker=sym)
        out[sym] = d
    return out


# ── the run ──────────────────────────────────────────────────────────────────
def warmup(llm, data, spare):
    """Two overviews outside the run: load the weights, prime the system prompt, confirm the cache hit."""
    from ui.llm import build_portal_messages
    for i, sym in enumerate(spare):
        t0 = time.monotonic()
        text, meta = llm.generate(build_portal_messages(data[sym]))
        usage = meta.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        m = meta.get("metrics") or {}
        print(f"🔥 warm-up {i + 1}/{len(spare)} ({sym}): {time.monotonic() - t0:.1f} s, TTFT {m.get('ttft_s')} s, "
              f"{usage.get('prompt_tokens', '?')} prompt tokens ({'?' if cached is None else cached} cached), "
              f"{m.get('tokens')} reply tokens at {m.get('tok_s')} tok/s", flush=True)


def visitor(jm, sym, t_arrival, results, routed):
    """One visitor asks for the AI overview of ``sym`` and watches it until it is finished."""
    t0 = time.monotonic()
    rec = {"ticker": sym, "t": round(t_arrival, 2)}
    snap = jm.request_ai(sym)
    routed[sym] = "quick" if snap.get("status") in ("unavailable", "error", "busy") else snap.get("backend", "local")
    if snap.get("fallback") == "quick" or snap.get("status") in ("unavailable", "error", "busy"):
        rec.update(route="quick", outcome="quick", reason=snap.get("reason") or snap.get("status"),
                   retry_after=snap.get("retry_after"), total_s=round(time.monotonic() - t0, 3), ttft_s=None)
        results.append(rec)
        return
    rec["route"] = snap.get("backend", "local")
    job = jm._ai_inflight.get(sym) or jm._recent_ai.get(sym)
    st, first = job.stream, None
    with st.cv:
        while st.final is None:
            if first is None and st.content.strip():
                first = time.monotonic() - t0
            st.cv.wait(0.25)
        if first is None and st.content.strip():
            first = time.monotonic() - t0
        final = st.final
    total = time.monotonic() - t0
    m, u = final.get("metrics") or {}, final.get("usage") or {}
    ok = final.get("status") == "done"
    rec.update(outcome=("complete" if final.get("complete") else "partial") if ok else "failed",
               status=final.get("status"), error=None if ok else final.get("error"),
               ttft_s=round(first, 3) if ok and first is not None else None, total_s=round(total, 3),
               wait_s=round((job.started or job.created) - job.created, 3), model_ttft_s=m.get("ttft_s"),
               tok_s=m.get("tok_s"), tokens=m.get("tokens"), model=final.get("model_short") or final.get("model"),
               prompt_tokens=u.get("prompt_tokens"),
               cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
               narrative=final.get("narrative"))
    results.append(rec)


def run(a):
    from ui.gemini import GeminiBudget, GeminiClient
    from ui.jobs import DayStore, JobManager
    from ui.llm import LocalLLMClient

    n = int(round(a.rate * a.minutes))
    s = Settings()
    s.llm_rpm, s.llm_parallel, s.llm_reasoning, s.llm_ctx = a.llm_rpm, a.llm_parallel, "off", a.llm_ctx
    s.gemini_rpm, s.gemini_rpd, s.gemini_model = a.gemini_rpm, a.gemini_rpd, a.gemini_model
    servers = []
    if a.fake:
        from ui.tests import fake_lmstudio
        data = fake_analyses(n)
        chunks = len(fake_lmstudio._tokens(fake_lmstudio.canned_reply("$MSFT x")))
        local_s = 60.0 * a.llm_parallel / max(1, a.llm_rpm)  # one overview per stream: llm_rpm a minute in all
        lhttpd, _ = fake_lmstudio.serve(state={"delay": local_s / chunks, "streams": a.llm_parallel})
        ghttpd, _ = fake_lmstudio.serve(state={"delay": 2.0 / chunks, "error_rate": a.fake_gemini_503})
        servers = [lhttpd, ghttpd]
        s.llm_base_url = f"http://127.0.0.1:{lhttpd.server_address[1]}"
        s.cache_dir = os.path.join(BENCH_DIR, f"fake-run-{os.getpid()}")
        gemini = None if a.no_gemini else GeminiClient(
            s, "fake", budget=GeminiBudget(os.path.join(s.cache_dir, "g.sqlite3"), s.gemini_rpm, s.gemini_rpd),
            base_url=f"http://127.0.0.1:{ghttpd.server_address[1]}/v1")
        print(f"🧪 fake local model: {a.llm_parallel} streams × {local_s:.0f} s per overview = {a.llm_rpm}/min; "
              f"fake Gemini: 2 s per overview, {a.fake_gemini_503:.0%} answered 503", flush=True)
    else:
        if not a.llm_url:
            sys.exit("--llm-url is required (or --fake)")
        used = set() if a.reuse else used_today()
        data = analyses(n + 2 + len(used), a.prepare_workers)  # + 2 spare tickers for the warm-up
        data = {k: v for k, v in data.items() if k not in used}
        s.llm_base_url, s.llm_model = a.llm_url.rstrip("/"), a.llm_model
        gemini = None if a.no_gemini else GeminiClient.from_settings(s)
        if gemini is None and not a.no_gemini:
            print("⚠️  GEMINI_API_KEY not set: local model only", flush=True)
    llm = LocalLLMClient(s)
    st = llm.status(force=True)
    if not st.get("available") and not a.allow_offline:
        sys.exit(f"local model unavailable: {st.get('reason')} (use --allow-offline to run on Gemini alone)")
    if len(data) < n + (0 if a.fake or a.no_warmup else 2):
        sys.exit(f"only {len(data)} analysed tickers not used yet today: analyse more with --prepare N")
    tickers = list(data)[:n]
    spare = list(data)[n:n + 2]
    if not a.fake:
        mark_used(tickers + ([] if a.no_warmup else spare))
    if not a.fake and not a.no_warmup and st.get("available") and spare:
        warmup(llm, data, spare)
    store = DayStore()
    for sym in tickers:
        store.put(sym, data[sym])
    jm = JobManager(s, analyzer=object(), store=store, llm=llm, gemini=gemini)  # no lookups: all analysed already
    if gemini is not None:
        b = gemini.budget.status()
        print(f"🔑 Gemini {gemini.model_id}: {b['today']}/{b['rpd']} used today (Pacific)", flush=True)
    print(f"🧠 local {st.get('model_short') or st.get('model') or '?'} at {s.llm_base_url}: "
          f"low watermark {s.llm_rpm}/min, {s.llm_parallel} streams; high watermark {jm.inference.high}/min", flush=True)
    print(f"🚦 {n} requests at {a.rate:g}/min for {a.minutes:g} min, every one a different ticker…", flush=True)

    results, threads, routed = [], [], {}
    gap = 60.0 / a.rate
    start = time.monotonic()
    try:
        for i, sym in enumerate(tickers):
            while time.monotonic() < start + i * gap:
                time.sleep(0.01)
            t = threading.Thread(target=visitor, args=(jm, sym, time.monotonic() - start, results, routed), daemon=True)
            t.start()
            threads.append(t)
            if (i + 1) % max(1, int(a.rate)) == 0:
                time.sleep(0.05)  # let the last visitor's admission land
                routes = list(routed.values())
                print(f"   minute {int((i + 1) / a.rate)}: {i + 1} sent · local {routes.count('local')} · "
                      f"gemini {routes.count('gemini')} · quick {routes.count('quick')} · "
                      f"{sum(r['outcome'] in ('complete', 'partial') for r in results)} AI overviews finished", flush=True)
        limit = time.monotonic() + a.drain
        for t in threads:
            t.join(max(0.0, limit - time.monotonic()))
    finally:
        jm.shutdown()
        for h in servers:
            h.shutdown()
            h.server_close()
    stuck = n - len(results)
    report(results, a, n, stuck, jm, time.monotonic() - start)


# ── report ───────────────────────────────────────────────────────────────────
def q(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p * len(v)) - 1))] if v else None


def fmt(x):
    return "—" if x is None else (f"{x:.2f}s" if x < 10 else f"{x:.1f}s" if x < 100 else f"{x:.0f}s")


def report(results, a, n, stuck, jm, elapsed):
    by = lambda key, val: [r for r in results if r.get(key) == val]  # noqa: E731
    local, gemini, quick = by("route", "local"), by("route", "gemini"), by("route", "quick")
    ai = [r for r in results if r["outcome"] in ("complete", "partial")]
    complete = [r for r in ai if r["outcome"] == "complete"]
    failed = by("outcome", "failed")
    pct = lambda k: f"{100 * k / n:.0f}%" if n else "—"  # noqa: E731
    print("\n" + "═" * 78)
    print(f"Requests: {n} unique tickers at {a.rate:g}/min over {a.minutes:g} min (run took {elapsed:.0f} s)")
    print(f"Routed:   local {len(local)} ({pct(len(local))}) · Gemini {len(gemini)} ({pct(len(gemini))}) · "
          f"Quick overview at once {len(quick)} ({pct(len(quick))})")
    print(f"AI overviews delivered: {len(ai)}/{n} = {pct(len(ai))} "
          f"(complete {len(complete)}, partial {len(ai) - len(complete)}); "
          f"failed → Quick overview {len(failed)}" + (f"; still running at the end {stuck}" if stuck else ""))
    if failed:
        errs = {}
        for r in failed:
            key = f"{r['route']}: {(r.get('error') or r.get('status') or '?')[:60]}"
            errs[key] = errs.get(key, 0) + 1
        for k, v in sorted(errs.items(), key=lambda kv: -kv[1]):
            print(f"   {v} × {k}")
    window = a.minutes * 60
    started = [r for r in ai if r.get("ttft_s") is not None and r["t"] + r["ttft_s"] <= window]
    finished = [r for r in started if r["t"] + r["total_s"] <= window]
    print(f"By {int(window // 60)}:{int(window % 60):02d} (the end of the arrivals): AI started for {len(started)}/{n} "
          f"= {pct(len(started))} ({len(finished)} done, {len(started) - len(finished)} still typing)")
    target = a.target
    print(f"Target: ≥{target:.0%} of requests get an AI overview → "
          f"{'✅ met' if n and len(ai) / n >= target else '❌ missed'} ({pct(len(ai))})")

    print(f"\n{'':10}{'n':>4} │ {'TTFT p50':>9}{'p90':>8}{'p99':>8}{'max':>8} │ "
          f"{'total p50':>10}{'p90':>8}{'p99':>8}{'max':>8} │ {'wait p50':>9}")
    for name, rows in (("local", [r for r in ai if r["route"] == "local"]),
                       ("gemini", [r for r in ai if r["route"] == "gemini"]), ("all AI", ai)):
        tt = [r["ttft_s"] for r in rows if r.get("ttft_s") is not None]
        tot = [r["total_s"] for r in rows]
        wt = [r["wait_s"] for r in rows if r.get("wait_s") is not None]
        print(f"{name:10}{len(rows):>4} │ {fmt(q(tt, .5)):>9}{fmt(q(tt, .9)):>8}{fmt(q(tt, .99)):>8}{fmt(max(tt) if tt else None):>8} │ "
              f"{fmt(q(tot, .5)):>10}{fmt(q(tot, .9)):>8}{fmt(q(tot, .99)):>8}{fmt(max(tot) if tot else None):>8} │ "
              f"{fmt(q(wt, .5)):>9}")

    for name in ("local", "gemini"):
        rows = [r for r in ai if r["route"] == name and r.get("prompt_tokens")]
        cached = [r["cached_tokens"] for r in rows if r.get("cached_tokens") is not None]
        if rows:
            print(f"{name}: prompt {sum(r['prompt_tokens'] for r in rows) / len(rows):.0f} tokens on average, "
                  + (f"cached {sum(cached) / len(cached):.0f} (min {min(cached)}, max {max(cached)})" if cached
                     else "cached tokens not reported"))

    minutes = int(math.ceil(a.minutes))
    print(f"\n{'minute':>6} │ {'sent':>4} {'local':>5} {'gemini':>6} {'quick':>5} │ {'AI delivered':>12}")
    for m in range(minutes):
        rows = [r for r in results if m * 60 <= r["t"] < (m + 1) * 60]
        got = sum(r["outcome"] in ("complete", "partial") for r in rows)
        print(f"{m + 1:>6} │ {len(rows):>4} {sum(r['route'] == 'local' for r in rows):>5} "
              f"{sum(r['route'] == 'gemini' for r in rows):>6} {sum(r['route'] == 'quick' for r in rows):>5} │ "
              f"{got:>5} ({100 * got / max(1, len(rows)):.0f}%)")
    print("Inference queue:", json.dumps(jm.inference.status()))

    os.makedirs(a.out, exist_ok=True)
    stem = os.path.join(a.out, "inference_bench" + (f"-{a.tag}" if a.tag else ""))
    with open(stem + ".json", "w") as f:
        json.dump({"args": vars(a), "requests": n, "elapsed_s": elapsed, "results": sorted(results, key=lambda r: r["t"]),
                   "inference": jm.inference.status()}, f, indent=1)
    png = cdf_chart(results, a, stem + ".png", n, len(ai), len(started))
    print(f"\n📄 {stem}.json\n📈 {png}")


def cdf_chart(results, a, path, n, delivered, started=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    secs = FuncFormatter(lambda x, _: f"{x:g}s" if x < 60 else f"{x / 60:g}m")
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.4), dpi=150, facecolor=SURFACE)
    panels = (("ttft_s", "Time to first token", "from the request, queue wait included"),
              ("total_s", "Total time", "from the request to the finished overview"))
    for ax, (key, title, sub) in zip(axes, panels):
        ax.set_facecolor(SURFACE)
        lo, hi = math.inf, 0.0
        for route in ("local", "gemini"):
            v = sorted(r[key] for r in results if r["route"] == route and r["outcome"] in ("complete", "partial")
                       and r.get(key) is not None)
            if not v:
                continue
            lo, hi = min(lo, v[0]), max(hi, v[-1])
            ys = [(i + 1) / len(v) for i in range(len(v))]
            ax.step([v[0]] + v, [0] + ys, where="post", color=COLORS[route], lw=2,
                    label=f"{'Local model' if route == 'local' else 'Gemini'} (n={len(v)}, p50 {fmt(q(v, .5))}, "
                          f"p90 {fmt(q(v, .9))}, p100 {fmt(v[-1])})")
            p50 = q(v, .5)
            ax.plot([p50], [0.5], "o", ms=7, color=COLORS[route], mec=SURFACE, mew=2, zorder=3)
            ax.plot([v[-1]], [1.0], "D", ms=6, color=COLORS[route], mec=SURFACE, mew=1.5, zorder=3)  # p100
            ax.annotate(f"p100 {fmt(v[-1])}", (v[-1], 1.0), xytext=(0, 8), textcoords="offset points",
                        ha="center", va="bottom", color=INK, fontsize=8.5)
        if hi > 0 and hi / max(lo, 0.01) > 20:
            ax.set_xscale("log")
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.xaxis.set_minor_formatter(NullFormatter())
        ax.xaxis.set_major_formatter(secs)
        ax.set_ylim(0, 1.1)
        ax.set_yticks([0, .25, .5, .75, 1])
        ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.grid(True, color=GRID, lw=1)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#3A3A3A")
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.set_xlabel("log scale" if ax.get_xscale() == "log" else "", color=MUTED, fontsize=8.5)
        ax.set_title(f"{title}\n", color=INK, fontsize=12, fontweight="bold", loc="left")
        ax.text(0, 1.02, sub, transform=ax.transAxes, color=MUTED, fontsize=9, va="bottom")
        if ax.get_legend_handles_labels()[0]:
            leg = ax.legend(loc="upper left", bbox_to_anchor=(0, -0.16), fontsize=8.5, frameon=False, labelcolor=INK)
            for h in leg.legend_handles if hasattr(leg, "legend_handles") else leg.legendHandles:
                h.set_linewidth(3)
    quick = sum(r["route"] == "quick" for r in results)
    by_end = "" if started is None else f" ({started} started by the end of the arrivals)"
    fig.suptitle(f"AI overview at {a.rate:g} requests/min for {a.minutes:g} min, every request a different ticker\n"
                 f"{delivered}/{n} got an AI overview ({100 * delivered / max(1, n):.0f}%){by_end} · "
                 f"{quick} got the Quick overview at once", color=INK, fontsize=11, x=0.01, ha="left",
                 linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


# Local runs side by side: one blue ramp, darker = lower low watermark (validated as an ordinal ramp on #1A1A1A)
RAMP = ("#1c5cab", "#3987e5", "#6da7ec", "#b7d3f6")


def compare_chart(paths, path):
    """Several runs on the same two CDFs: a local line per low watermark, Gemini pooled over the runs."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    runs = sorted((json.load(open(p)) for p in paths), key=lambda d: d["args"]["llm_rpm"])
    if len(runs) > len(RAMP):
        sys.exit(f"at most {len(RAMP)} runs per chart")
    secs = FuncFormatter(lambda x, _: f"{x:g}s" if x < 60 else f"{x / 60:g}m")
    fig, axes = plt.subplots(1, 2, figsize=(13, 6.6), dpi=150, facecolor=SURFACE)
    ok = lambda r: r["outcome"] in ("complete", "partial")  # noqa: E731
    summary = []
    for d in runs:
        res, n = d["results"], d["requests"]
        window = d["args"]["minutes"] * 60
        got = [r for r in res if ok(r)]
        started = [r for r in got if r.get("ttft_s") is not None and r["t"] + r["ttft_s"] <= window]
        summary.append(f"{d['args']['llm_rpm']}/min → {len(got)}/{n} ({100 * len(got) / n:.0f}%)")
        d["_started"] = len(started)
    for ax, (key, title, sub) in zip(axes, (("ttft_s", "Time to first token", "from the request, queue wait included"),
                                            ("total_s", "Total time", "from the request to the finished overview"))):
        ax.set_facecolor(SURFACE)
        series = [(f"Local, low watermark {d['args']['llm_rpm']}/min", RAMP[i],
                   [r[key] for r in d["results"] if r["route"] == "local" and ok(r) and r.get(key) is not None])
                  for i, d in enumerate(runs)]
        series.append((f"Gemini, {runs[0]['args']['gemini_rpm']}/min (all runs)", COLORS["gemini"],
                       [r[key] for d in runs for r in d["results"] if r["route"] == "gemini" and ok(r)
                        and r.get(key) is not None]))
        for name, color, v in series:
            v = sorted(v)
            if not v:
                continue
            ys = [(i + 1) / len(v) for i in range(len(v))]
            ax.step([v[0]] + v, [0] + ys, where="post", color=color, lw=2,
                    label=f"{name}: n={len(v)}, p50 {fmt(q(v, .5))}, p90 {fmt(q(v, .9))}, p100 {fmt(v[-1])}")
            ax.plot([v[-1]], [1.0], "D", ms=6, color=color, mec=SURFACE, mew=1.5, zorder=3)  # p100
        ax.axvline(60, color=MUTED, lw=1, ls=(0, (4, 3)), zorder=1)
        ax.text(60, 0.02, " 1 min", color=MUTED, fontsize=8.5, va="bottom")
        ax.set_xscale("log")
        ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.xaxis.set_major_formatter(secs)
        ax.set_ylim(0, 1.04)
        ax.set_yticks([0, .25, .5, .75, 1])
        ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.grid(True, color=GRID, lw=1)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#3A3A3A")
        ax.tick_params(colors=MUTED, labelsize=9)
        ax.set_xlabel("log scale", color=MUTED, fontsize=8.5)
        ax.set_title(f"{title}\n", color=INK, fontsize=12, fontweight="bold", loc="left")
        ax.text(0, 1.02, sub, transform=ax.transAxes, color=MUTED, fontsize=9, va="bottom")
        leg = ax.legend(loc="upper left", bbox_to_anchor=(0, -0.13), fontsize=8.3, frameon=False, labelcolor=INK)
        for h in leg.legend_handles if hasattr(leg, "legend_handles") else leg.legendHandles:
            h.set_linewidth(3)
    a0 = runs[0]["args"]
    fig.suptitle(f"AI overview at {a0['rate']:g} requests/min for {a0['minutes']:g} min, every request a different "
                 f"ticker, by the local model's low watermark\nGot an AI overview: " + " · ".join(summary),
                 color=INK, fontsize=11, x=0.01, ha="left", linespacing=1.5)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def main(argv=None):
    s = Settings()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--llm-url", help="local model server (LM Studio / Splash), e.g. http://192.168.1.20:8000")
    ap.add_argument("--llm-model", default="", help="model id (default: the first the server lists)")
    ap.add_argument("--llm-rpm", type=int, default=s.llm_rpm, help="low watermark: overviews a minute for the local "
                                                                   f"model (default {s.llm_rpm}, the portal's)")
    ap.add_argument("--llm-parallel", type=int, default=4, help="local overviews written at once (default 4)")
    ap.add_argument("--llm-ctx", type=int, default=32768, help="context window (default 32768, Splash's)")
    ap.add_argument("--no-warmup", action="store_true", help="skip the two warm-up overviews")
    ap.add_argument("--reuse", action="store_true", help="allow tickers already used today (prefix-cache hits)")
    ap.add_argument("--compare", nargs="+", metavar="JSON", help="draw these runs' CDFs on one chart, then exit")
    ap.add_argument("--gemini-rpm", type=int, default=s.gemini_rpm)
    ap.add_argument("--gemini-rpd", type=int, default=s.gemini_rpd)
    ap.add_argument("--gemini-model", default=s.gemini_model)
    ap.add_argument("--no-gemini", action="store_true", help="local model only")
    ap.add_argument("--rate", type=float, default=50, help="AI overview requests a minute (default 50)")
    ap.add_argument("--minutes", type=float, default=2, help="how long requests arrive (default 2)")
    ap.add_argument("--drain", type=float, default=600, help="seconds to wait for the last overviews (default 600)")
    ap.add_argument("--target", type=float, default=0.5, help="share of requests that should get AI (default 0.5)")
    ap.add_argument("--out", default=os.path.join(ROOT, "tmp"), help="where the JSON and the chart go (default tmp/)")
    ap.add_argument("--tag", default="", help="suffix for the output files, e.g. llm16")
    ap.add_argument("--prepare", type=int, metavar="N", help="only analyse N tickers for later runs, then exit")
    ap.add_argument("--prepare-workers", type=int, default=8, help="analysis processes (default 8)")
    ap.add_argument("--allow-offline", action="store_true", help="run even if the local model is unavailable")
    ap.add_argument("--fake", action="store_true", help="offline: fake local model and fake Gemini")
    ap.add_argument("--fake-gemini-503", type=float, default=0.1, help="share of fake Gemini requests answered 503")
    a = ap.parse_args(argv)
    if a.compare:
        os.makedirs(a.out, exist_ok=True)
        print(compare_chart(a.compare, os.path.join(a.out, "inference_bench-compare.png")))
        return
    if a.prepare:
        got = analyses(a.prepare, a.prepare_workers)
        print(f"✅ {len(got)} usable analyses cached for today in {BENCH_DIR}")
        return
    run(a)


if __name__ == "__main__":
    main()
