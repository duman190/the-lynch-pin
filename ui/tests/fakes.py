"""Offline stand-ins for the Lynch Pin engine (no network, no yfinance)."""
import os
import threading

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


def backends(**overrides):
    b = {"engine": FakeEngine, "grade_income": lambda t: INCOME, "grade_bs": lambda t: CREDIT,
         "technicals": lambda t: TECH, "edge": lambda s, idx, days: EDGE, "visualizer": FakeVisualizer}
    b.update(overrides)
    return b


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
