"""Price levels to watch, from the experimental trade assistant's quant toolkit.

Uses the same public functions ``experimental.trade_assistant.scan`` builds on:

* ``find_levels``      — support / resistance from KDE-clustered pivot highs & lows (last 6 months)
* ``volume_profile``   — Point of Control and high-volume nodes (last 3 months)
* ``expected_move``    — ±1σ / ±2σ ranges, here over 1 week and 1 month
* ``move_probability`` — chance of touching each level within a month

Volatility is the 20-day realised volatility (the trade assistant's fallback when no options IV is
available); the options chain is skipped on purpose — it is slow and flaky for a web lookup.
Plus the plain anchors every investor watches: SMA50, SMA200 and the 52-week high / low.
"""
import math

import numpy as np


def _r(x, nd=2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def realized_vol(close, window=20):
    returns = np.log(close / close.shift(1)).dropna()
    if len(returns) < 2:
        return None
    v = float(returns.tail(window).std() * np.sqrt(252))
    return v if math.isfinite(v) and v > 0 else None


def compute_levels(hist, price=None):
    """Levels from a daily OHLCV DataFrame (≥ 50 rows). Pure — no network."""
    from experimental.quant_engine import expected_move, find_levels, move_probability, volume_profile
    hist = hist.dropna(subset=["Close"])
    if hist.empty or len(hist) < 50:
        return None
    close = hist["Close"]
    price = float(price) if price else float(close.iloc[-1])
    vol = realized_vol(close)

    lv = find_levels(hist.tail(120)) or {}
    support = [s for s in (lv.get("support") or []) if s < price]
    resistance = [r for r in (lv.get("resistance") or []) if r > price]

    vp = volume_profile(hist.tail(60))
    poc = _r(vp["poc"]) if vp else None
    hvn = sorted(_r(x) for x in vp["hvn"].index.tolist()) if vp else []

    ranges = {}
    if vol:
        for label, days in (("1w", 5), ("1m", 21)):
            em = expected_move(price, vol, days=days)
            ranges[label] = {k: _r(em[k]) for k in ("1sigma_lower", "1sigma_upper", "2sigma_lower", "2sigma_upper")}
            ranges[label]["move_pct"] = _r(em["move_pct"], 1)

    def touch(level):
        if not vol or not level:
            return None
        p = move_probability(price, level, vol, days=21).get("probability")
        return _r(p, 0)

    sma50 = _r(close.tail(50).mean()) if len(close) >= 50 else None
    sma200 = _r(close.tail(200).mean()) if len(close) >= 200 else None
    year = hist.tail(252)
    return {
        "price": _r(price),
        "support": [{"price": _r(s), "p_touch_1m": touch(s)} for s in sorted(support, reverse=True)],   # nearest first
        "resistance": [{"price": _r(r), "p_touch_1m": touch(r)} for r in sorted(resistance)],          # nearest first
        "poc": poc, "hvn": hvn,
        "ranges": ranges,
        "realized_vol_pct": _r(vol * 100, 1) if vol else None,
        "sma50": sma50, "sma200": sma200,
        "high_52w": _r(year["High"].max()), "low_52w": _r(year["Low"].min()),
    }


def price_levels(ticker_obj, price=None):
    """Fetches one year of daily bars and computes the levels (None if history is too short)."""
    try:
        hist = ticker_obj.history(period="1y", interval="1d")
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    return compute_levels(hist, price)
