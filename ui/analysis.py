"""Single-ticker Lynch Pin analysis for the portal — the same pipeline main.py runs for a
``--top`` pick (stats → income/credit grades → technicals → 6M edge → chart), staged so the
UI can render each block as soon as it lands.

Only engine/ and graphics/ modules are imported (never main.py, which pulls in the social
publishers). matplotlib is forced onto the Agg backend before graphics.visualizer imports
pyplot. All of this is meant to run on the JobManager's single worker thread.
"""
import datetime as _dt
import os
import re
import time
import traceback

import matplotlib

matplotlib.use("Agg")

STAGES = ("stats", "grades", "technicals", "edge", "plot")

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


def _default_backends():
    """Real engine functions, imported lazily (they pull yfinance / scipy / pyplot)."""
    from engine.balance_sheet_grader import grade_ticker as grade_bs
    from engine.income_statement_grader import grade_ticker as grade_income
    from engine.lynch_pin_core import LynchPinEngine
    from engine.technical_timing import analyze, backtest_edge
    from graphics.visualizer import LynchPinVisualizer
    from ui.levels import price_levels
    return {"engine": LynchPinEngine, "grade_income": grade_income, "grade_bs": grade_bs,
            "technicals": analyze, "levels": price_levels, "edge": backtest_edge, "visualizer": LynchPinVisualizer}


class StageError(Exception):
    pass


class TickerAnalyzer:
    """Runs the staged pipeline for one symbol. Backends are injectable for offline tests."""

    def __init__(self, settings, backends=None, today=None):
        self.settings = settings
        self._backends = backends
        self._today = today or _dt.date.today
        self._viz = {}  # day → LynchPinVisualizer (its __init__ rewrites global rcParams; do it once)

    @property
    def backends(self):
        if self._backends is None:
            self._backends = _default_backends()
        return self._backends

    def plot_dir(self, day=None):
        return os.path.join(self.settings.cache_dir, "plots", (day or self._today()).isoformat())

    def _visualizer(self):
        day = self._today()
        if day not in self._viz:
            self._viz = {day: self.backends["visualizer"](output_dir=self.plot_dir(day))}
        return self._viz[day]

    def run(self, sym, on_stage=None, cancelled=None):
        """Analyse ``sym``. ``on_stage(name, state, data)`` is called as each stage starts/ends
        (state: running | done | error | skipped) with the partial result. ``cancelled()``
        returning True stops between stages. Returns the final result dict whose ``status`` is
        done | nodata | error (error = transient, e.g. Yahoo unreachable; must not be cached)."""
        b = self.backends
        emit = on_stage or (lambda *a: None)
        stop = cancelled or (lambda: False)
        data = {"ticker": sym, "status": "running", "reason": None, "stages": {s: "pending" for s in STAGES},
                "stage_ms": {}, "benchmark": self.settings.benchmark}
        ctx = {}

        def stage(name, fn):
            if stop():
                raise StageError("cancelled")
            data["stages"][name] = "running"
            emit(name, "running", data)
            t0 = time.monotonic()
            try:
                state = fn() or "done"
            except StageError:
                raise
            except Exception as e:  # isolate: one failing block never kills the rest
                print(f"⚠️  {sym} {name}: {type(e).__name__}: {e}")
                traceback.print_exc(limit=2)
                state = "error"
            data["stages"][name] = state
            data["stage_ms"][name] = int((time.monotonic() - t0) * 1000)
            emit(name, state, data)
            return state

        # 1. quote + GARP stats ────────────────────────────────────────────────
        def do_stats():
            engine = b["engine"](sym)
            ctx["engine"] = engine
            info = engine.info or {}
            if not info:
                data["status"], data["reason"] = "error", "quote unavailable (Yahoo Finance unreachable or rate-limited)"
                return "error"
            price = _extract_price(info)
            data.update({
                "name": info.get("longName") or info.get("shortName") or sym,
                "price": price, "currency": info.get("currency") or "USD",
                "sector": info.get("sector"), "industry": info.get("industry"),
                "quote_type": info.get("quoteType"), "exchange": info.get("exchange"),
                "market_cap": _num(info.get("marketCap")),
            })
            if price is None and not info.get("quoteType"):
                data["status"], data["reason"] = "nodata", "unknown symbol"
                return "skipped"
            fwd_pe = info.get("forwardPE")
            if not info.get("currentPrice"):
                data["status"], data["reason"] = "nodata", "no GARP data (not an operating company, e.g. ETF/index/fund)"
                return "skipped"
            if not isinstance(fwd_pe, (int, float)) or fwd_pe <= 0:
                data["status"], data["reason"] = "nodata", "no GARP data (negative or absent forward earnings)"
                return "skipped"
            row = engine.get_ticker_stats(enrich=self.settings.enrich_enabled)
            # Enriched = the FMP analyst estimate actually made it into the 5Y growth blend
            data["growth_enriched"] = "fmp" in (getattr(engine, "_growth_sources", None) or [])
            if not row:
                data["status"], data["reason"] = "nodata", "no GARP data (no usable growth estimate or EPS base)"
                return "skipped"
            ctx["row"] = row
            data["flagged"] = str(row.get("Ticker", "")).endswith("*")
            data["stats"] = format_stats(row)
            if data["stats"]["history"] != "ok":
                data["cacheable"] = False  # likely a transient Yahoo/EDGAR outage — let a retry recompute
            return "done"

        if stage("stats", do_stats) == "error" and "engine" not in ctx:
            data["status"] = "error"
            data["reason"] = data.get("reason") or "analysis engine failed"
        if data["status"] == "error" or data.get("reason") == "unknown symbol":
            for s in STAGES[1:]:
                data["stages"][s] = "skipped"
            return data

        ticker_obj = ctx["engine"].ticker

        # 2-4. grades / technicals / edge — useful even without GARP stats ─────
        def do_grades():
            ctx["g"] = b["grade_income"](ticker_obj)
            ctx["b"] = b["grade_bs"](ticker_obj)
            data["income"], data["credit"] = format_income(ctx["g"]), format_credit(ctx["b"])
            return "done" if (ctx["g"] or ctx["b"]) else "skipped"

        def do_tech():
            ctx["t"] = b["technicals"](ticker_obj)
            data["technicals"] = format_technicals(ctx["t"])
            levels_fn = b.get("levels")
            if levels_fn is not None:
                try:  # price levels are a bonus: never fail the technicals stage over them
                    data["levels"] = levels_fn(ticker_obj, data.get("price"))
                except Exception as e:
                    print(f"⚠️  {sym} levels: {type(e).__name__}: {e}")
                    data["levels"] = None
            return "done" if (ctx["t"] or data.get("levels")) else "skipped"

        def do_edge():
            ctx["e"] = b["edge"](sym, self.settings.benchmark, 180)
            data["edge"] = dict(ctx["e"]) if ctx["e"] else None
            return "done" if ctx["e"] else "skipped"

        def do_plot():
            if "row" not in ctx:
                return "skipped"
            path = self._visualizer().plot_ticker_distribution(ctx["row"], ctx.get("g"), ctx.get("b"),
                                                                ctx.get("t"), ctx.get("e"))
            if not path or not os.path.exists(path):
                raise RuntimeError("chart was not written")
            data["plot_file"] = path
            v = int(os.path.getmtime(path))
            data["plot_url"] = f"/plots/{sym}.png?v={v}"
            data["plot_preview_url"] = f"/plots/{sym}.jpg?v={v}"
            return "done"

        stage("grades", do_grades)
        stage("technicals", do_tech)
        stage("edge", do_edge)
        stage("plot", do_plot)

        if data["status"] == "running":
            data["status"] = "done"
        data["generated_at"] = time.time()
        # Kept for the AI overview (step 4): the same inputs main.py hands the researcher
        data["_ai_inputs"] = {"row": ctx.get("row"), "g": ctx.get("g"), "b": ctx.get("b"),
                              "t": ctx.get("t"), "e": ctx.get("e")}
        return data
