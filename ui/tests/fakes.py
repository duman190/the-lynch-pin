"""Offline stand-ins for the Lynch Pin engine (no network, no yfinance)."""
import os
import threading
import time

MSFT_INFO = {"currentPrice": 430.0, "forwardPE": 21.8, "longName": "Microsoft Corporation", "currency": "USD",
             "sector": "Technology", "industry": "Software—Infrastructure", "quoteType": "EQUITY",
             "marketCap": 3.2e12}
MSFT_ROW = {"Ticker": "MSFT", "PE": 28.7, "FwdPE": 21.8, "2YFwd": 19.3, "5YGrowth": "13.0%", "PEG": 1.68,
            "Mean": 1.72, "Dev_SD": -0.19, "Bull": "15.0%", "Base": "13.5%", "Bear": "12.1%"}
INCOME = {"grade": "A+", "items": [("Revenue", 0.18, "🟢"), ("COGS", 0.23, "🔵"), ("OpIncome", 0.18, "🟢"),
                                   ("EPS", None, "⚪"), ("G&A", 0.40, "🔴")]}
CREDIT = {"rating": "AAA", "metrics": [("IntCov", 50.9), ("ND/EBITDA", 0.2), ("Cash/Debt", 0.4), ("Svc/FCF%", None)]}
TECH = {"trend": "UPTREND", "price_vs_sma200": 18.0, "ema50_vs_sma200": 5.0, "rsi": 62.0, "atr_compression": 0.96,
        "signal": "BULLISH", "accumulation_zone": (419.0, 440.0)}
EDGE = {"bull_acc": 60.0, "bull_pnl": 2.1, "bull_n": 22, "bear_acc": 50.0, "bear_pnl": 0.4, "bear_n": 30,
        "best_edge": "BULL"}


class FakeEngine:
    infos = {"MSFT": MSFT_INFO}
    rows = {"MSFT": MSFT_ROW}
    calls = []

    def __init__(self, sym):
        FakeEngine.calls.append(sym)
        self.symbol = sym
        self.ticker = f"<ticker {sym}>"
        self.info = dict(self.infos.get(sym, {}))

    enrich_calls = []

    def get_ticker_stats(self, enrich=False):
        FakeEngine.enrich_calls.append((self.symbol, enrich))
        self._growth_sources = ["yahoo_peg", "fmp"] if enrich else ["yahoo_peg"]
        return dict(self.rows[self.symbol]) if self.symbol in self.rows else None


class FakeVisualizer:
    def __init__(self, output_dir="tmp"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def plot_ticker_distribution(self, row, g=None, b=None, t=None, e=None):
        from PIL import Image
        sym = row["Ticker"].replace("*", "")
        path = os.path.join(self.output_dir, f"{sym}_valuation.png")
        Image.new("RGB", (1568, 915), "#121212").save(path)
        return path


LEVELS = {"price": 430.0, "support": [{"price": 419.5, "p_touch_1m": 62.0}, {"price": 401.2, "p_touch_1m": 31.0}],
          "resistance": [{"price": 445.8, "p_touch_1m": 48.0}], "poc": 425.1, "hvn": [410.0, 425.1, 438.2],
          "ranges": {"1w": {"1sigma_lower": 415.0, "1sigma_upper": 445.0, "2sigma_lower": 400.0, "2sigma_upper": 460.0,
                            "move_pct": 3.5},
                     "1m": {"1sigma_lower": 399.0, "1sigma_upper": 461.0, "2sigma_lower": 368.0, "2sigma_upper": 492.0,
                            "move_pct": 7.2}},
          "realized_vol_pct": 24.9, "sma50": 420.0, "sma200": 380.0, "high_52w": 468.0, "low_52w": 344.0}


def backends(**overrides):
    b = {"engine": FakeEngine, "grade_income": lambda t: INCOME, "grade_bs": lambda t: CREDIT,
         "technicals": lambda t: TECH, "levels": lambda t, price=None: LEVELS,
         "edge": lambda s, idx, days: EDGE, "visualizer": FakeVisualizer}
    b.update(overrides)
    return b


class AnyEngine(FakeEngine):
    """Every symbol is a Microsoft look-alike: worker-process tests cannot patch class data from the parent."""

    def __init__(self, sym):
        super().__init__(sym)
        self.info = dict(MSFT_INFO, longName=f"{sym} Inc.")

    def get_ticker_stats(self, enrich=False):
        self._growth_sources = ["yahoo_peg"]
        return dict(MSFT_ROW, Ticker=self.symbol)


class ThrottledEngine(FakeEngine):
    """Yahoo answers 429 to the next ``budget`` quote requests (an empty quote, like the real engine)."""
    budget = 0

    def __init__(self, sym):
        super().__init__(sym)
        self.info = dict(MSFT_INFO, longName=f"{sym} Inc.")
        if ThrottledEngine.budget > 0:
            ThrottledEngine.budget -= 1
            from ui import yahoo
            yahoo.note_rate_limit()
            self.info = {}

    def get_ticker_stats(self, enrich=False):
        self._growth_sources = ["yahoo_peg"]
        return dict(MSFT_ROW, Ticker=self.symbol)


def _edge_by_symbol(sym, idx, days):
    if sym.startswith("SLOW"):
        time.sleep(1.0)
    elif sym.startswith("HANG"):
        time.sleep(120)
    elif sym.startswith("CRASH"):
        os._exit(3)
    elif sym.startswith("ENV"):  # what the worker process's math libraries see
        return dict(EDGE, env={k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                                                              "VECLIB_MAXIMUM_THREADS")})
    return EDGE


def process_backends():
    """Backends for a worker process (``JobManager(backends_spec="ui.tests.fakes:process_backends")``):
    the symbol picks the behaviour of the edge stage (SLOW… 1 s, HANG… hangs, CRASH… kills the process,
    ENV… reports the process's thread-count variables)."""
    return backends(engine=AnyEngine, edge=_edge_by_symbol)


class Gate:
    """Blocks a fake backend until released — lets tests observe queued/running states."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wrap(self, value):
        def fn(*a, **k):
            self.entered.set()
            self.release.wait(10)
            return value
        return fn
