"""Nightly pre-cache: right after the daily cache resets (local midnight), analyse the tickers visitors looked
up most over the last month, so tomorrow's first lookups are answered from the cache.

    python -m ui.server --lan              # pre-caches up to 100 tickers each night (--precache N, 0 = off)

The list is the stats page's (ui/stats.py, so --lan or --public): the ``count`` most looked-up tickers in the
last 30 days, errors left out; fewer when fewer were looked up. They run one at a time, and with the AI
overview on the next ticker starts only once the previous one's overview is finished, so the local model keeps
up and visitors' lookups at night wait behind at most one ticker. Pre-cache lookups are not visitors': they
don't count on the stats page or in the cache's hit rate. They do spend the usual Yahoo calls and, with
enrichment on, FMP requests (within the portal's 24 h cap, ui/fmp_budget.py).
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
        print(f"🌙 Pre-cache: {len(tickers)} most looked-up tickers of the last {self.days} days"
              f"{', each with its AI overview' if ai else ''}", flush=True)
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
        return self.last
