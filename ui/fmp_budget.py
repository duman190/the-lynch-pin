"""FMP request budget for the portal's growth enrichment.

FMP's free plan allows 250 requests a day; the portal keeps to ``limit`` (240) in any rolling 24 hours, leaving 10
for the daily scans (main.py). FMP counts every request it answers, errors included: measured on the
dashboard (2026-10-07), one request with data plus six HTTP 402 "Premium Query Parameter" answers (symbols
outside the free plan) raised it by 7. So every answered request counts, 4xx included; one that FMP didn't
answer (a network error, a timeout, a 5xx server failure) is handed back, and lookups that never call FMP (enrichment off, no forward
earnings, the budget spent) cost nothing. A rolling window
rather than a calendar day: FMP's reset time is not documented, and 240 per local day could put up to 480 inside
one of FMP's days.

The engine asks before every request (``engine.growth_estimator.FMP_GATE``, a 429 retry included): the request
reserves a slot first, so parallel lookups can't overshoot the cap, and hands it back if FMP didn't answer. Analyses run
in several processes, so the count lives in SQLite (``<cache_dir>/fmp_budget.sqlite3``): each grant is one
``BEGIN IMMEDIATE`` transaction, which serialises the processes, and it survives restarts.

A ticker FMP has nothing for is not asked again for ``skip_days`` (30): when FMP's answer leaves it "Not enriched"
(a 402 for a symbol outside the free plan, or estimates that give no 5Y growth), the ticker goes on a list in the same
database and the gate refuses its requests until then, so repeat lookups of the same foreign or thinly covered
tickers stop spending the budget. Only those answers count: FMP's own daily cap ("Limit Reach"), a rejected key, a
429, a 5xx, a network error or a request the budget refused say nothing about the ticker. The portal only: main.py
installs no gate and keeps asking.
"""
import functools
import os
import sqlite3
import threading
import time

LIMIT = 240          # requests per rolling 24 h (FMP free plan: 250 a day, 10 left for the daily scans)
WINDOW = 86400.0
SKIP_DAYS = 30       # a ticker FMP had nothing for is not asked again for this long


class FmpBudget:
    def __init__(self, path, limit=LIMIT, window=WINDOW, clock=time.time, skip_days=SKIP_DAYS):
        self.path, self.limit, self.window, self._clock = path, max(0, int(limit)), window, clock
        self.skip_s = max(0.0, float(skip_days)) * 86400
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, symbol TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
            # tickers FMP had nothing for, and when: not asked again for skip_days
            db.execute("CREATE TABLE IF NOT EXISTS unenriched (symbol TEXT PRIMARY KEY, ts REAL NOT NULL)")
        self._warned = False

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)  # transactions by hand

    def acquire(self, symbol):
        """Reserve one FMP request for ``symbol``: False when the rolling window is full or FMP had nothing for
        ``symbol`` in the last ``skip_days``, else ``done(answered)``, which keeps the slot if FMP answered (4xx
        included) and hands it back if it didn't (no response, 5xx)."""
        now = self._clock()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")  # one writer at a time across the worker processes
            db.execute("DELETE FROM calls WHERE ts <= ?", (now - self.window,))
            db.execute("DELETE FROM unenriched WHERE ts <= ?", (now - self.skip_s,))
            if db.execute("SELECT 1 FROM unenriched WHERE symbol = ?", (_key(symbol),)).fetchone():
                db.execute("COMMIT")
                return False  # not enriched lately: nothing to gain from asking again
            (used,) = db.execute("SELECT COUNT(*) FROM calls").fetchone()
            if used >= self.limit:
                db.execute("COMMIT")
                if not self._warned:
                    self._warned = True
                    print(f"⚠️  FMP enrichment paused: {used}/{self.limit} requests in the last 24 h "
                          "(the rest of the plan is kept for the daily scans)", flush=True)
                return False
            slot = db.execute("INSERT INTO calls VALUES (?, ?)", (now, str(symbol))).lastrowid
            db.execute("COMMIT")
            self._warned = False
            return lambda answered: answered or self._release(slot)
        except sqlite3.Error:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False  # can't count it: don't spend it
        finally:
            db.close()

    def _release(self, slot):
        """A request FMP didn't answer (no response, a 5xx): its reserved slot goes back."""
        db = self._connect()
        try:
            db.execute("DELETE FROM calls WHERE rowid = ?", (slot,))
        except sqlite3.Error:
            pass  # stays counted: on the safe side
        finally:
            db.close()

    def mark_unenriched(self, symbol):
        """FMP answered for ``symbol`` with nothing that enriches it: skip it for ``skip_days``."""
        if not self.skip_s:
            return
        db = self._connect()
        try:
            db.execute("INSERT OR REPLACE INTO unenriched VALUES (?, ?)", (_key(symbol), self._clock()))
        except sqlite3.Error:
            pass  # asked again next time: on the safe side
        finally:
            db.close()

    def status(self):
        """{"used", "limit", "remaining", "next_free_s", "unenriched"}: requests in the last 24 h, when the oldest
        expires, and the tickers skipped for having had nothing to enrich them in the last ``skip_days``."""
        now = self._clock()
        db = self._connect()
        try:
            used, oldest = db.execute("SELECT COUNT(*), MIN(ts) FROM calls WHERE ts > ?",
                                      (now - self.window,)).fetchone()
            (skipped,) = db.execute("SELECT COUNT(*) FROM unenriched WHERE ts > ?", (now - self.skip_s,)).fetchone()
        finally:
            db.close()
        return {"used": used, "limit": self.limit, "remaining": max(0, self.limit - used),
                "next_free_s": round(oldest + self.window - now) if used >= self.limit and oldest else 0,
                "unenriched": skipped}


def _key(symbol):
    return str(symbol).strip().upper()


def _nothing_for_it(resp):
    """An FMP answer about the ticker itself: a 402 (outside the free plan; its body is plain text, not JSON) or a
    list of estimates, however thin. Not FMP's daily cap (a 200 with an "Error Message"), a rejected key, a 429 or
    a 5xx."""
    if resp.status_code == 402:
        return True
    if resp.status_code != 200:
        return False
    try:
        return isinstance(resp.json(), list)
    except Exception:  # noqa: BLE001 — not JSON: says nothing about the ticker
        return False


def install(budget):
    """Gate this process's FMP requests through ``budget``, and put a ticker FMP answered for without enriching it
    on the budget's skip list (the engine is imported here, not at module load). The engine's FMP HTTP session and
    growth lookup are wrapped (once, however often this runs) to see FMP's last answer for the ticker: on the
    session, as the engine drops the status of an answer that isn't JSON (FMP's 402)."""
    from engine import growth_estimator as ge
    ge.FMP_GATE = budget.acquire
    get = getattr(ge._SESSION.get, "__wrapped__", ge._SESSION.get)
    growth = getattr(ge._fmp_growth, "__wrapped__", ge._fmp_growth)
    seen = threading.local()  # this lookup's FMP answers (worker threads share the module)

    @functools.wraps(get)
    def fmp_get(url, *args, **kwargs):
        resp = get(url, *args, **kwargs)  # a network error raises: no answer recorded
        if getattr(seen, "answers", None) is not None:
            seen.answers.append(_nothing_for_it(resp))
        return resp

    @functools.wraps(growth)
    def fmp_growth(symbol):
        seen.answers = []
        try:
            out = growth(symbol)
        finally:
            answers, seen.answers = seen.answers, None
        if out[0] is None and answers and answers[-1]:  # "Not enriched" by FMP's own final answer
            budget.mark_unenriched(symbol)
        return out

    ge._SESSION.get, ge._fmp_growth = fmp_get, fmp_growth
