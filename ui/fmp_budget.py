"""FMP request budget for the portal's growth enrichment.

FMP's free plan allows 250 requests a day and counts each request that reaches it, whatever the answer: an
estimate too thin to use (the lookup shows "Not enriched") still costs one. The daily scans (main.py) need up to
25, so the portal keeps to ``limit`` (225) requests in any rolling 24 hours. A rolling window rather than a
calendar day: FMP's reset time is not documented, and 225 per local day could put up to 450 inside one of FMP's
days. Lookups that never call FMP (enrichment off, no forward earnings, the budget spent) cost nothing.

The engine asks before every request (``engine.growth_estimator.FMP_GATE``, a 429 retry included). Analyses
run in several processes, so the count lives in SQLite (``<cache_dir>/fmp_budget.sqlite3``): each grant is one
``BEGIN IMMEDIATE`` transaction, which serialises the processes, and it survives restarts.
"""
import os
import sqlite3
import time

LIMIT = 225          # requests per rolling 24 h (FMP free plan: 250 a day, the daily scans use up to 25)
WINDOW = 86400.0


class FmpBudget:
    def __init__(self, path, limit=LIMIT, window=WINDOW, clock=time.time):
        self.path, self.limit, self.window, self._clock = path, max(0, int(limit)), window, clock
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, symbol TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
        self._warned = False

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)  # transactions by hand

    def acquire(self, symbol):
        """One FMP request for ``symbol``: True (and counted) while the rolling window has room, else False."""
        now = self._clock()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")  # one writer at a time across the worker processes
            db.execute("DELETE FROM calls WHERE ts <= ?", (now - self.window,))
            (used,) = db.execute("SELECT COUNT(*) FROM calls").fetchone()
            if used >= self.limit:
                db.execute("COMMIT")
                if not self._warned:
                    self._warned = True
                    print(f"⚠️  FMP enrichment paused: {used}/{self.limit} requests in the last 24 h "
                          "(the rest of the plan is kept for the daily scans)", flush=True)
                return False
            db.execute("INSERT INTO calls VALUES (?, ?)", (now, str(symbol)))
            db.execute("COMMIT")
            self._warned = False
            return True
        except sqlite3.Error:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False  # can't count it: don't spend it
        finally:
            db.close()

    def status(self):
        """{"used", "limit", "remaining", "next_free_s"}: requests in the last 24 h and when the oldest expires."""
        now = self._clock()
        db = self._connect()
        try:
            used, oldest = db.execute("SELECT COUNT(*), MIN(ts) FROM calls WHERE ts > ?",
                                      (now - self.window,)).fetchone()
        finally:
            db.close()
        return {"used": used, "limit": self.limit, "remaining": max(0, self.limit - used),
                "next_free_s": round(oldest + self.window - now) if used >= self.limit and oldest else 0}


def install(budget):
    """Gate this process's FMP requests through ``budget`` (the engine is imported here, not at module load)."""
    from engine import growth_estimator
    growth_estimator.FMP_GATE = budget.acquire
