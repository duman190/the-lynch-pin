"""The trading view's data, served by ui/tradingview.py: which Yahoo series each chart interval is built from, and the
build itself — Yahoo's chart JSON → candles at the chart's interval with SMA 50/100/200, EMA 8/21, RSI 14 and
MACD 12/26/9, as the JSON the page draws (and its gzip).

The build runs in the native library (ui/native/src/bars.rs, the crate of the native chart renderer): ctypes drops
the GIL for the call, so the portal's HTTP threads build charts on every core and Python never touches a bar.
Without the library (not built, an older build, or ``LYNCH_UI_NATIVE_BARS=0``) the numpy code below builds the same
JSON; ui/tests/test_tradingview.py checks the two agree and ui/tests/tv_bench.py times them.

Intervals Yahoo doesn't serve are built from one it does (3m from 1m, 10m from 5m, 30m / 45m from 15m, 2H-4H from
1H, 3M / 6M / 12M from 1M), intraday candles counted from each session's open as TradingView does. Yahoo's own 30m
series is skipped, as yfinance skips it: its bars are misaligned.
"""
import ctypes
import json
import math
import os
import threading
import time
import zlib

from ui import fastchart

# code (TradingView's resolution codes, used in URLs) → (label, Yahoo series, unit, count)
INTERVALS = {
    "1": ("1m", "1m", "minute", 1),
    "2": ("2m", "2m", "minute", 2),
    "3": ("3m", "1m", "minute", 3),
    "5": ("5m", "5m", "minute", 5),
    "10": ("10m", "5m", "minute", 10),
    "15": ("15m", "15m", "minute", 15),
    "30": ("30m", "15m", "minute", 30),
    "45": ("45m", "15m", "minute", 45),
    "60": ("1H", "60m", "minute", 60),
    "120": ("2H", "60m", "minute", 120),
    "180": ("3H", "60m", "minute", 180),
    "240": ("4H", "60m", "minute", 240),
    "1D": ("1D", "1d", "day", 1),
    "1W": ("1W", "1wk", "week", 1),
    "1M": ("1M", "1mo", "month", 1),
    "3M": ("3M", "1mo", "month", 3),
    "6M": ("6M", "1mo", "month", 6),
    "12M": ("12M", "1mo", "month", 12),
}
DEFAULT_INTERVAL = "1D"
# Yahoo's 1900-01-01: with range=max, Yahoo coarsens weekly and monthly series to quarters
MAX_PERIOD1 = -2208994789
# Yahoo series → its query. Ranges: what Yahoo keeps for the interval (1m: 8 days, 2m-15m: 60 days, 1h: 730 days),
# 2m and daily shortened to what a chart shows (MAX_BARS).
BASES = {
    "1m": {"interval": "1m", "range": "7d"},
    "2m": {"interval": "2m", "range": "1mo"},
    "5m": {"interval": "5m", "range": "60d"},
    "15m": {"interval": "15m", "range": "60d"},
    "60m": {"interval": "60m", "range": "730d"},
    "1d": {"interval": "1d", "range": "10y"},
    "1wk": {"interval": "1wk", "period1": MAX_PERIOD1},
    "1mo": {"interval": "1mo", "period1": MAX_PERIOD1},
}
MAX_BARS = 3000   # candles sent to the page (the most recent); indicators are computed over the whole series first
# deflate level: 2 is ~8% bigger than 6 and 3-4x faster (miniz: 3.6 vs 16.5 ms for 3000 one-minute bars)
GZIP_LEVEL = 2
ABI = 1


class NoData(Exception):
    """Yahoo has no chart for this symbol (unknown, delisted): remembered for a while."""


class BuildError(RuntimeError):
    pass


def request_for(code, max_bars=MAX_BARS, gzip_level=GZIP_LEVEL):
    label, _, unit, count = INTERVALS[code]
    return {"interval": code, "label": label, "unit": unit, "count": count, "max_bars": max_bars, "gzip": gzip_level}


def query_params(base, now=None):
    """Yahoo's query string for one series (regular session only)."""
    q = dict(BASES[base])
    if "period1" in q:
        q["period2"] = int(now if now is not None else time.time())
    q.update(includePrePost="false", events="div,splits")
    return q


# ── the native builder ──────────────────────────────────────────────────────────────────────────────────────────
class _BarsOut(ctypes.Structure):
    _fields_ = [("body", ctypes.c_void_p), ("body_len", ctypes.c_size_t), ("gz", ctypes.c_void_p),
                ("gz_len", ctypes.c_size_t), ("meta", ctypes.c_void_p), ("meta_len", ctypes.c_size_t)]


_lib = None
_lib_error = None
_lib_guard = threading.Lock()


def native():
    """The native builder (the chart renderer's library with ``lynch_bars_*``), or None."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    with _lib_guard:
        if _lib is not None or _lib_error is not None:
            return _lib
        if os.environ.get("LYNCH_UI_NATIVE_BARS", "1").strip().lower() in ("0", "false", "no", "off"):
            _lib_error = "disabled by LYNCH_UI_NATIVE_BARS"
            return None
        path = fastchart.library_path()
        try:
            lib = ctypes.CDLL(path)
            lib.lynch_bars_abi.restype = ctypes.c_uint32
            if lib.lynch_bars_abi() != ABI:
                raise OSError(f"bars ABI {lib.lynch_bars_abi()} != {ABI} (rebuild ui/native)")
            lib.lynch_bars_build.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
                                             ctypes.POINTER(_BarsOut)]
            lib.lynch_bars_build.restype = ctypes.c_int32
            lib.lynch_bars_free.argtypes = [ctypes.POINTER(_BarsOut)]
            lib.lynch_bars_free.restype = None
        except (OSError, AttributeError) as e:
            _lib_error = f"{path}: {e} (rebuild ui/native: cd ui/native && cargo build --release)"
            return None
        _lib = lib
        return lib


def native_unavailable_reason():
    native()
    return _lib_error


class Built:
    __slots__ = ("body", "gz", "meta", "native")

    def __init__(self, body, gz, meta, native):
        self.body, self.gz, self.meta, self.native = body, gz, meta, native


def build_native(raw, req):
    lib = native()
    if lib is None:
        raise BuildError(f"native bars builder unavailable: {_lib_error}")
    rq = json.dumps(req).encode()
    out = _BarsOut()
    rc = lib.lynch_bars_build(raw, len(raw), rq, len(rq), ctypes.byref(out))
    try:
        meta = ctypes.string_at(out.meta, out.meta_len) if out.meta_len else b""
        if rc != 0:
            msg = meta.decode("utf-8", "replace")
            raise (NoData if rc == 2 else BuildError)(msg)
        body = ctypes.string_at(out.body, out.body_len)
        gz = ctypes.string_at(out.gz, out.gz_len) if out.gz_len else b""
    finally:
        lib.lynch_bars_free(ctypes.byref(out))
    return Built(body, gz, json.loads(meta), True)


def build(raw, code, max_bars=MAX_BARS, gzip_level=GZIP_LEVEL, use_native=None):
    """Yahoo's chart JSON (bytes) → Built(body, gz, meta, native). Raises NoData / BuildError."""
    req = request_for(code, max_bars, gzip_level)
    if use_native is None:
        use_native = native() is not None
    return build_native(raw, req) if use_native else build_python(raw, req)


# ── the same build in numpy (no native library) ───────────────────────────────────────────────────────────────────
DAY = 86400
DATE_NUDGE = 6 * 3600  # see bars.rs: keeps a daily / monthly stamp an hour off on its own date


def _sessions(tp):
    out = []

    def collect(v):
        if isinstance(v, list):
            for x in v:
                collect(x)
        elif isinstance(v, dict):
            if isinstance(v.get("start"), int) and isinstance(v.get("end"), int):
                out.append((v["start"], v["end"]))
            elif "regular" in v:
                collect(v["regular"])
    collect(tp)
    seen, res = set(), []
    for s, e in sorted(p for p in out if p[1] > p[0]):
        if s not in seen:
            seen.add(s)
            res.append((s, e))
    return res


def _civil_month_index(days):
    """Days since 1970-01-01 (int array) → year * 12 + month - 1 (Hinnant's civil_from_days, vectorised)."""
    import numpy as np
    z = days + 719468
    era = np.floor_divide(z, 146097)
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    m = np.where(mp < 10, mp + 3, mp - 9)
    y = yoe + era * 400 + (m <= 2)
    return y * 12 + m - 1


def _keys(t, unit, count, off, periods):
    import numpy as np
    count = max(1, int(count))
    date = np.floor_divide(t + off + DATE_NUDGE, DAY)
    if unit == "minute":
        step = 60 * count
        clock = np.floor_divide(t + off, step) * step - off
        if not periods:
            return clock
        starts = np.array([p[0] for p in periods], dtype=np.int64)
        ends = np.array([p[1] for p in periods], dtype=np.int64)
        i = np.searchsorted(starts, t, side="right") - 1
        ok = i >= 0
        s, e = starts[np.maximum(i, 0)], ends[np.maximum(i, 0)]
        ok &= t < e + 3600
        y = np.where(t >= e, e - 1, t)
        return np.where(ok, s + np.floor_divide(y - s, step) * step, clock)
    if unit == "day":
        return np.floor_divide(date, count)
    if unit == "week":
        return np.floor_divide(date + 3, 7 * count)
    if unit == "month":
        return np.floor_divide(_civil_month_index(date), count)
    raise BuildError(f"unknown unit {unit!r}")


def _sma(x, n):
    import numpy as np
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    cs = np.cumsum(np.concatenate(([0.0], x)))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _ema(x, n):
    import numpy as np
    from scipy.signal import lfilter
    out = np.full(len(x), np.nan)
    finite = np.flatnonzero(np.isfinite(x))
    if n <= 0 or not len(finite) or len(x) < finite[0] + n:
        return out
    first = finite[0]
    seed_i = first + n - 1
    a = 2.0 / (n + 1)
    seed = x[first:seed_i + 1].mean()
    out[seed_i] = seed
    rest = x[seed_i + 1:]
    if len(rest):
        out[seed_i + 1:], _ = lfilter([a], [1.0, -(1 - a)], rest, zi=[(1 - a) * seed])
    return out


def _rma(x, n, seed):
    import numpy as np
    from scipy.signal import lfilter
    if not len(x):
        return np.array([])
    y, _ = lfilter([1.0 / n], [1.0, -(n - 1.0) / n], x, zi=[(n - 1.0) / n * seed])
    return y


def _rsi(c, n):
    import numpy as np
    out = np.full(len(c), np.nan)
    if n <= 0 or len(c) <= n:
        return out
    d = np.diff(c)
    up, down = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    u0, d0 = up[:n].mean(), down[:n].mean()
    u = np.concatenate(([u0], _rma(up[n:], n, u0)))
    dn = np.concatenate(([d0], _rma(down[n:], n, d0)))
    with np.errstate(divide="ignore", invalid="ignore"):
        v = 100.0 - 100.0 / (1.0 + u / dn)
    v = np.where(dn == 0, np.where(u == 0, 50.0, 100.0), v)
    out[n:] = v
    return out


def _significant(x, digits):
    """Rounded to ``digits`` significant digits (volumes), as bars.rs does."""
    import numpy as np
    out = x.copy()
    nz = np.isfinite(x) & (x != 0)
    e = np.floor(np.log10(np.abs(x[nz]))).astype(int) - (digits - 1)
    p = np.power(10.0, np.maximum(e, 0))
    half_away = lambda v: np.sign(v) * np.floor(np.abs(v) + 0.5)  # Rust's f64::round, not numpy's half-to-even
    out[nz] = np.where(e <= 0, half_away(x[nz]), half_away(x[nz] / p) * p)
    return out


def _round_half_away(s):
    t = math.trunc(s)
    f = s - t
    return t + (1 if f >= 0.5 else -1 if f <= -0.5 else 0)


def _fmt(values, d):
    """Numbers as bars.rs writes them: rounded to ``d`` decimals, trailing zeros dropped, NaN → null."""
    p = 10 ** d
    out = []
    for x in values:
        s = x * p
        if not math.isfinite(s) or abs(s) >= 9.0e15:
            out.append("null")
            continue
        m = _round_half_away(s)
        if m == 0:
            out.append("0")
            continue
        sign = "-" if m < 0 else ""
        m = abs(m)
        ip, fp = divmod(m, p)
        if fp:
            out.append(f"{sign}{ip}.{str(fp).rjust(d, '0').rstrip('0')}")
        else:
            out.append(f"{sign}{ip}")
    return "[" + ",".join(out) + "]"


def build_python(raw, req):
    import numpy as np
    t0 = time.perf_counter()
    try:
        env = json.loads(raw)
        chart = env["chart"]
    except (ValueError, KeyError, TypeError) as e:
        raise BuildError(f"bad Yahoo JSON: {e}")
    results = chart.get("result") or []
    if not results:
        err = chart.get("error") or {}
        raise NoData(f"{err.get('code') or ''}: {err.get('description') or ''}" if err else "no data")
    r = results[0]
    meta = r.get("meta") or {}
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]

    def col(name):
        v = q.get(name) or []
        a = np.array([np.nan if x is None else x for x in v] + [np.nan] * (len(ts) - len(v)), dtype=float)
        return a[:len(ts)]

    t = np.array(ts, dtype=np.int64)
    o, h, l, c, vol = col("open"), col("high"), col("low"), col("close"), col("volume")
    px, mt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if len(t) and not np.isfinite(c[-1]) and isinstance(px, (int, float)) and isinstance(mt, (int, float)) and mt >= t[-1]:
        # the bar of the session in progress (or just closed) can come without its close (see bars.rs)
        c[-1] = px
        for a, v in ((h, meta.get("regularMarketDayHigh")), (l, meta.get("regularMarketDayLow"))):
            if not np.isfinite(a[-1]):
                a[-1] = v if isinstance(v, (int, float)) else px
        if not np.isfinite(o[-1]):
            o[-1] = px
        if not np.isfinite(vol[-1]) and isinstance(meta.get("regularMarketVolume"), (int, float)):
            vol[-1] = meta["regularMarketVolume"]
    keep = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    if len(t):  # never back in time (see bars.rs)
        keep &= np.concatenate(([True], t[1:] >= np.maximum.accumulate(t)[:-1]))
    t, o, h, l, c, vol = t[keep], o[keep], h[keep], l[keep], c[keep], vol[keep]
    h, l = np.maximum(np.maximum(h, o), c), np.minimum(np.minimum(l, o), c)
    vol = np.where(np.isfinite(vol), np.maximum(vol, 0.0), 0.0)
    bars_in = len(t)

    off = int(meta.get("gmtoffset") or 0)
    unit, count = req["unit"], req.get("count", 1)
    periods = _sessions(meta.get("tradingPeriods")) if unit == "minute" else []
    keys = _keys(t, unit, count, off, periods)
    if len(t):
        starts = np.flatnonzero(np.concatenate(([True], keys[1:] != keys[:-1])))
        ends = np.concatenate((starts[1:], [len(t)])) - 1
        t = keys[starts] if unit == "minute" else t[starts]
        o, c = o[starts], c[ends]
        h, l = np.maximum.reduceat(h, starts), np.minimum.reduceat(l, starts)
        vol = np.add.reduceat(vol, starts)

    sma50, sma100, sma200 = _sma(c, 50), _sma(c, 100), _sma(c, 200)
    ema8, ema21 = _ema(c, 8), _ema(c, 21)
    rsi14 = _rsi(c, 14)
    line = _ema(c, 12) - _ema(c, 26)
    sig = _ema(line, 9)

    n = len(t)
    skip = max(0, n - max(1, int(req.get("max_bars", MAX_BARS))))
    d = min(int(meta.get("priceHint") or 2), 8)
    cur = meta.get("currentTradingPeriod") or {}

    def num(x, dd=d):
        return _fmt([x], dd)[1:-1] if isinstance(x, (int, float)) else "null"

    def period(p):
        return f'{{"start":{int(p["start"])},"end":{int(p["end"])}}}' if isinstance(p, dict) and "start" in p else "null"

    head = {"sym": meta.get("symbol"), "interval": req.get("interval", ""), "name": meta.get("longName") or meta.get("shortName"),
            "currency": meta.get("currency"), "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
            "tz": meta.get("exchangeTimezoneName")}
    parts = ['{"v":1']
    parts += [f'"{k}":{json.dumps(v, ensure_ascii=False)}' for k, v in head.items()]
    mt = meta.get("regularMarketTime")
    parts += [f'"gmtoffset":{off}', f'"decimals":{d}', f'"intraday":{"true" if unit == "minute" else "false"}',
              f'"price":{num(meta.get("regularMarketPrice"))}', f'"prev_close":{num(meta.get("previousClose"))}',
              f'"market_time":{int(mt) if isinstance(mt, (int, float)) else "null"}',
              f'"regular":{period(cur.get("regular"))}', f'"n":{n - skip}', f'"total":{n}',
              '"dt":[' + ",".join(str(int(x)) for x in np.diff(t[skip:], prepend=0)) + "]"]
    for name, arr, dd in (("o", o, d), ("h", h, d), ("l", l, d), ("c", c, d), ("vol", _significant(vol, 3), 0),
                          ("sma50", sma50, d), ("sma100", sma100, d), ("sma200", sma200, d),
                          ("ema8", ema8, d), ("ema21", ema21, d), ("rsi", rsi14, 1),
                          ("macd", line, d + 1), ("signal", sig, d + 1)):
        parts.append(f'"{name}":' + _fmt(arr[skip:].tolist(), dd))
    body = (",".join(parts) + "}").encode("utf-8")
    level = int(req.get("gzip", GZIP_LEVEL))
    gz = b""
    if level:
        co = zlib.compressobj(level, zlib.DEFLATED, 31)  # wbits 31: a gzip wrapper
        gz = co.compress(body) + co.flush()
    out_meta = {"bars": n - skip, "bars_in": bars_in, "last_t": int(t[-1]) if n else None, "market_time": mt,
                "regular": cur.get("regular"), "pre": cur.get("pre"), "post": cur.get("post"),
                "granularity": meta.get("dataGranularity"), "instrument": meta.get("instrumentType"),
                "body_bytes": len(body), "gz_bytes": len(gz), "build_us": int((time.perf_counter() - t0) * 1e6)}
    return Built(body, gz, out_meta, False)


if __name__ == "__main__":  # python -m ui.tvdata [FILE.json CODE]: which builder the portal uses; time one build
    import sys
    print(f"native bars builder: {'yes' if native() else 'no — ' + str(native_unavailable_reason())}")
    if len(sys.argv) >= 3:
        data = open(sys.argv[1], "rb").read()
        for use in ([True] if native() else []) + [False]:
            b = build(data, sys.argv[2], use_native=use)
            print(f"{'native' if use else 'python'}: {b.meta['bars']} bars, {len(b.body) // 1024} KB "
                  f"({len(b.gz) // 1024} KB gzip) in {b.meta['build_us'] / 1000:.2f} ms")
