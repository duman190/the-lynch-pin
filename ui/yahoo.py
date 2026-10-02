"""Yahoo Finance call hygiene for the portal's analysis processes (main.py never imports this).

A cold lookup makes ~14 Yahoo calls, and Yahoo answers 429 after a few hundred lookups in a burst, so:

* ``Ticker.history`` results are kept for a short while. The technicals and price-levels stages ask for
  the same year of daily bars (one download instead of two), and the edge backtest's benchmark index
  (SPY) is the same for every ticker (one download per worker every 10 minutes instead of one per
  lookup). Callers get a copy, so a caller that edits its frame in place cannot corrupt the cache.
* Every yfinance rate-limit path raises ``YFRateLimitError``; the engine swallows it (an empty quote,
  a skipped stage). Counting its construction tells the analyzer that a lookup was throttled, so the
  job manager can back off instead of caching a degraded result (ui/jobs.py).
"""
import collections
import datetime as _dt
import threading
import time

SAME_TICKER_TTL = 60.0   # covers one analysis (two stages asking for the same bars), not a ↻ Refresh
SHARED_TTL = 600.0       # the benchmark index: shared by every lookup in the process
MAX_ENTRIES = 64

_lock = threading.Lock()
_cache = collections.OrderedDict()  # key → (stored_at, DataFrame)
_shared = set()
_installed = False
_rate_limits = 0
stats = {"hits": 0, "misses": 0}


def note_rate_limit():
    """Records one Yahoo 429 (called by the yfinance hook; tests call it directly)."""
    global _rate_limits
    with _lock:
        _rate_limits += 1


def rate_limit_events():
    """Number of Yahoo 429s seen by this process so far (compare before / after an analysis)."""
    return _rate_limits


def _norm(v):
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()  # start/end derived from "now": same day = same bars within the TTL
    if isinstance(v, _dt.date):
        return v.isoformat()
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)


def _key(sym, args, kwargs):
    return sym, tuple(_norm(a) for a in args), tuple(sorted((k, _norm(v)) for k, v in kwargs.items()))


def cached_history(original, ticker, *args, **kwargs):
    import pandas as pd
    sym = str(getattr(ticker, "ticker", "")).upper()
    key = _key(sym, args, kwargs)
    ttl = SHARED_TTL if sym in _shared else SAME_TICKER_TTL
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            _cache.move_to_end(key)
            stats["hits"] += 1
            return hit[1].copy()
        stats["misses"] += 1
    df = original(ticker, *args, **kwargs)
    if isinstance(df, pd.DataFrame) and not df.empty:  # never cache a failure (empty frame)
        with _lock:
            _cache[key] = (now, df.copy())
            _cache.move_to_end(key)
            while len(_cache) > MAX_ENTRIES:
                _cache.popitem(last=False)
    return df


def clear():
    with _lock:
        _cache.clear()
        stats.update(hits=0, misses=0)


def install(shared_symbols=("SPY",)):
    """Patches yfinance in this process (idempotent). ``shared_symbols`` get the long TTL."""
    global _installed
    _shared.update(s.upper() for s in shared_symbols if s)
    if _installed:
        return
    from yfinance.base import TickerBase
    from yfinance.exceptions import YFRateLimitError

    history = TickerBase.history

    def patched_history(self, *args, **kwargs):
        return cached_history(history, self, *args, **kwargs)

    patched_history.__doc__ = history.__doc__
    TickerBase.history = patched_history

    rl_init = YFRateLimitError.__init__

    def counting_init(self, *args, **kwargs):
        note_rate_limit()
        rl_init(self, *args, **kwargs)

    YFRateLimitError.__init__ = counting_init
    _installed = True
