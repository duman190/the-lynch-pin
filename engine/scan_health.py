"""Scan health for main.py: tells Yahoo throttling apart from tickers that simply have no GARP data.

The engine swallows every Yahoo error (``get_ticker_stats`` returns None, a grader returns None), so a run
that Yahoo throttled looked like a run where most tickers had no forward earnings: it printed a shorter
table, or "No data processed", exited 0, and could post a "top 8" picked from whatever survived. Here:

* every ``YFRateLimitError`` yfinance raises is counted (``install``), so a ticker whose analysis saw a 429
  is known to be throttled, not empty, and is retried after a back-off (``BACKOFFS``);
* a ticker that is still throttled after the retries is left out of the ranking (its numbers may come from a
  fallback, e.g. the PE-volatility PEG history), and after ``MAX_THROTTLED_IN_A_ROW`` such tickers the scan
  stops asking Yahoo;
* a 429 during a top pick's later stages (grades, technicals, FMP enrichment, the edge backtest) marks the
  pick degraded: its grades may be missing, so ``--excl-bad`` could have let it through;
* ``problems()`` says why the run should not be posted: more than ``MAX_THROTTLED_SHARE`` of the tickers lost
  to throttling, more than ``MAX_NO_QUOTE_SHARE`` without a quote (a network outage, not a few delisted
  symbols), or any degraded top pick.
"""
import contextlib
import time

BACKOFFS = (60, 180)            # seconds to wait before each retry of a throttled ticker
MAX_THROTTLED_IN_A_ROW = 3      # then stop asking Yahoo: it is throttling the machine, not one ticker
MAX_THROTTLED_SHARE = 0.10      # of the tickers: above it the ranking is missing too much to post
MAX_NO_QUOTE_SHARE = 0.50       # a few unknown or delisted symbols are normal; half the list is an outage

_events = 0
_installed = False


def note_rate_limit():
    """Records one Yahoo 429 (called by the yfinance hook; tests call it directly)."""
    global _events
    _events += 1


def rate_limit_events():
    """Yahoo 429s seen by this process so far (compare before / after a step)."""
    return _events


def install():
    """Counts every yfinance rate-limit error in this process (idempotent)."""
    global _installed
    if _installed:
        return
    try:
        from yfinance.exceptions import YFRateLimitError
    except ImportError:  # an old yfinance without it: nothing to count
        _installed = True
        return
    original = YFRateLimitError.__init__

    def counting_init(self, *args, **kwargs):
        note_rate_limit()
        original(self, *args, **kwargs)

    YFRateLimitError.__init__ = counting_init
    _installed = True


class ScanHealth:
    """Outcome of every ticker in one run, and whether the run is sound enough to post."""

    def __init__(self, total, sleep=None, backoffs=None):
        self.total = total
        self.outcomes = {}       # ticker -> ok | nodata | no_quote | throttled
        self.degraded = []       # "TICKER stage" for top picks whose later stages saw a 429
        self._sleep = sleep or time.sleep
        self._backoffs = tuple(BACKOFFS if backoffs is None else backoffs)
        self._streak = 0
        self._start_events = rate_limit_events()

    @property
    def aborted(self):
        """Too many throttled tickers in a row: stop asking Yahoo for the rest of the list."""
        return self._streak >= MAX_THROTTLED_IN_A_ROW

    def analyse(self, sym, run):
        """``run(sym) -> (engine, stats or None)``, retried while Yahoo throttles it.

        Returns ``(engine, stats, outcome)``; ``stats`` is None unless the outcome is ``ok``."""
        engine = res = None
        throttled = False
        for attempt in range(len(self._backoffs) + 1):
            before = rate_limit_events()
            engine, res = run(sym)
            throttled = rate_limit_events() > before
            if not throttled:
                break
            if attempt < len(self._backoffs):
                wait = self._backoffs[attempt]
                print(f"  ⏳ {sym}: Yahoo is throttling (429), retrying in {wait}s "
                      f"({attempt + 1}/{len(self._backoffs)})...", flush=True)
                self._sleep(wait)
        if throttled:
            outcome, res = "throttled", None  # a throttled analysis may rest on fallbacks: never rank it
        elif res:
            outcome = "ok"
        elif not getattr(engine, "info", None):
            outcome = "no_quote"
        else:
            outcome = "nodata"
        self.outcomes[sym] = outcome
        self._streak = self._streak + 1 if outcome == "throttled" else 0
        if outcome == "throttled":
            print(f"  ⚠️ {sym}: still throttled after {len(self._backoffs)} retries, left out of the ranking")
        return engine, res, outcome

    def skip_rest(self, syms):
        """Marks tickers never asked for (the scan stopped after repeated throttling) as throttled."""
        for s in syms:
            self.outcomes.setdefault(s, "throttled")

    @contextlib.contextmanager
    def watch(self, sym, stage):
        """Marks top pick ``sym`` degraded if Yahoo answered 429 during ``stage``."""
        before = rate_limit_events()
        try:
            yield
        finally:
            if rate_limit_events() > before:
                self.degraded.append(f"{sym} {stage}")

    def count(self, outcome):
        return sum(1 for o in self.outcomes.values() if o == outcome)

    @property
    def rate_limits(self):
        """Yahoo 429s during this run."""
        return rate_limit_events() - self._start_events

    def summary(self):
        ok, nodata = self.count("ok"), self.count("nodata")
        throttled, no_quote = self.count("throttled"), self.count("no_quote")
        line = (f"📋 Coverage: {ok + nodata}/{self.total} tickers analysed "
                f"({ok} with GARP data, {nodata} without)")
        if throttled:
            line += f" · {throttled} lost to Yahoo throttling"
        if no_quote:
            no_quote_syms = sorted(s for s, o in self.outcomes.items() if o == "no_quote")
            line += f" · {no_quote} without a quote ({', '.join(no_quote_syms[:8])}{'…' if no_quote > 8 else ''})"
        if self.rate_limits:
            line += f" · {self.rate_limits} Yahoo 429s"
        if self.degraded:
            line += f"\n   ⚠️ Degraded top picks (429 during): {', '.join(self.degraded)}"
        return line

    def problems(self):
        """Why this run should not be posted ([] = sound)."""
        out = []
        if self.total:
            throttled, no_quote = self.count("throttled"), self.count("no_quote")
            if throttled / self.total > MAX_THROTTLED_SHARE:
                out.append(f"{throttled}/{self.total} tickers lost to Yahoo throttling "
                           f"(over {MAX_THROTTLED_SHARE:.0%}): the ranking is missing too much")
            if no_quote / self.total > MAX_NO_QUOTE_SHARE:
                out.append(f"{no_quote}/{self.total} tickers without a quote "
                           f"(over {MAX_NO_QUOTE_SHARE:.0%}): Yahoo or the network is down")
        if self.degraded:
            out.append(f"top picks degraded by Yahoo throttling: {', '.join(self.degraded)}")
        return out
