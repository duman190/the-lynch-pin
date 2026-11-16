"""The Portfolio X-ray's 5-year backtest (``/api/portfolio/backtest?k=KEY``, "5Y backtest" under View chart): what the
portfolio's weights, bought five years ago and held, would have done against VOO, QQQ, SCHD, SMH and IGV, all with
dividends reinvested — one value a week for the page's line chart, and a risk / return table (total return, CAGR,
volatility, Sharpe, Sortino, max drawdown, best / worst 12 months, correlation and beta against the portfolio).

* **Data:** Yahoo's weekly chart (5 years, adjusted closes: dividends and splits reinvested) of each holding and
  benchmark, plus ^IRX (13-week T-bill) for the risk-free rate. Only finished weeks count: the backtest ends with
  the last week that has closed, and a new point is added on Saturday. A series is fetched once a week and kept in
  memory and on disk (``cache_dir/backtest/SYM.json``), so the benchmark ETFs — and any ticker someone backtested
  this week — cost nothing, across restarts too; one backtest runs at a time, its fetches four at once.
* **Maths:** the native library (ui/native/src/backtest.rs, ctypes drops the GIL for the call), like the trading
  view's bars; ``compute_python`` below is its numpy twin (no library, an older build, or
  ``LYNCH_UI_NATIVE_BACKTEST=0``) and ui/tests/test_backtest.py checks the two agree. backtest.rs's header has the
  exact definitions.
"""
import concurrent.futures
import ctypes
import datetime as _dt
import json
import math
import os
import threading
import time

import numpy as np

from ui import fastchart

BENCHMARKS = ("VOO", "QQQ", "SCHD", "SMH", "IGV")
RF_SYMBOL = "^IRX"
YEARS = 5
START_VALUE = 10_000.0
YAHOO_CHART = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
QUERY = {"interval": "1wk", "range": "6y", "events": "div,splits", "includeAdjustedClose": "true"}
FETCH_TIMEOUT = 20
MAX_REPLY = 4 << 20
FAIL_RETRY = 30.0  # s before a failed backtest may be asked again
ABI = 1


class BacktestError(Exception):
    pass


class NoData(BacktestError):
    pass


# ── Yahoo ──────────────────────────────────────────────────────────────────────────────────────────────────────
def parse_yahoo(raw):
    """Yahoo's chart JSON (bytes or dict) → {"t": [unix s], "adj": [adjusted close or None]}. Raises NoData."""
    d = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    chart = (d or {}).get("chart") or {}
    res = chart.get("result") or []
    if not res:
        err = chart.get("error") or {}
        raise NoData(err.get("description") or "no data")
    r = res[0]
    t = r.get("timestamp") or []
    ind = r.get("indicators") or {}
    adj = ((ind.get("adjclose") or [{}])[0] or {}).get("adjclose")
    if adj is None:  # no adjusted series (an index such as ^IRX): its close
        adj = ((ind.get("quote") or [{}])[0] or {}).get("close") or []
    if not t or len(adj) != len(t):
        raise NoData("no price history")
    return {"t": [int(x) for x in t], "adj": [x if isinstance(x, (int, float)) and math.isfinite(x) else None
                                               for x in adj]}


class YahooWeekly:
    """GET of one symbol's weekly chart on a browser-like session (as the trading view's fetcher) → raw bytes."""

    def __init__(self):
        self._local = threading.local()

    def __call__(self, sym):
        s = getattr(self._local, "session", None)
        if s is None:
            from engine.http_session import browser_session
            s = self._local.session = browser_session()
        r = s.get(YAHOO_CHART.format(sym=sym), params=QUERY, timeout=FETCH_TIMEOUT)
        if r.status_code == 404:
            raise NoData(f"{sym}: unknown symbol")
        if r.status_code != 200 or len(r.content) > MAX_REPLY:
            raise BacktestError(f"{sym}: Yahoo HTTP {r.status_code}")
        return r.content


# ── the maths: numpy twin of ui/native/src/backtest.rs ────────────────────────────────────────────────────────
def _monday(t):
    day = t // 86400
    return day - (day + 3) % 7


def _weekly(series):
    weeks, vals = [], []
    for t, a in zip(series["t"], series["adj"]):
        if a is None or not (a > 0 and math.isfinite(a)):
            continue
        w = _monday(int(t))
        if weeks and weeks[-1] == w:
            vals[-1] = a
        elif weeks and weeks[-1] > w:
            continue
        else:
            weeks.append(w)
            vals.append(float(a))
    if not weeks:
        raise NoData(f"{series.get('sym', '?')}: no price history")
    return weeks, vals


def _metrics(v, years, rf_w):
    r = v[1:] / v[:-1] - 1.0
    ex = r - rf_w
    mean_ex = ex.mean()
    vol = r.std(ddof=1) * math.sqrt(52) if len(r) > 1 else math.nan
    down = math.sqrt(np.mean(np.minimum(ex, 0.0) ** 2)) * math.sqrt(52)
    peak = np.maximum.accumulate(v)
    roll = v[52:] / v[:-52] - 1.0 if len(v) > 52 else np.array([])
    fin = lambda x: float(x) if x is not None and math.isfinite(x) else None  # noqa: E731
    return {
        "total": fin(v[-1] / v[0] - 1.0),
        "cagr": fin((v[-1] / v[0]) ** (1.0 / years) - 1.0) if years > 0 else None,
        "vol": fin(vol),
        "sharpe": fin(mean_ex * 52 / vol) if vol > 0 else None,
        "sortino": fin(mean_ex * 52 / down) if down > 0 else None,
        "max_dd": fin(min(0.0, float((v / peak - 1.0).min()))),
        "best_12m": fin(roll.max()) if len(roll) else None,
        "worst_12m": fin(roll.min()) if len(roll) else None,
        "corr": None, "beta": None,
    }, r


def compute_python(req):
    """The request (as for backtest.rs) → the same output dict."""
    hold, weights, bench = req["holdings"], req["weights"], req["benchmarks"]
    if not hold or len(hold) != len(weights):
        raise BacktestError("one weight per holding")
    wsum = sum(weights)
    if not wsum > 0 or any(not (math.isfinite(w) and w >= 0) for w in weights):
        raise BacktestError("weights must be positive")
    start_value = req.get("start_value") or START_VALUE
    hw = [_weekly(s) for s in hold]
    bw = [_weekly(s) for s in bench]
    frm = _monday(int(req["start_day"]) * 86400)
    common = [w for w in hw[0][0] if w >= frm]
    for weeks, _ in hw[1:] + bw:
        have = set(weeks)
        common = [w for w in common if w in have]
    if len(common) < 2:
        raise NoData("not enough weeks of shared price history")

    def pick(wv):
        idx = {w: i for i, w in enumerate(wv[0])}
        return np.array([wv[1][idx[w]] for w in common])

    pc = np.zeros(len(common))
    for wv, w in zip(hw, weights):
        b = pick(wv)
        pc += (w / wsum * start_value / b[0]) * b
    years = (common[-1] - common[0]) / 365.25
    rf_w = (1.0 + req.get("rf", 0.0)) ** (1.0 / 52) - 1.0
    pm, pr = _metrics(pc, years, rf_w)
    pm.update(corr=1.0, beta=1.0)
    lines = []
    for src, wv in zip(bench, bw):
        b = pick(wv)
        c = b * (start_value / b[0])
        m, br = _metrics(c, years, rf_w)
        cpb = np.cov(pr, br, ddof=1)
        m["corr"] = float(cpb[0, 1] / math.sqrt(cpb[0, 0] * cpb[1, 1])) if cpb[0, 0] > 0 and cpb[1, 1] > 0 else None
        m["beta"] = float(cpb[0, 1] / cpb[1, 1]) if cpb[1, 1] > 0 else None
        lines.append({"sym": src["sym"], "c": c.tolist(), "metrics": m})
    return {"weeks": common, "portfolio": pc.tolist(), "metrics": pm, "benchmarks": lines,
            "first_weeks": [wv[0][0] for wv in hw]}


# ── the native library ────────────────────────────────────────────────────────────────────────────────────────
class _Out(ctypes.Structure):
    _fields_ = [("body", ctypes.c_void_p), ("body_len", ctypes.c_size_t), ("gz", ctypes.c_void_p),
                ("gz_len", ctypes.c_size_t), ("meta", ctypes.c_void_p), ("meta_len", ctypes.c_size_t)]


_lib, _lib_error = None, None
_lib_guard = threading.Lock()


def native():
    """The library with ``lynch_backtest_run``, or None (``native_unavailable_reason()`` says why)."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    with _lib_guard:
        if _lib is not None or _lib_error is not None:
            return _lib
        if os.environ.get("LYNCH_UI_NATIVE_BACKTEST", "1").strip().lower() in ("0", "false", "no", "off"):
            _lib_error = "disabled by LYNCH_UI_NATIVE_BACKTEST"
            return None
        path = fastchart.library_path()
        try:
            lib = ctypes.CDLL(path)
            lib.lynch_backtest_abi.restype = ctypes.c_uint32
            if lib.lynch_backtest_abi() != ABI:
                raise OSError(f"backtest ABI {lib.lynch_backtest_abi()} != {ABI}")
            lib.lynch_backtest_run.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(_Out)]
            lib.lynch_backtest_run.restype = ctypes.c_int32
            lib.lynch_bars_free.argtypes = [ctypes.POINTER(_Out)]
            lib.lynch_bars_free.restype = None
        except (OSError, AttributeError) as e:
            _lib_error = f"{path}: {e} (rebuild ui/native: cd ui/native && cargo build --release)"
            return None
        _lib = lib
        return lib


def native_unavailable_reason():
    native()
    return _lib_error


def compute_native(req):
    lib = native()
    if lib is None:
        raise BacktestError(f"native backtest unavailable: {_lib_error}")
    rq = json.dumps(req, allow_nan=False).encode()
    out = _Out()
    rc = lib.lynch_backtest_run(rq, len(rq), ctypes.byref(out))
    try:
        if rc != 0:
            msg = ctypes.string_at(out.meta, out.meta_len).decode("utf-8", "replace") if out.meta_len else "failed"
            raise (NoData if rc == 2 else BacktestError)(msg)
        body = ctypes.string_at(out.body, out.body_len)
    finally:
        lib.lynch_bars_free(ctypes.byref(out))
    return json.loads(body)


def compute(req, use_native=None):
    """backtest.rs when the library has it, else the numpy twin. Returns (output, native: bool)."""
    if use_native is None:
        use_native = native() is not None
    return (compute_native(req), True) if use_native else (compute_python(req), False)


# ── the service ───────────────────────────────────────────────────────────────────────────────────────────────
def last_closed_week(today):
    """The Monday of the newest finished trading week: this week's from Saturday on, else last week's."""
    monday = today - _dt.timedelta(days=today.weekday())
    return monday if today.weekday() >= 5 else monday - _dt.timedelta(days=7)


def _days(d):
    return (d - _dt.date(1970, 1, 1)).days


def _iso(day):
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(day))).isoformat()


class Backtester:
    """5Y backtests of portfolio X-rays, built in the background (the page polls), kept for the day.

    ``fetch(sym)`` → Yahoo's weekly chart JSON (bytes); ``today()`` → the date (its last finished week ends the
    backtest); ``cache_dir`` keeps the week's series across restarts (None: memory only)."""

    def __init__(self, fetch=None, today=_dt.date.today, clock=time.monotonic, workers=4, use_native=None,
                 cache_dir=None):
        self._fetch = fetch or YahooWeekly()
        self._today, self._clock, self._use_native = today, clock, use_native
        self._dir = os.path.join(cache_dir, "backtest") if cache_dir else None
        self._lock = threading.Lock()
        self._week = None   # the last finished week (its Monday) the cached series end with
        self._series = {}   # sym → parsed series (this week's)
        self._jobs = {}     # key → {"status", ...}
        self._gate = threading.Semaphore(1)  # one backtest at a time
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="backtest")

    def _roll(self):
        week = last_closed_week(self._today())
        if week != self._week:  # Saturday: one more finished week for every series
            self._week, self._series, self._jobs = week, {}, {}

    def request(self, key, weights):
        """The backtest of ``weights`` (ticker → weight) under ``key``: started on the first ask, then its state."""
        with self._lock:
            self._roll()
            job = self._jobs.get(key)
            if job is not None and job["status"] == "error" and self._clock() - job["at"] > FAIL_RETRY:
                job = None
            if job is None:
                job = self._jobs[key] = {"status": "running", "at": self._clock()}
                threading.Thread(target=self._run, args=(key, dict(weights), job), daemon=True,
                                 name=f"backtest-{key}").start()
            return {k: v for k, v in job.items() if k != "at"}

    def _path(self, sym):
        return os.path.join(self._dir, sym.replace("^", "_") + ".json") if self._dir else None

    def _get(self, sym, week):
        with self._lock:
            s = self._series.get(sym)
        if s is not None:
            return s
        path = self._path(sym)
        if path:
            try:
                with open(path) as f:
                    d = json.load(f)
                if d.get("week") == week.isoformat():
                    s = {"sym": sym, "t": d["t"], "adj": d["adj"]}
            except (OSError, ValueError, KeyError):
                s = None
        if s is None:
            s = parse_yahoo(self._fetch(sym))
            last = _days(week)  # finished weeks only: Yahoo's live bar of the week under way is left out
            keep = [i for i, t in enumerate(s["t"]) if _monday(t) <= last]
            s = {"sym": sym, "t": [s["t"][i] for i in keep], "adj": [s["adj"][i] for i in keep]}
            if path:
                try:
                    os.makedirs(self._dir, exist_ok=True)
                    tmp = f"{path}.{threading.get_ident()}.tmp"
                    with open(tmp, "w") as f:
                        json.dump({"week": week.isoformat(), "t": s["t"], "adj": s["adj"]}, f, separators=(",", ":"))
                    os.replace(tmp, path)
                except OSError as e:
                    print(f"⚠️  backtest cache {sym}: {e}")
        with self._lock:
            if self._week == week:
                self._series[sym] = s
        return s

    def _run(self, key, weights, job):
        t0 = time.perf_counter()
        try:
            with self._gate:
                with self._lock:
                    week = self._week
                syms = list(weights) + [b for b in BENCHMARKS if b not in weights] + [RF_SYMBOL]
                futs = {s: self._pool.submit(self._get, s, week) for s in dict.fromkeys(syms)}
                series, missing = {}, []
                for s, f in futs.items():
                    try:
                        series[s] = f.result()
                    except Exception as e:  # noqa: BLE001 — reported below
                        if s == RF_SYMBOL:
                            continue
                        missing.append(f"{s} ({e})")
                if missing:
                    raise BacktestError("no price history for " + ", ".join(missing))
                result = self._build(weights, series, week)
            result["elapsed_s"] = round(time.perf_counter() - t0, 3)
            update = {"status": "done", **result}
        except Exception as e:  # noqa: BLE001 — the page shows it and may retry
            update = {"status": "error", "error": str(e) or type(e).__name__}
        with self._lock:
            job.update(update, at=self._clock())

    def _build(self, weights, series, week):
        end = week + _dt.timedelta(days=4)  # the Friday that closed it
        try:
            start = end.replace(year=end.year - YEARS)
        except ValueError:  # 29 February
            start = end.replace(year=end.year - YEARS, day=28)
        start_day = _days(start)
        rf, rf_note = 0.0, "risk-free rate 0% (^IRX unavailable)"
        irx = series.get(RF_SYMBOL)
        if irx:
            vals = [a for t, a in zip(irx["t"], irx["adj"]) if a is not None and t // 86400 >= start_day]
            if vals:
                rf = float(np.mean(vals)) / 100.0
                rf_note = f"risk-free rate {rf * 100:.2f}% (13-week T-bill, 5Y average)"
        syms = list(weights)
        req = {"holdings": [series[s] for s in syms], "weights": [float(weights[s]) for s in syms],
               "benchmarks": [series[b] for b in BENCHMARKS], "start_day": start_day, "rf": rf,
               "start_value": START_VALUE}
        out, used_native = compute(req, self._use_native)
        weeks = out["weeks"]
        late = [(w, s) for w, s in zip(out["first_weeks"], syms) if w > _monday(start_day * 86400) + 7]
        notes = [rf_note, "bought at the first week's close and held, no rebalancing; dividends reinvested"]
        if late:
            w, s = max(late)
            notes.insert(0, f"starts {_iso(weeks[0])}: ${s}'s price history begins then")
        return {
            "start": _iso(weeks[0]), "end": _iso(weeks[-1]), "next_update": (week + _dt.timedelta(days=12)).isoformat(), "weeks": [_iso(w) for w in weeks],
            "start_value": START_VALUE, "rf": rf,
            "series": [{"sym": "Portfolio", "values": [round(v, 2) for v in out["portfolio"]], "metrics": out["metrics"]}]
            + [{"sym": b["sym"], "values": [round(v, 2) for v in b["c"]], "metrics": b["metrics"]}
               for b in out["benchmarks"]],
            "notes": notes, "native": used_native,
        }

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
