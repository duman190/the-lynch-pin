"""Single-ticker Lynch Pin analysis for the portal — the same pipeline main.py runs for a
``--top`` pick (stats → income/credit grades → technicals → 6M edge → chart), staged so the
UI can render each block as soon as it lands.

Only engine/ and graphics/ modules are imported (never main.py, which pulls in the social
publishers). matplotlib is forced onto the Agg backend before graphics.visualizer imports
pyplot. Analyses run on a JobManager worker: a thread, or a child process per worker
(ui/workers.py). pyplot state is process-global, so threads draw charts one at a time.
"""
import datetime as _dt
import os
import threading
import time
import traceback

import matplotlib

from ui import yahoo
from ui.formats import _extract_price, _num, format_credit, format_income, format_stats, format_technicals
from ui.quick import profile_from_info, quick_overview

matplotlib.use("Agg")

STAGES = ("stats", "grades", "technicals", "edge", "plot")

_PLOT_LOCK = threading.Lock()  # pyplot and the visualizer's rcParams are process-global


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
            yahoo.install((self.settings.benchmark,))  # dedupe history downloads, count Yahoo 429s
            if self.settings.enrich_enabled:  # FMP requests capped per rolling 24 h (the daily scans keep the rest)
                from ui import fmp_budget
                fmp_budget.install(fmp_budget.FmpBudget(self.settings.fmp_budget_path, self.settings.fmp_limit))
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
        rate_limits = yahoo.rate_limit_events()

        def throttled():
            """Yahoo answered 429 during this analysis: its blocks may be missing, so never cache it."""
            if yahoo.rate_limit_events() > rate_limits:
                data["rate_limited"], data["cacheable"] = True, False
            return data.get("rate_limited", False)

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
                data["status"] = "error"
                data["reason"] = ("Yahoo Finance is rate-limiting this server" if throttled()
                                  else "quote unavailable (Yahoo Finance unreachable or rate-limited)")
                return "error"
            price = _extract_price(info)
            data.update({
                "name": info.get("longName") or info.get("shortName") or sym,
                "price": price, "currency": info.get("currency") or "USD",
                "sector": info.get("sector"), "industry": info.get("industry"),
                "quote_type": info.get("quoteType"), "exchange": info.get("exchange"),
                "market_cap": _num(info.get("marketCap")),
                "profile": profile_from_info(info),  # business summary, margins, analysts… (Quick Overview)
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
            throttled()
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
            with _PLOT_LOCK:
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
        throttled()
        try:  # the --no-ai stand-in for the AI overview: rules over the numbers above, no model
            data["quick"] = quick_overview(data)
        except Exception as e:
            print(f"⚠️  {sym} quick overview: {type(e).__name__}: {e}")
        data["generated_at"] = time.time()
        # Kept for the AI overview (step 4): the same inputs main.py hands the researcher
        data["_ai_inputs"] = {"row": ctx.get("row"), "g": ctx.get("g"), "b": ctx.get("b"),
                              "t": ctx.get("t"), "e": ctx.get("e")}
        return data
