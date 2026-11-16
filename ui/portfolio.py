"""Portfolio Analyzer: main.py's ``--portfolio`` X-ray for a list of positions typed into the portal.

Each position is analysed like a ticker search (ui/jobs.py): today's cached analysis when there is one, else a queued
lookup, so a portfolio of names already looked up today is answered at once. When every position is final the
positions are rolled up exactly as the scan does (engine/portfolio.py's market-value weights and weighted metrics),
the weight donut is drawn (natively, ui/fastchart.py, like the per-ticker chart) and the AI overview is the X-ray's
portfolio verdict (engine/ai_research.py's ``build_portfolio_prompt``, fed the same per-ticker briefs as the scan).

Positions arrive either as weights in % (summing to 100) or as amounts: dollars or a number of shares per position
(shares are valued at the analysis's price). Only weights leave this module — never a dollar amount — and results
are kept for the day under a key derived from the weights alone.
"""
import collections
import datetime as _dt
import hashlib
import json
import math
import os
import re
import threading
import time

from engine.portfolio import format_weighted_summary, weighted_metrics

MAX_POSITIONS = 25
PCT_TOLERANCE = 0.5  # % weights must add up to 100 ± this
KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_AMOUNT_RE = re.compile(r"^(\$)?(\d{1,12}(?:\.\d{1,6})?)(sh)?$")
FINAL = ("done", "nodata", "error")
MAX_RESULTS = 200  # portfolios kept per day (LRU)


class PortfolioError(ValueError):
    """Bad input: the message is shown to the user."""


def parse_positions(spec, mode):
    """``"MSFT:42,AAPL:58"`` (mode ``pct``) or ``"MSFT:$5000,AAPL:12sh"`` (mode ``amt``) →
    ``OrderedDict[ticker -> (kind, value)]`` with kind ``pct`` | ``usd`` | ``sh``. A repeated ticker is summed
    (it must use the same unit). Raises PortfolioError with a message for the page."""
    if mode not in ("pct", "amt"):
        raise PortfolioError("mode must be pct or amt")
    items = [p for p in (spec or "").split(",") if p.strip()]
    if not items:
        raise PortfolioError("add at least one position")
    out = collections.OrderedDict()
    for item in items:
        sym, sep, raw = item.strip().partition(":")
        sym, raw = sym.strip().upper(), raw.strip().lower()
        if not sep or not _TICKER_RE.match(sym):
            raise PortfolioError(f"invalid ticker {sym or item.strip()!r}")
        m = _AMOUNT_RE.match(raw.rstrip("%"))
        if not m:
            raise PortfolioError(f"{sym}: invalid amount {raw!r}")
        dollar, value, shares = m.group(1), float(m.group(2)), m.group(3)
        if mode == "pct":
            if dollar or shares:
                raise PortfolioError(f"{sym}: enter a % weight")
            kind = "pct"
        else:
            if bool(dollar) == bool(shares):
                raise PortfolioError(f"{sym}: enter dollars ($) or shares (sh)")
            kind = "usd" if dollar else "sh"
        if not (value > 0 and math.isfinite(value)):
            raise PortfolioError(f"{sym}: the amount must be positive")
        if sym in out:
            if out[sym][0] != kind:
                raise PortfolioError(f"{sym} is listed twice with different units")
            value += out[sym][1]
        out[sym] = (kind, value)
    if len(out) > MAX_POSITIONS:
        raise PortfolioError(f"at most {MAX_POSITIONS} positions")
    if mode == "pct":
        total = sum(v for _, v in out.values())
        if abs(total - 100.0) > PCT_TOLERANCE:
            raise PortfolioError(f"weights add up to {total:g}%, not 100%")
    return out


def market_weights(positions, prices):
    """Market-value weights (fractions, largest first) and the positions that could not be valued (shares of a
    ticker without a price) as ``{ticker: reason}``."""
    values, unvalued = {}, {}
    for sym, (kind, v) in positions.items():
        if kind == "sh":
            px = prices.get(sym)
            if not isinstance(px, (int, float)) or not math.isfinite(px) or px <= 0:
                unvalued[sym] = "no price to value the shares"
                continue
            v = v * float(px)
        values[sym] = v
    total = sum(values.values())
    if total <= 0:
        return collections.OrderedDict(), unvalued
    weights = collections.OrderedDict(sorted(((s, v / total) for s, v in values.items()), key=lambda kv: -kv[1]))
    return weights, unvalued


def portfolio_key(weights, day):
    """A day's id for a portfolio: its weights (rounded) only, never the amounts typed in."""
    blob = json.dumps([day.isoformat(), sorted((s, round(w, 6)) for s, w in weights.items())])
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


_SENTIMENT_RE = re.compile(r"(?:^|\n)\W*SENTIMENT\W*:?\s*(.+)", re.IGNORECASE)
_BULL_RE = re.compile(r"(?:^|\n)[^\w\n]*Bull\W*:\s*(.+?)(?=\n[^\w\n]*Bear\W*:|\Z)", re.IGNORECASE | re.DOTALL)
_BEAR_RE = re.compile(r"(?:^|\n)[^\w\n]*Bear\W*:\s*(.+)", re.IGNORECASE | re.DOTALL)


def parse_verdict(text):
    """The X-ray's Section 1 → ``{verdict, bull, bear}`` (missing parts are None). Cashtags lose their ``$``, as in
    main.py's thread."""
    def clean(v):
        if not v:
            return None
        v = re.sub(r"\$([A-Z]{1,6})\b", r"\1", v.strip())
        v = re.sub(r"\n\s*PORTFOLIO:?\s*$", "", v).strip()
        return re.sub(r"[*_#`]+", "", v).strip() or None

    m = _SENTIMENT_RE.search(text or "")
    verdict = m.group(1).split("\n")[0] if m else None
    b = _BULL_RE.search(text or "")
    r = _BEAR_RE.search(text or "")
    return {"verdict": clean(verdict), "bull": clean(b.group(1)) if b else None, "bear": clean(r.group(1)) if r else None}


def _num(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _benchmarks():
    """QQQ and the S&P 500's 5Y total-return CAGRs (the donut's "5Y Index Return vs Base" box), via yfinance."""
    import matplotlib
    matplotlib.use("Agg")
    from graphics.visualizer import LynchPinVisualizer
    viz = LynchPinVisualizer.__new__(LynchPinVisualizer)
    out = []
    for src in ("qqq", "spy"):
        try:
            label, cagr = viz._get_benchmark_data(src)
            if isinstance(cagr, (int, float)) and math.isfinite(cagr):
                out.append((label, float(cagr)))
        except Exception as e:  # noqa: BLE001 — the box just lists fewer indexes
            print(f"⚠️  portfolio benchmark {src}: {type(e).__name__}: {e}")
    return out


class PortfolioAnalyzer:
    """Rolls today's ticker analyses (``jobs``, a ui.jobs.JobManager) up into portfolio X-rays.

    ``llm`` / ``gemini``: the AI overview's backends (the local model first, Gemini when it is down); None = off.
    ``benchmarks()`` → ``[(label, cagr %), ...]`` for the donut, fetched once a day.
    ``plot`` draws the donut: ``plot(weights, wm, benchmark_returns, out_dir, filename) → (path, timings)``."""

    def __init__(self, settings, jobs, llm=None, gemini=None, benchmarks=None, plot=None, today=_dt.date.today,
                 clock=time.monotonic):
        self.settings, self.jobs, self.llm, self.gemini = settings, jobs, llm, gemini
        self._benchmarks_fn = benchmarks or _benchmarks
        if plot is None:
            from ui.fastchart import plot_portfolio as plot
        self._plot = plot
        self._today, self._clock = today, clock
        self._lock = threading.Lock()
        self._day = None
        self._results = collections.OrderedDict()  # key → {"result", "ai", "prompt"}
        self._bench = None
        self._bench_lock = threading.Lock()
        self._ai_gate = threading.Semaphore(1)  # one portfolio overview at a time (the ticker overviews come first)

    @property
    def ai_enabled(self):
        return self.llm is not None or self.gemini is not None

    def _roll(self):
        """Lock held: a new day starts with no portfolios (the ticker cache resets at midnight too)."""
        day = self._today()
        if day != self._day:
            self._day, self._results = day, collections.OrderedDict()
        return day

    def chart_dir(self, day=None):
        return os.path.join(self.settings.cache_dir, "portfolio", (day or self._today()).isoformat())

    # ── analysis ─────────────────────────────────────────────────────────────
    def analyze(self, positions, poll=False, client=None):
        """Snapshot of the portfolio ``positions`` (parse_positions' output): ``running`` while any position is still
        being analysed (with each position's progress), else the X-ray (``done``) or why there is none."""
        snaps = {}
        for sym in positions:
            snap = self.jobs.request(sym, poll=poll, client=client)
            if snap.get("status") == "expired":  # the lookup a poll belonged to is gone: look it up again
                snap = self.jobs.request(sym, client=client)
            snaps[sym] = snap
        pending = [s for s, snap in snaps.items() if snap.get("status") not in FINAL]
        if pending:
            rows = []
            for sym, snap in snaps.items():
                rows.append({"ticker": sym, "status": snap.get("status"), "stage": snap.get("stage"),
                             "queue_position": snap.get("queue_position"), "note": snap.get("note") or
                             (snap.get("error") if snap.get("status") == "busy" else None)})
            retry = max([snaps[s].get("retry_after") or 0 for s in pending] + [0])
            return {"status": "running", "positions": rows, "done": len(snaps) - len(pending), "total": len(snaps),
                    "retry_after": retry or None}
        return self._result(positions, snaps)

    def _result(self, positions, snaps):
        entries = {sym: self._entry(sym) for sym in positions}
        prices = {sym: _num((e or {}).get("price")) for sym, e in entries.items()}
        weights, unvalued = market_weights(positions, prices)
        if not weights:
            return {"status": "error", "error": "none of the positions could be valued"}
        day = self._today()
        key = portfolio_key(weights, day)
        with self._lock:
            self._roll()
            kept = self._results.get(key)
            if kept is not None:
                self._results.move_to_end(key)
                return kept["result"]

        rows, grader, credit, briefs, excluded = [], {}, {}, {}, {}
        for sym in weights:
            e = entries.get(sym) or {}
            inputs = e.get("_ai_inputs") or {}
            if e.get("status") == "done" and inputs.get("row"):
                rows.append(dict(inputs["row"], Weight=weights[sym]))
                if inputs.get("g"):
                    grader[sym] = inputs["g"]
                if inputs.get("b"):
                    credit[sym] = inputs["b"]
                try:
                    from ui.llm import portal_data
                    briefs[sym] = portal_data(e)
                except Exception as ex:  # noqa: BLE001 — the prompt falls back to the raw metrics
                    print(f"⚠️  portfolio brief {sym}: {type(ex).__name__}: {ex}")
            else:
                excluded[sym] = (e.get("reason") or snaps.get(sym, {}).get("error")
                                 or "no GARP data").replace("no GARP data (", "").rstrip(")") or "no GARP data"
        if not rows:
            return {"status": "nodata", "error": "none of the positions has GARP valuation data",
                    "positions": self._positions(weights, entries, excluded, unvalued)}

        wm = weighted_metrics(rows, weights, grader, credit)
        summary = format_weighted_summary(wm)
        if excluded:
            summary += ("\n  Excluded (no GARP data — negative/absent forward PE or growth): "
                        + ", ".join(f"{s} ({weights[s] * 100:.1f}%)" for s in excluded))

        chart = None
        try:
            path, _ = self._plot(dict(weights), wm, self.benchmarks(), self.chart_dir(day), f"{key}.png")
            if path and os.path.exists(path):
                v = int(os.path.getmtime(path))
                chart = {"url": f"/portfolio/{key}.png?v={v}", "preview": f"/portfolio/{key}.jpg?v={v}"}
        except Exception as ex:  # noqa: BLE001 — the numbers stand without the picture
            print(f"⚠️  portfolio chart: {type(ex).__name__}: {ex}")

        result = {
            "status": "done", "key": key, "day": day.isoformat(),
            "positions": self._positions(weights, entries, excluded, unvalued),
            "metrics": {k: (_num(v) if not isinstance(v, str) else v) for k, v in wm.items()},
            "summary": summary, "chart": chart, "ai": self.ai_enabled,
        }
        from engine.ai_research import LynchPinResearcher
        prompt = LynchPinResearcher.build_portfolio_prompt(rows, summary, grader, credit, ticker_briefs=briefs)
        with self._lock:
            if self._roll() == day:
                self._results[key] = {"result": result, "prompt": prompt, "ai": None, "weights": dict(weights),
                                      "entries": entries, "summary": summary}
                while len(self._results) > MAX_RESULTS:
                    self._results.popitem(last=False)
        return result

    def _entry(self, sym):
        entry = getattr(self.jobs, "entry", None)
        return entry(sym) if entry else self.jobs.lookup(sym)

    @staticmethod
    def _positions(weights, entries, excluded, unvalued):
        out = []
        for sym in list(weights) + list(unvalued):
            e = entries.get(sym) or {}
            st = e.get("stats") or {}
            out.append({
                "ticker": sym, "name": e.get("name") or sym, "weight": weights.get(sym),
                "status": e.get("status") or "error",
                "garp": sym in weights and sym not in excluded,
                "reason": excluded.get(sym) or unvalued.get(sym),
                "peg": _num(st.get("PEG")), "dev_sd": _num(st.get("Dev_SD")), "fwd_pe": _num(st.get("FwdPE")),
                "growth": _num(st.get("growth_pct")), "base": _num(st.get("Base")),
                "income": (e.get("income") or {}).get("grade"), "credit": (e.get("credit") or {}).get("rating"),
            })
        return out

    def benchmarks(self):
        day = self._today()
        with self._bench_lock:
            if self._bench is None or self._bench[0] != day:
                self._bench = (day, self._benchmarks_fn())
            return self._bench[1]

    def plot_path(self, key, preview=False):
        if not KEY_RE.match(key or ""):
            return None
        path = os.path.join(self.chart_dir(), f"{key}.png")
        if not os.path.exists(path):
            return None
        if preview:
            from ui.imaging import PLOT_PREVIEW_WIDTH, jpeg_preview, plot_preview_dir
            return jpeg_preview(path, plot_preview_dir(path), PLOT_PREVIEW_WIDTH)
        return path

    def deep_dive(self, key):
        """The Portfolio Deep Dive Prompt of today's X-ray ``key`` (ui/deepdive.py), with the AI verdict once it is
        written; None when there is no such X-ray today."""
        from ui.deepdive import build_portfolio_deep_dive
        with self._lock:
            self._roll()
            kept = self._results.get(key)
            if kept is None:
                return None
            ai = kept["ai"]
            view = self._ai_view(ai) if ai is not None else None
        if not self.ai_enabled:
            state = "disabled"
        elif view is not None and view["status"] == "done":
            state = "included"
        elif view is None or view["status"] in ("queued", "running"):  # None: the page asks for it next
            state = "pending"
        else:
            state = "unavailable"
        text = build_portfolio_deep_dive(kept["result"], kept["weights"], kept["entries"], kept["summary"],
                                         ai=view if state == "included" else None, ai_state=state)
        return {"prompt": text, "words": len(text.split()), "ai": state}

    # ── AI overview ──────────────────────────────────────────────────────────
    def request_ai(self, key):
        """The portfolio verdict of today's X-ray ``key``: starts it on the first request, then reports progress
        (``queued`` | ``running`` with the text so far) until it is ``done`` | ``error`` | ``unavailable``."""
        if not self.ai_enabled:
            return {"status": "unavailable", "error": "AI overview disabled"}
        with self._lock:
            self._roll()
            kept = self._results.get(key)
            if kept is None:
                return {"status": "error", "need_analysis": True,
                        "error": "no portfolio analysis for this key today — analyze it again"}
            ai = kept["ai"]
            if ai is None or (ai["status"] in ("error", "unavailable") and ai.get("retry", True)
                              and self._clock() - ai.get("finished", 0) > 3):
                ai = kept["ai"] = {"status": "queued", "text": "", "started": self._clock()}
                threading.Thread(target=self._run_ai, args=(kept,), name=f"portfolio-ai-{key}", daemon=True).start()
            return self._ai_view(ai)

    def _ai_view(self, ai):
        """What the page shows: status and phase, the verdict so far and the generation metrics (TTFT, tok/s,
        tokens, thinking, total time — the ticker overview's metrics bar)."""
        out = {k: ai.get(k) for k in ("status", "error", "model", "backend", "phase", "reasoning")}
        text = ai.get("text") or ""
        out["text"] = text
        out.update(parse_verdict(text))
        metrics = dict(ai.get("metrics") or {})
        if ai["status"] in ("queued", "running") and ai.get("started") is not None:
            # the clock runs from the request (queue wait and prompt processing included), like the ticker's
            metrics["elapsed_s"] = round(self._clock() - ai["started"], 1)
        out["metrics"] = metrics
        return out

    def _client(self):
        """The backend for the next overview: the local model while it answers, else Gemini within its quota."""
        if self.llm is not None:
            try:
                if self.llm.status().get("available"):
                    return self.llm, "local"
            except Exception:  # noqa: BLE001 — try Gemini
                pass
        if self.gemini is not None:
            budget = getattr(self.gemini, "budget", None)
            if budget is None or budget.try_acquire("PORTFOLIO"):
                return self.gemini, "gemini"
        return None, None

    def _run_ai(self, kept):
        ai = kept["ai"]
        with self._ai_gate:
            client, backend = self._client()
            if client is None:
                ai.update(status="unavailable", error="AI overviews are unavailable right now — try again shortly",
                          finished=self._clock())
                return
            ai.update(status="running", backend=backend, phase="connecting")
            parts = []
            waited = self._clock() - ai["started"]  # queued behind another portfolio's overview

            def timed(m):
                m = dict(m or {})
                for k in ("ttft_s", "elapsed_s"):  # from the request, as the page's clock runs
                    if isinstance(m.get(k), (int, float)):
                        m[k] = round(m[k] + waited, 2 if k == "ttft_s" else 1)
                return m

            def on_delta(reasoning, content, metrics):
                ai["metrics"] = timed(metrics)
                if reasoning and not parts:
                    ai["phase"] = "thinking"
                if content:
                    ai["phase"] = "writing"
                    parts.append(content)
                    ai["text"] = "".join(parts)

            def on_rewind():
                parts.clear()
                ai["text"] = ""

            try:
                text, meta = client.generate(kept["prompt"], on_delta=on_delta, on_rewind=on_rewind)
            except Exception as e:  # noqa: BLE001 — shown on the page; a retry may work
                from ui.llm import public_error
                ai.update(status="error", error=public_error(e), finished=self._clock())
                return
            from ui.llm import strip_thinking
            ai["text"] = strip_thinking(text)
            parsed = parse_verdict(ai["text"])
            ok = bool(parsed["verdict"] or parsed["bull"] or parsed["bear"])
            ai.update(status="done" if ok else "error", model=meta.get("model"), finished=self._clock(),
                      error=None if ok else "the model returned no verdict", phase=None,
                      metrics=timed(meta.get("metrics")), reasoning=meta.get("reasoning"))
