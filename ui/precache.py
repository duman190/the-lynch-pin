"""Nightly pre-cache: right after the daily cache resets (local midnight), analyse the tickers visitors looked
up most over the last month, so tomorrow's first lookups are answered from the cache.

    python -m ui.server --lan              # pre-caches up to 100 tickers each night (--precache N, 0 = off)

The list is the stats page's (ui/stats.py, so --lan or --public): the ``count`` most looked-up tickers in the
last 30 days, errors left out; fewer when fewer were looked up. They run one at a time, and with the AI
overview on the next ticker starts only once the previous one's overview is finished, so the model keeps
up and visitors' lookups at night wait behind at most one ticker. Pre-cache lookups are not visitors': they
don't count on the stats page or in the cache's hit rate. They do spend the usual Yahoo calls and, with
enrichment on, FMP requests (within the portal's 24 h cap, ui/fmp_budget.py).

The overviews are written by the pre-cache's own model (--cache-llm-url: Qwen3.8-Flash-Next on Strata behind the
switchboard: ~31 s a ticker with its analysis on the M6, ~50 min for 100) while it answers, else by the online model.
On the switchboard that swaps the online model out, so once the run is over the online model (--llm-url) is primed
again: the swap back and the system prompt's prefill happen now, not on the first visitor's overview.
"""
import datetime as _dt
import threading
import time

START_AFTER = 30.0   # seconds past local midnight (the cache resets on its first access of the new day)
TICKER_TIMEOUT = 1800.0  # a ticker (analysis + AI overview) that takes longer is left behind


class Precacher:
    def __init__(self, jobs, stats, count=100, days=30, now=_dt.datetime.now):
        self.jobs, self.stats, self.count, self.days = jobs, stats, max(0, int(count)), days
        self._now = now
        self._stop = threading.Event()
        self._thread = None
        self.last = None  # summary of the last run, for /api/health

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="lynch-precache", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def seconds_to_next_run(self):
        now = self._now()
        midnight = _dt.datetime.combine(now.date() + _dt.timedelta(days=1), _dt.time())
        return max(1.0, (midnight - now).total_seconds() + START_AFTER)

    def _loop(self):
        while not self._stop.wait(self.seconds_to_next_run()):
            try:
                self.run()
            except Exception as e:  # never let the thread die: try again the next night
                print(f"⚠️  pre-cache: {type(e).__name__}: {e}", flush=True)

    def run(self):
        """Pre-cache now (one pass over today's list); returns the summary."""
        t0 = time.monotonic()
        tickers = self.stats.top_tickers(self.count, self.days)
        ai = self.jobs.llm is not None
        cache = getattr(self.jobs, "cache_llm", None) if ai else None
        print(f"🌙 Pre-cache: {len(tickers)} most looked-up tickers of the last {self.days} days"
              f"{', each with its AI overview' if ai else ''}{self._writer(cache) if ai else ''}", flush=True)
        done = ai_done = 0
        outcomes = {}
        for sym in tickers:
            if self._stop.is_set() or self.jobs._stop:
                break
            outcome, ai_outcome = self.jobs.warm(sym, ai=ai, timeout=TICKER_TIMEOUT)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            done += outcome in ("done", "cached")
            ai_done += ai_outcome in ("done", "cached")
        took = time.monotonic() - t0
        self.last = {"at": time.time(), "tickers": len(tickers), "analysed": done, "outcomes": outcomes,
                     "ai": ai_done if ai else None, "seconds": round(took)}
        print(f"🌙 Pre-cache done in {took / 60:.0f} min: {done}/{len(tickers)} tickers cached"
              f"{f', {ai_done} AI overviews' if ai else ''}"
              f"{'' if done == len(tickers) else f' ({outcomes})'}", flush=True)
        if cache is not None and not (self._stop.is_set() or self.jobs._stop):
            self.last["online_warm"] = self.warm_online()
        return self.last

    @staticmethod
    def _writer(cache):
        """Who writes tonight's overviews, for the log."""
        if cache is None:
            return " (the online model)"
        st = cache.status(force=True)
        if st.get("available"):
            return f" written by {st.get('model')} (--cache-llm-url)"
        return f" — the overnight model is unavailable ({st.get('reason')}): the online model writes them"

    def warm_online(self):
        """Primes the online model's system prompt (ui.llm.LocalLLMClient.warm_up) after a night on the overnight
        model, so the morning's first visitor gets neither the switchboard's swap nor the prompt's prefill."""
        out = self.jobs.llm.warm_up()
        if out["ok"]:
            print(f"🌅 Online model ready: {self.jobs.llm.settings.llm_model or 'the loaded model'} primed with the "
                  f"{out.get('prompt_tokens') or '?'}-token system prompt in {out['seconds']:.0f} s", flush=True)
        else:
            print(f"⚠️  pre-cache: couldn't prime the online model ({out.get('error')}); the first visitor's overview "
                  "primes it instead", flush=True)
        return out
