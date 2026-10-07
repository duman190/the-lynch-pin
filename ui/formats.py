"""Engine results → the plain numbers and labels the portal shows and its AI data block reads.

Pure functions, no I/O: shared by ui/analysis.py (the portal's pipeline) and main.py (the daily scan's
AI prompt, via ui.llm.scan_brief), so both describe a ticker with the same numbers.
"""
import re

_SIGNAL = {"🟢": "good", "🔵": "neutral", "🔴": "bad", "⚪": "na"}
_PCT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*%\s*$")


def _extract_price(info):
    """Current share price from a yfinance info dict (same order as main.py)."""
    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        v = info.get(key) if info else None
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _pct(value):
    """'12.3%' → 12.3 (None when unparsable)."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _PCT_RE.match(str(value or ""))
    return float(m.group(1)) if m else None


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def format_stats(row):
    """Engine row → numbers (for bars/badges) plus the display strings the charts use."""
    peg, mean, dev = _num(row.get("PEG")), _num(row.get("Mean")), _num(row.get("Dev_SD"))
    sd = abs((peg - mean) / dev) if (peg is not None and mean is not None and dev) else None
    return {
        "PE": _num(row.get("PE")), "FwdPE": _num(row.get("FwdPE")), "2YFwd": _num(row.get("2YFwd")),
        "growth_pct": _pct(row.get("5YGrowth")), "PEG": peg, "Mean": mean, "SD": sd, "Dev_SD": dev,
        "Bull": _pct(row.get("Bull")), "Base": _pct(row.get("Base")), "Bear": _pct(row.get("Bear")),
        "div_yield": _pct(row.get("DivYield")),  # cash dividend yield, already included in Bull / Base / Bear
        "display": {k: str(row.get(k)) for k in ("5YGrowth", "Bull", "Base", "Bear")},
        # The engine falls back to (PEG, 0.2·PEG, Dev 0.0) when the 5Y price/EPS history could not be
        # fetched — that is a data outage, not "exactly at the mean".
        "history": "unavailable" if (dev == 0 and peg is not None and mean is not None and abs(peg - mean) < 1e-9)
        else "ok",
    }


def format_income(g):
    if not g:
        return None
    return {"grade": g.get("grade"),
            "items": [{"label": label, "growth": _num(growth), "signal": _SIGNAL.get(sig, "na")}
                      for label, growth, sig in g.get("items", [])]}


def format_credit(b):
    if not b:
        return None
    return {"rating": b.get("rating"),
            "metrics": [{"label": label, "value": _num(val)} for label, val in b.get("metrics", [])]}


def format_technicals(t):
    if not t:
        return None
    out = dict(t)
    zone = t.get("accumulation_zone")
    out["accumulation_zone"] = [_num(zone[0]), _num(zone[1])] if zone and len(zone) >= 2 else None
    return out
