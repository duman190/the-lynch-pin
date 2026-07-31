"""Technical timing analysis for GARP top picks.

Computes trend, momentum, and accumulation signals from daily price history.
No external dependencies beyond numpy/pandas (uses yfinance history already fetched).
"""

import numpy as np
import pandas as pd
from experimental.back_test import backtest


def _ema(series, span):
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def _sma(series, window):
    """Simple moving average."""
    return series.rolling(window=window).mean()


def _rsi(series, period=14):
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(high, low, close, period=14):
    """Average True Range."""
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def analyze(ticker_obj):
    """Compute technical timing signals for a ticker.

    Args:
        ticker_obj: yfinance Ticker object (already initialized).

    Returns:
        dict with keys: trend, rsi, atr_compression, accumulation, signal
        or None if insufficient data.
    """
    try:
        hist = ticker_obj.history(period="1y", interval="1d")
        hist = hist.dropna(subset=['Close'])
        if hist.empty or len(hist) < 200:
            return None

        close = hist['Close']
        high = hist['High']
        low = hist['Low']

        # Indicators
        sma200 = _sma(close, 200).iloc[-1]
        ema50 = _ema(close, 50).iloc[-1]
        price = close.iloc[-1]
        rsi_val = _rsi(close).iloc[-1]

        # ATR compression: current ATR vs 3-month average ATR
        atr_series = _atr(high, low, close)
        atr_now = atr_series.iloc[-1]
        atr_3m_avg = atr_series.iloc[-63:].mean()  # ~3 months
        atr_compression = (atr_now / atr_3m_avg) if atr_3m_avg > 0 else 1.0

        # Trend determination
        if price > ema50 > sma200:
            trend = "BULLISH"
        elif price > sma200:
            trend = "NEUTRAL"
        else:
            trend = "BEARISH"

        # Proximity to SMA200
        pct_from_sma200 = ((price - sma200) / sma200) * 100

        # Accumulation detection
        accumulation = (
            trend != "BEARISH"
            and (rsi_val < 45 or pct_from_sma200 < 5.0)
            and atr_compression < 0.9
        )

        # Signal label
        if accumulation:
            signal = "ACCUMUL"
        elif trend == "BULLISH":
            signal = "BULLISH"
        elif trend == "NEUTRAL":
            signal = "NEUTRAL"
        else:
            signal = "BEARISH"

        # Accumulation zone: SMA200-based support band (SMA200 ± 1 ATR)
        if np.isnan(sma200) or np.isnan(atr_now):
            return None
        zone_low = round(float(sma200 - atr_now), 0)
        zone_high = round(float(sma200 + atr_now), 0)

        return {
            'trend': trend,
            'price_vs_sma200': round(pct_from_sma200, 1),
            'ema50_vs_sma200': round(((ema50 - sma200) / sma200) * 100, 1),
            'rsi': round(rsi_val, 0),
            'atr_compression': round(atr_compression, 2),
            'signal': signal,
            'accumulation_zone': (zone_low, zone_high),
        }
    except Exception:
        return None


def backtest_edge(ticker_symbol, index_symbol="QQQ", days=180):
    """Run 6-month directional backtest and return bull/bear accuracy.

    Uses the experimental trade assistant's scoring engine to determine
    which direction has a statistical edge for this ticker.

    Args:
        ticker_symbol: Stock symbol.
        index_symbol: Reference index for relative strength.
        days: Backtest window in trading days (default 180 ≈ 6 months).

    Returns:
        dict with bull_acc, bull_pnl, bear_acc, bear_pnl, best_edge
        or None if backtest fails.
    """
    try:
        bt = backtest(ticker_symbol, index_symbol, days=days)
        if "error" in bt:
            return None
        bd = bt["breakdown"]
        bull = bd.get("BULLISH", {})
        bear = bd.get("BEARISH", {})
        bull_acc = bull.get("accuracy", 0)
        bull_pnl = bull.get("avg_dir_pnl", 0)
        bear_acc = bear.get("accuracy", 0)
        bear_pnl = bear.get("avg_dir_pnl", 0)
        bull_n = bull.get("count", 0)
        bear_n = bear.get("count", 0)

        if bull_acc > bear_acc:
            best = "BULL"
        elif bear_acc > bull_acc:
            best = "BEAR"
        else:
            best = "—"

        return {
            "bull_acc": bull_acc,
            "bull_pnl": bull_pnl,
            "bull_n": bull_n,
            "bear_acc": bear_acc,
            "bear_pnl": bear_pnl,
            "bear_n": bear_n,
            "best_edge": best,
        }
    except Exception:
        return None
